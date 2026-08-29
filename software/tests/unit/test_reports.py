# tests/unit/test_reports.py
"""RunReports: the written record of each run -- collected at the boundary
events, written beside the rolling log."""

import json
import logging
import threading
from types import SimpleNamespace

from fluidics.events import RunEnded, RunStarted
from fluidics.reports import RunReports
from fluidics.subscribers import Subscribers

from ..worker_helpers import plan_for

ROWS = [(2, "wash buffer", 300.0), (5, None, 40.0)]


def rig(tmp_path, rows=ROWS):
    """A RunReports on real channels, the ledger canned."""
    events = Subscribers("test run events")
    warnings = Subscribers("test warnings")
    usage = SimpleNamespace(rows=lambda: list(rows))
    reports = RunReports(events, usage, warnings, directory=tmp_path)
    return SimpleNamespace(events=events, warnings=warnings, usage=usage,
                           reports=reports)


def written(tmp_path, reports, run_id):
    assert reports.wait(5), "a report write never finished"
    return json.loads((tmp_path / f"{run_id}.json").read_text())


class TestTheRecord:
    def test_a_finished_run_writes_the_whole_record(self, tmp_path):
        r = rig(tmp_path)
        plan = plan_for([{"type": "priming", "name": "prime"},
                         {"type": "flow_reagent", "name": "bleach",
                          "repeat": 2}], seconds=[10.0, 20.0, 20.0])
        r.events.notify(RunStarted("run-1", plan))
        r.warnings.notify("flow low on syringe_draw")
        r.events.notify(RunEnded("run-1", "finished", None, 47.5, None))
        report = written(tmp_path, r.reports, "run-1")
        assert report["outcome"] == "finished" and report["message"] is None
        assert report["elapsed_seconds"] == 47.5
        assert report["estimated_seconds"] == 50.0
        assert report["ended_at"] is None
        assert [s["row"] for s in report["sequences"]] == [0, 1], \
            "one entry per source sequence, not per repeat"
        assert report["sequences"][1]["name"] == "bleach"
        assert [(e["position"], e["repeat"]) for e in report["plan"]] == \
            [(0, 1), (1, 1), (2, 2)]
        assert report["reagent_used_ul"] == [
            {"port": 2, "reagent": "wash buffer", "volume_ul": 300.0},
            {"port": 5, "reagent": None, "volume_ul": 40.0}]
        assert [w["message"] for w in report["warnings"]] == \
            ["flow low on syringe_draw"]
        assert report["started"] and report["ended"], "wall times belong in a record"

    def test_an_early_end_names_where_it_stopped(self, tmp_path):
        r = rig(tmp_path)
        plan = plan_for([{"type": "priming", "repeat": 3}], seconds=5.0)
        r.events.notify(RunStarted("run-2", plan))
        r.events.notify(RunEnded("run-2", "stopped", None, 8.0, position=1))
        report = written(tmp_path, r.reports, "run-2")
        assert report["ended_at"] == {"position": 1, "row": 0,
                                      "label": "priming", "repeat": 2,
                                      "repeats": 3}

    def test_a_tail_starting_mid_repeat_still_records_its_sequence(self, tmp_path):
        """A resumed run's plan can open at repeat 2 of 3; the source
        sequence belongs in the record all the same."""
        r = rig(tmp_path)
        tail = plan_for([{"type": "priming", "repeat": 3}], seconds=5.0)[1:]
        r.events.notify(RunStarted("run-3", tail))
        r.events.notify(RunEnded("run-3", "finished", None, 10.0, None))
        report = written(tmp_path, r.reports, "run-3")
        assert [s["row"] for s in report["sequences"]] == [0]


class TestTheCollectionMoment:
    def test_totals_are_read_in_the_dispatch_not_at_the_write(self, tmp_path):
        """RunEnded's later subscribers may chain the next run and reset
        the ledger; the report must take its copy before the dispatch
        moves on -- on this thread, exactly once."""
        r = rig(tmp_path)
        calls = []
        r.usage.rows = lambda: calls.append(threading.current_thread()) or []
        r.events.notify(RunStarted("run-4", ()))
        r.events.notify(RunEnded("run-4", "finished", None, 1.0, None))
        assert calls == [threading.current_thread()]
        assert r.reports.wait(5)

    def test_warnings_outside_a_run_stay_out_of_the_record(self, tmp_path):
        r = rig(tmp_path)
        r.warnings.notify("before any run")
        r.events.notify(RunStarted("run-5", ()))
        r.warnings.notify("during")
        r.events.notify(RunEnded("run-5", "finished", None, 1.0, None))
        r.warnings.notify("after")
        report = written(tmp_path, r.reports, "run-5")
        assert [w["message"] for w in report["warnings"]] == ["during"]


class TestTheEdges:
    def test_an_ending_nobody_announced_is_still_recorded(self, tmp_path, caplog):
        r = rig(tmp_path)
        with caplog.at_level(logging.WARNING, logger="fluidics"):
            r.events.notify(RunEnded("run-6", "failed", "pump fault", 0.0, None))
        report = written(tmp_path, r.reports, "run-6")
        assert report["outcome"] == "failed" and report["plan"] == []
        assert "no start on record" in caplog.text

    def test_a_failed_write_is_loud_and_kills_nothing(self, tmp_path, caplog):
        blocked = tmp_path / "blocked"
        blocked.write_text("a file where the directory should be")
        r = rig(blocked)
        r.events.notify(RunStarted("run-7", ()))
        with caplog.at_level(logging.ERROR, logger="fluidics"):
            r.events.notify(RunEnded("run-7", "finished", None, 1.0, None))
            assert r.reports.wait(5)
        assert "could not be written" in caplog.text
