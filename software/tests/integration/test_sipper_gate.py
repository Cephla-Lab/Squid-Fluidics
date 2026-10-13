"""The reservoir gate: a run that would draw air is refused at time zero."""
import pytest

from fluidics.errors import ReservoirShortfall, ReservoirUndeclared
from fluidics.system import FluidicsSystem

HYB = {"type": "flow_reagent", "fluidic_port": 1, "well": "A1", "flow_rate": 2000, "volume": 100}
PUSH = {"type": "flow_reagent", "fluidic_port": 1, "well": "ssct/A1", "flow_rate": 2000, "volume": 490}
WASH = {"type": "flow_reagent", "fluidic_port": 1, "well": "ssct/A1", "flow_rate": 5000,
        "volume": 1000, "dip_cycles": 3}


@pytest.fixture
def system(plate_sipper_config, instant_devices, tmp_path):
    devices = instant_devices(plate_sipper_config)
    built = FluidicsSystem(plate_sipper_config, devices, report_dir=tmp_path)
    yield built
    assert built.close() == []


def test_a_round_that_fits_runs_and_the_report_lists_its_wells(system, real_clock):
    report = system.check_reservoirs(system.plan([HYB, PUSH, WASH]))
    assert ("ssct/A1", 1490, 1750 - 1490) in report
    system.run([HYB, PUSH, WASH])
    assert system.wait(5)
    assert system.devices.reservoirs.remaining("ssct/A1") == 1750 - 1490


def test_a_short_well_refuses_at_time_zero_naming_the_step(system):
    with pytest.raises(ReservoirShortfall, match=r"wash2.*ssct/A1"):
        system.run([HYB, PUSH, WASH, dict(WASH, name="wash2")])
    assert system.devices.reservoirs.remaining("ssct/A1") == 1750, "nothing was charged"


def test_a_short_bottle_refuses_at_time_zero_too(system):
    bulk = {"type": "flow_reagent", "fluidic_port": 25, "flow_rate": 5000, "volume": 4000}
    with pytest.raises(ReservoirShortfall, match="port_25"):
        system.run([dict(bulk, repeat=200)])


def test_a_resumed_tail_is_refused_when_the_well_is_short(system, real_clock):
    """Review focus 2: the interrupted wash already charged its well at
    dispatch; running it again whole must be refused, not draw air."""
    plan = system.plan([HYB, PUSH, WASH])
    system.run([HYB, PUSH, WASH], plan=plan)
    assert system.wait(5)
    tail = plan[2:]
    with pytest.raises(ReservoirShortfall, match="ssct/A1"):
        system.run(None, plan=tail)


def test_an_undeclared_ledger_refuses_the_run(plate_sipper_config, instant_devices, tmp_path, real_clock):
    """Review focus 3, end to end: a mismatched file at bring-up."""
    (tmp_path / "ledger.json").write_text(
        '{"format": 2, "definitions": {"port_25": {"loaded_ul": 1, "residual_ul": 0}}, "reservoirs": {}}')
    from fluidics.devices import build_devices
    devices = build_devices(plate_sipper_config, simulation=True, instant=True,
                            ledger_path=tmp_path / "ledger.json")
    for sensor in devices.flow_sensors:
        sensor.close()
    system = FluidicsSystem(plate_sipper_config, devices, report_dir=tmp_path)
    try:
        with pytest.raises(ReservoirUndeclared):
            system.run([HYB])
        system.refill("all")
        system.run([HYB])
        assert system.wait(5)
    finally:
        assert system.close() == []


def test_a_rig_without_a_ledger_skips_the_gate(flow_cell_config, instant_devices, tmp_path):
    system = FluidicsSystem(flow_cell_config, instant_devices(flow_cell_config), report_dir=tmp_path)
    try:
        assert system.check_reservoirs(system.plan([{"type": "flow_reagent", "fluidic_port": 2,
                                                     "flow_rate": 500, "volume": 500}])) == []
    finally:
        assert system.close() == []


def test_the_plan_prices_the_robots_travel(plate_sipper_config):
    """From home, probes/A1 is 0.24 s of XY (12 mm at 50 mm/s, Y shorter
    and concurrent) and 2.5 s of Z; probes/H12 is 2.01 s of XY (y = 100.5
    mm) and the same Z: 1.77 s more, and nothing else differs."""
    from fluidics.time_estimate import plan_run
    [near] = plan_run(plate_sipper_config, [HYB])
    [far] = plan_run(plate_sipper_config, [dict(HYB, well="H12")])
    assert far.duration_seconds - near.duration_seconds == pytest.approx(2.01 - 0.24, abs=0.01)
