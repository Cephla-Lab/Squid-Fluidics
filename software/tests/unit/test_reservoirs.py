"""VolumeTracker: counts every draw, authorizes the pump's, persists first, never guesses."""
import json
import logging
import re
from contextlib import closing
from types import SimpleNamespace

import pytest

from fluidics.errors import (ReservoirDrawTooLarge, ReservoirError, ReservoirInUse, ReservoirNoWell,
                             ReservoirOverdraw, ReservoirUndeclared, ReservoirWriteError)
from fluidics.events import RunEnded, RunStarted
from fluidics.reservoirs import VolumeTracker, describe_keys

DEFS = {"port_25": {"loaded_ul": 5000},
        "ssct/A1": {"loaded_ul": 1800},
        "ssct/A2": {"loaded_ul": 1800}}
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
    tracker = VolumeTracker(DEFS, path=tmp_path / "volumes.json")
    tracker.attach(pump, SIPPER_PORT, EXTRACT_PORT, current_port=lambda: state["port"],
                   current_well=lambda: state["well"])
    return tracker, pump, state


@pytest.fixture
def tally():
    """A rig that tracks nothing: the volume tracker is the tally alone, in memory
    (no file, no lock), with the valves' names for its ports; the run's
    boundary events reach it as the system delivers them."""
    pump = FakePump()
    state = {"port": 3}
    names = {3: "DAPI"}
    tracker = VolumeTracker({})
    tracker.attach(pump, SIPPER_PORT, EXTRACT_PORT, current_port=lambda: state["port"],
                   current_well=lambda: None, port_to_reagent=names.get)

    def started():
        tracker.on_run_event(RunStarted("run-1", ()))

    def ended():
        tracker.on_run_event(RunEnded("run-1", "finished", None, 0.0, None))

    return SimpleNamespace(tracker=tracker, pump=pump, state=state, names=names,
                           run_started=started, run_ended=ended)


