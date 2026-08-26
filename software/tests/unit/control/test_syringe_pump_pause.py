# tests/unit/control/test_syringe_pump_pause.py
"""Pausing a move that is already running.

The Tecan can only terminate a move, so a pause is terminate-and-finish-the-
remainder, inside the driver: the operations layer sees nothing but execute()
returning later than estimated. Ops are dispatched one at a time so the
interrupted op is simply the one in flight -- nothing is inferred from where
the plunger stopped -- and the remainder is an absolute move to the op's
target, fixed from a plunger reading taken before the op started.

Real clock and a real thread where the run has to be parked mid-move (the
`parks` fixture): the fake clock cannot show that a call has not returned.
"""

import logging
import threading

import pytest

from fluidics.errors import AbortRequested, RunControl, SafetyFault

from ...conftest import wait_until
from .pump_helpers import ScriptedSyringe, bare_pump, sim_pump


class TestOneOpAtATime:
    def test_each_op_is_its_own_dispatch(self):
        pump = bare_pump(ScriptedSyringe())
        pump.extract(2, 300, 10)
        pump.dispense_to_waste()
        pump.execute()
        assert pump.syringe.dispatched == [
            ["setSpeed(10)", "extract(2, 300)"],
            ["setSpeed(10)", "dispenseToWaste()"],
        ]

    def test_an_unpaused_move_is_the_relative_move_it_always_was(self):
        """The bytes an unpaused run sends are unchanged: P/D relative moves,
        not the absolute move the remainder uses."""
        pump = bare_pump(ScriptedSyringe())
        pump.dispense(3, 500, 10)
        pump.execute()
        assert pump.syringe.dispatched == [["setSpeed(10)", "dispense(3, 500)"]]

    def test_the_plunger_is_read_before_each_op_and_after_the_last(self):
        """The reading before an op is what fixes its target, should it have
        to be resumed."""
        pump = bare_pump(ScriptedSyringe())
        pump.extract(2, 300, 10)
        pump.dispense_to_waste()
        pump.execute()
        assert pump.syringe.reads == 3

    def test_the_estimate_is_the_drivers_and_leaves_its_chain_empty(self):
        pump = bare_pump(ScriptedSyringe())
        assert pump.extract(2, 300, 10) == 5
        assert pump.syringe._building == [], "the op was left on the driver's chain"

    def test_the_gate_is_passed_even_for_an_empty_chain(self):
        pump = bare_pump(ScriptedSyringe())
        pump.run_control.cancel()
        with pytest.raises(AbortRequested):
            pump.execute()

    def test_moving_is_the_pumps_word_on_a_move_in_flight(self, during_move):
        """What the DrawGuard stands down on: set once the move is sent,
        cleared when it ends."""
        pump = bare_pump(ScriptedSyringe())
        seen = []
        during_move(pump, lambda: seen.append(pump.moving))
        assert pump.moving is False
        pump.extract(2, 300, 10)
        pump.execute()
        assert seen == [True]
        assert pump.moving is False


