"""The deck map: both plates drawn where their placements put them, the
sipper's tip painted where the stage last published it and colored by
whether it is retracted or lowered, the wells with a role (priming,
clean-up) marked, and a click on a well that names the well -- never a point between
wells, so a click can only ever send the tip to a well's centre. Drawn as
the operator faces the rig, a half turn from the stage's axes (pixel).

Pure Qt over plain labware values: the Robot tab feeds it placements, a
position, the current well and the named wells, and hears `wellClicked`.
It knows nothing of the robot, the session or the bus; nothing here polls.
"""

from collections import namedtuple

from qtpy.QtCore import QPointF, QRectF, QSize, Qt, Signal
from qtpy.QtGui import QColor, QFont, QPainter, QPen
from qtpy.QtWidgets import QSizePolicy, QWidget

from fluidics.robot.labware import AXIS_DIRECTIONS, WellRef, well_key

# The colors: the named wells, and the tip in each of its states.
ROLE_COLORS = {"priming": QColor("#4a90e2"), "clean-up": QColor("#9b9b9b")}
TIP_RETRACTED = QColor("#2e8b57")
TIP_LOWERED = QColor("#d0021b")
TIP_UNKNOWN = QColor("#e0a800")
WELL_PEN = QPen(QColor("#999"), 0.8)       # every well's rim
WELL_EMPTY = QColor("#ffffff")             # a well with no role

WELL_RADIUS_PITCH = 0.36       # a 6.4 mm well on a 9 mm pitch
HIT_RADIUS_PITCH = 0.45        # a click this close to a centre is that well; farther is nothing
TEXT_BAND_PX = 20              # the state line above the plates, the legend below
TIP_RADIUS_PX = 3.0            # the tip's dot: small, so the well under it stays visible

# The mm-to-pixel transform: px/mm, the pixel (left, bottom) the box's
# high corner lands on, and that corner in mm -- the half turn from the
# stage's axes puts the far corner bottom left.
Frame = namedtuple("Frame", "scale left bottom x1 y1")


