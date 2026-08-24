# tests/integration/test_flow_draw_protection.py
"""Draw protection through MERFISHOperations, against the simulated pump.

The unit tests cover the rule and the guard in isolation; what is left is the
wiring -- that flow_reagent arms the sensors around the right calls, expects
the pump's real rate, and lets a FlowFault out intact.

The sensor publishes from inside the pump's wait_for_stop, which is where the
readings arrive on hardware: the sequence thread waits there while the reader
thread delivers packets.
"""

import time

import logging

import pytest

from fluidics.experiment_worker import OperationError
from fluidics.flow_monitor import FlowFault
from fluidics.merfish_operations import MERFISHOperations


class ScriptedSensor:
    """A flow sensor that plays a fixed reading back during every draw."""

    def __init__(self, flow, name="syringe_draw", monitor="stop",
                 ramp_up_seconds=0.0, tolerance_fraction=0.3):
        self.flow = flow
        self.name = name
        self.monitor = monitor
        self.ramp_up_seconds = ramp_up_seconds
        self.tolerance_fraction = tolerance_fraction
        self.subscribers = []
        self.guarded_draws = 0
        self.faults = []          # (mode, fault, timestamp) published back to us

    def subscribe(self, callback):
        self.subscribers.append(callback)

    def unsubscribe(self, callback):
        if callback in self.subscribers:
            self.subscribers.remove(callback)

    def notify_fault(self, mode, fault, timestamp):
        self.faults.append((mode, fault, timestamp))

    def play(self, samples=10, step=0.06):
        """One draw's worth of packets.

        Counts only draws that had a guard armed. Counting every call would
        make "the fill-tubing draw is guarded" pass even with the guard
        removed, since the pump moves either way.
        """
        if self.subscribers:
            self.guarded_draws += 1
        started = time.time()
        for i in range(samples):
            for callback in list(self.subscribers):
                callback(self.flow, started + (i + 1) * step)


@pytest.fixture
def ops_and_sensor(flow_cell_hardware):
    """MERFISHOperations whose pump publishes readings while it moves."""
    config, sp, sv = flow_cell_hardware
    sensor = ScriptedSensor(flow=500.0)

    # Publish from inside wait_for_stop, not around execute(): execute() clears
    # the interrupt on entry, so readings delivered before it would have their
    # stop() wiped by the very call they were meant to interrupt.
    original_wait = sp.wait_for_stop

    def wait_for_stop(t=0):
        sensor.play()
        return original_wait(t)

    sp.wait_for_stop = wait_for_stop
    return MERFISHOperations(config, sp, sv, flow_sensors=[sensor]), sensor, sp


# 500 uL/min is speed code 40 exactly on a 5000 uL syringe, so the expectation
# the guard builds is 500 and the arithmetic is not obscured by quantization.
SEQ = {"type": "flow_reagent", "fluidic_port": 1, "flow_rate": 500, "volume": 500}


class TestHealthyDraw:
    def test_a_draw_at_the_expected_rate_completes(self, ops_and_sensor):
        ops, sensor, _sp = ops_and_sensor
        ops.process_sequence(SEQ)
        assert sensor.guarded_draws == 1

    def test_the_sensor_is_released_afterwards(self, ops_and_sensor):
        """A leaked subscription would fault the next draw against this one's
        expectation."""
        ops, sensor, _sp = ops_and_sensor
        ops.process_sequence(SEQ)
        assert sensor.subscribers == []


class TestFaultingDraw:
    @pytest.fixture
    def dead_flow(self, ops_and_sensor):
        ops, sensor, sp = ops_and_sensor
        sensor.flow = 0.0
        return ops, sensor, sp

    def test_a_dead_draw_raises_a_flow_fault(self, dead_flow):
        ops, _sensor, _sp = dead_flow
        with pytest.raises(FlowFault):
            ops.process_sequence(SEQ)

    def test_the_fault_is_not_flattened_into_a_plain_operation_error(self, dead_flow):
        """flow_reagent wraps everything else in OperationError(str(e)). A
        FlowFault must come through with its fields, or the operator loses the
        sensor name and the numbers."""
        ops, _sensor, _sp = dead_flow
        with pytest.raises(OperationError) as excinfo:
            ops.process_sequence(SEQ)
        assert isinstance(excinfo.value, FlowFault)
        assert excinfo.value.sensor_name == "syringe_draw"
        assert excinfo.value.measured_ul_min == pytest.approx(0.0)
        assert excinfo.value.expected_ul_min == pytest.approx(500.0)

    def test_the_pump_is_stopped(self, dead_flow):
        ops, _sensor, sp = dead_flow
        with pytest.raises(FlowFault):
            ops.process_sequence(SEQ)
        assert sp._interrupt.is_set()

    def test_stopping_does_not_latch_the_abort_flag(self, dead_flow):
        """A flow fault is not the operator cancelling. If it set is_aborted,
        every later operation would silently return and the run would look
        like it completed."""
        ops, _sensor, sp = dead_flow
        with pytest.raises(FlowFault):
            ops.process_sequence(SEQ)
        assert sp.is_aborted is False

    def test_the_sensor_is_released_after_a_fault(self, dead_flow):
        ops, sensor, _sp = dead_flow
        with pytest.raises(FlowFault):
            ops.process_sequence(SEQ)
        assert sensor.subscribers == []


