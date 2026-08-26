# tests/unit/control/test_disc_pump.py
"""The disc pump's timed aspiration against the run's cancellation signal."""

import threading
import time

import pytest

from fluidics.control._def import CMD_SET, MCU_CONSTANTS
from fluidics.control.disc_pump import DiscPump
from fluidics.errors import AbortRequested, RunControl


class RecordingController:
    def __init__(self):
        self.sent = []
        self.timeouts = []

    def send_command(self, command, *args):
        self.sent.append((command, *args))

    def send_command_blocking(self, command, *args, timeout=30):
        self.sent.append((command, *args))
        self.timeouts.append(timeout)


@pytest.fixture
def pump():
    return DiscPump(RecordingController(), RunControl())


def power_commands(pump):
    return [args[0] for command, *args in pump.fc.sent
            if command == CMD_SET.SET_PUMP_PWR_OPEN_LOOP]


class TestAspirate:
    def test_full_power_for_the_duration_then_off(self, pump):
        spent = []
        pump.run_control.run_for = lambda seconds: spent.append(seconds) or seconds
        pump.aspirate(20)
        assert spent == [20]
        assert power_commands(pump) == [MCU_CONSTANTS.TTP_MAX_PW, 0]

    def test_a_pause_switches_the_pump_off_and_the_remainder_resumes_with_it(self, pump):
        """Twenty seconds of aspiration held for a coffee break must not drain
        the chamber for the length of the break."""
        control = pump.run_control
        spent = []

        def run_for(seconds):
            spent.append(seconds)
            if len(spent) == 1:
                control.pause()       # five seconds in, the operator pauses
                return 5
            return seconds

        control.run_for = run_for
        original_checkpoint = control.checkpoint
        control.checkpoint = lambda: (control.resume(), original_checkpoint())[1]

        pump.aspirate(20)
        assert spent == [20, 15]      # the remainder, not the whole twenty
        assert power_commands(pump) == [MCU_CONSTANTS.TTP_MAX_PW, 0,
                                        MCU_CONSTANTS.TTP_MAX_PW, 0]

    def test_a_cancel_before_it_starts_raises_without_touching_the_pump(self, pump):
        """A pre-tripped signal used to produce a full-power pulse followed at
        once by power 0."""
        pump.run_control.cancel()
        with pytest.raises(AbortRequested):
            pump.aspirate(20)
        assert power_commands(pump) == []

    def test_a_cancel_mid_way_switches_off_then_raises(self, pump, cancel_during_wait):
        cancel_during_wait(pump.run_control)
        with pytest.raises(AbortRequested):
            pump.aspirate(20)
        assert power_commands(pump) == [MCU_CONSTANTS.TTP_MAX_PW, 0]



class TestStartAndStop:
    def test_start_refuses_on_a_cancelled_run_without_powering_the_pump(self, pump):
        """The drain is started before the syringe pump's checked execute; on a
        cancelled run it would otherwise be powered for the moment before that
        call unwinds."""
        pump.run_control.cancel()
        with pytest.raises(AbortRequested):
            pump.start(0.3)
        assert power_commands(pump) == []

    def test_start_holds_while_the_run_is_paused(
            self, pump, real_clock, holds_while_paused):
        holds_while_paused(pump.run_control, lambda: pump.start(0.3))
        assert power_commands(pump) == [0.3 * MCU_CONSTANTS.TTP_MAX_PW]

    def test_stop_still_works_on_a_cancelled_run(self, pump):
        """It runs on the unwind path -- the drain's own finally -- and from
        DeviceSet.make_safe, both after the cancel."""
        pump.start(0.3)
        pump.run_control.cancel()
        pump.stop()
        assert power_commands(pump)[-1] == 0


def test_power_commands_wait_seconds_not_the_default_thirty(pump):
    """A power command completes within one MCU status interval; the 30 s
    default only ever costs time on a dead MCU -- with make_safe waiting."""
    pump.start(0.3)
    pump.stop()
    assert pump.fc.timeouts == [2, 2]


def test_a_pump_built_alone_gets_a_private_run_control():
    one = DiscPump(RecordingController())
    another = DiscPump(RecordingController())
    assert one.run_control is not another.run_control
