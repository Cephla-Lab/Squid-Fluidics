# tests/integration/test_run_session.py
"""RunSession: the one job on the rig, against the simulated devices.

Real threads throughout -- the object's job is to own one -- and the real
clock where a job has to be caught in flight.
"""

import threading

import pytest

from fluidics.devices import build_operations
from fluidics.events import RunEnded, RunStarted, SequenceStarted
from fluidics.manual_operations import ManualOperations
from fluidics.run_session import RunSession

from ..conftest import hears, wait_until
from .conftest import FLOW_CELL_STEP

INCUBATING = {**FLOW_CELL_STEP, "incubation_time": 60}     # minutes: a run to catch mid-way


@pytest.fixture
def devices(flow_cell_config, instant_devices):
    return instant_devices(flow_cell_config)


@pytest.fixture
def session(devices):
    return RunSession(devices)


@pytest.fixture
def ops(flow_cell_config, devices):
    return build_operations(flow_cell_config, devices)


@pytest.fixture
def seen(session):
    """Every state the session announced, in order."""
    seen = []
    session.state.subscribe(seen.append)
    return seen


@pytest.fixture
def manual(devices):
    return ManualOperations(devices)


class TestARun:
    def test_it_runs_on_another_thread_and_the_rig_is_free_after(
            self, devices, session, ops, seen, real_clock):
        during = hears(session.events, SequenceStarted,
                       key=lambda event: (threading.current_thread(),
                                          session.busy, session.kind))
        session.start([FLOW_CELL_STEP], ops)
        assert session.wait(5)
        assert during, "the run never reported"
        assert all(t is not threading.main_thread() for t, _, _ in during)
        assert all(busy and kind == "run" for _, busy, kind in during)
        assert not session.busy and session.kind is None
        assert seen == ["run", None]
        assert devices.syringe_pump.executed, "nothing moved"

    def test_run_ended_arrives_with_the_rig_free_and_the_signal_clean(
            self, session, ops, seen, real_clock):
        """A GUI paints its dialogs on RunEnded; it must see an idle rig
        and a signal the next job can use. The state(None) notification
        follows the terminal event -- that ordering is what stops a chained
        start from sliding in front of it."""
        at_end = hears(session.events, RunEnded,
                       key=lambda event: (event.outcome, session.busy,
                                          session.cancelled, list(seen)))
        session.start([FLOW_CELL_STEP], ops)
        session.abort()
        assert session.wait(5)
        assert wait_until(lambda: at_end != [])
        assert at_end[0][1:] == (False, False, ["run"])
        assert seen == ["run", None]

    def test_a_chained_start_cannot_precede_the_old_runs_ending(
            self, session, ops, real_clock):
        """A state(None) subscriber may start the next job synchronously;
        the old run's RunEnded must already be out, or the new RunStarted
        pairs with the old ending."""
        history = []
        session.events.subscribe(
            lambda event: history.append(type(event).__name__))
        chained = []

        def chain(kind):
            if kind is None and not chained:
                chained.append(True)
                session.start([FLOW_CELL_STEP], ops)

        session.state.subscribe(chain)
        session.start([FLOW_CELL_STEP], ops)
        assert session.wait(5)
        assert wait_until(lambda: history.count("RunEnded") == 2)
        first_end = history.index("RunEnded")
        second_start = history.index("RunStarted", 1)
        assert first_end < second_start, history

    def test_a_launch_that_fails_still_ends_the_run_it_announced(
            self, session, ops, thread_cannot_start):
        """RunStarted goes out from the worker's constructor; a thread that
        will not start must not leave it unpaired."""
        history = []
        session.events.subscribe(lambda event: history.append(event))
        with pytest.raises(RuntimeError, match="can't start"):
            session.start([FLOW_CELL_STEP], ops)
        kinds = [type(event).__name__ for event in history]
        assert kinds == ["RunStarted", "RunEnded"]
        assert history[1].outcome == "failed"
        assert not session.busy

    def test_a_runs_early_end_is_reported_after_the_rig_is_free(self, session, ops, real_clock):
        """As a manual move's report is: the worker records stop or error
        from inside run(), but RunEnded lands with the rig already idle."""
        ended = hears(session.events, RunEnded,
                      key=lambda event: (event.outcome, event.message,
                                         session.busy, session.cancelled))
        session.start([INCUBATING], ops)
        assert not session.wait(0.02)
        session.abort()
        assert session.wait(5)
        assert wait_until(lambda: ended != [])
        assert ended == [("stopped", None, False, False)]

        bad_port = {**FLOW_CELL_STEP, "fluidic_port": 99}
        session.start([bad_port], ops)
        assert session.wait(5)
        assert wait_until(lambda: len(ended) == 2)
        outcome, message, busy, _ = ended[1]
        assert (outcome, busy) == ("failed", False) and "99" in message

    def test_abort_stops_a_run_mid_incubation_and_wait_returns(self, session, ops, real_clock):
        ended = hears(session.events, RunEnded, key=lambda event: event.outcome)
        session.start([INCUBATING], ops)
        assert not session.wait(0.02), "an hour's incubation ended in 20 ms"
        assert session.abort() is True
        assert session.wait(5)
        assert wait_until(lambda: ended == ["stopped"])
        assert not session.busy

    def test_a_second_job_while_a_run_is_going_is_refused(self, session, ops, seen, real_clock):
        session.start([INCUBATING], ops)
        with pytest.raises(RuntimeError, match="busy: a run"):
            session.start([FLOW_CELL_STEP], ops)
        with pytest.raises(RuntimeError, match="busy: a run"):
            session.run_manual(lambda: None)
        session.abort()
        assert session.wait(5)
        assert seen == ["run", None], "the refused jobs left a trace"

    def test_a_thread_that_will_not_start_leaves_the_rig_free(
            self, session, ops, seen, thread_cannot_start):
        with pytest.raises(RuntimeError, match="can't start"):
            session.start([FLOW_CELL_STEP], ops)
        assert not session.busy
        assert seen == ["run", None]
        assert session.wait(0) is True

    def test_pause_and_resume_reach_the_run(self, session, ops, real_clock):
        session.start([INCUBATING], ops)
        assert session.pause() is True
        assert wait_until(lambda: session.snapshot().at_rest)
        assert session.resume() is True
        assert not session.paused
        session.abort()
        assert session.wait(5)


