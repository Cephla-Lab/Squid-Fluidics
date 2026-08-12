# tests/integration/test_flow_sensor_startup.py
from fluidics.control.controller import FluidControllerSimulation
from fluidics.control.flow_sensor import build_flow_sensors


class TestBuildFlowSensors:
    def test_returns_empty_when_not_configured(self, open_chamber_config):
        fc = FluidControllerSimulation(serial_number="test")
        assert build_flow_sensors(fc, open_chamber_config, simulation=True) == []

    def test_builds_one_sensor_from_flow_cell_config(self, flow_cell_config):
        fc = FluidControllerSimulation(serial_number="test")
        sensors = build_flow_sensors(fc, flow_cell_config, simulation=True)
        assert len(sensors) == 1
        assert sensors[0].name == "syringe_draw"
        assert sensors[0].index == 1

    def test_phase_1_sensor_reads_packet_slot_zero(self, flow_cell_config):
        fc = FluidControllerSimulation(serial_number="test")
        sensors = build_flow_sensors(fc, flow_cell_config, simulation=True)
        assert sensors[0].packet_slot == 0

    def test_simulated_sensors_begin_and_close_cleanly(self, flow_cell_config):
        fc = FluidControllerSimulation(serial_number="test")
        sensors = build_flow_sensors(fc, flow_cell_config, simulation=True)
        for s in sensors:
            s.begin()

        seen = []
        for s in sensors:
            s.subscribe(lambda flow, ts: seen.append(flow))

        for s in sensors:
            s.close()

        # FlowSensorSimulation.close() clears its own subscriber list and sets
        # terminate_reading_thread; it never touches fc.packet_callback (only
        # the real FlowSensor's __init__ installs that wiring, which these
        # simulation instances never go through), so there is nothing to
        # assert on fc here. _subscribers == [] is the actual mechanism by
        # which a closed sensor "stops being called": _reading_loop's only
        # notification step iterates that exact list, so clearing it is
        # equivalent to no registered callback ever firing again. We can't
        # start reading_thread to observe this end-to-end (constructors/tests
        # must never start a background thread in this suite), so this
        # asserts the real, documented effects of close() instead of a no-op.
        for s in sensors:
            assert s._subscribers == []
            assert s.terminate_reading_thread is True
        assert seen == []


class TestTwoSensorFactory:
    """Slot is derived from index, not from position in the config list."""

    @staticmethod
    def _config(flow_cell_config, entries):
        flow_cell_config.flow_sensors = [
            type(flow_cell_config.flow_sensors[0])(**e) for e in entries
        ]
        return flow_cell_config

    def test_slot_is_index_minus_one(self, flow_cell_config):
        cfg = self._config(flow_cell_config, [
            {"index": 1, "name": "a"}, {"index": 2, "name": "b"}])
        fc = FluidControllerSimulation(serial_number="test")
        sensors = build_flow_sensors(fc, cfg, simulation=True)
        assert [(s.index, s.packet_slot) for s in sensors] == [(1, 0), (2, 1)]

    def test_config_order_does_not_change_slots(self, flow_cell_config):
        """Reordering the YAML must not repoint a sensor at another slot."""
        cfg = self._config(flow_cell_config, [
            {"index": 2, "name": "b"}, {"index": 1, "name": "a"}])
        fc = FluidControllerSimulation(serial_number="test")
        sensors = build_flow_sensors(fc, cfg, simulation=True)
        assert {s.name: s.packet_slot for s in sensors} == {"a": 0, "b": 1}

    def test_lone_index_2_sensor_reads_slot_1(self, flow_cell_config):
        cfg = self._config(flow_cell_config, [{"index": 2, "name": "waste"}])
        fc = FluidControllerSimulation(serial_number="test")
        sensors = build_flow_sensors(fc, cfg, simulation=True)
        assert len(sensors) == 1
        assert sensors[0].packet_slot == 1
