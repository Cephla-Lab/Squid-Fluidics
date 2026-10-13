"""What a well reference may say on this rig (spec §3): one owner of the
spelling, read by the validators, the dialog, the sequence list and the
operations. None on a rig without a gantry. Placements are built from the
config each time they are asked for, so a taught value is what the next
placement carries."""

from .control.config import gantry
from .robot.labware import PlateGeometry, PlatePlacement, parse_ref


class SipperVocabulary:
    def __init__(self, sipper_port, default_reservoir, reservoirs):
        self.sipper_port = sipper_port
        self.default_reservoir = default_reservoir
        self.reservoirs = reservoirs            # name -> ReservoirConfig
        self._geometry = {name: PlateGeometry.from_well_count(r.number_of_wells)
                          for name, r in reservoirs.items()}

    def resolve(self, text):
        ref = parse_ref(text, self.default_reservoir)
        if ref.reservoir not in self.reservoirs:
            raise ValueError(f"no reservoir named {ref.reservoir!r}: this rig has "
                             + ", ".join(sorted(self.reservoirs)))
        if not self._geometry[ref.reservoir].is_well(ref.well):
            raise ValueError(f"{ref.well} is not a well on {ref.reservoir}")
        return ref

    def well_names(self, reservoir):
        """(well, label) per well, column-major, labelled from well_names."""
        names = self.reservoirs[reservoir].well_names or {}
        return [(well, f"{well}: {names[well]}" if well in names else well)
                for well in self._geometry[reservoir].wells()]

    def placement(self, name):
        """The plate as the config has it now."""
        r = self.reservoirs[name]
        return PlatePlacement(r.a1_xy_mm, r.dip_z_mm, self._geometry[name],
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
