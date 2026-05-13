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

    def test_abort_flag(self):
        tc = TCMControllerSimulation(sn=None, channels=1)
        assert tc.is_aborted is False
        tc.abort()
        assert tc.is_aborted is True
        tc.reset_abort()
        assert tc.is_aborted is False
