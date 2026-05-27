import logging
import time
from dataclasses import dataclass
from enum import Enum, IntEnum

from .modbus_rtu import ModbusRTUClient

logger = logging.getLogger(__name__)


# --- CiA402 Register Constants ---

class Reg:
    CONTROL_WORD = 0x0380
    STATUS_WORD = 0x0381
    MODES_OF_OPERATION = 0x03C2
    MODES_OF_OPERATION_DISPLAY = 0x03C3
    TARGET_POSITION = 0x03E7
    POSITION_ACTUAL_VALUE = 0x03C8
    TARGET_VELOCITY = 0x0448
    VELOCITY_ACTUAL_VALUE = 0x03D5
    PROFILE_VELOCITY = 0x03F8
    PROFILE_ACCELERATION = 0x03FC
    PROFILE_DECELERATION = 0x03FE
    HOMING_METHOD = 0x0416
    HOMING_SPEED_HIGH = 0x0417
    HOMING_SPEED_LOW = 0x0419
    HOMING_ACCELERATION = 0x041B
    HOMING_TIMEOUT = 0x012E
    BLOCKING_TORQUE = 0x0170
    BLOCKING_TIME = 0x0172
    ERROR_CODE = 0x0382
    ENCODER_RESOLUTION_NUM = 0x0406
    ENCODER_RESOLUTION_DEN = 0x0408
    GEAR_RATIO_NUM = 0x040C
    GEAR_RATIO_DEN = 0x040E
    POLARITY = 0x03F3
    DI1_FUNCTION = 0x00D5
    DI1_LOGIC = 0x00D6
    DI2_FUNCTION = 0x00D7
    DI2_LOGIC = 0x00D8
    DI3_FUNCTION = 0x00D9
    DI3_LOGIC = 0x00DA
    DO1_FUNCTION = 0x00F8
    DO1_LOGIC = 0x00F9


# --- Control Word / Status Word Bits ---

class ControlWordBits:
    SWITCH_ON = 0x0001
    ENABLE_VOLTAGE = 0x0002
    QUICK_STOP = 0x0004
    ENABLE_OPERATION = 0x0008
    NEW_SET_POINT = 0x0010
    CHANGE_SET_IMMEDIATELY = 0x0020
    ABS_REL = 0x0040
    FAULT_RESET = 0x0080
    HALT = 0x0100

    CMD_SHUTDOWN = 0x0006
    CMD_SWITCH_ON = 0x0007
    CMD_ENABLE_OPERATION = 0x000F
    CMD_DISABLE_OPERATION = 0x0007
    CMD_DISABLE_VOLTAGE = 0x0000
    CMD_QUICK_STOP = 0x0002


class StatusWordBits:
    READY_TO_SWITCH_ON = 0x0001
    SWITCHED_ON = 0x0002
    OPERATION_ENABLED = 0x0004
    FAULT = 0x0008
    VOLTAGE_ENABLED = 0x0010
    QUICK_STOP = 0x0020
    SWITCH_ON_DISABLED = 0x0040
    WARNING = 0x0080
    REMOTE = 0x0200
    TARGET_REACHED = 0x0400
    INTERNAL_LIMIT_ACTIVE = 0x0800
    SET_POINT_ACK = 0x1000
    FOLLOWING_ERROR = 0x2000

    STATE_MASK = 0x006F


# --- Operation Mode / Homing Method ---

class OperationMode(IntEnum):
    PROFILE_POSITION = 1
    VELOCITY = 2
    PROFILE_VELOCITY = 3
    PROFILE_TORQUE = 4
    HOMING = 6
    INTERPOLATED_POSITION = 7
    CYCLIC_SYNC_POSITION = 8
    CYCLIC_SYNC_VELOCITY = 9
    CYCLIC_SYNC_TORQUE = 10


class HomingMethod(IntEnum):
    NO_HOMING = 0
    POSITIVE_LIMIT_SWITCH = 1
    POSITIVE_LIMIT_SWITCH_AND_INDEX = 2
    HOME_SWITCH_POSITIVE = 7
    HOME_SWITCH_POSITIVE_AND_INDEX = 11
    NEGATIVE_LIMIT_SWITCH = 17
    NEGATIVE_LIMIT_SWITCH_AND_INDEX = 18
    HOME_SWITCH_NEGATIVE = 23
    HOME_SWITCH_NEGATIVE_AND_INDEX = 27
    CURRENT_POSITIVE = 33
    CURRENT_NEGATIVE = 34
    CURRENT_POSITION_AS_HOME = 35


