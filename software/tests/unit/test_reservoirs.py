"""ReservoirLedger: counts every draw, authorizes the pump's, persists first, never guesses."""
import json
import logging
import re
from contextlib import closing
from types import SimpleNamespace

import pytest

from fluidics.errors import (ReservoirDrawTooLarge, ReservoirError, ReservoirInUse, ReservoirNoWell,
                             ReservoirOverdraw, ReservoirUndeclared, ReservoirWriteError)
from fluidics.events import RunEnded, RunStarted
from fluidics.reservoirs import ReservoirLedger, describe_keys

DEFS = {"port_25": {"loaded_ul": 5000, "residual_ul": 200},
        "ssct/A1": {"loaded_ul": 1800, "residual_ul": 50},
        "ssct/A2": {"loaded_ul": 1800, "residual_ul": 50}}
SIPPER_PORT, EXTRACT_PORT = 1, 2


class FakePump:
    def __init__(self):
        self.draw_authorizers = []
        self.started = []

    def extract(self, port, volume_ul):
        """The pump's order: every authorizer, then the dispatch."""
        for authorize in self.draw_authorizers:
            authorize(port, volume_ul)
        self.started.append((port, volume_ul))


@pytest.fixture
def rig(tmp_path):
    pump = FakePump()
    state = {"port": 25, "well": None}
    ledger = ReservoirLedger(DEFS, path=tmp_path / "ledger.json")
    ledger.attach(pump, SIPPER_PORT, EXTRACT_PORT, current_port=lambda: state["port"],
                  current_well=lambda: state["well"])
    return ledger, pump, state


@pytest.fixture
def tally():
    """A rig that tracks nothing: the ledger is the tally alone, in memory
    (no file, no lock), with the valves' names for its ports; the run's
    boundary events reach it as the system delivers them."""
    pump = FakePump()
    state = {"port": 3}
    names = {3: "DAPI"}
    ledger = ReservoirLedger({})
    ledger.attach(pump, SIPPER_PORT, EXTRACT_PORT, current_port=lambda: state["port"],
                  current_well=lambda: None, port_to_reagent=names.get)

    def started():
        ledger.on_run_event(RunStarted("run-1", ()))

    def ended():
        ledger.on_run_event(RunEnded("run-1", "finished", None, 0.0, None))

    return SimpleNamespace(ledger=ledger, pump=pump, state=state, names=names,
                           run_started=started, run_ended=ended)


