"""What a well reference may say on this rig (spec §3): one owner of the
spelling, read by the validators, the dialog, the sequence list and the
operations. None on a rig without a gantry. Placements are built from the
config each time they are asked for, so a taught value is what the next
placement carries."""

from .control.config import gantry
from .robot.labware import PlatePlacement, parse_ref, unknown_reservoir


class SipperVocabulary:
    def __init__(self, sipper_port, default_reservoir, reservoirs):
        self.sipper_port = sipper_port
        self.default_reservoir = default_reservoir
        self.reservoirs = reservoirs            # name -> ReservoirConfig

    def resolve(self, text):
        ref = parse_ref(text, self.default_reservoir)
        if ref.reservoir not in self.reservoirs:
            raise ValueError(unknown_reservoir(ref.reservoir, self.reservoirs))
        return ref

    def well_names(self, reservoir):
        """(well, label) per well, column-major, labelled from well_names."""
        names = self.reservoirs[reservoir].well_names or {}
        return [(well, f"{well}: {names[well]}" if well in names else well)
                for well in self.reservoirs[reservoir].geometry.wells()]

    def well_name(self, key):
        """The config's label for the well a ledger key names (`ssct/A1`),
        or None where the plate gives it none: what the Reservoirs window
        calls a well's reagent."""
        plate, _, well = key.partition("/")
        r = self.reservoirs.get(plate)
        return None if r is None else (r.well_names or {}).get(well)

    def placement(self, name):
        """The plate as the config has it now."""
        r = self.reservoirs[name]
        return PlatePlacement(r.a1_xy_mm, r.dip_z_mm, r.geometry,
                              well_spacing=r.well_spacing,
                              column_axis=r.column_axis, row_axis=r.row_axis)

    def placements(self):
        return {name: self.placement(name) for name in self.reservoirs}


def sipper_vocabulary(config):
    g = gantry(config)
    if g is None:
        return None
    return SipperVocabulary(g.selector_valve_port, g.default_reservoir,
                            {r.name: r for r in g.reservoirs.values()})
