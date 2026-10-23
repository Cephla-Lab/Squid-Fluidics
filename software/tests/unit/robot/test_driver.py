"""The one import site of the servo driver, and the adapter's contracts."""
import dataclasses
import threading
import time

import pytest

from fluidics.errors import AbortRequested, DeviceError, RunControl
from fluidics.robot import _driver
from fluidics.robot.stage import TOLERANCE_MM, XYZStage
from tests.conftest import driver_or_skip

AXES = {"x": "X1", "y": "Y2", "z": "Z3"}

# CiA402 status words (NiMotion manual B06): Operation Enabled, with the
# target reached; Quick Stop Active, whose low three bits read like enabled;
# Fault.
ENABLED, REACHED, QUICK_STOP, FAULT = 0x0027, 0x0400, 0x0007, 0x0008
READ_HOLDING_REGISTERS = 0x03


def test_the_shim_says_whether_a_driver_is_present():
    assert isinstance(_driver.DRIVER_AVAILABLE, bool)


def test_open_service_without_a_driver_raises_a_device_error(monkeypatch):
    """A tree whose vendored copy is missing or fails to import fails at
    bring-up, naming the copy and its VENDORED.md."""
    monkeypatch.setattr(_driver, "DRIVER_AVAILABLE", False)
    with pytest.raises(DeviceError, match="fluidics/control/nimotion.*VENDORED.md"):
        _driver.open_service("COM5", 115200, AXES)


def test_the_bring_up_error_names_why_the_driver_did_not_import(monkeypatch):
    """A copy that is present but lacks a dependency fails at bring-up
    too: the line says what the import tripped on, and chains it."""
    cause = ImportError("no module named serial")
    monkeypatch.setattr(_driver, "DRIVER_AVAILABLE", False)
    monkeypatch.setattr(_driver, "_IMPORT_ERROR", cause)
    with pytest.raises(DeviceError, match="no module named serial") as info:
        _driver.open_service("COM5", 115200, AXES)
    assert info.value.__cause__ is cause


@pytest.fixture
def driver():
    """The vendored driver, for the tests that need it: a skip when it
    does not import, a failure under ROBOT_DRIVER_REQUIRED=1."""
    return driver_or_skip()


@pytest.fixture
def fake_service(driver, monkeypatch):
    """A DriverService opened on the three axes over a recording fake of
    the driver's ServoService, whose one motor records the registers read
    (`reads`): (service, names, calls)."""
    calls = []

    class FakeMotor:
        def __init__(self, word, mode):
            self.word, self.mode = word, mode
            self.inputs = 0                      # 60FDh
            self.position = 0                    # 6064h, pulses
            self.error_code = 0                  # 603Fh
            self.reads = []

        def _read(self, register, value):
            self.reads.append(register)
            return value

        def read_status_word(self):
            return self._read("status word", self.word)

        def read_operation_mode(self):
            return self._read("operation mode", self.mode)

        def read_digital_inputs(self):
            return self._read("digital inputs", self.inputs)

        def read_error_code(self):
            return self._read("error code", self.error_code)

        def read_position(self):
            return self._read("position", self.position)

    class FakeServo:
        def __init__(self, port, baudrate, axis_configs):
            self.axis_configs = axis_configs
            self.current_axis = None
            self.motor = FakeMotor(0x1000, 6)
            calls.append(("init", sorted(a.value for a in axis_configs)))

        def connect(self):
            calls.append(("connect", self.current_axis.value))

        def initialize_motor_parameters(self, axis, verbose=True):
            calls.append(("init_axis", axis.value))

        def get_motor(self, axis):
            return self.motor

        def stop(self, axis, wait=True):
            calls.append(("stop", axis.value, wait))
            if axis.value == "Y2":
                raise OSError("no response")

        def fault_reset(self, axis):
            calls.append(("fault_reset", axis.value))

    monkeypatch.setattr(driver, "ServoService", FakeServo)
    service, names = driver.open_service("COM5", 115200, AXES)
    return service, names, calls


def test_the_loaded_driver_is_the_vendored_copy(driver):
    """Open question 4, decided 2026-10-12: the driver is vendored under
    fluidics/control/nimotion and nothing else is tried, so the driver a
    rig runs is the one in the repository, never whatever is installed."""
    assert driver.ServoService.__module__.startswith("fluidics.control.nimotion.")
    assert driver.simulation.__name__ == "fluidics.control.nimotion.simulation"