class TestCharging:
    def test_a_bulk_draw_charges_the_open_ports_bottle(self, rig):
        ledger, pump, _ = rig
        pump.extract(EXTRACT_PORT, 500)
        assert ledger.remaining("port_25") == 5000 - 200 - 500
        assert ledger.used_rows() == [(25, None, 500.0)], "one charge keeps both figures"
        assert pump.started == [(EXTRACT_PORT, 500)]

    def test_a_sipper_draw_charges_the_current_well(self, rig):
        ledger, pump, state = rig
        state["port"], state["well"] = SIPPER_PORT, "ssct/A1"
        pump.extract(EXTRACT_PORT, 1000)
        assert ledger.remaining("ssct/A1") == 1800 - 50 - 1000
        assert ledger.remaining("port_25") == 4800

    def test_a_bulk_draw_after_a_well_row_charges_the_bottle_not_the_well_the_tip_sits_in(self, rig):
        """The sipper stays in its well while a bulk port is routed: the
        routed port decides, the well only when the sipper's port is routed."""
        ledger, pump, state = rig
        state["port"], state["well"] = SIPPER_PORT, "ssct/A1"
        pump.extract(EXTRACT_PORT, 100)
        state["port"] = 25                        # the valve moved on; the tip did not
        pump.extract(EXTRACT_PORT, 500)
        assert ledger.remaining("ssct/A1") == 1650
        assert ledger.remaining("port_25") == 4300

    def test_the_sippers_port_with_the_tip_in_no_well_is_refused_at_the_pump(self, rig):
        ledger, pump, state = rig
        state["port"], state["well"] = SIPPER_PORT, None
        with pytest.raises(ReservoirNoWell):
            pump.extract(EXTRACT_PORT, 100)
        assert pump.started == []

    def test_an_untracked_bottles_draw_is_counted_never_refused_never_written(self, rig, tmp_path):
        """No declared volume, so nothing to overdraw: the draw goes to the
        per-run figure and nowhere else -- not to rows(), not to the file."""
        ledger, pump, state = rig
        state["port"] = 26
        pump.extract(EXTRACT_PORT, 500)
        pump.extract(EXTRACT_PORT, 10 ** 9)
        assert ledger.used_rows() == [(26, None, 500.0 + 10 ** 9)]
        assert ledger.rows() == []
        assert not (tmp_path / "ledger.json").exists(), "an untracked draw is not the file's"
        assert len(pump.started) == 2

    def test_a_draw_through_another_syringe_port_is_ignored_everywhere(self, rig, tmp_path):
        """A chamber draw, air: not reagent, so neither figure moves."""
        ledger, pump, _ = rig
        pump.extract(1, 500)
        assert ledger.used_rows() == [] and ledger.rows() == []
        assert not (tmp_path / "ledger.json").exists()
        assert pump.started == [(1, 500)]

    def test_a_well_draw_is_counted_under_the_sippers_port(self, rig):
        """The file knows wells; the per-run figure knows ports, the thing
        the valves name, so every well's draw is the sipper port's."""
        ledger, pump, state = rig
        state["port"], state["well"] = SIPPER_PORT, "ssct/A1"
        pump.extract(EXTRACT_PORT, 1000)
        state["well"] = "ssct/A2"
        pump.extract(EXTRACT_PORT, 200)
        assert ledger.used_rows() == [(SIPPER_PORT, None, 1200.0)]
        assert ledger.rows() == [("ssct/A1", 1000.0, 750.0), ("ssct/A2", 200.0, 1550.0)]

    @pytest.mark.parametrize("volume", [float("nan"), -100], ids=["nan", "negative"])
    def test_a_draw_that_is_not_a_volume_is_refused_on_an_untracked_port_too(self, rig, volume):
        """The volume check comes before the untracked return: counted, a
        NaN would poison that port's per-run figure as it would a balance."""
        ledger, pump, state = rig
        state["port"] = 26
        refused = re.escape(f"port_26: {volume!r} uL is not a volume")
        with pytest.raises(ReservoirError, match=refused):
            pump.extract(EXTRACT_PORT, volume)
        assert ledger.used_rows() == []
        assert pump.started == []

    def test_an_overdraw_is_refused_before_the_pump_starts_and_charges_nothing(self, rig):
        ledger, pump, state = rig
        state["port"], state["well"] = SIPPER_PORT, "ssct/A1"
        pump.extract(EXTRACT_PORT, 1000)
        with pytest.raises(ReservoirOverdraw, match="ssct/A1.*1000.*750"):
            pump.extract(EXTRACT_PORT, 1000)
        assert ledger.remaining("ssct/A1") == 750
        assert pump.started == [(EXTRACT_PORT, 1000)]

    def test_a_bottle_overdraw_is_refused_too(self, rig):
        ledger, pump, _ = rig
        pump.extract(EXTRACT_PORT, 4800)
        with pytest.raises(ReservoirOverdraw, match="port_25"):
            pump.extract(EXTRACT_PORT, 1)
        assert ledger.remaining("port_25") == 0

    def test_check_refuses_an_overdraw_and_a_draw_too_large(self, rig):
        ledger, pump, state = rig
        state["port"], state["well"] = SIPPER_PORT, "ssct/A1"
        pump.extract(EXTRACT_PORT, 1000)
        with pytest.raises(ReservoirOverdraw, match="ssct/A1.*1000.*750"):
            ledger.check("ssct/A1", 1000)
        with pytest.raises(ReservoirDrawTooLarge, match="split"):
            ledger.check("ssct/A2", 2000)
        ledger.check("ssct/A2", 1750)     # exactly usable: fine
        ledger.check("port_99", 10 ** 9)  # untracked: not the ledger's business

    def test_the_sippers_port_over_a_well_the_ledger_does_not_define_is_refused(self, tmp_path):
        """Every well of every plate is defined, so a well the ledger does not
        know is not an untracked bottle: letting it through would be a guess."""
        pump = FakePump()
        ledger = ReservoirLedger({"probes/A1": {"loaded_ul": 1800, "residual_ul": 50}},
                                 path=tmp_path / "ledger.json")
        ledger.attach(pump, SIPPER_PORT, EXTRACT_PORT, lambda: SIPPER_PORT, lambda: "probes/B2")
        with pytest.raises(ReservoirUndeclared, match="probes/B2"):
            pump.extract(EXTRACT_PORT, 100)
        assert pump.started == []

    @pytest.mark.parametrize("volume", [float("nan"), -100, float("inf")],
                             ids=["nan", "negative", "infinite"])
    def test_a_draw_that_is_not_a_finite_volume_is_refused_and_charges_nothing(
            self, rig, tmp_path, volume):
        """NaN fails every comparison, so it passes every check; charged, it
        makes the balance NaN and lets every later draw through."""
        ledger, pump, _ = rig
        refused = re.escape(f"port_25: {volume!r} uL is not a volume")
        with pytest.raises(ReservoirError, match=refused):
            ledger.authorize(EXTRACT_PORT, volume)
        assert ledger.remaining("port_25") == 4800
        assert ledger.used_rows() == []
        assert not (tmp_path / "ledger.json").exists(), "nothing was charged, nothing written"
        with pytest.raises(ReservoirError, match=refused):
            ledger.check("port_25", volume)
        pump.extract(EXTRACT_PORT, 500)               # and the ledger still charges a real draw
        assert ledger.remaining("port_25") == 4300

    def test_a_draw_of_nothing_passes_and_charges_nothing(self, rig, tmp_path):
        """No liquid moves, so there is no air to refuse; the operations can
        compute a zero volume, and it must not stop the run."""
        ledger, pump, _ = rig
        pump.extract(EXTRACT_PORT, 0)
        ledger.check("port_25", 0)
        assert pump.started == [(EXTRACT_PORT, 0)]
        assert ledger.remaining("port_25") == 4800
        assert ledger.rows() == [] and ledger.used_rows() == []
        assert not (tmp_path / "ledger.json").exists(), "nothing was charged, nothing written"


