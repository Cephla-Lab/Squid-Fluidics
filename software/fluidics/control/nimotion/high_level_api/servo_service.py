"""
Servo Motor Service Module

Provides a unified high-level API for controlling the servo motor system.
"""

import logging
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from ..modbus_rtu import ModbusClient
from ..motor_control import DriveState, HomingMethod, Motor, MotorError, MotorStateError, Registers
from ..serial_comm import PortInfo, PortScanner, SerialPort
from .axis_config import DEFAULT_AXIS_CONFIGS, AxisConfig, AxisName

logger = logging.getLogger(__name__)

HOME_SWITCH_FUNCTION = 31  # DI function number of the home switch (2003h)


@dataclass
class AxisStatus:
    """Axis status data"""

    axis: AxisName
    """Axis name"""

    connected: bool
    """Whether connected"""

    state: DriveState
    """Driver state"""

    position_mm: float
    """Current position (axis units: mm, or ° for T5)"""

    velocity_mm_s: float
    """Current velocity (axis units per second)"""

    is_enabled: bool
    """Whether enabled"""

    is_fault: bool
    """Whether a fault is present"""

    is_homed: bool
    """Whether homing has completed"""

    is_target_reached: bool
    """Whether the target has been reached"""

    error_code: int
    """Error code"""

    status_word: int = 0
    """Raw status word value (for debugging)"""

    torque: int = 0
    """Actual torque (0.1% of rated)"""

    operation_mode: int = 0
    """Operation mode display (6061h)"""


