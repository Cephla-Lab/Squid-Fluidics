"""
Motor class tests (Modbus client mocked)
"""

from unittest.mock import MagicMock

import pytest

from fluidics.control.nimotion.motor_control.motor import Motor, MotorStateError
from fluidics.control.nimotion.motor_control.registers import HomingMethod, Registers


class TestSignedReads:
    """16-bit registers that hold signed values"""

    def test_torque_is_signed(self) -> None:
        client = MagicMock()
        client.read_register.return_value = 0xFFF6  # -10 => -1.0 % rated torque
        assert Motor(client, slave_id=4).read_torque() == -10

    def test_positive_torque_unchanged(self) -> None:
        client = MagicMock()
        client.read_register.return_value = 250
        assert Motor(client, slave_id=4).read_torque() == 250

    def test_operation_mode_display_is_signed(self) -> None:
        client = MagicMock()
        client.read_register.return_value = 0xFFFF  # INT8 -1
        assert Motor(client, slave_id=4).read_operation_mode() == -1


SWITCHED_ON = 0x0023  # status word: switched on, operation not enabled


def _client_with_status(status_word: int) -> MagicMock:
    client = MagicMock()
    client.read_register.return_value = status_word
    return client


class TestDisabledDriveIsNotEnabled:
    """Control words with bits 0-3 set mean "enable operation" in Switched On"""

    def test_stop_does_not_write_control_word(self) -> None:
        client = _client_with_status(SWITCHED_ON)
        Motor(client, slave_id=4).stop(wait=False)
        client.write_register.assert_not_called()
        client.write_register_32bit.assert_not_called()

    def test_move_refuses(self) -> None:
        client = _client_with_status(SWITCHED_ON)
        with pytest.raises(MotorStateError):
            Motor(client, slave_id=4).move_absolute(1000)
        client.write_register.assert_not_called()
        client.write_register_32bit.assert_not_called()


class TestStatus:
    def test_error_code_alone_is_not_a_fault(self) -> None:
        # Operation enabled, fault bit clear, warning code present (e.g. 0x7302)
        client = MagicMock()
        client.read_register.side_effect = lambda slave, address: {
            Registers.STATUS_WORD.address: 0x0027,
            Registers.ERROR_CODE.address: 0x7302,
        }.get(address, 0)
        client.read_register_32bit.return_value = 0
        status = Motor(client, slave_id=4).get_status()
        assert status.is_fault is False
        assert status.error_code == 0x7302


def test_homing_methods_match_manual() -> None:
    # Manual B06 section 5.7.4: 17-30 (CiA402), 35 (current position), 37/38 (blocking)
    assert {m.value for m in HomingMethod} == set(range(17, 31)) | {35, 37, 38}
    assert HomingMethod(18) is HomingMethod.POSITIVE_LIMIT_SWITCH
    assert HomingMethod(17) is HomingMethod.NEGATIVE_LIMIT_SWITCH
