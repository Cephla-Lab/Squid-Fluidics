import time

import pytest

from fluidics.control.temperature_controller import TCMControllerSimulation
from fluidics.errors import AbortRequested, OperationError, RunControl
from fluidics.sequence_utils import set_temperature


class _StuckController:
    """Test stub: targets are stored, but actuals never converge."""
    def __init__(self, channels, tolerance_celsius=1.0, stabilization_timeout_seconds=300):
        self.channels = channels
        self.tolerance_celsius = tolerance_celsius
        self.stabilization_timeout_seconds = stabilization_timeout_seconds
        self.target_temperatures = [0.0] * channels
        self.actual_temperatures = [0.0] * channels  # never matches a non-zero target

    def set_target_temperature(self, channel, t):
        self.target_temperatures[channel - 1] = t

    def get_actual_temperature(self, channel):
        return self.actual_temperatures[channel - 1]


@pytest.fixture
def control():
    """The run's signal, fresh per test -- set_temperature takes it now
    rather than reading it off the controller."""
    return RunControl()


class TestSetTemperature:
    def test_none_controller_warns_and_returns(self, caplog, control):
        with caplog.at_level("WARNING", logger="fluidics.sequence_utils"):
            set_temperature(None, 37.0, control)  # should not raise
        assert "No temperature controller" in caplog.text

    def test_one_channel_converges_immediately(self, control):
        tc = TCMControllerSimulation(sn=None, channels=1)
        set_temperature(tc, 42.0, control)
        assert tc.target_temperatures == [42.0]
        assert tc.actual_temperatures == [42.0]

    def test_two_channel_sets_both_channels(self, control):
        tc = TCMControllerSimulation(sn=None, channels=2)
        set_temperature(tc, 30.0, control)
        assert tc.target_temperatures == [30.0, 30.0]
        assert tc.actual_temperatures == [30.0, 30.0]

    def test_timeout_raises_operation_error(self, control):
        tc = _StuckController(channels=1, stabilization_timeout_seconds=5)
        with pytest.raises(OperationError, match="failed to stabilize"):
            set_temperature(tc, 50.0, control)

    def test_a_pause_does_not_use_up_the_stabilization_timeout(self, control):
        """A run held for an hour must come back with its timeout intact.

        The pause is real and the wall clock moves during it -- which is what
        an earlier version got wrong: it spent the wait in running time but
        still compared wall clock against the deadline, so the first check
        after a long hold reported "failed to stabilize".
        """
        tc = _StuckController(channels=1, stabilization_timeout_seconds=5)
        reads = []
        actual = tc.get_actual_temperature
        tc.get_actual_temperature = lambda channel: (reads.append(channel),
                                                     actual(channel))[1]
        original_delay = control.delay

        def delay_then_hold(seconds):
            original_delay(seconds)
            if len(reads) == 1:          # the operator pauses after one look
                control.pause()
                time.sleep(3600)         # an hour of wall clock, none of it running
                control.resume()

        control.delay = delay_then_hold
        with pytest.raises(OperationError, match="failed to stabilize"):
            set_temperature(tc, 50.0, control)
        # Five running seconds of trying, not one cut short by the hold.
        assert len(reads) > 5, reads

    def test_slow_reads_count_against_the_timeout(self, control):
        """A channel that has stopped answering costs half a second a read at
        the serial timeout. Charging only the delay would let a 4 s timeout
        run for 8 s -- and for 12 s on a four-channel controller."""
        tc = _StuckController(channels=2, stabilization_timeout_seconds=4)
        reads = []

        def get_actual_temperature(channel):
            reads.append(channel)
            time.sleep(0.5)              # what a silent channel costs
            return 0.0

        tc.get_actual_temperature = get_actual_temperature
        with pytest.raises(OperationError, match="failed to stabilize"):
            set_temperature(tc, 50.0, control)
        # One second of delay plus one of reads per iteration: it gives up on
        # the third, not the fifth.
        assert len(reads) == 6, reads

    def test_a_cancelled_run_raises_before_it_writes_a_target(self, control):
        """Not a silent return, and not after setting a target on a run that
        is over: the caller could not tell 'reached target' from 'gave up'."""
        tc = _StuckController(channels=1, stabilization_timeout_seconds=5)
        control.cancel()
        with pytest.raises(AbortRequested):
            set_temperature(tc, 50.0, control)
        assert tc.target_temperatures == [0.0]      # nothing written