# --- Drive State Machine ---

class DriveState(Enum):
    NOT_READY_TO_SWITCH_ON = "Not ready to switch on"
    SWITCH_ON_DISABLED = "Switch on disabled"
    READY_TO_SWITCH_ON = "Ready to switch on"
    SWITCHED_ON = "Switched on"
    OPERATION_ENABLED = "Operation enabled"
    QUICK_STOP_ACTIVE = "Quick stop active"
    FAULT_REACTION_ACTIVE = "Fault reaction active"
    FAULT = "Fault"
    UNKNOWN = "Unknown"


class DriveCommand(Enum):
    SHUTDOWN = "Shutdown"
    SWITCH_ON = "Switch on"
    ENABLE_OPERATION = "Enable operation"
    DISABLE_OPERATION = "Disable operation"
    DISABLE_VOLTAGE = "Disable voltage"
    QUICK_STOP = "Quick stop"
    FAULT_RESET = "Fault reset"


def decode_status_word(status_word: int) -> DriveState:
    state_bits = status_word & StatusWordBits.STATE_MASK

    if status_word & StatusWordBits.FAULT:
        if (state_bits & 0x004F) == 0x000F:
            return DriveState.FAULT_REACTION_ACTIVE
        return DriveState.FAULT

    if (state_bits & 0x006F) == 0x0027:
        return DriveState.OPERATION_ENABLED
    if (state_bits & 0x006F) == 0x0007:
        return DriveState.QUICK_STOP_ACTIVE
    if (state_bits & 0x006F) == 0x0023:
        return DriveState.SWITCHED_ON
    if (state_bits & 0x006F) == 0x0021:
        return DriveState.READY_TO_SWITCH_ON
    if (state_bits & 0x004F) == 0x0040:
        return DriveState.SWITCH_ON_DISABLED
    if (state_bits & 0x004F) == 0x0000:
        return DriveState.NOT_READY_TO_SWITCH_ON

    return DriveState.UNKNOWN


def get_transition_path(
    current_state: DriveState,
    target_state: DriveState,
) -> list[tuple[DriveCommand, int]]:
    if current_state == target_state:
        return []

    if current_state == DriveState.FAULT:
        path = [(DriveCommand.FAULT_RESET, ControlWordBits.FAULT_RESET)]
        remaining = get_transition_path(DriveState.SWITCH_ON_DISABLED, target_state)
        return path + remaining

    state_levels = {
        DriveState.NOT_READY_TO_SWITCH_ON: 0,
        DriveState.SWITCH_ON_DISABLED: 1,
        DriveState.READY_TO_SWITCH_ON: 2,
        DriveState.SWITCHED_ON: 3,
        DriveState.OPERATION_ENABLED: 4,
        DriveState.QUICK_STOP_ACTIVE: 4,
        DriveState.FAULT: 0,
        DriveState.FAULT_REACTION_ACTIVE: 0,
    }

    current_level = state_levels.get(current_state, 0)
    target_level = state_levels.get(target_state, 0)

    path: list[tuple[DriveCommand, int]] = []

    if target_level > current_level:
        if current_state == DriveState.SWITCH_ON_DISABLED:
            path.append((DriveCommand.SHUTDOWN, ControlWordBits.CMD_SHUTDOWN))
            current_state = DriveState.READY_TO_SWITCH_ON

        if current_state == DriveState.READY_TO_SWITCH_ON and target_level >= 3:
            path.append((DriveCommand.SWITCH_ON, ControlWordBits.CMD_SWITCH_ON))
            current_state = DriveState.SWITCHED_ON

        if current_state == DriveState.SWITCHED_ON and target_level >= 4:
            path.append((DriveCommand.ENABLE_OPERATION, ControlWordBits.CMD_ENABLE_OPERATION))

    else:
        if current_state == DriveState.OPERATION_ENABLED:
            if target_level <= 3:
                path.append((DriveCommand.DISABLE_OPERATION, ControlWordBits.CMD_DISABLE_OPERATION))
                current_state = DriveState.SWITCHED_ON

        if current_state == DriveState.SWITCHED_ON:
            if target_level <= 2:
                path.append((DriveCommand.SHUTDOWN, ControlWordBits.CMD_SHUTDOWN))
                current_state = DriveState.READY_TO_SWITCH_ON

        if current_state == DriveState.READY_TO_SWITCH_ON:
            if target_level <= 1:
                path.append((DriveCommand.DISABLE_VOLTAGE, ControlWordBits.CMD_DISABLE_VOLTAGE))

        if current_state == DriveState.QUICK_STOP_ACTIVE:
            path.append((DriveCommand.DISABLE_VOLTAGE, ControlWordBits.CMD_DISABLE_VOLTAGE))
            if target_level > 1:
                remaining = get_transition_path(DriveState.SWITCH_ON_DISABLED, target_state)
                return path + remaining

    return path


