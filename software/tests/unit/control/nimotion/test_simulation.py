"""
Drive simulator: frames, CiA402 behaviour, and the library running on it
"""

import itertools
import struct

import pytest

from fluidics.control.nimotion import AxisName, ServoService
from fluidics.control.nimotion.high_level_api.axis_config import DEFAULT_AXIS_CONFIGS
from fluidics.control.nimotion.modbus_rtu.crc import append_crc, verify_crc
from fluidics.control.nimotion.motor_control.registers import Registers as R
from fluidics.control.nimotion.simulation import SimulatedBus, SimulatedDrive, install_bus, reset_buses
from fluidics.control.nimotion.simulation.drive import FAULT_HOMING_TIMEOUT, WARNING_LIMIT_SWITCH

Y2 = DEFAULT_AXIS_CONFIGS[AxisName.Y2]  # no brake, so enable() does not sleep
PPM = Y2.pulses_per_mm
T5 = DEFAULT_AXIS_CONFIGS[AxisName.T5]

_urls = itertools.count()


@pytest.fixture(autouse=True)
def _fresh_buses():
    yield
    reset_buses()


class Drive:
    """A SimulatedDrive with its own clock, configured like ServoService.connect does"""

    def __init__(self, config=Y2):
        self.d = SimulatedDrive(config, now=0.0)
        self.t = 0.0
        self.w(R.DI2_FUNCTION.address, config.di2_function)
        self.w(R.DI2_LOGIC.address, config.di2_logic)
        self.w(R.DI3_FUNCTION.address, config.di3_function)
        self.w(R.DI3_LOGIC.address, config.di3_logic)
        self.w(R.HOMING_TIMEOUT.address, config.homing_timeout)
        for register, value in (
            (R.MAX_ACCELERATION, 5000 * PPM),  # the factory 500000 would cap the profile
            (R.MAX_DECELERATION, 5000 * PPM),
            (R.PROFILE_ACCELERATION, 500 * PPM),
            (R.PROFILE_DECELERATION, 500 * PPM),
            (R.QUICK_STOP_DECELERATION, 1000 * PPM),
            (R.HOMING_SPEED_HIGH, 20 * PPM),
            (R.HOMING_SPEED_LOW, 4 * PPM),
            (R.HOMING_ACCELERATION, 100 * PPM),
        ):
            self.w32(register.address, int(value))

    def w(self, address, *values):
        self.d.write(address, list(values))

    def w32(self, address, value):
        value &= 0xFFFFFFFF
        self.w(address, value >> 16, value & 0xFFFF)

    def cw(self, *words):
        for word in words:
            self.w(R.CONTROL_WORD.address, word)

    def run(self, seconds):
        self.t += seconds
        self.d.advance(self.t)

    def run_until(self, condition, limit=30.0):
        start = self.t
        while not condition():
            assert self.t - start < limit, "condition not reached"
            self.run(0.01)

    @property
    def sw(self):
        return self.d.status_word()

    @property
    def mm(self):
        return self.d.position() / PPM

    def enable(self):
        self.cw(0x06, 0x07, 0x0F)
        assert self.sw & 0x6F == 0x27

    def goto(self, mm):
        """Move to mm and wait until there"""
        self.move(mm)
        self.cw(0x0F)
        self.run_until(lambda: self.sw & 0x0400)

    def move(self, mm, word=0x1F):
        self.w(R.MODES_OF_OPERATION.address, 1)
        self.w32(R.PROFILE_VELOCITY.address, int(50 * PPM))
        self.w32(R.TARGET_POSITION.address, round(mm * PPM))
        self.cw(word & ~0x0110, word)


# ------------------------------------------------------------ frames


def frame(*parts):
    return append_crc(bytes(parts))


def test_read_answers_with_crc():
    bus = SimulatedBus([Y2])
    response = bus.handle_frame(frame(2, 0x03, 0x00, 0x63, 0x00, 0x01))  # rated voltage
    assert verify_crc(response)
    assert response[:3] == bytes([2, 0x03, 2])
    assert struct.unpack(">H", response[3:5])[0] == 48


