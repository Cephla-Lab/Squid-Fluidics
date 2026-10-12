"""The service a simulated stage runs over: the driver's shape, no driver.

Motion is modelled: a dispatched move advances at its velocity until it
arrives, so a poll can see an axis in flight, a stale target-reached bit,
or a halt landing mid-leg. Under `instant` the clock is virtual and every
read lands after the moves dispatched since the last read, so a leg costs
no real time but its travel is still counted (`travel_seconds`, the
longest axis of each leg, since X and Y run together). Every call is
logged for the tests; a test can plant a fault, freeze an axis, make a
stop or a read fail, make the target-reached bit read stale, put a limit
switch inside the stroke, or make a move end short of its target. Homing
presents the drive's real protocol: the attained bit reads stale once,
clears while the axis moves, then sets again in homing mode; it parks the
axis exactly at the stroke's low end (the drives park 0.1 to 0.5 mm past
it, which is why a travel height of 0 is refused on the real driver).
"""

import math
import time
from collections import namedtuple

AxisReading = namedtuple("AxisReading", "position_mm target_reached enabled fault error_code")
HomingReading = namedtuple("HomingReading", "attained error fault homing_mode")

# The XYG321-A's strokes as the driver's axis table has them (nm-servo PRs
# #5, #6, #8: switch spacing less a margin), keyed by driver axis name. A
# simulated rig uses these so a placement the real stage cannot reach is
# refused in simulation too.
XYG321A_STROKES_MM = {"X1": (0.0, 200.0), "Y2": (0.0, 102.0), "Z3": (0.0, 58.5)}

HOMING_POLLS = 3          # reads between the stale bit clearing and homing attained
LIMIT_SWITCH_ALARM = 0x8614   # 603Fh while a limit switch holds the axis (NiMotion B06 table 8-1)

_Move = namedtuple("_Move", "start target velocity t0")