class TestCharging:
    def test_a_bulk_draw_charges_the_open_ports_bottle(self, rig):
        tracker, pump, _ = rig
        pump.extract(EXTRACT_PORT, 500)
        assert tracker.remaining("port_25") == 5000 - 500
        assert tracker.used_rows() == [(25, None, 500.0)], "one charge keeps both figures"
        assert pump.started == [(EXTRACT_PORT, 500)]

    def test_a_sipper_draw_charges_the_current_well(self, rig):
        tracker, pump, state = rig
        state["port"], state["well"] = SIPPER_PORT, "ssct/A1"
        pump.extract(EXTRACT_PORT, 1000)
        assert tracker.remaining("ssct/A1") == 1800 - 1000
        assert tracker.remaining("port_25") == 5000

    def test_a_bulk_draw_after_a_well_row_charges_the_bottle_not_the_well_the_tip_sits_in(self, rig):
        """The sipper stays in its well while a bulk port is routed: the
        routed port decides, the well only when the sipper's port is routed."""
        tracker, pump, state = rig
        state["port"], state["well"] = SIPPER_PORT, "ssct/A1"
        pump.extract(EXTRACT_PORT, 100)
        state["port"] = 25                        # the valve moved on; the tip did not
        pump.extract(EXTRACT_PORT, 500)
        assert tracker.remaining("ssct/A1") == 1700
        assert tracker.remaining("port_25") == 4500

    def test_the_sippers_port_with_the_tip_in_no_well_is_refused_at_the_pump(self, rig):
        tracker, pump, state = rig
        state["port"], state["well"] = SIPPER_PORT, None
        with pytest.raises(ReservoirNoWell):
            pump.extract(EXTRACT_PORT, 100)
        assert pump.started == []

    def test_an_untracked_bottles_draw_is_counted_never_refused_never_written(self, rig, tmp_path):
        """No declared volume, so nothing to overdraw: the draw goes to the
        per-run figure and nowhere else -- not to rows(), not to the file."""
        tracker, pump, state = rig
        state["port"] = 26
        pump.extract(EXTRACT_PORT, 500)
        pump.extract(EXTRACT_PORT, 10 ** 9)
        assert tracker.used_rows() == [(26, None, 500.0 + 10 ** 9)]
        assert tracker.rows() == []
        assert not (tmp_path / "volumes.json").exists(), "an untracked draw is not the file's"
        assert len(pump.started) == 2

    def test_a_draw_through_another_syringe_port_is_ignored_everywhere(self, rig, tmp_path):
        """A chamber draw, air: not reagent, so neither figure moves."""
        tracker, pump, _ = rig
        pump.extract(1, 500)
        assert tracker.used_rows() == [] and tracker.rows() == []
        assert not (tmp_path / "volumes.json").exists()
        assert pump.started == [(1, 500)]

    def test_a_well_draw_is_counted_under_the_sippers_port(self, rig):
        """The file knows wells; the per-run figure knows ports, the thing
        the valves name, so every well's draw is the sipper port's."""
        tracker, pump, state = rig
        state["port"], state["well"] = SIPPER_PORT, "ssct/A1"
        pump.extract(EXTRACT_PORT, 1000)
        state["well"] = "ssct/A2"
        pump.extract(EXTRACT_PORT, 200)
        assert tracker.used_rows() == [(SIPPER_PORT, None, 1200.0)]
        assert tracker.rows() == [("ssct/A1", 1000.0, 800.0), ("ssct/A2", 200.0, 1600.0)]

    @pytest.mark.parametrize("volume", [float("nan"), -100], ids=["nan", "negative"])
    def test_a_draw_that_is_not_a_volume_is_refused_on_an_untracked_port_too(self, rig, volume):
        """The volume check comes before the untracked return: counted, a
        NaN would poison that port's per-run figure as it would a balance."""
        tracker, pump, state = rig
        state["port"] = 26
        refused = re.escape(f"port_26: {volume!r} uL is not a volume")
        with pytest.raises(ReservoirError, match=refused):
            pump.extract(EXTRACT_PORT, volume)
        assert tracker.used_rows() == []
        assert pump.started == []

    def test_an_overdraw_is_refused_before_the_pump_starts_and_charges_nothing(self, rig):
        tracker, pump, state = rig
        state["port"], state["well"] = SIPPER_PORT, "ssct/A1"
        pump.extract(EXTRACT_PORT, 1000)
        with pytest.raises(ReservoirOverdraw, match="ssct/A1.*1000.*800"):
            pump.extract(EXTRACT_PORT, 1000)
        assert tracker.remaining("ssct/A1") == 800
        assert pump.started == [(EXTRACT_PORT, 1000)]

    def test_a_bottle_overdraw_is_refused_too(self, rig):
        tracker, pump, _ = rig
        pump.extract(EXTRACT_PORT, 5000)
        with pytest.raises(ReservoirOverdraw, match="port_25"):
            pump.extract(EXTRACT_PORT, 1)
        assert tracker.remaining("port_25") == 0

    def test_check_refuses_an_overdraw_and_a_draw_too_large(self, rig):
        tracker, pump, state = rig
        state["port"], state["well"] = SIPPER_PORT, "ssct/A1"
        pump.extract(EXTRACT_PORT, 1000)
        with pytest.raises(ReservoirOverdraw, match="ssct/A1.*1000.*800"):
            tracker.check("ssct/A1", 1000)
        with pytest.raises(ReservoirDrawTooLarge, match="split"):
            tracker.check("ssct/A2", 2000)
        tracker.check("ssct/A2", 1800)     # exactly what it holds when loaded: fine
        tracker.check("port_99", 10 ** 9)  # untracked: not the volume tracker's business

    def test_the_sippers_port_over_a_well_volume_tracking_does_not_define_is_refused(self, tmp_path):
        """Every well of every plate is defined, so a well the volume tracker does not
        know is not an untracked bottle: letting it through would be a guess."""
        pump = FakePump()
        tracker = VolumeTracker({"probes/A1": {"loaded_ul": 1800}},
                                path=tmp_path / "volumes.json")
        tracker.attach(pump, SIPPER_PORT, EXTRACT_PORT, lambda: SIPPER_PORT, lambda: "probes/B2")
        with pytest.raises(ReservoirUndeclared, match="probes/B2"):
            pump.extract(EXTRACT_PORT, 100)
        assert pump.started == []

    @pytest.mark.parametrize("volume", [float("nan"), -100, float("inf")],
                             ids=["nan", "negative", "infinite"])
    def test_a_draw_that_is_not_a_finite_volume_is_refused_and_charges_nothing(
            self, rig, tmp_path, volume):
        """NaN fails every comparison, so it passes every check; charged, it
        makes the balance NaN and lets every later draw through."""
        tracker, pump, _ = rig
        refused = re.escape(f"port_25: {volume!r} uL is not a volume")
        with pytest.raises(ReservoirError, match=refused):
            tracker.authorize(EXTRACT_PORT, volume)
        assert tracker.remaining("port_25") == 5000
        assert tracker.used_rows() == []
        assert not (tmp_path / "volumes.json").exists(), "nothing was charged, nothing written"
        with pytest.raises(ReservoirError, match=refused):
            tracker.check("port_25", volume)
        pump.extract(EXTRACT_PORT, 500)               # and a real draw is still charged
        assert tracker.remaining("port_25") == 4500

    def test_a_draw_of_nothing_passes_and_charges_nothing(self, rig, tmp_path):
        """No liquid moves, so there is no air to refuse; the operations can
        compute a zero volume, and it must not stop the run."""
        tracker, pump, _ = rig
        pump.extract(EXTRACT_PORT, 0)
        tracker.check("port_25", 0)
        assert pump.started == [(EXTRACT_PORT, 0)]
        assert tracker.remaining("port_25") == 5000
        assert tracker.rows() == [] and tracker.used_rows() == []
        assert not (tmp_path / "volumes.json").exists(), "nothing was charged, nothing written"


