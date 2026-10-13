"""ReservoirLedger: authorizes the pump's draws, persists first, never guesses."""
import json
import re

import pytest

from fluidics.errors import (ReservoirDrawTooLarge, ReservoirError, ReservoirInUse, ReservoirNoWell,
                             ReservoirOverdraw, ReservoirUndeclared, ReservoirWriteError)
from fluidics.reservoirs import ReservoirLedger

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


class TestCharging:
    def test_a_bulk_draw_charges_the_open_ports_bottle(self, rig):
        ledger, pump, _ = rig
        pump.extract(EXTRACT_PORT, 500)
        assert ledger.remaining("port_25") == 5000 - 200 - 500
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

    def test_untracked_bottles_and_other_syringe_ports_are_ignored(self, rig):
        ledger, pump, state = rig
        state["port"] = 26
        pump.extract(EXTRACT_PORT, 500)
        pump.extract(1, 500)
        assert ledger.rows() == []
        assert len(pump.started) == 2

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
        assert ledger.rows() == []
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
        assert pump.started == []

    def test_a_matching_file_restores(self, rig, tmp_path):
        ledger, pump, _ = rig
        pump.extract(EXTRACT_PORT, 500)
        again = ReservoirLedger.load(tmp_path / "ledger.json", DEFS)
        try:
            assert again.declared and again.remaining("port_25") == 4300
        finally:
            again.close()

    def test_a_changed_entry_is_undeclared_and_the_others_keep_their_balances(self, rig, tmp_path):
        """Review focus 3: an edited loaded volume must not resurrect a
        reservoir; the entries that did not change are not punished."""
        ledger, pump, state = rig
        pump.extract(EXTRACT_PORT, 500)
        state["port"], state["well"] = SIPPER_PORT, "ssct/A1"
        pump.extract(EXTRACT_PORT, 300)
        changed = dict(DEFS, port_25={"loaded_ul": 6000, "residual_ul": 200},
                       **{"ssct/A3": {"loaded_ul": 1800, "residual_ul": 50}})
        again = ReservoirLedger.load(tmp_path / "ledger.json", changed)
        try:
            assert again.undeclared == ["port_25", "ssct/A3"]
            assert again.remaining("ssct/A1") == 1450, "an unchanged entry kept its balance"
            with pytest.raises(ReservoirUndeclared, match="port_25"):
                again.check("ssct/A1", 100)
            again.refill("port_25")
            again.refill("ssct/A3")
            assert again.declared and again.remaining("port_25") == 5800
        finally:
            again.close()

    def test_a_missing_or_corrupt_file_leaves_everything_undeclared(self, tmp_path):
        ledger = ReservoirLedger.load(tmp_path / "none.json", DEFS)
        try:
            assert ledger.undeclared == sorted(DEFS)
            ledger.refill("all")
            assert ledger.declared
        finally:
            ledger.close()
        (tmp_path / "bad.json").write_text("{not json")
        ledger = ReservoirLedger.load(tmp_path / "bad.json", DEFS)
        try:
            assert not ledger.declared
        finally:
            ledger.close()

    def test_a_second_process_cannot_hold_the_same_ledger(self, tmp_path):
        first = ReservoirLedger.load(tmp_path / "ledger.json", DEFS)
        try:
            with pytest.raises(ReservoirInUse, match="in use"):
                ReservoirLedger.load(tmp_path / "ledger.json", DEFS)
        finally:
            first.close()
        second = ReservoirLedger.load(tmp_path / "ledger.json", DEFS)
        second.close()

    def test_refilling_a_reservoir_name_fills_every_well_of_it(self, rig):
        ledger, pump, state = rig
        state["port"], state["well"] = SIPPER_PORT, "ssct/A1"
        pump.extract(EXTRACT_PORT, 1000)
        ledger.refill("ssct")
        assert ledger.remaining("ssct/A1") == 1750

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
        ledger = ReservoirLedger.load(path, DEFS)
        try:
            assert ledger.undeclared == sorted(DEFS)
        finally:
            ledger.close()

    def test_an_unexpected_failure_inside_load_releases_the_lock(self, tmp_path, monkeypatch):
        def fail(*args):
            raise RuntimeError("unexpected")

        with monkeypatch.context() as m:
            m.setattr(ReservoirLedger, "_reconcile", fail)
            with pytest.raises(RuntimeError, match="unexpected") as failure:
                ReservoirLedger.load(tmp_path / "ledger.json", DEFS)
        # `failure` keeps the traceback, and the half-built ledger in it, alive:
        # the lock must have been released by load, not by the garbage collector.
        again = ReservoirLedger.load(tmp_path / "ledger.json", DEFS)
        again.close()
        assert failure.value is not None

    @pytest.mark.parametrize("balance", ["NaN", "-500", "5001"],
                             ids=["nan", "negative", "above-loaded"])
    def test_a_balance_no_draw_could_have_left_is_undeclared(self, tmp_path, balance):
        """NaN passes every overdraw check (a volume > nan is False); a negative
        balance hands out liquid that was never loaded."""
        path = tmp_path / "ledger.json"
        path.write_text('{"definitions": %s, "reservoirs": {"port_25": {"dispatched_ul": %s}}, '
                        '"undeclared": []}' % (json.dumps(DEFS), balance))
        ledger = ReservoirLedger.load(path, DEFS)
        try:
            assert ledger.undeclared == ["port_25"]
            pump = FakePump()
            ledger.attach(pump, SIPPER_PORT, EXTRACT_PORT, lambda: 25, lambda: None)
            with pytest.raises(ReservoirUndeclared, match="port_25"):
                pump.extract(EXTRACT_PORT, 100)
            assert pump.started == []
        finally:
            ledger.close()


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
        "declare refills (Plate reloaded / Bottle refilled, or --refilled) first")
