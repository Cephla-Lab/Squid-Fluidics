"""
Profile Position motion tests against a simulated drive

FakePPDrive models the PP-mode behavior described in the NiMotion Modbus
manual (B06, 5.3.3-5.3.4), not a real drive:

- a rising edge on control word bit 4 is accepted only while status bit 12 is
  0; acceptance sets bit 12, and releasing bit 4 clears it
- with bit 5 set the new set-point replaces the active move, otherwise it is
  queued behind it
- Halt (bit 8) stops motion; clearing Halt continues toward the active target
- control words with bits 0-3 = 1111 enable a drive that is Switched On

Time advances by one step on every register read.
"""

from typing import List, Optional
from unittest.mock import MagicMock

import pytest

from fluidics.control.nimotion.high_level_api.axis_config import AxisName
from fluidics.control.nimotion.high_level_api.servo_service import ServoService
from fluidics.control.nimotion.motor_control.motor import Motor, MotorStateError
from fluidics.control.nimotion.motor_control.registers import OperationMode, Registers

STEP = 100  # position change per simulated tick while moving


class FakePPDrive:
    """Stands in for ModbusClient, simulating one drive in PP / HM mode"""

    is_connected = True

    def __init__(self, enabled: bool = True, position: int = 0) -> None:
        self.enabled = enabled
        self.position = position
        self.cw = 0x000F if enabled else 0x0007
        self.mode = OperationMode.PROFILE_POSITION
        self.target_register = 0
        self.active_target: Optional[int] = None
        self.queue: List[int] = []
        self.set_point_ack = False
        self.homing_ticks: Optional[int] = None
        self.homing_attained = False
        self.moving = False
        self.max_position = position
        self.control_words: List[int] = []

    # --- simulation ---------------------------------------------------

    def _tick(self) -> None:
        self.moving = False
        if not self.enabled or self.cw & 0x0100:  # disabled or halted
            return
        if self.mode == OperationMode.HOMING and self.homing_ticks is not None:
            self.homing_ticks -= 1
            self.moving = True
            if self.homing_ticks == 0:
                self.position, self.homing_ticks, self.homing_attained = 0, None, True
            return
        if self.mode == OperationMode.PROFILE_POSITION and self.active_target is not None:
            delta = self.active_target - self.position
            if delta:
                self.position += max(-STEP, min(STEP, delta))
                self.max_position = max(self.max_position, self.position)
                self.moving = True
            elif self.queue:
                self.active_target = self.queue.pop(0)

    def _status_word(self) -> int:
        sw = 0x0027 if self.enabled else 0x0023
        if self.mode == OperationMode.HOMING:
            if self.homing_attained:
                sw |= 0x1000
            if self.homing_ticks is None:
                sw |= 0x0400
        else:
            if self.set_point_ack:
                sw |= 0x1000
            halted = bool(self.cw & 0x0100)
            at_target = self.active_target in (None, self.position) and not self.queue
            if (halted and not self.moving) or (not halted and at_target):
                sw |= 0x0400
        return sw

    def _write_control_word(self, value: int) -> None:
        self.control_words.append(value)
        rising = value & 0x0010 and not self.cw & 0x0010
        falling = self.cw & 0x0010 and not value & 0x0010
        if not self.enabled and value & 0x000F == 0x000F:
            self.enabled = True  # CiA402 transition 4
        if value & 0x000F == 0x0006:
            self.enabled = False
        self.cw = value
        if not self.enabled:
            return
        if self.mode == OperationMode.HOMING:
            if rising:
                self.homing_ticks, self.homing_attained = 5, False
            return
        if falling:
            self.set_point_ack = False
        if rising and not self.set_point_ack:
            target = self.target_register
            if value & 0x0040:  # relative
                target += self.position
            if value & 0x0020 or self.active_target in (None, self.position):
                self.active_target, self.queue = target, []
            else:
                self.queue.append(target)
            self.set_point_ack = True

    # --- ModbusClient interface ----------------------------------------

    def read_register(self, slave_id: int, address: int) -> int:
        self._tick()
        if address == Registers.STATUS_WORD.address:
            return self._status_word()
        if address == Registers.CONTROL_WORD.address:
            return self.cw
        if address == Registers.MODES_OF_OPERATION_DISPLAY.address:
            return int(self.mode)
        return 0

    def read_register_32bit(self, slave_id: int, address: int, signed: bool = False) -> int:
        self._tick()
        if address == Registers.POSITION_ACTUAL_VALUE.address:
            return self.position
        if address == Registers.TARGET_POSITION.address:
            return self.target_register
        if address == Registers.VELOCITY_ACTUAL_VALUE.address:
            return 1000 if self.moving else 0
        return 0

    def write_register(self, slave_id: int, address: int, value: int) -> None:
        if address == Registers.CONTROL_WORD.address:
            self._write_control_word(value)
        elif address == Registers.MODES_OF_OPERATION.address:
            self.mode = OperationMode(value)

    def write_register_32bit(
        self, slave_id: int, address: int, value: int, signed: bool = False
    ) -> None:
        if address == Registers.TARGET_POSITION.address:
            self.target_register = value

    def run(self, ticks: int) -> None:
        """Let time pass"""
        for _ in range(ticks):
            self._tick()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fluidics.control.nimotion.motor_control.motor.time.sleep", lambda s: None)


