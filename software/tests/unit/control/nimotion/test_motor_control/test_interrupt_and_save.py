"""
Interrupting waits, saving parameters, and all-axes helpers (Modbus client mocked)
"""

import threading
import time
from typing import Dict
from unittest.mock import MagicMock

import pytest

from fluidics.control.nimotion.high_level_api.axis_config import AxisName
from fluidics.control.nimotion.high_level_api.servo_service import ServoService
from fluidics.control.nimotion.modbus_rtu import NoResponseError
from fluidics.control.nimotion.motor_control.motor import (
    MotionInterruptedError,
    Motor,
    MotorError,
    MotorStateError,
)
from fluidics.control.nimotion.motor_control.registers import Registers

ENABLED = 0x0027  # operation enabled, target not reached
SWITCH_ON_DISABLED = 0x0040
SWITCHED_ON = 0x0023


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Motor, "DEFAULT_POLL_INTERVAL", 0.001)


def client_with(registers: Dict[int, int]) -> MagicMock:
    """Client whose 16/32-bit reads come from a dict that tests can change"""
    client = MagicMock()
    client.read_register.side_effect = lambda slave, address: registers.get(address, 0)
    client.read_register_32bit.side_effect = lambda slave, address, signed=False: registers.get(
        address, 0
    )
    return client


class TestInterruptibleWaits:
    def test_request_stop_ends_motion_wait(self) -> None:
        regs = {Registers.STATUS_WORD.address: ENABLED}
        motor = Motor(client_with(regs), slave_id=4)
        threading.Timer(0.05, motor.request_stop).start()
        start = time.time()
        with pytest.raises(MotionInterruptedError):
            motor._wait_for_motion_complete(timeout=5.0)
        assert time.time() - start < 2.0

    def test_quick_stop_ends_homing_wait(self) -> None:
        # Quick stop: the drive leaves Operation Enabled (here: Switch On Disabled)
        regs = {Registers.STATUS_WORD.address: ENABLED}
        motor = Motor(client_with(regs), slave_id=4)

        def e_stop() -> None:
            regs[Registers.STATUS_WORD.address] = SWITCH_ON_DISABLED

        threading.Timer(0.05, e_stop).start()
        with pytest.raises(MotionInterruptedError):
            motor._wait_for_homing_complete(timeout=5.0)

    def test_drive_still_enabling_is_not_an_interruption(self) -> None:
        """The old home() sequence may still be enabling when the wait starts"""
        regs = {Registers.STATUS_WORD.address: SWITCHED_ON}
        motor = Motor(client_with(regs), slave_id=4)

        def enabled_and_homed() -> None:
            regs[Registers.STATUS_WORD.address] = ENABLED  # bit 12 clear: homing running
            time.sleep(0.05)
            regs[Registers.MODES_OF_OPERATION_DISPLAY.address] = 6
            regs[Registers.STATUS_WORD.address] = ENABLED | 0x1000 | 0x0400  # attained

        threading.Timer(0.05, enabled_and_homed).start()
        motor._wait_for_homing_complete(timeout=5.0)

    def test_new_wait_is_not_affected_by_an_old_stop(self) -> None:
        regs = {Registers.STATUS_WORD.address: ENABLED | 0x0400}  # target reached
        motor = Motor(client_with(regs), slave_id=4)
        motor.request_stop()
        motor._wait_for_motion_complete(timeout=1.0)  # returns normally


class TestSaveParameters:
    def regs(self, status: int = SWITCHED_ON, bus_dv: int = 480) -> Dict[int, int]:
        return {
            Registers.STATUS_WORD.address: status,
            Registers.RATED_VOLTAGE.address: 48,
            Registers.BUS_VOLTAGE.address: bus_dv,
            Registers.SAVE_PARAMETERS.address: 1,
        }

    def test_saves_and_confirms(self) -> None:
        client = client_with(self.regs())
        Motor(client, slave_id=4).save_parameters()
        client.write_register_32bit.assert_called_once_with(
            4, Registers.SAVE_PARAMETERS.address, 0x65766173
        )

    def test_refuses_while_enabled(self) -> None:
        client = client_with(self.regs(status=ENABLED))
        with pytest.raises(MotorStateError):
            Motor(client, slave_id=4).save_parameters()
        client.write_register_32bit.assert_not_called()

    def test_refuses_on_low_supply(self) -> None:
        client = client_with(self.regs(bus_dv=350))  # 35.0 V < 80% of 48 V
        with pytest.raises(MotorError, match="below 80%"):
            Motor(client, slave_id=4).save_parameters()
        client.write_register_32bit.assert_not_called()

    def test_no_reply_while_writing_flash_is_not_a_failure(self) -> None:
        """The drive is silent while it writes flash; the read-back decides"""
        regs = self.regs()
        client = client_with(regs)
        silent = {"reads": 0}

        def save(*args: object) -> None:
            silent["reads"] = 2  # the next two reads get no reply either
            raise NoResponseError(4, 3)

        def read(slave: int, address: int) -> int:
            if silent["reads"]:
                silent["reads"] -= 1
                raise NoResponseError(slave, 3)
            return regs.get(address, 0)

        client.write_register_32bit.side_effect = save
        client.read_register.side_effect = read
        Motor(client, slave_id=4).save_parameters(timeout=1.0)
        client.write_register_32bit.assert_called_once()
        assert silent["reads"] == 0

    def test_unconfirmed_save_raises(self) -> None:
        regs = self.regs()
        regs[Registers.SAVE_PARAMETERS.address] = 0
        with pytest.raises(MotorError, match="not confirmed"):
            Motor(client_with(regs), slave_id=4).save_parameters(timeout=0.2)


class TestAllAxes:
    def test_quick_stop_all_tries_every_axis(self) -> None:
        service = ServoService()
        service._modbus_client = MagicMock(is_connected=True)
        motors = {axis: MagicMock() for axis in AxisName}
        motors[AxisName.Z4].quick_stop.side_effect = RuntimeError("no response")
        service._motors = motors  # type: ignore[assignment]

        with pytest.raises(MotorError, match="Z4"):
            service.quick_stop_all()
        for axis in AxisName:
            motors[axis].quick_stop.assert_called_once()
