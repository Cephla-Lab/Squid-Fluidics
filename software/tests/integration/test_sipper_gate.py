"""The reservoir gate: a run that would draw air is refused at time zero."""
import pytest

from fluidics.errors import (OperationError, ReservoirDrawTooLarge, ReservoirNoWell,
                            ReservoirShortfall, ReservoirUndeclared)
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
        # every entry undeclared: the plates named once each, not 192 wells
        with pytest.raises(ReservoirUndeclared,
                           match="undeclared for port_25, every well of probes, every well of ssct: "):
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


def test_a_short_well_is_told_what_it_would_hold_at_that_step_of_a_full_wells_usable(system):
    """The figures are the step's: the well's projected remainder when the
    step comes round, against what a full well gives -- not one twice."""
    with pytest.raises(ReservoirShortfall) as refused:
        system.check_reservoirs(system.plan([HYB, PUSH, WASH, dict(WASH, name="wash2")]))
    e = refused.value
    assert (e.step_label, e.key, e.needed_ul, e.remaining_ul, e.usable_ul) == (
        "wash2", "ssct/A1", 1000, 260, 1750)
    assert str(e) == ("wash2: ssct/A1 cannot supply 1000 uL (it would have 260 uL left of "
                      "1750 usable at this step); refill or reload before starting")


def test_a_short_bottle_is_told_its_own_usable_volume(system):
    bulk = {"type": "flow_reagent", "fluidic_port": 25, "flow_rate": 5000, "volume": 4000,
            "name": "bulk"}
    with pytest.raises(ReservoirShortfall) as refused:
        system.check_reservoirs(system.plan([dict(bulk, repeat=200)]))
    e = refused.value
    assert (e.key, e.needed_ul, e.remaining_ul, e.usable_ul) == ("port_25", 4000, 0, 480000)
    assert "it would have 0 uL left of 480000 usable at this step" in str(e)


def test_a_draw_no_full_well_can_give_keeps_its_own_remedy(system):
    """Refilling cannot help a draw larger than a full well: the step must
    be split, and the refusal says so, with the step named."""
    with pytest.raises(ReservoirDrawTooLarge) as refused:
        system.run([dict(HYB, volume=200, name="big")])
    assert str(refused.value) == ("big: probes/A1: a draw of 200 uL exceeds a full well's "
                                  "105 uL; split the step")
    assert refused.value.step_label == "big"
    assert (refused.value.volume_ul, refused.value.usable_ul) == (200, 105)
    assert not system.busy
    assert system.devices.reservoirs.remaining("probes/A1") == 105, "nothing was charged"


def _twin_whose_step_raises(monkeypatch, make_error):
    """The gate's twin, its one step replaced by one that raises a fresh
    `make_error()`. The plan's own replay meets it too, and prices the run
    by its fallback, as it does any step it cannot replay."""
    import fluidics.time_estimate as time_estimate

    class Twin:
        def process_sequence(self, sequence):
            raise make_error()

    monkeypatch.setattr(time_estimate, "build_operations", lambda config, devices: Twin())


def test_a_reservoir_refusal_with_no_key_keeps_its_type_and_words(system, monkeypatch):
    _twin_whose_step_raises(monkeypatch, lambda: ReservoirNoWell(
        "port 1 is the sipper's and the sipper is not in a well"))
    with pytest.raises(ReservoirNoWell) as refused:
        system.run([dict(HYB, name="hyb")])
    assert str(refused.value) == "hyb: port 1 is the sipper's and the sipper is not in a well"
    assert refused.value.step_label == "hyb"
    assert not system.busy


@pytest.mark.parametrize("make_error", [lambda: OperationError("no such thing"),
                                        lambda: RuntimeError("no such thing")],
                         ids=["OperationError", "RuntimeError"])
def test_a_step_the_check_cannot_replay_refuses_the_run_naming_the_step(system, monkeypatch,
                                                                        make_error):
    """Refused, as a step's error and not a raw one: the GUI reads a raw
    RuntimeError at Start as a busy rig."""
    _twin_whose_step_raises(monkeypatch, make_error)
    with pytest.raises(OperationError) as refused:
        system.run([dict(HYB, name="hyb")])
    assert str(refused.value) == "hyb: the reservoir check could not replay this step (no such thing)"
    assert type(refused.value.__cause__) is type(make_error())
    assert not system.busy


def test_selective_priming_that_leaves_out_the_sipper_is_refused_at_time_zero(system):
    """Codex finding 3: the raw-dict run path took a priming row whose
    use_ports skipped the sipper's port (D7). validate_sequences refuses it,
    before the plan is replayed, and nothing is charged."""
    prime = {"type": "priming", "fluidic_port": 25, "flow_rate": 5000, "volume": 800,
             "use_ports": [25]}
    reservoirs = system.devices.reservoirs
    balances = {key: reservoirs.remaining(key) for key in ("port_25", "ssct/H11")}
    with pytest.raises(ValueError, match=r"^sequence 0 \(priming\): use_ports leaves out port 1 "
                                         r"\(the sipper\): priming and clean-up never skip the sipper$"):
        system.run([prime, HYB])
    assert not system.busy
    assert {key: reservoirs.remaining(key) for key in balances} == balances
