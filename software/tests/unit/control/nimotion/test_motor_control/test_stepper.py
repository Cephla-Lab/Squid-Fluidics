"""
Motor helpers for the T5 stepper bring-up (Modbus client mocked)
"""

from unittest.mock import MagicMock, call

import pytest

from fluidics.control.nimotion.motor_control.motor import Motor, MotorStateError
from fluidics.control.nimotion.motor_control.registers import Registers

SWITCHED_ON = 0x0023
OPERATION_ENABLED = 0x0027
QUICK_STOP_ACTIVE = 0x0007
SWITCH_ON_DISABLED = 0x0040
FAULT = 0x0008
CONTROL = Registers.CONTROL_WORD.address


def _client(status_word: int, error_code: int = 0) -> MagicMock:
    client = MagicMock()
    client.read_register.side_effect = lambda slave, address: {
        Registers.STATUS_WORD.address: status_word,
        Registers.ERROR_CODE.address: error_code,
    }.get(address, 0)
    return client


class TestResetAlarm:
    """B03 8.3: Disable, then a rising edge of control word bit 7"""

    @pytest.mark.parametrize("status_word", [OPERATION_ENABLED, QUICK_STOP_ACTIVE])
    def test_refuses_unless_disabled(self, status_word: int) -> None:
        client = _client(status_word, 0x8613)
        with pytest.raises(MotorStateError):
            Motor(client, slave_id=5).reset_alarm()
        client.write_register.assert_not_called()

    def test_sends_the_edge_outside_the_fault_state(self) -> None:
        client = _client(SWITCHED_ON, 0x8613)
        assert Motor(client, slave_id=5).reset_alarm() == (SWITCHED_ON, 0x8613)
        assert client.write_register.call_args_list == [
            call(5, CONTROL, 0x0000),
            call(5, CONTROL, 0x0080),
            call(5, CONTROL, 0x0000),
        ]

    def test_sends_the_edge_in_the_fault_state(self) -> None:
        client = _client(FAULT, 0x8611)
        assert Motor(client, slave_id=5).reset_alarm() == (FAULT, 0x8611)
        assert client.write_register.call_args_list == [
            call(5, CONTROL, 0x0000),
            call(5, CONTROL, 0x0080),
            call(5, CONTROL, 0x0000),
        ]


class TestDisableVoltage:
    def test_leaves_quick_stop_active(self) -> None:
        words = iter([QUICK_STOP_ACTIVE, SWITCH_ON_DISABLED])
        client = MagicMock()
        client.read_register.side_effect = lambda slave, address: next(words)
        Motor(client, slave_id=5).disable_voltage()
        client.write_register.assert_called_once_with(5, CONTROL, 0x0000)

    def test_does_nothing_when_already_disabled(self) -> None:
        client = _client(SWITCH_ON_DISABLED)
        Motor(client, slave_id=5).disable_voltage()
        client.write_register.assert_not_called()


class TestRawAccess:
    def test_signed_16_bit_read(self) -> None:
        client = MagicMock()
        client.read_register.return_value = 0xFFFE
        motor = Motor(client, slave_id=5)
        assert motor.read_raw(0x012F) == 0xFFFE
        assert motor.read_raw(0x012F, signed=True) == -2

    def test_32_bit_write_of_a_negative_value(self) -> None:
        client = MagicMock()
        Motor(client, slave_id=5).write_raw(0x03EF, -2548, size=2)
        client.write_register_32bit.assert_called_once_with(5, 0x03EF, -2548, signed=True)

    @pytest.mark.parametrize("value, size", [(70000, 1), (-40000, 1), (2**32, 2), (5, 3)])
    def test_out_of_range_is_refused(self, value: int, size: int) -> None:
        client = MagicMock()
        with pytest.raises(ValueError):
            Motor(client, slave_id=5).write_raw(0x0094, value, size)
        client.write_register.assert_not_called()
        client.write_register_32bit.assert_not_called()


class TestBringUpWrites:
    def test_software_limits(self) -> None:
        client = MagicMock()
        Motor(client, slave_id=5).set_software_limits(-2548, 11650)
        assert client.write_register_32bit.call_args_list == [
            call(5, Registers.SOFTWARE_LIMIT_MIN.address, -2548, signed=True),
            call(5, Registers.SOFTWARE_LIMIT_MAX.address, 11650, signed=False),
        ]
        with pytest.raises(ValueError):
            Motor(client, slave_id=5).set_software_limits(10, -10)

    def test_restore_mode_and_cut_point_are_checked(self) -> None:
        motor = Motor(MagicMock(), slave_id=5)
        with pytest.raises(ValueError):
            motor.set_position_restore_mode(2)
        with pytest.raises(ValueError):
            motor.set_restore_blocking_position(70000)

    def test_slave_address_range(self) -> None:
        client = MagicMock()
        motor = Motor(client, slave_id=1)
        motor.set_slave_address(5)
        client.write_register.assert_called_once_with(1, Registers.SLAVE_ADDRESS.address, 5)
        with pytest.raises(ValueError):
            motor.set_slave_address(248)

    def test_di_config_addresses(self) -> None:
        client = MagicMock()
        client.read_register.side_effect = lambda slave, address: address
        motor = Motor(client, slave_id=5)
        assert motor.read_di_config(1) == (0x00D5, 0x00D6)
        assert motor.read_di_config(3) == (0x00D9, 0x00DA)
        with pytest.raises(ValueError):
            motor.read_di_config(4)
