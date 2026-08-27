# tests/integration/conftest.py
import pytest

from fluidics.control.config import load_config
from fluidics.control.controller import FluidControllerSimulation
from fluidics.control.disc_pump import DiscPump
from fluidics.control.selector_valve import SelectorValveSystem
from fluidics.control.syringe_pump import SyringePumpSimulation
from fluidics.control.temperature_controller import TCMControllerSimulation
from fluidics.devices import build_devices
from fluidics.merfish_operations import MERFISHOperations
from fluidics.open_chamber_operations import OpenChamberOperations


# A step that moves liquid on each application, against the fixture configs.
# Shared by the modules that need one without caring which it is.
FLOW_CELL_STEP = {"type": "flow_reagent", "fluidic_port": 1,
                  "flow_rate": 500, "volume": 500}


@pytest.fixture
def instant_devices(built):
    """A simulated DeviceSet with its pacing switched off: the controller's
    one-second commands and the pump's five-second moves would otherwise be
    spent for real under real_clock. The gates are untouched."""
    def _build(config):
        devices = built(config, simulation=True)
        devices.controller.COMMAND_SECONDS = 0
        devices.syringe_pump.ESTIMATE_SECONDS = 0
        return devices

    return _build


@pytest.fixture
def thread_cannot_start(monkeypatch):
    """The session's Thread whose start() raises -- what a host out of
    threads looks like. Patched after the simulated drivers have started
    their own threads."""
    class CannotStart:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    import fluidics.run_session as run_session
    monkeypatch.setattr(run_session.threading, "Thread", CannotStart)


@pytest.fixture
def built():
    """build_devices that closes what it built when the test ends.

    Not housekeeping: start_flow_sensors starts the simulated sensor's publish
    thread, and a leaked DeviceSet keeps it running for the rest of the suite.
    Teardown also asserts the close reported no errors, so a test cannot pass
    while quietly leaving a device it built in a state that will not shut down.
    """
    device_sets = []

    def _build(config, **kwargs):
        devices = build_devices(config, **kwargs)
        device_sets.append(devices)
        return devices

    yield _build
    close_errors = [e for d in device_sets for e in d.close()]
    assert close_errors == []


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
    # Every device that can start motion shares one signal, as build_devices
    # wires them: on separate ones a cancel would reach the pump but not the
    # valves, and the operations would keep moving liquid.
    sv = SelectorValveSystem(fc, config, sp.run_control)
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
    dp = DiscPump(fc, sp.run_control)
    tc = TCMControllerSimulation(channels=2)
    return open_chamber_config, sp, sv, dp, tc


@pytest.fixture
def flow_cell_rig(flow_cell_hardware):
    """(MERFISHOperations, syringe_pump) over the flow cell fixture config."""
    config, sp, sv = flow_cell_hardware
    return MERFISHOperations(config, sp, sv), sp


@pytest.fixture
def open_chamber_rig(open_chamber_hardware):
    """(OpenChamberOperations, syringe_pump) over the open chamber fixture config."""
    config, sp, sv, dp, tc = open_chamber_hardware
    return OpenChamberOperations(config, sp, sv, dp, tc), sp


@pytest.fixture
def flow_cell_hardware_with_tc(flow_cell_config):
    """Return (config, sp, sv, tc) for flow cell with a 1-channel temperature controller."""
    _fc, sp, sv = _make_sim_hardware(flow_cell_config)
    tc = TCMControllerSimulation(channels=1)
    return flow_cell_config, sp, sv, tc