class ServoService:
    """
    Servo motor service class

    Provides a unified interface for multi-axis control.
    """

    def __init__(
        self,
        port: Optional[str] = None,
        baudrate: int = 115200,
        axis_configs: Optional[Dict[AxisName, AxisConfig]] = None,
        timeout: float = 0.5,
        retries: int = 3,
    ) -> None:
        """
        Initialize the servo service

        Args:
            port: serial port name (optional, can be specified later via connect)
            baudrate: baud rate
            axis_configs: dictionary of axis configurations (defaults to the XYG321-A configuration)
            timeout: communication timeout (seconds)
            retries: number of retries
        """
        self._port = port
        self._baudrate = baudrate
        self._timeout = timeout
        self._retries = retries

        # Axis configurations
        self._axis_configs = axis_configs or DEFAULT_AXIS_CONFIGS.copy()

        # Communication objects
        self._serial_port: Optional[SerialPort] = None
        self._modbus_client: Optional[ModbusClient] = None

        # Motor instances
        self._motors: Dict[AxisName, Motor] = {}

        # Currently selected axis
        self._current_axis: AxisName = AxisName.Z4

        # Homing state
        self._homed_axes: Dict[AxisName, bool] = {axis: False for axis in AxisName}

        # Port scanner
        self._port_scanner = PortScanner()

    # ==================== Connection Management ====================

    @property
    def is_connected(self) -> bool:
        """Whether connected"""
        return self._modbus_client is not None and self._modbus_client.is_connected

    def get_modbus_client(self) -> Optional[ModbusClient]:
        """
        Get the underlying Modbus client (for debugging)

        Returns:
            ModbusClient instance, or None if not connected
        """
        return self._modbus_client

    @property
    def current_axis(self) -> AxisName:
        """Get the currently selected axis"""
        return self._current_axis

    @current_axis.setter
    def current_axis(self, axis: AxisName) -> None:
        """Set the currently selected axis"""
        if axis not in self._axis_configs:
            raise ValueError(f"Axis {axis.value} is not configured")

        old_axis = self._current_axis
        self._current_axis = axis
        logger.info(f"Switched to axis {axis.value}")

        # If connected and the axis changed, write the new axis's parameters
        if self.is_connected and axis != old_axis:
            try:
                self.initialize_motor_parameters(axis=axis, verbose=True)
            except Exception as e:
                logger.warning(f"Failed to initialize axis {axis.value} parameters: {e}")

    def verify_axis_connection(self, axis: Optional[AxisName] = None) -> bool:
        """
        Verify whether the motor on the specified axis is reachable

        Attempts to read the status word to confirm the motor is responding normally.

        Args:
            axis: axis name (None uses the current axis)

        Returns:
            True if the motor responds normally, False if communication fails
        """
        if not self.is_connected:
            return False

        if axis is None:
            axis = self._current_axis

        if axis not in self._motors:
            return False

        motor = self._motors[axis]
        config = self._axis_configs[axis]

        try:
            # Try reading the status word (6041h) to verify communication
            status_word = motor.read_status_word()
            logger.debug(
                f"Axis {axis.value} communication verified: "
                f"slave address={config.slave_id}, status word=0x{status_word:04X}"
            )
            return True
        except Exception as e:
            logger.warning(
                f"Axis {axis.value} communication failed: "
                f"slave address={config.slave_id}, error={e}"
            )
            return False

    def switch_axis(self, axis: AxisName) -> bool:
        """
        Safely switch to the specified axis

        Verifies that the target axis motor is reachable before switching. If
        communication fails, keeps the current axis unchanged and returns False.

        Args:
            axis: target axis

        Returns:
            True if the switch succeeds, False if the target axis is unavailable

        Raises:
            Exception: writing the axis' parameters failed (e.g. MotorError: the
                drive did not keep a motion limit); the current axis is unchanged
        """
        if axis not in self._axis_configs:
            logger.error(f"Axis {axis.value} is not configured")
            return False

        # If not connected, switch directly (allows selecting an axis offline)
        if not self.is_connected:
            self._current_axis = axis
            logger.info(f"Switched to axis {axis.value} (offline)")
            return True

        # If it is already the current axis, no switch is needed
        if axis == self._current_axis:
            return True

        # Verify whether the target axis motor is reachable
        if not self.verify_axis_connection(axis):
            logger.error(
                f"Cannot switch to axis {axis.value}: motor not responding "
                f"(slave address={self._axis_configs[axis].slave_id})"
            )
            return False

        # Write the axis parameters; an axis whose drive did not take them (a
        # profile capped by the drive's limits, say) must not become current
        try:
            self.initialize_motor_parameters(axis=axis, verbose=True)
        except Exception as e:
            logger.error(f"Failed to initialize axis {axis.value} parameters: {e}")
            raise

        self._current_axis = axis
        logger.info(f"Switched to axis {axis.value}")
        return True

    def get_available_ports(self) -> List[PortInfo]:
        """
        Get the list of available serial ports

        Returns:
            list of PortInfo
        """
        return self._port_scanner.scan_ports()

    def connect(
        self, port: Optional[str] = None, baudrate: Optional[int] = None, initialize: bool = True
    ) -> None:
        """
        Connect to the servo system

        Args:
            port: serial port name
            baudrate: baud rate
            initialize: write the current axis' parameters (False: only check that
                it answers, writing nothing; the T5 bring-up window connects so)
        """
        if port is not None:
            self._port = port
        if baudrate is not None:
            self._baudrate = baudrate

        if not self._port:
            raise ValueError("No serial port specified")

        logger.info(f"Connecting to servo system: {self._port} @ {self._baudrate}")

        from ..simulation import get_bus, is_simulator_port

        if is_simulator_port(self._port):
            # The simulated bus starts with the default axes; add any custom ones
            get_bus(self._port).ensure_axes(self._axis_configs.values())

        # Create the serial port
        self._serial_port = SerialPort(
            port=self._port,
            baudrate=self._baudrate,
            timeout=self._timeout,
        )
        self._serial_port.open()

        # Create the Modbus client
        self._modbus_client = ModbusClient(
            serial_port=self._serial_port,
            timeout=self._timeout,
            retries=self._retries,
        )

        # Create motor instances
        self._motors = {}
        for axis_name, config in self._axis_configs.items():
            self._motors[axis_name] = Motor(
                modbus_client=self._modbus_client,
                slave_id=config.slave_id,
            )

        # Verify communication with the current axis motor
        self._verify_motor_communication()

        if not initialize:
            logger.info("Servo system connected (no parameters written)")
            return

        # Automatically initialize the current axis parameters. On failure,
        # close the port: a leaked open handle blocks reconnecting on Windows.
        try:
            self.initialize_motor_parameters(verbose=True)
        except Exception:
            self.disconnect()
            raise

        logger.info("Servo system connected successfully")

    def _verify_motor_communication(self) -> None:
        """
        Verify communication with the motor

        Attempts to read the status word to confirm the motor is responding normally.

        Raises:
            RuntimeError: if communication fails
        """
        motor = self._motors.get(self._current_axis)
        if motor is None:
            raise RuntimeError(f"No motor instance found for axis {self._current_axis.value}")

        config = self._axis_configs[self._current_axis]

        try:
            # Try reading the status word (6041h) to verify communication
            status_word = motor.read_status_word()
            logger.info(
                f"Motor communication verified: axis={self._current_axis.value}, "
                f"slave address={config.slave_id}, status word=0x{status_word:04X}"
            )
        except Exception as e:
            # Close resources that were already opened
            self._motors.clear()
            if self._modbus_client:
                self._modbus_client.disconnect()
                self._modbus_client = None
            if self._serial_port:
                self._serial_port.close()
                self._serial_port = None

            raise RuntimeError(
                f"Unable to communicate with the motor: axis={self._current_axis.value}, "
                f"slave address={config.slave_id}. "
                f"Please check that the slave address is correct.\n"
                f"Original error: {e}"
            )

    def disconnect(self) -> None:
        """Disconnect"""
        logger.info("Disconnecting from the servo system")

        self._motors.clear()

        if self._modbus_client is not None:
            self._modbus_client.disconnect()
            self._modbus_client = None

        if self._serial_port is not None:
            self._serial_port.close()
            self._serial_port = None

        # Reset homing state
        self._homed_axes = {axis: False for axis in AxisName}

    # ==================== Motor Parameter Initialization ====================

    def initialize_motor_parameters(
        self,
        axis: Optional[AxisName] = None,
        verbose: bool = True,
        config: Optional[AxisConfig] = None,
    ) -> None:
        """
        Initialize motor parameters

        According to the AxisConfig settings, writes the following parameters to the driver:
        - Encoder resolution (608Fh) - numerator/denominator
        - Gear ratio (6091h) - numerator/denominator
        - Motion limits (607Fh, 60C5h, 60C6h) and profile (6081h, 6083h-6085h),
          each read back
        - Homing timeout
        - Stall homing parameters
        - DI (digital input) function configuration

        Args:
            axis: axis name (None uses the current axis)
            verbose: whether to display detailed information
            config: write this configuration instead of the axis' own (the bring-up
                window uses it for open drive limits)

        Raises:
            MotorError: the drive did not keep a motion limit or profile value

        Note:
            It is recommended to call this function after connecting and before running any
            motion, to ensure the driver parameters are correct. In particular, the homing
            timeout parameter defaults to 0ms, which causes homing to time out immediately!
        """
        if not self.is_connected:
            raise MotorError("Not connected to the servo system")

        if axis is None:
            axis = self._current_axis

        motor = self.get_motor(axis)
        if config is None:
            config = self.get_axis_config(axis)

        if verbose:
            logger.info(f"Initializing motor parameters for axis {axis.value}...")

        # 0. CiA402 control (2002h:01h); PP/HM do not work in the NiMotion modes
        motor.set_control_mode_cia402()

        # 1. Set encoder resolution (608Fh)
        motor.set_encoder_resolution(
            config.encoder_resolution, config.encoder_resolution_denominator
        )
        if verbose:
            logger.info(
                f"  Encoder resolution: {config.encoder_resolution}/"
                f"{config.encoder_resolution_denominator}"
            )

        # 2. Set gear ratio (6091h)
        motor.set_gear_ratio(config.gear_ratio_numerator, config.gear_ratio_denominator)
        if verbose:
            logger.info(
                f"  Gear ratio: {config.gear_ratio_numerator}/" f"{config.gear_ratio_denominator}"
            )

        # 2b. Motion limits, then the motion profile. Written so moves don't
        # depend on whatever was last stored in the drive; Halt (Stop, jog
        # release) decelerates with 6084h. The drive caps the profile at the
        # limits without reporting an error, so the limits go first and every
        # value is read back.
        for register, value in (
            (Registers.MAX_PROFILE_VELOCITY, config.max_velocity),
            (Registers.MAX_ACCELERATION, config.max_acceleration),
            (Registers.MAX_DECELERATION, config.max_acceleration),
            (Registers.PROFILE_VELOCITY, config.default_velocity),
            (Registers.PROFILE_ACCELERATION, config.default_acceleration),
            (Registers.PROFILE_DECELERATION, config.default_deceleration),
            (Registers.QUICK_STOP_DECELERATION, config.quick_stop_deceleration),
        ):
            motor.write_checked(register, config.velocity_mm_to_pulses(value))
        if verbose:
            logger.info(f"  Limits: {config.max_velocity} mm/s, {config.max_acceleration} mm/s²")
            logger.info(
                f"  Profile: {config.default_velocity} mm/s, "
                f"accel {config.default_acceleration} / decel {config.default_deceleration} / "
                f"quick stop {config.quick_stop_deceleration} mm/s²"
            )

        # 3. Set homing timeout
        motor.set_homing_timeout(config.homing_timeout)
        if verbose:
            logger.info(f"  Homing timeout: {config.homing_timeout}ms")

        # 4. Set stall homing parameters
        motor.set_blocking_parameters(config.blocking_torque, config.blocking_time)
        if verbose:
            logger.info(
                f"  Stall parameters: torque threshold={config.blocking_torque/10}%, "
                f"detection time={config.blocking_time}ms"
            )

        # 5. Set polarity (607Eh). Always written, including 0x00, so a value
        # left in the drive by an earlier configuration cannot linger.
        motor.set_polarity(config.driver_polarity)
        if verbose:
            logger.info(f"  Polarity (607Eh): 0x{config.driver_polarity:02X}")

        # 6. Configure DI functions
        di_configs = [
            (1, config.di1_function, config.di1_logic),
            (2, config.di2_function, config.di2_logic),
            (3, config.di3_function, config.di3_logic),
        ]

        for di_num, func, logic in di_configs:
            if func is not None:
                motor.set_di_config(di_num, func, logic)
                func_name = self._get_di_function_name(func)
                if verbose:
                    logger.info(f"  DI{di_num}: {func_name} (logic={logic})")

        # 6b. Position restore and drive software limits (the T5 tilt; spec section 8).
        # Written only when configured, so the servo axes keep what their drives hold.
        if config.position_restore_mode is not None:
            motor.set_position_restore_mode(config.position_restore_mode)
            if verbose:
                logger.info(f"  Position restore (2005h:18h): {config.position_restore_mode}")
        if config.restore_blocking_position is not None:
            motor.set_restore_blocking_position(config.restore_blocking_position)
            if verbose:
                logger.info(f"  Restore cut point (2005h:1Dh): {config.restore_blocking_position}")
        if config.drive_limit_min is not None and config.drive_limit_max is not None:
            low = config.limit_to_counts(config.drive_limit_min)
            high = config.limit_to_counts(config.drive_limit_max)
            motor.set_software_limits(low, high)
            if verbose:
                logger.info(f"  Drive limits (607Dh): {low} .. {high}")

        # 7. Configure the brake (if present)
        if config.has_brake:
            # Set the brake release delay
            motor.set_brake_release_delay(config.brake_release_delay_ms)
            if verbose:
                logger.info(f"  Brake release delay: {config.brake_release_delay_ms}ms")

            if config.brake_auto_control:
                # Set to driver automatic control mode
                motor.set_brake_auto_control(
                    do_number=config.brake_do_number, logic=config.brake_do_logic
                )
                if verbose:
                    logger.info(
                        f"  Brake: DO{config.brake_do_number} automatic control "
                        f"(release on {'high' if config.brake_do_logic else 'low'} level)"
                    )
            else:
                # Set to manual control mode via communication
                motor.set_do_config(
                    do_number=config.brake_do_number,
                    function=1,  # general-purpose output
                    logic=config.brake_do_logic,
                )
                if verbose:
                    logger.info(
                        f"  Brake: DO{config.brake_do_number} manual control "
                        f"(release on {'high' if config.brake_do_logic else 'low'} level)"
                    )

        if verbose:
            logger.info(f"Motor parameter initialization for axis {axis.value} complete")

    def initialize_all_motors(self, verbose: bool = True) -> None:
        """
        Initialize parameters for all motors

        Args:
            verbose: whether to display detailed information
        """
        for axis in self._axis_configs.keys():
            try:
                self.initialize_motor_parameters(axis, verbose=verbose)
            except Exception as e:
                logger.warning(f"Failed to initialize axis {axis.value}: {e}")

    @staticmethod
    def _get_di_function_name(function: int) -> str:
        """Get the DI function name"""
        di_functions = {
            0: "Undefined",
            1: "Motor enable",
            2: "Alarm reset",
            14: "Positive limit switch",
            15: "Negative limit switch",
            31: "Home switch",
        }
        return di_functions.get(function, f"Function {function}")

    # ==================== Axis Access ====================

    def get_motor(self, axis: Optional[AxisName] = None) -> Motor:
        """
        Get a motor instance

        Args:
            axis: axis name (None uses the current axis)

        Returns:
            Motor instance
        """
        if not self.is_connected:
            raise MotorError("Not connected to the servo system")

        if axis is None:
            axis = self._current_axis

        if axis not in self._motors:
            raise ValueError(f"Axis {axis.value} is not configured")

        return self._motors[axis]

    def get_axis_config(self, axis: Optional[AxisName] = None) -> AxisConfig:
        """
        Get an axis configuration

        Args:
            axis: axis name (None uses the current axis)

        Returns:
            AxisConfig
        """
        if axis is None:
            axis = self._current_axis
        return self._axis_configs[axis]

    # ==================== Status Reading ====================

    def get_axis_status(self, axis: Optional[AxisName] = None) -> AxisStatus:
        """
        Get the axis status

        Args:
            axis: axis name (None uses the current axis)

        Returns:
            AxisStatus
        """
        if axis is None:
            axis = self._current_axis

        config = self.get_axis_config(axis)

        if not self.is_connected:
            return AxisStatus(
                axis=axis,
                connected=False,
                state=DriveState.UNKNOWN,
                position_mm=0.0,
                velocity_mm_s=0.0,
                is_enabled=False,
                is_fault=False,
                is_homed=False,
                is_target_reached=False,
                error_code=0,
                status_word=0,
            )

        motor = self.get_motor(axis)
        status = motor.get_status()

        return AxisStatus(
            axis=axis,
            connected=True,
            state=status.state,
            position_mm=config.pulses_to_mm(status.position),
            velocity_mm_s=config.rpm_to_mm_s(status.velocity),
            is_enabled=status.state == DriveState.OPERATION_ENABLED,
            is_fault=status.is_fault,
            is_homed=self._homed_axes.get(axis, False),
            is_target_reached=status.is_target_reached,
            error_code=status.error_code,
            status_word=status.status_word,
            torque=status.torque,
            operation_mode=status.operation_mode,
        )

    def get_all_axis_status(self) -> Dict[AxisName, AxisStatus]:
        """
        Get the status of all axes

        Returns:
            dictionary of axis statuses
        """
        return {axis: self.get_axis_status(axis) for axis in self._axis_configs.keys()}

    def get_position(self, axis: Optional[AxisName] = None) -> float:
        """
        Get the current position (mm)

        Args:
            axis: axis name

        Returns:
            position (mm)
        """
        if axis is None:
            axis = self._current_axis

        config = self.get_axis_config(axis)
        motor = self.get_motor(axis)
        position_pulses = motor.read_position()

        return config.pulses_to_mm(position_pulses)

    def get_velocity(self, axis: Optional[AxisName] = None) -> float:
        """
        Get the current velocity (mm/s)

        Args:
            axis: axis name

        Returns:
            velocity (mm/s)
        """
        if axis is None:
            axis = self._current_axis

        config = self.get_axis_config(axis)
        motor = self.get_motor(axis)
        return config.rpm_to_mm_s(motor.read_velocity())

    # ==================== Enable Control ====================

    def enable(self, axis: Optional[AxisName] = None) -> None:
        """
        Enable an axis

        Args:
            axis: axis name
        """
        if axis is None:
            axis = self._current_axis

        logger.info(f"Enabling axis {axis.value}")
        motor = self.get_motor(axis)
        motor.enable()

    def disable(self, axis: Optional[AxisName] = None) -> None:
        """
        Disable an axis

        Args:
            axis: axis name
        """
        if axis is None:
            axis = self._current_axis

        logger.info(f"Disabling axis {axis.value}")
        motor = self.get_motor(axis)
        motor.disable()

    def _for_all_axes(self, action: Callable[[AxisName], None], what: str) -> None:
        """
        Apply action to every configured axis, even if some fail (an axis whose
        drive is absent must not keep the others from being stopped/disabled)

        Raises:
            MotorError: listing every axis that failed
        """
        failures = []
        for axis in self._axis_configs.keys():
            try:
                action(axis)
            except Exception as e:
                logger.warning(f"{what} failed on axis {axis.value}: {e}")
                failures.append(f"{axis.value}: {e}")
        if failures:
            raise MotorError(f"{what} failed on " + "; ".join(failures))

    def enable_all(self) -> None:
        """Enable all axes"""
        self._for_all_axes(self.enable, "Enable")

    def disable_all(self) -> None:
        """Disable all axes"""
        self._for_all_axes(self.disable, "Disable")

    def quick_stop(self, axis: Optional[AxisName] = None) -> None:
        """
        Quick stop

        Args:
            axis: axis name (None stops the current axis)
        """
        if axis is None:
            axis = self._current_axis

        logger.warning(f"Quick stop on axis {axis.value}")
        motor = self.get_motor(axis)
        motor.quick_stop()

    def quick_stop_all(self) -> None:
        """Quick stop all axes"""
        self._for_all_axes(self.quick_stop, "Quick stop")

    def fault_reset(self, axis: Optional[AxisName] = None) -> None:
        """
        Fault reset

        Args:
            axis: axis name
        """
        if axis is None:
            axis = self._current_axis

        logger.info(f"Fault reset on axis {axis.value}")
        motor = self.get_motor(axis)
        motor.fault_reset()

    # ==================== Brake Control ====================

    def release_brake(self, axis: Optional[AxisName] = None) -> None:
        """
        Release the brake

        Args:
            axis: axis name

        Note:
            Only valid for axes configured with a brake (has_brake=True)
        """
        if axis is None:
            axis = self._current_axis

        config = self.get_axis_config(axis)
        if not config.has_brake:
            logger.warning(f"Axis {axis.value} has no brake configured")
            return

        motor = self.get_motor(axis)
        motor.release_brake(
            do_number=config.brake_do_number, logic_high_release=(config.brake_do_logic == 1)
        )
        logger.info(f"Axis {axis.value} brake released")

    def engage_brake(self, axis: Optional[AxisName] = None) -> None:
        """
        Engage the brake

        Args:
            axis: axis name

        Note:
            Only valid for axes configured with a brake (has_brake=True)
        """
        if axis is None:
            axis = self._current_axis

        config = self.get_axis_config(axis)
        if not config.has_brake:
            logger.warning(f"Axis {axis.value} has no brake configured")
            return

        motor = self.get_motor(axis)
        motor.engage_brake(
            do_number=config.brake_do_number, logic_high_release=(config.brake_do_logic == 1)
        )
        logger.info(f"Axis {axis.value} brake engaged")

    def is_brake_released(self, axis: Optional[AxisName] = None) -> bool:
        """
        Check whether the brake is released

        Args:
            axis: axis name

        Returns:
            True=released, False=engaged
        """
        if axis is None:
            axis = self._current_axis

        config = self.get_axis_config(axis)
        if not config.has_brake:
            return True  # no brake is treated as released

        motor = self.get_motor(axis)
        do_state = motor.read_do_physical_state()
        do_bit = (do_state >> (config.brake_do_number - 1)) & 1

        # Determine based on logic
        if config.brake_do_logic == 1:
            return do_bit == 1  # release on high level
        else:
            return do_bit == 0  # release on low level

    def wait_for_brake_release(
        self, axis: Optional[AxisName] = None, delay_ms: Optional[int] = None
    ) -> None:
        """
        Wait for the brake to release

        Args:
            axis: axis name
            delay_ms: delay time (milliseconds); if None, uses the axis configuration's delay time

        Note:
            In automatic brake mode, call this function after enabling the motor to wait
            for the brake to fully release
        """
        if axis is None:
            axis = self._current_axis

        config = self.get_axis_config(axis)
        if not config.has_brake:
            return  # no brake, no need to wait

        if delay_ms is None:
            delay_ms = config.brake_release_delay_ms

        motor = self.get_motor(axis)
        motor.wait_for_brake_release(delay_ms)

    # ==================== Motion Control ====================

    def move_to(
        self,
        position_mm: float,
        velocity_mm_s: Optional[float] = None,
        axis: Optional[AxisName] = None,
        wait: bool = True,
    ) -> None:
        """
        Move to the specified position (absolute position)

        Args:
            position_mm: target position (axis units)
            velocity_mm_s: movement velocity (axis units/s)
            axis: axis name
            wait: whether to wait for completion
        """
        if axis is None:
            axis = self._current_axis

        config = self.get_axis_config(axis)

        # Check position validity
        if not config.is_position_valid(position_mm):
            raise ValueError(
                f"Position {position_mm}mm is outside the stroke range "
                f"[{config.stroke_min}, {config.stroke_max}]"
            )

        # Convert units
        position_pulses = config.mm_to_pulses(position_mm)

        velocity_pulses = None
        if velocity_mm_s is not None:
            velocity_pulses = config.velocity_mm_to_pulses(velocity_mm_s)

        logger.info(f"Axis {axis.value} moving to {position_mm}mm")

        motor = self.get_motor(axis)
        motor.move_absolute(position_pulses, velocity_pulses, wait=wait)

    def move_by(
        self,
        distance_mm: float,
        velocity_mm_s: Optional[float] = None,
        axis: Optional[AxisName] = None,
        wait: bool = True,
    ) -> None:
        """
        Relative move

        Args:
            distance_mm: movement distance (axis units)
            velocity_mm_s: movement velocity (axis units/s)
            axis: axis name
            wait: whether to wait for completion
        """
        if axis is None:
            axis = self._current_axis

        config = self.get_axis_config(axis)

        # Check target position validity
        current_pos = self.get_position(axis)
        target_pos = current_pos + distance_mm

        if not config.is_position_valid(target_pos):
            raise ValueError(
                f"Target position {target_pos}mm is outside the stroke range "
                f"[{config.stroke_min}, {config.stroke_max}]"
            )

        # Convert units
        distance_pulses = config.mm_to_pulses(distance_mm)

        velocity_pulses = None
        if velocity_mm_s is not None:
            velocity_pulses = config.velocity_mm_to_pulses(velocity_mm_s)

        logger.info(f"Axis {axis.value} relative move {distance_mm}mm")

        motor = self.get_motor(axis)
        motor.move_relative(distance_pulses, velocity_pulses, wait=wait)

    def jog(
        self,
        velocity_mm_s: float,
        axis: Optional[AxisName] = None,
    ) -> None:
        """
        Jog: move toward the end of the stroke until stop() is called

        A positive velocity moves toward stroke_max, a negative one toward
        stroke_min; the move ends there if it is never stopped. An axis already
        at or past that end (homing can leave it short of stroke_min) is only
        halted: it never moves against the jog direction. Returns at once.

        Args:
            velocity_mm_s: velocity (axis units/s, positive=forward, negative=reverse)
            axis: axis name
        """
        if axis is None:
            axis = self._current_axis

        if velocity_mm_s == 0:
            self.stop(axis=axis, wait=False)
            return

        config = self.get_axis_config(axis)
        speed_mm_s = min(abs(velocity_mm_s), config.max_velocity)
        target_mm = config.stroke_max if velocity_mm_s > 0 else config.stroke_min

        if (target_mm - self.get_position(axis)) * velocity_mm_s <= 0:
            logger.info(f"Axis {axis.value} jog: already at or past {target_mm}mm")
            self.stop(axis=axis, wait=False)
            return

        logger.info(f"Axis {axis.value} jog: {velocity_mm_s}mm/s toward {target_mm}mm")

        motor = self.get_motor(axis)
        motor.move_absolute(
            config.mm_to_pulses(target_mm),
            config.velocity_mm_to_pulses(speed_mm_s),
            wait=False,
        )

    def stop(self, axis: Optional[AxisName] = None, wait: bool = True) -> None:
        """
        Stop motion

        Args:
            axis: axis name
            wait: whether to wait for the stop to complete
        """
        if axis is None:
            axis = self._current_axis

        logger.info(f"Stopping axis {axis.value}")
        motor = self.get_motor(axis)
        motor.request_stop()  # end a wait in progress (e.g. move_to in another thread)
        motor.stop(wait=wait)

    # ==================== Homing Control ====================

    def home(
        self,
        axis: Optional[AxisName] = None,
        method: Optional[HomingMethod] = None,
        wait: bool = True,
    ) -> None:
        """
        Perform homing

        Args:
            axis: axis name
            method: homing method (if not specified, uses the default from the axis configuration)
            wait: whether to wait for completion
        """
        if axis is None:
            axis = self._current_axis

        config = self.get_axis_config(axis)

        # If no homing method is specified, use the default from the configuration.
        # An axis without one (the T5 tilt) refuses: the drive's stored method
        # (20 on a new STM28) would search for a switch that is not there.
        if method is None:
            if config.homing_method is None:
                raise ValueError(
                    f"Axis {axis.value} has no homing method; set its zero with "
                    "set_home_position()"
                )
            method = HomingMethod(config.homing_method)

        # Set homing speeds
        motor = self.get_motor(axis)
        motor.set_homing_speeds(
            high_speed=config.velocity_mm_to_pulses(config.homing_velocity_high),
            low_speed=config.velocity_mm_to_pulses(config.homing_velocity_low),
            acceleration=config.velocity_mm_to_pulses(config.homing_acceleration),
        )

        logger.info(f"Axis {axis.value} performing homing (method: {method})")
        # Wait as long as the drive's own homing timeout (2005h:1Ch), plus margin
        motor.home(method=method, wait=wait, timeout=config.homing_timeout / 1000 + 5)

        if wait:
            self._homed_axes[axis] = True

    def home_all(self, wait: bool = True) -> None:
        """
        Home all axes

        Args:
            wait: whether to wait for completion
        """
        for axis, config in self._axis_configs.items():
            if config.homing_method is None:
                logger.info(f"Axis {axis.value} has no homing method; not homed")
                continue
            self.home(axis, wait=wait)

    def set_home_position(self, axis: Optional[AxisName] = None) -> None:
        """
        Set the current position as the origin (homing method 35, no motion)

        Refuses while an input configured as home switch (function 31) reads
        active, then writes the home offset 607Ch = 0 and reads it back, so
        "here" reads 0 rather than an offset left in the drive. The homing run
        enables the drive, and it stays enabled.

        Args:
            axis: axis name

        Raises:
            MotorStateError: a home switch input reads active
            MotorError: 607Ch did not read back 0
        """
        if axis is None:
            axis = self._current_axis

        motor = self.get_motor(axis)
        active = self._active_home_switch_inputs(axis)
        if active:
            inputs = ", ".join(f"DI{n}" for n in active)
            raise MotorStateError(
                f"Home switch input {inputs} reads active on axis {axis.value}; "
                "the zero was not set"
            )
        motor.set_home_offset(0)
        offset = motor.read_home_offset()
        if offset != 0:
            raise MotorError(f"Home offset 607Ch reads {offset} after writing 0")
        motor.set_homing_method(HomingMethod.CURRENT_POSITION_AS_HOME)
        motor.home(wait=True)

        self._homed_axes[axis] = True
        logger.info(f"Axis {axis.value} current position set as origin")

    def _active_home_switch_inputs(self, axis: AxisName) -> List[int]:
        """DI numbers configured as home switch (function 31) that read active now"""
        config = self.get_axis_config(axis)
        inputs = [
            (n, getattr(config, f"di{n}_logic"))
            for n in (1, 2, 3)
            if getattr(config, f"di{n}_function") == HOME_SWITCH_FUNCTION
        ]
        if not inputs:
            return []
        levels = self.get_motor(axis).read_di_physical_state()
        active = []
        for n, logic in inputs:
            high = bool(levels & (1 << (n - 1)))
            if high == (logic in (1, 2)):  # 1 active high, 2 rising edge; 0 and 3 active low
                active.append(n)
        return active

    # ==================== Parameter Storage ====================

    def save_parameters(self, axis: Optional[AxisName] = None) -> None:
        """
        Save the drive's current parameters to its flash, so they survive a
        power cycle. The motor must be disabled.

        Args:
            axis: axis name
        """
        if axis is None:
            axis = self._current_axis

        logger.info(f"Saving parameters on axis {axis.value}")
        self.get_motor(axis).save_parameters()

    # ==================== Parameter Settings ====================

    def set_velocity(
        self,
        velocity_mm_s: float,
        axis: Optional[AxisName] = None,
    ) -> None:
        """
        Set the default velocity

        Args:
            velocity_mm_s: velocity (mm/s)
            axis: axis name
        """
        if axis is None:
            axis = self._current_axis

        config = self.get_axis_config(axis)
        velocity_pulses = config.velocity_mm_to_pulses(velocity_mm_s)

        motor = self.get_motor(axis)
        motor.set_profile_velocity(velocity_pulses)

    def set_acceleration(
        self,
        acceleration_mm_s2: float,
        axis: Optional[AxisName] = None,
    ) -> None:
        """
        Set the acceleration

        Args:
            acceleration_mm_s2: acceleration (mm/s²)
            axis: axis name
        """
        if axis is None:
            axis = self._current_axis

        config = self.get_axis_config(axis)
        acceleration_pulses = config.velocity_mm_to_pulses(acceleration_mm_s2)

        motor = self.get_motor(axis)
        motor.set_profile_acceleration(acceleration_pulses)

    def set_deceleration(
        self,
        deceleration_mm_s2: float,
        axis: Optional[AxisName] = None,
    ) -> None:
        """
        Set the deceleration

        Args:
            deceleration_mm_s2: deceleration (mm/s²)
            axis: axis name
        """
        if axis is None:
            axis = self._current_axis

        config = self.get_axis_config(axis)
        deceleration_pulses = config.velocity_mm_to_pulses(deceleration_mm_s2)

        motor = self.get_motor(axis)
        motor.set_profile_deceleration(deceleration_pulses)

    def get_profile_velocity(
        self,
        axis: Optional[AxisName] = None,
    ) -> float:
        """
        Read the profile velocity

        Args:
            axis: axis name

        Returns:
            velocity (mm/s)
        """
        if axis is None:
            axis = self._current_axis

        config = self.get_axis_config(axis)
        motor = self.get_motor(axis)
        velocity_pulses = motor.get_profile_velocity()

        return config.velocity_pulses_to_mm(velocity_pulses)

    def get_profile_acceleration(
        self,
        axis: Optional[AxisName] = None,
    ) -> float:
        """
        Read the profile acceleration

        Args:
            axis: axis name

        Returns:
            acceleration (mm/s²)
        """
        if axis is None:
            axis = self._current_axis

        config = self.get_axis_config(axis)
        motor = self.get_motor(axis)
        acceleration_pulses = motor.get_profile_acceleration()

        return config.velocity_pulses_to_mm(acceleration_pulses)

    def get_profile_deceleration(
        self,
        axis: Optional[AxisName] = None,
    ) -> float:
        """
        Read the profile deceleration

        Args:
            axis: axis name

        Returns:
            deceleration (mm/s²)
        """
        if axis is None:
            axis = self._current_axis

        config = self.get_axis_config(axis)
        motor = self.get_motor(axis)
        deceleration_pulses = motor.get_profile_deceleration()

        return config.velocity_pulses_to_mm(deceleration_pulses)

    # ==================== Context Management ====================

    def __enter__(self) -> "ServoService":
        """Context manager entry"""
        if self._port:
            self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # type: ignore
        """Context manager exit"""
        self.disconnect()

    def __repr__(self) -> str:
        status = "connected" if self.is_connected else "disconnected"
        return f"ServoService({self._port}, {status})"
