"""The exceptions this package raises on purpose, and the run's control signal.

A leaf module -- stdlib only -- so the drivers under fluidics/control can
import it without the control layer depending on the experiment layer.

    FluidicsError
    ├── OperationError      a step failed; something is wrong with the step
    └── Cancelled           the run stopped early, on purpose
        ├── AbortRequested  the operator pressed Abort. Expected, not an error.
        └── SafetyFault     the instrument stopped itself. A failure.

Two things worth stating because both are easy to get wrong. A safety fault is
a sibling of AbortRequested, never a subclass: the worker reports an abort as
"aborted by user", and a fault reported that way would discard exactly the
diagnosis it exists to deliver. And code that must let cancellation through
names `Cancelled`, the base -- not a tuple of concrete types that someone
forgets to extend when the next kind of fault arrives.
"""

import contextlib
import logging
import threading
import time


_logger = logging.getLogger(__name__)


class FluidicsError(Exception):
    """Base for everything this package raises on purpose."""


class OperationError(FluidicsError):
    """A step failed; something is wrong with the step."""


class Cancelled(FluidicsError):
    """The run stopped early, on purpose."""


class AbortRequested(Cancelled):
    """The operator pressed Abort. Expected, not an error."""


class SafetyFault(Cancelled):
    """The instrument stopped itself. A failure, reported with its cause."""


