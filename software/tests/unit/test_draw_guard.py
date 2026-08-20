# tests/unit/test_draw_guard.py
"""Arming, disarming, and what each policy does when the rule trips.

The rule itself is covered by test_flow_monitor.py; here the sensors are fakes
driven sample by sample, so a whole draw is a few function calls.
Timestamps are taken from time.time(), which the autouse _fast_clock fixture
freezes -- the guard stamps its start from the same clock, so a sample fed at
`now + n*0.06` lands after the start rather than decades before it, and the
ramp-up window means what it says.
"""

import time

import pytest

from fluidics.flow_monitor import DrawGuard, FlowFault


class FakeSensor:
    """A flow sensor's publish side, driven by hand."""

    def __init__(self, name="s", monitor="stop", ramp_up_seconds=0.0,
                 tolerance_fraction=0.3):
        self.name = name
        self.monitor = monitor
        self.ramp_up_seconds = ramp_up_seconds
        self.tolerance_fraction = tolerance_fraction
        self.subscribers = []
        self.faults = []          # (mode, fault, timestamp) published back to us

    def subscribe(self, callback):
        self.subscribers.append(callback)

    def unsubscribe(self, callback):
        if callback in self.subscribers:
            self.subscribers.remove(callback)

    def notify_fault(self, mode, fault, timestamp):
        self.faults.append((mode, fault, timestamp))

    def feed(self, flow, timestamp):
        for callback in list(self.subscribers):
            callback(flow, timestamp)


class RecordingPump:
    def __init__(self):
        self.stops = 0

    def stop(self):
        self.stops += 1


def guard_for(*sensors, pump=None, expected=500.0, log=None):
    return DrawGuard(list(sensors), expected_ul_min=expected,
                     stop_pump=(pump or RecordingPump()).stop,
                     log=(log if log is not None else lambda m: None))


def _drive(emit, flows, step=0.06):
    """Push readings at the 60 ms packet cadence.

    `emit` is either a sensor's fan-out or a bare handler -- the late-sample
    tests call one directly, the way notify's snapshot does after unsubscribe.
    """
    started = time.time()
    for i, flow in enumerate(flows):
        emit(flow, started + (i + 1) * step)


def _feed(sensor, flows):
    _drive(sensor.feed, flows)


def _replay(handler, flows):
    _drive(handler, flows)


def draw(guard, sensor, flows):
    """Run one draw: arm, feed, disarm, then raise if it faulted."""
    with guard:
        _feed(sensor, flows)
        guard.raise_if_faulted()


class TestArming:
    def test_an_off_sensor_is_never_subscribed(self):
        """Off must cost nothing at all -- not a subscription, not a rule."""
        sensor = FakeSensor(monitor="off")
        with guard_for(sensor):
            assert sensor.subscribers == []

    @pytest.mark.parametrize("mode", ["warn", "stop"])
    def test_an_active_sensor_is_subscribed_then_released(self, mode):
        sensor = FakeSensor(monitor=mode)
        g = guard_for(sensor)
        with g:
            assert len(sensor.subscribers) == 1
        assert sensor.subscribers == []

    def test_the_subscription_is_released_when_the_draw_raises(self):
        """Otherwise every failed draw strands a handler on the packet stream,
        faulting forever against an expectation from a finished draw."""
        sensor = FakeSensor(monitor="stop")
        with pytest.raises(RuntimeError):
            with guard_for(sensor):
                raise RuntimeError("the pump call blew up")
        assert sensor.subscribers == []

    def test_a_mid_draw_mode_change_does_not_affect_the_running_draw(self):
        """The GUI can flip the combo at any moment. A draw that armed under
        `warn` must finish under `warn`, not start halting halfway."""
        sensor = FakeSensor(monitor="warn")
        pump = RecordingPump()
        g = guard_for(sensor, pump=pump)
        with g:
            sensor.monitor = "stop"
            _feed(sensor, [0.0] * 10)
            g.raise_if_faulted()
        assert pump.stops == 0

    def test_no_sensors_at_all_is_fine(self):
        g = guard_for()
        with g:
            g.raise_if_faulted()