class DeckMapWidget(QWidget):
    wellClicked = Signal(str, str)         # reservoir, well

    def __init__(self, extents_mm, parent=None):
        """extents_mm: {"x": (low, high), "y": (low, high)}, the XY strokes,
        so the tip is on the map wherever the stage can be."""
        super().__init__(parent)
        self._extents = {axis: (float(low), float(high)) for axis, (low, high) in extents_mm.items()}
        self._placements = {}
        self._roles = {}                   # WellRef -> [role, ...]
        self._position = None
        self._retracted = False
        self._current_well = None
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(300)

    def sizeHint(self):
        return QSize(560, 330)

    # --- what the tab feeds ---

    def setPlacements(self, placements):
        """{name: PlatePlacement}; re-fed after teaching, so a taught corner
        moves the drawn plate."""
        self._placements = dict(placements)
        self.update()

    def setNamedWells(self, named):
        """{role: WellRef}: "priming", "clean-up" (either may be absent)."""
        roles = {}
        for role, ref in named.items():
            if ref is not None:
                roles.setdefault(WellRef(*ref), []).append(role)
        self._roles = roles
        self.update()

    def setPosition(self, position, retracted):
        """The stage's published (x, y, z), or None while unknown; whether
        that z counts as retracted (the tab knows the travel height)."""
        self._position = None if position is None else tuple(float(v) for v in position)
        self._retracted = bool(retracted)
        self.update()

    def setCurrentWell(self, ref):
        self._current_well = None if ref is None else WellRef(*ref)
        self.update()

    @property
    def currentWell(self):
        return self._current_well

    # --- what the tab and the tests read back ---

    def stateText(self):
        if self._position is None:
            return "sipper: position unknown (home or retract)"
        x, y, z = self._position
        if self._retracted:
            over = self.wellAtXY(x, y)
            state = "retracted" + (f" over {well_key(over)}" if over is not None else "")
        else:
            state = "lowered" + (f" in {well_key(self._current_well)}" if self._current_well else "")
        return f"sipper: {state}   x {x:.1f}  y {y:.1f}  z {z:.1f} mm"

    def wellAtXY(self, x_mm, y_mm):
        """The well whose centre is within the hit radius of a deck point,
        or None: what the tip is over, read from its published XY."""
        for name, placement in self._placements.items():
            radius = HIT_RADIUS_PITCH * placement.pitch_mm
            for well in placement.geometry.wells():
                wx, wy = placement.xy_mm(well)
                if (wx - x_mm) ** 2 + (wy - y_mm) ** 2 <= radius ** 2:
                    return WellRef(name, well)
        return None

    def legend(self):
        """The roles present, a well with two roles listed once with both."""
        return [label for label, _ in self._legendEntries()]

    def _legendEntries(self):
        """(label, color) per distinct role combination, in label order;
        the color is the first role's, the one that fills the well."""
        combos = {}
        for roles in self._roles.values():
            combos.setdefault(" + ".join(roles), ROLE_COLORS[roles[0]])
        return [(f"{combo} well", combos[combo]) for combo in sorted(combos)]

    def pixel(self, x_mm, y_mm, frame=None):
        """Deck mm to widget pixels: the deck as the operator faces it, a
        half turn from the stage's axes, X to the left and Y down, so plate
        1's A1 (near the stage origin) sits top right. A paint passes the
        frame it computed once."""
        scale, left, bottom, x1, y1 = frame or self._frame()
        return left + (x1 - x_mm) * scale, bottom - (y1 - y_mm) * scale

    def wellAt(self, point):
        """The well whose centre is within the hit radius of `point` (a
        QPoint in widget pixels), or None: between wells is nowhere."""
        return self.wellAtXY(*self._mm(point.x(), point.y()))

    def _mm(self, px, py):
        """Widget pixels back to deck mm (the inverse of pixel())."""
        scale, left, bottom, x1, y1 = self._frame()
        return x1 - (px - left) / scale, y1 - (bottom - py) / scale

    # --- clicks ---

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.isEnabled():
            ref = self.wellAt(event.pos())
            if ref is not None:
                self.wellClicked.emit(ref.reservoir, ref.well)
                return
        super().mousePressEvent(event)

    # --- geometry ---

    def _box(self):
        """The mm box the map shows: the strokes, padded by a pitch and a
        half so the row and column labels outside a plate at the edge fit."""
        pad = 1.5 * max((p.pitch_mm for p in self._placements.values()), default=9.0)
        (x0, x1), (y0, y1) = self._extents["x"], self._extents["y"]
        return (x0 - pad, x1 + pad), (y0 - pad, y1 + pad)

    def _frame(self):
        """The Frame fitting the box between the text bands, centred
        horizontally."""
        (x0, x1), (y0, y1) = self._box()
        width = max(self.width(), 1)
        height = max(self.height() - 2 * TEXT_BAND_PX, 1)
        scale = min(width / (x1 - x0), height / (y1 - y0))
        left = (width - (x1 - x0) * scale) / 2
        bottom = TEXT_BAND_PX + height - (height - (y1 - y0) * scale) / 2
        return Frame(scale, left, bottom, x1, y1)

    # --- painting ---

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), self.palette().base())
        frame = self._frame()
        self._paintState(painter)
        for name, placement in self._placements.items():
            self._paintPlate(painter, name, placement, frame)
        self._paintTip(painter, frame)
        self._paintLegend(painter)
        painter.end()

    def _paintState(self, painter):
        if self._position is None:
            color = TIP_UNKNOWN
        else:
            color = TIP_RETRACTED if self._retracted else TIP_LOWERED
        painter.setFont(QFont("Helvetica", 10))
        painter.setPen(QPen(color))
        painter.drawText(QRectF(4, 0, self.width() - 8, TEXT_BAND_PX), Qt.AlignLeft | Qt.AlignVCenter,
                         self.stateText())

    def _paintPlate(self, painter, name, placement, frame):
        geometry = placement.geometry
        pitch = placement.pitch_mm
        radius = WELL_RADIUS_PITCH * pitch * frame.scale
        corners = [xy for _, xy in placement.corners()]
        xs, ys = [c[0] for c in corners], [c[1] for c in corners]
        pad = 0.75 * pitch
        # Both corners through the transform, then sorted: the turn lands
        # the low-mm corner right and top.
        xa, ya = self.pixel(min(xs) - pad, max(ys) + pad, frame)
        xb, yb = self.pixel(max(xs) + pad, min(ys) - pad, frame)
        left, right = sorted((xa, xb))
        top, bottom = sorted((ya, yb))
        painter.setPen(QPen(QColor("#444"), 1.2))
        painter.setBrush(QColor("#f7f7f7"))
        painter.drawRoundedRect(QRectF(left, top, right - left, bottom - top), 6, 6)
        painter.setFont(QFont("Helvetica", 9, QFont.Bold))
        painter.setPen(QPen(QColor("#222")))
        # Under the plate: the half turn puts the row letters along its top
        # edge and the column numbers along its right.
        painter.drawText(QPointF(left, bottom + 4 + painter.fontMetrics().ascent()),
                         f"{name}  (dip {placement.dip_z_mm:g} mm)")
        painter.setPen(WELL_PEN)
        for well in geometry.wells():
            cx, cy = self.pixel(*placement.xy_mm(well), frame)
            roles = self._roles.get(WellRef(name, well), ())
            painter.setBrush(ROLE_COLORS[roles[0]] if roles else WELL_EMPTY)
            painter.drawEllipse(QPointF(cx, cy), radius, radius)
            if len(roles) > 1:
                # Two roles on one well: the first fills it, the second rings it.
                painter.setPen(QPen(ROLE_COLORS[roles[1]], 2))
                painter.setBrush(Qt.NoBrush)
                painter.drawEllipse(QPointF(cx, cy), radius + 2, radius + 2)
                painter.setPen(WELL_PEN)
        # Row letters one pitch before column 1 along the column axis, column
        # numbers one pitch before row A along the row axis -- in deck mm, so
        # they sit beside their rows and columns in any orientation.
        painter.setFont(QFont("Helvetica", 7))
        painter.setPen(QPen(QColor("#555")))
        cx_dir, cy_dir = AXIS_DIRECTIONS[placement.column_axis]
        rx_dir, ry_dir = AXIS_DIRECTIONS[placement.row_axis]
        for row in range(geometry.rows):
            letter = chr(ord("A") + row)
            x, y = placement.xy_mm(f"{letter}1")
            lx, ly = self.pixel(x - pitch * cx_dir, y - pitch * cy_dir, frame)
            painter.drawText(QRectF(lx - 8, ly - 6, 16, 12), Qt.AlignCenter, letter)
        for column in range(geometry.columns):
            x, y = placement.xy_mm(f"A{column + 1}")
            lx, ly = self.pixel(x - pitch * rx_dir, y - pitch * ry_dir, frame)
            painter.drawText(QRectF(lx - 8, ly - 6, 16, 12), Qt.AlignCenter, str(column + 1))

    def _paintTip(self, painter, frame):
        if self._position is None:
            return
        x, y, _ = self._position
        color = TIP_RETRACTED if self._retracted else TIP_LOWERED
        # The ring only when the dot is over its well: the witness is read
        # live while the position is the last publish, so a paint that lands
        # between the two must not ring a well the dot has left.
        if (self._current_well is not None and self._current_well.reservoir in self._placements
                and self.wellAtXY(x, y) == self._current_well):
            placement = self._placements[self._current_well.reservoir]
            wx, wy = self.pixel(*placement.xy_mm(self._current_well.well), frame)
            ring = WELL_RADIUS_PITCH * placement.pitch_mm * frame.scale + 4
            painter.setPen(QPen(color, 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(wx, wy), ring, ring)
        px, py = self.pixel(x, y, frame)
        painter.setPen(QPen(QColor("#111"), 1))
        painter.setBrush(color)
        painter.drawEllipse(QPointF(px, py), TIP_RADIUS_PX, TIP_RADIUS_PX)

    def _paintLegend(self, painter):
        # A well's swatch is well-sized, the tip's is the dot's own size.
        entries = [(label, color, 4.5) for label, color in self._legendEntries()]
        entries += [("tip retracted", TIP_RETRACTED, TIP_RADIUS_PX), ("tip lowered", TIP_LOWERED, TIP_RADIUS_PX)]
        painter.setFont(QFont("Helvetica", 8))
        x = 6.0
        y = self.height() - TEXT_BAND_PX / 2
        for label, color, radius in entries:
            painter.setPen(QPen(QColor("#999") if radius > TIP_RADIUS_PX else QColor("#111"), 0.8))
            painter.setBrush(color)
            painter.drawEllipse(QPointF(x + 4, y), radius, radius)
            painter.setPen(QPen(QColor("#333")))
            painter.drawText(QPointF(x + 13, y + 4), label)
            x += 14 + painter.fontMetrics().horizontalAdvance(label) + 14
