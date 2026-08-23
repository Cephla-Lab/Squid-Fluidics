# tests/unit/control/test_selector_valve.py
import pytest

from fluidics.control.config import load_config
from fluidics.control.controller import FluidControllerSimulation
from fluidics.control.selector_valve import SelectorValveSystem


def _make_valve_system(config_path):
    config = load_config(str(config_path))
    fc = FluidControllerSimulation(serial_number="test")
    return SelectorValveSystem(fc, config)


@pytest.fixture
def flow_cell_system(fixtures_dir):
    """SelectorValveSystem with 3 valves (flow cell config)."""
    return _make_valve_system(fixtures_dir / "flow_cell_config.yaml")


@pytest.fixture
def open_chamber_system(fixtures_dir):
    """SelectorValveSystem with 1 valve (open chamber config)."""
    return _make_valve_system(fixtures_dir / "open_chamber_config.yaml")


class TestSelectorValveSystemInit:
    def test_flow_cell_port_count(self, flow_cell_system):
        # 3 valves with 10 ports each: (10-1) + (10-1) + 10 = 28
        assert flow_cell_system.available_port_number == 28

    def test_the_count_is_the_shared_config_arithmetic(self, fixtures_dir):
        """available_port_count is the same formula without hardware attached
        -- the pre-run sequence check uses it, so the two must be one."""
        from fluidics.control.config import available_port_count
        config = load_config(str(fixtures_dir / "flow_cell_config.yaml"))
        assert available_port_count(config) == 28

    def test_open_chamber_port_count(self, open_chamber_system):
        # 1 valve with 10 ports: 10
        assert open_chamber_system.available_port_number == 10


class TestPortToReagent:
    def test_known_mapping(self, flow_cell_system):
        assert flow_cell_system.port_to_reagent(1) == "reagent x"
        assert flow_cell_system.port_to_reagent(25) == "buffer 1"

    def test_out_of_range_returns_none(self, flow_cell_system):
        assert flow_cell_system.port_to_reagent(999) is None

    def test_unmapped_port_returns_none(self, open_chamber_system):
        # port_2 has no name mapping in open chamber config
        assert open_chamber_system.port_to_reagent(2) is None


class TestTubingFluidAmounts:
    def test_flow_cell_tubing_to_valve(self, flow_cell_system):
        # Port 1 is on valve 0: common(800) + valve_0(0) = 800
        assert flow_cell_system.get_tubing_fluid_amount_to_valve(1) == 800
        # Port 10 is on valve 1: common(800) + valve_1(200) = 1000
        assert flow_cell_system.get_tubing_fluid_amount_to_valve(10) == 1000
        # Port 19 is on valve 2: common(800) + valve_2(340) = 1140
        assert flow_cell_system.get_tubing_fluid_amount_to_valve(19) == 1140

    def test_open_chamber_tubing_to_valve(self, open_chamber_system):
        # Single valve: common(300) + valve_0(0) = 300
        assert open_chamber_system.get_tubing_fluid_amount_to_valve(1) == 300

    def test_tubing_to_port(self, flow_cell_system):
        assert flow_cell_system.get_tubing_fluid_amount_to_port(1) == 700
        assert flow_cell_system.get_tubing_fluid_amount_to_port(10) == 600
        assert flow_cell_system.get_tubing_fluid_amount_to_port(19) == 450


class TestGetPortNames:
    def test_flow_cell_names_count(self, flow_cell_system):
        names = flow_cell_system.get_port_names()
        assert len(names) == 28

    def test_names_format(self, flow_cell_system):
        names = flow_cell_system.get_port_names()
        assert names[0] == "Port 1: reagent x"
        assert names[24] == "Port 25: buffer 1"

    def test_open_chamber_unmapped_port(self, open_chamber_system):
        names = open_chamber_system.get_port_names()
        # port_2 has no mapping -> just "Port 2: "
        assert names[1] == "Port 2: "


class TestOpenPort:
    def test_open_port_single_valve(self, open_chamber_system):
        open_chamber_system.open_port(5)
        assert open_chamber_system.get_current_port() == 5

    def test_open_port_multi_valve(self, flow_cell_system):
        flow_cell_system.open_port(1)
        assert flow_cell_system.get_current_port() == 1

        flow_cell_system.open_port(10)
        assert flow_cell_system.get_current_port() == 10

        flow_cell_system.open_port(19)
        assert flow_cell_system.get_current_port() == 19

    @pytest.mark.parametrize("port", [0, -1, 999])
    def test_open_port_out_of_range_raises_and_leaves_the_selection(
            self, open_chamber_system, port):
        """This used to be a silent no-op: the cascade kept the previous port
        selected and the next draw pulled the wrong reagent with nothing
        saying so. The pre-run check in sequences.py catches the typo first;
        this raise is the backstop for callers that bypass it."""
        open_chamber_system.open_port(5)
        with pytest.raises(ValueError, match=r"1\.\.10"):
            open_chamber_system.open_port(port)
        assert open_chamber_system.get_current_port() == 5
