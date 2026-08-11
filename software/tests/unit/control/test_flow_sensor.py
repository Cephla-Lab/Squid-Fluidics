# tests/unit/control/test_flow_sensor.py
import pytest

from fluidics.control.controller import FluidController
from fluidics.control.flow_sensor import FlowSensor, FlowSensorSimulation, INVALID_RAW
from fluidics.control._def import CMD_SET, COMMAND_STATUS


def _make_packet(flow_raw=1000, uid=1, status=COMMAND_STATUS.COMPLETED_WITHOUT_ERRORS):
    msg = [0] * 30
    msg[0] = (uid >> 8) & 0xFF
    msg[1] = uid & 0xFF
    msg[3] = status
    unsigned = flow_raw & 0xFFFF
    msg[23] = (unsigned >> 8) & 0xFF
    msg[24] = unsigned & 0xFF
    return msg


class FakeController:
    """Minimal stand-in that can publish packets to a FlowSensor."""

    def __init__(self, status=COMMAND_STATUS.COMPLETED_WITHOUT_ERRORS):
        self.packet_callback = None
        self.commands = []
        self._status = status

    def send_command_blocking(self, command, *args):
        self.commands.append((command, args))
        return self._status

    def publish(self, flow_raw):
        parsed = FluidController._parse_packet(self, _make_packet(flow_raw=flow_raw))
        if self.packet_callback is not None:
            self.packet_callback(parsed)


class TestFlowSensorReadings:
    def test_positive_reading(self):
        fc = FakeController()
        sensor = FlowSensor(fc, index=1, name="s")
        fc.publish(1000)
        assert sensor.latest_flow_ul_min == pytest.approx(100.0)

    def test_negative_reading(self):
        fc = FakeController()
        sensor = FlowSensor(fc, index=1, name="s")
        fc.publish(-1000)
        assert sensor.latest_flow_ul_min == pytest.approx(-100.0)

    def test_sentinel_maps_to_none(self):
        fc = FakeController()
        sensor = FlowSensor(fc, index=1, name="s")
        fc.publish(INVALID_RAW)
        assert sensor.latest_flow_ul_min is None

    def test_saturation_is_a_real_reading(self):
        fc = FakeController()
        sensor = FlowSensor(fc, index=1, name="s")
        fc.publish(32500)
        assert sensor.latest_flow_ul_min == pytest.approx(3250.0)

    def test_no_reading_before_first_packet(self):
        sensor = FlowSensor(FakeController(), index=1, name="s")
        assert sensor.latest_flow_ul_min is None

    def test_recovers_after_a_sentinel(self):
        fc = FakeController()
        sensor = FlowSensor(fc, index=1, name="s")
        fc.publish(INVALID_RAW)
        fc.publish(500)
        assert sensor.latest_flow_ul_min == pytest.approx(50.0)


class TestFlowSensorSubscribers:
    def test_subscriber_receives_each_reading(self):
        fc = FakeController()
        sensor = FlowSensor(fc, index=1, name="s")
        seen = []
        sensor.subscribe(lambda flow, ts: seen.append(flow))
        fc.publish(100)
        fc.publish(200)
        assert seen == [pytest.approx(10.0), pytest.approx(20.0)]

    def test_subscriber_sees_none_for_sentinel(self):
        fc = FakeController()
        sensor = FlowSensor(fc, index=1, name="s")
        seen = []
        sensor.subscribe(lambda flow, ts: seen.append(flow))
        fc.publish(INVALID_RAW)
        assert seen == [None]

    def test_failing_subscriber_does_not_break_others(self):
        fc = FakeController()
        sensor = FlowSensor(fc, index=1, name="s")
        seen = []
        sensor.subscribe(lambda flow, ts: 1 / 0)
        sensor.subscribe(lambda flow, ts: seen.append(flow))
        fc.publish(100)
        assert seen == [pytest.approx(10.0)]


