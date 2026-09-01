"""The one job on the rig at a time.

A rig does one thing: a run of sequences, or a manual move. `RunSession`
owns whichever is going -- starts it off the caller's thread, says whether
the rig is busy and with what, stops it, waits for it, and resets the run's
RunControl once it has ended so the next job starts clean. The GUI's tabs
and the CLI are its callers; none of them owns a thread or a worker, and an
abort from anywhere stops whichever job is running.

Qt-free. Callbacks run on the job's thread; a GUI posts them across.
"""

import collections
import itertools
import logging
import threading
import time
from functools import partial

from .devices import build_worker
from .errors import Cancelled
from .events import RunEnded
from .subscribers import Subscribers
from .time_estimate import plan_run

_logger = logging.getLogger(__name__)


# What a display needs to draw the run, in one read: RunSession.snapshot().
SessionSnapshot = collections.namedtuple(
    "SessionSnapshot", "kind cancelled paused at_rest elapsed_seconds")


class RunSession:
    def __init__(self, devices):
        self.devices = devices
        self.control = devices.run_control
        # Notified with the kind of job ("run", "manual") as one starts -- on
        # the starter's thread, before any of the job's callbacks -- and with
        # None when it ends, on the job's thread. In that order, always: both
        # happen under the session's lock, which is re-entrant so a subscriber may
        # start the next job from the end of this one (a waiter then waits
        # for that one too). Subscribers must not block on another thread
        # that needs the session; the GUI posts an event -- and a subscriber
        # that defers its work that way must re-read the session when it
        # runs rather than trust the kind it was handed, which describes
        # the moment it was sent. The payload is exact for whoever acts on
        # it synchronously (the ordering tests do).
        self.state = Subscribers("run session")
        # The run's boundary facts (fluidics.events): the worker publishes
        # the in-run ones; the session publishes RunEnded once the rig is
        # free, so a dialog painted on it sees an idle rig -- the same
        # deferral the early-end report has always had. Subscription order
        # is delivery order, and a subscriber may chain the next run from
        # RunEnded (the GUI's resume offer) while later subscribers still
        # wait for theirs -- so system-level bookkeeping (usage, reports)
        # must subscribe before any widget that can chain.
        self.events = Subscribers("run events")
        self._run_ids = itertools.count(1)     # atomic under the GIL
        self._lock = threading.RLock()
        self._kind = None
        self._thread = None
        self._done = threading.Event()
        self._done.set()

    # --- what is going on ---

    @property
    def busy(self):
        return self._kind is not None

    @property
    def kind(self):
        """One of "run", "manual", or None."""
        return self._kind

    @property
    def paused(self):
        """Asked to pause -- the move in flight may still be running."""
        return self.control.paused

    @property
    def elapsed_seconds(self):
        """This job's running time so far: wall time minus held spans."""
        return self.control.running_seconds()

    @property
    def cancelled(self):
        """The job is unwinding after an abort or a fault; the rig reads
        busy until it has."""
        return self.control.cancelled

    def snapshot(self):
        """The display's inputs in one call: the job's kind and the
        control's state, coherent with each other -- the session's lock is
        held through both reads (the established session-then-control
        order), so a job cannot start or finish between them and pair one
        job's kind with another's flags."""
        with self._lock:
            kind = self._kind
            control = self.control.snapshot()
        return SessionSnapshot(kind, control.cancelled, control.paused,
                               control.at_rest, control.running_seconds)

    # --- starting a job ---

    # A run reports through `events` (RunStarted and the per-sequence facts
    # from the worker; RunEnded from here, after the rig is free, so a GUI
    # painting on it sees an idle rig). A manual move reports through its
    # callbacks -- on_stopped() if the operator stopped it, on_error(message)
    # if it failed, on_finished() last, always -- likewise after the rig is
    # free.

    def start(self, sequences, operations, plan=None):
        """Run `sequences` through `operations` on a new thread. Refused with
        RuntimeError while a job runs.

        plan: the run plan (fluidics.events.PlanEntry per repeat), when the
        caller already has one in hand (the GUI prices its confirm dialog
        with it, so the dialog and the run cannot disagree). Built here
        otherwise -- the one place every run passes through, so every run
        carries one. `sequences` is read only to build a missing plan: a
        caller holding a plan (a resume tail, say) may pass sequences=None. The run reports through `self.events`: the worker
        publishes RunStarted and the per-sequence facts; RunEnded is
        published once the rig is free, the same deferral the early-end
        report has always had.
        """
        if self.busy:       # before the worker is built, whose RunStarted would fire
            raise RuntimeError(f"the rig is busy: a {self._kind} is in progress")
        if plan is None:
            plan = plan_run(self.devices.config, sequences)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())   # UTC: sortable across DST
        run_id = f"run-{stamp}-{next(self._run_ids)}"
        worker = build_worker(self.devices, operations, plan, run_id,
                              self.events)

        def body():
            worker.run()
            return partial(self.events.notify,
                           RunEnded(run_id, worker.outcome, worker.message,
                                    worker.elapsed_seconds,
                                    worker.ended_position))

        try:
            self._launch("run", body, None)
        except BaseException as e:
            # The worker's RunStarted already went out (from its
            # constructor); a launch that failed must not leave it
            # unpaired -- consumers pairing starts with endings (the usage
            # ledger's totals, a display's lifecycle) would wait forever.
            # This one RunEnded lands after _finish's state(None), outside
            # the serialized ordering -- acceptable for a thread that would
            # not start, revisit if a chaining subscriber ever exists.
            self.events.notify(RunEnded(run_id, "failed", str(e), 0.0, None))
            raise

    def run_manual(self, verb, callbacks=None):
        """Run one manual verb -- a callable -- on a new thread. Refused with
        RuntimeError while a job runs."""
        callbacks = callbacks or {}
        on_stopped, on_error = callbacks.get("on_stopped"), callbacks.get("on_error")

        def body():
            try:
                verb()
            except Cancelled as stop:
                _logger.info("Manual move stopped: %s", stop)
                return on_stopped
            except Exception as e:
                _logger.error("Manual move failed: %s", e, exc_info=True)
                return on_error and partial(on_error, str(e))
            return None

        self._launch("manual", body, callbacks.get("on_finished"))

    def _launch(self, kind, body, on_finished):
        """Start `body` on the job's thread. It returns how the job ended --
        a callable that reports it (a run's RunEnded, a manual move's held
        callback), delivered by _finish once the rig reads free -- then
        on_finished runs."""
        with self._lock:
            if self._kind is not None:
                raise RuntimeError(f"the rig is busy: a {self._kind} is in progress")
            self._kind = kind
            self._done.clear()
            self.control.restart_clock()
            self.state.notify(kind)

        def job():
            report = None
            try:
                report = body()
            finally:
                self._finish(report)    # whatever happened, the rig is free
            if on_finished is not None:
                on_finished()

        thread = threading.Thread(target=job, daemon=True)
        try:
            thread.start()
        except BaseException:
            # A thread that will not start is no job: leave the rig free.
            self._finish()
            raise
        self._thread = thread       # only ever a started thread, for wait() to join

    def _finish(self, report=None):
        """The job has ended. One transition, under the lock: the rig is
        free, the signal is clean, the job's own report lands, the
        subscribers know, the waiters wake -- and nothing can start in
        between, or the ending job's tail would land on the new one.

        The report (a run's RunEnded, a manual move's held callback) runs
        after kind and signal are cleared -- whoever hears it sees an idle
        rig -- and *before* state.notify(None), so a state subscriber that
        chains the next job cannot slide a new RunStarted in front of the
        old run's ending. The price is a rule the channel already carries:
        a report subscriber must not block on the session (the waiters are
        woken only at the end of this transition).
        """
        with self._lock:
            self._kind = None
            # The cancellation -- or pause -- belonged to the job that just
            # ended; the next one must not raise on a stale abort.
            self.control.reset()
            if report is not None:
                report()
            self.state.notify(None)
            # Done means no job is current. A subscriber may have started the
            # next one just now, in which case waiters keep waiting for it --
            # a shutdown wants the last job in a chain, not the first.
            if self._kind is None:
                self._done.set()

    # --- controlling and waiting ---

    def abort(self):
        """Cancel whichever job is running: one signal, no I/O on this thread.
        True if there was one. Idle, the signal is left alone -- a cancel
        with no job to reset it would abort the next start at once."""
        with self._lock:
            if self._kind is None:
                return False
            self.control.cancel()
            return True

    def pause(self):
        """Hold the running job at its next gate. True if this call did;
        False when there is none -- the next job must not start paused."""
        with self._lock:
            return self._kind is not None and self.control.pause()

    def resume(self):
        with self._lock:
            return self._kind is not None and self.control.resume()

    def wait(self, timeout=None):
        """Block until the current job has ended and its thread is gone.
        True if it has; False if `timeout` passed first.

        On the session's own event, not Thread.join() alone: on CPython 3.10
        a KeyboardInterrupt delivered inside join() marks the thread as
        stopped (Thread._wait_for_tstate_lock releases the state lock on the
        way out), so a join() after the interrupt handler's abort returns at
        once, and a caller would tear the devices down under a job still
        driving the pump. The event is set from the job's own end, so it
        cannot be fooled the same way. A callback on the job's thread may
        wait too; it cannot join itself and is not asked to.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        if not self._done.wait(timeout):
            return False
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            # The deadline covers the join too: a report that hangs must not
            # hang the caller past what it asked for.
            thread.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
            return not thread.is_alive()
        return True
