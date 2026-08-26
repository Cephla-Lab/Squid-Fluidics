# tests/unit/control/test_syringe_pump_pause.py
"""Pausing a move that is already running.

The Tecan can only terminate a move, so a pause is terminate-and-finish-the-
remainder, inside the driver: the operations layer sees nothing but execute()
returning later than estimated. Ops are dispatched one at a time so the
interrupted op is simply the one in flight -- nothing is inferred from where
the plunger stopped -- and the remainder is an absolute move to the op's
target, fixed from a plunger reading taken before the op started.

Real clock and a real thread where the run has to be parked mid-move: the
fake clock cannot show that a call has not returned. The simulation's
estimate is shortened so the remainder's wait does not pad the suite.
"""

import logging
import threading
import time

import pytest

from fluidics.control.syringe_pump import SyringePumpSimulation
from fluidics.errors import AbortRequested

from .pump_helpers import ScriptedSyringe, bare_pump


def pause_inside_the_first_move(pump):
    """Pause the run from inside the first move's wait -- the shape of an
    operator pressing Pause with the plunger running. Once."""
    original = pump.wait_for_stop
    paused = []

    def wait_for_stop(t=0):
        if not paused:
            paused.append(True)
            pump.run_control.pause()
        return original(t)

    pump.wait_for_stop = wait_for_stop


def until_at_rest(control, timeout=2):
    deadline = time.monotonic() + timeout
    while not control.at_rest and time.monotonic() < deadline:
        time.sleep(0.002)
    assert control.at_rest, "the run never parked"


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
        pump.dispense_to_waste()
        assert pump.get_time_to_finish() == 10

    def test_the_gate_is_passed_even_for_an_empty_chain(self):
        pump = bare_pump(ScriptedSyringe())
        pump.run_control.cancel()
        with pytest.raises(AbortRequested):
            pump.execute()


class TestAPauseMidMove:
    def test_the_move_is_halted_the_run_parks_and_the_remainder_runs_on_resume(
            self, real_clock, run_in_background):
        pump = bare_pump(ScriptedSyringe(position=1500))
        pump.extract(2, 300, 10)          # 180 steps on a 5000 uL syringe
        pump.dispense_to_waste()
        pause_inside_the_first_move(pump)

        finished, error = run_in_background(pump.execute)
        until_at_rest(pump.run_control)
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

    def test_a_dispense_resumes_toward_its_own_target(self, real_clock,
                                                       run_in_background):
        pump = bare_pump(ScriptedSyringe(position=1500))
        pump.dispense(3, 500, 10)         # 300 steps down
        pause_inside_the_first_move(pump)
        finished, error = run_in_background(pump.execute)
        until_at_rest(pump.run_control)
        pump.run_control.resume()
        assert finished.wait(2) and not error, error
        assert pump.syringe.dispatched[1] == [
            "setSpeed(10)", "changePort(3)", "movePlungerAbs(1200)"]

    def test_a_dump_resumes_to_empty_on_the_waste_port(self, real_clock,
                                                        run_in_background):
        pump = bare_pump(ScriptedSyringe(position=1500, waste_port=1))
        pump.dispense_to_waste()
        pause_inside_the_first_move(pump)
        finished, error = run_in_background(pump.execute)
        until_at_rest(pump.run_control)
        pump.run_control.resume()
        assert finished.wait(2) and not error, error
        assert pump.syringe.dispatched[1] == [
            "setSpeed(10)", "changePort(1)", "movePlungerAbs(0)"]

    def test_a_cancel_while_parked_never_restarts_the_pump(self, real_clock,
                                                            run_in_background):
        """First cause wins: an Abort pressed during a paused move unwinds
        the run; nothing is re-issued to the pump."""
        pump = bare_pump(ScriptedSyringe())
        pump.extract(2, 300, 10)
        pause_inside_the_first_move(pump)
        finished, error = run_in_background(pump.execute)
        until_at_rest(pump.run_control)
        pump.run_control.cancel()
        assert finished.wait(2)
        assert isinstance(error[0], AbortRequested), error
        assert len(pump.syringe.dispatched) == 1
        assert pump.syringe.terminated == 1

    def test_the_plunger_must_come_to_rest_before_the_run_parks(self, during_move):
        """Re-issuing onto a still-moving plunger is what the wait exists to
        prevent; a plunger that will not stop is a fault, not a pause."""
        pump = bare_pump(ScriptedSyringe(ready=False))
        pump.extract(2, 300, 10)
        during_move(pump, pump.run_control.pause)
        with pytest.raises(RuntimeError, match="did not stop"):
            pump.execute()
        assert pump.syringe.terminated == 1

    def test_an_unreadable_position_after_the_halt_is_reported_not_raised(
            self, real_clock, run_in_background, caplog):
        """The resume does not depend on that reading -- the target was fixed
        before the op started -- so a decelerating pump that will not answer
        costs the log a line, not the operator their pause."""
        pump = bare_pump(ScriptedSyringe())
        pump.extract(2, 300, 10)
        pause_inside_the_first_move(pump)

        def unreadable():
            raise RuntimeError("no reply while decelerating")

        pump.get_plunger_position = unreadable
        with caplog.at_level(logging.WARNING, logger="fluidics"):
            finished, error = run_in_background(pump.execute)
            until_at_rest(pump.run_control)
            pump.run_control.resume()
            assert finished.wait(2)
        assert "unreadable" in caplog.text
        assert pump.syringe.dispatched[1][-1] == "movePlungerAbs(1680)"


class TestTheSimulationRecordsTheSplit:
    """So an operations test can pin that a paused-and-resumed operation moves
    exactly the liquid an uninterrupted one does."""

    def _pump(self):
        pump = SyringePumpSimulation(sn=None, syringe_ul=5000,
                                     speed_code_limit=10, waste_port=1)
        pump.ESTIMATE_SECONDS = 0.2
        return pump

    def test_a_pause_splits_the_op_where_it_lands(self, real_clock,
                                                   run_in_background):
        pump = self._pump()
        pump.extract(2, 500, 10)
        threading.Timer(0.05, pump.run_control.pause).start()
        finished, error = run_in_background(pump.execute)
        until_at_rest(pump.run_control)

        (piece,) = pump.executed[0]
        assert piece[:2] == ("extract", 2) and piece[3] == 10
        assert 0 < piece[2] < 500, "the split was recorded at one end"
        assert pump.get_current_volume() == pytest.approx(2500 + piece[2])

        pump.run_control.resume()
        assert finished.wait(2) and not error, error
        first, remainder = pump.executed[0]
        assert first[2] + remainder[2] == pytest.approx(500)
        assert pump.get_current_volume() == pytest.approx(3000)

    def test_a_dump_is_recorded_once_and_empties(self, real_clock,
                                                 run_in_background):
        pump = self._pump()
        pump.dispense_to_waste()
        threading.Timer(0.05, pump.run_control.pause).start()
        finished, error = run_in_background(pump.execute)
        until_at_rest(pump.run_control)
        assert 0 < pump.get_current_volume() < 2500, "the held volume did not move"
        pump.run_control.resume()
        assert finished.wait(2) and not error, error
        assert pump.executed == [[("dispense_to_waste", 10)]]
        assert pump.get_current_volume() == 0

    def test_an_uninterrupted_op_is_recorded_exactly_as_queued(self):
        pump = self._pump()
        pump.extract(2, 233.33, 10)
        pump.dispense(3, 0, 10)
        pump.execute()
        assert pump.executed == [[("extract", 2, 233.33, 10), ("dispense", 3, 0, 10)]]