class TestAPauseMidMove:
    def test_the_move_is_halted_the_run_parks_and_the_remainder_runs_on_resume(
            self, during_move, parks):
        pump = bare_pump(ScriptedSyringe(position=1500))
        pump.extract(2, 300, 10)          # 180 steps on a 5000 uL syringe
        pump.dispense_to_waste()
        during_move(pump, pump.run_control.pause, nth=1)

        finished, error = parks(pump.run_control, pump.execute)
        assert pump.syringe.terminated == 1
        assert pump.syringe.dispatched == [["setSpeed(10)", "extract(2, 300)"]]
        assert not finished.is_set()

        pump.syringe.position = 1600      # where the plunger actually stopped
        pump.run_control.resume()
        assert finished.wait(2) and not error, error
        assert pump.syringe.dispatched[1:] == [
            ["setSpeed(10)", "changePort(2)", "movePlungerAbs(1680)"],
            ["setSpeed(10)", "dispenseToWaste()"],
        ]

    def test_the_target_comes_from_a_fresh_reading_not_the_drivers_prediction(
            self, during_move, parks):
        """The plunger was moved by something else since the op was queued
        (the manual tab, a halt). The driver's chain state still says where
        it predicted the plunger to be; the pump reads before it sends, and
        the target follows the reading."""
        pump = bare_pump(ScriptedSyringe(position=1500))
        pump.extract(2, 300, 10)          # estimated from 1500
        pump.syringe.position = 1400      # ...but the plunger is here now
        during_move(pump, pump.run_control.pause, nth=1)
        finished, error = parks(pump.run_control, pump.execute)
        pump.run_control.resume()
        assert finished.wait(2) and not error, error
        assert pump.syringe.dispatched[1][-1] == "movePlungerAbs(1580)"

    def test_moving_is_cleared_before_the_halt_is_sent(self, during_move, parks):
        """So the flow's decay after the halt is never judged as a fault."""
        pump = bare_pump(ScriptedSyringe())
        pump.extract(2, 300, 10)
        moving_at_halt = []
        terminate = pump.syringe.terminateCmd

        def terminateCmd():
            moving_at_halt.append(pump.moving)
            terminate()

        pump.syringe.terminateCmd = terminateCmd
        during_move(pump, pump.run_control.pause, nth=1)
        finished, error = parks(pump.run_control, pump.execute)
        assert moving_at_halt == [False]
        pump.run_control.resume()
        assert finished.wait(2) and not error, error

    def test_a_dispense_resumes_toward_its_own_target(self, during_move, parks):
        pump = bare_pump(ScriptedSyringe(position=1500))
        pump.dispense(3, 500, 10)         # 300 steps down
        during_move(pump, pump.run_control.pause, nth=1)
        finished, error = parks(pump.run_control, pump.execute)
        pump.run_control.resume()
        assert finished.wait(2) and not error, error
        assert pump.syringe.dispatched[1] == [
            "setSpeed(10)", "changePort(3)", "movePlungerAbs(1200)"]

    def test_a_dump_resumes_to_empty_on_the_waste_port(self, during_move, parks):
        pump = bare_pump(ScriptedSyringe(position=1500, waste_port=1))
        pump.dispense_to_waste()
        during_move(pump, pump.run_control.pause, nth=1)
        finished, error = parks(pump.run_control, pump.execute)
        pump.run_control.resume()
        assert finished.wait(2) and not error, error
        assert pump.syringe.dispatched[1] == [
            "setSpeed(10)", "changePort(1)", "movePlungerAbs(0)"]

    def test_a_cancel_while_parked_never_restarts_the_pump(self, during_move, parks):
        """First cause wins: an Abort pressed during a paused move unwinds
        the run; nothing is re-issued to the pump."""
        pump = bare_pump(ScriptedSyringe())
        pump.extract(2, 300, 10)
        during_move(pump, pump.run_control.pause, nth=1)
        finished, error = parks(pump.run_control, pump.execute)
        pump.run_control.cancel()
        assert finished.wait(2)
        assert isinstance(error[0], AbortRequested), error
        assert len(pump.syringe.dispatched) == 1
        assert pump.syringe.terminated == 1

    def test_a_plunger_that_will_not_stop_is_a_fault_that_ends_the_run(self, during_move):
        """Re-issuing onto a still-moving plunger is what the wait exists to
        prevent. It ends the run as the instrument's own fault -- through
        cancel(), so the gate opens and nothing downstream can park behind a
        pause that no longer means anything."""
        pump = bare_pump(ScriptedSyringe(ready=False))
        pump.extract(2, 300, 10)
        during_move(pump, pump.run_control.pause)
        with pytest.raises(SafetyFault, match="did not stop"):
            pump.execute()
        assert pump.syringe.terminated == 1
        assert pump.run_control.cancelled and not pump.run_control.paused

    def test_an_unreadable_position_after_the_halt_is_reported_not_raised(
            self, during_move, parks, caplog):
        """The resume does not depend on that reading -- the target was fixed
        before the op started -- so a decelerating pump that will not answer
        costs the log a line, not the operator their pause."""
        pump = bare_pump(ScriptedSyringe())
        pump.extract(2, 300, 10)
        during_move(pump, pump.run_control.pause, nth=1)

        def unreadable():
            raise RuntimeError("no reply while decelerating")

        pump.get_plunger_position = unreadable
        with caplog.at_level(logging.WARNING, logger="fluidics"):
            finished, error = parks(pump.run_control, pump.execute)
            pump.run_control.resume()
            assert finished.wait(2)
        assert "unreadable" in caplog.text
        assert pump.syringe.dispatched[1][-1] == "movePlungerAbs(1680)"


