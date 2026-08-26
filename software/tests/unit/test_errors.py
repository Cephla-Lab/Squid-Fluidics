# tests/unit/test_errors.py
"""The exception taxonomy and the run's one cancellation signal."""

import threading
import time

import pytest

from ..conftest import SETTLE
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

    def test_checkpoint_holds_until_resume(self, holds_while_paused):
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

    def test_run_for_reports_the_time_it_spent_off_the_monotonic_clock(
            self, real_clock, monkeypatch):
        """The wall clock is frozen here: an NTP correction, or an operator
        setting the clock back, must not make a wait look like it took no time
        -- delay() would then spend the whole interval again, and an
        incubation would start over."""
        control = RunControl()
        monkeypatch.setattr("time.time", lambda: 0.0)     # a wall clock going nowhere
        assert control.run_for(0.03) == pytest.approx(0.03, abs=0.02)

    def test_delay_does_not_count_paused_time(self, real_clock):
        """The point of pause: an incubation held for a coffee break resumes
        with its remaining time, it does not expire during the break."""
        control = RunControl()
        control.pause()
        threading.Timer(0.05, control.resume).start()
        started = time.monotonic()
        control.delay(0.02)
        assert time.monotonic() - started >= 0.06

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


class TestWaitInterrupted:
    """The wait for a device that can stop a move and finish it later: it
    wakes on a pause as well as a cancel, where wait() wakes on cancel only."""

    def test_a_pause_wakes_it(self, real_clock):
        control = RunControl()
        control.pause()
        started = time.monotonic()
        assert control.wait_interrupted(2) is True
        assert time.monotonic() - started < 0.5

    def test_a_cancel_wakes_it(self):
        control = RunControl()
        control.cancel()
        assert control.wait_interrupted(2) is True

    def test_an_undisturbed_wait_runs_out(self, real_clock):
        control = RunControl()
        started = time.monotonic()
        assert control.wait_interrupted(0.02) is False
        assert time.monotonic() - started >= 0.015

    def test_a_resumed_run_no_longer_wakes_it(self, real_clock):
        control = RunControl()
        control.pause()
        control.resume()
        assert control.wait_interrupted(0.02) is False


class TestWhenHeld:
    """A region whose hooks run around an actual park: for the caller that
    has something powered while a gated call runs -- the drain under a
    dispense -- so what is powered can follow the run into the hold and out."""

    def _hooked(self):
        control = RunControl()
        events = []
        region = control.when_held(lambda: events.append("hold"),
                                   lambda: events.append("release"))
        return control, events, region

    def test_the_hooks_run_around_a_park(self, holds_while_paused):
        control, events, region = self._hooked()

        def gate():
            with region:
                control.checkpoint()

        def held_so_far():
            assert events == ["hold"]

        holds_while_paused(control, gate, while_held=held_so_far)
        assert events == ["hold", "release"]

    def test_the_hold_hook_runs_before_the_park_and_the_release_after(
            self, real_clock, run_in_background):
        control, events, region = self._hooked()
        control.pause()

        def gate():
            with region:
                control.checkpoint()

        finished, error = run_in_background(gate)
        deadline = time.monotonic() + 2
        # Wait for the park itself, not for the hook: on_hold() runs before the
        # park is counted, so seeing the hook does not yet mean at_rest.
        while not control.at_rest and time.monotonic() < deadline:
            time.sleep(0.005)
        assert control.at_rest, "the run never parked"
        assert events == ["hold"], events
        control.resume()
        assert finished.wait(2)
        assert events == ["hold", "release"] and not error

    def test_a_gate_the_run_walks_through_runs_no_hooks(self):
        control, events, region = self._hooked()
        with region:
            control.checkpoint()
        assert events == []

    def test_a_cancel_while_parked_skips_the_release(self, real_clock,
                                                    run_in_background):
        """The drain must not come back on for a run that is unwinding."""
        control, events, region = self._hooked()
        control.pause()

        def gate():
            with region:
                control.checkpoint()

        finished, error = run_in_background(gate)
        deadline = time.monotonic() + 2
        while events != ["hold"] and time.monotonic() < deadline:
            time.sleep(0.005)
        control.cancel()
        assert finished.wait(2)
        assert events == ["hold"]
        assert isinstance(error[0], AbortRequested)

    def test_the_region_ends_and_the_hooks_stop(self, real_clock,
                                                run_in_background):
        control, events, region = self._hooked()
        with region:
            pass
        control.pause()
        finished, _ = run_in_background(control.checkpoint)
        assert not finished.wait(SETTLE)
        control.resume()
        assert finished.wait(2)
        assert events == []

    def test_the_hooks_are_per_thread(self, real_clock, run_in_background):
        """Another thread's gate parks too, but it is not inside the region
        and must not switch this thread's hardware."""
        control, events, region = self._hooked()
        control.pause()
        with region:
            finished, _ = run_in_background(control.checkpoint)
            assert not finished.wait(SETTLE)
            control.resume()
            assert finished.wait(2)
        assert events == []

    def test_nested_regions_release_innermost_first(self, real_clock,
                                                    run_in_background):
        control = RunControl()
        events = []
        outer = control.when_held(lambda: events.append("hold outer"),
                                  lambda: events.append("release outer"))
        inner = control.when_held(lambda: events.append("hold inner"),
                                  lambda: events.append("release inner"))
        control.pause()

        def gate():
            with outer, inner:
                control.checkpoint()

        finished, error = run_in_background(gate)
        deadline = time.monotonic() + 2
        while len(events) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        control.resume()
        assert finished.wait(2) and not error
        assert events == ["hold outer", "hold inner",
                          "release inner", "release outer"]


