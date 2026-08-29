# tests/unit/test_experiment_worker.py
"""The run loop every experiment goes through, pinned by what it publishes.

Deliberately written against the observable contract -- which events fire,
in what order, and how the worker records its ending -- rather than the
abort mechanics, so internals can be rewritten against these tests.
"""

from types import SimpleNamespace

from fluidics.errors import AbortRequested, RunControl, SafetyFault
from fluidics.events import RunStarted, SequenceCompleted
from fluidics.experiment_worker import ExperimentWorker
from fluidics.subscribers import Subscribers

from ..conftest import wait_until
from ..worker_helpers import plan_for, record_run


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
    """RecordingOps over the stub config; returns (ops, worker, events) --
    the event shapes are record_run's."""
    ops = ops or RecordingOps()
    worker, events = record_run(ops, sequences, CONFIG, run_control=run_control)
    return ops, worker, events


FLOW = {"type": "flow_reagent", "fluidic_port": 1, "flow_rate": 500,
        "volume": 100}


class TestHappyPath:
    def test_each_sequence_starts_and_completes_in_order(self):
        ops, worker, events = run_worker([dict(FLOW, name="a"), dict(FLOW, name="b")])
        assert [s["name"] for s in ops.processed] == ["a", "b"]
        assert events == [
            ("started",),
            ("sequence", 1, "started"), ("sequence", 1, "completed"),
            ("sequence", 2, "started"), ("sequence", 2, "completed"),
        ]
        assert worker.outcome == "finished"
        assert worker.ended_position is None

    def test_repeat_runs_the_sequence_again_and_counts_each_run(self):
        ops, _, events = run_worker([dict(FLOW, repeat=3)])
        assert len(ops.processed) == 3
        completed = [e[1] for e in events if e[2:] == ("completed",)]
        assert completed == [1, 2, 3]

    def test_incubation_reports_between_start_and_completion(self):
        _, _, events = run_worker([dict(FLOW, incubation_time=2)])
        kinds = [e[2] for e in events if e[0] == "sequence"]
        assert kinds == ["started", "incubating", "completed"]


class TestFailurePath:
    def test_an_operation_error_ends_the_run_and_names_the_sequence(self):
        ops = RecordingOps(raise_on={1: ValueError("pump went away")})
        ops, worker, events = run_worker(
            [dict(FLOW), dict(FLOW), dict(FLOW)], ops=ops)
        # The third sequence must not run after the second failed.
        assert len(ops.processed) == 2
        assert worker.outcome == "failed"
        # Tagged the way the narrative tags them, and the label is named.
        assert "Sequence 2/3" in worker.message
        assert "flow_reagent" in worker.message
        assert "pump went away" in worker.message
        assert worker.ended_position == 1
        assert ("sequence", 2, "completed") not in events

    def test_an_abort_from_the_operations_records_a_stop(self):
        """A stop is what the operator asked for: recorded as one, with the
        entry that was in flight, never as an error."""
        ops = RecordingOps(raise_on={0: AbortRequested()})
        _, worker, events = run_worker([dict(FLOW), dict(FLOW)], ops=ops)
        assert worker.outcome == "stopped"
        assert worker.message is None
        assert worker.ended_position == 0


class TestRunStarted:
    def test_the_plan_is_published_before_the_run_as_handed_in(self):
        """Fired from the constructor: the GUI sizes its display before the
        run's thread exists."""
        heard = []
        channel = Subscribers("events")
        channel.subscribe(heard.append)
        plan = plan_for([dict(FLOW, repeat=2), dict(FLOW)])
        ExperimentWorker(RecordingOps(), plan, CONFIG, run_id="run-x",
                         events=channel)
        assert heard == [RunStarted("run-x", plan)]


