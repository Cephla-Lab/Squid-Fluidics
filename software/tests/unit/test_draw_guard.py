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

from fluidics.errors import AbortRequested, RunControl
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


class RecordingControl(RunControl):
    """Counts cancel() calls: the guard must cancel once per draw, not once
    per faulting sample."""

    def __init__(self):
        super().__init__()
        self.cancels = 0

    def cancel(self, cause=None):
        self.cancels += 1
        return super().cancel(cause)


def guard_for(*sensors, control=None, expected=500.0, log=None):
    return DrawGuard(list(sensors), expected_ul_min=expected,
                     run_control=control if control is not None else RunControl(),
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
    """Run one draw: arm, feed, then raise the cause if the run was cancelled
    -- standing in for the pump's wait, which is what raises it in production."""
    with guard:
        _feed(sensor, flows)
        guard.run_control.check()


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
        g = guard_for(sensor)
        with g:
            sensor.monitor = "stop"
            _feed(sensor, [0.0] * 10)
            g.run_control.check()
        assert not g.run_control.cancelled

    def test_no_sensors_at_all_is_fine(self):
        g = guard_for()
        with g:
            g.run_control.check()


class TestSamplesArrivingAfterTheDraw:
    """Subscribers.notify snapshots the callback list and dispatches outside
    its lock, so a handler can still be entered after unsubscribe() returned.
    By then the draw is over and the pump may be executing the next chain.
    """

    def test_a_late_sample_does_not_cancel_the_run(self):
        sensor = FakeSensor(monitor="stop")
        g = guard_for(sensor)
        with g:
            handler = sensor.subscribers[0]      # the snapshot notify would hold
        _replay(handler, [0.0] * 10)
        assert not g.run_control.cancelled

    def test_a_late_sample_does_not_log_a_warning(self):
        lines = []
        sensor = FakeSensor(monitor="warn")
        g = guard_for(sensor, log=lines.append)
        with g:
            handler = sensor.subscribers[0]
        _replay(handler, [0.0] * 10)
        assert lines == []


class TestStopPolicy:
    def test_healthy_flow_neither_cancels_nor_raises(self):
        sensor = FakeSensor(monitor="stop")
        g = guard_for(sensor)
        draw(g, sensor, [500.0] * 50)
        assert not g.run_control.cancelled

    def test_a_dead_draw_cancels_the_run_with_the_fault_as_its_cause(self):
        """The whole halt-and-unwind path is the cancel: the pump's wait wakes
        on it, halts the plunger where the port lives, and raises this."""
        sensor = FakeSensor(monitor="stop")
        g = guard_for(sensor)
        with pytest.raises(FlowFault) as excinfo:
            draw(g, sensor, [0.0] * 10)
        assert g.run_control.cause is excinfo.value

    def test_the_run_is_cancelled_the_moment_the_rule_trips(self):
        """From the reader thread, mid-draw: the sequence thread's wait wakes
        on it. Waiting for the draw to return would let the liquid keep
        moving until the move finished anyway."""
        sensor = FakeSensor(monitor="stop")
        with guard_for(sensor) as g:
            _feed(sensor, [0.0] * 10)
            assert isinstance(g.run_control.cause, FlowFault)   # still inside

    def test_the_run_is_cancelled_once_however_long_the_fault_persists(self):
        sensor = FakeSensor(monitor="stop")
        g = guard_for(sensor, control=RecordingControl())
        with pytest.raises(FlowFault):
            draw(g, sensor, [0.0] * 200)
        assert g.run_control.cancels == 1

    def test_a_failing_log_still_cancels_the_run(self):
        """`log` is injected -- the GUI marshals it to the Qt thread. If it
        raises, the cancel must already have happened, or the plunger keeps
        moving with the fault recorded on a guard nobody re-checks."""
        def explode(message):
            raise RuntimeError("the warning channel went away")

        sensor = FakeSensor(monitor="stop")
        g = guard_for(sensor, log=explode)
        with g:
            with pytest.raises(RuntimeError):
                _feed(sensor, [0.0] * 10)
        assert isinstance(g.run_control.cause, FlowFault)

    def test_a_fault_is_never_reported_as_an_abort(self):
        """The operator's reflex after a flow alarm is to press Abort a second
        later; first cause wins, so the diagnosis survives."""
        sensor = FakeSensor(monitor="stop")
        g = guard_for(sensor)
        with g:
            _feed(sensor, [0.0] * 10)
            g.run_control.cancel()          # the operator's Abort, a beat later
        assert isinstance(g.run_control.cause, FlowFault)
        assert not isinstance(g.run_control.cause, AbortRequested)


class TestWarnPolicy:
    def test_warn_does_not_cancel_the_run(self):
        sensor = FakeSensor(monitor="warn")
        g = guard_for(sensor)
        draw(g, sensor, [0.0] * 50)
        assert not g.run_control.cancelled

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

    def test_a_stop_trip_is_published_after_the_run_is_cancelled(self):
        """The cancel is what halts the pump; the bookkeeping never delays it."""
        cancelled_before_publish = []
        control = RecordingControl()
        sensor = FakeSensor(monitor="stop")
        sensor.notify_fault = lambda mode, fault, ts: cancelled_before_publish.append(control.cancelled)
        g = guard_for(sensor, control=control)
        with pytest.raises(FlowFault):
            draw(g, sensor, [0.0] * 10)
        assert cancelled_before_publish == [True]

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
                g.run_control.check()
        assert [m for m, f, t in first.faults] == ["stop"]
        assert [m for m, f, t in second.faults] == ["stop"]
        assert second.faults[0][1].sensor_name == "second"


class TestTwoSensors:
    def test_each_sensor_gets_its_own_tolerance(self):
        """Same expectation, different bands: 400 is inside a 30% band around
        500 and outside a 10% one."""
        loose = FakeSensor(name="loose", monitor="stop", tolerance_fraction=0.3)
        tight = FakeSensor(name="tight", monitor="stop", tolerance_fraction=0.1)
        g = guard_for(loose, tight)
        with pytest.raises(FlowFault) as excinfo:
            with g:
                _feed(loose, [400.0] * 10)
                _feed(tight, [400.0] * 10)
                g.run_control.check()
        assert excinfo.value.sensor_name == "tight"

    def test_one_sensor_can_stop_while_the_other_only_warns(self):
        lines = []
        warner = FakeSensor(name="warner", monitor="warn")
        stopper = FakeSensor(name="stopper", monitor="stop")
        g = guard_for(warner, stopper, log=lines.append)
        with pytest.raises(FlowFault) as excinfo:
            with g:
                _feed(warner, [0.0] * 10)
                _feed(stopper, [0.0] * 10)
                g.run_control.check()
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
                g.run_control.check()
        assert excinfo.value.sensor_name == "first"
