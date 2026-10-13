"""
ServoService tests (serial port and motors mocked)
"""

import dataclasses
import math
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from fluidics.control.nimotion.high_level_api.axis_config import DEFAULT_AXIS_CONFIGS, AxisName
from fluidics.control.nimotion.high_level_api.servo_service import ServoService
from fluidics.control.nimotion.motor_control import HomingMethod, MotorError, MotorStateError, Registers


class TestConnect:
    """connect() must not leave the port open when it fails"""

    def test_port_closed_when_parameter_init_fails(self) -> None:
        serial_port = MagicMock()
        with (
            patch(
                "fluidics.control.nimotion.high_level_api.servo_service.SerialPort", return_value=serial_port
            ),
            patch.object(ServoService, "_verify_motor_communication"),
            patch.object(
                ServoService,
                "initialize_motor_parameters",
                side_effect=RuntimeError("write failed"),
            ),
        ):
            service = ServoService(port="COM_TEST")
            with pytest.raises(RuntimeError, match="write failed"):
                service.connect()

        serial_port.close.assert_called()
        assert service.is_connected is False


class TestSwitchAxis:
    """An axis whose parameters were not written must not become current"""

    def test_failed_parameter_write_keeps_the_axis(self) -> None:
        service = ServoService()
        service.current_axis = AxisName.Z4
        with (
            patch.object(
                ServoService, "is_connected", new_callable=lambda: property(lambda self: True)
            ),
            patch.object(ServoService, "verify_axis_connection", return_value=True),
            patch.object(
                ServoService,
                "initialize_motor_parameters",
                side_effect=MotorError("Drive at slave 3 kept 6083h"),
            ),
        ):
            with pytest.raises(MotorError, match="6083h"):
                service.switch_axis(AxisName.Z3)
        assert service.current_axis == AxisName.Z4


class TestInitializeMotorParameters:
    """Parameters written to the drive on connect / axis switch"""

    def _initialize(self, axis: AxisName, **changes: object) -> MagicMock:
        motor = MagicMock()
        configs = dict(DEFAULT_AXIS_CONFIGS)
        configs[axis] = dataclasses.replace(configs[axis], **changes)  # type: ignore[arg-type]
        service = ServoService(axis_configs=configs)
        with (
            patch.object(
                ServoService, "is_connected", new_callable=lambda: property(lambda self: True)
            ),
            patch.object(service, "get_motor", return_value=motor),
        ):
            service.initialize_motor_parameters(axis=axis, verbose=False)
        return motor

    def test_zero_polarity_is_written(self) -> None:
        # Polarity 0x00 must still be written so that a value left in the
        # drive by an earlier configuration is cleared.
        motor = self._initialize(AxisName.Z3, driver_polarity=0x00)
        motor.set_polarity.assert_called_once_with(0x00)

    def test_z4_polarity(self) -> None:
        motor = self._initialize(AxisName.Z4)
        motor.set_polarity.assert_called_once_with(0x80)

    def test_servo_axes_write_no_stepper_registers(self) -> None:
        motor = self._initialize(AxisName.X1)
        motor.set_position_restore_mode.assert_not_called()
        motor.set_restore_blocking_position.assert_not_called()
        motor.set_software_limits.assert_not_called()

    def test_t5_writes_its_limits_after_the_inputs(self) -> None:
        motor = self._initialize(AxisName.T5)
        motor.set_di_config.assert_any_call(1, 31, 0)
        motor.set_software_limits.assert_called_once_with(-2548, 11650)
        motor.set_position_restore_mode.assert_not_called()  # waits for the bench
        names = [c[0] for c in motor.mock_calls]
        last_input = max(i for i, n in enumerate(names) if n == "set_di_config")
        assert names.index("set_software_limits") > last_input

    def test_restore_settings_are_written_in_order_when_set(self) -> None:
        motor = self._initialize(
            AxisName.T5, position_restore_mode=1, restore_blocking_position=5278
        )
        names = [c[0] for c in motor.mock_calls]
        assert (
            names.index("set_position_restore_mode")
            < names.index("set_restore_blocking_position")
            < names.index("set_software_limits")
        )
        motor.set_position_restore_mode.assert_called_once_with(1)
        motor.set_restore_blocking_position.assert_called_once_with(5278)

    def test_open_limits_are_the_int32_extremes(self) -> None:
        motor = self._initialize(AxisName.T5, drive_limit_min=-math.inf, drive_limit_max=math.inf)
        motor.set_software_limits.assert_called_once_with(-(2**31), 2**31 - 1)

    def test_config_argument_replaces_the_axis_config(self) -> None:
        motor = MagicMock()
        service = ServoService()
        override = dataclasses.replace(
            DEFAULT_AXIS_CONFIGS[AxisName.T5], drive_limit_min=-math.inf, drive_limit_max=math.inf
        )
        with (
            patch.object(
                ServoService, "is_connected", new_callable=lambda: property(lambda self: True)
            ),
            patch.object(service, "get_motor", return_value=motor),
        ):
            service.initialize_motor_parameters(AxisName.T5, verbose=False, config=override)
        motor.set_software_limits.assert_called_once_with(-(2**31), 2**31 - 1)

    def test_cia402_mode_and_motion_profile(self) -> None:
        motor = self._initialize(AxisName.Z4)
        motor.set_control_mode_cia402.assert_called_once_with()
        # Z4: 13107.2 pulses/mm. The limits first: the drive caps the profile at them.
        assert motor.write_checked.call_args_list == [
            call(Registers.MAX_PROFILE_VELOCITY, int(500.0 * 13107.2)),
            call(Registers.MAX_ACCELERATION, int(5000.0 * 13107.2)),
            call(Registers.MAX_DECELERATION, int(5000.0 * 13107.2)),
            call(Registers.PROFILE_VELOCITY, int(100.0 * 13107.2)),
            call(Registers.PROFILE_ACCELERATION, int(500.0 * 13107.2)),
            call(Registers.PROFILE_DECELERATION, int(500.0 * 13107.2)),
            call(Registers.QUICK_STOP_DECELERATION, int(1000.0 * 13107.2)),
        ]