class TestPersistence:
    def test_every_charge_is_written_before_the_pump_starts(self, rig, tmp_path):
        ledger, pump, _ = rig
        seen = []
        pump.draw_authorizers.append(
            lambda port, ul: seen.append(json.loads((tmp_path / "ledger.json").read_text())))
        pump.extract(EXTRACT_PORT, 500)
        assert seen[0]["reservoirs"]["port_25"]["dispatched_ul"] == 500

    def test_a_write_that_fails_refuses_the_draw_and_charges_nothing(self, tmp_path):
        pump = FakePump()
        path = tmp_path / "ledger.json"
        path.mkdir()                               # a directory where the file should be
        ledger = ReservoirLedger(DEFS, path=path)
        ledger.attach(pump, SIPPER_PORT, EXTRACT_PORT, lambda: 25, lambda: None)
        with pytest.raises(ReservoirWriteError, match="not started"):
            pump.extract(EXTRACT_PORT, 500)
        assert ledger.remaining("port_25") == 4800
        assert ledger.used_rows() == [], "a draw that never started is in no figure"
        assert pump.started == []

    def test_a_matching_file_restores(self, rig, tmp_path):
        ledger, pump, _ = rig
        pump.extract(EXTRACT_PORT, 500)
        with closing(ReservoirLedger.load(tmp_path / "ledger.json", DEFS)) as again:
            assert again.declared and again.remaining("port_25") == 4300

    def test_a_changed_entry_is_undeclared_and_the_others_keep_their_balances(self, rig, tmp_path):
        """Review focus 3: an edited loaded volume must not resurrect a
        reservoir; the entries that did not change are not punished."""
        ledger, pump, state = rig
        pump.extract(EXTRACT_PORT, 500)
        state["port"], state["well"] = SIPPER_PORT, "ssct/A1"
        pump.extract(EXTRACT_PORT, 300)
        changed = dict(DEFS, port_25={"loaded_ul": 6000, "residual_ul": 200},
                       **{"ssct/A3": {"loaded_ul": 1800, "residual_ul": 50}})
        with closing(ReservoirLedger.load(tmp_path / "ledger.json", changed)) as again:
            assert again.undeclared == ["port_25", "ssct/A3"]
            assert again.remaining("ssct/A1") == 1450, "an unchanged entry kept its balance"
            with pytest.raises(ReservoirUndeclared, match="port_25"):
                again.check("ssct/A1", 100)
            again.refill("port_25")
            again.refill("ssct/A3")
            assert again.declared and again.remaining("port_25") == 5800

    def test_a_missing_or_corrupt_file_leaves_everything_undeclared(self, tmp_path):
        with closing(ReservoirLedger.load(tmp_path / "none.json", DEFS)) as ledger:
            assert ledger.undeclared == sorted(DEFS)
            ledger.refill("all")
            assert ledger.declared
        (tmp_path / "bad.json").write_text("{not json")
        with closing(ReservoirLedger.load(tmp_path / "bad.json", DEFS)) as ledger:
            assert not ledger.declared

    def test_a_second_process_cannot_hold_the_same_ledger(self, tmp_path):
        with closing(ReservoirLedger.load(tmp_path / "ledger.json", DEFS)):
            with pytest.raises(ReservoirInUse, match="in use"):
                ReservoirLedger.load(tmp_path / "ledger.json", DEFS)
        ReservoirLedger.load(tmp_path / "ledger.json", DEFS).close()

    def test_refilling_a_reservoir_name_fills_every_well_of_it(self, rig):
        ledger, pump, state = rig
        state["port"], state["well"] = SIPPER_PORT, "ssct/A1"
        pump.extract(EXTRACT_PORT, 1000)
        ledger.refill("ssct")
        assert ledger.remaining("ssct/A1") == 1750

    def test_a_refill_of_nothing_or_of_an_unknown_name_is_refused(self, rig):
        """A bottle has no plate, so a None key must not match every bottle."""
        ledger, pump, state = rig
        for key in (None, "water", "ssct/Z9"):
            with pytest.raises(ValueError, match="nothing to refill"):
                ledger.refill(key)

    def test_a_refill_that_cannot_be_written_changes_nothing(self, tmp_path):
        """Like a draw: a refill the file does not hold is not one, or memory
        would read full over a file that does not."""
        path = tmp_path / "ledger.json"
        path.mkdir()                               # a directory where the file should be
        ledger = ReservoirLedger(DEFS, path=path)
        ledger.restore({"reservoirs": {"ssct/A1": {"dispatched_ul": 700}},
                        "undeclared": ["port_25"]})
        with pytest.raises(ReservoirWriteError, match="not recorded"):
            ledger.refill("all")
        assert ledger.remaining("ssct/A1") == 1050
        assert ledger.remaining("port_25") == 4800
        assert ledger.undeclared == ["port_25"]

    @pytest.mark.parametrize("on_disk", [
        {"definitions": []},
        {"definitions": DEFS, "undeclared": 5},
        {"definitions": DEFS, "reservoirs": ["port_25"], "undeclared": []},
        {"definitions": DEFS, "reservoirs": {"port_25": {"dispatched_ul": "x"}}, "undeclared": []},
    ], ids=["definitions-a-list", "undeclared-a-number", "reservoirs-a-list", "balance-a-string"])
    def test_a_file_of_the_wrong_shape_is_corrupt_and_leaves_everything_undeclared(
            self, tmp_path, on_disk):
        path = tmp_path / "ledger.json"
        path.write_text(json.dumps(on_disk))
        with closing(ReservoirLedger.load(path, DEFS)) as ledger:
            assert ledger.undeclared == sorted(DEFS)

    def test_an_unexpected_failure_inside_load_releases_the_lock(self, tmp_path, monkeypatch):
        def fail(*args):
            raise RuntimeError("unexpected")

        with monkeypatch.context() as m:
            m.setattr(ReservoirLedger, "_reconcile", fail)
            with pytest.raises(RuntimeError, match="unexpected") as failure:
                ReservoirLedger.load(tmp_path / "ledger.json", DEFS)
        # `failure` keeps the traceback, and the half-built ledger in it, alive:
        # the lock must have been released by load, not by the garbage collector.
        ReservoirLedger.load(tmp_path / "ledger.json", DEFS).close()
        assert failure.value is not None

    @pytest.mark.parametrize("balance", ["NaN", "-500", "5001"],
                             ids=["nan", "negative", "above-loaded"])
    def test_a_balance_no_draw_could_have_left_is_undeclared(self, tmp_path, balance):
        """NaN passes every overdraw check (a volume > nan is False); a negative
        balance hands out liquid that was never loaded."""
        path = tmp_path / "ledger.json"
        path.write_text('{"definitions": %s, "reservoirs": {"port_25": {"dispatched_ul": %s}}, '
                        '"undeclared": []}' % (json.dumps(DEFS), balance))
        with closing(ReservoirLedger.load(path, DEFS)) as ledger:
            assert ledger.undeclared == ["port_25"]
            pump = FakePump()
            ledger.attach(pump, SIPPER_PORT, EXTRACT_PORT, lambda: 25, lambda: None)
            with pytest.raises(ReservoirUndeclared, match="port_25"):
                pump.extract(EXTRACT_PORT, 100)
            assert pump.started == []