def test_connect_selects_a_configured_axis_and_initializes_all_three(fake_service):
    service, _, calls = fake_service
    service.connect()
    assert calls[0] == ("init", ["X1", "Y2", "Z3"])
    assert calls[1] == ("connect", "Z3")
    assert sorted(c[1] for c in calls if c[0] == "init_axis") == ["X1", "Y2", "Z3"]


def test_homing_state_reads_the_bits_and_the_mode(fake_service):
    service, names, _ = fake_service
    assert service.homing_state(names["z"]) == (True, False, False, True)
    service._service.motor.mode = 1
    assert service.homing_state(names["z"]).homing_mode is False
    service._service.motor.word = 0x2008
    reading = service.homing_state(names["z"])
    assert (reading.attained, reading.error, reading.fault) == (False, True, True)


def test_read_carries_the_limit_inputs_once_the_target_reads_reached(fake_service):
    """60FDh bits 0-1, asked only at a move's end as the driver's own wait
    asks them: a target reached on an active switch is not an arrival."""
    service, names, _ = fake_service
    motor = service._service.motor
    motor.word = ENABLED | REACHED
    motor.inputs = 0b10
    assert service.read(names["x"]).on_limit is True
    motor.inputs = 0b100                           # another input: not a limit
    assert service.read(names["x"]).on_limit is False
    motor.word = ENABLED                           # still on its way
    motor.inputs = 0b01
    motor.reads.clear()
    reading = service.read(names["x"])
    assert reading.target_reached is False and reading.on_limit is False
    assert motor.reads == ["status word", "error code", "position"], "no limit read mid-move"


def test_read_decodes_the_word_and_the_position_as_the_driver_does(fake_service):
    """Three registers, decoded as get_axis_status decodes them: the
    position through the axis's own pulses_to_mm, the fault and target
    bits, and enabled as the driver's state machine reads the word -- a
    quick stop sets the low bits an enabled drive does, and is not
    enabled."""
    service, names, _ = fake_service
    y = names["y"]
    motor = service._service.motor
    motor.word, motor.position = ENABLED | REACHED, service._configs[y].mm_to_pulses(42.0)
    motor.reads.clear()
    reading = service.read(y)
    assert reading.position_mm == pytest.approx(42.0, abs=0.001)
    assert (reading.target_reached, reading.enabled, reading.fault, reading.error_code) == (
        True, True, False, 0)
    assert motor.reads == ["status word", "error code", "position", "digital inputs"]
    motor.word, motor.error_code = FAULT, 0x2310
    reading = service.read(y)
    assert (reading.target_reached, reading.enabled, reading.fault, reading.error_code) == (
        False, False, True, 0x2310)
    motor.word, motor.error_code = QUICK_STOP, 0
    assert service.read(y).enabled is False


def test_stop_is_the_waited_controlled_stop_and_a_failure_is_a_device_error(fake_service):
    service, names, calls = fake_service
    service.stop(names["x"])
    assert ("stop", "X1", True) in calls
    with pytest.raises(DeviceError, match="axis Y2: stop failed"):
        service.stop(names["y"])


def test_reset_fault_is_the_drivers_fault_reset(fake_service):
    service, names, calls = fake_service
    service.reset_fault(names["z"])
    assert ("fault_reset", "Z3") in calls


def test_strokes_come_from_the_driver_table(fake_service):
    service, names, _ = fake_service
    assert service.stroke(names["x"]) == (0.5, 200.0)       # every stroke starts clear of the origin switch
    assert service.stroke(names["y"]) == (0.5, 109.0)       # measured between Y2's switches (nm-servo #15)
    assert service.stroke(names["z"]) == (0.5, 58.5)


def test_an_axis_the_driver_does_not_know_is_a_device_error(driver):
    with pytest.raises(DeviceError, match="no axis named 'X9'"):
        driver.open_service("COM5", 115200, {**AXES, "x": "X9"})