class SimulatedService:
    def __init__(self, axes, strokes, instant=False):
        axes = list(axes)
        self._strokes = {axis: tuple(strokes[axis]) for axis in axes}
        self._position = {axis: 0.0 for axis in axes}     # at the last event
        self._target = dict(self._position)
        self._moves = {}                                   # axis -> _Move in flight
        self._enabled = {axis: False for axis in axes}
        self._homed = {axis: False for axis in axes}
        self._homing = {}                                  # axis -> polls since home()
        self._frozen = set()
        self._faults = {}                                  # axis -> (polls until, error_code)
        self._stop_failures = {}
        self._read_failures = {}
        self._limits = {}                                  # axis -> mm of a switch inside the stroke
        self._on_limit = set()                             # axes a switch stopped, alarm 8614h showing
        self._short = {}                                   # axis -> mm where the next move ends, "reached"
        self._polls = {axis: 0 for axis in axes}
        self.instant = instant
        self._clock = 0.0
        self._leg = []                 # travel of the moves dispatched since the last read
        self.travel_seconds = 0.0
        self.stale_target_reached = False
        self.on_poll = None            # a test hook, called on every read()
        self.log = []
        self.connected = False

    # --- the driver's shape ---

    def connect(self):
        self.connected = True
        self.log.append(("connect", None))

    def disconnect(self):
        self.connected = False
        self.log.append(("disconnect", None))

    def enable(self, axis):
        self._enabled[axis] = True
        self.log.append(("enable", axis))

    def disable(self, axis):
        self._enabled[axis] = False
        self.log.append(("disable", axis))

    def engage_brake(self, axis):
        self.log.append(("engage_brake", axis))

    def stroke(self, axis):
        return self._strokes[axis]

    def homing_timeout_s(self, axis):
        return 60.0

    def move_to(self, position_mm, velocity_mm_s, axis):
        self.log.append(("move_to", axis))
        now = self._now()
        start = self._position_at(axis, now)
        self._position[axis] = start
        self._on_limit.discard(axis)       # a new set-point leading away clears the alarm
        short = self._short.pop(axis, None)
        if short is not None:
            position_mm = short            # the drive stops early and still reports the target reached
        self._target[axis] = position_mm
        self._moves[axis] = _Move(start, position_mm, float(velocity_mm_s), now)
        if axis not in self._frozen:
            self._leg.append(abs(position_mm - start) / velocity_mm_s)

    def home(self, axis):
        self.log.append(("home", axis))
        self._homed[axis] = False
        self._homing[axis] = 0

    def homing_state(self, axis):
        self._poll(axis)
        fault = self._fault_reading(axis)
        if fault is not None:
            return HomingReading(False, False, True, True)
        polls = self._homing.get(axis)
        if polls is None:
            return HomingReading(self._homed[axis], False, False, False)
        self._homing[axis] = polls + 1
        if polls == 0:
            return HomingReading(True, False, False, True)      # stale, from the last homing
        if polls <= HOMING_POLLS or axis in self._frozen:
            return HomingReading(False, False, False, True)     # the axis is moving
        del self._homing[axis]
        self._moves.pop(axis, None)
        self._position[axis] = self._target[axis] = self._strokes[axis][0]
        self._homed[axis] = True
        return HomingReading(True, False, False, True)

    def read(self, axis):
        self._poll(axis)
        failure = self._read_failures.get(axis)
        if failure is not None and self._polls[axis] >= failure[0]:
            del self._read_failures[axis]
            raise failure[1]
        fault = self._fault_reading(axis)
        if fault is not None:
            return fault
        position = self._position_at(axis, self._now())
        limit = self._limits.get(axis)
        if limit is not None and axis in self._moves and self._crossed(axis, limit, position):
            self._on_limit.add(axis)       # as the drives do: enabled, alarm 8614h, never target reached
            del self._moves[axis]
            self._position[axis] = self._target[axis] = limit
        if axis in self._on_limit:
            return AxisReading(self._position[axis], False, self._enabled[axis], False, LIMIT_SWITCH_ALARM)
        arrived = axis not in self._moves or math.isclose(position, self._target[axis], abs_tol=1e-9)
        if arrived and axis in self._moves:
            del self._moves[axis]
            self._position[axis] = self._target[axis]
            position = self._target[axis]
        reached = arrived or self.stale_target_reached
        return AxisReading(position, reached, self._enabled[axis], False, 0)

    def stop(self, axis):
        self.log.append(("stop", axis))
        failure = self._stop_failures.pop(axis, None)
        if failure is not None:
            raise failure
        self._position[axis] = self._position_at(axis, self._now())
        self._moves.pop(axis, None)
        self._target[axis] = self._position[axis]
        self._on_limit.discard(axis)

    # --- test hooks ---

    def fault(self, axis, after_polls=1, error_code=0x1000):
        """The axis reads faulted from its `after_polls`-th poll from now."""
        self._faults[axis] = (self._polls[axis] + after_polls, error_code)

    def freeze(self, axis):
        """The axis never arrives (and never homes)."""
        self._frozen.add(axis)

    def limit_switch(self, axis, at_mm):
        """A limit switch inside the stroke at `at_mm`: a move that reaches
        it stops there and reads enabled, not reached, alarm 8614h until
        a stop or a move away (what the drives do, nm-servo PR #10)."""
        self._limits[axis] = float(at_mm)

    def end_short(self, axis, at_mm):
        """The axis's next move ends at `at_mm` with target reached set, as
        X1's flickering far switch once made the drive do."""
        self._short[axis] = float(at_mm)

    def reset_fault(self, axis):
        self.log.append(("reset_fault", axis))
        self._faults.pop(axis, None)

    def fail_stop(self, axis, exc=None):
        self._stop_failures[axis] = exc or OSError(f"{axis}: no response to stop")

    def fail_read(self, axis, exc=None, after_polls=1):
        """The axis's `after_polls`-th read from now raises `exc`."""
        self._read_failures[axis] = (self._polls[axis] + after_polls,
                                     exc or OSError(f"{axis}: no response"))

    def position_of(self, axis):
        """Where the axis is right now, for a test's eyes."""
        return self._position_at(axis, self._now())

    # --- the model ---

    def _now(self):
        return self._clock if self.instant else time.monotonic()

    def _poll(self, axis):
        self._polls[axis] += 1
        if self._leg:
            longest = max(self._leg)
            self.travel_seconds += longest
            if self.instant:
                self._clock += longest
            self._leg = []
        if self.on_poll is not None:
            self.on_poll()

    def _fault_reading(self, axis):
        fault = self._faults.get(axis)
        if fault is not None and self._polls[axis] >= fault[0]:
            position = self._position_at(axis, self._now())
            return AxisReading(position, False, self._enabled[axis], True, fault[1])
        return None

    def _crossed(self, axis, limit, position):
        """Whether the move in flight has carried the axis onto `limit`."""
        start = self._moves[axis].start
        if self._target[axis] > start:
            return start < limit <= position
        return position <= limit < start

    def _position_at(self, axis, now):
        move = self._moves.get(axis)
        if move is None:
            return self._position[axis]
        if axis in self._frozen:
            return move.start
        distance = move.target - move.start
        travelled = move.velocity * (now - move.t0)
        if travelled >= abs(distance):
            return move.target
        return move.start + math.copysign(travelled, distance)