class TestTheRunTally:
    """The per-run figure (used_rows): every draw through the reagent path,
    under the port the valves had open, declared volumes or not."""

    def test_a_reagent_draw_is_counted_under_the_open_port(self, tally):
        tally.state["port"] = 5      # port 5 first: the rows come in port order, not draw order
        tally.pump.extract(EXTRACT_PORT, 250)
        tally.pump.extract(EXTRACT_PORT, 250)
        tally.state["port"] = 3
        tally.pump.extract(EXTRACT_PORT, 500)
        assert tally.ledger.used_rows() == [(3, "DAPI", 500.0), (5, None, 500.0)]
        assert len(tally.pump.started) == 3, "a rig that tracks nothing refuses nothing"

    def test_a_run_start_resets_the_view(self, tally):
        tally.pump.extract(EXTRACT_PORT, 500)     # a manual draw between runs stays visible
        assert tally.ledger.used_rows() == [(3, "DAPI", 500.0)]
        tally.run_started()
        assert tally.ledger.used_rows() == [], "the experiment did not start clean"

    def test_a_runs_totals_reach_the_log_at_its_end_with_its_id(self, tally, caplog):
        tally.run_started()
        tally.state["port"] = 5      # port 5 first: the line is in port order too
        tally.pump.extract(EXTRACT_PORT, 40)
        tally.state["port"] = 3
        tally.pump.extract(EXTRACT_PORT, 1500)
        with caplog.at_level(logging.INFO, logger="fluidics.reservoirs"):
            tally.run_ended()
        assert "Reagent used (run-1): port 3 (DAPI): 1500 uL; port 5: 40 uL." in caplog.text

    def test_a_run_that_drew_nothing_says_so_at_its_end(self, tally, caplog):
        tally.run_started()
        with caplog.at_level(logging.INFO, logger="fluidics.reservoirs"):
            tally.run_ended()
        assert "Reagent used (run-1): none drawn." in caplog.text

    def test_names_are_read_when_the_rows_are_not_when_the_draw_was(self, tally):
        """The valves own the names: a port renamed after its draw shows the
        new name on the next paint."""
        tally.pump.extract(EXTRACT_PORT, 500)
        tally.names[3] = "Hoechst"
        assert tally.ledger.used_rows() == [(3, "Hoechst", 500.0)]

    def test_the_rows_are_a_copy(self, tally):
        """The report takes its rows in the RunEnded dispatch, and a later
        subscriber may chain the next run, whose start zeroes the figure:
        what was handed out must stand."""
        tally.pump.extract(EXTRACT_PORT, 500)
        rows = tally.ledger.used_rows()
        tally.run_started()
        assert rows == [(3, "DAPI", 500.0)]

    def test_the_per_run_figure_rides_on_no_snapshot(self, tally):
        """snapshot() is the file and the gate's twin: balances and the
        undeclared set, nothing of the run; and no restore touches the run."""
        tally.pump.extract(EXTRACT_PORT, 500)
        snapshot = tally.ledger.snapshot()
        assert set(snapshot) == {"format", "definitions", "reservoirs", "undeclared"}
        assert snapshot["reservoirs"] == {}
        tally.ledger.restore(snapshot)
        assert tally.ledger.used_rows() == [(3, "DAPI", 500.0)]


