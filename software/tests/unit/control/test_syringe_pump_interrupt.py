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

from fluidics.control.syringe_pump import SyringePumpSimulation
from fluidics.errors import AbortRequested, RunControl, SafetyFault

from .pump_helpers import ScriptedSyringe, bare_pump, halt_on_cancel


def _pump(**kwargs):
    return SyringePumpSimulation(sn=None, syringe_ul=5000,
                                 speed_code_limit=10, waste_port=1, **kwargs)


def _real_pump(ready=True):
    """A real SyringePump around a driver fake -- see pump_helpers.

    The interrupt logic lives in Interruptible, shared with the simulation, so
    the only thing left to check on this side is that the hooks are wired to
    the driver. Without this the shipped path had no test at all -- which is
    how the sleep-through-abort bug survived.
    """
    return bare_pump(ScriptedSyringe(ready=ready))


class TestTheRealPumpsHooks:
    def test_halt_halts_the_plunger(self):
        pump = _real_pump()
        pump.halt()
        assert pump.syringe.terminated == 1

    def test_a_cancel_touches_nothing_with_no_move_in_flight(self):
        """The halt belongs to the thread inside the wait; with nothing
        waiting there is nothing to halt."""
        pump = _real_pump()
        pump.run_control.cancel()
        assert pump.syringe.terminated == 0

    def test_the_wait_ends_when_the_driver_reports_ready(self):
        pump = _real_pump(ready=True)
        assert pump.wait_for_stop(0) is False, "reported as cut short"

    def test_the_wait_keeps_polling_while_the_driver_is_not_ready(self, real_clock):
        """_checkReady, not the estimate, is what ends the move."""
        pump = _real_pump(ready=False)
        threading.Timer(0.05, pump.run_control.cancel).start()
        started = time.monotonic()
        with pytest.raises(AbortRequested):
            pump.wait_for_stop(0)
        assert time.monotonic() - started >= 0.04


class TestACancelWakesTheWait:
    """The bug this closes: wait_for_stop used to be time.sleep(t) where t is
    the pump's estimate for the whole move -- about 240 s for a 2000 uL draw
    at 500 uL/min. A halt from another thread stopped the plunger at once but
    the caller stayed asleep, so the run did not unwind for minutes.
    """

    def test_a_cancel_wakes_a_long_wait_promptly_and_raises(self, real_clock):
        pump = _pump()

        started = time.monotonic()
        threading.Timer(0.05, pump.run_control.cancel).start()
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


class TestTheSignal:
    """The pump waits on the run's RunControl -- the object every waiting
    device shares -- and raises whatever cause it carries."""

    def test_a_later_execute_raises_after_a_cancel(self):
        """Raises rather than returns: a silent no-op is how an aborted
        operation used to report success."""
        pump = _pump()
        pump.run_control.cancel()
        with pytest.raises(AbortRequested):
            pump.execute()

    def test_a_reset_lets_the_next_chain_run(self):
        pump = _pump()
        pump.run_control.cancel()
        pump.run_control.reset()
        # is_busy cannot answer whether the chain was dispatched -- it is
        # False either way by the time execute returns.
        awaited = []
        pump.wait_for_stop = lambda t=0: awaited.append(t)
        pump.extract(2, 100, 10)
        pump.execute()
        assert awaited == [5]

    def test_a_pump_built_alone_gets_its_own(self):
        assert _pump().run_control is not _pump().run_control

    def test_the_pump_waits_on_the_signal_it_was_built_with(self):
        control = RunControl()
        pump = _pump(run_control=control)
        control.cancel()
        with pytest.raises(AbortRequested):
            pump.wait_for_stop(3)

    def test_a_safety_fault_is_raised_as_itself(self):
        """Draw protection cancels with a FlowFault; the wait must raise that
        fault, not translate it into an abort."""
        pump = _pump()
        pump.run_control.cancel(SafetyFault("flow collapsed"))
        with pytest.raises(SafetyFault, match="flow collapsed"):
            pump.wait_for_stop(0)


class TestTheCancelPathOnTheRealPump:
    def test_a_cancel_mid_move_is_halted_by_the_waiting_thread(self, during_move):
        """No I/O on the cancelling thread: the thread inside the wait halts
        the plunger, then raises. The same branch closes the window between
        _arm() and dispatch -- a cancel landing there returns from the wait at
        once and halts the move it could not prevent."""
        pump = _real_pump(ready=False)
        pump.extract(2, 100, 10)
        during_move(pump, pump.run_control.cancel)
        with pytest.raises(AbortRequested):
            pump.execute()
        assert pump.syringe.terminated == 1

    def test_a_failed_halt_is_logged_and_the_cancel_still_raises(self, caplog):
        """If terminateCmd fails, the cancellation must still reach the worker
        -- its safety cleanup depends on it -- with the failure on record."""
        pump = _real_pump(ready=True)

        def broken():
            raise IOError("no reply from pump")

        pump.syringe.terminateCmd = broken
        with caplog.at_level(logging.ERROR, logger="fluidics"):
            halt_on_cancel(pump)
        assert "no reply from pump" in caplog.text

    def test_a_cancelled_run_never_dispatches_a_chain(self):
        """Without the entry check an execute() after a cancel would send the
        chain to the Tecan and wait out the whole move before raising."""
        pump = _real_pump()
        pump.extract(2, 100, 10)
        pump.run_control.cancel()
        with pytest.raises(AbortRequested):
            pump.execute()
        assert pump.syringe.dispatched == []

    def test_an_unreadable_position_does_not_replace_the_cancellation(self, caplog, during_move):
        """After terminateCmd the plunger is still decelerating and the
        position read can fail. A driver error surfacing here would show the
        operator a pump fault instead of their own abort."""
        pump = _real_pump(ready=False)
        pump.extract(2, 5, 10)
        reads = []

        def unreadable():
            reads.append(True)
            raise RuntimeError("no reply while decelerating")

        pump.get_plunger_position = unreadable
        during_move(pump, pump.run_control.cancel)
        with caplog.at_level(logging.WARNING, logger="fluidics"):
            with pytest.raises(AbortRequested):
                pump.execute()
        assert reads == [True]
        assert pump.get_chained_volume() == 0
        assert "unreadable" in caplog.text


class TestPauseHoldsTheNextChain:
    """A run paused before a chain is dispatched holds at the gate; the move
    in flight is the pause tests' business (test_syringe_pump_pause)."""

    def test_execute_holds_while_paused_and_runs_on_resume(self, holds_while_paused):
        pump = _pump()
        dispatched = []
        pump.wait_for_stop = lambda t=0: dispatched.append(t)
        pump.extract(2, 100, 10)
        holds_while_paused(pump.run_control, pump.execute)
        assert dispatched, "the chain never ran after the resume"

    def test_a_cancel_beats_a_pending_pause(self):
        """The cancel clears the pause on its way past, so the gate raises
        rather than holding. (A thread already parked at the gate is woken by
        the same act -- pinned in test_errors and the pause integration test.)"""
        pump = _pump()
        pump.run_control.pause()
        pump.run_control.cancel()
        with pytest.raises(AbortRequested):
            pump.execute()