# --- Axis Config ---

@dataclass
class AxisConfig:
    slave_id: int
    encoder_resolution: int = 10000
    gear_ratio_numerator: int = 1
    gear_ratio_denominator: int = 1
    ball_screw_lead: float = 10.0
    stroke_min: float = 0.0
    stroke_max: float = 61.0
    default_velocity: float = 100.0
    default_acceleration: float = 500.0
    default_deceleration: float = 500.0
    max_velocity: float = 500.0
    homing_method: int = 17
    homing_velocity_high: float = 20.0
    homing_velocity_low: float = 4.0
    homing_acceleration: float = 100.0
    homing_timeout: int = 60000
    velocity_polarity: int = 1
    driver_polarity: int = 0x00
    di1_function: int | None = None
    di1_logic: int = 0
    di2_function: int | None = None
    di2_logic: int = 0
    di3_function: int | None = None
    di3_logic: int = 0
    blocking_torque: int = 300
    blocking_time: int = 500
    has_brake: bool = False
    brake_do_logic: int = 1
    brake_release_delay_ms: int = 500

    @property
    def pulses_per_mm(self) -> float:
        return self.encoder_resolution / self.ball_screw_lead

    def mm_to_pulses(self, mm: float) -> int:
        return int(mm * self.pulses_per_mm)

    def pulses_to_mm(self, pulses: int) -> float:
        return pulses / self.pulses_per_mm

    def velocity_mm_to_pulses(self, mm_s: float) -> int:
        return int(mm_s * self.pulses_per_mm)

    def velocity_pulses_to_mm(self, pulses_s: int) -> float:
        return pulses_s / self.pulses_per_mm

    def is_position_valid(self, mm: float) -> bool:
        return self.stroke_min <= mm <= self.stroke_max


# --- Default Configs ---

DEFAULT_AXIS_CONFIGS: dict[str, AxisConfig] = {
    "Z": AxisConfig(
        slave_id=4,
        encoder_resolution=131072,
        ball_screw_lead=10.0,
        stroke_min=0.0,
        stroke_max=61.0,
        default_velocity=100.0,
        default_acceleration=500.0,
        default_deceleration=500.0,
        max_velocity=500.0,
        homing_method=17,
        homing_velocity_high=20.0,
        homing_velocity_low=4.0,
        homing_acceleration=100.0,
        homing_timeout=60000,
        velocity_polarity=-1,
        has_brake=True,
        brake_do_logic=1,
        brake_release_delay_ms=500,
        di2_function=15,
        di2_logic=0,
        di3_function=14,
        di3_logic=0,
        blocking_torque=300,
        blocking_time=500,
    ),
}


# --- ServoMotor ---

POLL_INTERVAL = 0.01
STATE_TIMEOUT = 2.0
MOTION_TIMEOUT = 30.0


