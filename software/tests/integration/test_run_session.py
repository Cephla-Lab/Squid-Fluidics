# tests/integration/test_run_session.py
"""RunSession: the one job on the rig, against the simulated devices.

Real threads throughout -- the object's job is to own one -- and the real
clock where a job has to be caught in flight.
"""

import threading

import pytest

from fluidics.devices import build_operations
from fluidics.errors import AbortRequested
from fluidics.manual_operations import ManualOperations
from fluidics.run_session import RunSession

from ..conftest import wait_until
from .conftest import FLOW_CELL_STEP

INCUBATING = {**FLOW_CELL_STEP, "incubation_time": 60}     # minutes: a run to catch mid-way


@pytest.fixture
def rig(flow_cell_config, built):
    devices = built(flow_cell_config, simulation=True)
    devices.controller.COMMAND_SECONDS = 0
    devices.syringe_pump.ESTIMATE_SECONDS = 0
    session = RunSession(devices)
    seen = []
    session.state.subscribe(seen.append)
    return devices, session, build_operations(flow_cell_config, devices), seen


class TestARun:
    def test_it_runs_on_another_thread_and_the_rig_is_free_after(self, rig, real_clock):
        devices, session, ops, seen = rig
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
            self, rig, real_clock):
        """A GUI renders its controls on on_finished; it must see an idle rig
        and a signal the next job can use."""
        devices, session, ops, seen = rig
        at_finish = []

        def on_finished():
            at_finish.append((session.busy, devices.run_control.cancelled, list(seen)))

        session.start([FLOW_CELL_STEP], ops, callbacks={"on_finished": on_finished})
        session.abort()
        assert session.wait(5)
        assert at_finish == [(False, False, ["run", None])]

    def test_abort_stops_a_run_mid_incubation_and_wait_returns(self, rig, real_clock):
        devices, session, ops, seen = rig
        errors = []
        session.start([INCUBATING], ops, callbacks={"on_error": errors.append})
        assert not session.wait(0.05), "an hour's incubation ended in 50 ms"
        session.abort()
        assert session.wait(5)
        assert errors == ["Operation aborted by user"]
        assert not session.busy

    def test_a_second_job_while_a_run_is_going_is_refused(self, rig, real_clock):
        devices, session, ops, seen = rig
        session.start([INCUBATING], ops)
        with pytest.raises(RuntimeError, match="busy: a run"):
            session.start([FLOW_CELL_STEP], ops)
        with pytest.raises(RuntimeError, match="busy: a run"):
            session.run_manual(lambda: None)
        session.abort()
        assert session.wait(5)
        assert seen == ["run", None], "the refused jobs left a trace"

    def test_a_thread_that_will_not_start_leaves_the_rig_free(self, rig, monkeypatch):
        devices, session, ops, seen = rig

        class CannotStart:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("can't start new thread")

        import fluidics.run_session as module
        monkeypatch.setattr(module.threading, "Thread", CannotStart)
        with pytest.raises(RuntimeError, match="can't start"):
            session.start([FLOW_CELL_STEP], ops)
        assert not session.busy
        assert seen == ["run", None]

    def test_pause_and_resume_reach_the_run(self, rig, real_clock):
        devices, session, ops, seen = rig
        session.start([INCUBATING], ops)
        assert session.pause() is True
        assert wait_until(lambda: devices.run_control.at_rest)
        assert session.resume() is True
        assert not devices.run_control.paused
        session.abort()
        assert session.wait(5)


