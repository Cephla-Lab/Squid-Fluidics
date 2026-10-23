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

from .control.config import (assign_loaded_volumes, bottle_keys, gantry, loaded_volume_problem,
                             port_key, save_loaded_volumes)
from .errors import ReservoirWriteError
from .reservoirs import describe_keys
from .robot.labware import WellRef, agitation_problem, plate_of, ref_of, unknown_reservoir, well_key
from .robot.stage import MIN_TRAVEL_Z_MM
from .vocabulary import sipper_vocabulary

_logger = logging.getLogger(__name__)

# The sipper's dedicated wells, by the role the deck map marks them with:
# the gantry block's field that names each.
DEDICATED_WELLS = {"priming": "priming_well", "clean-up": "clean_up_well"}


def dedicated_wells(config):
    """{role: WellRef}: the dedicated wells as `config` names them now."""
    g, vocabulary = gantry(config), sipper_vocabulary(config)
    return {role: vocabulary.resolve(getattr(g, field)) for role, field in DEDICATED_WELLS.items()}


def _taught(mm):
    """A taught position, to 0.01 mm: what a position is taught to (the stage
    accepts 0.05 mm), and what the config file then reads -- 7.2, not the
    7.200000000000003 that 0.1 mm jogs add up to."""
    return round(float(mm), 2)