class ServoMotor:
    def __init__(
        self,
        port: str | None = None,
        baudrate: int = 115200,
        axis_configs: dict[str, AxisConfig] | None = None,
        timeout: float = 0.5,
        retries: int = 3,
    ):
        self._client = ModbusRTUClient(port, baudrate, timeout, retries)
        self._axis_configs = axis_configs or {k: v for k, v in DEFAULT_AXIS_CONFIGS.items()}
        self._homed: dict[str, bool] = {name: False for name in self._axis_configs}
        self.is_aborted = False

    # --- Connection ---

    def connect(self, port: str | None = None, baudrate: int | None = None):
        self._client.connect(port, baudrate)

    def disconnect(self):
        self._client.disconnect()

    @property
    def is_connected(self) -> bool:
        return self._client.is_connected

    # --- Axis resolution ---

    def _resolve_axis(self, axis: str | None) -> tuple[str, AxisConfig, int]:
        if axis is None:
            axis = next(iter(self._axis_configs))
        if axis not in self._axis_configs:
            raise ValueError(f"Unknown axis: {axis}")
        config = self._axis_configs[axis]
        return axis, config, config.slave_id

    # --- Low-level register access ---

    def _read_status_word(self, slave_id: int) -> int:
        return self._client.read_register(slave_id, Reg.STATUS_WORD)

    def _read_control_word(self, slave_id: int) -> int:
        return self._client.read_register(slave_id, Reg.CONTROL_WORD)

    def _write_control_word(self, slave_id: int, value: int):
        self._client.write_register(slave_id, Reg.CONTROL_WORD, value)

    def _get_state(self, slave_id: int) -> DriveState:
        status_word = self._read_status_word(slave_id)
        return decode_status_word(status_word)

    def _wait_for_state(self, slave_id: int, target: DriveState, timeout: float):
        start = time.time()
        while time.time() - start < timeout:
            if self.is_aborted:
                raise RuntimeError("Operation aborted")
            state = self._get_state(slave_id)
            if state == target:
                return
            if state == DriveState.FAULT:
                error = self._client.read_register(slave_id, Reg.ERROR_CODE)
                raise RuntimeError(f"Motor fault during state wait: error 0x{error:04X}")
            time.sleep(POLL_INTERVAL)
        raise TimeoutError(
            f"Timeout waiting for state {target.value} ({timeout}s), "
            f"current: {self._get_state(slave_id).value}"
        )

    def _wait_for_motion(self, slave_id: int, timeout: float):
        start = time.time()
        while time.time() - start < timeout:
            if self.is_aborted:
                raise RuntimeError("Operation aborted")
            status_word = self._read_status_word(slave_id)
            if status_word & StatusWordBits.FAULT:
                error = self._client.read_register(slave_id, Reg.ERROR_CODE)
                raise RuntimeError(f"Motor fault during motion: error 0x{error:04X}")
            if status_word & StatusWordBits.TARGET_REACHED:
                return
            time.sleep(POLL_INTERVAL)
        raise TimeoutError(f"Timeout waiting for motion complete ({timeout}s)")

    def _wait_for_homing(self, slave_id: int, timeout: float):
        start = time.time()
        while time.time() - start < timeout:
            if self.is_aborted:
                raise RuntimeError("Operation aborted")
            status_word = self._read_status_word(slave_id)
            if status_word & StatusWordBits.FAULT:
                error = self._client.read_register(slave_id, Reg.ERROR_CODE)
                raise RuntimeError(f"Motor fault during homing: error 0x{error:04X}")
            if status_word & StatusWordBits.TARGET_REACHED:
                mode = self._client.read_register(slave_id, Reg.MODES_OF_OPERATION_DISPLAY)
                if mode == OperationMode.HOMING:
                    return
            time.sleep(POLL_INTERVAL)
        raise TimeoutError(f"Timeout waiting for homing complete ({timeout}s)")

    # --- Initialization ---

    def initialize_axis(self, axis: str | None = None):
        name, config, sid = self._resolve_axis(axis)
        logger.info(f"Initializing axis {name} (slave {sid})")

        self._client.write_register_32bit(sid, Reg.ENCODER_RESOLUTION_NUM, config.encoder_resolution)
        self._client.write_register_32bit(sid, Reg.ENCODER_RESOLUTION_DEN, 1)
        self._client.write_register_32bit(sid, Reg.GEAR_RATIO_NUM, config.gear_ratio_numerator)
        self._client.write_register_32bit(sid, Reg.GEAR_RATIO_DEN, config.gear_ratio_denominator)
        self._client.write_register(sid, Reg.HOMING_TIMEOUT, config.homing_timeout)
        self._client.write_register(sid, Reg.BLOCKING_TORQUE, config.blocking_torque)
        self._client.write_register(sid, Reg.BLOCKING_TIME, config.blocking_time)

        if config.driver_polarity != 0x00:
            self._client.write_register(sid, Reg.POLARITY, config.driver_polarity)

        di_configs = [
            (config.di1_function, config.di1_logic, Reg.DI1_FUNCTION, Reg.DI1_LOGIC),
            (config.di2_function, config.di2_logic, Reg.DI2_FUNCTION, Reg.DI2_LOGIC),
            (config.di3_function, config.di3_logic, Reg.DI3_FUNCTION, Reg.DI3_LOGIC),
        ]
        for func, logic, func_reg, logic_reg in di_configs:
            if func is not None:
                self._client.write_register(sid, func_reg, func)
                self._client.write_register(sid, logic_reg, logic)

        if config.has_brake:
            self._client.write_register(sid, Reg.DO1_FUNCTION, 5)
            self._client.write_register(sid, Reg.DO1_LOGIC, config.brake_do_logic)

    # --- State control ---

    def enable(self, axis: str | None = None):
        name, config, sid = self._resolve_axis(axis)
        current_state = self._get_state(sid)
        if current_state == DriveState.OPERATION_ENABLED:
            return

        path = get_transition_path(current_state, DriveState.OPERATION_ENABLED)
        if not path:
            raise RuntimeError(f"Cannot enable from state {current_state.value}")

        for command, control_word in path:
            self._write_control_word(sid, control_word)
            time.sleep(0.01)

        self._wait_for_state(sid, DriveState.OPERATION_ENABLED, STATE_TIMEOUT)

        if config.has_brake and config.brake_release_delay_ms > 0:
            time.sleep(config.brake_release_delay_ms / 1000.0)

    def disable(self, axis: str | None = None):
        _, _, sid = self._resolve_axis(axis)
        current_state = self._get_state(sid)
        if current_state in (DriveState.SWITCHED_ON, DriveState.READY_TO_SWITCH_ON):
            return
        if current_state == DriveState.OPERATION_ENABLED:
            self._write_control_word(sid, ControlWordBits.CMD_DISABLE_OPERATION)
            self._wait_for_state(sid, DriveState.SWITCHED_ON, STATE_TIMEOUT)

    def fault_reset(self, axis: str | None = None):
        _, _, sid = self._resolve_axis(axis)
        current_state = self._get_state(sid)
        if current_state != DriveState.FAULT:
            return

        cw = self._read_control_word(sid)
        self._write_control_word(sid, cw & ~ControlWordBits.FAULT_RESET)
        time.sleep(0.01)
        self._write_control_word(sid, cw | ControlWordBits.FAULT_RESET)
        time.sleep(0.05)
        self._write_control_word(sid, cw & ~ControlWordBits.FAULT_RESET)

        self._wait_for_state(sid, DriveState.SWITCH_ON_DISABLED, STATE_TIMEOUT)

    def quick_stop(self, axis: str | None = None):
        _, _, sid = self._resolve_axis(axis)
        self._write_control_word(sid, ControlWordBits.CMD_QUICK_STOP)

    def is_enabled(self, axis: str | None = None) -> bool:
        _, _, sid = self._resolve_axis(axis)
        return self._get_state(sid) == DriveState.OPERATION_ENABLED

    # --- Motion ---

    def move_to(
        self,
        position_mm: float,
        axis: str | None = None,
        velocity_mm_s: float | None = None,
        wait: bool = True,
    ):
        name, config, sid = self._resolve_axis(axis)

        if not config.is_position_valid(position_mm):
            raise ValueError(
                f"Position {position_mm}mm out of range "
                f"[{config.stroke_min}, {config.stroke_max}]"
            )

        self._client.write_register(sid, Reg.MODES_OF_OPERATION, OperationMode.PROFILE_POSITION)

        if velocity_mm_s is not None:
            self._client.write_register_32bit(sid, Reg.PROFILE_VELOCITY, config.velocity_mm_to_pulses(abs(velocity_mm_s)))

        position_pulses = config.mm_to_pulses(position_mm)
        self._client.write_register_32bit(sid, Reg.TARGET_POSITION, position_pulses, signed=True)

        cw = self._read_control_word(sid)
        cw &= ~ControlWordBits.ABS_REL
        cw &= ~ControlWordBits.NEW_SET_POINT
        cw &= ~ControlWordBits.HALT
        self._write_control_word(sid, cw)
        time.sleep(0.001)
        cw |= ControlWordBits.NEW_SET_POINT
        self._write_control_word(sid, cw)

        if wait:
            self._wait_for_motion(sid, MOTION_TIMEOUT)

    def move_relative(
        self,
        distance_mm: float,
        axis: str | None = None,
        velocity_mm_s: float | None = None,
        wait: bool = True,
    ):
        name, config, sid = self._resolve_axis(axis)

        current_pulses = self._client.read_register_32bit(sid, Reg.POSITION_ACTUAL_VALUE, signed=True)
        current_mm = config.pulses_to_mm(current_pulses)
        target_mm = current_mm + distance_mm
        if not config.is_position_valid(target_mm):
            raise ValueError(
                f"Target position {target_mm:.2f}mm out of range "
                f"[{config.stroke_min}, {config.stroke_max}]"
            )

        self._client.write_register(sid, Reg.MODES_OF_OPERATION, OperationMode.PROFILE_POSITION)

        if velocity_mm_s is not None:
            self._client.write_register_32bit(sid, Reg.PROFILE_VELOCITY, config.velocity_mm_to_pulses(abs(velocity_mm_s)))

        distance_pulses = config.mm_to_pulses(distance_mm)
        self._client.write_register_32bit(sid, Reg.TARGET_POSITION, distance_pulses, signed=True)

        cw = self._read_control_word(sid)
        cw |= ControlWordBits.ABS_REL
        cw &= ~ControlWordBits.NEW_SET_POINT
        cw &= ~ControlWordBits.HALT
        self._write_control_word(sid, cw)
        time.sleep(0.001)
        cw |= ControlWordBits.NEW_SET_POINT
        self._write_control_word(sid, cw)

        if wait:
            self._wait_for_motion(sid, MOTION_TIMEOUT)

    def jog(self, velocity_mm_s: float, axis: str | None = None):
        name, config, sid = self._resolve_axis(axis)

        if velocity_mm_s == 0.0:
            raise ValueError("Jog velocity must not be zero")

        if abs(velocity_mm_s) > config.max_velocity:
            logger.warning(
                "Jog velocity %.1f mm/s exceeds max %.1f mm/s, clamping",
                velocity_mm_s, config.max_velocity,
            )
            velocity_mm_s = config.max_velocity * (1 if velocity_mm_s > 0 else -1)

        velocity_corrected = velocity_mm_s * config.velocity_polarity

        self._client.write_register(sid, Reg.MODES_OF_OPERATION, OperationMode.PROFILE_POSITION)
        self._client.write_register_32bit(sid, Reg.PROFILE_VELOCITY, config.velocity_mm_to_pulses(abs(velocity_corrected)))

        far_position = 10000000 if velocity_corrected > 0 else -10000000
        self._client.write_register_32bit(sid, Reg.TARGET_POSITION, far_position, signed=True)

        self._write_control_word(sid, ControlWordBits.CMD_ENABLE_OPERATION)
        time.sleep(0.001)
        self._write_control_word(sid, ControlWordBits.CMD_ENABLE_OPERATION | ControlWordBits.NEW_SET_POINT)

    def stop(self, axis: str | None = None, wait: bool = True):
        _, config, sid = self._resolve_axis(axis)

        current_pos = self._client.read_register_32bit(sid, Reg.POSITION_ACTUAL_VALUE, signed=True)
        self._client.write_register_32bit(sid, Reg.TARGET_POSITION, current_pos, signed=True)
        self._write_control_word(sid, ControlWordBits.CMD_ENABLE_OPERATION | ControlWordBits.HALT)

        if wait:
            stop_timeout = 5.0
            start = time.time()
            stopped = False
            while time.time() - start < stop_timeout:
                if self.is_aborted:
                    raise RuntimeError("Operation aborted")
                status_word = self._read_status_word(sid)
                if status_word & StatusWordBits.FAULT:
                    error = self._client.read_register(sid, Reg.ERROR_CODE)
                    raise RuntimeError(f"Motor fault during stop: error 0x{error:04X}")
                velocity = self._client.read_register_32bit(sid, Reg.VELOCITY_ACTUAL_VALUE, signed=True)
                if abs(velocity) < 10:
                    stopped = True
                    break
                time.sleep(POLL_INTERVAL)

            if not stopped:
                raise TimeoutError(f"Timeout waiting for motor to stop ({stop_timeout}s)")

            final_pos = self._client.read_register_32bit(sid, Reg.POSITION_ACTUAL_VALUE, signed=True)
            self._client.write_register_32bit(sid, Reg.TARGET_POSITION, final_pos, signed=True)
            self._write_control_word(sid, ControlWordBits.CMD_ENABLE_OPERATION)

    def home(self, axis: str | None = None, wait: bool = True):
        name, config, sid = self._resolve_axis(axis)

        self._client.write_register(sid, Reg.MODES_OF_OPERATION, OperationMode.HOMING)
        time.sleep(0.05)

        self._client.write_register(sid, Reg.HOMING_METHOD, config.homing_method)
        self._client.write_register_32bit(
            sid, Reg.HOMING_SPEED_HIGH, config.velocity_mm_to_pulses(config.homing_velocity_high)
        )
        self._client.write_register_32bit(
            sid, Reg.HOMING_SPEED_LOW, config.velocity_mm_to_pulses(config.homing_velocity_low)
        )
        self._client.write_register_32bit(
            sid, Reg.HOMING_ACCELERATION, config.velocity_mm_to_pulses(config.homing_acceleration)
        )

        self._write_control_word(sid, ControlWordBits.CMD_SHUTDOWN)
        time.sleep(0.01)
        self._write_control_word(sid, ControlWordBits.CMD_SWITCH_ON)
        time.sleep(0.01)
        self._write_control_word(sid, ControlWordBits.CMD_ENABLE_OPERATION)
        time.sleep(0.01)
        self._write_control_word(sid, ControlWordBits.CMD_ENABLE_OPERATION | ControlWordBits.NEW_SET_POINT)

        if wait:
            self._wait_for_homing(sid, MOTION_TIMEOUT)
            self._homed[name] = True

    # --- Status ---

    def get_position(self, axis: str | None = None) -> float:
        _, config, sid = self._resolve_axis(axis)
        pulses = self._client.read_register_32bit(sid, Reg.POSITION_ACTUAL_VALUE, signed=True)
        return config.pulses_to_mm(pulses)

    def get_velocity(self, axis: str | None = None) -> float:
        _, config, sid = self._resolve_axis(axis)
        pulses = self._client.read_register_32bit(sid, Reg.VELOCITY_ACTUAL_VALUE, signed=True)
        return config.velocity_pulses_to_mm(pulses)

    def is_homed(self, axis: str | None = None) -> bool:
        name, _, _ = self._resolve_axis(axis)
        return self._homed.get(name, False)

    # --- Parameters ---

    def set_speed(self, velocity_mm_s: float, axis: str | None = None):
        _, config, sid = self._resolve_axis(axis)
        self._client.write_register_32bit(sid, Reg.PROFILE_VELOCITY, config.velocity_mm_to_pulses(abs(velocity_mm_s)))

    def set_acceleration(self, accel_mm_s2: float, axis: str | None = None):
        _, config, sid = self._resolve_axis(axis)
        self._client.write_register_32bit(sid, Reg.PROFILE_ACCELERATION, config.velocity_mm_to_pulses(abs(accel_mm_s2)))

    def set_deceleration(self, decel_mm_s2: float, axis: str | None = None):
        _, config, sid = self._resolve_axis(axis)
        self._client.write_register_32bit(sid, Reg.PROFILE_DECELERATION, config.velocity_mm_to_pulses(abs(decel_mm_s2)))

    # --- Abort ---

    def abort(self):
        self.is_aborted = True
        if self.is_connected:
            for name in self._axis_configs:
                try:
                    self.quick_stop(name)
                except Exception:
                    logger.error("Failed to quick-stop axis %s during abort", name, exc_info=True)

    def reset_abort(self):
        self.is_aborted = False

    # --- Context manager ---

    def __enter__(self) -> "ServoMotor":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.disconnect()


