"""Qt-free sensor sample buffers, and a long-format recorder for embedders.

`SensorSeries` is the one sample buffer in this package: producer threads
append, a GUI reads a window on its own clock. The standalone tabs' plots
hold their series in these (`fluidics.qt.sensor_plots`), and so does
`SensorRecorder`, so there is one implementation of "keep the last N
samples and hand me the last M seconds". One implementation, not one
stream: a plot's series holds what survived its query-interval throttle,
while a recorder's holds every sample it was handed.

`SensorRecorder` adds a CSV in long format -- `time,channel,value,step` --
for an application embedding these widgets (Squid's Fluidics display tab)
that wants every channel in one file, tagged with the protocol step that
was running. The standalone tabs deliberately do NOT record through it:
each plot writes its own wide CSV, whose columns are named for what they
hold and whose flow recording carries the draw-protection fault column
(`fluidics.qt.sensor_plots.FlowSensorWidget`). Two artifacts, because
they answer to different readers; one buffer, because that part is the
same. Neither raises on I/O -- a recording that fails stops and says so
in the log rather than taking the run with it.
"""

import csv
import logging
import threading
import time
from collections import deque

_logger = logging.getLogger(__name__)

# A recording is flushed at most this often rather than per sample: at a
# flow sensor's ~17 Hz that would be a syscall per sample, taken on the
# reader thread and under this recorder's lock. What a crash can cost is
# bounded by this instead -- measured on the monotonic clock, never on the
# sample's own timestamp, which the caller supplies and may be historic,
# replayed out of order, or stepped by NTP.
FLUSH_INTERVAL_SECONDS = 1.0


class SensorSeries:
    """One channel's samples, newest last, capped at `maxlen`.

    Timestamps are expected in order -- `window` walks back from the
    newest and stops at the cutoff, so a caller handing them back out of
    order gets a short slice rather than an error (which is also why the
    recorder's flush deadline reads the monotonic clock, not these).
    `maxlen` is the caller's to size: hold what you can actually ask for.
    The default is 10 hours at 1 Hz, about 35 minutes at a flow sensor's
    full rate.
    """

    def __init__(self, maxlen=36000):
        self._t = deque(maxlen=maxlen)
        self._v = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def append(self, value, t=None):
        with self._lock:
            self._t.append(time.time() if t is None else t)
            self._v.append(value)

    def window(self, seconds=None):
        """(times, values) for the last `seconds`, or everything held.

        Walked from the newest back to the cutoff rather than copied whole
        and sliced: the window is small and the buffer need not be, so the
        cost is the answer's size instead of the history's."""
        with self._lock:
            if seconds is None:
                return list(self._t), list(self._v)
            if not self._t:
                return [], []
            cutoff = self._t[-1] - seconds
            times, values = [], []
            for t, v in zip(reversed(self._t), reversed(self._v)):
                if t < cutoff:
                    break
                times.append(t)
                values.append(v)
        times.reverse()
        values.reverse()
        return times, values


class SensorRecorder:
    """Per-channel buffers plus an operator-toggled long-format CSV."""

    def __init__(self):
        self._series = {}
        self._lock = threading.RLock()
        self._file = None
        self._writer = None
        self._step = ""
        self._flushed_at = 0.0

    def channel(self, name):
        with self._lock:
            return self._series.setdefault(name, SensorSeries())

    @property
    def recording(self):
        return self._file is not None

    def set_step_label(self, label):
        """What the run is doing, tagged onto the rows written from now on."""
        self._step = label or ""

    def record(self, name, value, t=None):
        t = time.time() if t is None else t
        self.channel(name).append(value, t)
        with self._lock:
            if self._writer is None:
                return
            try:
                self._writer.writerow([f"{t:.3f}", name, value, self._step])
                # Time-based, not per row: see FLUSH_INTERVAL_SECONDS.
                now = time.monotonic()
                if now - self._flushed_at >= FLUSH_INTERVAL_SECONDS:
                    self._file.flush()
                    self._flushed_at = now
            except (OSError, csv.Error) as e:
                _logger.warning("Sensor CSV write failed; stopping the "
                                "recording: %s", e)
                self.stop_recording()

    def record_fault(self, name, mode, fault, t=None):
        """A draw-protection trip, filed on its own channel beside the
        sensor's readings. The recording is the only durable trace a
        `warn` leaves -- the operator's notice is cleared at the next run
        -- so a long-format recording has to carry it too, or the rig
        embedding these widgets keeps a record that quietly omits the one
        event worth keeping."""
        self.record(f"{name}.fault", f"{mode}: {fault}", t)

    def start_recording(self, path):
        """Open `path` and write the header; True if the recording started."""
        with self._lock:
            self.stop_recording()
            try:
                self._file = open(path, "w", newline="", encoding="utf-8")
                self._writer = csv.writer(self._file)
                self._writer.writerow(["time", "channel", "value", "step"])
                self._file.flush()
                self._flushed_at = time.monotonic()
                return True
            except OSError as e:
                _logger.warning("Could not start the sensor recording at "
                                "%s: %s", path, e)
                self._file = None
                self._writer = None
                return False

    def stop_recording(self):
        """Close the recording if one is open. Idempotent."""
        with self._lock:
            if self._file is not None:
                try:
                    self._file.close()      # flushes the tail on the way out
                except OSError as e:
                    _logger.warning("Sensor recording did not close "
                                    "cleanly: %s", e)
            self._file = None
            self._writer = None
