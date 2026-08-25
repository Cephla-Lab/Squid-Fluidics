# tests/unit/control/test_syringe_pump_interrupt.py
"""Interrupting a running move.

These use a real thread and a real clock deliberately, which the rest of the
suite avoids -- the autouse _fast_clock fixture patches both time.sleep and
threading.Event.wait, and the point being tested (that a long wait wakes early)
is exactly what those patches would paper over. Under the fake clock the old
time.sleep(t) also "returns instantly", so a timing test written against it
would pass on the broken code. The real_clock fixture (tests/conftest.py)
undoes both.
"""

import logging
import threading
import time

import pytest

from fluidics.control.syringe_pump import SyringePump, SyringePumpSimulation
from fluidics.errors import AbortRequested, RunControl

from .pump_helpers import bare_pump


def _pump(**kwargs):
    return SyringePumpSimulation(sn=None, syringe_ul=5000,
                                 speed_code_limit=10, waste_port=1, **kwargs)


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
    """A real SyringePump around a driver fake -- see pump_helpers.

    The interrupt logic lives in Interruptible, shared with the simulation, so
    the only thing left to check on this side is that the two hooks are wired
    to the driver. Without this the shipped path had no test at all -- which is
    how the sleep-through-abort bug survived.
    """
    return bare_pump(FakeSyringe(ready=ready))


class TestTheRealPumpsHooks:
    def test_stop_halts_the_plunger(self):
        pump = _real_pump()
        pump.stop()
        assert pump.syringe.terminated == 1

    def test_abort_touches_nothing_with_no_move_in_flight(self):
        """abort() only cancels; the halt belongs to the thread inside the
        wait, and with nothing waiting there is nothing to halt."""
        pump = _real_pump()
        pump.abort()
        assert pump.syringe.terminated == 0

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

    def test_abort_wakes_a_long_wait_promptly_and_raises(self, real_clock):
        pump = _pump()

        started = time.monotonic()
        threading.Timer(0.05, pump.abort).start()
        with pytest.raises(AbortRequested):
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

    def test_a_later_execute_raises_after_abort(self):
        """Raises rather than returns: a silent no-op is how an aborted
        operation used to report success."""
        pump = _pump()
        pump.abort()
        with pytest.raises(AbortRequested):
            pump.execute()

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

    def test_execute_clears_a_stale_interrupt(self):
        """stop() leaves the event set. If execute did not clear it, the next
        chain's wait would return instantly on the stale event and the caller
        would dispatch onto a pump that had not moved yet."""
        pump = _pump()
        pump.stop()
        pump.execute()
        assert not pump._interrupt.is_set()


class TestTheSharedRunControl:
    """abort() speaks through the run's RunControl, the object every waiting
    device of the run shares; stop() deliberately does not."""

    def test_abort_cancels_the_run_control_the_pump_was_built_with(self):
        control = RunControl()
        pump = _pump(run_control=control)
        pump.abort()
        assert isinstance(control.cause, AbortRequested)

    def test_reset_abort_resets_it(self):
        pump = _pump()
        pump.abort()
        pump.reset_abort()
        assert not pump.run_control.cancelled

    def test_stop_does_not_cancel_the_run(self):
        pump = _pump()
        pump.stop()
        assert not pump.run_control.cancelled

    def test_a_pump_built_alone_gets_its_own(self):
        assert _pump().run_control is not _pump().run_control

    def test_a_cancel_wakes_the_waiter_through_the_waker(self):
        """The wait blocks on the pump's own event (stop() sets it too); a
        cancel from anywhere reaches that event through the waker."""
        pump = _pump()
        pump.run_control.cancel()
        assert pump._interrupt.is_set()


class TestTheCancelPathOnTheRealPump:
    def test_a_cancel_mid_move_is_halted_by_the_waiting_thread(self, during_move):
        """No I/O on the cancelling thread: the thread inside the wait halts
        the plunger, then raises."""
        pump = _real_pump(ready=False)
        pump.syringe.executeChain = lambda minimal_reset=True: 0
        during_move(pump, pump.abort)
        with pytest.raises(AbortRequested):
            pump.execute()
        assert pump.syringe.terminated == 1

    def test_a_cancel_landing_after_dispatch_still_halts_the_move(self):
        """The window between _arm() and executeChain: a cancel there could
        not stop the dispatch, so the wait must halt the move that just
        started before it raises. (ready=True keeps this from spinning if
        the waker is ever lost; the waker itself is pinned separately.)"""
        pump = _real_pump(ready=True)

        def dispatch_then_cancel(minimal_reset=True):
            pump.run_control.cancel()
            return 240

        pump.syringe.executeChain = dispatch_then_cancel
        with pytest.raises(AbortRequested):
            pump.execute()
        assert pump.syringe.terminated == 1

    def test_a_cancelled_run_never_dispatches_a_chain(self):
        """The entry check exists for the real pump: _arm() clears the wake
        event first, so without it an execute() after an abort would send the
        chain to the Tecan and then wait out the whole move before raising."""
        pump = _real_pump()
        dispatched = []
        pump.syringe.executeChain = lambda minimal_reset=True: dispatched.append(True) or 0
        pump.abort()
        with pytest.raises(AbortRequested):
            pump.execute()
        assert dispatched == []

    def test_an_unreadable_position_does_not_replace_the_cancellation(self, caplog, during_move):
        """After terminateCmd the plunger is still decelerating and the
        position read can fail. A driver error surfacing here would show the
        operator a pump fault instead of their own abort."""
        pump = _real_pump(ready=False)
        pump.syringe.executeChain = lambda minimal_reset=True: 0
        pump.chained_volume = 5
        reads = []

        def unreadable():
            reads.append(True)
            raise RuntimeError("no reply while decelerating")

        pump.get_plunger_position = unreadable
        during_move(pump, pump.abort)
        with caplog.at_level(logging.WARNING, logger="fluidics"):
            with pytest.raises(AbortRequested):
                pump.execute()
        assert reads == [True]
        assert pump.chained_volume == 0
        assert "unreadable" in caplog.text