def test_the_adapter_runs_a_leg_end_to_end_on_the_driver_simulator(driver, real_clock):
    """nm-servo's own simulator (PR #4) behind the port name sim://: the
    real ServoService, Motor and Modbus client, no hardware. The simulator
    homes to the low end of the stroke and ignores polarity, so Z3 reads
    about 0 after homing, which is the top under this rig's convention.
    Homing parks the axis a little past 0 (0.12 mm here; 0.12 to 0.5 mm on
    the drives, nm-servo PR #12), so the travel height must sit below the
    park: a travel height of 0 is refused by home(). The stage then clears
    each axis to its stroke's start (0.5 mm)."""
    # the simulated drives home in real time (about 7 s an axis at the homing
    # speeds); this bus runs its clock 20 times faster
    driver.simulation.install_bus("sim://fast", driver.simulation.SimulatedBus(
        list(driver.DEFAULT_AXIS_CONFIGS.values()), time_scale=20.0))
    service, names = driver.open_service("sim://fast", 115200, AXES)
    stage = XYZStage(service, names, travel_z_mm=1.0, xy_velocity_mm_s=100.0,
                     z_velocity_mm_s=50.0, run_control=RunControl())
    stage.connect()
    try:
        stage.home()
        assert stage.is_retracted
        stage.move_xy(5.0, 5.0)
        stage.move_z(3.0)
        x, y, z = stage.position()
        assert (round(x, 1), round(y, 1), round(z, 1)) == (5.0, 5.0, 3.0)
        stage.halt()
        assert service.read(names["z"]).enabled
    finally:
        stage.close(quiescent=True)


def test_a_read_of_a_drive_that_is_not_connected_is_a_device_error(fake_service):
    """Where get_axis_status read a disconnected axis as zeroed and
    disabled, the three-register read asks the motor, and a service that
    is not connected says so: the stage's early-end path takes it as any
    failed status read."""
    service, names, _ = fake_service

    def not_connected(axis):
        raise RuntimeError("Not connected to the servo system")

    service._service.get_motor = not_connected
    with pytest.raises(DeviceError, match="axis X1: status read failed: Not connected"):
        service.read(names["x"])


def test_read_asks_three_registers_and_agrees_with_the_drivers_own_status(driver, real_clock):
    """On the driver's own simulator: each poll is three requests on the
    bus (status word, error code, position; a fourth, the limit inputs,
    once the target reads reached), where get_axis_status makes six, and
    what it returns is what get_axis_status says -- position, target
    reached, enabled, fault, error code -- read by read across a move and
    into a fault. The bus clock is held still around each pair of reads, so
    both see the same instant of the drive."""
    bus = driver.simulation.install_bus("sim://reads", driver.simulation.SimulatedBus(
        list(driver.DEFAULT_AXIS_CONFIGS.values()), time_scale=20.0))
    service, names = driver.open_service("sim://reads", 115200, AXES)
    stage = XYZStage(service, names, travel_z_mm=1.0, xy_velocity_mm_s=100.0,
                     z_velocity_mm_s=50.0, run_control=RunControl())
    stage.connect()
    x = names["x"]

    def both():
        requests.clear()
        reading = service.read(x)
        asked = list(requests)
        status = service._service.get_axis_status(x)
        assert (reading.position_mm, reading.target_reached, reading.enabled,
                reading.fault, reading.error_code) == (
            status.position_mm, status.is_target_reached, status.is_enabled,
            status.is_fault, status.error_code)
        return reading, asked

    try:
        # Not homed: the simulated X starts mid-stroke and move_to does not ask
        # for homing, so the three axes' homing (3 s at this time scale) buys
        # nothing read here.
        now = [bus.now()]
        bus.now = lambda: now[0]                  # the drives advance when the test says
        requests = []
        handle = bus.handle_frame
        bus.handle_frame = lambda frame: requests.append(frame[1]) or handle(frame)
        service.move_to(40.0, 100.0, x)
        readings = []
        for _ in range(100):
            reading, asked = both()
            assert asked == [READ_HOLDING_REGISTERS] * (4 if reading.target_reached else 3)
            readings.append(reading)
            if reading.target_reached:
                break
            now[0] += 0.05
        assert len(readings) > 3, "the move was read on its way, not only at its end"
        assert readings[-1].target_reached
        assert readings[-1].position_mm == pytest.approx(40.0, abs=TOLERANCE_MM)
        bus.inject_fault(driver.DEFAULT_AXIS_CONFIGS[x].slave_id, 0x2310)
        reading, asked = both()
        assert (reading.enabled, reading.fault, reading.error_code) == (False, True, 0x2310)
        assert asked == [READ_HOLDING_REGISTERS] * (4 if reading.target_reached else 3)
    finally:
        # The bus's own clock and frame handler again, however the test
        # ended: a frozen clock would make the reset and the close's stop
        # wait out their timeouts.
        bus.__dict__.pop("now", None)
        bus.__dict__.pop("handle_frame", None)
        service.reset_fault(x)
        stage.close(quiescent=True)


