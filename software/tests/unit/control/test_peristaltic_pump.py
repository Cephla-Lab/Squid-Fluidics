import threading
import time

import pytest

from fluidics.control.peristaltic_pump import (
    Direction,
    PeristalticPumpSimulation,
)


class TestDirection:
    def test_clockwise_value(self):
        assert Direction.CLOCKWISE == 1

    def test_counter_clockwise_value(self):
        assert Direction.COUNTER_CLOCKWISE == -1


class TestPeristalticPumpSimulation:
    def test_create(self):
        pump = PeristalticPumpSimulation(slave_id=1)
        assert not pump.is_running
        assert not pump.is_aborted

    def test_set_speed(self):
        pump = PeristalticPumpSimulation()
        pump.set_speed(100.0)
        assert pump._speed_rpm == 100.0

    def test_set_speed_zero_rejected(self):
        pump = PeristalticPumpSimulation()
        with pytest.raises(ValueError, match="positive"):
            pump.set_speed(0.0)

    def test_set_speed_negative_rejected(self):
        pump = PeristalticPumpSimulation()
        with pytest.raises(ValueError, match="positive"):
            pump.set_speed(-10.0)

    def test_set_speed_clamps_to_max(self):
        pump = PeristalticPumpSimulation(max_speed=100.0)
        pump.set_speed(200.0)
        assert pump._speed_rpm == 100.0

    def test_start_stop(self):
        pump = PeristalticPumpSimulation()
        pump.set_speed(50.0)
        pump.start()
        assert pump.is_running
        pump.stop()
        assert not pump.is_running

    def test_start_without_speed_raises(self):
        pump = PeristalticPumpSimulation()
        with pytest.raises(RuntimeError, match="Speed not set"):
            pump.start()

    def test_set_acceleration(self):
        pump = PeristalticPumpSimulation()
        pump.set_acceleration(300, 400)

    def test_run_for_duration(self):
        pump = PeristalticPumpSimulation()
        pump.run_for_duration(50.0, 0.2)
        assert not pump.is_running

    def test_run_for_duration_abort(self):
        pump = PeristalticPumpSimulation()
        pump.is_aborted = True
        with pytest.raises(RuntimeError, match="aborted"):
            pump.run_for_duration(50.0, 5.0)
        assert not pump.is_running

    def test_abort(self):
        pump = PeristalticPumpSimulation()
        pump.set_speed(50.0)
        pump.start()
        pump.abort()
        assert pump.is_aborted
        assert not pump.is_running

    def test_reset_abort(self):
        pump = PeristalticPumpSimulation()
        pump.abort()
        assert pump.is_aborted
        pump.reset_abort()
        assert not pump.is_aborted

    def test_direction_stored(self):
        pump = PeristalticPumpSimulation(direction=Direction.COUNTER_CLOCKWISE)
        assert pump._direction == Direction.COUNTER_CLOCKWISE
