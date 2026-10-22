"""The reservoir gate: a run that would draw air is refused at time zero."""
import logging

import pytest

from fluidics.errors import (OperationError, ReservoirDrawTooLarge, ReservoirNoWell,
                            ReservoirShortfall, ReservoirUndeclared)

from ..conftest import HYB, PRIME, PUSH, SKIPS_THE_SIPPER
from .conftest import FLOW_CELL_STEP

WASH = {"type": "flow_reagent", "fluidic_port": 1, "well": "ssct/A1", "flow_rate": 5000,
        "volume": 1000, "dip_cycles": 3}


def test_a_round_that_fits_runs_and_the_report_lists_its_wells(plate_sipper_system, real_clock):
    plan = plate_sipper_system.plan([HYB, PUSH, WASH])
    report = plate_sipper_system.check_reservoirs(plan)
    assert ("ssct/A1", 1490, 1750 - 1490) in report
    plate_sipper_system.run([HYB, PUSH, WASH], plan=plan)
    assert plate_sipper_system.wait(5)
    assert plate_sipper_system.devices.reservoirs.remaining("ssct/A1") == 1750 - 1490


def test_a_short_well_refuses_at_time_zero_naming_the_step(plate_sipper_system):
    with pytest.raises(ReservoirShortfall, match=r"wash2.*ssct/A1"):
        plate_sipper_system.run([HYB, PUSH, WASH, dict(WASH, name="wash2")])
    assert plate_sipper_system.devices.reservoirs.remaining("ssct/A1") == 1750, "nothing was charged"


def test_a_short_bottle_refuses_at_time_zero_too(plate_sipper_system):
    bulk = {"type": "flow_reagent", "fluidic_port": 25, "flow_rate": 5000, "volume": 4000}
    with pytest.raises(ReservoirShortfall, match="port_25"):
        plate_sipper_system.run([dict(bulk, repeat=200)])


def test_a_resumed_tail_is_refused_when_the_well_is_short(plate_sipper_system, real_clock):
    """Review focus 2: the interrupted wash already charged its well at
    dispatch; running it again whole must be refused, not draw air."""
    plan = plate_sipper_system.plan([HYB, PUSH, WASH])
    plate_sipper_system.run([HYB, PUSH, WASH], plan=plan)
    assert plate_sipper_system.wait(5)
    tail = plan[2:]
    with pytest.raises(ReservoirShortfall, match="ssct/A1"):
        plate_sipper_system.run(None, plan=tail)


def test_an_undeclared_ledger_refuses_the_run(plate_sipper_config, make_system, tmp_path, real_clock):
    """Review focus 3, end to end: a mismatched file at bring-up."""
    (tmp_path / "ledger.json").write_text(
        '{"format": 2, "definitions": {"port_25": {"loaded_ul": 1, "residual_ul": 0}}, "reservoirs": {}}')
    system = make_system(plate_sipper_config, ledger_path=tmp_path / "ledger.json")
    # every entry undeclared: the plates named once each, not 192 wells
    with pytest.raises(ReservoirUndeclared,
                       match="undeclared for port_25, every well of probes, every well of ssct: "):
        system.run([HYB])
    system.refill("all")
    system.run([HYB])
    assert system.wait(5)


def test_a_rig_that_tracks_nothing_skips_the_gate(system, monkeypatch, caplog):
    """Its ledger is the tally alone, with nothing to refuse, so no twin is
    built, no step is replayed and nothing is said: a step the twin could
    not replay must not become a refusal here, where plan_run deliberately
    falls back."""
    import fluidics.time_estimate as time_estimate
    plan = system.plan([FLOW_CELL_STEP])         # priced by replay, before the twin is taken away

    def no_twin(*args, **kwargs):
        raise AssertionError("the gate built a twin on a rig that tracks nothing")

    monkeypatch.setattr(time_estimate, "build_devices", no_twin)
    with caplog.at_level(logging.INFO, logger="fluidics.system"):
        assert system.check_reservoirs(plan) == []
    assert not [record for record in caplog.records
                if record.getMessage().startswith("Reservoirs after this run")]


