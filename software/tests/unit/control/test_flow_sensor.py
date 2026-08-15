# tests/unit/control/test_flow_sensor.py
from types import SimpleNamespace

import pytest

from fluidics.control.config import FlowSensorConfig
from fluidics.control.controller import FluidController, PacketSubscribers
from fluidics.control.flow_sensor import (
    FlowSensor, FlowSensorSimulation, INVALID_RAW, start_flow_sensors)
from fluidics.control._def import CMD_SET, COMMAND_STATUS

from .packet_helpers import make_status_packet as _make_packet


class FakeController(PacketSubscribers):
    """Minimal stand-in that can publish packets to any number of FlowSensors.

    Uses the real PacketSubscribers mixin rather than reimplementing fan-out,
    so these tests exercise the production dispatch path.
    """

    def __init__(self, status=COMMAND_STATUS.COMPLETED_WITHOUT_ERRORS):
        self._init_packet_subscribers()
        self.commands = []
        self._status = status

    def send_command_blocking(self, command, *args):
        self.commands.append((command, args))
        return self._status

    def publish(self, flow_raw, flow_2_raw=0):
        parsed = FluidController._parse_packet(
            self, _make_packet(flow_raw=flow_raw, flow_2_raw=flow_2_raw))
        self._notify_packet_subscribers(parsed)


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
    def test_close_unsubscribes_from_the_controller(self):
        fc = FakeController()
        sensor = FlowSensor(fc, index=1, name="s")
        assert len(fc._packet_subscribers) == 1
        sensor.close()
        assert len(fc._packet_subscribers) == 0

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

    def test_close_is_idempotent(self):
        """begin()'s failure path calls close(), and teardown calls it again."""
        fc = FakeController()
        sensor = FlowSensor(fc, index=1, name="s")
        sensor.close()
        sensor.close()
        assert len(fc._packet_subscribers) == 0

    def test_close_leaves_other_subscribers_intact(self):
        fc = FakeController()
        first = FlowSensor(fc, index=1, name="first")
        second = FlowSensor(fc, index=2, name="second")
        second.close()
        fc.publish(1000, flow_2_raw=2000)
        assert first.latest_flow_ul_min == pytest.approx(100.0)
        assert second.latest_flow_ul_min is None


class TestTwoSensorsOnOneController:
    """The configuration the single-callback slot used to make impossible."""

    def test_each_sensor_reads_its_own_slot(self):
        fc = FakeController()
        first = FlowSensor(fc, index=1, name="first")
        second = FlowSensor(fc, index=2, name="second")
        fc.publish(1000, flow_2_raw=-2000)
        assert first.latest_flow_ul_min == pytest.approx(100.0)
        assert second.latest_flow_ul_min == pytest.approx(-200.0)

    def test_slot_1_sentinel_is_independent_of_slot_0(self):
        fc = FakeController()
        first = FlowSensor(fc, index=1, name="first")
        second = FlowSensor(fc, index=2, name="second")
        fc.publish(1000, flow_2_raw=INVALID_RAW)
        assert first.latest_flow_ul_min == pytest.approx(100.0)
        assert second.latest_flow_ul_min is None

    def test_empty_slot_0_does_not_disturb_a_lone_slot_1_sensor(self):
        """One sensor in board slot 2: index 2, reading bytes 25-26, while
        bytes 23-24 carry the no-sensor sentinel. Must be a normal
        configuration, not an error.
        """
        fc = FakeController()
        lone = FlowSensor(fc, index=2, name="waste_line")
        fc.publish(INVALID_RAW, flow_2_raw=1500)
        assert lone.latest_flow_ul_min == pytest.approx(150.0)

    def test_a_raising_sensor_does_not_starve_the_other(self):
        fc = FakeController()
        first = FlowSensor(fc, index=1, name="first")
        second = FlowSensor(fc, index=2, name="second")
        first.subscribe(lambda flow, ts: 1 / 0)
        seen = []
        second.subscribe(lambda flow, ts: seen.append(flow))
        fc.publish(1000, flow_2_raw=2000)
        assert seen == [pytest.approx(200.0)]


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

    def test_begin_does_not_start_the_thread(self):
        """The suite's fake clock turns the publish loop into a busy spin, so
        a test that only calls begin() must not get a thread it did not ask
        for. start() is the explicit opt-in."""
        sim = FlowSensorSimulation()
        sim.begin()
        assert not sim.reading_thread.is_alive()


class TestStartFlowSensors:
    """A sensor claims a slot on the packet stream in its constructor, and only
    close() gives it back. A partial failure that drops the list without
    closing leaves live handlers with nothing holding a reference to them.
    """

    def _config(self, count):
        """The real config model, so a field added to FlowSensorConfig and to
        the build_flow_sensors call cannot quietly stop being covered here."""
        return SimpleNamespace(flow_sensors=[
            FlowSensorConfig(index=i + 1, name=f"s{i + 1}") for i in range(count)
        ])

    def test_all_sensors_are_initialized_and_subscribed(self):
        fc = FakeController()
        sensors = start_flow_sensors(fc, self._config(2))
        assert len(sensors) == 2
        assert len(fc._packet_subscribers) == 2

    def test_a_failing_sensor_takes_the_healthy_ones_down_with_it(self, monkeypatch):
        """Sensor 1 came up, sensor 2 did not. Sensor 1 must not be left
        publishing into a handler no one can reach."""
        fc = FakeController()
        real_begin = FlowSensor.begin

        def begin(self):
            if self.index == 2:
                raise RuntimeError("sensor 2 is not connected")
            return real_begin(self)

        monkeypatch.setattr(FlowSensor, "begin", begin)
        with pytest.raises(RuntimeError, match="not connected"):
            start_flow_sensors(fc, self._config(2))
        assert len(fc._packet_subscribers) == 0

    def test_cleanup_does_not_depend_on_begin_cleaning_up_after_itself(self, monkeypatch):
        """start() can raise too -- a thread started twice, say -- and by then
        begin() has returned and the sensor is fully live."""
        fc = FakeController()
        monkeypatch.setattr(FlowSensor, "start",
                            lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
        with pytest.raises(RuntimeError, match="boom"):
            start_flow_sensors(fc, self._config(2))
        assert len(fc._packet_subscribers) == 0

    def test_no_sensors_configured_returns_nothing(self):
        fc = FakeController()
        assert start_flow_sensors(fc, SimpleNamespace(flow_sensors=None)) == []
