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
        self.run_control = RunControl()

    def set_target_temperature(self, channel, t):
        self.target_temperatures[channel - 1] = t

    def get_actual_temperature(self, channel):
        return self.actual_temperatures[channel - 1]


class TestSetTemperature:
    def test_none_controller_warns_and_returns(self, caplog):
        with caplog.at_level("WARNING", logger="fluidics.sequence_utils"):
            set_temperature(None, 37.0)  # should not raise
        assert "No temperature controller" in caplog.text

    def test_one_channel_converges_immediately(self):
        tc = TCMControllerSimulation(sn=None, channels=1)
        set_temperature(tc, 42.0)
        assert tc.target_temperatures == [42.0]
        assert tc.actual_temperatures == [42.0]

    def test_two_channel_sets_both_channels(self):
        tc = TCMControllerSimulation(sn=None, channels=2)
        set_temperature(tc, 30.0)
        assert tc.target_temperatures == [30.0, 30.0]
        assert tc.actual_temperatures == [30.0, 30.0]

    def test_timeout_raises_operation_error(self):
        tc = _StuckController(channels=1, stabilization_timeout_seconds=5)
        with pytest.raises(OperationError, match="failed to stabilize"):
            set_temperature(tc, 50.0)

    def test_a_cancelled_run_raises_out_of_the_wait(self):
        """Not a silent return: the caller could not tell 'reached target'
        from 'gave up'."""
        tc = _StuckController(channels=1, stabilization_timeout_seconds=5)
        tc.run_control.cancel()
        with pytest.raises(AbortRequested):
            set_temperature(tc, 50.0)
        # The target was set before the wait; the cancel raised inside it.
        assert tc.target_temperatures == [50.0]
