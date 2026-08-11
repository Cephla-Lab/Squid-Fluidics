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
        for s in sensors:
            s.close()
