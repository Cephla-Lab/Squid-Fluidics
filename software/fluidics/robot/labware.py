"""Plate geometry and well references, as plain values (spec §2.1).

A 96-well plate is 8 rows (A-H) by 12 columns (1-12) on a 9 mm pitch;
wells are addressed column-major (A1, B1, ... H1, A2, ...), the order the
rounds tool walks them. A placement is where A1 was taught, how deep the
tip dips, and which stage axis the columns and the rows run along: the
plate is axis-aligned with the stage (spec D10: one taught corner is
enough for a plate on a keyed nest) but may sit with its long side along
either axis, in either sense. On the XYG321-A the two plates sit side by
side along X with their columns along Y (spec §2.1).
"""

import re
import string
from collections import namedtuple

WellRef = namedtuple("WellRef", "reservoir well")

_WELL = re.compile(r"^([A-Z])([1-9][0-9]?)$")
_REF = re.compile(r"^(?:([A-Za-z0-9_-]+)/)?([A-Z][1-9][0-9]?)$")

SBS_PITCH_MM = 9.0

# A direction a plate's columns or rows can run along, as a unit step on the stage.
AXIS_DIRECTIONS = {"+x": (1.0, 0.0), "-x": (-1.0, 0.0), "+y": (0.0, 1.0), "-y": (0.0, -1.0)}


class PlateGeometry:
    def __init__(self, rows, columns):
        self.rows = rows
        self.columns = columns
        self._row_letters = string.ascii_uppercase[:rows]

    @classmethod
    def from_well_count(cls, count):
        if count == 96:
            return cls(8, 12)
        raise ValueError(f"unsupported plate: {count} wells (96 only)")

    def index(self, well):
        """(row, column), zero-based; ValueError for a well this plate lacks."""
        match = _WELL.match(well or "")
        if not match:
            raise ValueError(f"{well!r} is not a well name")
        letter, number = match.group(1), int(match.group(2))
        if letter not in self._row_letters or not 1 <= number <= self.columns:
            raise ValueError(
                f"{well} is not a well on an {self.rows} x {self.columns} plate")
        return self._row_letters.index(letter), number - 1

    def is_well(self, well):
        try:
            self.index(well)
        except ValueError:
            return False
        return True

    def wells(self):
        """Every well, column-major."""
        return [f"{letter}{column}" for column in range(1, self.columns + 1)
                for letter in self._row_letters]

    def corners(self):
        """The four corner wells: the reach check visits these."""
        last = self._row_letters[-1]
        return ["A1", f"A{self.columns}", f"{last}1", f"{last}{self.columns}"]


G96 = PlateGeometry.from_well_count(96)


def parse_ref(text, default_reservoir, geometry=G96):
    """`A3` on the default reservoir, or `ssct/A3`. Anything else is refused
    with the rule spelled out, so a typo fails at time zero; the well must
    exist on `geometry` (I1 and A13 are not wells)."""
    match = _REF.match(text or "")
    if not match:
        raise ValueError(
            f"{text!r} is not a well: write A1..H12, or reservoir/A1 "
            "(uppercase row letter, no spaces)")
    reservoir, well = match.groups()
    if not geometry.is_well(well):
        raise ValueError(f"{text!r}: {well} is not a well on an "
                         f"{geometry.rows} x {geometry.columns} plate")
    return WellRef(reservoir or default_reservoir, well)


def well_key(ref):
    """The ledger's and the log's spelling of a well."""
    return f"{ref.reservoir}/{ref.well}"


def expand_wells(items, geometry):
    """`["A1:H6", "C8"]` -> the wells, column-major within each range."""
    order = geometry.wells()
    out = []
    for item in items:
        if ":" in item:
            start, end = item.split(":", 1)
            first, last = order.index(_checked(start, geometry)), order.index(_checked(end, geometry))
            if last < first:
                raise ValueError(f"range {item} runs backwards")
            out.extend(order[first:last + 1])
        else:
            out.append(_checked(item, geometry))
    return out


def _checked(well, geometry):
    geometry.index(well)        # raises for a well the plate lacks
    return well


class PlatePlacement:
    """Where a plate's wells are: A1 as taught, then the pitch along
    `column_axis` per column and along `row_axis` per row."""

    def __init__(self, a1_xy_mm, dip_z_mm, geometry, well_spacing=1,
                 pitch_mm=SBS_PITCH_MM, column_axis="+x", row_axis="+y"):
        for name, value in (("column_axis", column_axis), ("row_axis", row_axis)):
            if value not in AXIS_DIRECTIONS:
                raise ValueError(f"{name} must be one of {sorted(AXIS_DIRECTIONS)}, not {value!r}")
        if column_axis[1] == row_axis[1]:
            raise ValueError("column_axis and row_axis must be perpendicular "
                             f"(got {column_axis} and {row_axis})")
        self.a1_xy_mm = (float(a1_xy_mm[0]), float(a1_xy_mm[1]))
        self.dip_z_mm = float(dip_z_mm)
        self.geometry = geometry
        self.pitch_mm = pitch_mm * well_spacing
        self.column_axis = column_axis
        self.row_axis = row_axis

    def xy_mm(self, well):
        row, column = self.geometry.index(well)
        cx, cy = AXIS_DIRECTIONS[self.column_axis]
        rx, ry = AXIS_DIRECTIONS[self.row_axis]
        return (self.a1_xy_mm[0] + self.pitch_mm * (column * cx + row * rx),
                self.a1_xy_mm[1] + self.pitch_mm * (column * cy + row * ry))

    def corners(self):
        """(well, (x, y)) for the four corner wells."""
        return [(well, self.xy_mm(well)) for well in self.geometry.corners()]