class TestAtRest:
    """"Asked to pause" and "actually stopped" are different moments, and the
    GUI shows the difference. Composed here so everything reporting a pause
    agrees."""

    def test_a_running_control_is_not_at_rest(self):
        assert not RunControl().at_rest

    def test_a_gate_the_run_walks_through_is_never_counted(self):
        """The gate is passed on every chain, valve move and drain start. If
        each pass counted, another thread sampling `holding` mid-pass would
        see a run going full tilt as a stopped one -- and the GUI would tell
        the operator no liquid was moving.

        Sampled from inside the gate, because that is the only place the
        difference shows: a pass that counts increments and decrements within
        the one call.
        """
        control = RunControl()
        inside = []
        waiting = control._running.wait

        def wait(*args):
            inside.append(control.holding)
            return waiting(*args)

        control._running.wait = wait
        control.checkpoint()
        assert all(seen == 0 for seen in inside), \
            "a gate the run walked straight through was counted"
        assert control.holding == 0 and not control.at_rest

    def test_a_pause_alone_is_not_yet_at_rest(self):
        """The move in flight keeps going until it reaches a gate."""
        control = RunControl()
        control.pause()
        assert control.paused and not control.at_rest

    def test_a_parked_thread_is_at_rest(self, real_clock, run_in_background):
        control = RunControl()
        control.pause()
        finished, _ = run_in_background(control.checkpoint)
        deadline = time.monotonic() + 2
        while not control.at_rest and time.monotonic() < deadline:
            time.sleep(0.005)
        assert control.at_rest
        control.resume()
        assert finished.wait(2)
        assert not control.at_rest

    def test_a_resumed_run_is_not_at_rest_even_before_the_thread_wakes(self):
        """at_rest is paused *and* parked, so the moment the operator resumes
        the run counts as going again -- the label must not keep saying
        "paused" while a thread is on its way out of the gate."""
        control = RunControl()
        control.pause()
        control._holding = 1          # a thread still inside the gate
        control.resume()
        assert not control.at_rest


class TestHolding:
    """"Pause requested" and "the run has come to rest" are different moments:
    a move in flight keeps going until it reaches a gate. The GUI tells the
    operator which, so RunControl has to say."""

    def test_nothing_is_holding_on_a_running_control(self):
        assert RunControl().holding == 0

    def test_a_pause_alone_is_not_yet_a_hold(self):
        control = RunControl()
        control.pause()
        assert control.paused and control.holding == 0

    def test_a_thread_parked_at_the_gate_counts(self, real_clock, run_in_background):
        control = RunControl()
        control.pause()
        finished, _ = run_in_background(control.checkpoint)
        deadline = time.monotonic() + 2
        while control.holding == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert control.holding == 1
        control.resume()
        assert finished.wait(2)
        assert control.holding == 0