class TestTheSimulationRecordsTheSplit:
    """So an operations test can pin that a paused-and-resumed operation moves
    exactly the liquid an uninterrupted one does."""

    def _pump(self):
        pump = sim_pump()
        pump.ESTIMATE_SECONDS = 0.1
        return pump

    def test_a_pause_splits_the_op_where_it_lands(self, parks):
        pump = self._pump()
        pump.extract(2, 500, 10)
        threading.Timer(0.02, pump.run_control.pause).start()
        finished, error = parks(pump.run_control, pump.execute)

        (piece,) = pump.executed[0]
        assert piece[:2] == ("extract", 2) and piece[3] == 10
        assert 0 < piece[2] < 500, "the split was recorded at one end"
        assert pump.get_current_volume() == pytest.approx(2500 + piece[2])

        pump.run_control.resume()
        assert finished.wait(2) and not error, error
        first, remainder = pump.executed[0]
        assert first[2] + remainder[2] == pytest.approx(500)
        assert pump.get_current_volume() == 3000

    def test_the_remainder_waits_only_its_share_of_the_estimate(self, parks):
        """A --simulation run must not sit through the whole estimate again
        after a pause; only what was left of it."""
        pump = self._pump()
        pump.extract(2, 500, 10)
        waits = []
        original = pump.wait_for_stop
        pump.wait_for_stop = lambda t=0: waits.append(t) or original(t)
        threading.Timer(0.02, pump.run_control.pause).start()
        finished, error = parks(pump.run_control, pump.execute)
        pump.run_control.resume()
        assert finished.wait(2) and not error, error
        assert waits[0] == 0.1
        assert 0 < waits[1] < 0.1

    def test_a_dump_is_recorded_once_and_empties(self, parks):
        pump = self._pump()
        pump.dispense_to_waste()
        threading.Timer(0.02, pump.run_control.pause).start()
        finished, error = parks(pump.run_control, pump.execute)
        assert 0 < pump.get_current_volume() < 2500, "the held volume did not move"
        pump.run_control.resume()
        assert finished.wait(2) and not error, error
        assert pump.executed == [[("dispense_to_waste", 10)]]
        assert pump.get_current_volume() == 0

    def test_under_the_fake_clock_the_split_lands_at_the_end(self, during_move):
        """Every fake-clock wait "takes" its whole estimate, so the op is
        complete by the time the pause is seen: recorded once, in full, with
        no empty remainder after the resume. The gate resumes itself, since
        nothing else could on the one thread."""
        class SelfResuming(RunControl):
            def checkpoint(self):
                self.resume()
                super().checkpoint()

        pump = sim_pump(run_control=SelfResuming())
        pump.extract(2, 500, 10)
        during_move(pump, pump.run_control.pause, nth=1)
        pump.execute()
        assert pump.executed == [[("extract", 2, 500, 10)]]
        assert pump.get_current_volume() == 3000

    def test_an_uninterrupted_op_is_recorded_exactly_as_queued(self):
        pump = self._pump()
        pump.extract(2, 233.33, 10)
        pump.dispense(3, 0, 10)
        pump.execute()
        assert pump.executed == [[("extract", 2, 233.33, 10), ("dispense", 3, 0, 10)]]