class TestPersistence:
    def test_every_charge_is_written_before_the_pump_starts(self, rig, tmp_path):
        tracker, pump, _ = rig
        seen = []
        pump.draw_authorizers.append(
            lambda port, ul: seen.append(json.loads((tmp_path / "volumes.json").read_text())))
        pump.extract(EXTRACT_PORT, 500)
        assert seen[0]["reservoirs"]["port_25"]["dispatched_ul"] == 500

    def test_a_write_that_fails_refuses_the_draw_and_charges_nothing(self, tmp_path):
        pump = FakePump()
        path = tmp_path / "volumes.json"
        path.mkdir()                               # a directory where the file should be
        tracker = VolumeTracker(DEFS, path=path)
        tracker.attach(pump, SIPPER_PORT, EXTRACT_PORT, lambda: 25, lambda: None)
        with pytest.raises(ReservoirWriteError, match="not started"):
            pump.extract(EXTRACT_PORT, 500)
        assert tracker.remaining("port_25") == 5000
        assert tracker.used_rows() == [], "a draw that never started is in no figure"
        assert pump.started == []

    def test_a_matching_file_restores(self, rig, tmp_path):
        tracker, pump, _ = rig
        pump.extract(EXTRACT_PORT, 500)
        with closing(VolumeTracker.load(tmp_path / "volumes.json", DEFS)) as again:
            assert again.declared and again.remaining("port_25") == 4500

    def test_a_changed_entry_is_undeclared_and_the_others_keep_their_balances(self, rig, tmp_path):
        """Review focus 3: an edited loaded volume must not resurrect a
        reservoir; the entries that did not change are not punished."""
        tracker, pump, state = rig
        pump.extract(EXTRACT_PORT, 500)
        state["port"], state["well"] = SIPPER_PORT, "ssct/A1"
        pump.extract(EXTRACT_PORT, 300)
        changed = dict(DEFS, port_25={"loaded_ul": 6000}, **{"ssct/A3": {"loaded_ul": 1800}})
        with closing(VolumeTracker.load(tmp_path / "volumes.json", changed)) as again:
            assert again.undeclared == ["port_25", "ssct/A3"]
            assert again.remaining("ssct/A1") == 1500, "an unchanged entry kept its balance"
            with pytest.raises(ReservoirUndeclared, match="port_25"):
                again.check("ssct/A1", 100)
            again.refill("port_25")
            again.refill("ssct/A3")
            assert again.declared and again.remaining("port_25") == 6000

    def test_a_missing_or_corrupt_file_leaves_everything_undeclared(self, tmp_path):
        with closing(VolumeTracker.load(tmp_path / "none.json", DEFS)) as tracker:
            assert tracker.undeclared == sorted(DEFS)
            tracker.refill("all")
            assert tracker.declared
        (tmp_path / "bad.json").write_text("{not json")
        with closing(VolumeTracker.load(tmp_path / "bad.json", DEFS)) as tracker:
            assert not tracker.declared

    def test_a_second_process_cannot_hold_the_same_volume_tracking_file(self, tmp_path):
        with closing(VolumeTracker.load(tmp_path / "volumes.json", DEFS)):
            with pytest.raises(ReservoirInUse, match="in use"):
                VolumeTracker.load(tmp_path / "volumes.json", DEFS)
        VolumeTracker.load(tmp_path / "volumes.json", DEFS).close()

    def test_refilling_a_reservoir_name_fills_every_well_of_it(self, rig):
        tracker, pump, state = rig
        state["port"], state["well"] = SIPPER_PORT, "ssct/A1"
        pump.extract(EXTRACT_PORT, 1000)
        tracker.refill("ssct")
        assert tracker.remaining("ssct/A1") == 1800

    def test_a_refill_of_nothing_or_of_an_unknown_name_is_refused(self, rig):
        """A bottle has no plate, so a None key must not match every bottle;
        an untracked bottle never drawn from has no figure to reset."""
        tracker, pump, state = rig
        for key in (None, "water", "ssct/Z9", "port_26"):
            with pytest.raises(ValueError, match="nothing to refill"):
                tracker.refill(key)

    def test_a_refill_that_cannot_be_written_changes_nothing(self, tmp_path):
        """Like a draw: a refill the file does not hold is not one, or memory
        would read full over a file that does not."""
        path = tmp_path / "volumes.json"
        path.mkdir()                               # a directory where the file should be
        tracker = VolumeTracker(DEFS, path=path)
        tracker.restore({"reservoirs": {"ssct/A1": {"dispatched_ul": 700}},
                         "undeclared": ["port_25"]})
        with pytest.raises(ReservoirWriteError, match="not recorded"):
            tracker.refill("all")
        assert tracker.remaining("ssct/A1") == 1100
        assert tracker.remaining("port_25") == 5000
        assert tracker.undeclared == ["port_25"]

    def test_several_keys_reset_in_one_write_or_not_at_all(self, tmp_path):
        """A selection of rows: one write. The file refusing it leaves every
        one of them as it was, and a key that names nothing refuses all of
        them before anything changes."""
        tracker = VolumeTracker(DEFS, path=tmp_path / "volumes.json")
        tracker.restore({"reservoirs": {"ssct/A1": {"dispatched_ul": 700},
                                        "port_25": {"dispatched_ul": 300}}})
        writes = []
        write = tracker._write
        tracker._write = lambda: writes.append(1) or write()
        with pytest.raises(ValueError, match="nothing to refill named 'water'"):
            tracker.refill("ssct/A1", "water")
        assert writes == [] and tracker.remaining("ssct/A1") == 1100
        tracker.refill("ssct/A1", "port_25")
        assert writes == [1]
        assert tracker.remaining("ssct/A1") == 1800 and tracker.remaining("port_25") == 5000
        blocked = tmp_path / "blocked.json"
        blocked.mkdir()                            # a directory where the file should be
        failing = VolumeTracker(DEFS, path=blocked)
        failing.restore({"reservoirs": {"ssct/A1": {"dispatched_ul": 700},
                                        "port_25": {"dispatched_ul": 300}}})
        with pytest.raises(ReservoirWriteError, match="'ssct/A1', 'port_25' was not recorded"):
            failing.refill("ssct/A1", "port_25")
        assert failing.remaining("ssct/A1") == 1100 and failing.remaining("port_25") == 4700

    def test_all_on_a_rig_with_no_bottle_or_well_says_so(self):
        with pytest.raises(ValueError, match="no bottle or well to reset"):
            VolumeTracker({}).refill("all")

    @pytest.mark.parametrize("on_disk", [
        {"definitions": []},
        {"definitions": DEFS, "undeclared": 5},
        {"definitions": DEFS, "reservoirs": ["port_25"], "undeclared": []},
        {"definitions": DEFS, "reservoirs": {"port_25": {"dispatched_ul": "x"}}, "undeclared": []},
    ], ids=["definitions-a-list", "undeclared-a-number", "reservoirs-a-list", "balance-a-string"])
    def test_a_file_of_the_wrong_shape_is_corrupt_and_leaves_everything_undeclared(
            self, tmp_path, on_disk):
        path = tmp_path / "volumes.json"
        path.write_text(json.dumps(on_disk))
        with closing(VolumeTracker.load(path, DEFS)) as tracker:
            assert tracker.undeclared == sorted(DEFS)

    def test_an_unexpected_failure_inside_load_releases_the_lock(self, tmp_path, monkeypatch):
        def fail(*args):
            raise RuntimeError("unexpected")

        with monkeypatch.context() as m:
            m.setattr(VolumeTracker, "_reconcile", fail)
            with pytest.raises(RuntimeError, match="unexpected") as failure:
                VolumeTracker.load(tmp_path / "volumes.json", DEFS)
        # `failure` keeps the traceback, and the half-built volume tracker in it, alive:
        # the lock must have been released by load, not by the garbage collector.
        VolumeTracker.load(tmp_path / "volumes.json", DEFS).close()
        assert failure.value is not None

    @pytest.mark.parametrize("balance", ["NaN", "-500", "5001"],
                             ids=["nan", "negative", "above-loaded"])
    def test_a_balance_no_draw_could_have_left_is_undeclared(self, tmp_path, balance):
        """NaN passes every overdraw check (a volume > nan is False); a negative
        balance hands out liquid that was never loaded."""
        path = tmp_path / "volumes.json"
        path.write_text('{"definitions": %s, "reservoirs": {"port_25": {"dispatched_ul": %s}}, '
                        '"undeclared": []}' % (json.dumps(DEFS), balance))
        with closing(VolumeTracker.load(path, DEFS)) as tracker:
            assert tracker.undeclared == ["port_25"]
            pump = FakePump()
            tracker.attach(pump, SIPPER_PORT, EXTRACT_PORT, lambda: 25, lambda: None)
            with pytest.raises(ReservoirUndeclared, match="port_25"):
                pump.extract(EXTRACT_PORT, 100)
            assert pump.started == []

    def test_a_drained_reservoir_restores_declared_and_empty(self, tmp_path):
        """Everything loaded drawn is the balance a used-up reservoir leaves:
        it restores as that, declared with nothing left, so the next draw is
        an overdraw to refill for, not an undeclared refusal."""
        path = tmp_path / "volumes.json"
        path.write_text('{"definitions": %s, "reservoirs": {"port_25": {"dispatched_ul": 5000}}, '
                        '"undeclared": []}' % json.dumps(DEFS))
        with closing(VolumeTracker.load(path, DEFS)) as tracker:
            assert tracker.undeclared == []
            assert tracker.remaining("port_25") == 0
            with pytest.raises(ReservoirOverdraw, match="port_25"):
                tracker.check("port_25", 1)