class TestAManualMove:
    def test_it_runs_off_the_callers_thread_and_reports_done(
            self, devices, session, manual, seen, real_clock):
        during = []

        def move():
            during.append((threading.current_thread(), session.kind))
            manual.extract(2, 300, 500)

        done = threading.Event()
        session.run_manual(move, callbacks={"on_finished": done.set})
        assert done.wait(5) and session.wait(5)
        assert during == [(during[0][0], "manual")]
        assert during[0][0] is not threading.main_thread()
        assert devices.syringe_pump.executed == [[("extract", 2, 300, 40)]]
        assert seen == ["manual", None]

    def test_a_failing_move_reports_the_error_after_freeing_the_rig(self, session, real_clock):
        reported = []

        def broken():
            raise IOError("no reply from pump")

        session.run_manual(broken, callbacks={
            "on_error": lambda m: reported.append((m, session.busy)),
            "on_finished": lambda: reported.append("finished")})
        assert session.wait(5)
        assert reported == [("no reply from pump", False), "finished"]

    @pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
    def test_a_move_that_dies_of_anything_still_frees_the_rig(self, session, real_clock):
        """Not an Exception -- the thread dies with it -- and the session must
        not stay busy for good."""
        def dies():
            raise SystemExit("gone")

        session.run_manual(dies)
        assert session.wait(5)
        assert not session.busy

    def test_stop_ends_a_move_through_on_stopped_not_on_error(
            self, devices, session, manual, real_clock):
        """The operator pressed Stop: not a failure, and the next move must
        not raise on the cancel that stopped this one."""
        devices.syringe_pump.ESTIMATE_SECONDS = 60
        reports = []
        callbacks = {"on_finished": lambda: reports.append("finished"),
                     "on_error": lambda m: reports.append(("error", m)),
                     "on_stopped": lambda: reports.append("stopped")}
        session.run_manual(lambda: manual.extract(2, 300, 500), callbacks)
        assert wait_until(lambda: devices.syringe_pump.moving)
        session.abort()
        assert session.wait(5)
        assert reports == ["stopped", "finished"]
        assert not session.cancelled, "the stop was not reset for the next job"
        devices.syringe_pump.ESTIMATE_SECONDS = 0
        session.run_manual(manual.empty_to_waste, callbacks)
        assert session.wait(5)
        assert reports == ["stopped", "finished", "finished"]

    def test_a_run_while_a_move_is_going_is_refused(self, session, ops, real_clock):
        release = threading.Event()
        session.run_manual(release.wait)
        with pytest.raises(RuntimeError, match="busy: a manual"):
            session.start([FLOW_CELL_STEP], ops)
        release.set()
        assert session.wait(5)