class TestBalances:
    """balances(): the Run tab's rows -- every reservoir drawn from this run
    or since its refill, what this run drew from each, and where a volume
    is declared what is loaded and left."""

    @pytest.fixture
    def named(self, tmp_path):
        """The tracked rig with the names a rig attaches: the valves' for
        every port, the config's for one well."""
        pump = FakePump()
        state = {"port": 25, "well": None}
        names = {SIPPER_PORT: "sipper", 25: "4x SSC", 26: "DAPI"}
        ledger = ReservoirLedger(DEFS, path=tmp_path / "ledger.json")
        ledger.attach(pump, SIPPER_PORT, EXTRACT_PORT, current_port=lambda: state["port"],
                      current_well=lambda: state["well"], port_to_reagent=names.get,
                      well_name={"ssct/A1": "hyb R01"}.get)
        return SimpleNamespace(ledger=ledger, pump=pump, state=state, names=names)

    def test_every_reservoir_drawn_from_has_a_row_in_key_order(self, named):
        """A tracked bottle, an untracked one (no volumes to show), a named
        well and one that takes the sipper port's name; and used_rows still
        answers per port, the wells summed under the sipper's."""
        named.pump.extract(EXTRACT_PORT, 500)
        named.state["port"] = 26
        named.pump.extract(EXTRACT_PORT, 300)
        named.state["port"], named.state["well"] = SIPPER_PORT, "ssct/A2"
        named.pump.extract(EXTRACT_PORT, 200)
        named.state["well"] = "ssct/A1"
        named.pump.extract(EXTRACT_PORT, 1000)
        assert named.ledger.balances() == [
            ("port_25", "4x SSC", 500.0, 5000, 4300.0),
            ("port_26", "DAPI", 300.0, None, None),
            ("ssct/A1", "hyb R01", 1000.0, 1800, 750.0),
            ("ssct/A2", "sipper", 200.0, 1800, 1550.0)]
        assert named.ledger.used_rows() == [(SIPPER_PORT, "sipper", 1200.0),
                                            (25, "4x SSC", 500.0), (26, "DAPI", 300.0)]

    def test_bottles_come_first_by_port_number_then_the_wells(self, named):
        """port_10 after port_2, as the valves count, not as strings sort."""
        for port in (10, 2):
            named.state["port"] = port
            named.pump.extract(EXTRACT_PORT, 100)
        named.state["port"], named.state["well"] = SIPPER_PORT, "ssct/A1"
        named.pump.extract(EXTRACT_PORT, 100)
        assert [key for key, *_ in named.ledger.balances()] == ["port_2", "port_10", "ssct/A1"]

    def test_a_run_start_zeroes_used_and_a_touched_reservoir_stays_listed(self, named):
        """Since its refill the well has given liquid, and the table says so
        with nothing drawn this run; the untracked bottle, with no balance
        of its own, is off the table until it is drawn from again."""
        named.state["port"], named.state["well"] = SIPPER_PORT, "ssct/A1"
        named.pump.extract(EXTRACT_PORT, 1000)
        named.state["port"] = 26
        named.pump.extract(EXTRACT_PORT, 300)
        named.ledger.on_run_event(RunStarted("run-1", ()))
        assert named.ledger.balances() == [("ssct/A1", "hyb R01", 0.0, 1800, 750.0)]
        assert named.ledger.used_rows() == []

    def test_names_are_read_when_the_rows_are(self, named):
        named.pump.extract(EXTRACT_PORT, 500)
        named.names[25] = "SSC"
        assert named.ledger.balances() == [("port_25", "SSC", 500.0, 5000, 4300.0)]


