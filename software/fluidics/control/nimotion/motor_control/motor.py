"""
Motor control class

Provides a high-level control interface for the servo motor.
"""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from ..modbus_rtu import ModbusClient, NoResponseError
from .registers import ControlWordBits, HomingMethod, OperationMode, Registers, StatusWordBits
from .state_machine import DriveState, StateMachine, get_transition_path

logger = logging.getLogger(__name__)


def _to_signed16(value: int) -> int:
    """Interpret a raw 16-bit register value as two's-complement"""
    return value - 0x10000 if value & 0x8000 else value


class MotorError(Exception):
    """Motor control error"""


class MotorStateError(MotorError):
    """Motor state error"""


class MotorTimeoutError(MotorError):
    """Motor operation timeout"""


class MotionInterruptedError(MotorStateError):
    """A move or homing run was ended by stop, quick stop, or the drive leaving Operation Enabled"""


class LimitSwitchError(MotorStateError):
    """A move was stopped by a limit switch (alarm 8614h); the drive stays enabled"""


class MoveIncompleteError(MotorError):
    """The drive reported a move finished, but away from its target"""


LIMIT_SWITCH_ALARM = 0x8614  # 603Fh while a limit switch holds the axis (B06 table 8-1)


@dataclass
class MotorStatus:
    """Motor status data"""

    state: DriveState
    """Drive state"""

    status_word: int
    """Status word"""

    position: int
    """Current position (user units)"""

    velocity: int
    """Current velocity"""

    torque: int
    """Current torque"""

    operation_mode: int
    """Current operation mode"""

    is_target_reached: bool
    """Whether the target has been reached"""

    is_fault: bool
    """Whether a fault is present"""

    is_warning: bool
    """Whether a warning is present"""

    error_code: int
    """Error code"""


