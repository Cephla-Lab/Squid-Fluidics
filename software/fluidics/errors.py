"""The exceptions this package raises on purpose, and the run's one cancel signal.

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

import threading


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
    """The one cancellation signal a run shares, owned at app scope and
    injected into every device that waits.

    cancel() performs no device I/O. It is called from the Qt thread, a SIGINT
    handler, or the MCU reader thread -- none of which owns a serial port.
    Each device stops itself, on the thread that owns it, when its wait wakes.

    First cause wins: the operator's reflex after a flow alarm is to press
    Abort a second later, and last-writer-wins would overwrite the fault with
    a bare abort before the worker read it. cancel() and reset() share a lock,
    so a cancel racing a reset cannot leave the event set with no cause (a
    fault reported as an abort) or a cause with the event clear (a fault that
    never fires).

    Built on threading.Event rather than a Condition so the test suite's fake
    clock, which patches Event.wait, covers every wait that goes through here.
    Pause (a reversible gate on the same object) is designed and arrives later.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._tripped = threading.Event()
        self._cause = None

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
            return True

    def reset(self):
        with self._lock:
            self._cause = None
            self._tripped.clear()

    @property
    def cancelled(self):
        return self._tripped.is_set()

    @property
    def cause(self):
        return self._cause

    def check(self):
        """Raise the cause if the run is cancelled; otherwise return."""
        with self._lock:
            cause = self._cause
        if cause is not None:
            raise cause

    def wait(self, timeout):
        """Block up to `timeout` seconds. True if cancelled, False on timeout."""
        return self._tripped.wait(timeout)

    def sleep(self, timeout):
        """wait(), then raise if the run was cancelled meanwhile."""
        if self.wait(timeout):
            self.check()
