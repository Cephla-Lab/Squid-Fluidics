# tests/integration/conftest.py
import pytest

from fluidics.control.config import load_config
from fluidics.control.controller import FluidControllerSimulation
from fluidics.control.disc_pump import DiscPump
from fluidics.control.selector_valve import SelectorValveSystem
from fluidics.control.syringe_pump import SyringePumpSimulation
from fluidics.control.temperature_controller import TCMControllerSimulation


@pytest.fixture
def flow_cell_config(fixtures_dir):
    return load_config(str(fixtures_dir / "flow_cell_config.yaml"))


@pytest.fixture
def open_chamber_config(fixtures_dir):
    return load_config(str(fixtures_dir / "open_chamber_config.yaml"))


def _make_sim_hardware(config):
    """Create simulation hardware instances from a config."""
    fc = FluidControllerSimulation(serial_number="test")
    sp = SyringePumpSimulation(
        sn=None,
        syringe_ul=config.syringe_pump.volume_ul,
        speed_code_limit=config.syringe_pump.speed_code_limit,
        waste_port=config.syringe_pump.waste_port,
    )
    sv = SelectorValveSystem(fc, config)
    return fc, sp, sv


@pytest.fixture
def flow_cell_hardware(flow_cell_config):
    """Return (config, syringe_pump, selector_valves) for flow cell."""
    _fc, sp, sv = _make_sim_hardware(flow_cell_config)
    return flow_cell_config, sp, sv


@pytest.fixture
def open_chamber_hardware(open_chamber_config):
    """Return (config, syringe_pump, selector_valves, disc_pump, temperature_controller) for open chamber."""
    fc, sp, sv = _make_sim_hardware(open_chamber_config)
    dp = DiscPump(fc)
    tc = TCMControllerSimulation(channels=2)
    return open_chamber_config, sp, sv, dp, tc


@pytest.fixture
def flow_cell_hardware_with_tc(flow_cell_config):
    """Return (config, sp, sv, tc) for flow cell with a 1-channel temperature controller."""
    _fc, sp, sv = _make_sim_hardware(flow_cell_config)
    tc = TCMControllerSimulation(channels=1)
    return flow_cell_config, sp, sv, tc