class TestFlowSensorBegin:
    def test_sends_initialize_with_index_water_and_crc(self):
        fc = FakeController()
        sensor = FlowSensor(fc, index=2, name="s")
        sensor.begin()
        command, args = fc.commands[0]
        assert command == CMD_SET.INITIALIZE_FLOW_SENSOR
        assert args == (2, 0x08, True)

    def test_raises_when_mcu_reports_execution_error(self):
        fc = FakeController(status=COMMAND_STATUS.CMD_EXECUTION_ERROR)
        sensor = FlowSensor(fc, index=1, name="s")
        with pytest.raises(RuntimeError, match="index 1"):
            sensor.begin()

    def test_none_status_is_treated_as_success(self):
        # FluidControllerSimulation's send_command_blocking returns None
        # (no real MCU to report a status). begin() must not raise.
        fc = FakeController(status=None)
        sensor = FlowSensor(fc, index=1, name="s")
        sensor.begin()


class TestFlowSensorClose:
    def test_close_clears_controller_callback(self):
        fc = FakeController()
        sensor = FlowSensor(fc, index=1, name="s")
        sensor.close()
        assert fc.packet_callback is None

    def test_close_stops_updating_latest_reading(self):
        fc = FakeController()
        sensor = FlowSensor(fc, index=1, name="s")
        fc.publish(100)
        sensor.close()
        fc.publish(200)
        assert sensor.latest_flow_ul_min == pytest.approx(10.0)

    def test_close_stops_notifying_subscribers(self):
        fc = FakeController()
        sensor = FlowSensor(fc, index=1, name="s")
        seen = []
        sensor.subscribe(lambda flow, ts: seen.append(flow))
        sensor.close()
        fc.publish(100)
        assert seen == []

    def test_close_leaves_other_callback_intact(self):
        fc = FakeController()
        sensor = FlowSensor(fc, index=1, name="s")
        other_callback = lambda parsed: None
        fc.packet_callback = other_callback
        sensor.close()
        assert fc.packet_callback is other_callback


class TestFlowSensorPacketSlot:
    def test_slot_one_reads_bytes_25_26(self):
        fc = FakeController()
        sensor = FlowSensor(fc, index=2, name="s", packet_slot=1)
        fc.publish(1000)   # writes bytes 23-24 only; slot 1 stays zero
        assert sensor.latest_flow_ul_min == pytest.approx(0.0)


class TestFlowSensorPacketCallbackGuard:
    """Config caps at one sensor today, so this collision can't happen via
    normal wiring -- but if Phase 2 lifts that cap, a second FlowSensor
    claiming the same fc.packet_callback slot would silently steal it and
    leave the first sensor's subscribers permanently dead with no error.
    """

    def test_second_sensor_on_same_controller_raises(self):
        fc = FakeController()
        FlowSensor(fc, index=1, name="first")
        with pytest.raises(RuntimeError, match="second"):
            FlowSensor(fc, index=2, name="second")

    def test_first_sensor_keeps_its_callback_after_failed_second(self):
        fc = FakeController()
        first = FlowSensor(fc, index=1, name="first")
        with pytest.raises(RuntimeError):
            FlowSensor(fc, index=2, name="second")
        assert fc.packet_callback is first._packet_handler


class TestFlowSensorSimulation:
    def test_default_reading_is_available(self):
        sim = FlowSensorSimulation()
        assert sim.latest_flow_ul_min is not None

    def test_settable_reading(self):
        sim = FlowSensorSimulation()
        sim.simulated_flow_ul_min = 123.0
        assert sim.latest_flow_ul_min == pytest.approx(123.0)

    def test_can_simulate_a_dead_sensor(self):
        sim = FlowSensorSimulation()
        sim.simulated_flow_ul_min = None
        assert sim.latest_flow_ul_min is None

    def test_begin_is_a_noop(self):
        FlowSensorSimulation().begin()

    def test_thread_is_not_started_on_construction(self):
        sim = FlowSensorSimulation()
        assert not sim.reading_thread.is_alive()
