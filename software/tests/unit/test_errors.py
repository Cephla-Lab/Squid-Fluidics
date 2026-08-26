# tests/unit/test_errors.py
"""The exception taxonomy and the run's one cancellation signal."""

import threading
import time

import pytest

from fluidics.errors import (AbortRequested, Cancelled, FluidicsError,
                             OperationError, RunControl, SafetyFault)


class TestTaxonomy:
    def test_an_abort_and_a_safety_fault_are_both_cancellations(self):
        assert issubclass(AbortRequested, Cancelled)
        assert issubclass(SafetyFault, Cancelled)

    def test_a_safety_fault_is_not_an_abort(self):
        """The worker reports an abort as 'aborted by user'; a fault reported
        that way would lose exactly the diagnosis it exists to deliver."""
        assert not issubclass(SafetyFault, AbortRequested)

    def test_a_failed_step_is_not_a_cancellation(self):
        assert not issubclass(OperationError, Cancelled)

    def test_everything_is_a_fluidics_error(self):
        for cls in (OperationError, Cancelled, AbortRequested, SafetyFault):
            assert issubclass(cls, FluidicsError)


class TestRunControl:
    def test_a_fresh_signal_is_clear(self):
        control = RunControl()
        assert not control.cancelled
        control.check()
        assert control.wait(5) is False

    def test_cancel_defaults_to_the_operator_abort(self):
        control = RunControl()
        control.cancel()
        assert control.cancelled
        assert isinstance(control.cause, AbortRequested)
        with pytest.raises(AbortRequested):
            control.check()

    def test_wait_returns_true_once_cancelled(self):
        control = RunControl()
        control.cancel()
        assert control.wait(5) is True

    def test_sleep_raises_the_cause(self):
        control = RunControl()
        control.cancel()
        with pytest.raises(AbortRequested):
            control.sleep(5)

    def test_first_cause_wins(self):
        """The operator's reflex after a flow alarm is to press Abort a second
        later; the fault must survive that."""
        control = RunControl()
        fault = SafetyFault("flow collapsed")
        assert control.cancel(fault) is True
        assert control.cancel() is False
        assert control.cause is fault
        with pytest.raises(SafetyFault, match="flow collapsed"):
            control.check()

    def test_reset_clears_both_cause_and_signal(self):
        control = RunControl()
        control.cancel(SafetyFault("x"))
        control.reset()
        assert not control.cancelled
        assert control.cause is None
        control.check()

    def test_only_a_cancellation_can_be_the_cause(self):
        """Callers catch Cancelled, the base; a cause outside it would slip
        past every one of them."""
        control = RunControl()
        with pytest.raises(TypeError):
            control.cancel(RuntimeError("not a cancellation"))
        assert not control.cancelled

    def test_cancel_wakes_a_waiting_thread_promptly(self, real_clock):
        control = RunControl()
        started = time.monotonic()
        threading.Timer(0.05, control.cancel).start()
        assert control.wait(3) is True
        assert time.monotonic() - started < 1


class TestPause:
    """The gate half. Cancel latches and raises; pause holds and never does.
    They share an object because every wait must answer to both -- an Abort
    pressed while paused has to unwind rather than deadlock.
    """

    def test_a_fresh_control_is_running(self):
        control = RunControl()
        assert not control.paused
        control.checkpoint()          # returns at once

    def test_pause_then_resume(self):
        control = RunControl()
        assert control.pause() is True
        assert control.paused
        assert control.pause() is False        # already paused
        assert control.resume() is True
        assert not control.paused
        assert control.resume() is False       # already running

    def test_a_cancelled_run_cannot_be_paused(self):
        """The gate must never close on a thread that is unwinding."""
        control = RunControl()
        control.cancel()
        assert control.pause() is False
        assert not control.paused

    def test_reset_clears_pause_too(self):
        """A run never starts already stopped."""
        control = RunControl()
        control.pause()
        control.reset()
        assert not control.paused
        control.checkpoint()

    def test_checkpoint_holds_until_resume(self, real_clock, holds_while_paused):
        control = RunControl()
        holds_while_paused(control, control.checkpoint)

    def test_a_cancel_opens_the_gate_and_raises(self, real_clock):
        """Abort while paused: the operator must not have to resume first."""
        control = RunControl()
        control.pause()
        threading.Timer(0.05, control.cancel).start()
        with pytest.raises(AbortRequested):
            control.checkpoint()
        assert not control.paused

    def test_a_cancelled_run_never_holds_at_the_gate(self):
        control = RunControl()
        control.pause()
        control.cancel()
        with pytest.raises(AbortRequested):
            control.checkpoint()      # would deadlock if the gate stayed shut

    def test_run_for_returns_early_when_the_run_is_paused(self, real_clock):
        control = RunControl()
        control.pause()
        assert control.run_for(30) == 0.0

    def test_run_for_reports_the_time_it_spent(self, real_clock):
        control = RunControl()
        assert control.run_for(0.05) == pytest.approx(0.05, abs=0.03)

    def test_delay_does_not_count_paused_time(self, real_clock):
        """The point of pause: an incubation held for a coffee break resumes
        with its remaining time, it does not expire during the break."""
        control = RunControl()
        control.pause()
        threading.Timer(0.15, control.resume).start()
        started = time.monotonic()
        control.delay(0.05)
        assert time.monotonic() - started >= 0.19

    def test_delay_raises_when_the_run_is_cancelled(self, real_clock):
        control = RunControl()
        threading.Timer(0.05, control.cancel).start()
        with pytest.raises(AbortRequested):
            control.delay(30)

    def test_wait_ignores_pause(self, real_clock):
        """Hardware polls must not stop: a command already in flight has to be
        waited out whether or not the operator has paused."""
        control = RunControl()
        control.pause()
        started = time.monotonic()
        assert control.wait(0.05) is False
        assert time.monotonic() - started >= 0.04