def test_unknown_slave_and_bad_crc_get_no_answer():
    bus = SimulatedBus([Y2])
    assert bus.handle_frame(frame(9, 0x03, 0x03, 0x81, 0x00, 0x01)) == b""
    corrupted = bytearray(frame(2, 0x03, 0x03, 0x81, 0x00, 0x01))
    corrupted[-1] ^= 0xFF
    assert bus.handle_frame(bytes(corrupted)) == b""


def test_write_to_read_only_register_is_an_exception():
    bus = SimulatedBus([Y2])
    response = bus.handle_frame(frame(2, 0x06, 0x03, 0x81, 0x00, 0x00))  # status word
    assert response[1] == 0x86 and response[2] == 0x02


def test_factory_limits_cap_the_profile_acceleration():
    """60C5h / 60C6h are 500000 from the factory; the X stage's drive capped 6083h-6085h"""
    d = SimulatedDrive(Y2, now=0.0)
    value = int(500 * PPM)
    for register in (R.PROFILE_ACCELERATION, R.PROFILE_DECELERATION, R.QUICK_STOP_DECELERATION):
        d.write(register.address, [value >> 16, value & 0xFFFF])
        high, low = d.read(register.address, 2)
        assert (high << 16) | low == 500000


def test_offline_drive_ignores_frames():
    bus = SimulatedBus([Y2])
    bus.set_online(2, False)
    assert bus.handle_frame(frame(2, 0x03, 0x03, 0x81, 0x00, 0x01)) == b""


# ------------------------------------------------------------ state machine


def test_enable_sequence_and_quick_stop():
    d = Drive()
    assert d.sw & 0x4F == 0x40  # switch on disabled
    d.cw(0x0F)  # enable operation is not allowed from switch on disabled
    assert d.sw & 0x4F == 0x40
    d.enable()
    d.move(30)
    d.run(0.3)
    d.cw(0x02)  # quick stop: ramp down, then switch on disabled (605Ah = 2)
    assert d.sw & 0x6F == 0x07
    d.run(1.0)
    assert d.sw & 0x4F == 0x40
    assert d.d._velocity == 0


def test_fault_and_reset():
    d = Drive()
    d.enable()
    d.d.inject_fault(0x2300)
    assert d.sw & 0x08
    assert d.d.error_code() == 0x2300
    d.cw(0x80)
    assert d.sw & 0x4F == 0x40
    assert d.d.error_code() == 0


# ------------------------------------------------------------ profile position


def test_absolute_and_relative_moves():
    d = Drive()
    d.enable()
    start = d.mm
    d.move(30)
    assert d.sw & 0x1000  # set-point acknowledged
    assert not d.sw & 0x0400  # target not reached
    d.run_until(lambda: d.sw & 0x0400)
    assert d.mm == pytest.approx(30, abs=1e-3)
    d.move(-5, word=0x5F)  # relative
    d.run_until(lambda: d.sw & 0x0400)
    assert d.mm == pytest.approx(25, abs=1e-3)
    assert start != 25


def test_set_point_ack_follows_bit_4():
    d = Drive()
    d.enable()
    d.move(30)
    assert d.sw & 0x1000
    d.cw(0x0F)
    assert not d.sw & 0x1000


def test_set_point_waits_for_the_move_in_progress_unless_immediate():
    d = Drive()
    d.enable()
    d.goto(5)
    d.move(40)
    d.run(0.2)
    d.cw(0x0F)
    d.move(10)  # buffered: finishes 40 first
    d.run_until(lambda: d.mm > 39.9)
    d.run_until(lambda: d.sw & 0x0400)
    assert d.mm == pytest.approx(10, abs=1e-3)

    d.move(40)
    d.run(0.2)
    d.cw(0x0F)
    peak = 0.0
    d.move(10, word=0x3F)  # change set immediately
    while not d.sw & 0x0400:
        d.run(0.01)
        peak = max(peak, d.mm)
    assert d.mm == pytest.approx(10, abs=1e-3)
    assert peak < 39


def test_halt_holds_and_releasing_it_continues_the_move():
    """B06 5.3.4: halt = 0 means continue positioning"""
    d = Drive()
    d.enable()
    d.goto(5)
    d.move(40)
    d.run(0.3)
    d.cw(0x010F)  # halt
    d.run(0.5)
    assert d.d._velocity == 0
    assert d.sw & 0x0400  # halted at rest counts as target reached
    held = d.mm
    assert held < 39
    d.cw(0x000F)  # release halt, no new set-point
    d.run_until(lambda: d.sw & 0x0400)
    assert d.mm == pytest.approx(40, abs=1e-3)