# --- Simulation ---

class ServoMotorSimulation:
    def __init__(self, axis_configs: dict[str, AxisConfig] | None = None):
        self._axis_configs = axis_configs or {k: v for k, v in DEFAULT_AXIS_CONFIGS.items()}
        self._positions: dict[str, float] = {name: 0.0 for name in self._axis_configs}
        self._homed: dict[str, bool] = {name: False for name in self._axis_configs}
        self._enabled: dict[str, bool] = {name: False for name in self._axis_configs}
        self._connected = False
        self.is_aborted = False

    def _resolve_axis(self, axis: str | None) -> tuple[str, AxisConfig]:
        if axis is None:
            axis = next(iter(self._axis_configs))
        if axis not in self._axis_configs:
            raise ValueError(f"Unknown axis: {axis}")
        return axis, self._axis_configs[axis]

    # --- Connection ---

    def connect(self, port: str | None = None, baudrate: int | None = None):
        self._connected = True

    def disconnect(self):
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    # --- Initialization ---

    def initialize_axis(self, axis: str | None = None):
        pass

    # --- State control ---

    def enable(self, axis: str | None = None):
        name, _ = self._resolve_axis(axis)
        self._enabled[name] = True

    def disable(self, axis: str | None = None):
        name, _ = self._resolve_axis(axis)
        self._enabled[name] = False

    def fault_reset(self, axis: str | None = None):
        pass

    def quick_stop(self, axis: str | None = None):
        pass

    def is_enabled(self, axis: str | None = None) -> bool:
        name, _ = self._resolve_axis(axis)
        return self._enabled.get(name, False)

    # --- Motion ---

    def move_to(
        self,
        position_mm: float,
        axis: str | None = None,
        velocity_mm_s: float | None = None,
        wait: bool = True,
    ):
        name, config = self._resolve_axis(axis)
        if not config.is_position_valid(position_mm):
            raise ValueError(
                f"Position {position_mm}mm out of range "
                f"[{config.stroke_min}, {config.stroke_max}]"
            )
        self._positions[name] = position_mm

    def move_relative(
        self,
        distance_mm: float,
        axis: str | None = None,
        velocity_mm_s: float | None = None,
        wait: bool = True,
    ):
        name, config = self._resolve_axis(axis)
        new_pos = self._positions[name] + distance_mm
        if not config.is_position_valid(new_pos):
            raise ValueError(
                f"Position {new_pos}mm out of range "
                f"[{config.stroke_min}, {config.stroke_max}]"
            )
        self._positions[name] = new_pos

    def jog(self, velocity_mm_s: float, axis: str | None = None):
        self._resolve_axis(axis)

    def stop(self, axis: str | None = None, wait: bool = True):
        self._resolve_axis(axis)

    def home(self, axis: str | None = None, wait: bool = True):
        name, _ = self._resolve_axis(axis)
        self._positions[name] = 0.0
        self._homed[name] = True

    # --- Status ---

    def get_position(self, axis: str | None = None) -> float:
        name, _ = self._resolve_axis(axis)
        return self._positions[name]

    def get_velocity(self, axis: str | None = None) -> float:
        self._resolve_axis(axis)
        return 0.0

    def is_homed(self, axis: str | None = None) -> bool:
        name, _ = self._resolve_axis(axis)
        return self._homed.get(name, False)

    # --- Parameters ---

    def set_speed(self, velocity_mm_s: float, axis: str | None = None):
        self._resolve_axis(axis)

    def set_acceleration(self, accel_mm_s2: float, axis: str | None = None):
        self._resolve_axis(axis)

    def set_deceleration(self, decel_mm_s2: float, axis: str | None = None):
        self._resolve_axis(axis)

    # --- Abort ---

    def abort(self):
        self.is_aborted = True

    def reset_abort(self):
        self.is_aborted = False

    # --- Context manager ---

    def __enter__(self) -> "ServoMotorSimulation":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.disconnect()
