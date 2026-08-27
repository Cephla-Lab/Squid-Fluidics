"""The one job on the rig at a time.

A rig does one thing: a run of sequences, or a manual move. `RunSession`
owns whichever is going -- starts it off the caller's thread, says whether
the rig is busy and with what, stops it, waits for it, and resets the run's
RunControl once it has ended so the next job starts clean. The GUI's tabs
and the CLI are its callers; none of them owns a thread or a worker any
more, and an abort from anywhere stops whichever job is running.

Qt-free. Callbacks run on the job's thread; a GUI posts them across.
"""

import logging
import threading

from .control.controller import Subscribers
from .devices import build_worker
from .errors import Cancelled

_logger = logging.getLogger(__name__)


class RunSession:
    def __init__(self, devices):
        self.devices = devices
        # Notified with the kind of job ("run", "manual") as one starts -- on
        # the starter's thread, before any of the job's callbacks -- and with
        # None when it ends, on the job's own thread.
        self.state = Subscribers("run session")
        self._lock = threading.Lock()
        self._kind = None
        self._thread = None
        self._done = threading.Event()
        self._done.set()
        self.worker = None      # the current run's ExperimentWorker, while a run is the job

    @property
    def busy(self):
        return self._kind is not None

    @property
    def kind(self):
        """One of "run", "manual", or None."""
        return self._kind

    # --- starting a job ---

    def start(self, sequences, operations, callbacks=None):
        """Run `sequences` through `operations` on a new thread.

        `callbacks` are ExperimentWorker's (update_progress, on_error,
        on_finished, on_estimate); make_safe is the session's, as
        build_worker insists. The run's signal is reset and the rig marked
        free *before* the caller's on_finished runs, so a GUI rendering on
        it sees an idle rig. Refused with RuntimeError while a job runs.
        """
        self._refuse_if_busy()
        callbacks = dict(callbacks or {})
        callers_finished = callbacks.get("on_finished")

        def on_finished():
            self._finish()
            if callers_finished is not None:
                callers_finished()

        callbacks["on_finished"] = on_finished
        worker = build_worker(self.devices, operations, sequences, callbacks)
        self.worker = worker
        self._launch("run", worker.run)
        return worker

    def run_manual(self, verb, on_done=None, on_error=None, on_stopped=None):
        """Run one manual verb -- a callable -- on a new thread.

        on_done() when it finished, on_error(message) when it raised,
        on_stopped() when the run's signal cancelled it (the operator pressed
        Stop; defaults to on_done). Each is called after the rig is free.
        Refused with RuntimeError while a job runs.
        """
        def body():
            try:
                verb()
            except Cancelled as stop:
                _logger.info("Manual move stopped: %s", stop)
                report, args = (on_stopped or on_done), ()
            except Exception as e:
                _logger.error("Manual move failed: %s", e, exc_info=True)
                report, args = on_error, (str(e),)
            else:
                report, args = on_done, ()
            self._finish()          # the rig is free before anyone is told
            if report is not None:
                report(*args)

        self._launch("manual", body)

    def _refuse_if_busy(self):
        if self._kind is not None:
            raise RuntimeError(f"the rig is busy: a {self._kind} is in progress")

    def _launch(self, kind, body):
        with self._lock:
            self._refuse_if_busy()
            self._kind = kind
            self._done.clear()
            self._thread = threading.Thread(target=body, daemon=True)
        self.state.notify(kind)
        try:
            self._thread.start()
        except BaseException:
            # A thread that will not start is no job: leave the rig free.
            self._finish()
            raise

    def _finish(self):
        """The job has ended: the rig is free, the run's signal clean."""
        with self._lock:
            self._kind = None
            self.worker = None
        # The cancellation -- or pause -- belonged to the job that just
        # ended; the next one must not raise on a stale abort.
        self.devices.reset()
        self.state.notify(None)
        self._done.set()

    # --- controlling and waiting ---

    def abort(self):
        """Cancel whichever job is running: one signal, no I/O on this thread.
        True if there was one. Idle, the signal is left alone -- a cancel
        with no job to reset it would abort the next start at once."""
        if self._kind is None:
            return False
        self.devices.abort()
        return True

    def pause(self):
        """Hold the running job at its next gate. True if this call did;
        False when there is none -- the next job must not start paused."""
        if self._kind is None:
            return False
        return self.devices.pause()

    def resume(self):
        if self._kind is None:
            return False
        return self.devices.resume()

    def wait(self, timeout=None):
        """Block until the current job has ended and its thread is gone.
        True if it has; False if `timeout` passed first.

        On the session's own event, not Thread.join() alone: on CPython 3.10
        a KeyboardInterrupt delivered inside join() marks the thread as
        stopped (Thread._wait_for_tstate_lock releases the state lock on the
        way out), so a join() after the interrupt handler's abort returns at
        once, and a caller would tear the devices down under a job still
        driving the pump. The event is set from the job's own end, so it
        cannot be fooled the same way.
        """
        if not self._done.wait(timeout):
            return False
        thread = self._thread
        # Only a live thread that is not this one can be joined: the thread
        # of a start() that failed never ran, and a callback on the job's
        # thread may want to wait on its own end.
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join()
        return True