def test_limit_switch_stops_the_axis():
    """As on the drives: enabled, warning bit, alarm 8614h, target never reached"""
    d = Drive()
    d.enable()
    d.move(Y2.stroke_max + 20)  # beyond the far limit switch
    d.run_until(lambda: d.sw & 0x0080 and d.d._velocity == 0)
    d.run(1.0)
    assert d.d.error_code() == WARNING_LIMIT_SWITCH
    assert d.sw & 0x6F == 0x27  # still operation enabled: an alarm, not a fault
    assert not d.sw & 0x0400  # the move stays unfinished
    assert not d.sw & 0x0800
    d.move(10, word=0x3F)  # moving away is allowed (a queued set-point would wait behind the stop)
    d.run_until(lambda: d.sw & 0x0400)
    assert d.mm == pytest.approx(10, abs=1e-3)
    assert d.d.error_code() == 0


# ------------------------------------------------------------ homing


def home(d, method):
    d.w(R.HOMING_METHOD.address, method)
    d.w(R.MODES_OF_OPERATION.address, 6)
    d.cw(0x0F, 0x1F)


def test_homing_on_the_limit_switch():
    d = Drive()
    d.enable()
    home(d, 17)
    assert not d.sw & 0x1400  # in progress
    d.run_until(lambda: d.sw & 0x1400 == 0x1400)
    assert 0 <= d.mm < 0.2  # stops just past the switch edge
    assert d.d.error_code() == 0


def test_homing_on_current_position_is_immediate():
    d = Drive()
    d.enable()
    home(d, 35)
    assert d.sw & 0x1400 == 0x1400
    assert d.mm == 0


def test_homing_timeout_faults():
    d = Drive()
    d.w(R.HOMING_TIMEOUT.address, 200)
    d.enable()
    home(d, 17)
    d.run(1.0)
    assert d.sw & 0x08
    assert d.d.error_code() == FAULT_HOMING_TIMEOUT


def test_homing_without_limit_inputs_runs_into_the_end_stop():
    d = Drive()
    d.w(R.DI2_FUNCTION.address, 0)  # limit inputs not configured
    d.w(R.DI3_FUNCTION.address, 0)
    d.enable()
    home(d, 17)
    d.run_until(lambda: d.sw & 0x08)
    assert d.d.error_code() == 0x8611  # following error


# ------------------------------------------------------------ the T5 tilt


def r32(drive, address, signed=True):
    high, low = drive.read(address, 2)
    value = (high << 16) | low
    return value - (1 << 32) if signed and value & 0x80000000 else value


def test_axis_without_limit_inputs_has_no_end_stops():
    d = Drive(T5)
    d.enable()
    target = round(400.0 * T5.pulses_per_mm)  # far past any stroke
    d.w(R.MODES_OF_OPERATION.address, 1)
    d.w32(R.PROFILE_VELOCITY.address, round(90 * T5.pulses_per_mm))
    d.w32(R.TARGET_POSITION.address, target)
    d.cw(0x0F, 0x1F)
    d.run_until(lambda: d.d.position() == target)
    assert d.d.error_code() == 0
    assert d.d.read(R.DI_PHYSICAL_STATE.address, 1) == [0b111]  # nothing pressed


def test_internal_position_ignores_the_home_offset():
    d = Drive(T5)
    d.enable()
    before = r32(d.d, R.POSITION_ACTUAL_INTERNAL.address)
    home(d, 35)
    assert d.d.position() == 0
    assert r32(d.d, R.POSITION_ACTUAL_INTERNAL.address) == before != 0


def test_bring_up_registers_start_at_factory_values():
    d = SimulatedDrive(T5)
    assert r32(d, R.ENCODER_BITS.address, signed=False) == 17
    assert r32(d, R.SOFTWARE_LIMIT_MIN.address) == -65535
    assert r32(d, R.SOFTWARE_LIMIT_MAX.address) == 65535
    assert d.read(R.POSITION_RESTORE_MODE.address, 1) == [0]
    assert d.read(R.UNDER_VOLTAGE_POINT.address, 1) == [24]