class RunControl:
    """One run's control signal, shared by every device: cancel and pause.

    Built once per run and handed to everything that waits, so an abort from
    any entry point reaches all of them. A device constructed alone gets a
    private one.

    Cancel is a latch -- one-shot, carries a cause, unwinds by raising. Pause
    is a gate -- reversible, carries nothing, never raises. They live on one
    object because every wait must answer to both: an Abort pressed while
    paused has to unwind at once, and a pause must never be mistaken for a
    cancel.

    cancel(), pause() and resume() do no device I/O -- they are called from
    the Qt thread, a SIGINT handler, or the MCU reader thread, none of which
    owns a serial port. Each device stops itself, on the thread that owns it,
    when its wait wakes.

    First cause wins: the operator's reflex after a flow alarm is to press
    Abort a second later, and last-writer-wins would overwrite the fault with
    a bare abort before the worker read it. The mutators share a lock so a
    cancel racing a reset cannot leave the cause and the events disagreeing.
    Built on threading.Event rather than a Condition so the test suite's fake
    clock, which patches Event.wait, covers every wait here.

    Two kinds of wait, and the difference is the whole of pause:

      neither          a command already sent, waited out with no signal at
                       all (send_command_blocking without a run_control).
      wait()/sleep()   cancellation only. For polling hardware that is
                       already moving -- a command in flight must be waited
                       out whether or not the operator has paused.
      checkpoint()     the gate: block while paused, before starting
                       something new.
      delay()          a run-level delay -- an incubation, a settle wait --
                       measured in running time, so paused time does not
                       count against it.

    And when_held() marks a region with something powered in it, whose hooks
    run around an actual park so that it can follow the run into the hold.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._tripped = threading.Event()
        self._running = threading.Event()
        self._running.set()
        # Set while the run is stopped for any reason, so a timed wait wakes
        # instead of sleeping through a pause or a cancel.
        self._interrupted = threading.Event()
        self._cause = None
        self._paused = False
        self._holding = 0
        # Per-thread: a region belongs to the thread that entered it, and it
        # is that thread's park the hooks are about.
        self._local = threading.local()

    def cancel(self, cause=None):
        """Trip the signal with `cause` (default: the operator aborted).

        Returns True if this call set the cause, False if one was already set
        and therefore wins.
        """
        if cause is None:
            cause = AbortRequested()
        if not isinstance(cause, Cancelled):
            raise TypeError(f"cancel() takes a Cancelled instance, not {cause!r}")
        with self._lock:
            if self._cause is not None:
                return False
            self._cause = cause
            self._tripped.set()
            # A cancelled run never waits at the gate: opening it is what
            # lets an Abort pressed while paused unwind instead of deadlock.
            self._paused = False
            self._running.set()
            self._interrupted.set()
            return True

    def reset(self):
        """Clear both cancel and pause -- a run never starts already stopped."""
        with self._lock:
            self._cause = None
            self._paused = False
            self._tripped.clear()
            self._running.set()
            self._interrupted.clear()

    def pause(self):
        """Hold the run at the next gate. True if this call paused it.

        Refused once cancelled: a run that is over cannot be suspended, and
        the gate must never close on a thread that is unwinding.
        """
        with self._lock:
            if self._cause is not None or self._paused:
                return False
            self._paused = True
            self._running.clear()
            self._interrupted.set()
        _logger.info("Pause requested; the run will hold after the move in flight.")
        return True

    def resume(self):
        """Let the run go on. True if this call resumed it."""
        with self._lock:
            if not self._paused:
                return False
            self._paused = False
            self._running.set()
            if self._cause is None:
                self._interrupted.clear()
        _logger.info("Resumed.")
        return True

    @property
    def paused(self):
        """Whether a pause has been asked for -- not whether the run has come
        to rest on it. See `holding`."""
        return self._paused

    @property
    def holding(self):
        """How many threads are parked at a gate right now.

        Only threads that actually stopped: a gate the run walks straight
        through does not count, or a running run would read as a stopped one.
        Anything else that learns to hold a run -- a driver that parks a move
        rather than finishing it -- has to be counted here too, or `at_rest`
        will say the run is still moving when it is not.
        """
        return self._holding

    @property
    def at_rest(self):
        """Whether the run has actually come to a stop, as against having been
        asked to.

        The two are different moments -- a move in flight keeps going until it
        reaches a gate -- and the operator is owed the difference: "pausing"
        means liquid may still be moving. Composed here rather than by each
        caller so that everything reporting a pause agrees, and so a cancel
        (which clears the pause) cannot leave a caller reading "stopped" off a
        thread that is on its way out of the gate.
        """
        return self._paused and self._holding > 0

    @contextlib.contextmanager
    def when_held(self, on_hold, on_release):
        """Mark a region with something powered in it.

        For the caller that has committed hardware around a gated call -- the
        drain pump pulling under a dispense. When a gate inside the region
        actually parks this thread, `on_hold` runs first (drain off) and
        `on_release` runs once the gate opens on a resume (drain on). Neither
        runs for a gate the run walks through, and `on_release` never runs for
        a cancel: a run that is unwinding must not power anything back up.

        Per thread, and the hooks belong to the thread that parks, so they
        run where the hardware's port lives.
        """
        hooks = self._hooks
        hooks.append((on_hold, on_release))
        try:
            yield
        finally:
            hooks.remove((on_hold, on_release))

    @property
    def _hooks(self):
        hooks = getattr(self._local, "hooks", None)
        if hooks is None:
            hooks = self._local.hooks = []
        return hooks

    @property
    def cancelled(self):
        return self._cause is not None

    @property
    def cause(self):
        return self._cause

    def check(self):
        """Raise the cause if the run is cancelled; otherwise return."""
        cause = self._cause
        if cause is not None:
            raise cause

    def wait(self, timeout):
        """Block up to `timeout` seconds. True if cancelled, False on timeout."""
        return self._tripped.wait(timeout)

    def wait_interrupted(self, timeout):
        """Block up to `timeout` seconds. True if the run stopped meanwhile --
        paused or cancelled -- False on timeout.

        For a device that can stop a move and finish it later; a device that
        must wait its move out uses wait().
        """
        return self._interrupted.wait(timeout)

    def sleep(self, timeout):
        """wait(), then raise if the run was cancelled meanwhile."""
        self.wait(timeout)
        self.check()

    def checkpoint(self):
        """Block while the run is paused, then raise if it is cancelled.

        The gate every device passes before it starts something new, so a move
        already in flight finishes and the next one waits here -- or, for a
        device that stops its move on a pause, where it parks before finishing
        it. An untimed wait: cancel() and resume() both open it.
        """
        # Counted only if this thread is actually going to stop. Counting
        # every pass would make a running run look stopped to anyone reading
        # `holding`, one gate at a time.
        if self._running.is_set():
            self.check()
            return
        hooks = self._hooks
        for on_hold, _ in hooks:
            on_hold()
        with self._lock:
            self._holding += 1
        try:
            self._running.wait()
        finally:
            with self._lock:
                self._holding -= 1
        self.check()
        for _, on_release in reversed(hooks):
            on_release()

    def run_for(self, seconds):
        """Spend up to `seconds` of running time, returning early if the run
        is paused. Returns the seconds actually spent; raises if cancelled.

        The primitive under delay(), and what a device with something powered
        needs directly: the drain pump switches off for the pause and comes
        back for the remainder.
        """
        self.check()
        if self._paused:
            return 0.0
        # Monotonic: an NTP step or an operator setting the clock back would
        # otherwise make this look like no time had passed, and delay() would
        # wait the whole interval again.
        started = time.monotonic()
        # Wakes early if the run stops, so a pause does not sit through the
        # rest of an incubation before anyone notices.
        self._interrupted.wait(seconds)
        spent = min(seconds, time.monotonic() - started)
        self.check()
        return spent

    def delay(self, seconds):
        """Spend `seconds` of running time here: a pause stops the clock and
        the remainder resumes with it. Raises if the run is cancelled."""
        remaining = seconds
        while remaining > 0:
            self.checkpoint()
            remaining -= self.run_for(remaining)
        # Answers a cancel even for delay(0), which the loop skips entirely.
        self.check()