def make_service(drive: FakePPDrive) -> ServoService:
    service = ServoService()
    service._modbus_client = MagicMock(is_connected=True)
    service._motors = {AxisName.Z4: Motor(drive, slave_id=4)}  # type: ignore[arg-type]
    return service


class TestMoves:
    def test_move_completes_at_new_target(self) -> None:
        drive = FakePPDrive(position=0)
        Motor(drive, slave_id=4).move_absolute(1000)  # type: ignore[arg-type]
        assert drive.position == 1000

    def test_move_after_halt_goes_to_new_target(self) -> None:
        """A halted move must not resume when the next move starts"""
        drive = FakePPDrive(position=0)
        motor = Motor(drive, slave_id=4)  # type: ignore[arg-type]
        motor.move_absolute(100_000, wait=False)
        drive.run(5)
        motor.stop(wait=False)
        drive.run(5)
        halted_at = drive.position
        motor.move_absolute(0)
        assert drive.position == 0
        assert drive.max_position == halted_at  # never continued toward 100000

    def test_move_refuses_on_disabled_drive(self) -> None:
        drive = FakePPDrive(enabled=False)
        with pytest.raises(MotorStateError):
            Motor(drive, slave_id=4).move_absolute(1000)  # type: ignore[arg-type]
        assert drive.enabled is False


class TestStop:
    def test_stop_and_wait_cancels_the_move(self) -> None:
        drive = FakePPDrive(position=0)
        motor = Motor(drive, slave_id=4)  # type: ignore[arg-type]
        motor.move_absolute(100_000, wait=False)
        drive.run(5)
        motor.stop(wait=True)
        stopped_at = drive.position
        drive.run(50)
        assert drive.position == stopped_at
        assert drive.cw & 0x0100 == 0  # Halt released without resuming

    def test_stop_on_disabled_drive_writes_nothing(self) -> None:
        drive = FakePPDrive(enabled=False)
        Motor(drive, slave_id=4).stop(wait=False)  # type: ignore[arg-type]
        assert drive.control_words == []
        assert drive.enabled is False


class TestJog:
    def test_reverse_jog_after_release(self) -> None:
        """Jog -, release, jog +: the axis must move +"""
        drive = FakePPDrive(position=300_000)
        service = make_service(drive)
        service.jog(-20.0, axis=AxisName.Z4)
        drive.run(5)
        service.stop(axis=AxisName.Z4, wait=False)  # button released
        drive.run(5)
        released_at = drive.position
        service.jog(20.0, axis=AxisName.Z4)
        drive.run(5)
        assert drive.position > released_at

    def test_jog_stops_at_stroke_end(self) -> None:
        drive = FakePPDrive(position=0)
        service = make_service(drive)
        config = service.get_axis_config(AxisName.Z4)
        service.jog(50.0, axis=AxisName.Z4)
        drive.run(20_000)
        assert drive.position == config.mm_to_pulses(config.stroke_max)


class TestHoming:
    def test_home_keeps_an_enabled_drive_enabled(self) -> None:
        drive = FakePPDrive(position=5000)
        Motor(drive, slave_id=4).home()  # type: ignore[arg-type]
        assert drive.position == 0
        assert 0x0006 not in drive.control_words  # no Shutdown

    def test_home_enables_a_disabled_drive(self) -> None:
        drive = FakePPDrive(enabled=False, position=5000)
        Motor(drive, slave_id=4).home()  # type: ignore[arg-type]
        assert drive.enabled is True
        assert drive.position == 0

    def test_move_after_homing(self) -> None:
        drive = FakePPDrive(position=5000)
        motor = Motor(drive, slave_id=4)  # type: ignore[arg-type]
        motor.home()
        motor.move_absolute(2000)
        assert drive.position == 2000