class TestHomingAndZero:
    """Spec section 7, rules 1 and 5"""

    def _run(self, axis: AxisName, action: Any, motor: MagicMock, configs: Any = None) -> Any:
        service = ServoService(axis_configs=configs or dict(DEFAULT_AXIS_CONFIGS))
        service.current_axis = axis
        with (
            patch.object(
                ServoService, "is_connected", new_callable=lambda: property(lambda self: True)
            ),
            patch.object(service, "get_motor", return_value=motor),
        ):
            return action(service)

    def test_home_refuses_an_axis_without_a_method(self) -> None:
        motor = MagicMock()
        with pytest.raises(ValueError):
            self._run(AxisName.T5, lambda s: s.home(AxisName.T5), motor)
        motor.home.assert_not_called()

    def test_home_with_an_explicit_method_is_allowed(self) -> None:
        motor = MagicMock()
        method = HomingMethod.CURRENT_POSITION_AS_HOME
        self._run(AxisName.T5, lambda s: s.home(AxisName.T5, method=method), motor)
        motor.home.assert_called_once()

    def test_home_all_skips_axes_without_a_method(self) -> None:
        motor = MagicMock()
        configs = {a: DEFAULT_AXIS_CONFIGS[a] for a in (AxisName.Y2, AxisName.T5)}
        self._run(AxisName.Y2, lambda s: s.home_all(), motor, configs)
        assert motor.home.call_count == 1  # Y2 only

    def test_set_zero_clears_the_home_offset_first(self) -> None:
        motor = MagicMock()
        motor.read_di_physical_state.return_value = 0b111  # DI1 high: inactive
        motor.read_home_offset.return_value = 0
        self._run(AxisName.T5, lambda s: s.set_home_position(AxisName.T5), motor)
        names = [c[0] for c in motor.mock_calls]
        assert (
            names.index("set_home_offset") < names.index("set_homing_method") < names.index("home")
        )
        motor.set_home_offset.assert_called_once_with(0)

    def test_set_zero_refuses_while_the_home_input_is_active(self) -> None:
        motor = MagicMock()
        motor.read_di_physical_state.return_value = 0b110  # DI1 low: active (logic 0)
        with pytest.raises(MotorStateError):
            self._run(AxisName.T5, lambda s: s.set_home_position(AxisName.T5), motor)
        motor.set_home_offset.assert_not_called()
        motor.home.assert_not_called()

    def test_set_zero_stops_when_the_offset_does_not_read_back(self) -> None:
        motor = MagicMock()
        motor.read_di_physical_state.return_value = 0b111
        motor.read_home_offset.return_value = 12
        with pytest.raises(MotorError):
            self._run(AxisName.T5, lambda s: s.set_home_position(AxisName.T5), motor)
        motor.home.assert_not_called()

    def test_servo_axes_do_not_read_the_inputs(self) -> None:
        motor = MagicMock()
        motor.read_home_offset.return_value = 0
        self._run(AxisName.Y2, lambda s: s.set_home_position(AxisName.Y2), motor)
        motor.read_di_physical_state.assert_not_called()

    def test_servo_axes_clear_the_home_offset_too(self) -> None:
        motor = MagicMock()
        motor.read_home_offset.return_value = 0
        self._run(AxisName.Y2, lambda s: s.set_home_position(AxisName.Y2), motor)
        motor.set_home_offset.assert_called_once_with(0)
        names = [c[0] for c in motor.mock_calls]
        assert (
            names.index("set_home_offset")
            < names.index("read_home_offset")
            < names.index("set_homing_method")
        )