class TestTheEndIsOneTransition:
    """Nothing from another thread may start between the rig being marked
    free, the signal being reset, the waiters woken and the subscribers
    told: the ending job's tail would land on the new one."""

    def test_a_start_from_another_thread_waits_for_the_whole_end(
            self, session, ops, seen, real_clock):
        release = threading.Event()
        racer_started = threading.Event()
        during_end = []

        def hound(kind):
            # From inside the ending job's notification: another thread tries
            # to start now. It must not get in before this notification is
            # over -- and must get in afterwards.
            if kind is None and not during_end:
                threading.Thread(target=lambda: (session.start([FLOW_CELL_STEP], ops),
                                                 racer_started.set()), daemon=True).start()
                during_end.append(racer_started.wait(0.1))

        session.state.subscribe(hound)
        session.run_manual(release.wait)
        session.abort()                 # a cancel the new job must not inherit
        release.set()
        assert racer_started.wait(5), "the racer never got its start"
        assert session.wait(5)
        assert during_end == [False], "a start got in half-way through the end"
        assert seen == ["manual", None, "run", None]
        assert not session.cancelled

    def test_a_subscriber_may_start_the_next_job_from_the_end_of_this_one(
            self, session, seen, real_clock):
        """The headless pattern: run the next thing when this ends. Same
        thread, so it goes straight through, and in order."""
        release, release_next = threading.Event(), threading.Event()
        chained = []

        def chain(kind):
            if kind is None and not chained:
                chained.append(True)
                session.run_manual(release_next.wait)

        session.state.subscribe(chain)
        session.run_manual(release.wait)
        session.abort()
        release.set()
        assert wait_until(lambda: seen == ["manual", None, "manual"], timeout=5), seen
        assert not session.wait(0.05), "wait() returned while the chained job still ran"
        assert not session.cancelled
        release_next.set()
        assert session.wait(5)
        assert seen == ["manual", None, "manual", None]

    def test_the_ending_job_never_marks_the_chained_one_done(self, session, real_clock):
        """Done means no job is current: the end of the job that chained
        this one must not mark the session done behind its back. Watched
        from inside the chained job, where a join cannot mask it."""
        release = threading.Event()
        saw_done = []

        def chained_body():
            # Poll for a while: the wrong order would set it a moment after
            # this job started.
            saw_done.append(wait_until(lambda: session.wait(0), timeout=0.2))

        def chain(kind):
            if kind is None and not saw_done and not session.busy:
                session.run_manual(chained_body)

        session.state.subscribe(chain)
        session.run_manual(release.wait)
        release.set()
        assert session.wait(5)
        assert saw_done == [False], "the chained job read the session as done while it ran"


