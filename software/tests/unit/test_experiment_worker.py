# tests/unit/test_experiment_worker.py
"""The run loop every experiment goes through, pinned by its callbacks.

Deliberately written against the observable contract -- which callbacks fire,
in what order, with what payloads -- rather than the abort mechanics, so the
planned cancellation redesign can rewrite the internals against these tests
instead of rewriting the tests.
"""

from types import SimpleNamespace

from fluidics.errors import AbortRequested, RunControl, SafetyFault
from fluidics.experiment_worker import ExperimentWorker

from ..worker_helpers import errors_in, record_run


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


def run_worker(sequences, ops=None, run_control=None):
    """RecordingOps over the stub config; returns (ops, events) -- the
    event shapes are record_run's."""
    ops = ops or RecordingOps()
    return ops, record_run(ops, sequences, CONFIG, run_control=run_control)


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
        # Tagged the way the progress narrative tags them: the second
        # sequence is "Sequence 2/3", and the label is named.
        assert "Sequence 2/3" in errors[0]
        assert "flow_reagent" in errors[0]
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

    def test_an_abort_from_the_operations_reports_a_stop_and_finishes(self):
        """A stop is what the operator asked for: reported as one, never as
        an error -- and finished still fires."""
        ops = RecordingOps(raise_on={0: AbortRequested()})
        _, events = run_worker([dict(FLOW), dict(FLOW)], ops=ops)
        assert errors_in(events) == []
        assert events.count(("stopped",)) == 1
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


class TestRunNarrative:
    """The worker logs its own run -- one source feeding the console, the run
    log, and any UI -- so the record exists even when nothing is watching.
    The CLI renders nothing itself: its console output is these lines."""

    def test_a_run_narrates_start_completion_and_finish(self, caplog):
        with caplog.at_level("INFO", logger="fluidics.experiment_worker"):
            run_worker([dict(FLOW, name="wash")])
        text = caplog.text
        assert "Run of 1 sequence(s)" in text
        assert "Sequence 1/1 (wash): started" in text
        assert "Sequence 1/1 (wash): completed" in text
        assert "Run finished." in text

    def test_a_failure_is_logged_as_an_error(self, caplog):
        ops = RecordingOps(raise_on={0: ValueError("pump went away")})
        with caplog.at_level("INFO", logger="fluidics.experiment_worker"):
            run_worker([dict(FLOW)], ops=ops)
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 1
        assert "pump went away" in errors[0].getMessage()
        assert "Run finished." in caplog.text


