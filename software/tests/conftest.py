import os
import threading
import time as _time
from pathlib import Path

import pytest

# Widget tests construct a real QApplication; Qt picks its platform when that
# happens, and on a headless box (CI, an SSH session) the default xcb aborts.
# Owned here, once, so local headless runs and CI behave identically and every
# future widget test is covered without remembering its own setdefault.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    """The process's one QApplication, for the widget tests. Lazy: nothing
    Qt is imported or constructed unless a test asks for it."""
    from PyQt5.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir():
    return FIXTURES_DIR


# Captured at import, before _fast_clock patches them.
_pristine_wait = threading.Event.wait
_pristine_sleep = _time.sleep
_pristine_time = _time.time
_pristine_monotonic = _time.monotonic


@pytest.fixture
def real_clock(monkeypatch):
    """Undo _fast_clock for one test, so elapsed wall time means something.

    For the few tests whose point is that a long wait wakes early: under the
    fake clock every wait "returns instantly", so a timing test written
    against it would pass on the broken code too.
    """
    monkeypatch.setattr(threading.Event, "wait", _pristine_wait)
    monkeypatch.setattr("time.sleep", _pristine_sleep)
    # Both clocks too, or code that measures how long a wait took (RunControl
    # .run_for) reads a frozen clock, concludes no time passed, and loops.
    monkeypatch.setattr("time.time", _pristine_time)
    monkeypatch.setattr("time.monotonic", _pristine_monotonic)


@pytest.fixture
def during_move():
    """Hook a side effect into a pump's wait_for_stop -- inside the move.

    Not around execute(): a cancel delivered before execute() is caught by
    _arm() before any chain is dispatched, a different path from the mid-move
    one these tests model.
    """
    def _hook(sp, side_effect, nth=None):
        """Fire on every call, or only on the nth -- a pause that must land
        once, inside the first move, and not again on the resumed remainder."""
        original_wait = sp.wait_for_stop
        calls = []

        def wait_for_stop(t=0):
            calls.append(t)
            if nth is None or len(calls) == nth:
                side_effect()
            return original_wait(t)

        sp.wait_for_stop = wait_for_stop

    return _hook


@pytest.fixture
def cancel_after_chain():
    """Cancel the run once the pump has executed a chain `when` picks out.

    Where a cancel lands relative to the queued chains decides which checked
    call raises next, so it is the seam the operations' unwinding is tested
    at. `when` receives the list of chains executed so far.
    """
    def _hook(sp, when):
        original = sp.execute

        def execute():
            original()
            if when(sp.executed):
                sp.run_control.cancel()

        sp.execute = execute

    return _hook


def dispenses(chain):
    """Whether a chain pushes liquid out -- the last chain of a fluidic
    operation, as opposed to a draw or a dump to waste."""
    return any(op[0] == "dispense" for op in chain)


def moved_ul(sp, kind):
    """Total volume the simulated pump has moved in ops of `kind`, across
    every executed chain -- the figure a paused-and-resumed operation has to
    match against an uninterrupted one."""
    return sum(op[2] for op in sp.executed_ops if op[0] == kind)


def wait_until(predicate, timeout=2, step=0.002):
    """Poll `predicate` on the real clock. True once it holds, False if
    `timeout` seconds pass first."""
    deadline = _time.monotonic() + timeout
    while not predicate():
        if _time.monotonic() > deadline:
            return False
        _time.sleep(step)
    return True


# Long enough to outlast thread start-up, short enough not to pad the suite:
# the gates under test resolve in sub-microseconds, so this only has to be
# unambiguously longer than "immediately".
SETTLE = 0.02


@pytest.fixture
def run_in_background():
    """Run `call` on a daemon thread. Returns (finished, error): an Event set
    when it returns or raises, and a list that will hold the exception.

    Recorded rather than swallowed -- a call that raises would otherwise look
    like a call that never finished.
    """
    def _run(call):
        finished = threading.Event()
        error = []

        def run():
            try:
                call()
            except BaseException as e:
                error.append(e)
            finally:
                finished.set()

        threading.Thread(target=run, daemon=True).start()
        return finished, error

    return _run


@pytest.fixture
def holds_while_paused(real_clock, run_in_background):
    """Assert a call blocks while the run is paused and completes on resume.

    Real clock by construction: the point is that time passes and the call
    does not return. `while_held` runs once the call is confirmed parked.
    """
    def _check(control, call, while_held=None):
        control.pause()
        finished, error = run_in_background(call)
        assert not finished.wait(SETTLE), f"the call ran on through the pause: {error}"
        if while_held is not None:
            while_held()
        control.resume()
        assert finished.wait(2), "the call did not finish after the resume"
        assert not error, error

    return _check


@pytest.fixture
def parks(real_clock, run_in_background):
    """Run `call` in the background and wait until the run has come to rest at
    a gate -- the shape of a pause landing inside a move. Returns (finished,
    error) for the test to resume or cancel into; `finished.wait(2) and not
    error` is then the usual close.

    Real clock by construction, like holds_while_paused: the point is that
    the call has not returned.
    """
    def _park(control, call):
        finished, error = run_in_background(call)
        assert wait_until(lambda: control.at_rest), f"the run never parked: {error}"
        return finished, error

    return _park


@pytest.fixture
def cancel_during_wait():
    """Cancel a RunControl from inside its own wait -- the shape of an abort
    landing mid-incubation or mid-aspiration.

    Both kinds are wrapped: the cancellation-only wait hardware polls sit in,
    and run_for, the running-time one behind delay() and the drain pump.
    """
    def _hook(control):
        original_wait, original_run_for = control.wait, control.run_for

        def wait(timeout):
            control.cancel()
            return original_wait(timeout)

        def run_for(seconds):
            control.cancel()
            return original_run_for(seconds)

        control.wait = wait
        control.run_for = run_for

    return _hook


@pytest.fixture(autouse=True)
def _fast_clock(monkeypatch):
    """Patch time.sleep, time.time, time.monotonic and Event.wait so tests
    run instantly.

    sleep() advances the fake clock instead of blocking.
    time() returns the fake clock value, so timeouts expire immediately.
    Event.wait() advances the fake clock by the timeout instead of blocking
    (used by DiscPump.aspirate).

    Must patch both the time module AND each module that did
    'from time import sleep/time' (since those hold a direct reference).
    """
    fake_time = [_time.time()]
    real_sleep = _time.sleep

    def fake_sleep(seconds):
        fake_time[0] += seconds
        # Yield the GIL the way a real sleep would. Without this, a simulated
        # sensor's publish loop (sleep 0.06s per reading) becomes a pure CPU
        # spin that starves every other thread -- measured as ~6.7s of a
        # sample-experiment end-to-end test that otherwise takes 0.08s. The
        # captured reference matters: time.sleep itself is patched below.
        real_sleep(0)

    def fake_time_fn():
        return fake_time[0]

    def fake_event_wait(self, timeout=None):
        if timeout is None:
            # An untimed wait has no clock to fake: it blocks until another
            # thread sets the event. Thread.start() relies on exactly that
            # to return only once the thread is really running; faking it
            # let start() return early and a following join() raise.
            return _pristine_wait(self)
        fake_time[0] += timeout
        return self.is_set()

    # Patch the time module itself. monotonic moves with the same fake clock:
    # durations are measured off it (RunControl.run_for), so a test that
    # advances one clock and reads the other would see no time pass at all.
    monkeypatch.setattr("time.sleep", fake_sleep)
    monkeypatch.setattr("time.time", fake_time_fn)
    monkeypatch.setattr("time.monotonic", fake_time_fn)

    # Patch modules that use 'from time import sleep' or 'from time import time'
    # No module-level patches for the operations or sequence_utils: their
    # waits go through RunControl (Event.wait, patched below) and they read
    # the clock through the time module, patched above.
    monkeypatch.setattr("fluidics.control.controller.sleep", fake_sleep)
    monkeypatch.setattr("fluidics.control.controller.time", fake_time_fn)

    # Patch threading.Event.wait (used by DiscPump.aspirate)
    monkeypatch.setattr(threading.Event, "wait", fake_event_wait)