def test_a_move_after_a_halt_goes_to_the_new_target_not_the_old_one(driver, real_clock):
    """nm-servo PR #4 found that without PR #1 a move after Stop first
    resumes the halted move (Halt released re-arms the old set-point).
    The stage's halt contract needs the driver to have PR #1: pinned here
    on the driver's own simulator, which models the drive's handshake. Its
    bus runs 3 times real time: homing the three axes is 20 s of the test
    at real time, and at 3x the cancel still lands well before X would
    reach 195, the far end of its stroke (at the
    siblings' 20x it does not land in time). The cancel waits for a reading (X past 30 mm),
    not the clock, and the move back is 5 mm short of where X stopped, so
    a rise past the stop can only be the old move resuming, however loaded
    the machine is."""
    control = RunControl()
    driver.simulation.install_bus("sim://halt", driver.simulation.SimulatedBus(
        list(driver.DEFAULT_AXIS_CONFIGS.values()), time_scale=3.0))
    service, names = driver.open_service("sim://halt", 115200, AXES)
    stage = XYZStage(service, names, 1.0, 100.0, 50.0, control)
    stage.connect()
    try:
        stage.home()
        move_over = threading.Event()

        def cancel_past_30_mm():
            while not move_over.is_set():
                if service.read(names["x"]).position_mm >= 30.0:
                    control.cancel(AbortRequested("stop"))
                    return
                time.sleep(0.01)
        canceller = threading.Thread(target=cancel_past_30_mm)
        canceller.start()
        try:
            with pytest.raises(AbortRequested):
                stage.move_xy(195.0, 1.0)             # 1.95 s of drive time at 100 mm/s, cancelled past 30 mm
        finally:
            move_over.set()
            canceller.join()
        stopped_at = stage.position()[0]
        assert 30.0 <= stopped_at < 195.0
        back_to = stopped_at - 5.0                    # 25 mm or more: inside the stroke
        seen = []
        stage2 = XYZStage(service, names, 1.0, 100.0, 50.0, RunControl())
        stage2._position = stage.position()
        stage2._homed = True
        done = threading.Event()

        def sample():
            while not done.is_set():
                seen.append(service.read(names["x"]).position_mm)
                time.sleep(0.01)
        sampler = threading.Thread(target=sample)
        sampler.start()
        try:
            stage2.move_xy(back_to, 1.0)
        finally:
            done.set()
            sampler.join()
        assert max(seen) <= stopped_at + 1.0, "the halted move did not resume toward 195"
        assert service.read(names["x"]).position_mm == pytest.approx(back_to, abs=TOLERANCE_MM)
    finally:
        stage.close(quiescent=True)


def test_a_limit_stop_on_the_driver_simulator_ends_the_leg_at_once(driver, real_clock):
    """nm-servo PR #10: the simulated drive stops on its switch as the
    drives do (enabled, warning bit, alarm 8614h, target never reached).
    The stage polls with wait=False, so it never sees the driver's own
    LimitSwitchError: its poll must read the alarm itself."""
    y2 = driver.AxisName.Y2
    drive = dataclasses.replace(driver.DEFAULT_AXIS_CONFIGS[y2], stroke_max=50.0)  # its switch at 51 mm
    driver.simulation.install_bus("sim://limit", driver.simulation.SimulatedBus([drive], time_scale=20.0))
    service, names = driver.open_service("sim://limit", 115200, AXES)
    stage = XYZStage(service, names, 1.0, 100.0, 50.0, RunControl())
    stage.connect()
    try:
        stage.home()
        start = time.monotonic()
        with pytest.raises(DeviceError, match=r"Y2.*move to 58\.00 mm stopped by a limit switch at"):
            stage.move_xy(5.0, 58.0)                  # 58 mm is inside the configured 102 mm stroke
        assert time.monotonic() - start < 2.0, "not the leg's timeout (2.2 s)"
        # the drive decelerates past the switch at its quick-stop rate: v^2 / 2a =
        # 100^2 / (2 * 1000) = 5 mm, plus a poll's worth of travel (the stage polls
        # the drive's simulated clock 20 times slower than its own)
        assert 51.0 < stage.position()[1] < 57.0
        assert service.read(names["y"]).enabled
        stage.move_xy(5.0, 20.0)                      # moving back off the switch works
        assert round(stage.position()[1], 1) == 20.0
    finally:
        stage.close(quiescent=True)