class TestTheRunTally:
    """The per-run figure (used_rows): every draw through the reagent path,
    under the port the valves had open, declared volumes or not."""

    def test_a_reagent_draw_is_counted_under_the_open_port(self, tally):
        tally.state["port"] = 5      # port 5 first: the rows come in port order, not draw order
        tally.pump.extract(EXTRACT_PORT, 250)
        tally.pump.extract(EXTRACT_PORT, 250)
        tally.state["port"] = 3
        tally.pump.extract(EXTRACT_PORT, 500)
        assert tally.tracker.used_rows() == [(3, "DAPI", 500.0), (5, None, 500.0)]
        assert len(tally.pump.started) == 3, "a rig that tracks nothing refuses nothing"

    def test_a_run_start_resets_the_view(self, tally):
        tally.pump.extract(EXTRACT_PORT, 500)     # a manual draw between runs stays visible
        assert tally.tracker.used_rows() == [(3, "DAPI", 500.0)]
        tally.run_started()
        assert tally.tracker.used_rows() == [], "the experiment did not start clean"

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
        assert tally.tracker.used_rows() == [(3, "Hoechst", 500.0)]

    def test_the_rows_are_a_copy(self, tally):
        """The report takes its rows in the RunEnded dispatch, and a later
        subscriber may chain the next run, whose start zeroes the figure:
        what was handed out must stand."""
        tally.pump.extract(EXTRACT_PORT, 500)
        rows = tally.tracker.used_rows()
        tally.run_started()
        assert rows == [(3, "DAPI", 500.0)]

    def test_the_per_run_figure_rides_on_no_snapshot(self, tally):
        """snapshot() is the file and the gate's twin: balances and the
        undeclared set, nothing of the run; and no restore touches the run."""
        tally.pump.extract(EXTRACT_PORT, 500)
        snapshot = tally.tracker.snapshot()
        assert set(snapshot) == {"format", "definitions", "reservoirs", "undeclared"}
        assert snapshot["reservoirs"] == {}
        tally.tracker.restore(snapshot)
        assert tally.tracker.used_rows() == [(3, "DAPI", 500.0)]


