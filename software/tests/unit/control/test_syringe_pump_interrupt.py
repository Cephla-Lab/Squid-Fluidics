# tests/unit/control/test_syringe_pump_interrupt.py
"""Interrupting a running move.

These use a real thread and a real clock deliberately, which the rest of the
suite avoids -- the autouse _fast_clock fixture patches both time.sleep and
threading.Event.wait, and the point being tested (that a long wait wakes early)
is exactly what those patches would paper over. Under the fake clock the old
time.sleep(t) also "returns instantly", so a timing test written against it
would pass on the broken code. The real_clock fixture undoes both.
"""

import threading
import time

import pytest

from fluidics.control.syringe_pump import SyringePump, SyringePumpSimulation


# Captured at import, before conftest's autouse fixture patches them.
_pristine_wait = threading.Event.wait
_pristine_sleep = time.sleep


@pytest.fixture
def real_clock(monkeypatch):
    """Undo _fast_clock for one test, so elapsed wall time means something."""
    monkeypatch.setattr(threading.Event, "wait", _pristine_wait)
    monkeypatch.setattr("time.sleep", _pristine_sleep)


def _pump():
    return SyringePumpSimulation(sn=None, syringe_ul=5000,
                                 speed_code_limit=10, waste_port=1)


class FakeSyringe:
    """The two calls Interruptible makes into the Tecan driver."""

    def __init__(self, ready=True):
        self.terminated = 0
        self.ready = ready

    def terminateCmd(self):
        self.terminated += 1

    def _checkReady(self):
        return self.ready


def _real_pump(ready=True):
    """A real SyringePump with the hardware left out of its __init__.

    The interrupt logic lives in Interruptible, shared with the simulation, so
    the only thing left to check on this side is that the two hooks are wired
    to the driver. Without this the shipped path had no test at all -- which is
    how the sleep-through-abort bug survived.
    """
    pump = SyringePump.__new__(SyringePump)
    pump.syringe = FakeSyringe(ready=ready)
    pump._init_interrupt()
    return pump


class TestTheRealPumpsHooks:
    def test_stop_halts_the_plunger(self):
        pump = _real_pump()
        pump.stop()
        assert pump.syringe.terminated == 1

    def test_abort_halts_the_plunger(self):
        pump = _real_pump()
        pump.abort()
        assert pump.syringe.terminated == 1

    def test_the_plunger_is_halted_before_the_waiter_is_woken(self):
        """Both run on the interrupting thread while the sequence thread waits.
        Waking first would let the sequence thread resume into get_plunger_position
        -- a second serial round trip -- while terminateCmd is still in flight on
        the same port."""
        pump = _real_pump()
        order = []
        pump.syringe.terminateCmd = lambda: order.append("terminate")
        pump._interrupt.set = lambda: order.append("wake")
        pump.stop()
        assert order == ["terminate", "wake"]

    def test_the_wait_ends_when_the_driver_reports_ready(self):
        pump = _real_pump(ready=True)
        pump.wait_for_stop(0)
        assert pump.is_busy is False

    def test_the_wait_keeps_polling_while_the_driver_is_not_ready(self, real_clock):
        """_checkReady, not the estimate, is what ends the move."""
        pump = _real_pump(ready=False)
        threading.Timer(0.05, pump.stop).start()
        started = time.monotonic()
        pump.wait_for_stop(0)
        assert time.monotonic() - started >= 0.04


def _ran_the_chain(pump):
    """Whether execute() dispatched the chain, rather than returning at the
    abort check. is_busy cannot answer this -- it is False either way by the
    time execute returns."""
    awaited = []
    pump.wait_for_stop = lambda t=0: awaited.append(t)
    pump.execute()
    return bool(awaited)


class TestInterruptWakesTheWait:
    """The bug this closes: wait_for_stop used to be time.sleep(t) where t is
    the pump's estimate for the whole move -- about 240 s for a 2000 uL draw
    at 500 uL/min. An interrupt halted the plunger at once but the caller
    stayed asleep, so the run did not unwind for minutes.
    """

    def test_stop_wakes_a_long_wait_promptly(self, real_clock):
        pump = _pump()

        started = time.monotonic()
        threading.Timer(0.05, pump.stop).start()
        pump.wait_for_stop(3)           # slept the full 3 s before the fix
        elapsed = time.monotonic() - started

        assert elapsed < 1, f"took {elapsed:.1f}s; the wait was not interrupted"

    def test_abort_wakes_a_long_wait_promptly(self, real_clock):
        pump = _pump()

        started = time.monotonic()
        threading.Timer(0.05, pump.abort).start()
        pump.wait_for_stop(3)
        elapsed = time.monotonic() - started

        assert elapsed < 1, f"took {elapsed:.1f}s; the wait was not interrupted"

    def test_an_uninterrupted_wait_still_runs_its_full_duration(self, real_clock):
        """The wait must not return early when nothing interrupted it --
        otherwise the next chain is dispatched onto a still-moving pump."""
        pump = _pump()

        started = time.monotonic()
        pump.wait_for_stop(0.3)
        elapsed = time.monotonic() - started

        assert elapsed >= 0.3


class TestStopDoesNotLatch:
    """stop() is for a fault the caller is about to raise on. abort() means
    the operator cancelled. Conflating them would make a flow fault report as
    a user action, and would silently disable every later operation.
    """

    def test_stop_leaves_is_aborted_false(self):
        pump = _pump()
        pump.stop()
        assert pump.is_aborted is False

    def test_abort_sets_is_aborted(self):
        pump = _pump()
        pump.abort()
        assert pump.is_aborted is True

    def test_a_later_execute_still_runs_after_stop(self):
        """A stopped draw must not disable the run. Contrast abort, which
        deliberately does."""
        pump = _pump()
        pump.stop()
        assert _ran_the_chain(pump)

    def test_a_later_execute_returns_immediately_after_abort(self):
        pump = _pump()
        pump.abort()
        assert not _ran_the_chain(pump)

    def test_reset_abort_clears_the_interrupt(self):
        """Otherwise the next wait returns instantly on a stale event."""
        pump = _pump()
        pump.abort()
        pump.reset_abort()
        assert not pump._interrupt.is_set()


class TestSimulationHonoursInterruption:
    """Before this the simulation ignored is_aborted entirely, so no test
    could exercise an interrupted operation -- which is why the sleep-through
    bug survived 230 passing tests.
    """

    def test_execute_respects_a_prior_abort(self):
        pump = _pump()
        pump.abort()
        assert not _ran_the_chain(pump)

    def test_execute_clears_a_stale_interrupt(self):
        """stop() leaves the event set. If execute did not clear it, the next
        chain's wait would return instantly on the stale event and the caller
        would dispatch onto a pump that had not moved yet."""
        pump = _pump()
        pump.stop()
        pump.execute()
        assert not pump._interrupt.is_set()