def test_hooks_refuse_writes_and_override_reads():
    bus = SimulatedBus([T5])
    drive = bus.drive(T5.slave_id)
    drive.rejected_writes.add(R.POLARITY.address)
    response = bus.handle_frame(frame(5, 0x06, 0x03, 0xF3, 0x00, 0x80))
    assert response[1:3] == bytes([0x86, 0x03])  # illegal data value
    drive.read_overrides[R.POLARITY.address] = 0x40
    response = bus.handle_frame(frame(5, 0x03, 0x03, 0xF3, 0x00, 0x01))
    assert struct.unpack(">H", response[3:5])[0] == 0x40


# ------------------------------------------------------------ library on the simulator


@pytest.fixture
def service():
    url = f"sim://test{next(_urls)}"
    install_bus(url, SimulatedBus(time_scale=20.0))
    service = ServoService(port=url)
    service.current_axis = AxisName.Y2
    service.connect()
    yield service
    service.disconnect()


def test_service_homes_and_moves(service):
    service.enable()
    service.home()
    assert abs(service.get_position()) < 0.2
    service.move_to(30.0, velocity_mm_s=50.0)
    assert service.get_position() == pytest.approx(30.0, abs=1e-3)
    service.move_by(-5.0, velocity_mm_s=50.0)
    assert service.get_position() == pytest.approx(25.0, abs=1e-3)


def test_velocity_is_positive_while_the_position_increases(service):
    """Y2 uses polarity 0x80; 606Ch reads in motor direction, the library flips it"""
    assert DEFAULT_AXIS_CONFIGS[AxisName.Y2].driver_polarity == 0x80
    service.enable()
    service.home()
    service.move_to(40.0, velocity_mm_s=20.0, wait=False)
    speeds = []
    for _ in range(20):
        speeds.append(service.get_axis_status().velocity_mm_s)
    service.stop()
    assert max(speeds) > 1.0
    assert min(speeds) >= 0.0


def test_move_onto_a_limit_switch_fails_at_once():
    """The drive's far switch sits inside the configured stroke: move_to must not wait 30 s"""
    import dataclasses
    import time

    from fluidics.control.nimotion.motor_control import LimitSwitchError

    url = f"sim://test{next(_urls)}"
    install_bus(url, SimulatedBus([dataclasses.replace(Y2, stroke_max=50.0)], time_scale=20.0))
    service = ServoService(
        port=url, axis_configs={AxisName.Y2: dataclasses.replace(Y2, stroke_max=60.0)}
    )
    service.current_axis = AxisName.Y2
    service.connect()
    try:
        service.enable()
        service.home()
        start = time.monotonic()
        with pytest.raises(LimitSwitchError):
            service.move_to(58.0, velocity_mm_s=10.0)  # switch at 51 mm
        assert time.monotonic() - start < 5
        assert 51.0 < service.get_position() < 53.0
    finally:
        service.disconnect()


def test_connect_writes_the_limits_before_the_profile(service):
    """Written after the profile (or not at all), the factory limits would cap it"""
    config = service.get_axis_config()
    client = service.get_modbus_client()
    for register, mm in (
        (R.PROFILE_ACCELERATION, config.default_acceleration),
        (R.PROFILE_DECELERATION, config.default_deceleration),
        (R.QUICK_STOP_DECELERATION, config.quick_stop_deceleration),
    ):
        assert client.read_register_32bit(config.slave_id, register.address) == (
            config.velocity_mm_to_pulses(mm)
        )


def test_connect_fails_when_the_drive_does_not_keep_the_profile():
    """A profile above the limits is capped by the drive; connect must say so"""
    import dataclasses

    from fluidics.control.nimotion.motor_control import MotorError

    url = f"sim://test{next(_urls)}"
    install_bus(url, SimulatedBus(time_scale=20.0))
    config = dataclasses.replace(Y2, max_acceleration=300.0)  # below the 500 mm/s² profile
    service = ServoService(port=url, axis_configs={AxisName.Y2: config})
    service.current_axis = AxisName.Y2
    with pytest.raises(MotorError, match="6083h"):
        service.connect()
    assert not service.is_connected