class TestTheClock:
    def test_it_restarts_with_each_job(self, session, real_clock):
        release = threading.Event()
        session.run_manual(release.wait)
        assert wait_until(lambda: session.elapsed_seconds > 0.02)
        release.set()
        assert session.wait(5)
        first = session.elapsed_seconds
        release2 = threading.Event()
        session.run_manual(release2.wait)
        assert session.elapsed_seconds < first, "the new job inherited the old clock"
        release2.set()
        assert session.wait(5)


class TestIdle:
    """Controls with no job leave the run's signal alone: a cancel or a pause
    set now would land on the next job."""

    def test_abort_pause_and_resume_do_nothing_and_say_so(self, session):
        assert session.abort() is False
        assert session.pause() is False
        assert session.resume() is False
        assert not session.cancelled and not session.paused

    def test_the_next_job_runs(self, devices, session, ops, real_clock):
        session.abort()
        session.pause()
        session.start([FLOW_CELL_STEP], ops)
        assert session.wait(5)
        assert devices.syringe_pump.executed, "the idle abort or pause reached the next job"


class TestWait:
    def test_an_idle_session_is_waited_for_at_once(self, session):
        assert session.wait(0) is True

    def test_wait_reports_a_timeout_rather_than_returning_early(self, session, real_clock):
        release = threading.Event()
        session.run_manual(release.wait)
        assert session.wait(0.02) is False
        release.set()
        assert session.wait(5) is True

    def test_wait_from_inside_a_manual_moves_on_finished_does_not_raise(
            self, session, real_clock):
        """on_finished runs after the end's one transition, so a callback
        that wants to know the job is over may ask; it cannot join the
        job's own thread, and is not asked to. (An events subscriber may
        NOT wait -- reports land inside the transition, before the waiters
        are woken.)"""
        waited = []
        session.run_manual(lambda: None,
                           callbacks={"on_finished": lambda: waited.append(session.wait(1))})
        assert session.wait(5)
        assert wait_until(lambda: waited == [True])


class TestRunIds:
    def test_two_runs_in_one_second_get_distinct_ids(self, session, ops):
        """The counter, not the clock, is what keeps back-to-back ids
        apart -- under the suite's fake clock both stamps share a second."""
        ids = hears(session.events, RunStarted, key=lambda event: event.run_id)
        session.start([FLOW_CELL_STEP], ops)
        assert session.wait()
        session.start([FLOW_CELL_STEP], ops)
        assert session.wait()
        assert len(ids) == 2 and len(set(ids)) == 2, ids


class TestSnapshot:
    def test_the_snapshot_names_the_job_and_ends_with_it(self, session):
        """One call carries what a display draws: the job's kind and the
        control's state. Idle before, named during, idle again after."""
        assert session.snapshot().kind is None
        gate = threading.Event()
        session.run_manual(gate.wait)
        assert wait_until(lambda: session.snapshot().kind == "manual")
        gate.set()
        assert session.wait()      # untimed: a timed wait cannot block under the fake clock
        snap = session.snapshot()
        assert snap.kind is None and not snap.paused and not snap.cancelled

    def test_kind_and_control_are_read_under_one_lock(self, session):
        """A job must not start or finish between the two reads -- the
        control is read with the session's lock still held, or the snapshot
        can pair one job's kind with another's flags."""
        held = []
        original = session.control.snapshot

        def probing():
            # From another thread, a held session lock refuses the acquire.
            def probe():
                if session._lock.acquire(blocking=False):
                    session._lock.release()
                    held.append(False)
                else:
                    held.append(True)
            prober = threading.Thread(target=probe)
            prober.start()
            prober.join()
            return original()

        session.control.snapshot = probing
        session.snapshot()
        assert held == [True], "the session lock was dropped before the control read"