class TestAManualMove:
    def test_it_runs_off_the_callers_thread_and_reports_done(self, rig, real_clock):
        devices, session, ops, seen = rig
        manual = ManualOperations(devices)
        during = []

        def move():
            during.append((threading.current_thread(), session.kind))
            manual.extract(2, 300, 500)

        done = threading.Event()
        session.run_manual(move, on_done=done.set)
        assert done.wait(5) and session.wait(5)
        assert during == [(during[0][0], "manual")]
        assert during[0][0] is not threading.main_thread()
        assert devices.syringe_pump.executed == [[("extract", 2, 300, 40)]]
        assert seen == ["manual", None]

    def test_a_failing_move_reports_the_error_after_freeing_the_rig(self, rig, real_clock):
        devices, session, ops, seen = rig
        reported = []

        def broken():
            raise IOError("no reply from pump")

        session.run_manual(broken, on_error=lambda m: reported.append((m, session.busy)))
        assert session.wait(5)
        assert reported == [("no reply from pump", False)]

    def test_stop_ends_a_move_through_on_stopped_not_on_error(self, rig, real_clock):
        """The operator pressed Stop: not a failure, and the next move must
        not raise on the cancel that stopped this one."""
        devices, session, ops, seen = rig
        devices.syringe_pump.ESTIMATE_SECONDS = 60
        manual = ManualOperations(devices)
        reports = []
        session.run_manual(lambda: manual.extract(2, 300, 500),
                           on_done=lambda: reports.append("done"),
                           on_error=lambda m: reports.append(("error", m)),
                           on_stopped=lambda: reports.append("stopped"))
        assert wait_until(lambda: devices.syringe_pump.moving)
        session.abort()
        assert session.wait(5)
        assert reports == ["stopped"]
        assert not devices.run_control.cancelled, "the stop was not reset for the next job"
        devices.syringe_pump.ESTIMATE_SECONDS = 0
        session.run_manual(lambda: manual.empty_to_waste(), on_done=lambda: reports.append("done"))
        assert session.wait(5)
        assert reports == ["stopped", "done"]

    def test_on_stopped_defaults_to_on_done(self, rig, real_clock):
        devices, session, ops, seen = rig
        devices.syringe_pump.ESTIMATE_SECONDS = 60
        manual = ManualOperations(devices)
        reports = []
        session.run_manual(lambda: manual.extract(2, 300, 500),
                           on_done=lambda: reports.append("done"))
        assert wait_until(lambda: devices.syringe_pump.moving)
        session.abort()
        assert session.wait(5)
        assert reports == ["done"]

    def test_a_run_while_a_move_is_going_is_refused(self, rig, real_clock):
        devices, session, ops, seen = rig
        release = threading.Event()
        session.run_manual(release.wait)
        with pytest.raises(RuntimeError, match="busy: a manual"):
            session.start([FLOW_CELL_STEP], ops)
        release.set()
        assert session.wait(5)


class TestIdle:
    """Controls with no job leave the run's signal alone: a cancel or a pause
    set now would land on the next job."""

    def test_abort_pause_and_resume_do_nothing_and_say_so(self, rig):
        devices, session, ops, seen = rig
        assert session.abort() is False
        assert session.pause() is False
        assert session.resume() is False
        assert not devices.run_control.cancelled and not devices.run_control.paused

    def test_the_next_job_runs(self, rig, real_clock):
        devices, session, ops, seen = rig
        session.abort()
        session.pause()
        session.start([FLOW_CELL_STEP], ops)
        assert session.wait(5)
        assert devices.syringe_pump.executed, "the idle abort or pause reached the next job"


class TestWait:
    def test_an_idle_session_is_waited_for_at_once(self, rig):
        assert rig[1].wait(0) is True

    def test_wait_after_a_start_that_failed_does_not_raise(self, rig, monkeypatch):
        devices, session, ops, seen = rig

        class CannotStart:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("can't start new thread")

            def is_alive(self):
                return False

        import fluidics.run_session as module
        monkeypatch.setattr(module.threading, "Thread", CannotStart)
        with pytest.raises(RuntimeError, match="can't start"):
            session.start([FLOW_CELL_STEP], ops)
        assert session.wait(0) is True

    def test_wait_from_inside_the_jobs_own_callback_does_not_raise(self, rig, real_clock):
        """A callback that wants to know the job is over may ask; it cannot
        join its own thread, and must not be made to try."""
        devices, session, ops, seen = rig
        waited = []
        session.start([FLOW_CELL_STEP], ops,
                      callbacks={"on_finished": lambda: waited.append(session.wait(1))})
        assert session.wait(5)
        assert waited == [True]

    def test_wait_reports_a_timeout_rather_than_returning_early(self, rig, real_clock):
        devices, session, ops, seen = rig
        release = threading.Event()
        session.run_manual(release.wait)
        assert session.wait(0.02) is False
        release.set()
        assert session.wait(5) is True
