# tests/unit/test_flow_monitor.py
"""The fault rule, driven by synthetic sample streams.

No controller, no thread, no clock -- timestamps are supplied, so these run
under the autouse _fast_clock fixture without any interaction with it.
"""

import pytest

from fluidics.flow_monitor import FlowMonitor, FlowFault


def make(expected=500.0, ramp_up=3.0, tolerance=0.3, debounce=3):
    m = FlowMonitor("syringe_draw", expected_ul_min=expected,
                    ramp_up_seconds=ramp_up, tolerance_fraction=tolerance,
                    debounce_samples=debounce)
    m.start(0.0)
    return m


def feed(monitor, flows, start=0.0, step=0.06):
    """Feed readings at the 60 ms packet cadence. Returns the first fault."""
    for i, flow in enumerate(flows):
        fault = monitor.sample(flow, start + (i + 1) * step)
        if fault is not None:
            return fault
    return None


class TestBand:
    def test_band_is_expected_plus_minus_tolerance(self):
        m = make(expected=500.0, tolerance=0.3)
        assert m.low_ul_min == pytest.approx(350.0)
        assert m.high_ul_min == pytest.approx(650.0)

    @pytest.mark.parametrize("flow", [350.0, 500.0, 650.0])
    def test_in_band_values_accepted(self, flow):
        assert make().in_band(flow)

    @pytest.mark.parametrize("flow", [349.0, 651.0, 0.0])
    def test_out_of_band_values_rejected(self, flow):
        assert not make().in_band(flow)

    def test_sign_is_ignored(self):
        """The sensor reads negative or positive during a draw depending on
        which way round it sits in the line; the rule must not care.
        """
        m = make(expected=500.0)
        assert m.in_band(-500.0)
        assert m.in_band(500.0)

    def test_invalid_is_never_in_band(self):
        """A sensor that stopped reporting is not the same as one reading
        fine -- nothing can be verified, so it counts against."""
        assert not make().in_band(None)


class TestSteadyFlow:
    def test_healthy_flow_never_faults(self):
        m = make()
        assert feed(m, [500.0] * 200) is None

    def test_jitter_within_tolerance_never_faults(self):
        m = make(expected=500.0, tolerance=0.3)
        noisy = [500.0, 610.0, 380.0, 520.0, 640.0, 360.0] * 30
        assert feed(m, noisy) is None


class TestRampUp:
    def test_no_fault_during_ramp_up_however_bad(self):
        """Startup transients are the whole reason ramp-up exists: a draw
        reads nothing at all until the plunger is moving."""
        m = make(ramp_up=3.0)
        # 3 s at 60 ms is 50 samples; feed 49 of zero flow.
        assert feed(m, [0.0] * 49) is None

    def test_faults_once_ramp_up_ends(self):
        m = make(ramp_up=3.0)
        assert feed(m, [0.0] * 60) is not None

    def test_ramp_up_measured_from_start_not_first_sample(self):
        m = FlowMonitor("s", expected_ul_min=500.0, ramp_up_seconds=3.0,
                        tolerance_fraction=0.3, debounce_samples=3)
        m.start(1000.0)
        # Two seconds in, well past debounce, still inside ramp-up.
        assert m.sample(0.0, 1001.0) is None
        assert m.sample(0.0, 1001.5) is None
        assert m.sample(0.0, 1002.0) is None
        # Past it.
        assert m.sample(0.0, 1003.5) is not None


class TestDebounce:
    def test_a_single_dropout_does_not_fault(self):
        m = make(ramp_up=0.0, debounce=3)
        assert feed(m, [500.0, 500.0, 0.0, 500.0, 500.0]) is None

    def test_two_consecutive_do_not_fault_when_debounce_is_three(self):
        m = make(ramp_up=0.0, debounce=3)
        assert feed(m, [500.0, 0.0, 0.0, 500.0, 500.0]) is None

    def test_three_consecutive_fault(self):
        m = make(ramp_up=0.0, debounce=3)
        assert feed(m, [500.0, 0.0, 0.0, 0.0]) is not None

    def test_a_good_sample_resets_the_run(self):
        m = make(ramp_up=0.0, debounce=3)
        assert feed(m, [0.0, 0.0, 500.0, 0.0, 0.0, 500.0]) is None


class TestInvalidReadings:
    def test_sustained_invalid_faults(self):
        """A sensor that dies mid-draw must fault, not pass silently."""
        m = make(ramp_up=0.0, debounce=3)
        fault = feed(m, [500.0, None, None, None])
        assert fault is not None
        assert fault.measured_ul_min is None
        assert "no reading" in str(fault)

    def test_invalid_mixes_with_low_flow_in_one_run(self):
        m = make(ramp_up=0.0, debounce=3)
        assert feed(m, [500.0, None, 0.0, None]) is not None


class TestFaultContents:
    def test_fault_carries_the_numbers(self):
        m = make(expected=500.0, ramp_up=0.0, tolerance=0.3, debounce=3)
        fault = feed(m, [340.0, 340.0, 340.0])
        assert fault.sensor_name == "syringe_draw"
        assert fault.expected_ul_min == pytest.approx(500.0)
        assert fault.measured_ul_min == pytest.approx(340.0)
        assert fault.consecutive_samples == 3

    def test_out_of_band_duration_spans_the_whole_run(self):
        """Timed from the first bad sample, not from the one that tripped it."""
        m = make(expected=500.0, ramp_up=0.0, debounce=3)
        fault = feed(m, [340.0, 340.0, 340.0], step=0.06)
        assert fault.out_of_band_seconds == pytest.approx(0.12, abs=1e-9)

    def test_message_names_the_sensor_the_band_and_the_reading(self):
        m = make(expected=500.0, ramp_up=0.0, tolerance=0.3)
        text = str(feed(m, [340.0] * 3))
        assert "syringe_draw" in text
        assert "340" in text
        assert "350" in text and "650" in text   # the band, spelled out

    def test_fault_is_an_operation_error(self):
        """So the worker reports it through the existing on_error path."""
        from fluidics.errors import OperationError
        m = make(ramp_up=0.0)
        assert isinstance(feed(m, [0.0] * 3), OperationError)


class TestArming:
    def test_samples_before_start_are_ignored(self):
        """A subscriber can fire between construction and arming; that must
        not produce a fault against an expectation not yet set."""
        m = FlowMonitor("s", expected_ul_min=500.0, ramp_up_seconds=0.0,
                        tolerance_fraction=0.3, debounce_samples=1)
        assert m.sample(0.0, 1.0) is None
        assert m.sample(0.0, 2.0) is None

    def test_start_clears_state_from_a_previous_draw(self):
        m = make(ramp_up=0.0, debounce=3)
        m.sample(0.0, 1.0)
        m.sample(0.0, 2.0)
        m.start(10.0)                      # re-armed for the next draw
        assert m.sample(0.0, 11.0) is None  # run length starts over