class TestRunNarrative:
    """The worker logs its own run -- one source feeding the console, the run
    log, and any UI -- so the record exists even when nothing is watching.
    The CLI renders nothing itself: its console output is these lines."""

    def test_a_run_narrates_start_completion_and_finish(self, caplog):
        with caplog.at_level("INFO", logger="fluidics.experiment_worker"):
            run_worker([dict(FLOW, name="wash")])
        text = caplog.text
        assert "1 sequence(s)" in text
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
    the rig safe before it is recorded."""

    def test_a_cancel_during_incubation_stops_after_making_safe(self, cancel_during_wait):
        control = RunControl()
        cancel_during_wait(control)
        _, worker, events = run_worker([dict(FLOW, incubation_time=30)],
                                       run_control=control)
        assert ("sequence", 1, "incubating") in events
        assert ("sequence", 1, "completed") not in events
        assert worker.outcome == "stopped"
        assert ("make_safe",) in events

    def test_a_cancel_landing_between_sequences_does_not_start_the_next(self):
        """After the post-operation check and before the next dispatch is a
        real window: the operator's abort can land while the completion is
        published. The next sequence must not even be reported started."""
        control = RunControl()
        heard = []
        channel = Subscribers("events")

        def cancel_after_first(event):
            heard.append(event)
            if isinstance(event, SequenceCompleted) and event.position == 0:
                control.cancel()

        channel.subscribe(cancel_after_first)
        worker = ExperimentWorker(RecordingOps(), plan_for([dict(FLOW), dict(FLOW)]),
                                  CONFIG, events=channel, run_control=control)
        worker.run()
        assert not any(isinstance(e, SequenceCompleted) and e.position == 1
                       for e in heard)
        assert worker.outcome == "stopped"

    def test_a_safety_fault_is_recorded_with_its_diagnosis_not_as_an_abort(self):
        """The instrument stopped itself. The operator must read what the
        sensor saw, tagged with the sequence, never 'aborted by user'."""
        _, worker, events = run_worker(
            [FLOW], ops=RecordingOps(raise_on={0: SafetyFault("flow collapsed on 'inlet'")}))
        assert worker.outcome == "failed"
        assert "Sequence 1/1" in worker.message
        assert "flow collapsed on 'inlet'" in worker.message
        assert "abort" not in worker.message.lower()
        assert ("make_safe",) in events

    def test_a_sequence_cancelled_in_its_tail_is_not_reported_completed(self):
        """Every wait inside an operation raises, so this is the residual
        window: a cancel landing after the operation's last wait and before it
        returns. The operator pressed Abort; the run must not say completed."""
        control = RunControl()

        class CancelsOnTheWayOut(RecordingOps):
            def process_sequence(self, seq):
                super().process_sequence(seq)
                control.cancel()

        _, worker, events = run_worker([dict(FLOW), dict(FLOW)],
                                       ops=CancelsOnTheWayOut(),
                                       run_control=control)
        assert ("sequence", 1, "completed") not in events
        assert ("sequence", 2, "started") not in events
        assert worker.outcome == "stopped"

    def test_a_completed_run_leaves_the_rig_alone(self):
        """The temperature must keep holding: a run whose last step is
        set_temperature exists to leave the sample at it."""
        _, _, events = run_worker([FLOW])
        assert ("make_safe",) not in events

    def test_a_fault_outside_the_sequence_loop_makes_the_rig_safe_too(self):
        """The outer except catches faults raised where no sequence tag
        exists yet -- before any operation, so nothing has moved. The run
        has still ended, so the rig is still quieted."""
        made_safe = []
        worker = ExperimentWorker(RecordingOps(), plan_for([FLOW]), CONFIG,
                                  make_safe=lambda: made_safe.append(True) or [])
        # Past RunStarted in __init__; fails on the run's own pass.
        worker.plan = (None,)
        worker.run()
        assert made_safe == [True]
        assert worker.outcome == "failed"

    def test_what_make_safe_could_not_switch_off_reaches_the_record(self):
        """After a failure, "the rig could not be made safe" is the line
        that matters; the ERROR in the log alone does not reach the dialog."""
        worker = ExperimentWorker(
            RecordingOps(raise_on={0: ValueError("bad step")}), plan_for([FLOW]),
            CONFIG, make_safe=lambda: [IOError("TEC channel 1 not answering")])
        worker.run()
        assert "bad step" in worker.message
        assert "TEC channel 1 not answering" in worker.message


class TestPause:
    """The worker holds between sequences and stops the incubation clock."""

    def test_incubation_is_spent_in_running_time(self):
        """So a pause stops the clock -- that behaviour is pinned on delay()
        itself; this pins that the incubation goes through it."""
        control = RunControl()
        asked = []
        control.delay = lambda seconds: asked.append(seconds)
        ExperimentWorker(RecordingOps(), plan_for([FLOW]), CONFIG,
                         run_control=control).wait_for_incubation(30)
        assert asked == [1800]

    def test_it_holds_between_sequences(self, real_clock, run_in_background):
        control = RunControl()

        class PauseAfterFirst(RecordingOps):
            def process_sequence(self, seq):
                super().process_sequence(seq)
                if len(self.processed) == 1:
                    control.pause()

        ops = PauseAfterFirst()
        worker = ExperimentWorker(ops, plan_for([dict(FLOW), dict(FLOW)]),
                                  CONFIG, run_control=control)
        finished, error = run_in_background(worker.run)
        assert wait_until(lambda: control.at_rest), "the run never parked"
        assert len(ops.processed) == 1, "the second sequence ran through the hold"
        control.resume()
        assert finished.wait(2) and not error
        assert len(ops.processed) == 2, "the run did not carry on after the resume"

    def test_an_early_end_lifts_a_pending_pause(self):
        """A failure while a pause is pending must not unwind behind a shut
        gate: make_safe and the record land with nothing able to park."""
        control = RunControl()

        class PausedThenFailing(RecordingOps):
            def process_sequence(self, seq):
                control.pause()                      # the operator, mid-sequence
                raise RuntimeError("pump fault")     # the rig, a moment later

        record_run(PausedThenFailing(), [FLOW], CONFIG, run_control=control)
        assert not control.paused

    def test_a_cancel_beats_a_pending_pause(self):
        """Pause then Abort: the run records aborted rather than waiting for
        a resume that is never coming."""
        control = RunControl()

        class PauseThenAbort(RecordingOps):
            def process_sequence(self, seq):
                super().process_sequence(seq)
                control.pause()
                control.cancel()      # Abort, while the run is held

        ops = PauseThenAbort()
        _, worker, _ = run_worker([dict(FLOW), dict(FLOW)], ops=ops,
                                  run_control=control)
        assert len(ops.processed) == 1
        assert worker.outcome == "stopped"
