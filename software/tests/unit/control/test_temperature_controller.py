import pytest

from fluidics.control.temperature_controller import TCMControllerSimulation


class TestTCMControllerSimulation:
    def test_default_channels_is_2(self):
        tc = TCMControllerSimulation(sn=None)
        assert tc.channels == 2
        assert len(tc.target_temperatures) == 2
        assert len(tc.actual_temperatures) == 2

    def test_one_channel(self):
        tc = TCMControllerSimulation(sn=None, channels=1)
        assert tc.channels == 1
        assert len(tc.target_temperatures) == 1
        assert len(tc.actual_temperatures) == 1

    def test_set_target_updates_actual_in_simulation(self):
        tc = TCMControllerSimulation(sn=None, channels=2)
        tc.set_target_temperature(1, 37.5)
        assert tc.target_temperatures[0] == 37.5
        assert tc.actual_temperatures[0] == 37.5

    def test_set_target_only_updates_named_channel(self):
        tc = TCMControllerSimulation(sn=None, channels=2)
        tc.set_target_temperature(2, 50.0)
        # channel 1 untouched (still default 10.0)
        assert tc.target_temperatures[0] == 10.0
        assert tc.target_temperatures[1] == 50.0

    def test_get_target_temperature_returns_current_target(self):
        tc = TCMControllerSimulation(sn=None, channels=2)
        tc.set_target_temperature(1, 25.0)
        assert tc.get_target_temperature(1) == 25.0

    def test_get_actual_temperature_returns_simulated_actual(self):
        tc = TCMControllerSimulation(sn=None, channels=1)
        tc.set_target_temperature(1, 42.0)
        assert tc.get_actual_temperature(1) == 42.0

    def test_invalid_channel_raises(self):
        tc = TCMControllerSimulation(sn=None, channels=1)
        with pytest.raises(ValueError):
            tc.set_target_temperature(2, 25.0)

    def test_tolerance_and_timeout_stored(self):
        tc = TCMControllerSimulation(
            sn=None, channels=1,
            tolerance_celsius=0.5, stabilization_timeout_seconds=60,
        )
        assert tc.tolerance_celsius == 0.5
        assert tc.stabilization_timeout_seconds == 60

    def test_save_target_temperature_does_not_raise(self):
        tc = TCMControllerSimulation(sn=None, channels=2)
        tc.save_target_temperature(1)
        tc.save_target_temperature(2)

    def test_output_enabled_defaults_to_false(self):
        tc = TCMControllerSimulation(sn=None, channels=2)
        assert tc.output_enabled == [False, False]
        assert tc.get_output_enabled(1) is False
        assert tc.get_output_enabled(2) is False

    def test_set_output_enabled_only_updates_named_channel(self):
        tc = TCMControllerSimulation(sn=None, channels=2)
        tc.set_output_enabled(1, True)
        assert tc.output_enabled == [True, False]
        assert tc.get_output_enabled(1) is True
        assert tc.get_output_enabled(2) is False

    def test_set_output_enabled_coerces_truthy_to_bool(self):
        tc = TCMControllerSimulation(sn=None, channels=1)
        tc.set_output_enabled(1, 1)
        assert tc.output_enabled[0] is True
        tc.set_output_enabled(1, 0)
        assert tc.output_enabled[0] is False

    def test_output_enabled_invalid_channel_raises(self):
        tc = TCMControllerSimulation(sn=None, channels=1)
        with pytest.raises(ValueError):
            tc.set_output_enabled(2, True)
        with pytest.raises(ValueError):
            tc.get_output_enabled(2)

    def test_close_does_not_raise(self):
        tc = TCMControllerSimulation(sn=None, channels=1)
        tc.close()



# --- The readings channel ---
#
# The flow-sensor contract, pinned on both classes (why it exists: the
# start() docstring in the driver). _publish() is driven directly so nothing
# here starts the polling thread; the one lifecycle test that does closes it
# again.

from fluidics.control.controller import Subscribers
from fluidics.control.temperature_controller import TCMController


class TestReadingsChannel:
    def test_subscriber_receives_each_publish(self):
        tc = TCMControllerSimulation(channels=2)
        seen = []
        tc.subscribe(seen.append)
        tc.actual_temperatures = [21.0, 37.0]
        tc._publish()
        assert seen == [[21.0, 37.0]]

    def test_the_payload_is_a_copy_not_the_live_list(self):
        tc = TCMControllerSimulation(channels=2)
        seen = []
        tc.subscribe(seen.append)
        tc._publish()
        tc.actual_temperatures[0] = 99.0
        assert seen[0][0] != 99.0

    def test_unsubscribed_callback_is_not_called(self):
        tc = TCMControllerSimulation(channels=2)
        seen = []
        callback = seen.append   # held once: unsubscribe matches by identity
        tc.subscribe(callback)
        tc.unsubscribe(callback)
        tc._publish()
        assert seen == []

    def test_a_failing_subscriber_does_not_break_others(self):
        tc = TCMControllerSimulation(channels=2)
        seen = []
        tc.subscribe(lambda temps: (_ for _ in ()).throw(RuntimeError("bad")))
        tc.subscribe(seen.append)
        tc._publish()
        assert len(seen) == 1

    def test_close_drops_subscribers(self):
        tc = TCMControllerSimulation(channels=2)
        seen = []
        tc.subscribe(seen.append)
        tc.close()
        tc._publish()
        assert seen == []

    def test_the_real_class_publishes_the_same_way(self):
        """The channel lives identically on both classes; the real one is
        built here without hardware, the interrupt-test way."""
        tcm = TCMController.__new__(TCMController)
        tcm._subscribers = Subscribers("Temperature controller")
        tcm.actual_temperatures = [42.0]
        seen = []
        tcm.subscribe(seen.append)
        tcm._publish()
        assert seen == [[42.0]]


class TestStart:
    def test_start_is_idempotent_and_close_joins(self):
        tc = TCMControllerSimulation(channels=1)
        tc.start()
        tc.start()   # a second call must not raise on the running thread
        tc.close()
        assert not tc._polling_thread.is_alive()