class TestTheUntrackedFigure:
    """What an untracked bottle has given since launch or its last reset:
    counted on every draw, run after run, in memory alone; its reset zeroes
    it with no write and nothing to declare."""

    def test_a_bottle_the_rig_can_draw_from_is_listed_before_its_first_draw(self):
        """The rig's bottles come in with the definitions: each listed at 0
        from the start, in port order among the tracked one, untracked ones
        with no volumes, and none of it on the file."""
        tracker = VolumeTracker(DEFS, bottles=["port_26", "port_3", "port_25"])
        assert [(key, accumulated, current, loaded)
                for key, _, accumulated, current, loaded, _ in tracker.balances()] == [
            ("port_3", 0.0, None, None), ("port_25", 0.0, 5000, 5000),
            ("port_26", 0.0, None, None), ("ssct/A1", 0.0, 1800, 1800),
            ("ssct/A2", 0.0, 1800, 1800)]
        assert tracker.rows() == [] and tracker.snapshot()["reservoirs"] == {}

    def test_a_tracker_loaded_from_its_file_lists_its_bottles_too(self, tmp_path):
        """A real rig's tracker comes from load(): the bottles come with it,
        listed at 0 and kept off the file."""
        with closing(VolumeTracker.load(tmp_path / "volumes.json", DEFS,
                                        bottles=["port_26"])) as tracker:
            assert ("port_26", None, 0.0, None, None, False) in tracker.balances()
            assert "port_26" not in tracker.snapshot()["reservoirs"]

    def test_a_bottle_never_drawn_from_resets_without_a_write(self, tmp_path):
        """Listed from launch, a bottle can be reset before its first draw
        (Reset selected on a fresh row, --refilled port_26), and Reset all
        on a rig with bottles and nothing tracked is not refused."""
        tracker = VolumeTracker(DEFS, path=tmp_path / "volumes.json", bottles=["port_26"])
        tracker.refill("port_26")
        assert ("port_26", None, 0.0, None, None, False) in tracker.balances()
        assert not (tmp_path / "volumes.json").exists(), "nothing tracked was reset: no write"
        untracked = VolumeTracker({}, bottles=["port_3"])
        untracked.refill("all")
        assert [key for key, *_ in untracked.balances()] == ["port_3"]

    def test_an_untracked_bottle_accumulates_across_runs_and_never_reaches_the_file(self, rig, tmp_path):
        tracker, pump, state = rig
        state["port"] = 26
        pump.extract(EXTRACT_PORT, 300)
        tracker.on_run_event(RunStarted("run-1", ()))
        pump.extract(EXTRACT_PORT, 200)
        assert ("port_26", None, 500.0, None, None, False) in tracker.balances()
        assert tracker.used_rows() == [(26, None, 200.0)], "the run's figure starts over"
        assert tracker.rows() == [] and tracker.snapshot()["reservoirs"] == {}
        assert not (tmp_path / "volumes.json").exists()

    def test_its_reset_zeroes_it_without_a_write_and_keeps_its_row(self, rig, tmp_path):
        """The run's own figure is the run's: the report and the run-end
        line keep what this run drew."""
        tracker, pump, state = rig
        state["port"] = 26
        pump.extract(EXTRACT_PORT, 300)
        tracker.refill("port_26")
        assert ("port_26", None, 0.0, None, None, False) in tracker.balances()
        assert not (tmp_path / "volumes.json").exists(), "nothing tracked was reset: no write"
        assert tracker.used_rows() == [(26, None, 300.0)]
        pump.extract(EXTRACT_PORT, 50)
        assert ("port_26", None, 50.0, None, None, False) in tracker.balances()

    def test_reset_all_zeroes_the_untracked_bottles_with_the_tracked(self, rig, tmp_path):
        tracker, pump, state = rig
        pump.extract(EXTRACT_PORT, 500)               # port_25: tracked
        state["port"] = 26
        pump.extract(EXTRACT_PORT, 300)
        tracker.refill("all")
        assert [(key, accumulated) for key, _, accumulated, *_ in tracker.balances()] == [
            ("port_25", 0.0), ("port_26", 0.0), ("ssct/A1", 0.0), ("ssct/A2", 0.0)]
        assert json.loads((tmp_path / "volumes.json").read_text())["reservoirs"] == {}

    def test_a_reset_all_that_cannot_be_written_leaves_the_untracked_figure_too(self, tmp_path):
        """One reset, all or nothing: memory must not read reset over a
        file that is not."""
        path = tmp_path / "volumes.json"
        path.mkdir()                               # a directory where the file should be
        pump = FakePump()
        tracker = VolumeTracker(DEFS, path=path)
        tracker.attach(pump, SIPPER_PORT, EXTRACT_PORT, lambda: 26, lambda: None)
        pump.extract(EXTRACT_PORT, 300)            # untracked: no write, so it goes
        with pytest.raises(ReservoirWriteError, match="not recorded"):
            tracker.refill("all")
        assert ("port_26", None, 300.0, None, None, False) in tracker.balances()

    def test_a_rig_that_tracks_nothing_accumulates_and_resets_its_bottles(self, tally):
        tally.pump.extract(EXTRACT_PORT, 500)
        tally.run_started()
        tally.pump.extract(EXTRACT_PORT, 100)
        assert tally.tracker.balances() == [("port_3", "DAPI", 600.0, None, None, False)]
        tally.tracker.refill("all")
        assert tally.tracker.balances() == [("port_3", "DAPI", 0.0, None, None, False)]
        assert tally.tracker.used_rows() == [(3, "DAPI", 100.0)]