def test_jog_never_moves_against_its_direction():
    """Homing leaves X1 short of stroke_min: jog - must not drive it forward to stroke_min"""
    import time

    url = f"sim://test{next(_urls)}"
    install_bus(url, SimulatedBus(time_scale=20.0))
    service = ServoService(port=url)
    service.current_axis = AxisName.X1
    service.connect()
    try:
        service.enable()
        service.home()
        time.sleep(0.1)  # 2 s simulated: the axis decelerates past the home switch edge
        start = service.get_position()
        assert start < service.get_axis_config().stroke_min
        service.jog(-10.0)
        time.sleep(0.2)  # 4 s of simulated time
        assert service.get_position() == pytest.approx(start, abs=1e-3)
        service.jog(10.0)  # toward the far end it still moves
        time.sleep(0.2)
        service.stop()
        assert service.get_position() > start + 1.0
    finally:
        service.disconnect()


def test_home_returns_once_the_axis_has_stopped():
    """Bit 12 comes on at the switch edge, while the axis is still decelerating"""
    import time

    url = f"sim://test{next(_urls)}"
    install_bus(url, SimulatedBus(time_scale=1.0))  # real time: the deceleration takes 50 ms
    service = ServoService(port=url)
    service.current_axis = AxisName.Y2
    service.connect()
    try:
        service.enable()
        service.move_to(2.0, velocity_mm_s=50.0)
        service.home()
        at_return = service.get_position()
        time.sleep(0.3)
        assert service.get_position() == pytest.approx(at_return, abs=1e-4)
    finally:
        service.disconnect()


def test_set_home_position_does_not_wait_for_the_timeout(service):
    """Method 35 completes on the trigger; the wait used to need a 1 -> 0 edge"""
    service.enable()
    service.set_home_position()
    assert service.get_position() == 0


def test_save_parameters_needs_a_disabled_drive(service):
    service.disable()
    service.save_parameters()
    service.enable()
    with pytest.raises(Exception):
        service.save_parameters()


def test_custom_axis_config_gets_a_simulated_drive():
    url = f"sim://test{next(_urls)}"
    custom = {AxisName.Y2: DEFAULT_AXIS_CONFIGS[AxisName.Y2]}
    custom[AxisName.Y2] = type(Y2)(**{**Y2.__dict__, "slave_id": 7})
    install_bus(url, SimulatedBus(axis_configs=[], time_scale=20.0))
    service = ServoService(port=url, axis_configs=custom)
    service.current_axis = AxisName.Y2
    service.connect()
    try:
        assert service.get_axis_status().connected
    finally:
        service.disconnect()


def test_connect_without_initialize_writes_nothing(monkeypatch):
    url = f"sim://test{next(_urls)}"
    bus = install_bus(url, SimulatedBus([T5], time_scale=20.0))
    functions = []
    real = bus.handle_frame

    def record(frame_bytes):
        functions.append(frame_bytes[1])
        return real(frame_bytes)

    monkeypatch.setattr(bus, "handle_frame", record)
    service = ServoService(port=url, axis_configs={AxisName.T5: T5})
    service.current_axis = AxisName.T5
    service.connect(initialize=False)
    try:
        assert functions and set(functions) <= {0x03, 0x04}
    finally:
        service.disconnect()


def test_t5_zeroes_and_tilts():
    url = f"sim://test{next(_urls)}"
    install_bus(url, SimulatedBus([T5], time_scale=20.0))
    service = ServoService(port=url, axis_configs={AxisName.T5: T5})
    service.current_axis = AxisName.T5
    service.connect()
    try:
        assert service.get_motor().read_software_limits() == (-2548, 11650)
        service.enable()
        service.set_home_position()
        assert service.get_position() == 0
        service.move_to(30.0, velocity_mm_s=90.0)
        assert service.get_position() == pytest.approx(30.0, abs=0.01)
        service.move_to(-5.0, velocity_mm_s=90.0)
        assert service.get_position() == pytest.approx(-5.0, abs=0.01)
        with pytest.raises(ValueError):
            service.move_to(31.0)
        with pytest.raises(ValueError):
            service.home()
    finally:
        service.disconnect()
