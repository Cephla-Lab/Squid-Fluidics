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


@pytest.fixture
def real_clock(monkeypatch):
    """Undo _fast_clock for one test, so elapsed wall time means something.

    For the few tests whose point is that a long wait wakes early: under the
    fake clock every wait "returns instantly", so a timing test written
    against it would pass on the broken code too.
    """
    monkeypatch.setattr(threading.Event, "wait", _pristine_wait)
    monkeypatch.setattr("time.sleep", _pristine_sleep)


@pytest.fixture
def during_move():
    """Hook a side effect into a pump's wait_for_stop -- inside the move.

    Not around execute(): a cancel delivered before execute() is caught by
    _arm() before any chain is dispatched, a different path from the mid-move
    one these tests model.
    """
    def _hook(sp, side_effect):
        original_wait = sp.wait_for_stop

        def wait_for_stop(t=0):
            side_effect()
            return original_wait(t)

        sp.wait_for_stop = wait_for_stop

    return _hook


@pytest.fixture
def cancel_during_wait():
    """Cancel a RunControl from inside its own wait -- the shape of an abort
    landing mid-incubation or mid-aspiration."""
    def _hook(control):
        original_wait = control.wait

        def wait(timeout):
            control.cancel()
            return original_wait(timeout)

        control.wait = wait

    return _hook


@pytest.fixture(autouse=True)
def _fast_clock(monkeypatch):
    """Patch time.sleep, time.time, and Event.wait so tests run instantly.

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

    # Patch the time module itself
    monkeypatch.setattr("time.sleep", fake_sleep)
    monkeypatch.setattr("time.time", fake_time_fn)

    # Patch modules that use 'from time import sleep' or 'from time import time'
    monkeypatch.setattr("fluidics.merfish_operations.sleep", fake_sleep)
    monkeypatch.setattr("fluidics.open_chamber_operations.sleep", fake_sleep)
    monkeypatch.setattr("fluidics.open_chamber_operations.time", fake_time_fn, raising=False)
    monkeypatch.setattr("fluidics.control.controller.sleep", fake_sleep)
    monkeypatch.setattr("fluidics.control.controller.time", fake_time_fn)
    monkeypatch.setattr("fluidics.sequence_utils.sleep", fake_sleep, raising=False)
    monkeypatch.setattr("fluidics.sequence_utils.time", fake_time_fn, raising=False)

    # Patch threading.Event.wait (used by DiscPump.aspirate)
    monkeypatch.setattr(threading.Event, "wait", fake_event_wait)
