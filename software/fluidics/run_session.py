"""The one job on the rig at a time.

A rig does one thing: a run of sequences, or a manual move. `RunSession`
owns whichever is going -- starts it off the caller's thread, says whether
the rig is busy and with what, stops it, waits for it, and resets the run's
RunControl once it has ended so the next job starts clean. The GUI's tabs
and the CLI are its callers; none of them owns a thread or a worker, and an
abort from anywhere stops whichever job is running.

Qt-free. Callbacks run on the job's thread; a GUI posts them across.
"""

import logging
import threading
import time
from functools import partial

from .devices import build_worker
from .errors import Cancelled
from .subscribers import Subscribers
from .time_estimate import estimate_run_time

_logger = logging.getLogger(__name__)


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
        # that needs the session; the GUI posts an event.
        self.state = Subscribers("run session")
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
    def at_rest(self):
        """Paused and actually stopped at a gate."""
        return self.control.at_rest

    @property
    def elapsed_seconds(self):
        """This job's running time so far: wall time minus held spans."""
        return self.control.running_seconds()

    @property
    def cancelled(self):
        """The job is unwinding after an abort or a fault; the rig reads
        busy until it has."""
        return self.control.cancelled

    # --- starting a job ---

    # Every job reports the same way: on_stopped() if the operator stopped
    # it, on_error(message) if it failed, and on_finished() last, always. A
    # run's callbacks also carry the worker's update_progress and
    # on_estimate. The reports run after the rig is free, so a GUI
    # rendering on them sees an idle rig.

    def start(self, sequences, operations, callbacks=None, durations=None):
        """Run `sequences` through `operations` on a new thread. Refused with
        RuntimeError while a job runs.

        durations: the run's per-sequence time estimate, when the caller
        already has one in hand (the GUI prices its confirm dialog first and
        passes the same figures, so the dialog and the countdown cannot
        disagree). Estimated here otherwise -- the one place every run
        passes through, so every run carries figures.
        """
        if self.busy:       # before the worker is built, whose estimate callback would fire
            raise RuntimeError(f"the rig is busy: a {self._kind} is in progress")
        if durations is None:
            durations = estimate_run_time(self.devices.config, sequences)[1]
        callbacks = dict(callbacks or {})
        on_finished = callbacks.pop("on_finished", None)
        on_stopped = callbacks.pop("on_stopped", None)
        on_error = callbacks.pop("on_error", None)
        # The worker reports an early end from inside run(), while the rig is
        # still its; the report is held and delivered once the rig is free,
        # as a manual move's is.
        ended = []
        callbacks["on_stopped"] = lambda: ended.append(on_stopped)
        callbacks["on_error"] = lambda message: ended.append(
            on_error and partial(on_error, message))
        worker = build_worker(self.devices, operations, sequences, callbacks,
                              durations=durations)

        def body():
            worker.run()
            return ended[0] if ended else None

        self._launch("run", body, on_finished)

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
        """Start `body` on the job's thread. It returns how the job ended
        early -- a callable to report it, or None -- and once the rig is free
        that report runs, then on_finished."""
        with self._lock:
            if self._kind is not None:
                raise RuntimeError(f"the rig is busy: a {self._kind} is in progress")
            self._kind = kind
            self._done.clear()
            self.control.restart_clock()
            self.state.notify(kind)

        def job():
            try:
                report = body()
            finally:
                self._finish()          # whatever happened, the rig is free
            if report is not None:
                report()
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

    def _finish(self):
        """The job has ended. One transition, under the lock: the rig is
        free, the signal is clean, the subscribers know, the waiters wake --
        and nothing can start in between, or the ending job's tail would
        land on the new one."""
        with self._lock:
            self._kind = None
            # The cancellation -- or pause -- belonged to the job that just
            # ended; the next one must not raise on a stale abort.
            self.control.reset()
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
