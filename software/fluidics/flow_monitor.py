"""The draw-protection fault rule.

Deliberately a pure function of `(flow, timestamp)` samples: no controller, no
thread, no clock. That is what makes it testable without hardware, and it
matters here because `tests/conftest.py` patches `time.time` and `time.sleep`
process-wide -- a rule that read the clock itself could not be tested at all.

Deciding is separated from acting. `sample()` returns a fault rather than
raising or touching the pump, so the caller chooses what a fault means: `warn`
logs it, `stop` halts the draw and raises it.
"""

from .experiment_worker import OperationError


class FlowFault(OperationError):
    """Measured flow left its expected band for long enough to count.

    Carries the numbers rather than a pre-baked string so the log line, the
    GUI and the CLI can each render what they need.
    """

    def __init__(self, sensor_name, expected_ul_min, tolerance_fraction,
                 measured_ul_min, out_of_band_seconds, consecutive_samples):
        self.sensor_name = sensor_name
        self.expected_ul_min = expected_ul_min
        self.tolerance_fraction = tolerance_fraction
        self.measured_ul_min = measured_ul_min
        self.out_of_band_seconds = out_of_band_seconds
        self.consecutive_samples = consecutive_samples

        low = expected_ul_min * (1 - tolerance_fraction)
        high = expected_ul_min * (1 + tolerance_fraction)
        measured = ("no reading" if measured_ul_min is None
                    else f"{measured_ul_min:.0f} µL/min")
        super().__init__(
            f"Flow fault on {sensor_name!r}: {measured} for "
            f"{out_of_band_seconds:.2f} s ({consecutive_samples} samples), "
            f"expected {expected_ul_min:.0f} ±{tolerance_fraction:.0%} "
            f"({low:.0f}–{high:.0f} µL/min)"
        )


class FlowMonitor:
    """Watches one sensor's readings against an expected rate for one draw.

    Construct per draw, call `start()` when the draw begins, then feed every
    reading to `sample()`. It returns None while things are fine and a
    FlowFault the moment the rule trips.
    """

    DEFAULT_DEBOUNCE_SAMPLES = 3

    def __init__(self, sensor_name, expected_ul_min, ramp_up_seconds,
                 tolerance_fraction, debounce_samples=DEFAULT_DEBOUNCE_SAMPLES):
        self.sensor_name = sensor_name
        self.expected_ul_min = expected_ul_min
        self.ramp_up_seconds = ramp_up_seconds
        self.tolerance_fraction = tolerance_fraction
        self.debounce_samples = debounce_samples

        self._started_at = None
        self._out_of_band_since = None
        self._consecutive = 0

    # --- the band ---

    @property
    def low_ul_min(self):
        return abs(self.expected_ul_min) * (1 - self.tolerance_fraction)

    @property
    def high_ul_min(self):
        return abs(self.expected_ul_min) * (1 + self.tolerance_fraction)

    def in_band(self, flow):
        """Whether a reading counts as healthy.

        Magnitude, not sign: the sensor reports negative during a draw or
        positive depending on which way round it sits in the line, and which
        way that is should not change the rule. An invalid reading (None) is
        never in band -- a sensor that stopped reporting mid-draw means
        nothing can be verified, which is not the same as being fine.
        """
        if flow is None:
            return False
        return self.low_ul_min <= abs(flow) <= self.high_ul_min

    # --- the rule ---

    def start(self, timestamp):
        self._started_at = timestamp
        self._out_of_band_since = None
        self._consecutive = 0

    def sample(self, flow, timestamp):
        """Feed one reading. Returns a FlowFault if the rule trips, else None.

        Samples arriving before `start()` are ignored rather than treated as
        an error, so a subscriber that fires between construction and arming
        cannot produce a spurious fault.
        """
        if self._started_at is None:
            return None

        if self.in_band(flow):
            self._out_of_band_since = None
            self._consecutive = 0
            return None

        # Out of band. Track it during ramp-up too, so the run length is
        # honest the moment ramp-up ends -- but never fault while ramping.
        if self._out_of_band_since is None:
            self._out_of_band_since = timestamp
        self._consecutive += 1

        if timestamp - self._started_at < self.ramp_up_seconds:
            return None

        if self._consecutive < self.debounce_samples:
            return None

        return FlowFault(
            sensor_name=self.sensor_name,
            expected_ul_min=self.expected_ul_min,
            tolerance_fraction=self.tolerance_fraction,
            measured_ul_min=flow,
            out_of_band_seconds=timestamp - self._out_of_band_since,
            consecutive_samples=self._consecutive,
        )
