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
