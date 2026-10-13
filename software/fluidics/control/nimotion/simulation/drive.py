"""
Simulated NiMotion drive

One integrated servo that answers register reads and writes the way the drive
does: the CiA402 state machine, profile position (PP), profile velocity (PV)
and homing (HM) modes, and a carriage that travels between two limit switches.
Behaviour follows manual B06 (documents/); where the manual is silent the
simulator does the simplest plausible thing.

It is for exercising software without the rig, not for predicting the rig:

- Positions are in user units of the configured axis, and every homing method
  ends at the low end of the stroke (position 0). 607Eh (polarity) only sets
  the sign of 606Ch: with 0x80 it reads in the motor's direction, opposite to
  the position (measured on the Y stage).
- Addresses the simulator does not know read as 0 and accept writes.
- Writing 6083h stores at most 60C5h, and 6084h / 6085h at most 60C6h, as
  the X stage's drive does (factory limits 500000). Speed limits (607Fh,
  6080h) are not applied.
- Following error, torque and bus voltage are plausible readings, not a model.
- An axis whose configuration gives no input a limit function (the T5 tilt)
  has no limit switches and no end stops.
- 6063h reads the carriage in encoder units; homing does not change it.
  Position restore is not modelled; 607Dh is stored, not enforced.
- rejected_writes and read_overrides let tests make a write fail or a
  read-back differ.
"""

import math
import random
from typing import Dict, List, Optional, Set, Tuple

from ..high_level_api.axis_config import AxisConfig
from ..motor_control.registers import Registers, StatusWordBits

# Objects the simulator needs that servo_service does not use itself (B06 ch. 10)
CONTROL_MODE = 0x00B1  # 2002h:01h, 0 = CiA402
QUICK_STOP_OPTION = 0x03BF  # 605Ah
HALT_OPTION = 0x03C0  # 605Dh
FAULT_REACTION_OPTION = 0x03C1  # 605Eh
HOME_OFFSET = Registers.HOME_OFFSET.address  # 607Ch, INT32

# Alarm codes (603Fh, B06 table 8-1)
FAULT_FOLLOWING_ERROR = 0x8611
FAULT_HOMING_TIMEOUT = 0x8610
WARNING_LIMIT_SWITCH = 0x8614

# Modbus exception codes
ILLEGAL_DATA_ADDRESS = 0x02
ILLEGAL_DATA_VALUE = 0x03

SAVE_COMMAND = 0x65766173  # "save", written to 1010h:01

STEP = 0.001  # integration step (s)
MAX_CATCH_UP = 60.0  # longest gap simulated in one go (s)
LIMIT_MARGIN_MM = 1.0  # far limit switch sits this far past stroke_max
END_STOP_MM = 5.0  # end stops sit this far past each limit switch
DI_FUNCTION_NEGATIVE_LIMIT = 15
DI_FUNCTION_POSITIVE_LIMIT = 14
DO_FUNCTION_BRAKE = 5


class SimulatedModbusError(Exception):
    """A request the drive answers with a Modbus exception response"""

    def __init__(self, code: int) -> None:
        super().__init__(f"Modbus exception {code:#04x}")
        self.code = code


class State:
    """CiA402 states, as the status word bits 0-3, 5, 6 show them"""

    SWITCH_ON_DISABLED = 0x0040
    READY_TO_SWITCH_ON = 0x0021
    SWITCHED_ON = 0x0023
    OPERATION_ENABLED = 0x0027
    QUICK_STOP_ACTIVE = 0x0007
    FAULT = 0x0008


MODE_PP, MODE_PV, MODE_HM = 1, 3, 6


def _s32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x1_0000_0000 if value & 0x8000_0000 else value


def _s16(value: int) -> int:
    value &= 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


def _s8(value: int) -> int:
    value &= 0xFF
    return value - 0x100 if value & 0x80 else value


