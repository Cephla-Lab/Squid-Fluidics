# tests/integration/test_run_session.py
"""RunSession: the one job on the rig, against the simulated devices.

Real threads throughout -- the object's job is to own one -- and the real
clock where a job has to be caught in flight.
"""

import threading

import pytest

from fluidics.devices import build_operations
from fluidics.manual_operations import ManualOperations
from fluidics.run_session import RunSession

from ..conftest import wait_until
from .conftest import FLOW_CELL_STEP

INCUBATING = {**FLOW_CELL_STEP, "incubation_time": 60}     # minutes: a run to catch mid-way


@pytest.fixture
def devices(flow_cell_config, instant_devices):
    devices = instant_devices(flow_cell_config)
    # Nothing here reads flow, and closing a simulated sensor joins its
    # publish thread mid-sleep -- 50 ms on the real clock, per test. Done
    # now, under the fake clock, where it is instant.
    for sensor in devices.flow_sensors:
        sensor.close()
    return devices


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
        during = []
        session.start([FLOW_CELL_STEP], ops, callbacks={
            "update_progress": lambda *a: during.append(
                (threading.current_thread(), session.busy, session.kind))})
        assert session.wait(5)
        assert during, "the run never reported"
        assert all(t is not threading.main_thread() for t, _, _ in during)
        assert all(busy and kind == "run" for _, busy, kind in during)
        assert not session.busy and session.kind is None
        assert seen == ["run", None]
        assert devices.syringe_pump.executed, "nothing moved"

    def test_the_rig_is_free_and_the_signal_clean_before_the_callers_on_finished(
            self, session, ops, seen, real_clock):
        """A GUI renders its controls on on_finished; it must see an idle rig
        and a signal the next job can use."""
        at_finish = []
        session.start([FLOW_CELL_STEP], ops, callbacks={"on_finished": lambda: at_finish.append(
            (session.busy, session.cancelled, list(seen)))})
        session.abort()
        assert session.wait(5)
        assert at_finish == [(False, False, ["run", None])]

    def test_a_runs_early_end_is_reported_after_the_rig_is_free(self, session, ops, real_clock):
        """As a manual move's is: the worker says stop or error from inside
        run(), but whoever listens must see an idle rig, the same as they do
        on on_finished."""
        seen = []
        session.start([INCUBATING], ops, callbacks={
            "on_stopped": lambda: seen.append(("stopped", session.busy, session.cancelled))})
        assert not session.wait(0.02)
        session.abort()
        assert session.wait(5)
        assert seen == [("stopped", False, False)]

        bad_port = {**FLOW_CELL_STEP, "fluidic_port": 99}
        session.start([bad_port], ops, callbacks={
            "on_error": lambda m: seen.append(("error", session.busy, "99" in m))})
        assert session.wait(5)
        assert seen[-1] == ("error", False, True)

    def test_abort_stops_a_run_mid_incubation_and_wait_returns(self, session, ops, real_clock):
        reports = []
        session.start([INCUBATING], ops, callbacks={
            "on_stopped": lambda: reports.append("stopped"),
            "on_error": lambda m: reports.append(("error", m)),
            "on_finished": lambda: reports.append("finished")})
        assert not session.wait(0.02), "an hour's incubation ended in 20 ms"
        assert session.abort() is True
        assert session.wait(5)
        assert reports == ["stopped", "finished"]
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
        assert wait_until(lambda: session.at_rest)
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

    def test_wait_from_inside_the_jobs_own_callback_does_not_raise(self, session, ops, real_clock):
        """A callback that wants to know the job is over may ask; it cannot
        join its own thread, and is not asked to."""
        waited = []
        session.start([FLOW_CELL_STEP], ops,
                      callbacks={"on_finished": lambda: waited.append(session.wait(1))})
        assert session.wait(5)
        assert waited == [True]