class TestBalances:
    """balances(): the Reservoirs window's rows -- every tracked reservoir
    and every bottle the volume tracker was given, drawn from or not, a
    reset keeping its row (built without them, a bottle joins at its first
    draw): what each has given since its reset and, where a volume is
    declared, what is left and what was loaded; and whether the volume
    tracker lacks its declaration."""

    @pytest.fixture
    def named(self, tmp_path):
        """The tracked rig with the names a rig attaches: the valves' for
        every port, the config's for one well."""
        pump = FakePump()
        state = {"port": 25, "well": None}
        names = {SIPPER_PORT: "sipper", 25: "4x SSC", 26: "DAPI"}
        tracker = VolumeTracker(DEFS, path=tmp_path / "volumes.json")
        tracker.attach(pump, SIPPER_PORT, EXTRACT_PORT, current_port=lambda: state["port"],
                       current_well=lambda: state["well"], port_to_reagent=names.get,
                      well_name={"ssct/A1": "hyb R01"}.get)
        return SimpleNamespace(tracker=tracker, pump=pump, state=state, names=names)

    def test_every_tracked_reservoir_has_a_row_before_any_draw(self, named):
        """Full and declared, a named well and one that takes the sipper
        port's name; no untracked bottle has been drawn from."""
        assert named.tracker.balances() == [
            ("port_25", "4x SSC", 0.0, 5000.0, 5000, False),
            ("ssct/A1", "hyb R01", 0.0, 1800.0, 1800, False),
            ("ssct/A2", "sipper", 0.0, 1800.0, 1800, False)]

    def test_each_draw_accumulates_on_its_reservoir_with_blanks_where_none_is_declared(self, named):
        """A tracked bottle, an untracked one (no volumes to show) and two
        wells; used_rows still answers per port, the wells summed under the
        sipper's."""
        named.pump.extract(EXTRACT_PORT, 500)
        named.state["port"] = 26
        named.pump.extract(EXTRACT_PORT, 300)
        named.state["port"], named.state["well"] = SIPPER_PORT, "ssct/A2"
        named.pump.extract(EXTRACT_PORT, 200)
        named.state["well"] = "ssct/A1"
        named.pump.extract(EXTRACT_PORT, 1000)
        assert named.tracker.balances() == [
            ("port_25", "4x SSC", 500.0, 4500.0, 5000, False),
            ("port_26", "DAPI", 300.0, None, None, False),
            ("ssct/A1", "hyb R01", 1000.0, 800.0, 1800, False),
            ("ssct/A2", "sipper", 200.0, 1600.0, 1800, False)]
        assert named.tracker.used_rows() == [(SIPPER_PORT, "sipper", 1200.0),
                                             (25, "4x SSC", 500.0), (26, "DAPI", 300.0)]

    def test_bottles_come_first_by_port_number_then_the_wells_by_plate_in_its_own_order(self):
        """port_10 after port_2, as the valves count, not as strings sort;
        a plate's wells column-major, as the plate and the well pickers
        order them: B1 before A2, A2 before A10."""
        pump, state = FakePump(), {"port": 10}
        well = {"loaded_ul": 110}
        tracker = VolumeTracker({key: well for key in
                                 ("ssct/A1", "probes/A10", "probes/A2", "probes/B1", "port_25")})
        tracker.attach(pump, SIPPER_PORT, EXTRACT_PORT, lambda: state["port"], lambda: None)
        for port in (10, 2):
            state["port"] = port
            pump.extract(EXTRACT_PORT, 100)
        assert [key for key, *_ in tracker.balances()] == [
            "port_2", "port_10", "port_25", "probes/B1", "probes/A2", "probes/A10", "ssct/A1"]

    def test_a_run_start_zeroes_used_rows_and_nothing_accumulated(self, named):
        """The run's figure starts over at RunStarted; what a reservoir has
        given since its reset does not, an untracked bottle's included."""
        named.state["port"], named.state["well"] = SIPPER_PORT, "ssct/A1"
        named.pump.extract(EXTRACT_PORT, 1000)
        named.state["port"] = 26
        named.pump.extract(EXTRACT_PORT, 300)
        named.tracker.on_run_event(RunStarted("run-1", ()))
        rows = {key: tuple(row) for key, *row in named.tracker.balances()}
        assert rows["ssct/A1"] == ("hyb R01", 1000.0, 800.0, 1800, False)
        assert rows["port_26"] == ("DAPI", 300.0, None, None, False)
        assert named.tracker.used_rows() == []

    def test_an_undeclared_reservoir_is_flagged_until_its_reset(self, named):
        named.tracker.restore({"undeclared": ["port_25", "ssct/A2"]})
        assert [(key, undeclared) for key, *_, undeclared in named.tracker.balances()] == [
            ("port_25", True), ("ssct/A1", False), ("ssct/A2", True)]
        named.tracker.refill("port_25")
        assert [key for key, *_, undeclared in named.tracker.balances() if undeclared] == ["ssct/A2"]

    def test_a_reset_puts_a_tracked_reservoir_back_to_full(self, named):
        named.state["port"], named.state["well"] = SIPPER_PORT, "ssct/A1"
        named.pump.extract(EXTRACT_PORT, 1000)
        named.tracker.refill("ssct/A1")
        assert ("ssct/A1", "hyb R01", 0.0, 1800.0, 1800, False) in named.tracker.balances()

    def test_names_are_read_when_the_rows_are(self, named):
        named.pump.extract(EXTRACT_PORT, 500)
        named.names[25] = "SSC"
        assert named.tracker.balances()[0] == ("port_25", "SSC", 500.0, 4500.0, 5000, False)


