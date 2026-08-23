# tests/unit/test_experiment_worker.py
"""The run loop every experiment goes through, pinned by its callbacks.

Deliberately written against the observable contract -- which callbacks fire,
in what order, with what payloads -- rather than the abort mechanics, so the
planned cancellation redesign can rewrite the internals against these tests
instead of rewriting the tests.
"""

from types import SimpleNamespace

import pytest

from fluidics.experiment_worker import AbortRequested, ExperimentWorker


class RecordingOps:
    def __init__(self, raise_on=None):
        self.processed = []
        self.raise_on = raise_on or {}   # {call_index: exception}

    def process_sequence(self, seq):
        self.processed.append(seq)
        exc = self.raise_on.get(len(self.processed) - 1)
        if exc is not None:
            raise exc


CONFIG = SimpleNamespace(
    reagent_selection=SimpleNamespace(common_tubing_fluid_amount_ul=800))


def run_worker(sequences, ops=None):
    """Construct and run synchronously, returning (ops, events).

    events is the flat callback record: ("estimate", n), ("progress", num,
    status), ("error", message), ("finished",).
    """
    ops = ops or RecordingOps()
    events = []
    worker = ExperimentWorker(ops, sequences, CONFIG, callbacks={
        "on_estimate": lambda t, n: events.append(("estimate", n)),
        "update_progress":
            lambda index, num, status: events.append(("progress", num, status)),
        "on_error": lambda message: events.append(("error", message)),
        "on_finished": lambda: events.append(("finished",)),
    })
    worker.run()
    return ops, events


FLOW = {"type": "flow_reagent", "fluidic_port": 1, "flow_rate": 500,
        "volume": 100}


class TestHappyPath:
    def test_each_sequence_starts_and_completes_in_order(self):
        ops, events = run_worker([dict(FLOW, name="a"), dict(FLOW, name="b")])
        assert [s["name"] for s in ops.processed] == ["a", "b"]
        assert events == [
            ("estimate", 2),
            ("progress", 1, "Started"), ("progress", 1, "Completed"),
            ("progress", 2, "Started"), ("progress", 2, "Completed"),
            ("finished",),
        ]

    def test_repeat_runs_the_sequence_again_and_counts_each_run(self):
        ops, events = run_worker([dict(FLOW, repeat=3)])
        assert len(ops.processed) == 3
        completed = [e[1] for e in events if e[2:] == ("Completed",)]
        assert completed == [1, 2, 3]

    def test_incubation_reports_between_start_and_completion(self):
        _, events = run_worker([dict(FLOW, incubation_time=2)])
        statuses = [e[2] for e in events if e[0] == "progress"]
        assert statuses == ["Started", "Incubating", "Completed"]


class TestFailurePath:
    def test_an_operation_error_stops_the_run_and_names_the_sequence(self):
        ops = RecordingOps(raise_on={1: ValueError("pump went away")})
        ops, events = run_worker(
            [dict(FLOW), dict(FLOW), dict(FLOW)], ops=ops)
        # The third sequence must not run after the second failed.
        assert len(ops.processed) == 2
        errors = [e[1] for e in events if e[0] == "error"]
        assert len(errors) == 1
        # 0-based today ("sequence 1" is the second sequence); if the redesign
        # renumbers the operator-facing message, update this deliberately.
        assert "sequence 1" in errors[0]
        assert "pump went away" in errors[0]
        # A failed run still announces it is over -- the GUI re-enables its
        # buttons in on_finished.
        assert events[-1] == ("finished",)
        assert ("progress", 2, "Completed") not in events

    def test_a_repeat_failure_names_the_repeat(self):
        ops = RecordingOps(raise_on={1: ValueError("boom")})
        _, events = run_worker([dict(FLOW, repeat=3)], ops=ops)
        errors = [e[1] for e in events if e[0] == "error"]
        assert "repeat 2" in errors[0]

    def test_an_abort_from_the_operations_reports_and_finishes(self):
        ops = RecordingOps(raise_on={0: AbortRequested()})
        _, events = run_worker([dict(FLOW), dict(FLOW)], ops=ops)
        errors = [e[1] for e in events if e[0] == "error"]
        # Substring, not the exact wording: the contract is one error
        # callback that says aborted, and finished still firing.
        assert len(errors) == 1
        assert "aborted" in errors[0]
        assert events[-1] == ("finished",)


class TestEstimate:
    def test_the_estimate_arrives_before_run_and_counts_repeats(self):
        ops = RecordingOps()
        events = []
        ExperimentWorker(ops, [dict(FLOW, repeat=2), dict(FLOW)], CONFIG,
                         callbacks={
                             "on_estimate":
                                 lambda t, n: events.append((t, n)),
                         })
        # Fired from the constructor: the GUI sizes its progress bar before
        # the run starts.
        assert len(events) == 1
        seconds, n = events[0]
        assert n == 3
        assert seconds > 0