def test_a_run_start_logs_the_projected_balances(plate_sipper_system, caplog):
    """The general notice at the start of a run, once, at INFO: what the
    gate's replay says each touched reservoir has left when the run is
    done."""
    with caplog.at_level(logging.INFO, logger="fluidics.system"):
        plate_sipper_system.run([HYB, PUSH, WASH])
        assert plate_sipper_system.wait()
    assert [record.getMessage() for record in caplog.records
            if record.name == "fluidics.system"
            and record.getMessage().startswith("Reservoirs after this run")] == \
        ["Reservoirs after this run: probes/A1 5 uL left; ssct/A1 260 uL left."]


def test_the_plan_prices_the_robots_travel(plate_sipper_config):
    """From home, probes/A1 is 0.24 s of XY (12 mm at 50 mm/s, Y shorter
    and concurrent) and 2.5 s of Z; probes/H12 is 2.01 s of XY (y = 100.5
    mm) and the same Z: 1.77 s more, and nothing else differs."""
    from fluidics.time_estimate import plan_run
    [near] = plan_run(plate_sipper_config, [HYB])
    [far] = plan_run(plate_sipper_config, [dict(HYB, well="H12")])
    assert far.duration_seconds - near.duration_seconds == pytest.approx(2.01 - 0.24, abs=0.01)


def test_a_short_well_is_told_what_it_would_hold_at_that_step_of_a_full_wells_usable(plate_sipper_system):
    """The figures are the step's: the well's projected remainder when the
    step comes round, against what a full well gives -- not one twice."""
    with pytest.raises(ReservoirShortfall) as refused:
        plate_sipper_system.check_reservoirs(
            plate_sipper_system.plan([HYB, PUSH, WASH, dict(WASH, name="wash2")]))
    e = refused.value
    assert (e.step_label, e.key, e.needed_ul, e.remaining_ul, e.usable_ul) == (
        "wash2", "ssct/A1", 1000, 260, 1750)
    assert str(e) == ("wash2: ssct/A1 cannot supply 1000 uL (it would have 260 uL left of "
                      "1750 usable at this step); refill or reload before starting")


def test_a_short_bottle_is_told_its_own_usable_volume(plate_sipper_system):
    bulk = {"type": "flow_reagent", "fluidic_port": 25, "flow_rate": 5000, "volume": 4000,
            "name": "bulk"}
    with pytest.raises(ReservoirShortfall) as refused:
        plate_sipper_system.check_reservoirs(plate_sipper_system.plan([dict(bulk, repeat=200)]))
    e = refused.value
    assert (e.key, e.needed_ul, e.remaining_ul, e.usable_ul) == ("port_25", 4000, 0, 480000)
    assert "it would have 0 uL left of 480000 usable at this step" in str(e)


def test_a_draw_no_full_well_can_give_keeps_its_own_remedy(plate_sipper_system):
    """Refilling cannot help a draw larger than a full well: the step must
    be split, and the refusal says so, with the step named."""
    with pytest.raises(ReservoirDrawTooLarge) as refused:
        plate_sipper_system.run([dict(HYB, volume=200, name="big")])
    assert str(refused.value) == ("big: probes/A1: a draw of 200 uL exceeds a full well's "
                                  "105 uL; split the step")
    assert refused.value.step_label == "big"
    assert (refused.value.volume_ul, refused.value.usable_ul) == (200, 105)
    assert not plate_sipper_system.busy
    assert plate_sipper_system.devices.reservoirs.remaining("probes/A1") == 105, "nothing was charged"


def _twin_whose_step_raises(monkeypatch, make_error):
    """The gate's twin, its one step replaced by one that raises a fresh
    `make_error()`. The plan's own replay meets it too, and prices the run
    by its fallback, as it does any step it cannot replay."""
    import fluidics.time_estimate as time_estimate

    class Twin:
        def process_sequence(self, sequence):
            raise make_error()

    monkeypatch.setattr(time_estimate, "build_operations", lambda config, devices: Twin())