def test_tracked_names_every_declared_key_in_order():
    """What the system asks before the gate and the manual verb before a
    refill: a declared volume is a limit, and none declared, there is
    nothing to refuse or refill."""
    assert ReservoirLedger(DEFS).tracked == ("port_25", "ssct/A1", "ssct/A2")
    assert ReservoirLedger({}).tracked == ()


def test_snapshot_round_trips_through_restore():
    ledger = ReservoirLedger(DEFS)
    ledger.restore({"definitions": DEFS, "reservoirs": {"ssct/A1": {"dispatched_ul": 700}},
                    "undeclared": ["port_25"]})
    assert ledger.remaining("ssct/A1") == 1050
    assert ledger.undeclared == ["port_25"]
    assert ledger.snapshot()["reservoirs"]["ssct/A1"]["dispatched_ul"] == 700


def test_the_undeclared_refusal_names_a_wholly_undeclared_plate_once():
    """A first run has every well undeclared (193 entries on the sample
    rig): a plate none of whose wells is declared is named once, a plate
    partly declared by its undeclared wells, a bottle by its key."""
    defs = dict(DEFS, **{"probes/A1": {"loaded_ul": 110, "residual_ul": 5},
                         "probes/A2": {"loaded_ul": 110, "residual_ul": 5}})
    ledger = ReservoirLedger(defs)
    ledger.restore({"undeclared": ["port_25", "probes/A1", "probes/A2", "ssct/A2"]})
    with pytest.raises(ReservoirUndeclared) as refused:
        ledger.check("ssct/A1", 100)
    assert str(refused.value) == (
        "the reservoir ledger is undeclared for port_25, every well of probes, ssct/A2: "
        "declare refills (Refilled… on the Run Experiments tab, or --refilled) first")


def test_describe_keys_keeps_a_label_to_a_line_or_two():
    """The undeclared notice on a rig of 192 wells: every well of a plate as
    one phrase, more than four of them by their count, a bottle and the few
    wells of a plate by key."""
    wells = [f"probes/{well}" for well in ("A1", "B1", "C1", "D1", "E1", "F1")]
    defined = dict(DEFS, **{key: {"loaded_ul": 110, "residual_ul": 5} for key in wells})
    assert describe_keys(["port_25", *wells], defined) == "port_25, every well of probes"
    assert describe_keys(wells[:5], defined) == "5 wells of probes"
    assert describe_keys(wells[:4] + ["ssct/A2"], defined) == \
        "probes/A1, probes/B1, probes/C1, probes/D1, ssct/A2"