class TestSetLoaded:
    """set_loaded(): what a reservoir holds when loaded, set while the rig
    runs. It changes a number, not what is in the reservoir: accumulated
    stands, current follows, and only a reset declares."""

    def _on_disk(self, tmp_path):
        return json.loads((tmp_path / "volumes.json").read_text())

    def test_accumulated_stands_and_current_follows_the_new_volume(self, rig, tmp_path):
        tracker, pump, state = rig
        state["port"], state["well"] = SIPPER_PORT, "ssct/A1"
        pump.extract(EXTRACT_PORT, 1490)
        tracker.set_loaded({"ssct/A1": 2500})
        assert tracker.loaded("ssct/A1") == 2500 and tracker.remaining("ssct/A1") == 1010
        assert ("ssct/A1", None, 1490.0, 1010.0, 2500, False) in tracker.balances()
        on_disk = self._on_disk(tmp_path)
        assert on_disk["definitions"]["ssct/A1"] == {"loaded_ul": 2500}
        assert on_disk["reservoirs"]["ssct/A1"] == {"dispatched_ul": 1490.0}
        pump.extract(EXTRACT_PORT, 1000)              # 1000 more fits the new volume
        assert tracker.remaining("ssct/A1") == 10

    def test_an_undeclared_reservoir_stays_undeclared_until_its_reset(self, rig):
        tracker, _, _ = rig
        tracker.restore({"undeclared": ["port_25"]})
        tracker.set_loaded({"port_25": 6000})
        assert tracker.undeclared == ["port_25"]
        tracker.refill("port_25")
        assert tracker.declared and tracker.remaining("port_25") == 6000

    def test_a_volume_below_what_the_reservoir_has_given_is_refused(self, rig):
        """The balance a file restore would refuse as no draw could leave
        it; exactly what it has given is the floor, leaving it empty."""
        tracker, pump, state = rig
        state["port"], state["well"] = SIPPER_PORT, "ssct/A1"
        pump.extract(EXTRACT_PORT, 1490)
        with pytest.raises(ValueError, match=re.escape(
                "ssct/A1 has given 1490 uL since its last reset: its loaded volume cannot be less")):
            tracker.set_loaded({"ssct/A1": 1489})
        assert tracker.loaded("ssct/A1") == 1800
        tracker.set_loaded({"ssct/A1": 1490})
        assert tracker.remaining("ssct/A1") == 0

    @pytest.mark.parametrize("volume", [0, -5, 1.5, True, "350"],
                             ids=["zero", "negative", "fraction", "bool", "text"])
    def test_a_volume_that_is_not_a_whole_number_above_0_is_refused(self, rig, volume):
        tracker, _, _ = rig
        with pytest.raises(ValueError, match=re.escape(f"port_25: {volume!r} uL is not a loaded volume")):
            tracker.set_loaded({"port_25": volume})
        assert tracker.loaded("port_25") == 5000

    @pytest.mark.parametrize("key", ["water", "ssct/Z9", "probes/A1", "port_26"])
    def test_a_key_that_names_no_tracked_reservoir_or_bottle_is_refused(self, rig, key):
        """A well the tracker does not define is no well of this rig; a
        bottle it does not list (built without its bottles, never drawn
        from) is no bottle it knows."""
        tracker, _, _ = rig
        with pytest.raises(ValueError, match=re.escape(f"{key} names no tracked reservoir or bottle")):
            tracker.set_loaded({key: 1000})
        assert key not in tracker.tracked

    def test_several_keys_are_set_in_one_write_or_not_at_all(self, rig, tmp_path):
        tracker, _, _ = rig
        writes = []
        write = tracker._write
        tracker._write = lambda: writes.append(1) or write()
        with pytest.raises(ValueError, match="ssct/A2: 0 uL"):
            tracker.set_loaded({"port_25": 6000, "ssct/A2": 0})
        assert writes == [] and tracker.loaded("port_25") == 5000
        assert not (tmp_path / "volumes.json").exists()
        tracker.set_loaded({"port_25": 6000, "ssct/A2": 2000})
        assert writes == [1]
        assert tracker.loaded("port_25") == 6000 and tracker.loaded("ssct/A2") == 2000

    def test_an_untracked_bottle_given_a_volume_is_tracked_from_then_on(self, tmp_path):
        """Undeclared until its reset -- what it gave before it was tracked
        is not known -- its accumulated figure kept and now on the file; once
        reset, declared full and its draws limited."""
        pump = FakePump()
        tracker = VolumeTracker(DEFS, path=tmp_path / "volumes.json", bottles=["port_26"])
        tracker.attach(pump, SIPPER_PORT, EXTRACT_PORT, lambda: 26, lambda: None)
        pump.extract(EXTRACT_PORT, 300)               # untracked: counted in memory alone
        tracker.set_loaded({"port_26": 1000})
        assert tracker.tracked == ("port_25", "port_26", "ssct/A1", "ssct/A2")
        assert tracker.undeclared == ["port_26"]
        assert ("port_26", None, 300.0, 700.0, 1000, True) in tracker.balances()
        assert ("port_26", 300.0, 700.0) in tracker.rows()
        on_disk = self._on_disk(tmp_path)
        assert on_disk["definitions"]["port_26"] == {"loaded_ul": 1000}
        assert on_disk["reservoirs"]["port_26"] == {"dispatched_ul": 300.0}
        assert on_disk["undeclared"] == ["port_26"]
        with pytest.raises(ReservoirUndeclared, match="port_26"):
            pump.extract(EXTRACT_PORT, 1)
        tracker.refill("port_26")
        assert tracker.declared and tracker.remaining("port_26") == 1000
        pump.extract(EXTRACT_PORT, 600)
        with pytest.raises(ReservoirOverdraw, match="port_26"):
            pump.extract(EXTRACT_PORT, 401)

    def test_a_new_bottle_never_drawn_from_is_in_rows_once_drawn(self, tmp_path):
        pump = FakePump()
        tracker = VolumeTracker(DEFS, path=tmp_path / "volumes.json", bottles=["port_26"])
        tracker.attach(pump, SIPPER_PORT, EXTRACT_PORT, lambda: 26, lambda: None)
        tracker.set_loaded({"port_26": 1000})
        assert "port_26" in tracker.tracked and tracker.rows() == []
        tracker.refill("port_26")                     # newly tracked: declared by its reset
        pump.extract(EXTRACT_PORT, 100)
        assert tracker.rows() == [("port_26", 100.0, 900.0)]
        assert self._on_disk(tmp_path)["reservoirs"]["port_26"] == {"dispatched_ul": 100.0}

    def test_a_setting_the_file_cannot_hold_changes_nothing(self, tmp_path):
        """Like a refill: memory must not read the new volume over a file
        that does not, and a bottle that would have become tracked is not."""
        path = tmp_path / "volumes.json"
        path.mkdir()                               # a directory where the file should be
        tracker = VolumeTracker(DEFS, path=path, bottles=["port_26"])
        tracker.restore({"reservoirs": {"ssct/A1": {"dispatched_ul": 700}}})
        with pytest.raises(ReservoirWriteError,
                           match="the loaded volume of port_26, ssct/A1 was not recorded"):
            tracker.set_loaded({"ssct/A1": 2500, "port_26": 1000})
        assert tracker.loaded("ssct/A1") == 1800 and tracker.remaining("ssct/A1") == 1100
        assert "port_26" not in tracker.tracked
        assert ("port_26", None, 0.0, None, None, False) in tracker.balances()

    def test_the_next_launch_restores_it_where_the_config_carries_it(self, rig, tmp_path):
        """The config file carries the new volume to the next launch: with
        it the file's entry matches and its balance stands; a launch with
        the old figure finds them differ and leaves it undeclared."""
        tracker, pump, _ = rig
        pump.extract(EXTRACT_PORT, 500)
        tracker.set_loaded({"port_25": 6000})
        with closing(VolumeTracker.load(tmp_path / "volumes.json", tracker.snapshot()["definitions"])) as again:
            assert again.declared and again.remaining("port_25") == 5500
        with closing(VolumeTracker.load(tmp_path / "volumes.json", DEFS)) as stale:
            assert stale.undeclared == ["port_25"]

    def test_a_volume_tracker_with_no_file_keeps_it_in_memory(self):
        tracker = VolumeTracker({}, bottles=["port_26"])
        tracker.set_loaded({"port_26": 1000})
        assert tracker.tracked == ("port_26",) and tracker.remaining("port_26") == 1000


