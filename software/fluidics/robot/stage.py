"""The three axes as one thing (spec §2.2).

Z is 0 at the retracted top and increases toward the deck. XY moves only
while retracted. Every leg: pause gate, pre-check, dispatch without
waiting, then a poll through run_control.sleep (which raises on cancel);
a leg is complete when the drive reports target reached AND the position
agrees; anything that ends a leg early -- a cancel, a fault, a limit
switch, a move that ended off its target, a timeout, a driver or port
error -- halts every axis that may have started, re-reads where the axes
stopped, and only then lets the error go on. A stop that itself fails is
reported, never swallowed. The cached position is the witness the XY rule
reads, so it is dropped before a leg can leave it wrong and re-read after
any early end; while it is unknown, XY is refused. Position is published
after each leg and after each halt, never polled by a display.

The drives (nm-servo PR #10, measured): a move a limit switch ends leaves
the drive enabled with the warning bit and alarm 8614h, and target
reached never comes, so the poll reads the alarm itself rather than wait
out the timeout (the driver's own LimitSwitchError belongs to its
blocking wait, which the stage does not use). A marginal switch can also
let the drive report the target reached short of it; target reached with
the position off, once the axis has been seen in flight, ends the leg
too. Before the axis is seen in flight the bit may still be the previous
move's, so it is not trusted then; under an instant simulation, which
never shows an axis in flight, such a move ends in the timeout instead.
"""

import logging
import time

from ..errors import DeviceError, StageError
from ..subscribers import Subscribers
from .simulation import AxisReading, HomingReading  # noqa: F401  (what both services return)

_logger = logging.getLogger(__name__)

POLL_SECONDS = 0.05
TOLERANCE_MM = 0.05
LIMIT_SWITCH_ALARM = 0x8614   # 603Fh while a limit switch holds the axis (NiMotion B06 table 8-1)


