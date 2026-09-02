"""Sensor sample buffers, shared by every GUI that plots this system's
sensors.

`SensorSeries` is the one buffer in this package: producer threads
append, a GUI reads a window on its own clock. The standalone tabs'
plots hold their series in these (`fluidics.qt.sensor_plots`), and an
embedding application can hold its own.

Recording is not here. Each plot writes its own CSV, whose columns are
named for what it holds and whose flow recording carries the
draw-protection fault column that is a `warn` fault's only durable
trace (`fluidics.qt.sensor_plots.FlowSensorWidget`).
"""

import threading
import time
from collections import deque


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