class Motor:
    """
    Servo motor control class

    Provides motor state control, motion control, and related functionality.
    """

    # Default timeout settings
    DEFAULT_STATE_TIMEOUT = 2.0  # State transition timeout (seconds)
    DEFAULT_MOTION_TIMEOUT = 30.0  # Motion timeout (seconds)
    DEFAULT_POLL_INTERVAL = 0.01  # Polling interval (seconds)
    SET_POINT_TIMEOUT = 0.5  # Each step of the PP set-point handshake (seconds)

    def __init__(
        self,
        modbus_client: ModbusClient,
        slave_id: int = 1,
        state_timeout: float = DEFAULT_STATE_TIMEOUT,
        motion_timeout: float = DEFAULT_MOTION_TIMEOUT,
    ) -> None:
        """
        Initialize motor control

        Args:
            modbus_client: Modbus client
            slave_id: Slave address
            state_timeout: State transition timeout (seconds)
            motion_timeout: Motion timeout (seconds)
        """
        self._client = modbus_client
        self._slave_id = slave_id
        self._state_timeout = state_timeout
        self._motion_timeout = motion_timeout
        self._state_machine = StateMachine()
        # Set by request_stop(); makes a waiting move/homing call return early
        self._stop_requested = threading.Event()
        self._brake_release_delay_ms = 0  # Brake release delay (ms)

    @property
    def slave_id(self) -> int:
        """Get the slave address"""
        return self._slave_id

    @property
    def is_connected(self) -> bool:
        """Whether the client is connected"""
        return self._client.is_connected

    # ==================== State reading ====================

    def read_status_word(self) -> int:
        """
        Read the status word

        Returns:
            Status word value
        """
        return self._client.read_register(self._slave_id, Registers.STATUS_WORD.address)

    def read_control_word(self) -> int:
        """
        Read the control word

        Returns:
            Control word value
        """
        return self._client.read_register(self._slave_id, Registers.CONTROL_WORD.address)

    def write_control_word(self, value: int) -> None:
        """
        Write the control word

        Args:
            value: Control word value
        """
        self._client.write_register(self._slave_id, Registers.CONTROL_WORD.address, value)

    def read_position(self) -> int:
        """
        Read the current position

        Returns:
            Position value (encoder units)
        """
        return self._client.read_register_32bit(
            self._slave_id, Registers.POSITION_ACTUAL_VALUE.address, signed=True
        )

    def read_velocity(self) -> int:
        """
        Read the actual velocity (606Ch)

        Returns:
            Motor speed in rpm (not user units/s)
        """
        return self._client.read_register_32bit(
            self._slave_id, Registers.VELOCITY_ACTUAL_VALUE.address, signed=True
        )

    def read_torque(self) -> int:
        """
        Read the current torque

        Returns:
            Torque value (0.1% of rated torque)
        """
        return _to_signed16(
            self._client.read_register(self._slave_id, Registers.TORQUE_ACTUAL_VALUE.address)
        )

    def read_operation_mode(self) -> int:
        """
        Read the current operation mode

        Returns:
            Operation mode value
        """
        # 6061h is an INT8 register
        return _to_signed16(
            self._client.read_register(self._slave_id, Registers.MODES_OF_OPERATION_DISPLAY.address)
        )

    def read_error_code(self) -> int:
        """
        Read the error code

        Returns:
            Error code
        """
        return self._client.read_register(self._slave_id, Registers.ERROR_CODE.address)

    def get_state(self) -> DriveState:
        """
        Get the current drive state

        Returns:
            Drive state
        """
        status_word = self.read_status_word()
        return self._state_machine.update(status_word)

    def get_status(self) -> MotorStatus:
        """
        Get the complete motor status

        Returns:
            A MotorStatus instance
        """
        status_word = self.read_status_word()
        state = self._state_machine.update(status_word)

        # Always read the error code, not only when in a fault state.
        # This captures all errors, including warnings and historical faults.
        error_code = self.read_error_code()

        return MotorStatus(
            state=state,
            status_word=status_word,
            position=self.read_position(),
            velocity=self.read_velocity(),
            torque=self.read_torque(),
            operation_mode=self.read_operation_mode(),
            is_target_reached=bool(status_word & StatusWordBits.TARGET_REACHED),
            # A non-zero error code alone may be a warning; the fault bit decides
            is_fault=bool(status_word & StatusWordBits.FAULT),
            is_warning=bool(status_word & StatusWordBits.WARNING),
            error_code=error_code,
        )

    # ==================== State control ====================

    def enable(self, timeout: Optional[float] = None) -> None:
        """
        Enable the motor

        Transitions the motor from any state to the Operation Enabled state.

        Args:
            timeout: Timeout (seconds)

        Raises:
            MotorStateError: State transition failed
            MotorTimeoutError: Timed out
        """
        if timeout is None:
            timeout = self._state_timeout

        current_state = self.get_state()
        logger.info(f"Enabling motor (current state: {current_state.value})")

        if current_state == DriveState.OPERATION_ENABLED:
            logger.info("Motor is already enabled")
            return

        # Get the transition path
        path = get_transition_path(current_state, DriveState.OPERATION_ENABLED)
        if not path:
            raise MotorStateError(
                f"Cannot transition from {current_state.value} to the enabled state"
            )

        # Execute the state transition
        for command, control_word in path:
            logger.debug(f"Executing command: {command.value}, control word: 0x{control_word:04X}")
            self.write_control_word(control_word)
            time.sleep(0.01)

        # Wait until the target state is reached
        self._wait_for_state(DriveState.OPERATION_ENABLED, timeout)
        logger.info("Motor enabled successfully")

        # Wait for the brake to release (if a brake release delay is configured)
        if hasattr(self, "_brake_release_delay_ms") and self._brake_release_delay_ms > 0:
            delay_s = self._brake_release_delay_ms / 1000.0
            logger.info(f"Waiting for brake release ({self._brake_release_delay_ms}ms)...")
            time.sleep(delay_s)

    def disable(self, timeout: Optional[float] = None) -> None:
        """
        Disable the motor

        Transitions the motor to the Switched On state.

        Args:
            timeout: Timeout (seconds)
        """
        if timeout is None:
            timeout = self._state_timeout

        current_state = self.get_state()
        logger.info(f"Disabling motor (current state: {current_state.value})")

        if current_state in (DriveState.SWITCHED_ON, DriveState.READY_TO_SWITCH_ON):
            logger.info("Motor is already disabled")
            return

        if current_state == DriveState.OPERATION_ENABLED:
            self.write_control_word(ControlWordBits.CMD_DISABLE_OPERATION)
            self._wait_for_state(DriveState.SWITCHED_ON, timeout)
            logger.info("Motor disabled successfully")

    def quick_stop(self) -> None:
        """
        Quick stop

        Performs an immediate quick stop.
        """
        logger.warning("Performing quick stop")
        self.request_stop()
        self.write_control_word(ControlWordBits.CMD_QUICK_STOP)

    def request_stop(self) -> None:
        """
        Make a move or homing run that is waiting for completion (possibly in
        another thread) raise MotionInterruptedError instead of waiting for
        its timeout. Does not command the drive by itself.
        """
        self._stop_requested.set()

    def _check_interrupted(self, status_word: int, seen_enabled: bool) -> bool:
        """
        Raise MotionInterruptedError if a stop was requested, or if the drive
        left Operation Enabled after having been seen in it during this wait.

        Returns:
            Whether the drive is (now) in Operation Enabled
        """
        if self._stop_requested.is_set():
            raise MotionInterruptedError("Motion stopped")
        state = self._state_machine.update(status_word)
        enabled = state == DriveState.OPERATION_ENABLED
        if seen_enabled and not enabled:
            raise MotionInterruptedError(f"Motion interrupted: drive state {state.value}")
        return enabled

    def fault_reset(self, timeout: Optional[float] = None) -> None:
        """
        Fault reset

        Args:
            timeout: Timeout (seconds)
        """
        if timeout is None:
            timeout = self._state_timeout

        current_state = self.get_state()
        if current_state != DriveState.FAULT:
            logger.info("Motor has no fault; no reset needed")
            return

        logger.info("Performing fault reset")

        # Send fault reset (rising edge)
        current_cw = self.read_control_word()
        self.write_control_word(current_cw & ~ControlWordBits.FAULT_RESET)
        time.sleep(0.01)
        self.write_control_word(current_cw | ControlWordBits.FAULT_RESET)
        time.sleep(0.05)
        self.write_control_word(current_cw & ~ControlWordBits.FAULT_RESET)

        # Wait to leave the fault state
        self._wait_for_state(DriveState.SWITCH_ON_DISABLED, timeout)
        logger.info("Fault reset successful")

    def _wait_for_state(
        self,
        target_state: DriveState,
        timeout: float,
    ) -> None:
        """
        Wait until the target state is reached

        Args:
            target_state: Target state
            timeout: Timeout

        Raises:
            MotorTimeoutError: Timed out
            MotorStateError: Entered a fault state
        """
        start_time = time.time()

        while time.time() - start_time < timeout:
            current_state = self.get_state()

            if current_state == target_state:
                return

            if current_state == DriveState.FAULT:
                error_code = self.read_error_code()
                raise MotorStateError(f"Motor fault: error code 0x{error_code:04X}")

            time.sleep(self.DEFAULT_POLL_INTERVAL)

        raise MotorTimeoutError(
            f"Timed out waiting for state {target_state.value} ({timeout}s), "
            f"current state: {self.get_state().value}"
        )

    # ==================== Operation mode ====================

    def set_operation_mode(self, mode: OperationMode) -> None:
        """
        Set the operation mode

        Args:
            mode: Operation mode
        """
        logger.info(f"Setting operation mode: {mode.name}")
        self._client.write_register(
            self._slave_id, Registers.MODES_OF_OPERATION.address, mode.value
        )

    # ==================== Position control ====================

    def set_target_position(self, position: int) -> None:
        """
        Set the target position

        Args:
            position: Target position (encoder units)
        """
        self._client.write_register_32bit(
            self._slave_id, Registers.TARGET_POSITION.address, position, signed=True
        )

    def set_control_mode_cia402(self) -> None:
        """
        Select CiA402 control (2002h:01h = 0)

        PP, HM and the other CiA402 modes only work in this control mode; the
        object dictionary lists 4 (NiMotion open loop) as the factory default.
        """
        self._client.write_register(self._slave_id, Registers.CONTROL_MODE.address, 0)

    def set_quick_stop_deceleration(self, deceleration: int) -> None:
        """
        Set the quick stop deceleration (6085h)

        Args:
            deceleration: Deceleration (user units/s²)
        """
        self._client.write_register_32bit(
            self._slave_id, Registers.QUICK_STOP_DECELERATION.address, deceleration
        )

    def set_profile_velocity(self, velocity: int) -> None:
        """
        Set the profile velocity

        Args:
            velocity: Velocity value
        """
        self._client.write_register_32bit(
            self._slave_id, Registers.PROFILE_VELOCITY.address, velocity
        )

    def get_profile_velocity(self) -> int:
        """
        Read the profile velocity

        Returns:
            Velocity value (user units/s)
        """
        return self._client.read_register_32bit(self._slave_id, Registers.PROFILE_VELOCITY.address)

    def set_profile_acceleration(self, acceleration: int) -> None:
        """
        Set the profile acceleration

        Args:
            acceleration: Acceleration value
        """
        self._client.write_register_32bit(
            self._slave_id, Registers.PROFILE_ACCELERATION.address, acceleration
        )

    def get_profile_acceleration(self) -> int:
        """
        Read the profile acceleration

        Returns:
            Acceleration value (user units/s²)
        """
        return self._client.read_register_32bit(
            self._slave_id, Registers.PROFILE_ACCELERATION.address
        )

    def set_profile_deceleration(self, deceleration: int) -> None:
        """
        Set the profile deceleration

        Args:
            deceleration: Deceleration value
        """
        self._client.write_register_32bit(
            self._slave_id, Registers.PROFILE_DECELERATION.address, deceleration
        )

    def get_profile_deceleration(self) -> int:
        """
        Read the profile deceleration

        Returns:
            Deceleration value (user units/s²)
        """
        return self._client.read_register_32bit(
            self._slave_id, Registers.PROFILE_DECELERATION.address
        )

    def move_absolute(
        self,
        position: int,
        velocity: Optional[int] = None,
        wait: bool = True,
        timeout: Optional[float] = None,
    ) -> None:
        """
        Absolute position move (Profile Position mode)

        Replaces any move in progress, including a halted one.

        Args:
            position: Target position
            velocity: Move velocity (optional)
            wait: Whether to wait for completion
            timeout: Timeout (seconds)

        Raises:
            MotorStateError: Motor not enabled, or fault
            MotorTimeoutError: Set-point not acknowledged, or move not finished in time
        """
        logger.info(f"Absolute position move: target={position}")
        self._start_pp_move(position, velocity, relative=False)
        if wait:
            self._wait_for_motion_complete(self._motion_timeout if timeout is None else timeout)

    def move_relative(
        self,
        distance: int,
        velocity: Optional[int] = None,
        wait: bool = True,
        timeout: Optional[float] = None,
    ) -> None:
        """
        Relative position move (Profile Position mode)

        Replaces any move in progress, including a halted one.

        Args:
            distance: Move distance
            velocity: Move velocity (optional)
            wait: Whether to wait for completion
            timeout: Timeout (seconds)

        Raises:
            MotorStateError: Motor not enabled, or fault
            MotorTimeoutError: Set-point not acknowledged, or move not finished in time
        """
        logger.info(f"Relative position move: distance={distance}")
        self._start_pp_move(distance, velocity, relative=True)
        if wait:
            self._wait_for_motion_complete(self._motion_timeout if timeout is None else timeout)

    def _start_pp_move(self, target: int, velocity: Optional[int], relative: bool) -> None:
        """
        Hand a new Profile Position set-point to the drive

        Follows the handshake in the NiMotion Modbus manual (B06, 5.3.3-5.3.4):

        - "Change set immediately" (bit 5) is set, so the new set-point replaces
          a move in progress instead of being queued behind it.
        - Halt (bit 8) is cleared in the same write as the bit 4 rising edge.
          Clearing Halt on its own means "continue positioning": a halted move
          would resume toward its old target before the new one takes over.
        - The drive accepts the set-point by setting status bit 12; bit 4 is
          then released, and the drive clears bit 12 again.
        """
        self._require_enabled()

        self.set_operation_mode(OperationMode.PROFILE_POSITION)
        if velocity is not None:
            self.set_profile_velocity(velocity)
        self.set_target_position(target)

        cw = ControlWordBits.CMD_ENABLE_OPERATION | ControlWordBits.CHANGE_SET_IMMEDIATELY
        if relative:
            cw |= ControlWordBits.ABS_REL

        # Release bit 4 if an earlier command left it set, keeping Halt as it is
        # so that a halted move does not resume in between
        halt = self.read_control_word() & ControlWordBits.HALT
        self.write_control_word(cw | halt)
        self._wait_for_status_bit(
            StatusWordBits.SET_POINT_ACK, False, "drive ready for a new set-point"
        )

        self.write_control_word(cw | ControlWordBits.NEW_SET_POINT)
        self._wait_for_status_bit(StatusWordBits.SET_POINT_ACK, True, "set-point acknowledge")
        self.write_control_word(cw)

    def _require_enabled(self) -> None:
        """
        Raise unless operation is enabled

        Motion control words carry the "enable operation" bits; written in
        Switched On they would power up a drive that was deliberately disabled.
        """
        state = self.get_state()
        if state != DriveState.OPERATION_ENABLED:
            raise MotorStateError(f"Motor is not enabled (state: {state.value})")

    def _wait_for_status_bit(
        self,
        mask: int,
        expected: bool,
        what: str,
        timeout: Optional[float] = None,
    ) -> None:
        """
        Poll the status word until (status & mask) matches expected

        Raises:
            MotorStateError: Fault
            MotorTimeoutError: Timed out
        """
        if timeout is None:
            timeout = self.SET_POINT_TIMEOUT
        start_time = time.time()
        while time.time() - start_time < timeout:
            status_word = self.read_status_word()
            if status_word & StatusWordBits.FAULT:
                error_code = self.read_error_code()
                raise MotorStateError(
                    f"Fault while waiting for {what}: error code 0x{error_code:04X}"
                )
            if bool(status_word & mask) == expected:
                return
            time.sleep(self.DEFAULT_POLL_INTERVAL)
        raise MotorTimeoutError(f"Timed out waiting for {what} ({timeout}s)")

    def _wait_for_motion_complete(self, timeout: float) -> None:
        """
        Wait for the motion to complete

        Args:
            timeout: Timeout

        A limit switch ends a move without a fault: the drive stays enabled,
        sets the warning bit with alarm 8614h, and never reports the target
        reached, so the wait would otherwise run into its timeout. The error is
        raised once the axis is at standstill: a set-point sent during the
        drive's limit reaction is reported done without moving (seen on Y2).
        A marginal switch can also let a move finish short of its target, or
        drive through to it (both seen on the X stage), so an absolute move
        must end at its target and no move may end on an active limit input.

        Args:
            timeout: Timeout

        Raises:
            MotorTimeoutError: Timed out
            MotorStateError: Fault
            LimitSwitchError: Stopped by a limit switch
            MoveIncompleteError: Reported finished away from the target (absolute moves)
        """
        self._stop_requested.clear()
        seen_enabled = False
        start_time = time.time()
        target: Optional[int] = None
        if not self.read_control_word() & ControlWordBits.ABS_REL:
            target = self._client.read_register_32bit(
                self._slave_id, Registers.TARGET_POSITION.address, signed=True
            )
        window = self._client.read_register_32bit(self._slave_id, Registers.POSITION_WINDOW.address)
        tolerance = max(20 * window, 1000)

        while time.time() - start_time < timeout:
            status_word = self.read_status_word()

            # Check for a fault
            if status_word & StatusWordBits.FAULT:
                error_code = self.read_error_code()
                raise MotorStateError(
                    f"Fault occurred during motion: error code 0x{error_code:04X}"
                )
            seen_enabled = self._check_interrupted(status_word, seen_enabled) or seen_enabled

            if status_word & StatusWordBits.WARNING:
                if self.read_error_code() == LIMIT_SWITCH_ALARM:
                    self._wait_for_standstill()
                    raise LimitSwitchError(
                        f"Move stopped by a limit switch at position {self.read_position()}"
                    )

            # Check for target reached
            if status_word & StatusWordBits.TARGET_REACHED:
                if self.read_digital_inputs() & 0b11:  # 60FDh: negative / positive limit
                    raise LimitSwitchError(
                        f"Move ended on a limit switch at position {self.read_position()}"
                    )
                if target is not None:
                    position = self.read_position()
                    if abs(position - target) > tolerance:
                        raise MoveIncompleteError(
                            f"Move ended at {position}, {position - target:+d} from its target "
                            f"{target} (stopped by a limit switch?)"
                        )
                logger.info("Motion complete")
                return

            time.sleep(self.DEFAULT_POLL_INTERVAL)

        raise MotorTimeoutError(f"Timed out waiting for motion to complete ({timeout}s)")

    def _wait_for_standstill(self, timeout: float = 2.0) -> None:
        """Wait until the actual velocity reads (about) zero twice in a row"""
        still = 0
        deadline = time.time() + timeout
        while still < 2 and time.time() < deadline:
            still = still + 1 if abs(self.read_velocity()) <= 1 else 0
            time.sleep(self.DEFAULT_POLL_INTERVAL)

    # ==================== Velocity control ====================

    def set_target_velocity(self, velocity: int) -> None:
        """
        Set the target velocity

        Args:
            velocity: Target velocity
        """
        self._client.write_register_32bit(
            self._slave_id, Registers.TARGET_VELOCITY.address, velocity, signed=True
        )

    def stop(self, wait: bool = True, timeout: float = 5.0) -> None:
        """
        Stop the current move with Halt (bit 8)

        With wait=True, once the motor is at rest the halted move is replaced by
        a set-point at the actual position, leaving the drive enabled, holding
        position, with Halt cleared. With wait=False only Halt is set; the next
        move replaces the halted target (see _start_pp_move), so the old move
        cannot resume.

        Args:
            wait: Whether to wait for the stop to complete
            timeout: Timeout (seconds)
        """
        logger.info("Stopping motion")

        # Halt only means something while operation is enabled. Writing the
        # control word below in any other state would enable the drive.
        state = self.get_state()
        if state != DriveState.OPERATION_ENABLED:
            logger.info(f"Stop ignored: motor not enabled (state: {state.value})")
            return

        # Halt first: every Modbus transaction before it delays the stop
        self.write_control_word(ControlWordBits.CMD_ENABLE_OPERATION | ControlWordBits.HALT)

        if not wait:
            logger.info("Stop command sent (Halt)")
            return

        start_time = time.time()
        while time.time() - start_time < timeout:
            if abs(self.read_velocity()) < 10:  # below 10 rpm
                break
            time.sleep(self.DEFAULT_POLL_INTERVAL)
        else:
            logger.warning(f"Motor still moving {timeout}s after Halt")

        # Replace the halted move so that it cannot resume
        final_pos = self.read_position()
        self._start_pp_move(final_pos, None, relative=False)
        logger.info(f"Motion stopped, position: {final_pos}")

    # ==================== Homing control ====================

    def set_homing_method(self, method: HomingMethod) -> None:
        """
        Set the homing method

        Args:
            method: Homing method
        """
        self._client.write_register(self._slave_id, Registers.HOMING_METHOD.address, method.value)

    def set_homing_speeds(
        self,
        high_speed: int,
        low_speed: int,
        acceleration: int,
    ) -> None:
        """
        Set the homing speed parameters

        Args:
            high_speed: High speed (search for switch)
            low_speed: Low speed (search for home)
            acceleration: Acceleration
        """
        self._client.write_register_32bit(
            self._slave_id, Registers.HOMING_SPEED_HIGH.address, high_speed
        )
        self._client.write_register_32bit(
            self._slave_id, Registers.HOMING_SPEED_LOW.address, low_speed
        )
        self._client.write_register_32bit(
            self._slave_id, Registers.HOMING_ACCELERATION.address, acceleration
        )

    def home(
        self,
        method: Optional[HomingMethod] = None,
        wait: bool = True,
        timeout: Optional[float] = None,
    ) -> None:
        """
        Perform homing

        Args:
            method: Homing method (optional; uses the current setting if not specified)
            wait: Whether to wait for completion
            timeout: Timeout
        """
        if timeout is None:
            timeout = self._motion_timeout

        logger.info("Performing homing")

        # Set homing mode
        self.set_operation_mode(OperationMode.HOMING)
        time.sleep(0.05)

        # Set the homing method
        if method is not None:
            self.set_homing_method(method)

        # The drive switches between PP and HM while enabled at standstill
        # (manual B06 5.1), so only enable when needed. enable() waits for the
        # state change and the brake release delay. (This used to run Shutdown /
        # Switch on / Enable every time, which dropped the power stage, engaging
        # the brake on Z axes, and started homing 30 ms later.)
        if self.get_state() != DriveState.OPERATION_ENABLED:
            self.enable()

        # Start homing on a rising edge of bit 4 (this also clears Halt)
        self.write_control_word(ControlWordBits.CMD_ENABLE_OPERATION)
        time.sleep(0.01)
        self.write_control_word(
            ControlWordBits.CMD_ENABLE_OPERATION | ControlWordBits.NEW_SET_POINT
        )

        if wait:
            self._wait_for_homing_complete(timeout)

    def _wait_for_homing_complete(self, timeout: float) -> None:
        """
        Wait for homing to complete

        Uses **edge detection** on HOMING_ATTAINED (status word bit 12, in the
        homing-mode sense) rather than level detection on TARGET_REACHED (bit 10).

        Why bit 10 cannot be used: bit 10 (target reached) is set by a previous
        positioning move and lingers in the drive's status word. When homing
        starts, the operation mode is set to HOMING; if bit 10 still holds the
        residual value from the last move, level detection would immediately
        mistake it for "complete" before the motor has actually homed, leaving
        the axis un-homed and its position stuck at the last move's endpoint.

        Two-stage edge detection (1 -> 0 -> 1):
          1. First wait for bit 12 to clear (1 -> 0), proving the drive has begun
             a new homing move;
          2. Then wait for bit 12 to set (-> 1) with the operation mode display
             showing HOMING before declaring completion.
        This distinguishes a "genuine new homing cycle" from "residual bits left
        by the previous move."

        Method 35 (current position as home) does not move: the drive sets the
        home when triggered (B06 5.7.4), so bit 12 can be set again before the
        first read and there is no 1 -> 0 edge to wait for. Stage 1 is skipped.

        Args:
            timeout: Timeout
        """
        self._stop_requested.clear()
        seen_enabled = False
        start_time = time.time()
        method = self._client.read_register(self._slave_id, Registers.HOMING_METHOD.address)
        motion_started = method & 0xFF == HomingMethod.CURRENT_POSITION_AS_HOME

        while time.time() - start_time < timeout:
            status_word = self.read_status_word()

            # Check for a fault
            if status_word & StatusWordBits.FAULT:
                error_code = self.read_error_code()
                raise MotorStateError(
                    f"Fault occurred during homing: error code 0x{error_code:04X}"
                )
            seen_enabled = self._check_interrupted(status_word, seen_enabled) or seen_enabled

            attained = bool(status_word & StatusWordBits.HOMING_ATTAINED)

            if not motion_started:
                # Stage 1: wait for bit 12 to clear: the drive has started a new homing move
                if not attained:
                    motion_started = True
            else:
                # Homing is underway. Only now is bit 13 meaningful as a homing error;
                # checking it earlier could trip on a stale following-error bit (bit 13
                # in profile-position mode) left by a previous move.
                if status_word & StatusWordBits.HOMING_ERROR:
                    error_code = self.read_error_code()
                    raise MotorStateError(f"Homing error: error code 0x{error_code:04X}")

                # Stage 2: bit 12 set again, and the mode is still HOMING: homing complete
                if attained and self.read_operation_mode() == OperationMode.HOMING:
                    logger.info("Homing complete")
                    return

            time.sleep(self.DEFAULT_POLL_INTERVAL)

        raise MotorTimeoutError(f"Timed out waiting for homing to complete ({timeout}s)")

    # ==================== Digital I/O ====================

    def read_digital_inputs(self) -> int:
        """
        Read the digital input state

        Returns:
            Digital input state (32-bit)
        """
        return self._client.read_register_32bit(self._slave_id, Registers.DIGITAL_INPUTS.address)

    def read_digital_outputs(self) -> int:
        """
        Read the digital output state

        Returns:
            Digital output state (32-bit)
        """
        return self._client.read_register_32bit(self._slave_id, Registers.DIGITAL_OUTPUTS.address)

    # ==================== Parameter storage ====================

    SAVE_COMMAND = 0x65766173  # ASCII "save"

    def save_parameters(self, timeout: float = 5.0) -> None:
        """
        Save all parameters to the drive's flash (1010h:01)

        Manual B06 6.6.1 and 8.4: the drive refuses (fault "command cannot be
        executed") while enabled or when the supply is below 80% of the rated
        voltage, and the object reads back 1 once the save succeeded.

        The drive does not answer while it writes flash (measured: longer than
        0.4 s), so a missing reply to the save command or to the first
        read-backs is not a failure; only the read-back decides.

        Raises:
            MotorStateError: Motor enabled, or a fault during the save
            MotorError: Supply voltage too low, or the save was not confirmed
        """
        state = self.get_state()
        if state in (DriveState.OPERATION_ENABLED, DriveState.QUICK_STOP_ACTIVE):
            raise MotorStateError("Disable the motor before saving parameters")

        rated_v = self._client.read_register(self._slave_id, Registers.RATED_VOLTAGE.address)
        bus_v = self._client.read_register(self._slave_id, Registers.BUS_VOLTAGE.address) / 10
        if rated_v and bus_v < 0.8 * rated_v:
            raise MotorError(
                f"Supply voltage {bus_v:.1f} V is below 80% of the rated {rated_v} V; "
                "the drive will not save parameters"
            )

        try:
            self._client.write_register_32bit(
                self._slave_id, Registers.SAVE_PARAMETERS.address, self.SAVE_COMMAND
            )
        except NoResponseError:
            logger.info("No reply to the save command (drive busy writing flash)")
        time.sleep(0.1)

        value = None
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                status_word = self.read_status_word()
                if status_word & StatusWordBits.FAULT:
                    error_code = self.read_error_code()
                    raise MotorStateError(f"Parameter save failed: error code 0x{error_code:04X}")
                value = self._client.read_register_32bit(
                    self._slave_id, Registers.SAVE_PARAMETERS.address
                )
            except NoResponseError:
                time.sleep(self.DEFAULT_POLL_INTERVAL * 5)
                continue
            if value == 1:
                logger.info("Parameters saved")
                return
            time.sleep(self.DEFAULT_POLL_INTERVAL * 5)
        raise MotorError(f"Parameter save not confirmed (1010h:01 reads {value!r})")

    # ==================== Parameter initialization ====================

    def set_homing_timeout(self, timeout_ms: int) -> None:
        """
        Set the homing timeout

        Args:
            timeout_ms: Timeout (milliseconds)

        Note:
            A default value of 0ms causes homing to time out immediately;
            60000ms (60 seconds) is recommended.
        """
        self._client.write_register(self._slave_id, Registers.HOMING_TIMEOUT.address, timeout_ms)
        logger.debug(f"Set homing timeout: {timeout_ms}ms")

    def set_blocking_parameters(self, torque: int, time_ms: int) -> None:
        """
        Set the blocking homing parameters

        Args:
            torque: Torque threshold (0.1% units, 300 = 30%)
            time_ms: Detection time (milliseconds)
        """
        self._client.write_register(self._slave_id, Registers.BLOCKING_TORQUE.address, torque)
        self._client.write_register(self._slave_id, Registers.BLOCKING_TIME.address, time_ms)
        logger.debug(
            f"Set blocking parameters: torque threshold={torque/10}%, detection time={time_ms}ms"
        )

    def set_di_config(
        self,
        di_number: int,
        function: int,
        logic: int = 0,
    ) -> None:
        """
        Configure a digital input (DI) function

        Args:
            di_number: DI number (1, 2, or 3)
            function: Function number (0=undefined, 14=positive limit,
                15=negative limit, 31=home, ...)
            logic: Logic (0=active low, 1=active high, 2=rising edge, 3=falling edge)

        Note:
            Common function numbers:
            - 0: Undefined
            - 1: Motor enable (NiMotion mode)
            - 2: Alarm reset
            - 14: Positive limit switch
            - 15: Negative limit switch
            - 31: Home switch
        """
        if di_number == 1:
            func_reg = Registers.DI1_FUNCTION.address
            logic_reg = Registers.DI1_LOGIC.address
        elif di_number == 2:
            func_reg = Registers.DI2_FUNCTION.address
            logic_reg = Registers.DI2_LOGIC.address
        elif di_number == 3:
            func_reg = Registers.DI3_FUNCTION.address
            logic_reg = Registers.DI3_LOGIC.address
        else:
            raise ValueError(f"Invalid DI number: {di_number}, must be 1, 2, or 3")

        self._client.write_register(self._slave_id, func_reg, function)
        self._client.write_register(self._slave_id, logic_reg, logic)
        logger.debug(f"Configured DI{di_number}: function={function}, logic={logic}")

    def set_encoder_resolution(self, numerator: int, denominator: int = 1) -> None:
        """
        Set the encoder resolution (608Fh)

        Encoder resolution = numerator / denominator (pulses per motor turn)

        Args:
            numerator: Numerator - encoder pulse count (608Fh:01)
            denominator: Denominator - motor turns (608Fh:02), default 1

        Example:
            Set 10000 pulses/turn: set_encoder_resolution(10000, 1)
        """
        self._client.write_register_32bit(
            self._slave_id, Registers.ENCODER_RESOLUTION_NUM.address, numerator
        )
        self._client.write_register_32bit(
            self._slave_id, Registers.ENCODER_RESOLUTION_DEN.address, denominator
        )
        logger.debug(f"Set encoder resolution: {numerator}/{denominator}")

    def set_gear_ratio(self, numerator: int = 1, denominator: int = 1) -> None:
        """
        Set the electronic gear ratio (6091h)

        Gear ratio = numerator / denominator (motor turns per shaft turn)

        Args:
            numerator: Numerator - motor shaft turns (6091h:01)
            denominator: Denominator - drive shaft turns (6091h:02)

        Example:
            No reduction (1:1): set_gear_ratio(1, 1)
            Gear ratio 10:1: set_gear_ratio(10, 1)
        """
        self._client.write_register_32bit(
            self._slave_id, Registers.GEAR_RATIO_NUM.address, numerator
        )
        self._client.write_register_32bit(
            self._slave_id, Registers.GEAR_RATIO_DEN.address, denominator
        )
        logger.debug(f"Set gear ratio: {numerator}/{denominator}")

    def set_polarity(self, polarity: int) -> None:
        """
        Set the polarity (607Eh)

        Used to invert the position and velocity directions.

        Args:
            polarity: Polarity value
                - 0x00: position forward, velocity forward
                - 0x40: position forward, velocity reverse
                - 0x80: position reverse, velocity forward
                - 0xC0: position reverse, velocity reverse

        Note:
            Bit7: position polarity (0=forward, 1=reverse)
            Bit6: velocity polarity (0=forward, 1=reverse)
        """
        self._client.write_register(self._slave_id, Registers.POLARITY.address, polarity)
        logger.debug(f"Set polarity: 0x{polarity:02X}")

    def read_di_physical_state(self) -> int:
        """
        Read the DI physical state

        Returns:
            DI state (bit0=DI1, bit1=DI2, bit2=DI3)
        """
        return self._client.read_register(self._slave_id, Registers.DI_PHYSICAL_STATE.address)

    # ==================== DO control ====================

    def set_do_config(
        self,
        do_number: int,
        function: int,
        logic: int = 1,
    ) -> None:
        """
        Configure a digital output (DO) function

        Args:
            do_number: DO number (currently only 1 is supported)
            function: Function number (1=general output, 5=brake)
            logic: Logic (0=active low, 1=active high)

        Note:
            Common function numbers:
            - 0: Configure as input
            - 1: General output (controlled via communication)
            - 2: Motor running/stopped
            - 3: Target reached
            - 4: Alarm output
            - 5: Brake output (controlled automatically by the drive)
        """
        if do_number != 1:
            raise ValueError(f"Invalid DO number: {do_number}, only DO1 is currently supported")

        self._client.write_register(self._slave_id, Registers.DO1_FUNCTION.address, function)
        self._client.write_register(self._slave_id, Registers.DO1_LOGIC.address, logic)
        logger.debug(f"Configured DO{do_number}: function={function}, logic={logic}")

    def set_do_output(self, do_number: int, state: bool) -> None:
        """
        Set the DO output state

        Args:
            do_number: DO number (currently only 1 is supported)
            state: True=high level, False=low level

        Note:
            The DO function must be set to 1 (general output) to be controlled
            via this method.
        """
        if do_number != 1:
            raise ValueError(f"Invalid DO number: {do_number}, only DO1 is currently supported")

        # Read the current state and modify only the specified bit
        current = self._client.read_register(self._slave_id, Registers.DO_CONTROL.address)

        if state:
            new_value = current | (1 << (do_number - 1))
        else:
            new_value = current & ~(1 << (do_number - 1))

        self._client.write_register(self._slave_id, Registers.DO_CONTROL.address, new_value)
        logger.debug(f"Set DO{do_number} = {state}")

    def read_do_physical_state(self) -> int:
        """
        Read the DO physical state

        Returns:
            DO state (bit0=DO1)
        """
        return self._client.read_register(self._slave_id, Registers.DO_PHYSICAL_STATE.address)

    def release_brake(self, do_number: int = 1, logic_high_release: bool = True) -> None:
        """
        Release the brake

        Args:
            do_number: DO number that controls the brake
            logic_high_release: True=release on high level, False=release on low level
        """
        # Set to general output mode
        self.set_do_config(do_number, function=1, logic=1 if logic_high_release else 0)
        # Output the release signal
        self.set_do_output(do_number, logic_high_release)
        logger.info(f"Brake released (DO{do_number}={'high' if logic_high_release else 'low'})")

    def engage_brake(self, do_number: int = 1, logic_high_release: bool = True) -> None:
        """
        Engage the brake

        Args:
            do_number: DO number that controls the brake
            logic_high_release: True = high level releases (low engages),
                False = low level releases (high engages)
        """
        # Output the engage signal (opposite of the release signal)
        self.set_do_output(do_number, not logic_high_release)
        logger.info(f"Brake engaged (DO{do_number}={'low' if logic_high_release else 'high'})")

    def set_brake_auto_control(self, do_number: int = 1, logic: int = 1) -> None:
        """
        Set the brake to automatic drive-controlled mode

        Args:
            do_number: DO number that controls the brake
            logic: Logic (0=release on low level, 1=release on high level)

        Note:
            In automatic control mode, the drive releases the brake automatically
            when enabled and engages it automatically when disabled.
        """
        if do_number != 1:
            raise ValueError(f"Invalid DO number: {do_number}, only DO1 is currently supported")

        self._client.write_register(
            self._slave_id, Registers.DO1_FUNCTION.address, 5  # 5=brake function
        )
        self._client.write_register(self._slave_id, Registers.DO1_LOGIC.address, logic)
        logger.info(f"Brake set to automatic control mode (DO{do_number}, logic={logic})")

    def set_brake_release_delay(self, delay_ms: int) -> None:
        """
        Set the brake release delay

        Args:
            delay_ms: Delay (milliseconds)

        Note:
            After enabling the motor, this delay is waited to ensure the brake is
            fully released.
        """
        self._brake_release_delay_ms = delay_ms
        logger.info(f"Brake release delay set to {delay_ms}ms")

    def wait_for_brake_release(self, delay_ms: Optional[int] = None) -> None:
        """
        Wait for the brake to release

        Args:
            delay_ms: Delay (milliseconds); uses the configured delay if None

        Note:
            In brake automatic mode, call this function after enabling the motor
            to wait for the brake to fully release.
        """
        if delay_ms is None:
            delay_ms = self._brake_release_delay_ms

        if delay_ms > 0:
            delay_s = delay_ms / 1000.0
            logger.info(f"Waiting for brake release ({delay_ms}ms)...")
            time.sleep(delay_s)
            logger.info("Brake release complete")

    def __repr__(self) -> str:
        return f"Motor(slave_id={self._slave_id}, state={self.get_state().value})"