class TestTheSharedSignal:
    """The worker waits and checks on the run's RunControl -- the same object
    the devices share -- so one cancel reaches the operation, the incubation
    wait, and the check between sequences alike; and every early end makes
    the rig safe before it is reported."""

    def test_a_cancel_during_incubation_reports_aborted_after_making_safe(self, cancel_during_wait):
        control = RunControl()
        cancel_during_wait(control)
        _, events = run_worker([dict(FLOW, incubation_time=30)], run_control=control)
        assert ("progress", 1, "Incubating") in events
        assert ("progress", 1, "Completed") not in events
        assert errors_in(events) == []
        assert events.index(("make_safe",)) < events.index(("stopped",))
        assert events[-1] == ("finished",)


    def test_a_cancel_landing_between_sequences_does_not_start_the_next(self):
        """After the post-operation check and before the next dispatch is a
        real window: the operator's abort can land while the worker reports
        'Completed'. The next sequence must not even be reported as started."""
        control = RunControl()
        events = []

        def on_progress(index, num, status):
            events.append(("progress", num, status))
            if (num, status) == (1, "Completed"):
                control.cancel()

        worker = ExperimentWorker(RecordingOps(), [dict(FLOW), dict(FLOW)], CONFIG, callbacks={
            "update_progress": on_progress,
            "on_stopped": lambda: events.append(("stopped",)),
            "on_error": lambda message: events.append(("error", message)),
        }, run_control=control)
        worker.run()
        assert ("progress", 2, "Started") not in events
        assert ("stopped",) in events and errors_in(events) == []

    def test_a_safety_fault_is_reported_with_its_diagnosis_not_as_an_abort(self):
        """The instrument stopped itself. The operator must read what the
        sensor saw, tagged with the sequence, never 'aborted by user'."""
        _, events = run_worker([FLOW], ops=RecordingOps(raise_on={0: SafetyFault("flow collapsed on 'inlet'")}))
        (error,) = errors_in(events)
        assert "Sequence 1/1" in error and "flow collapsed on 'inlet'" in error
        assert "abort" not in error.lower()
        assert "failed on repeat" not in error      # a fault, not a failed step
        assert events.index(("make_safe",)) < events.index(("error", error))

    def test_a_sequence_cancelled_in_its_tail_is_not_reported_completed(self):
        """Every wait inside an operation raises, so this is the residual
        window: a cancel landing after the operation's last wait and before it
        returns. The operator pressed Abort; the run must not say Completed."""
        control = RunControl()

        class CancelsOnTheWayOut(RecordingOps):
            def process_sequence(self, seq):
                super().process_sequence(seq)
                control.cancel()

        _, events = run_worker([dict(FLOW), dict(FLOW)], ops=CancelsOnTheWayOut(),
                               run_control=control)
        assert ("progress", 1, "Completed") not in events
        assert ("progress", 2, "Started") not in events
        assert ("stopped",) in events and errors_in(events) == []

    def test_a_completed_run_leaves_the_rig_alone(self):
        """The temperature must keep holding: a run whose last step is
        set_temperature exists to leave the sample at it."""
        _, events = run_worker([FLOW])
        assert ("make_safe",) not in events

    def test_a_failed_step_makes_the_rig_safe_before_reporting(self):
        """A failure switches the TEC off like an abort: an abort has the
        operator standing at the rig; a failure is the one nobody is present
        for."""
        _, events = run_worker([FLOW], ops=RecordingOps(raise_on={0: ValueError("bad step")}))
        (error,) = errors_in(events)
        assert "bad step" in error
        assert events.index(("make_safe",)) < events.index(("error", error))

    def test_a_fault_outside_the_sequence_loop_makes_the_rig_safe_too(self):
        """The outer except catches faults raised where no sequence tag exists
        yet -- before any operation, so nothing has moved. The run has still
        ended, so the rig is still quieted."""
        events = []
        worker = ExperimentWorker(RecordingOps(), [FLOW], CONFIG, callbacks={
            "make_safe": lambda: events.append("make_safe"),
            "on_error": lambda message: events.append(("error", message)),
        })
        # Past the estimate in __init__; fails on the run's own pass.
        worker.sequences = [None]
        worker.run()
        assert events[0] == "make_safe"
        assert events[1][0] == "error"

    def test_what_make_safe_could_not_switch_off_reaches_the_report(self):
        """After an abort, "the rig could not be made safe" is the line that
        matters; the ERROR in the log alone does not reach the dialog."""
        errors = []
        worker = ExperimentWorker(RecordingOps(raise_on={0: ValueError("bad step")}), [FLOW], CONFIG, callbacks={
            "make_safe": lambda: [IOError("TEC channel 1 not answering")],
            "on_error": errors.append,
        })
        worker.run()
        (error,) = errors
        assert "bad step" in error
        assert "TEC channel 1 not answering" in error



class TestPause:
    """The worker holds between sequences and stops the incubation clock."""

    def test_incubation_is_spent_in_running_time(self):
        """So a pause stops the clock -- that behaviour is pinned on delay()
        itself; this pins that the incubation goes through it."""
        control = RunControl()
        asked = []
        control.delay = lambda seconds: asked.append(seconds)
        ExperimentWorker(RecordingOps(), [FLOW], CONFIG,
                         run_control=control).wait_for_incubation(30)
        assert asked == [1800]

    def test_it_holds_between_sequences_and_says_so(self):
        control = RunControl()
        events = []

        class PauseAfterFirst(RecordingOps):
            def process_sequence(self, seq):
                super().process_sequence(seq)
                if len(self.processed) == 1:
                    control.pause()

        def on_progress(index, num, status):
            events.append((num, status))
            if status == "Paused":
                control.resume()      # the operator, once the run has parked

        ops = PauseAfterFirst()
        worker = ExperimentWorker(ops, [dict(FLOW), dict(FLOW)], CONFIG, callbacks={
            "update_progress": on_progress,
        }, run_control=control)
        worker.run()
        assert (2, "Paused") in events, events
        assert events.index((2, "Paused")) < events.index((2, "Started"))
        assert len(ops.processed) == 2, "the run did not carry on after the resume"

    def test_an_early_end_lifts_a_pending_pause(self):
        """A failure while a pause is pending must not unwind behind a shut
        gate: make_safe and the report run with nothing able to park."""
        control = RunControl()

        class PausedThenFailing(RecordingOps):
            def process_sequence(self, seq):
                control.pause()                      # the operator, mid-sequence
                raise RuntimeError("pump fault")     # the rig, a moment later

        record_run(PausedThenFailing(), [FLOW], CONFIG, run_control=control)
        assert not control.paused

    def test_a_cancel_beats_a_pending_pause(self):
        """Pause then Abort: the run reports aborted rather than waiting for a
        resume that is never coming."""
        control = RunControl()

        class PauseThenAbort(RecordingOps):
            def process_sequence(self, seq):
                super().process_sequence(seq)
                control.pause()
                control.cancel()      # Abort, while the run is held

        ops = PauseThenAbort()
        _, events = run_worker([dict(FLOW), dict(FLOW)], ops=ops, run_control=control)
        assert len(ops.processed) == 1
        assert ("stopped",) in events and errors_in(events) == []