class ManualOperations:
    def __init__(self, devices):
        self.sp = devices.syringe_pump
        self.sv = devices.selector_valves
        self.dp = devices.disc_pump
        self.robot = devices.robot
        self.reservoirs = devices.reservoirs
        self._config = devices.config
        g = gantry(devices.config)
        self._gantry = g
        self._sipper_port = g.selector_valve_port if devices.robot is not None else None
        self._extract_port = devices.config.syringe_pump.extract_port
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
        # Only the extract port draws through the selector and the sipper
        # (the volume tracker's rule too); the pump's other ports never
        # reach them.
        if (syringe_port == self._extract_port and self._sipper_port is not None
                and self.sv.get_current_port() == self._sipper_port
                and self.robot.current_well is None):
            # The switch first: with it on, Go to well stops above the well,
            # and "go to a well" would send the operator back to the move
            # they just made.
            if not self.robot.lower_tip:
                raise RuntimeError("Do not lower tip is on: Go to well stops above the well, so "
                                   "nothing can be drawn through the sipper; switch it off on "
                                   "the Robot tab")
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
        self.robot.jog(axis, mm)

    def robot_position(self):
        self._need_robot()
        return self.robot.position()

    def reset_robot_faults(self):
        """Clear a drive fault from the Robot tab; home again afterwards
        if the fault interrupted a homing."""
        self._need_robot()
        self.robot.reset_faults()

    # --- teaching: the config and the live robot change together, or neither ---

    def _reservoir_config(self, name):
        try:
            return self._vocabulary.reservoirs[name]
        except KeyError:
            raise ValueError(unknown_reservoir(name, self._vocabulary.reservoirs)) from None

    def teach_a1(self, name, xy_mm):
        """A1 of `name` is at `xy_mm` now: the live placement first (every
        corner must stay in reach), then the config value save_config
        writes. The current well is forgotten: the wells moved."""
        self._need_robot()
        r = self._reservoir_config(name)
        xy = (_taught(xy_mm[0]), _taught(xy_mm[1]))
        placement = self._vocabulary.placement(name)
        placement.a1_xy_mm = xy
        self.robot.set_placement(name, placement)
        r.a1_xy_mm = xy
        _logger.info("Taught: %s A1 at x %.2f, y %.2f.", name, *xy)

    def teach_dip(self, name, z_mm):
        self._need_robot()
        r = self._reservoir_config(name)
        z = _taught(z_mm)
        problem = agitation_problem(name, z, self._gantry.agitate_amplitude_mm, self._gantry.travel_z_mm)
        if problem is not None:
            raise ValueError(problem)
        placement = self._vocabulary.placement(name)
        placement.dip_z_mm = z
        self.robot.set_placement(name, placement)
        r.dip_z_mm = z
        _logger.info("Taught: %s dip depth %.2f mm.", name, z)

    def teach_travel(self, z_mm):
        self._need_robot()
        z = _taught(z_mm)
        if z < MIN_TRAVEL_Z_MM:
            # Home, then "Set travel height here", would teach where homing
            # parked Z; the next homing that parks Z lower refuses it.
            raise ValueError(f"a travel height of {z:.2f} mm is above where homing may leave Z "
                             f"(the drives park 0.1 to 0.5 mm past 0, and homing refuses a travel "
                             f"height Z parks below): give {MIN_TRAVEL_Z_MM:g} mm or more")
        for r in self._gantry.reservoirs.values():
            problem = agitation_problem(r.name, r.dip_z_mm, self._gantry.agitate_amplitude_mm, z)
            if problem is not None:
                raise ValueError(problem)
        # The live placements too: they are what go_to_well and agitate use.
        self.robot.set_travel(z)
        self._gantry.travel_z_mm = z
        _logger.info("Taught: travel height %.2f mm.", z)

    def set_dedicated_well(self, role, text):
        """The priming or the clean-up well (`role`, as DEDICATED_WELLS
        names them) is `text` from now on: resolved through the rig's
        vocabulary first, so a plate or a well this rig has not got changes
        nothing, then written to the config as reservoir/well -- the value
        Save writes, and the one the operations read at the next priming or
        clean-up row. No move: the live robot holds no dedicated well."""
        self._need_robot()
        ref = self._vocabulary.resolve(text)
        key = well_key(ref)
        loaded = self.reservoirs.loaded(key)     # every well of every plate is tracked
        need = self._prime_shortfall(loaded)
        if need is not None:
            raise ValueError(f"{key} holds {loaded} uL when loaded, less than the {need} uL the "
                             f"sipper's line takes to prime: pick a fuller well")
        setattr(self._gantry, DEDICATED_WELLS[role], key)
        _logger.info("Taught: %s well %s.", role, key)

    def _prime_shortfall(self, loaded_ul):
        """What the sipper's line takes to prime, where a dedicated well
        holding `loaded_ul` when loaded is short of it, else None: priming
        and clean-up each draw the line full from their well."""
        need = self.sv.get_tubing_fluid_amount_to_port(self._gantry.selector_valve_port)
        return need if loaded_ul < need else None

    def refill(self, *keys):
        """Reset `keys` in the volume tracker, each `all`, a plate name, or
        one key -- a tracked reservoir declared full, an untracked bottle's
        accumulated figure zeroed -- all of them in one write or none. The
        tip's rule is asked of every key before anything changes."""
        for key in keys:
            refusal = self.refill_refusal(key)
            if refusal:
                raise RuntimeError(refusal)
        self.reservoirs.refill(*keys)

    def refill_refusal(self, key):
        """Why refill(key) would be refused for where the tip is, or None:
        the one statement of the rule, which the Reservoirs window asks
        before its question. With no park well the tip rests in a well between steps,
        and lifting that plate with the tip in it bends the tip: a refill of
        that plate (or of everything) waits for Retract. The witness names
        the well; without one -- a jog, a halt, a fault reset or teaching
        clears it where the tip is -- only a position known to be retracted
        vouches for the deck, so any plate's reload waits for Retract. A
        bottle is never under the tip."""
        plate = plate_of(key) or key         # the plate a key names: `ssct` or `ssct/A1`
        if self.robot is None or not (key == "all" or plate in self._vocabulary.reservoirs):
            return None                      # a bottle: never under the tip
        well = self.robot.current_well
        if well is not None:
            if key == "all" or plate == well.reservoir:
                # Any well of that plate: refilling one means the plate was lifted.
                return (f"the sipper's tip is in {well.reservoir}/{well.well}: "
                        f"Retract before reloading {well.reservoir}")
        elif not self.robot.is_retracted:
            return ("the sipper's tip is not known to be retracted: "
                    f"Retract before reloading {key}")
        return None

    def set_loaded_volume(self, keys, ul):
        """Every reservoir `keys` names -- bottles (`port_N`), wells
        (`plate/well`), or a plate's name for every well of it -- holds `ul`
        when loaded from now on: written to the config file's volume fields
        and nothing else of the config, then to the volume tracker, whose
        figures the next draw and the time-zero gate read. What each has
        given since its last reset stands and whether it is declared does
        not change (a bottle that becomes tracked comes in undeclared), so
        reloading with a new volume is this, then a reset.
        Refused before anything changes for what loaded_volume_refusal
        names. No tip rule: a number changes, no plate moves."""
        file_keys, named = self._loaded_volume_change(keys, ul)
        # The config file first, the live config last. If the volume
        # tracking write fails between them, the file has the new volume
        # while this session's config and volume tracker both keep the old
        # one, so the time-zero gate (whose twin is built from the config)
        # and the pump still read one figure; the next launch finds the
        # config's definition differing from the volume tracking file's and
        # brings that reservoir up undeclared until it is reset.
        save_loaded_volumes(self._config, file_keys, ul)
        try:
            self.reservoirs.set_loaded(named, ul)
        except ReservoirWriteError as e:
            raise ReservoirWriteError(
                f"{e}. The config file has the new volume already: this session keeps the "
                "old one, and the next launch brings it up undeclared until it is reset") from e
        assign_loaded_volumes(self._config, file_keys, ul)

    def loaded_volume_refusal(self, keys, ul=None):
        """Why set_loaded_volume(keys, ul) would be refused, or None: the
        one statement of its rules, which the Reservoirs window asks before
        its dialog, with ul None for what the keys alone decide (a key that
        names no tracked reservoir or plumbed bottle), and again with the
        value before it starts the job: a volume that is not a whole number
        of uL above 0, the priming or the clean-up well set below what the
        sipper's line takes to prime, a volume below what a reservoir has
        given since its last reset."""
        try:
            self._loaded_volume_change(keys, ul)
        except ValueError as e:
            return str(e)
        return None

    def _loaded_volume_change(self, keys, ul):
        """(file_keys, named) for set_loaded_volume: the keys the config
        file is given, a plate every one of whose wells is among `keys` as
        the plate, so the file says it once; and the volume tracker's,
        every well by its own key. ValueError naming the refusal; with ul
        None, the keys are checked and (None, None) returned."""
        if not keys:
            raise ValueError("no reservoir named: pick a bottle, a well or a plate")
        plates = self._vocabulary.reservoirs if self._vocabulary is not None else {}
        plumbed = set(bottle_keys(self._config))
        chosen, bottles = {}, []         # plate -> its wells named; bottle keys
        for key in keys:
            ref = ref_of(key) if isinstance(key, str) else None
            if isinstance(key, str) and key in plates:
                chosen.setdefault(key, set()).update(plates[key].geometry.wells())
            elif ref is not None and ref.reservoir in plates and plates[ref.reservoir].geometry.is_well(ref.well):
                chosen.setdefault(ref.reservoir, set()).add(ref.well)
            elif key in plumbed:
                bottles.append(key)
            elif self._gantry is not None and key == port_key(self._gantry.selector_valve_port):
                raise ValueError(f"{key} is the sipper's port, not a bottle: its wells are set "
                                 "by well or by plate")
            else:
                raise ValueError(f"{key} names no tracked reservoir or plumbed bottle on this rig")
        if ul is None:
            return None, None
        by_plate = {name: [well for well in plates[name].geometry.wells() if well in wells]
                    for name, wells in chosen.items()}
        named = [well_key(WellRef(name, well)) for name, wells in by_plate.items()
                 for well in wells] + bottles
        problem = loaded_volume_problem(ul)
        if problem:
            raise ValueError(f"{describe_keys(named, self.reservoirs.tracked)}: {problem}")
        file_keys = list(bottles)
        for name, wells in by_plate.items():
            if len(wells) == len(plates[name].geometry.wells()):
                file_keys.append(name)
            else:
                file_keys.extend(well_key(WellRef(name, well)) for well in wells)
        # set_dedicated_well's rule, kept as the volume moves.
        need = None if self._gantry is None else self._prime_shortfall(ul)
        if need is not None:
            for role, ref in dedicated_wells(self._config).items():
                if well_key(ref) in named:
                    raise ValueError(f"{well_key(ref)} is the {role} well: it must hold at least the "
                                     f"{need} uL the sipper's line takes to prime")
        refusal = self.reservoirs.loaded_refusal(named, ul)
        if refusal:
            raise ValueError(refusal)
        return file_keys, named

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
