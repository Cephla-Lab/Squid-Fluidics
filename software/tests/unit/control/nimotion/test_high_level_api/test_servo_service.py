"""
ServoService tests (serial port and motors mocked)
"""

import dataclasses
from unittest.mock import MagicMock, patch

import pytest

from fluidics.control.nimotion.high_level_api.axis_config import DEFAULT_AXIS_CONFIGS, AxisName
from fluidics.control.nimotion.high_level_api.servo_service import ServoService


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

    def test_cia402_mode_and_motion_profile(self) -> None:
        motor = self._initialize(AxisName.Z4)
        motor.set_control_mode_cia402.assert_called_once_with()
        # Z4: 13107.2 pulses/mm
        motor.set_profile_velocity.assert_called_once_with(int(100.0 * 13107.2))
        motor.set_profile_acceleration.assert_called_once_with(int(500.0 * 13107.2))
        motor.set_profile_deceleration.assert_called_once_with(int(500.0 * 13107.2))
        motor.set_quick_stop_deceleration.assert_called_once_with(int(1000.0 * 13107.2))