def test_rows_are_the_tracked_part_of_balances_in_its_order():
    """The report's and the gate's rows are the window's, filtered: the
    tracked reservoirs with something given, bottles by port number (port_3
    before port_25) and wells column-major (ssct/B1 before ssct/A2), not a
    sort of the keys as strings."""
    defs = {key: {"loaded_ul": 1000} for key in ("port_3", "port_25", "ssct/A2", "ssct/B1")}
    tracker = VolumeTracker(defs)
    tracker.restore({"reservoirs": {key: {"dispatched_ul": 100} for key in defs}})
    expected = [(key, accumulated, current)
                for key, _, accumulated, current, loaded, _ in tracker.balances()
                if loaded is not None and accumulated]
    assert tracker.rows() == expected
    assert [key for key, *_ in tracker.rows()] == ["port_3", "port_25", "ssct/B1", "ssct/A2"]


def test_tracked_names_every_declared_key_in_order():
    """What the system asks before the gate: a declared volume is a
    limit, and none declared, there is nothing to refuse or declare."""
    assert VolumeTracker(DEFS).tracked == ("port_25", "ssct/A1", "ssct/A2")
    assert VolumeTracker({}).tracked == ()


def test_snapshot_round_trips_through_restore():
    tracker = VolumeTracker(DEFS)
    tracker.restore({"definitions": DEFS, "reservoirs": {"ssct/A1": {"dispatched_ul": 700}},
                     "undeclared": ["port_25"]})
    assert tracker.remaining("ssct/A1") == 1100
    assert tracker.undeclared == ["port_25"]
    assert tracker.snapshot()["reservoirs"]["ssct/A1"]["dispatched_ul"] == 700


def test_the_undeclared_refusal_names_a_wholly_undeclared_plate_once():
    """A first run has every well undeclared (193 entries on the sample
    rig): a plate none of whose wells is declared is named once, a plate
    partly declared by its undeclared wells, a bottle by its key."""
    defs = dict(DEFS, **{"probes/A1": {"loaded_ul": 110}, "probes/A2": {"loaded_ul": 110}})
    tracker = VolumeTracker(defs)
    tracker.restore({"undeclared": ["port_25", "probes/A1", "probes/A2", "ssct/A2"]})
    with pytest.raises(ReservoirUndeclared) as refused:
        tracker.check("ssct/A1", 100)
    assert str(refused.value) == (
        "volume tracking is not declared for port_25, every well of probes, ssct/A2: "
        "reset them (Reservoirs… on the Run Experiments tab, or --refilled) first")


def test_describe_keys_keeps_a_label_to_a_line_or_two():
    """The undeclared notice on a rig of 192 wells: every well of a plate as
    one phrase, more than four of them by their count, a bottle and the few
    wells of a plate by key."""
    wells = [f"probes/{well}" for well in ("A1", "B1", "C1", "D1", "E1", "F1")]
    defined = dict(DEFS, **{key: {"loaded_ul": 110} for key in wells})
    assert describe_keys(["port_25", *wells], defined) == "port_25, every well of probes"
    assert describe_keys(wells[:5], defined) == "5 wells of probes"
    assert describe_keys(wells[:4] + ["ssct/A2"], defined) == \
        "probes/A1, probes/B1, probes/C1, probes/D1, ssct/A2"


def test_bottle_keys_are_the_plumbed_ports_but_the_sippers(plate_sipper_config, flow_cell_config):
    """What the Reservoirs window lists before any draw: every port with a
    tubing volume, the sipper's left out (its draws are its wells'), and a
    port whose tubing volume is 0 left out as unplumbed."""
    from fluidics.reservoirs import bottle_keys
    assert bottle_keys(plate_sipper_config) == ["port_24", "port_25", "port_26", "port_27", "port_28"]
    assert bottle_keys(flow_cell_config) == [f"port_{n}" for n in range(1, 29)]
    plate_sipper_config.reagent_selection.selector_valves.tubing_fluid_amount_ul["port_28"] = 0
    assert "port_28" not in bottle_keys(plate_sipper_config)