class TestSamplesArrivingAfterTheDraw:
    """Subscribers.notify snapshots the callback list and dispatches outside
    its lock, so a handler can still be entered after unsubscribe() returned.
    By then the draw is over and the pump may be executing the next chain.
    """

    def test_a_late_sample_does_not_stop_the_pump(self):
        sensor, pump = FakeSensor(monitor="stop"), RecordingPump()
        g = guard_for(sensor, pump=pump)
        with g:
            handler = sensor.subscribers[0]      # the snapshot notify would hold
        _replay(handler, [0.0] * 10)
        assert pump.stops == 0

    def test_a_late_sample_does_not_record_a_fault(self):
        """It would land on a guard whose raise_if_faulted has already run, so
        the run would carry on with a fault recorded and never reported."""
        sensor = FakeSensor(monitor="stop")
        g = guard_for(sensor)
        with g:
            handler = sensor.subscribers[0]
        _replay(handler, [0.0] * 10)
        assert g.fault is None

    def test_a_late_sample_does_not_log_a_warning(self):
        lines = []
        sensor = FakeSensor(monitor="warn")
        g = guard_for(sensor, log=lines.append)
        with g:
            handler = sensor.subscribers[0]
        _replay(handler, [0.0] * 10)
        assert lines == []


class TestStopPolicy:
    def test_healthy_flow_neither_stops_nor_raises(self):
        sensor, pump = FakeSensor(monitor="stop"), RecordingPump()
        g = guard_for(sensor, pump=pump)
        draw(g, sensor, [500.0] * 50)
        assert pump.stops == 0

    def test_a_dead_draw_stops_the_pump_and_raises(self):
        sensor, pump = FakeSensor(monitor="stop"), RecordingPump()
        g = guard_for(sensor, pump=pump)
        with pytest.raises(FlowFault):
            draw(g, sensor, [0.0] * 10)
        assert pump.stops == 1

    def test_the_pump_is_stopped_before_the_draw_returns(self):
        """The raise happens after the pump call returns; the stop cannot wait
        for that or the liquid keeps moving until the move finishes anyway."""
        sensor, pump = FakeSensor(monitor="stop"), RecordingPump()
        with guard_for(sensor, pump=pump) as g:
            _feed(sensor, [0.0] * 10)
            assert pump.stops == 1          # already stopped, still inside
            with pytest.raises(FlowFault):
                g.raise_if_faulted()

    def test_the_pump_is_stopped_once_however_long_the_fault_persists(self):
        sensor, pump = FakeSensor(monitor="stop"), RecordingPump()
        g = guard_for(sensor, pump=pump)
        with pytest.raises(FlowFault):
            draw(g, sensor, [0.0] * 200)
        assert pump.stops == 1

    def test_a_failing_stop_does_not_lose_the_fault(self):
        """stop() runs on the reader thread. If it raises there, the exception
        would kill the reader and the operator would be told nothing."""
        def explode():
            raise IOError("serial port went away")

        sensor = FakeSensor(monitor="stop")
        g = DrawGuard([sensor], expected_ul_min=500.0, stop_pump=explode,
                      log=lambda m: None)
        with pytest.raises(FlowFault):
            draw(g, sensor, [0.0] * 10)


class TestWarnPolicy:
    def test_warn_does_not_stop_the_pump(self):
        sensor, pump = FakeSensor(monitor="warn"), RecordingPump()
        g = guard_for(sensor, pump=pump)
        draw(g, sensor, [0.0] * 50)
        assert pump.stops == 0

    def test_warn_does_not_raise(self):
        sensor = FakeSensor(monitor="warn")
        g = guard_for(sensor)
        draw(g, sensor, [0.0] * 50)          # would raise under stop

    def test_warn_logs_the_fault(self):
        lines = []
        sensor = FakeSensor(monitor="warn", name="syringe_draw")
        draw(guard_for(sensor, log=lines.append), sensor, [0.0] * 10)
        assert len(lines) == 1
        assert "syringe_draw" in lines[0]

    def test_warn_logs_once_per_draw_not_once_per_sample(self):
        """The rule keeps faulting on every sample after it trips; one line
        per 60 ms packet would bury the rest of the run log."""
        lines = []
        sensor = FakeSensor(monitor="warn")
        draw(guard_for(sensor, log=lines.append), sensor, [0.0] * 500)
        assert len(lines) == 1


