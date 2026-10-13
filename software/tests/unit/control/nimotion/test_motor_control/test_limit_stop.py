"""
Moves that a limit switch ends (Modbus client mocked)
"""

import time
from typing import Dict
from unittest.mock import MagicMock

import pytest

from fluidics.control.nimotion.motor_control import LimitSwitchError, Motor, MoveIncompleteError
from fluidics.control.nimotion.motor_control.registers import Registers

R = Registers
ON_LIMIT = 0x02B7  # operation enabled + warning, target not reached (seen on the drives)
REACHED = 0x0637  # operation enabled + target reached
ABSOLUTE, RELATIVE = 0x000F, 0x004F  # control word after the set-point, bit 6 = relative


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Motor, "DEFAULT_POLL_INTERVAL", 0.001)


def motor(regs: Dict[int, int]) -> Motor:
    client = MagicMock()
    client.read_register.side_effect = lambda slave, address: regs.get(address, 0)
    client.read_register_32bit.side_effect = lambda slave, address, signed=False: regs.get(
        address, 0
    )
    return Motor(client, slave_id=1)


def registers(
    status: int, control: int, target: int, position: int, error: int = 0
) -> Dict[int, int]:
    return {
        R.STATUS_WORD.address: status,
        R.CONTROL_WORD.address: control,
        R.TARGET_POSITION.address: target,
        R.POSITION_ACTUAL_VALUE.address: position,
        R.POSITION_WINDOW.address: 10,
        R.ERROR_CODE.address: error,
    }


def test_limit_stop_raises_at_once() -> None:
    m = motor(registers(ON_LIMIT, ABSOLUTE, 50000, 31000, error=0x8614))
    start = time.monotonic()
    with pytest.raises(LimitSwitchError, match="31000"):
        m._wait_for_motion_complete(timeout=30)
    assert time.monotonic() - start < 1


def test_limit_stop_waits_for_standstill() -> None:
    """A set-point sent during the limit reaction is ignored (Y2), so raise once still"""
    regs = registers(ON_LIMIT, ABSOLUTE, 50000, 31000, error=0x8614)
    m = motor(regs)
    speeds = [120, 40, 5, 0, 0]
    reads = m._client.read_register_32bit.side_effect

    def read32(slave: int, address: int, signed: bool = False) -> int:
        if address == R.VELOCITY_ACTUAL_VALUE.address:
            return speeds.pop(0) if speeds else 0
        return reads(slave, address, signed)

    m._client.read_register_32bit.side_effect = read32
    with pytest.raises(LimitSwitchError):
        m._wait_for_motion_complete(timeout=30)
    assert speeds == []  # waited through the deceleration


def test_move_ending_on_a_limit_input_is_an_error() -> None:
    """X's far switch once let the drive run through to the target past it"""
    regs = registers(REACHED, ABSOLUTE, 50000, 50000)
    regs[R.DIGITAL_INPUTS.address] = 0b01  # 60FDh: negative limit active
    with pytest.raises(LimitSwitchError, match="ended on a limit switch"):
        motor(regs)._wait_for_motion_complete(timeout=1)


def test_other_warnings_do_not_end_the_wait() -> None:
    regs = registers(ON_LIMIT, ABSOLUTE, 50000, 50000, error=0x7302)  # encoder battery warning
    m = motor(regs)
    regs[R.STATUS_WORD.address] = REACHED | 0x0080
    m._wait_for_motion_complete(timeout=1)


def test_reported_finished_short_of_the_target() -> None:
    m = motor(registers(REACHED, ABSOLUTE, 50000, 30000))
    with pytest.raises(MoveIncompleteError, match="-20000"):
        m._wait_for_motion_complete(timeout=1)


def test_finished_within_tolerance() -> None:
    m = motor(registers(REACHED, ABSOLUTE, 50000, 50394))  # X once settled 394 units past
    m._wait_for_motion_complete(timeout=1)


def test_relative_moves_are_not_position_checked() -> None:
    m = motor(registers(REACHED, RELATIVE, 5000, 123456))  # 607Ah holds a distance
    m._wait_for_motion_complete(timeout=1)