def test_a_reservoir_refusal_with_no_key_keeps_its_type_and_words(plate_sipper_system, monkeypatch):
    _twin_whose_step_raises(monkeypatch, lambda: ReservoirNoWell(
        "port 1 is the sipper's and the sipper is not in a well"))
    with pytest.raises(ReservoirNoWell) as refused:
        plate_sipper_system.run([dict(HYB, name="hyb")])
    assert str(refused.value) == "hyb: port 1 is the sipper's and the sipper is not in a well"
    assert refused.value.step_label == "hyb"
    assert not plate_sipper_system.busy


@pytest.mark.parametrize("make_error", [lambda: OperationError("no such thing"),
                                        lambda: RuntimeError("no such thing")],
                         ids=["OperationError", "RuntimeError"])
def test_a_step_the_check_cannot_replay_refuses_the_run_naming_the_step(plate_sipper_system, monkeypatch,
                                                                        make_error):
    """Refused, as a step's error and not a raw one: the GUI reads a raw
    RuntimeError at Start as a busy rig."""
    _twin_whose_step_raises(monkeypatch, make_error)
    with pytest.raises(OperationError) as refused:
        plate_sipper_system.run([dict(HYB, name="hyb")])
    assert str(refused.value) == "hyb: the reservoir check could not replay this step (no such thing)"
    assert type(refused.value.__cause__) is type(make_error())
    assert not plate_sipper_system.busy


def test_selective_priming_that_leaves_out_the_sipper_is_refused_at_time_zero(plate_sipper_system):
    """Codex finding 3: the raw-dict run path took a priming row whose
    use_ports skipped the sipper's port (D7). validate_sequences refuses it,
    before the plan is replayed, and nothing is charged."""
    prime = dict(PRIME, use_ports=[25])
    reservoirs = plate_sipper_system.devices.reservoirs
    balances = {key: reservoirs.remaining(key) for key in ("port_25", "ssct/H11")}
    with pytest.raises(ValueError) as refused:
        plate_sipper_system.run([prime, HYB])
    assert str(refused.value) == f"sequence 0 (priming): {SKIPS_THE_SIPPER}"
    assert not plate_sipper_system.busy
    assert {key: reservoirs.remaining(key) for key in balances} == balances


def test_a_run_is_refused_at_time_zero_while_do_not_lower_tip_is_on(plate_sipper_system):
    """The sipper would travel to its wells without dipping, so no well
    row could draw: refused before the replay and before anything moves,
    as a FluidicsError the editor's start slot shows; nothing is charged."""
    robot = plate_sipper_system.devices.robot
    plate_sipper_system.manual.set_lower_tip(False)
    log = robot._stage._service.log
    before = len(log)
    balance = plate_sipper_system.devices.reservoirs.remaining("probes/A1")
    with pytest.raises(OperationError, match="^Do not lower tip is on"):
        plate_sipper_system.run([HYB])
    assert not plate_sipper_system.busy and len(log) == before
    assert plate_sipper_system.devices.reservoirs.remaining("probes/A1") == balance
    plate_sipper_system.manual.set_lower_tip(True)
    plate_sipper_system.run([HYB])
    assert plate_sipper_system.wait()


def test_a_push_is_priced_by_replay_like_the_draw_it_stands_for(plate_sipper_config):
    """No per-type pricing: the twin runs the push, so it costs what the
    flow_reagent row it replaces cost, never the flat fallback."""
    from fluidics.time_estimate import FALLBACK_SEQUENCE_SECONDS, plan_run
    [push] = plan_run(plate_sipper_config, [PUSH])
    [same] = plan_run(plate_sipper_config, [{"type": "flow_reagent", "fluidic_port": 1, "well": "ssct/A1",
                                             "flow_rate": 2000, "volume": 490}])
    assert push.duration_seconds == same.duration_seconds != FALLBACK_SEQUENCE_SECONDS
