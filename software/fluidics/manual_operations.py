"""The manual tab's verbs, as a headless object.

`ManualOperations(devices)` gives a script -- or the GUI -- the moves an
operator makes by hand: turn the selector valves to a reagent, draw or push a
volume through the syringe pump, empty it to waste, run the drain for a
while. Each verb blocks until the hardware has finished and raises whatever
the driver raises; the GUI runs them off its own thread. None of it is a
sequence: no tubing arithmetic, no overflow protection, no flow guard -- an
operator asking for 300 uL gets 300 uL.

The verbs go through the same gated driver calls a run does, on the
DeviceSet's RunControl, so the session's abort() stops a manual move the
way it stops a run.
"""

import logging

from .control.config import gantry
from .robot.stage import MIN_TRAVEL_Z_MM
from .vocabulary import sipper_vocabulary

_logger = logging.getLogger(__name__)


class ManualOperations:
    def __init__(self, devices):
        self.sp = devices.syringe_pump
        self.sv = devices.selector_valves
        self.dp = devices.disc_pump
        self.config = devices.config
        self.robot = devices.robot
        self.reservoirs = devices.reservoirs
        g = gantry(devices.config)
        self._gantry = g
        self._sipper_port = g.selector_valve_port if devices.robot is not None else None
        self._vocabulary = sipper_vocabulary(devices.config)

    # --- what the rig offers ---

    def flow_rates(self):
        """The flow rates the syringe pump can be asked for, in uL/min,
        fastest first."""
        return self.sp.available_flow_rates()

    def port_names(self):
        """The selector-valve ports as the operator names them."""
        return self.sv.get_port_names()

    def current_port(self):
        """The reagent port the valves are on now."""
        return self.sv.get_current_port()

    def held_volume_ul(self, refresh=True):
        """What the syringe holds: read fresh from the plunger, or -- with
        refresh=False -- as of the pump's last reading, which it takes after
        every move. The fresh read is a serial round trip; a display polling
        an idle pump has no reason to make one."""
        if refresh:
            self.sp.get_plunger_position()
        return self.sp.get_current_volume()

    # --- the verbs ---

    def open_port(self, port):
        """Turn the selector valves to reagent `port`."""
        _logger.info("Manual: selector valve to port %d.", port)
        self.sv.open_port(port)

    def extract(self, syringe_port, volume_ul, flow_rate_ul_min, on_started=None):
        """Draw `volume_ul` in through the pump's `syringe_port` at `flow_rate_ul_min`."""
        if (self._sipper_port is not None and self.sv.get_current_port() == self._sipper_port
                and self.robot.current_well is None):
            raise RuntimeError("the sipper is not in a well: go to a well first")
        _logger.info("Manual: extract %s uL through port %d at %s uL/min.",
                     volume_ul, syringe_port, flow_rate_ul_min)
        self._move("extract", (syringe_port, volume_ul), flow_rate_ul_min, on_started)

    def dispense(self, syringe_port, volume_ul, flow_rate_ul_min, on_started=None):
        """Push `volume_ul` out through the pump's `syringe_port` at `flow_rate_ul_min`."""
        _logger.info("Manual: dispense %s uL through port %d at %s uL/min.",
                     volume_ul, syringe_port, flow_rate_ul_min)
        self._move("dispense", (syringe_port, volume_ul), flow_rate_ul_min, on_started)

    def empty_to_waste(self, on_started=None):
        """Push whatever the syringe holds out to waste, as fast as the rig allows."""
        _logger.info("Manual: empty the syringe to waste.")
        self._move("dispense_to_waste", (), None, on_started)

    def aspirate(self, seconds, on_started=None):
        """Run the drain pump at full power for `seconds` (Open Chamber rigs)."""
        if self.dp is None:
            raise RuntimeError("this rig has no disc pump to aspirate with")
        _logger.info("Manual: aspirate for %s s.", seconds)
        if on_started is not None:
            on_started(seconds)
        self.dp.aspirate(seconds)

    # --- the sipper robot (spec §9): every verb a manual job ---

    def _need_robot(self):
        if self.robot is None:
            raise RuntimeError("this rig has no robot")

    def go_to_well(self, text):
        self._need_robot()
        ref = self._vocabulary.resolve(text)
        _logger.info("Manual: sipper to %s/%s.", ref.reservoir, ref.well)
        self.robot.go_to_well(ref)

    def retract(self):
        self._need_robot()
        self.robot.retract()

    def set_lower_tip(self, enabled):
        """"Do not lower tip" off (True) or on (False): with it on,
        go_to_well ends retracted over the well (no dip, no witness) and a
        run is refused at time zero. A setting, not a move: no job."""
        self._need_robot()
        self.robot.lower_tip = bool(enabled)

    def home_robot(self):
        self._need_robot()
        self.robot.home()

    def jog(self, axis, mm):
        """One axis by `mm` from where it is; XY refused below travel height
        or with the position unknown, by the stage itself."""
        self._need_robot()
        position = self.robot.position()
        if position is None:
            raise RuntimeError("the stage position is unknown: home first")
        x, y, z = position
        self.robot.current_well = None
        if axis == "z":
            self.robot._stage.move_z(z + mm)
        else:
            self.robot._stage.move_xy(x + (mm if axis == "x" else 0), y + (mm if axis == "y" else 0))

    def robot_position(self):
        self._need_robot()
        return self.robot.position()

    def reset_robot_faults(self):
        """Clear a drive fault from the Robot tab; home again afterwards
        if the fault interrupted a homing."""
        self._need_robot()
        self.robot.current_well = None
        self.robot._stage.reset_faults()

    # --- teaching: the config and the live robot change together, or neither ---

    def _reservoir_config(self, name):
        try:
            return next(r for r in self._gantry.reservoirs.values() if r.name == name)
        except StopIteration:
            raise ValueError(f"no reservoir named {name!r}") from None

    def teach_a1(self, name, xy_mm):
        """A1 of `name` is at `xy_mm` now: the live placement first (every
        corner must stay in reach), then the config value save_config
        writes. The current well is forgotten: the wells moved."""
        self._need_robot()
        r = self._reservoir_config(name)
        xy = (float(xy_mm[0]), float(xy_mm[1]))
        placement = self._vocabulary.placement(name)
        placement.a1_xy_mm = xy
        self.robot.set_placement(name, placement)
        r.a1_xy_mm = xy
        _logger.info("Taught: %s A1 at x %.2f, y %.2f.", name, *xy)

    def teach_dip(self, name, z_mm):
        self._need_robot()
        r = self._reservoir_config(name)
        z = float(z_mm)
        if self._gantry.travel_z_mm >= z - self._gantry.agitate_amplitude_mm:
            raise ValueError(f"a dip depth of {z:.2f} mm leaves no room for the "
                             f"{self._gantry.agitate_amplitude_mm:g} mm agitation stroke above "
                             f"travel height {self._gantry.travel_z_mm:.2f}")
        placement = self._vocabulary.placement(name)
        placement.dip_z_mm = z
        self.robot.set_placement(name, placement)
        r.dip_z_mm = z
        _logger.info("Taught: %s dip depth %.2f mm.", name, z)

    def teach_travel(self, z_mm):
        self._need_robot()
        z = float(z_mm)
        if z < MIN_TRAVEL_Z_MM:
            # Home, then "Set travel height here", would teach where homing
            # parked Z; the next homing that parks Z lower refuses it.
            raise ValueError(f"a travel height of {z:.2f} mm is above where homing may leave Z "
                             f"(the drives park 0.1 to 0.5 mm past 0, and homing refuses a travel "
                             f"height Z parks below): give {MIN_TRAVEL_Z_MM:g} mm or more")
        for r in self._gantry.reservoirs.values():
            if z >= r.dip_z_mm - self._gantry.agitate_amplitude_mm:
                raise ValueError(f"a travel height of {z:.2f} mm is not above {r.name}'s "
                                 f"agitation stroke (dip {r.dip_z_mm:.2f} less "
                                 f"{self._gantry.agitate_amplitude_mm:g})")
        # The live placements too: they are what go_to_well and agitate use.
        self.robot.check_travel(z)
        self.robot._stage.check_reach(z=z)
        self.robot._stage.travel_z_mm = z
        self.robot.current_well = None
        self._gantry.travel_z_mm = z
        _logger.info("Taught: travel height %.2f mm.", z)

    def refill(self, key):
        if self.reservoirs is None:
            raise RuntimeError("this rig tracks no reservoir volumes")
        self.reservoirs.refill(key)

    def _move(self, op, args, flow_rate_ul_min, on_started):
        """One syringe move: a fresh queue, the op, then the wait.

        `on_started`, if given, gets the pump's own time estimate once the
        move is queued and before it runs -- what a progress bar needs. The
        queue is reset first so a move never inherits ops a failed one left.
        """
        self.sp.reset_chain()
        code = (None if flow_rate_ul_min is None
                else self.sp.flow_rate_to_speed_code(flow_rate_ul_min))
        estimate = getattr(self.sp, op)(*args, code)
        if on_started is not None:
            on_started(estimate)
        self.sp.execute()