class SimulatedDrive:
    """One simulated drive and the axis it moves. Not thread-safe; the bus locks."""

    def __init__(self, config: AxisConfig, now: float = 0.0, seed: int = 0) -> None:
        self.config = config
        self.slave_id = config.slave_id
        self.online = True
        self._rng = random.Random(seed or config.slave_id)
        self._time = now

        ppm = config.pulses_per_mm
        self._ppm = ppm
        self._vertical = config.has_brake
        # Carriage frame: 0 is the low limit switch's edge, so a homed axis
        # reads 0 there (plus 607Ch)
        self._far_switch = (config.stroke_max + LIMIT_MARGIN_MM) * ppm
        self._low_stop = -END_STOP_MM * ppm
        self._far_stop = self._far_switch + END_STOP_MM * ppm
        self._low_di, self._far_di = self._wiring(config)
        self._switches = self._has_limit_switches(config)

        self._words: Dict[int, int] = {}
        self._load_defaults()

        self._carriage = float(round((config.stroke_min + config.stroke_max) / 2 * ppm))
        self._offset = 0.0  # reported position = carriage + offset
        self._velocity = 0.0
        self._accel = 0.0

        self._state = State.SWITCH_ON_DISABLED
        self._fault_code = 0
        self._control_word = 0
        self._mode = MODE_PP

        # PP
        self._pp_target: Optional[float] = None  # carriage frame
        self._pp_pending: Optional[float] = None
        self._set_point_ack = False
        # HM
        self._homing_phase: Optional[str] = None
        self._homing_started = 0.0
        self._homing_attained = False
        self._homing_error = False
        self._stall_since: Optional[float] = None
        self._saved = False
        # Test hooks: writes to these addresses are refused; reads return these values
        self.rejected_writes: Set[int] = set()
        self.read_overrides: Dict[int, int] = {}

    # ------------------------------------------------------------ set-up

    @staticmethod
    def _wiring(config: AxisConfig) -> Tuple[int, int]:
        """
        DI inputs wired to the (low, far) limit switches: the DIs the axis
        configuration gives limit functions, the one its homing method looks
        for at the low end. Falls back to DI2 / DI3.
        """
        functions = {
            n: f
            for n, f in (
                (1, config.di1_function),
                (2, config.di2_function),
                (3, config.di3_function),
            )
            if f in (DI_FUNCTION_NEGATIVE_LIMIT, DI_FUNCTION_POSITIVE_LIMIT)
        }
        homes_positive = config.homing_method in (18, 23, 24, 25, 26)
        low_function = DI_FUNCTION_POSITIVE_LIMIT if homes_positive else DI_FUNCTION_NEGATIVE_LIMIT
        low = next((n for n, f in functions.items() if f == low_function), None)
        far = next((n for n, f in functions.items() if f != low_function), None)
        if low is None or far is None:
            return 2, 3
        return low, far

    @staticmethod
    def _has_limit_switches(config: AxisConfig) -> bool:
        """Whether the configuration gives some input a limit function (the tilt gives none)"""
        limits = (DI_FUNCTION_NEGATIVE_LIMIT, DI_FUNCTION_POSITIVE_LIMIT)
        return any(
            f in limits for f in (config.di1_function, config.di2_function, config.di3_function)
        )

    def _load_defaults(self) -> None:
        """Factory settings (B06 object dictionary)"""
        r = Registers
        for address, value in (
            (CONTROL_MODE, 4),
            (QUICK_STOP_OPTION, 2),
            (HALT_OPTION, 1),
            (FAULT_REACTION_OPTION, 0),
            (r.MODES_OF_OPERATION.address, MODE_PP),
            (r.HOMING_METHOD.address, 20),
            (r.HOMING_TIMEOUT.address, 10000),
            (r.BLOCKING_TORQUE.address, 500),
            (r.BLOCKING_TIME.address, 500),
            (r.RATED_VOLTAGE.address, 48),
            (r.SLAVE_ADDRESS.address, self.slave_id),
            (r.UNDER_VOLTAGE_POINT.address, 24),
            (r.OVER_VOLTAGE_POINT.address, 100),
            (r.POSITION_RESTORE_MODE.address, 0),
        ):
            self._words[address] = value & 0xFFFF
        for address, value in (
            (r.MAX_PROFILE_VELOCITY.address, 500000),
            (r.MAX_ACCELERATION.address, 500000),
            (r.MAX_DECELERATION.address, 500000),
            (r.PROFILE_VELOCITY.address, 500000),
            (r.PROFILE_ACCELERATION.address, 409600),
            (r.PROFILE_DECELERATION.address, 409600),
            (r.QUICK_STOP_DECELERATION.address, 500000),
            (r.HOMING_SPEED_HIGH.address, 10000),
            (r.HOMING_SPEED_LOW.address, 2730),
            (r.HOMING_ACCELERATION.address, 409600),
            (r.ENCODER_RESOLUTION_NUM.address, 10000),
            (r.ENCODER_RESOLUTION_DEN.address, 1),
            (r.GEAR_RATIO_NUM.address, 1),
            (r.GEAR_RATIO_DEN.address, 1),
            (r.ENCODER_BITS.address, 17),
            (r.SOFTWARE_LIMIT_MIN.address, -65535),
            (r.SOFTWARE_LIMIT_MAX.address, 65535),
            (r.POSITION_RANGE_MIN.address, -1048576),
            (r.POSITION_RANGE_MAX.address, 1048576),
        ):
            self._set32(address, value)

    # ------------------------------------------------------------ register access

    _READ_ONLY = frozenset(
        a
        for reg in (
            Registers.STATUS_WORD,
            Registers.MODES_OF_OPERATION_DISPLAY,
            Registers.ERROR_CODE,
            Registers.POSITION_ACTUAL_VALUE,
            Registers.POSITION_ACTUAL_INTERNAL,
            Registers.VELOCITY_ACTUAL_VALUE,
            Registers.TORQUE_ACTUAL_VALUE,
            Registers.FOLLOWING_ERROR_ACTUAL,
            Registers.DIGITAL_INPUTS,
            Registers.DIGITAL_OUTPUTS,
            Registers.DI_PHYSICAL_STATE,
            Registers.DO_PHYSICAL_STATE,
            Registers.BUS_VOLTAGE,
        )
        for a in range(reg.address, reg.address + reg.size)
    )

    # (register, the limit the drive caps it at when it is written)
    _CAPPED = (
        (Registers.PROFILE_ACCELERATION, Registers.MAX_ACCELERATION),
        (Registers.PROFILE_DECELERATION, Registers.MAX_DECELERATION),
        (Registers.QUICK_STOP_DECELERATION, Registers.MAX_DECELERATION),
    )

    def _get32(self, address: int) -> int:
        return (self._words.get(address, 0) << 16) | self._words.get(address + 1, 0)

    def _set32(self, address: int, value: int) -> None:
        value &= 0xFFFFFFFF
        self._words[address] = value >> 16
        self._words[address + 1] = value & 0xFFFF

    def read(self, address: int, count: int) -> List[int]:
        """Holding registers address .. address+count-1"""
        live = self._live_values()
        return [
            self.read_overrides.get(a, live.get(a, self._words.get(a, 0)))
            for a in range(address, address + count)
        ]

    def write(self, address: int, values: List[int]) -> None:
        """Write registers starting at address, then act on what changed"""
        addresses = range(address, address + len(values))
        if any(a in self.rejected_writes for a in addresses):
            raise SimulatedModbusError(ILLEGAL_DATA_VALUE)
        if any(a in self._READ_ONLY for a in addresses):
            raise SimulatedModbusError(ILLEGAL_DATA_ADDRESS)
        control = Registers.CONTROL_WORD.address
        mode = Registers.MODES_OF_OPERATION.address
        if mode in addresses and _s8(values[mode - address]) not in range(0, 11):
            raise SimulatedModbusError(ILLEGAL_DATA_VALUE)
        for a, value in zip(addresses, values):
            if a != control:
                self._words[a] = value & 0xFFFF
        for reg, limit in self._CAPPED:
            if reg.address in addresses:
                self._set32(reg.address, min(self._get32(reg.address), self._get32(limit.address)))
        if mode in addresses:
            self._set_mode(_s8(values[mode - address]))
        if Registers.SAVE_PARAMETERS.address in addresses:
            self._save()
        if control in addresses:
            self._command(values[control - address] & 0xFFFF)

    def _live_values(self) -> Dict[int, int]:
        """Registers computed from the simulation rather than stored"""
        r = Registers
        values = {
            r.CONTROL_WORD.address: self._control_word,
            r.STATUS_WORD.address: self.status_word(),
            r.MODES_OF_OPERATION_DISPLAY.address: self._mode & 0xFF,
            r.ERROR_CODE.address: self.error_code(),
            r.TORQUE_ACTUAL_VALUE.address: self._torque() & 0xFFFF,
            r.DI_PHYSICAL_STATE.address: self._di_levels(),
            r.DO_PHYSICAL_STATE.address: self._do_levels(),
            r.BUS_VOLTAGE.address: 483 + self._rng.randint(-2, 2),
            r.SAVE_PARAMETERS.address: 0,
            r.SAVE_PARAMETERS.address + 1: 1 if self._saved else 0,
        }
        for reg, value in (
            (r.POSITION_ACTUAL_VALUE, self.position()),
            (r.POSITION_ACTUAL_INTERNAL, self._internal_position()),
            (r.VELOCITY_ACTUAL_VALUE, self._rpm()),
            (r.FOLLOWING_ERROR_ACTUAL, round(self._velocity * 0.002)),
            (r.DIGITAL_INPUTS, self._digital_inputs()),
            (r.DIGITAL_OUTPUTS, self._do_levels()),
        ):
            value &= 0xFFFFFFFF
            values[reg.address] = value >> 16
            values[reg.address + 1] = value & 0xFFFF
        return values

    # ------------------------------------------------------------ readings

    def position(self) -> int:
        """6064h, user units"""
        return round(self._carriage + self._offset)

    def _internal_position(self) -> int:
        """6063h: the carriage in encoder units (6064h x 6091h), unaffected by homing"""
        r = Registers
        gear = self._get32(r.GEAR_RATIO_NUM.address) / max(1, self._get32(r.GEAR_RATIO_DEN.address))
        return round(self._carriage * gear)

    def _rpm(self) -> int:
        """606Ch: motor speed in rpm (B06: 6081h x 6091h x 60 / 608Fh)"""
        r = Registers
        resolution = self._get32(r.ENCODER_RESOLUTION_NUM.address) / max(
            1, self._get32(r.ENCODER_RESOLUTION_DEN.address)
        )
        gear = self._get32(r.GEAR_RATIO_NUM.address) / max(1, self._get32(r.GEAR_RATIO_DEN.address))
        polarity = self._words.get(r.POLARITY.address, 0)
        sign = -1 if (polarity & 0xC0) == 0x80 else 1  # position polarity does not apply to 606Ch
        return sign * round(self._velocity * gear * 60 / max(1.0, resolution))

    def _torque(self) -> int:
        """6077h in 0.1 % of rated torque: holding load, friction and acceleration"""
        if not self._powered():
            return 0
        torque = 120.0 if self._vertical else 0.0
        if self._velocity:
            torque += math.copysign(40.0, self._velocity)
        torque += 300.0 * self._accel / (3000.0 * self._ppm)
        return max(-1000, min(1000, round(torque) + self._rng.randint(-3, 3)))

    def _switch_levels(self) -> Tuple[bool, bool]:
        """Physical (low, far) switches pressed"""
        if not self._switches:
            return False, False
        return self._carriage < 0.0, self._carriage > self._far_switch

    def _di_levels(self) -> int:
        """200Bh:05h: DI pin levels. Limit switches are NO contacts to 0 V: pressed reads 0."""
        low, far = self._switch_levels()
        levels = 0b111
        for di, pressed in ((self._low_di, low), (self._far_di, far)):
            if pressed:
                levels &= ~(1 << (di - 1))
        return levels

    def _di_active(self, di: int) -> bool:
        """Whether the drive sees DI n as active, given its logic setting (2003h)"""
        level = bool(self._di_levels() & (1 << (di - 1)))
        logic = self._words.get(Registers.DI1_LOGIC.address + 2 * (di - 1), 0)
        return level if logic in (1, 2) else not level

    def _di_function(self, di: int) -> int:
        return self._words.get(Registers.DI1_FUNCTION.address + 2 * (di - 1), 0)

    def _limit_active(self) -> Tuple[bool, bool]:
        """(low, far) limit as the drive sees it: a limit function on an active DI"""
        limits = (DI_FUNCTION_NEGATIVE_LIMIT, DI_FUNCTION_POSITIVE_LIMIT)
        low = self._di_function(self._low_di) in limits and self._di_active(self._low_di)
        far = self._di_function(self._far_di) in limits and self._di_active(self._far_di)
        return low, far

    def _digital_inputs(self) -> int:
        """60FDh: bit 0 negative limit, bit 1 positive limit (CiA402 layout)"""
        value = 0
        for di in (1, 2, 3):
            if not self._di_active(di):
                continue
            function = self._di_function(di)
            if function == DI_FUNCTION_NEGATIVE_LIMIT:
                value |= 0x1
            elif function == DI_FUNCTION_POSITIVE_LIMIT:
                value |= 0x2
        return value

    def _do_levels(self) -> int:
        """200Bh:06h: DO1 drives the brake (released while powered) or follows 2031h:02h"""
        function = self._words.get(Registers.DO1_FUNCTION.address, 0)
        if function == DO_FUNCTION_BRAKE:
            logic = self._words.get(Registers.DO1_LOGIC.address, 1)
            released = self._powered()
            return int(released == bool(logic))
        return self._words.get(Registers.DO_CONTROL.address, 0) & 0x1

    def error_code(self) -> int:
        if self._state == State.FAULT:
            return self._fault_code
        if any(self._limit_active()):
            return WARNING_LIMIT_SWITCH
        return 0

    def status_word(self) -> int:
        bits = StatusWordBits
        word = self._state | bits.VOLTAGE_ENABLED | bits.REMOTE
        stopped = self._velocity == 0.0
        if any(self._limit_active()):
            word |= bits.WARNING  # bit 11 stays clear (as on the drives)
        halted = bool(self._control_word & 0x0100)
        if self._mode == MODE_HM:
            if self._homing_error:
                word |= bits.HOMING_ERROR
            elif self._homing_phase is not None:
                pass  # in progress: bits 10, 12 clear
            elif self._homing_attained:
                word |= bits.HOMING_ATTAINED | (bits.TARGET_REACHED if stopped else 0)
            else:
                word |= bits.TARGET_REACHED  # interrupted or not started
        elif self._mode == MODE_PV:
            target = self._pv_target()
            if stopped:
                word |= bits.SET_POINT_ACK  # bit 12: zero speed
            if (halted and stopped) or abs(self._velocity - target) <= 0.01 * abs(target) + 1:
                word |= bits.TARGET_REACHED
        else:
            if self._set_point_ack:
                word |= bits.SET_POINT_ACK
            if stopped and (self._pp_target is None or halted):
                word |= bits.TARGET_REACHED
        return word

    # ------------------------------------------------------------ commands

    def _powered(self) -> bool:
        return self._state in (State.OPERATION_ENABLED, State.QUICK_STOP_ACTIVE)

    def _command(self, word: int) -> None:
        """Control word 6040h: state transitions, then mode-specific bits"""
        previous, self._control_word = self._control_word, word
        rising = word & ~previous
        falling = previous & ~word
        state = self._state

        if state == State.FAULT:
            if rising & 0x0080:
                self._state = State.SWITCH_ON_DISABLED
                self._fault_code = 0
            return

        if not word & 0x0002:  # disable voltage
            self._power_off(State.SWITCH_ON_DISABLED)
        elif not word & 0x0004:  # quick stop
            if state == State.OPERATION_ENABLED:
                self._state = State.QUICK_STOP_ACTIVE
                self._cancel_motion()
            elif state != State.QUICK_STOP_ACTIVE:
                self._state = State.SWITCH_ON_DISABLED
        elif not word & 0x0001:  # shutdown
            if state in (State.SWITCH_ON_DISABLED, State.SWITCHED_ON, State.OPERATION_ENABLED):
                self._power_off(State.READY_TO_SWITCH_ON)
        elif not word & 0x0008:  # switch on / disable operation
            if state in (State.READY_TO_SWITCH_ON, State.OPERATION_ENABLED):
                self._power_off(State.SWITCHED_ON)
        elif state in (State.READY_TO_SWITCH_ON, State.SWITCHED_ON):  # enable operation
            self._state = State.OPERATION_ENABLED

        if self._state != State.OPERATION_ENABLED:
            self._set_point_ack = False
            return

        if falling & 0x0010:
            self._set_point_ack = False
        if rising & 0x0010:
            if self._mode == MODE_PP and not self._set_point_ack:
                self._accept_set_point(word)
            elif self._mode == MODE_HM and not word & 0x0100:
                self._start_homing()
        if self._mode == MODE_HM and word & 0x0100:
            self._homing_phase = None  # halt interrupts homing

    def _power_off(self, state: int) -> None:
        """Leave Operation Enabled without a ramp: the brake or friction stops the axis"""
        if self._state == State.OPERATION_ENABLED or self._state == State.QUICK_STOP_ACTIVE:
            self._velocity = 0.0
            self._accel = 0.0
            self._cancel_motion()
        self._state = state

    def _cancel_motion(self) -> None:
        self._pp_target = None
        self._pp_pending = None
        if self._homing_phase is not None:
            self._homing_phase = None

    def _set_mode(self, mode: int) -> None:
        if mode != self._mode:
            self._cancel_motion()
            self._set_point_ack = False
            self._mode = mode

    def _accept_set_point(self, word: int) -> None:
        """
        New set-point (B06 5.3.3): acknowledged with status bit 12. Bit 5
        replaces the move in progress; otherwise the set-point waits for it.
        """
        target = _s32(self._get32(Registers.TARGET_POSITION.address))
        if word & 0x0040:  # relative to the move in progress, or to where the axis is
            base = self._pp_target if self._pp_target is not None else self._carriage
            goal = base + target
        else:
            goal = target - self._offset
        self._set_point_ack = True
        if self._pp_target is None or word & 0x0020:
            self._pp_target = goal
            self._pp_pending = None
        else:
            self._pp_pending = goal

    def _start_homing(self) -> None:
        method = _s8(self._words.get(Registers.HOMING_METHOD.address, 0))
        self._homing_attained = False
        self._homing_error = False
        self._stall_since = None
        self._homing_started = self._time
        low, _far = self._limit_active()
        if method == 35:
            self._set_home()
        elif method in (37, 38):
            self._homing_phase = "blocking"
        elif 17 <= method <= 30:
            self._homing_phase = "backoff" if low else "search"
        else:
            self._homing_error = True

    def _set_home(self) -> None:
        home_offset = _s32(self._get32(HOME_OFFSET))
        self._offset = home_offset - self._carriage
        self._homing_phase = None
        self._homing_attained = True

    def _save(self) -> None:
        """1010h:01 = "save" stores the parameters; refused while powered (B06 6.6.1)"""
        if self._get32(Registers.SAVE_PARAMETERS.address) == SAVE_COMMAND:
            self._saved = not self._powered()

    def inject_fault(self, code: int) -> None:
        """Put the drive into Fault with the given 603Fh code"""
        self._state = State.FAULT
        self._fault_code = code & 0xFFFF
        self._velocity = 0.0
        self._accel = 0.0
        self._cancel_motion()

    # ------------------------------------------------------------ motion

    def advance(self, now: float) -> None:
        """Run the simulation up to `now` (seconds, the bus clock)"""
        remaining = min(now - self._time, MAX_CATCH_UP)
        while remaining > 1e-9:
            dt = min(STEP, remaining)
            if self._velocity == 0.0 and self._goal_velocity()[0] == 0.0:
                if self._homing_phase is None:
                    self._time += remaining  # nothing will change
                    self._settle()
                    break
            self._time += dt
            self._step(dt)
            remaining -= dt
        self._time = max(self._time, now)

    def _pv_target(self) -> float:
        return float(_s32(self._get32(Registers.TARGET_VELOCITY.address)))

    def _rate(self, register: int) -> float:
        return float(max(1, self._get32(register)))

    def _goal_velocity(self) -> Tuple[float, float, float]:
        """(velocity the axis heads for, acceleration, deceleration)"""
        r = Registers
        accel = self._rate(r.PROFILE_ACCELERATION.address)
        decel = self._rate(r.PROFILE_DECELERATION.address)
        quick = self._rate(r.QUICK_STOP_DECELERATION.address)
        if self._state == State.QUICK_STOP_ACTIVE:
            option = self._words.get(QUICK_STOP_OPTION, 2)
            return 0.0, quick, decel if option == 1 else quick
        if self._state != State.OPERATION_ENABLED:
            return 0.0, accel, decel
        if self._control_word & 0x0100:  # halt
            option = self._words.get(HALT_OPTION, 1)
            return 0.0, accel, quick if option == 2 else decel

        if self._mode == MODE_HM:
            homing_accel = self._rate(r.HOMING_ACCELERATION.address)
            if self._homing_phase is None:
                return 0.0, homing_accel, homing_accel
            high = float(self._get32(r.HOMING_SPEED_HIGH.address))
            low = abs(float(_s32(self._get32(r.HOMING_SPEED_LOW.address))))
            speed = {"search": -high, "backoff": low, "blocking": -low}[self._homing_phase]
            return speed, homing_accel, homing_accel
        if self._mode == MODE_PV:
            return self._pv_target(), accel, decel
        if self._mode == MODE_PP and self._pp_target is not None:
            vmax = float(self._get32(r.PROFILE_VELOCITY.address))
            remaining = self._pp_target - self._carriage
            speed = min(vmax, math.sqrt(2 * decel * abs(remaining)))
            return math.copysign(speed, remaining), accel, decel
        return 0.0, accel, decel

    def _step(self, dt: float) -> None:
        goal, accel, decel = self._goal_velocity()
        v = self._velocity
        low_limit, far_limit = self._limit_active()
        homing = self._mode == MODE_HM and self._homing_phase is not None
        # Limit reaction (alarm 8614h, "halt and lock"): stop at the quick-stop
        # ramp. As on the drives, a move into the limit stays unfinished (target
        # reached stays clear) until a new set-point leads away from it.
        if not homing and (low_limit and min(goal, v) < 0 or far_limit and max(goal, v) > 0):
            if low_limit and goal < 0 or far_limit and goal > 0:
                goal = 0.0
            decel = self._rate(Registers.QUICK_STOP_DECELERATION.address)

        speeding_up = abs(goal) > abs(v) and (
            v == 0 or math.copysign(1, goal) == math.copysign(1, v)
        )
        rate = accel if speeding_up else decel
        change = max(-rate * dt, min(rate * dt, goal - v))
        new_v = v + change
        self._accel = change / dt
        start = self._carriage
        self._carriage += (v + new_v) / 2 * dt
        self._velocity = new_v

        if self._pp_target is not None and self._mode == MODE_PP:
            before, after = self._pp_target - start, self._pp_target - self._carriage
            if (before == 0 or after == 0 or (before > 0) != (after > 0)) or (
                abs(after) < 0.5 and abs(self._velocity) < decel * dt * 2
            ):
                self._carriage = self._pp_target
                self._velocity = 0.0
                self._pp_target, self._pp_pending = self._pp_pending, None

        self._end_stops()
        if homing:
            self._homing_step()
        if self._velocity != 0.0 and abs(self._velocity) < 1e-6:
            self._velocity = 0.0
        self._settle()

    def _end_stops(self) -> None:
        """Hitting an end stop stalls the motor: following error, unless homing on it"""
        if not self._switches:
            return  # no end stops either (the tilt)
        if self._low_stop <= self._carriage <= self._far_stop:
            self._stall_since = None
            return
        self._carriage = max(self._low_stop, min(self._far_stop, self._carriage))
        self._velocity = 0.0
        if self._homing_phase == "blocking":
            if self._stall_since is None:
                self._stall_since = self._time
        elif self._state == State.OPERATION_ENABLED:
            self.inject_fault(FAULT_FOLLOWING_ERROR)

    def _homing_step(self) -> None:
        low, far = self._limit_active()
        timeout = self._words.get(Registers.HOMING_TIMEOUT.address, 0) / 1000.0
        if self._time - self._homing_started > timeout:
            self._homing_phase = None
            self._homing_error = True
            self.inject_fault(FAULT_HOMING_TIMEOUT)
        elif far:
            self._homing_phase = None
            self._homing_error = True
        elif self._homing_phase == "search" and low:
            self._homing_phase = "backoff"
        elif self._homing_phase == "backoff" and not low:
            self._set_home()
        elif self._homing_phase == "blocking" and self._stall_since is not None:
            blocking_time = self._words.get(Registers.BLOCKING_TIME.address, 0) / 1000.0
            if self._time - self._stall_since >= blocking_time:
                self._set_home()

    def _settle(self) -> None:
        """Transitions that happen once the axis is at rest"""
        if self._velocity != 0.0:
            return
        self._accel = 0.0
        if self._state == State.QUICK_STOP_ACTIVE:
            self._state = State.SWITCH_ON_DISABLED
        if (
            self._mode == MODE_PP
            and self._pp_target is not None
            and abs(self._pp_target - self._carriage) < 0.5
        ):
            self._carriage = self._pp_target
            self._pp_target, self._pp_pending = self._pp_pending, None
