"""The sipper on the stage: the verbs the operations call (spec §2.3).

current_well is the witness the no-air rule reads (spec D9): set only
while the tip sits at dip depth in a well, cleared by retract, home, a
park in air, and for the length of an agitation (the tip leaves dip depth,
so a draw interrupted there would start from the wrong depth); a park in
the park well sets it, since the tip is wet there. Every plate's corners
are checked against the stage's strokes when the sipper is built and when
a placement is taught, so a plate the stage cannot reach fails bring-up
with the plate and the well named, not a run hours in. So is its
agitation stroke against travel height (the config's rule, here too so
the verb that sets the witness never vouches for a tip in air), and a new
travel height against every plate (check_travel).
"""

from ..errors import StageError
from .labware import WellRef  # noqa: F401  (the type callers hand in)


class RobotSipper:
    def __init__(self, stage, placements, default_reservoir, agitate_amplitude_mm,
                 park_xy_mm=None, park_well=None):
        self._stage = stage
        self._placements = {}
        self.default_reservoir = default_reservoir
        self.agitate_amplitude_mm = float(agitate_amplitude_mm)
        self._park_xy = park_xy_mm
        self._park_well = park_well
        self.current_well = None
        self.moves = stage.moves
        for name, placement in placements.items():
            self.set_placement(name, placement)
        if park_well is not None:
            self._placement(park_well).xy_mm(park_well.well)

    @property
    def placements(self):
        return dict(self._placements)

    def set_placement(self, name, placement):
        """Install (or replace, after teaching) a plate, once every corner
        of it is within reach and its agitation stroke stays below travel
        height. Clears the witness: the taught depth or corner may have
        moved the wells. A refused placement changes nothing."""
        self._check_agitation(name, placement, self._stage.travel_z_mm)
        for well, (x, y) in placement.corners():
            try:
                self._stage.check_reach(x, y, placement.dip_z_mm)
            except StageError as e:
                raise StageError(f"plate {name!r}: well {well} is out of reach: {e}") from None
        self._placements[name] = placement
        self.current_well = None

    def check_travel(self, travel_z_mm):
        """StageError naming the first installed plate whose agitation
        stroke would reach `travel_z_mm`; no motion. A new travel height
        is checked here before it is set."""
        for name, placement in self._placements.items():
            self._check_agitation(name, placement, travel_z_mm)

    def _check_agitation(self, name, placement, travel_z_mm):
        """Z grows toward the deck: the top of the stroke, dip less the
        amplitude, must be deeper than travel height, or agitate (and a
        draw after it) runs with the tip in air."""
        if not placement.dip_z_mm - self.agitate_amplitude_mm > travel_z_mm:
            raise StageError(f"plate {name!r}: a dip depth of {placement.dip_z_mm:.2f} mm leaves "
                             f"no room for the {self.agitate_amplitude_mm:g} mm agitation stroke "
                             f"below travel height {travel_z_mm:.2f} mm (the tip would agitate "
                             "in air)")

    def _placement(self, ref):
        try:
            return self._placements[ref.reservoir]
        except KeyError:
            raise ValueError(f"no reservoir named {ref.reservoir!r}: "
                             f"this rig has {', '.join(sorted(self._placements))}") from None

    def go_to_well(self, ref):
        plate = self._placement(ref)
        x, y = plate.xy_mm(ref.well)          # refuses a well the plate lacks
        self.current_well = None
        self._stage.retract()
        self._stage.move_xy(x, y)
        self._stage.move_z(plate.dip_z_mm)
        self.current_well = ref

    def agitate(self, cycles):
        """Up by the amplitude and back, `cycles` times, ending at dip
        depth. The witness is down for the duration: an interrupted cycle
        leaves the tip above dip depth, where the residual is wrong."""
        if self.current_well is None:
            raise StageError("agitate refused: the sipper is not in a well")
        ref = self.current_well
        dip = self._placement(ref).dip_z_mm
        self.current_well = None
        for _ in range(cycles):
            self._stage.move_z(dip - self.agitate_amplitude_mm)
            self._stage.move_z(dip)
        self.current_well = ref

    def retract(self):
        self.current_well = None
        self._stage.retract()

    def park(self):
        self.retract()
        if self._park_well is not None:
            self.go_to_well(self._park_well)
        elif self._park_xy is not None:
            self._stage.move_xy(*self._park_xy)

    def home(self):
        self.current_well = None
        self._stage.home()

    def position(self):
        return self._stage.position()

    def halt(self):
        self.current_well = None
        self._stage.halt()

    def close(self, quiescent=True):
        """Quiescent on a homed stage: park, then the stage's close without
        its retract, so the tip stays wet in the park well (a park that
        failed still ends retracted). Otherwise the stage's close alone,
        which makes no move here. A closed robot vouches for no well.
        Returns the exceptions the steps raised."""
        self.current_well = None
        errors = []
        parked = False
        if quiescent and self._stage.is_homed:
            try:
                self.park()
                parked = True
            except Exception as e:
                errors.append(e)
        errors.extend(self._stage.close(quiescent=quiescent, retract=not parked))
        self.current_well = None
        return errors