class TestPolicies:
    def test_off_lets_a_dead_draw_through(self, ops_and_sensor):
        ops, sensor, _sp = ops_and_sensor
        sensor.flow = 0.0
        sensor.monitor = "off"
        ops.process_sequence(SEQ)          # no raise

    def test_warn_lets_a_dead_draw_through(self, ops_and_sensor):
        ops, sensor, _sp = ops_and_sensor
        sensor.flow = 0.0
        sensor.monitor = "warn"
        ops.process_sequence(SEQ)

    def test_a_gui_flip_to_stop_takes_effect_on_the_next_draw(self, ops_and_sensor):
        """This is what runtime switching is for: run in warn, watch, then
        turn it on without restarting."""
        ops, sensor, _sp = ops_and_sensor
        sensor.flow = 0.0
        sensor.monitor = "warn"
        ops.process_sequence(SEQ)

        sensor.monitor = "stop"
        with pytest.raises(FlowFault):
            ops.process_sequence(SEQ)


class TestScope:
    def test_no_sensors_configured_changes_nothing(self, flow_cell_hardware):
        config, sp, sv = flow_cell_hardware
        MERFISHOperations(config, sp, sv).process_sequence(SEQ)

    def test_the_fill_tubing_draw_is_guarded_too(self, ops_and_sensor):
        """Two draws, both through the flow cell, both watched."""
        ops, sensor, _sp = ops_and_sensor
        seq = dict(SEQ, volume=2000, fill_tubing_with=25)
        ops.process_sequence(seq)
        assert sensor.guarded_draws == 2

    def test_priming_is_not_guarded(self, ops_and_sensor):
        """Priming dispenses to waste between extracts, so the sensors would
        read nothing for half of it. Out of scope for now, by design."""
        ops, sensor, _sp = ops_and_sensor
        sensor.flow = 0.0
        ops.process_sequence({"type": "priming", "fluidic_port": 10,
                              "flow_rate": 500, "volume": 2000})


class TestWarningsAreReportable:
    """`warn` deliberately raises nothing, so the notice is the only trace it
    leaves. Defaulting it to stdout meant a GUI-launched run showed nothing at
    all for the mode operators are told to start on.
    """

    @pytest.fixture
    def warned(self, flow_cell_hardware):
        config, sp, sv = flow_cell_hardware
        sensor = ScriptedSensor(flow=0.0, monitor="warn")
        original_wait = sp.wait_for_stop

        def wait_for_stop(t=0):
            sensor.play()
            return original_wait(t)

        sp.wait_for_stop = wait_for_stop
        lines = []
        ops = MERFISHOperations(config, sp, sv, flow_sensors=[sensor],
                                on_warning=lines.append)
        return ops, lines

    def test_the_notice_reaches_the_injected_channel(self, warned):
        ops, lines = warned
        ops.process_sequence(SEQ)
        assert len(lines) == 1
        assert "syringe_draw" in lines[0]

    def test_the_fault_is_published_back_to_the_sensor(self, warned):
        """The durable half: whatever records the sensor (the GUI's CSV) gets
        the verdict beside the readings, even though warn raises nothing."""
        ops, _lines = warned
        sensor = ops.flow_sensors[0]
        ops.process_sequence(SEQ)
        assert [mode for mode, fault, ts in sensor.faults] == ["warn"]
        assert sensor.faults[0][1].sensor_name == "syringe_draw"

    def test_a_stop_also_reports_before_it_raises(self, warned):
        """The operator sees why the run is about to fail, not just that it
        did -- the two arrive by different routes and the notice comes first."""
        ops, lines = warned
        ops.flow_sensors[0].monitor = "stop"
        with pytest.raises(FlowFault):
            ops.process_sequence(SEQ)
        assert any("Stopping draw" in line for line in lines)

    def test_it_defaults_to_the_fluidics_logger_when_nothing_is_injected(
            self, flow_cell_hardware):
        """The CLI relies on this: warn notices reach the console and the
        run log with no channel injected."""
        config, sp, sv = flow_cell_hardware
        assert (MERFISHOperations(config, sp, sv).on_warning
                == logging.getLogger("fluidics.merfish_operations").warning)