class XYZStage:
    def __init__(self, service, axes, travel_z_mm, xy_velocity_mm_s,
                 z_velocity_mm_s, run_control, poll_seconds=POLL_SECONDS):
        self._service = service
        self._x, self._y, self._z = axes["x"], axes["y"], axes["z"]
        self.travel_z_mm = float(travel_z_mm)
        self._vxy = float(xy_velocity_mm_s)
        self._vz = float(z_velocity_mm_s)
        self._control = run_control
        self._poll = poll_seconds
        self._position = None          # (x, y, z) after the last leg; None = unknown
        self._homed = False
        self.moves = Subscribers("stage moves")

    # --- bring-up and shutdown ---

    def connect(self):
        """Connect, then enable every axis. The real service selects a
        configured axis and initializes all three inside its connect (see
        _driver.open_service and the driver notes in the spec)."""
        self._service.connect()
        for axis in (self._x, self._y, self._z):
            self._service.enable(axis)

    def home(self):
        """Z first, to the retracted end; X and Y only once Z is up. Ends
        with every axis read, so the position is known from here on."""
        self._homed = False
        self._position = None
        self._home_axis(self._z)
        z = self._service.read(self._z).position_mm
        if z > self.travel_z_mm + TOLERANCE_MM:
            raise DeviceError(f"axis {self._z} homed to {z:.2f} mm, below travel height "
                              f"{self.travel_z_mm:.2f} mm: either the homing seeks the deck end "
                              "(check the homing method) or the travel height is above where "
                              "homing parks (the drives park 0.1 to 0.5 mm past 0: set "
                              "travel_z_mm to 1 mm or more)")
        for axis in (self._x, self._y):
            self._home_axis(axis)
        self._refresh_position()
        self._homed = True

    def _home_axis(self, axis):
        """The drive's homing protocol, polled: the attained bit may read
        stale from the last homing, so it must clear (the axis has started)
        before it counts, and it counts only in homing mode; a fault ends
        it; the error bit is read only once the homing has started (before
        that it is the previous mode's following-error bit)."""
        self._control.checkpoint()
        started = False
        try:
            self._service.home(axis)
            deadline = time.monotonic() + self._service.homing_timeout_s(axis)
            while True:
                self._control.sleep(self._poll)
                reading = self._service.homing_state(axis)
                if reading.fault:
                    raise DeviceError(f"axis {axis}: drive fault during homing")
                if not started:
                    if not reading.attained:
                        started = True
                elif reading.error:
                    raise DeviceError(f"axis {axis}: homing error")
                elif reading.attained and reading.homing_mode:
                    return
                if time.monotonic() > deadline:
                    raise DeviceError(f"axis {axis}: homing did not complete in "
                                      f"{self._service.homing_timeout_s(axis):.0f} s")
        except BaseException as cause:
            self._end_early([axis], cause)
            raise

    def close(self, quiescent=True, retract=True):
        """Quiescent: retract, brake, disable, disconnect. Under a job that
        did not stop in time: no moves at all, a halt instead. Returns the
        exceptions the steps raised, each shielded.
        retract=False: a quiescent close makes no move (the sipper parked)."""
        steps = []
        if quiescent and self._homed:
            if retract:
                steps.append(self.retract)
        else:
            steps.append(self.halt)
        steps.append(lambda: self._service.engage_brake(self._z))
        steps.extend(lambda axis=axis: self._service.disable(axis)
                     for axis in (self._x, self._y, self._z))
        steps.append(self._service.disconnect)
        errors = []
        for step in steps:
            try:
                step()
            except Exception as e:
                errors.append(e)
                _logger.error("Error while closing the stage: %s", e)
        return errors

    # --- where the axes are ---

    @property
    def is_homed(self):
        return self._homed

    @property
    def is_retracted(self):
        """True only while the stage is homed, the position is known and Z
        is at or above travel height: anything less refuses XY."""
        return (self._homed and self._position is not None
                and self._position[2] <= self.travel_z_mm + TOLERANCE_MM)

    def position(self):
        """(x, y, z), or None while unknown (before homing, after a halt
        whose re-read failed)."""
        return self._position

    def check_reach(self, x=None, y=None, z=None):
        """StageError for any given coordinate outside its axis stroke;
        no motion. The sipper checks every plate's corners with this."""
        for axis, target in ((self._x, x), (self._y, y), (self._z, z)):
            if target is not None:
                self._check_stroke(axis, target)

    # --- moves ---

    def retract(self):
        self.move_z(self.travel_z_mm)

    def move_xy(self, x, y):
        if not self._homed:
            raise StageError("XY move refused: the stage is not homed")
        if self._position is None:
            raise StageError("XY move refused: the stage position is unknown; retract first")
        if not self.is_retracted:
            raise StageError(f"XY move refused: Z is at {self._position[2]:.2f} mm, "
                             f"below travel height {self.travel_z_mm:.2f} mm")
        self._check_stroke(self._x, x)
        self._check_stroke(self._y, y)
        z = self._position[2]
        self._leg({self._x: (x, self._vxy), self._y: (y, self._vxy)})
        self._position = (x, y, z)
        self._publish()

    def move_z(self, z):
        if not self._homed:
            raise StageError("Z move refused: the stage is not homed")
        self._check_stroke(self._z, z)
        known = self._position
        self._leg({self._z: (z, self._vz)})
        if known is None:
            self._refresh_position()
        else:
            self._position = (known[0], known[1], z)
            self._publish()

    def reset_faults(self):
        """Ask every drive to clear its fault and enable it again (the
        drive's fault reset leaves it disabled), then re-read the position.
        A fault that persists (encoder fault 7306h needs a power cycle)
        shows on the next move; the stage stays unhomed if it was."""
        for axis in (self._x, self._y, self._z):
            self._service.reset_fault(axis)
            self._service.enable(axis)
        self._refresh_position()

    def halt(self):
        """Stop every axis, re-read where they are. A stop that fails is
        raised as DeviceError after every axis has been tried."""
        failures = self._halt([self._x, self._y, self._z])
        self._refresh_position(shielded=True)
        if failures:
            raise DeviceError(self._stop_failure_message(failures))

    # --- the leg ---

    def _check_stroke(self, axis, target):
        low, high = self._service.stroke(axis)
        if not low <= target <= high:
            raise StageError(f"axis {axis}: {target:.2f} mm is outside the stroke "
                             f"{low:.2f}..{high:.2f} mm")

    def _leg(self, targets):
        """Dispatch every axis in `targets` ({axis: (mm, mm/s)}), then poll
        them together to completion. Any early end halts every axis that
        may have started, re-reads the position, and goes on raising."""
        self._control.checkpoint()
        longest = 0.0
        for axis, (target, velocity) in targets.items():
            reading = self._service.read(axis)
            if reading.fault or not reading.enabled:
                raise DeviceError(self._describe(axis, reading))
            longest = max(longest, abs(target - reading.position_mm) / velocity)
        self._control.check()          # a cancel that landed during the reads: dispatch nothing
        started = []
        self._position = None          # unknown until the leg ends, one way or the other
        try:
            for axis, (target, velocity) in targets.items():
                started.append(axis)   # before the call: a dispatch that raises may have moved the drive
                self._service.move_to(target, velocity, axis)
            deadline = time.monotonic() + 2 * longest + 1.0
            pending = dict(targets)
            in_flight = set()          # axes seen with the bit clear: the move has started
            while pending:
                self._control.sleep(self._poll)
                for axis in list(pending):
                    reading = self._service.read(axis)
                    if reading.fault or not reading.enabled:
                        raise DeviceError(self._describe(axis, reading))
                    target = pending[axis][0]
                    if reading.error_code == LIMIT_SWITCH_ALARM:
                        raise DeviceError(f"axis {axis}: move to {target:.2f} mm stopped by a "
                                          f"limit switch at {reading.position_mm:.2f} mm")
                    if not reading.target_reached:
                        in_flight.add(axis)
                    elif abs(reading.position_mm - target) <= TOLERANCE_MM:
                        del pending[axis]
                    elif axis in in_flight:
                        raise DeviceError(f"axis {axis}: move to {target:.2f} mm ended at "
                                          f"{reading.position_mm:.2f} mm (a limit switch?)")
                if pending and time.monotonic() > deadline:
                    axis = next(iter(pending))
                    raise DeviceError(f"axis {axis}: move to {pending[axis][0]:.2f} mm "
                                      "did not complete in time")
        except BaseException as cause:
            self._end_early(started, cause)
            raise

    def _end_early(self, started, cause):
        """Halt what may have started, re-read the position, and turn a
        failed stop into the error that goes on (chained to `cause`)."""
        failures = self._halt(started)
        self._refresh_position(shielded=True)
        if failures:
            raise DeviceError(self._stop_failure_message(failures)) from cause

    def _halt(self, axes):
        """Stop each axis, every one tried; returns [(axis, exception)]."""
        failures = []
        for axis in axes:
            try:
                self._service.stop(axis)
            except Exception as e:
                failures.append((axis, e))
                _logger.error("axis %s: stop failed: %s", axis, e)
        return failures

    @staticmethod
    def _stop_failure_message(failures):
        return "could not stop " + "; ".join(f"axis {axis}: {e}" for axis, e in failures)

    def _refresh_position(self, shielded=False):
        """Read all three axes into the cache and publish; with `shielded`,
        a read that fails leaves the position unknown instead of raising."""
        try:
            self._position = tuple(self._service.read(axis).position_mm
                                   for axis in (self._x, self._y, self._z))
        except Exception as e:
            self._position = None
            if not shielded:
                raise
            _logger.error("The stage position could not be read after a halt: %s", e)
        self._publish()

    @staticmethod
    def _describe(axis, reading):
        if reading.fault:
            return f"axis {axis}: drive fault, error code 0x{reading.error_code:04X}"
        return f"axis {axis}: drive is not enabled"

    def _publish(self):
        self.moves.notify(self._position)