class TestFaultPublication:
    """A trip is published back to the sensor that saw it, so a recording of
    that sensor can file the verdict beside the readings it was made from.
    This is the durable half of a `warn` fault -- the log line is cleared or
    lost with the session.
    """

    def test_a_healthy_draw_publishes_nothing(self):
        sensor = FakeSensor(monitor="stop")
        draw(guard_for(sensor), sensor, [500.0] * 50)
        assert sensor.faults == []

    def test_a_warn_trip_is_published_with_its_mode_and_timestamp(self):
        sensor = FakeSensor(monitor="warn")
        draw(guard_for(sensor), sensor, [0.0] * 10)
        assert len(sensor.faults) == 1
        mode, fault, timestamp = sensor.faults[0]
        assert mode == "warn"
        assert isinstance(fault, FlowFault)
        assert fault.sensor_name == "s"
        # The tripping sample's own timestamp, so the row lands where the
        # readings around it do.
        assert timestamp == pytest.approx(time.time() + 3 * 0.06)

    def test_a_stop_trip_is_published_after_the_pump_is_halted(self):
        stops_before_publish = []
        pump = RecordingPump()
        sensor = FakeSensor(monitor="stop")
        sensor.notify_fault = lambda mode, fault, ts: stops_before_publish.append(pump.stops)
        g = guard_for(sensor, pump=pump)
        with pytest.raises(FlowFault):
            draw(g, sensor, [0.0] * 10)
        assert stops_before_publish == [1]

    def test_published_once_per_draw_not_once_per_sample(self):
        sensor = FakeSensor(monitor="warn")
        draw(guard_for(sensor), sensor, [0.0] * 500)
        assert len(sensor.faults) == 1

    def test_a_late_sample_publishes_nothing(self):
        """After __exit__ the guard no longer speaks for the draw -- not to the
        pump, not to the log, and not to the sensor's record."""
        for mode in ("warn", "stop"):
            sensor = FakeSensor(monitor=mode)
            g = guard_for(sensor)
            with g:
                handler = sensor.subscribers[0]
            _replay(handler, [0.0] * 10)
            assert sensor.faults == []

    def test_a_lost_claim_publishes_even_if_the_draw_disarms_mid_handler(self):
        """The winner's stop unblocks the sequence thread, which can disarm the
        guard before the losing sensor's handler finishes. The publish decision
        must come from the claim-time snapshot inside _claim's locked section;
        a later re-read of _active would misfile this in-draw trip as a late
        sample and drop it from the sensor's record."""
        sensor = FakeSensor(monitor="stop")
        g = guard_for(sensor)
        with g:
            handler = sensor.subscribers[0]
            # Another sensor won the claim while this draw was still armed.
            g._claim = lambda fault: (False, True)
        # __exit__ has disarmed by the time the reader thread finishes
        # dispatching this sample to the losing handler.
        _replay(handler, [0.0] * 10)
        assert [m for m, f, t in sensor.faults] == ["stop"]

    def test_a_stop_sensor_that_loses_the_claim_still_publishes_its_own_fault(self):
        """First cause wins the raise, but the second sensor genuinely saw a
        fault; its recording should say so."""
        first = FakeSensor(name="first", monitor="stop")
        second = FakeSensor(name="second", monitor="stop")
        g = guard_for(first, second)
        with pytest.raises(FlowFault):
            with g:
                _feed(first, [0.0] * 10)
                _feed(second, [0.0] * 10)
                g.raise_if_faulted()
        assert [m for m, f, t in first.faults] == ["stop"]
        assert [m for m, f, t in second.faults] == ["stop"]
        assert second.faults[0][1].sensor_name == "second"


class TestTwoSensors:
    def test_each_sensor_gets_its_own_tolerance(self):
        """Same expectation, different bands: 400 is inside a 30% band around
        500 and outside a 10% one."""
        loose = FakeSensor(name="loose", monitor="stop", tolerance_fraction=0.3)
        tight = FakeSensor(name="tight", monitor="stop", tolerance_fraction=0.1)
        pump = RecordingPump()
        g = guard_for(loose, tight, pump=pump)
        with pytest.raises(FlowFault) as excinfo:
            with g:
                _feed(loose, [400.0] * 10)
                _feed(tight, [400.0] * 10)
                g.raise_if_faulted()
        assert excinfo.value.sensor_name == "tight"

    def test_one_sensor_can_stop_while_the_other_only_warns(self):
        lines = []
        warner = FakeSensor(name="warner", monitor="warn")
        stopper = FakeSensor(name="stopper", monitor="stop")
        pump = RecordingPump()
        g = guard_for(warner, stopper, pump=pump, log=lines.append)
        with pytest.raises(FlowFault) as excinfo:
            with g:
                _feed(warner, [0.0] * 10)
                _feed(stopper, [0.0] * 10)
                g.raise_if_faulted()
        assert excinfo.value.sensor_name == "stopper"
        assert any("warner" in line for line in lines)

    def test_the_first_fault_wins(self):
        """Both stop-mode. The second must not overwrite the first, or the
        operator is told about whichever sensor sorts later in the list."""
        first = FakeSensor(name="first", monitor="stop")
        second = FakeSensor(name="second", monitor="stop")
        g = guard_for(first, second)
        with pytest.raises(FlowFault) as excinfo:
            with g:
                _feed(first, [0.0] * 10)
                _feed(second, [0.0] * 10)
                g.raise_if_faulted()
        assert excinfo.value.sensor_name == "first"
