"""The draw-protection fault rule.

Deliberately a pure function of `(flow, timestamp)` samples: no controller, no
thread, no clock. That is what makes it testable without hardware, and it
matters here because `tests/conftest.py` patches `time.time` and `time.sleep`
process-wide -- a rule that read the clock itself could not be tested at all.

Deciding is separated from acting. `sample()` returns a fault rather than
raising or touching the pump, so the caller chooses what a fault means: `warn`
logs it, `stop` halts the draw and raises it.
"""

import threading
import time

from .experiment_worker import OperationError


def band(expected_ul_min, tolerance_fraction):
    """The healthy range around an expected rate, as (low, high).

    One definition, used by both the rule and the message it produces --
    they had drifted apart already, one applying abs() and one not.

    Magnitude, not sign: the sensor reads negative or positive during a draw
    depending on which way round it sits in the line, and which way that is
    should not change the rule.
    """
    magnitude = abs(expected_ul_min)
    return (magnitude * (1 - tolerance_fraction),
            magnitude * (1 + tolerance_fraction))


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

        low, high = band(expected_ul_min, tolerance_fraction)
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

        # Fixed for the life of the monitor, so computed once rather than on
        # every sample -- and so the band reads as the invariant it is.
        self.low_ul_min, self.high_ul_min = band(expected_ul_min,
                                                 tolerance_fraction)

        self._started_at = None
        self._out_of_band_since = None
        self._consecutive = 0

    def in_band(self, flow):
        """Whether a reading counts as healthy.

        An invalid reading (None) is never in band -- a sensor that stopped
        reporting mid-draw means nothing can be verified, which is not the same
        as being fine.
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


class DrawGuard:
    """Watches every armed sensor for the duration of one draw.

    Wrap the blocking pump call:

        with DrawGuard(sensors, expected, stop_pump=self.sp.stop) as guard:
            self.sp.execute()
            guard.raise_if_faulted()

    Arming and disarming are the context manager's job because the failure
    path is the one that matters: a draw that raises must still leave the
    packet stream clean, or every later draw accumulates another dead handler
    faulting against a stale expectation.

    A trip is also published back to the sensor that saw it, via
    sensor.notify_fault(mode, fault, timestamp), so anything recording that
    sensor (the GUI's CSV) can file the verdict beside the readings it was
    made from. `log` stays the operator-facing channel; notify_fault is the
    per-sensor record.
    """

    def __init__(self, sensors, expected_ul_min, stop_pump, log=print):
        self.sensors = sensors
        self.expected_ul_min = expected_ul_min
        self.stop_pump = stop_pump
        self.log = log

        self._handlers = []
        self._lock = threading.Lock()
        self._fault = None
        self._active = False

    @property
    def fault(self):
        """The first fault a `stop` sensor saw, or None. Set from the reader
        thread. A `warn` sensor never fills this -- warn logs and nothing more,
        so a fault recorded here always means the draw is being failed."""
        with self._lock:
            return self._fault

    def __enter__(self):
        started_at = time.time()
        with self._lock:
            self._active = True
        for sensor in self.sensors:
            # Read once, here, so a mid-draw flip from the GUI cannot change
            # the rules underneath a draw already running.
            mode = sensor.monitor
            if mode == "off":
                continue
            rule = FlowMonitor(
                sensor.name,
                expected_ul_min=self.expected_ul_min,
                ramp_up_seconds=sensor.ramp_up_seconds,
                tolerance_fraction=sensor.tolerance_fraction,
            )
            rule.start(started_at)
            handler = self._make_handler(sensor, mode, rule)
            self._handlers.append((sensor, handler))
            sensor.subscribe(handler)
        return self

    def __exit__(self, exc_type, exc, tb):
        # Disarm before unsubscribing, and under the lock. Subscribers.notify
        # snapshots the callback list and dispatches outside its own lock, so a
        # handler can still be entered after unsubscribe() has returned -- by
        # which point the draw is over and the pump may already be executing
        # the next chain. Without this, that late sample could terminate a
        # chain it knows nothing about, and record the fault on a guard nobody
        # will ever check.
        with self._lock:
            self._active = False
        for sensor, handler in self._handlers:
            sensor.unsubscribe(handler)
        self._handlers.clear()
        return False

    def raise_if_faulted(self):
        """Raise the fault a `stop` sensor recorded, if there was one.

        Separate from the callback so the raise happens on the thread running
        the sequence. Raising from the reader thread would only kill the reader.
        """
        fault = self.fault
        if fault is not None:
            raise fault

    def _make_handler(self, sensor, mode, rule):
        """One closure per sensor, holding its own rule and mode."""
        stop = (mode == "stop")
        done = False

        def handle(flow, timestamp):
            nonlocal done
            # The rule keeps faulting on every sample once it has tripped, and
            # this handler has nothing more to say after the first: warn has
            # logged, and stop has either claimed the draw or lost it. Bailing
            # here rather than re-deriving a fault to discard also keeps the
            # reader thread's per-packet work flat for the rest of the draw.
            if done:
                return

            fault = rule.sample(flow, timestamp)
            if fault is None:
                return
            done = True

            if not stop:
                if self._still_running():
                    self.log(f"WARNING: {fault}")
                    sensor.notify_fault(mode, fault, timestamp)
                return

            claimed, was_active = self._claim(fault)
            if not was_active:
                # A late sample: the draw is over and this guard no longer
                # speaks for anything, including the sensor's own record.
                # was_active comes from inside _claim's locked section, not a
                # second _still_running() read: the winner's stop unblocks the
                # sequence thread, which can disarm the guard before this
                # (losing) handler gets here, and a re-read would then misfile
                # an in-draw trip as late.
                return
            if claimed:
                self.log(f"Stopping draw: {fault}")
                # terminateCmd() from the reader thread, while the sequence
                # thread sits in wait_for_stop -- the same crossing abort()
                # already makes from the Qt thread. If it raises we must not
                # take the reader down with it; the fault is already recorded
                # and will still be raised.
                try:
                    self.stop_pump()
                except Exception as e:
                    self.log(f"Failed to stop the pump after a flow fault: {e}")
            # Published win or lose: losing the claim only means another sensor
            # faulted first, not that this one saw nothing. Its recording
            # should carry its own observation. After the pump halt, so the
            # bookkeeping never delays the stop.
            sensor.notify_fault(mode, fault, timestamp)

        return handle

    def _still_running(self):
        """Whether the draw is still ours to speak for. False once __exit__ has
        disarmed -- see the note there on late samples."""
        with self._lock:
            return self._active

    def _claim(self, fault):
        """Record `fault` if the draw is still running and nothing has faulted
        yet. Returns (claimed, was_active).

        claimed is the caller's permission to stop the pump -- checking and
        claiming under one lock leaves no window where a second sensor, or a
        sample arriving after __exit__, could stop a pump this guard no longer
        speaks for. First cause wins, and two sensors can fault on the same
        packet: without that the second overwrites the first and the operator
        is told about whichever sensor happened to be later in the list.

        was_active is whether the draw was still armed at that same locked
        moment, so a losing sensor can tell "another sensor beat me" (publish
        my own fault) from "the draw is already over" (stay silent) without a
        second, later read of _active.
        """
        with self._lock:
            if not self._active:
                return False, False
            if self._fault is not None:
                return False, True
            self._fault = fault
            return True, True
