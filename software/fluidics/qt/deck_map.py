"""The deck map: both plates drawn where their placements put them, the
sipper's tip painted where the stage last published it and colored by
whether it is retracted or lowered, the priming, clean-up and park wells
marked, and a click on a well that names the well -- never a point between
wells, so a click can only ever send the tip to a well's centre.

Pure Qt over plain labware values: the Robot tab feeds it placements, a
position, the current well and the named wells, and hears `wellClicked`.
It knows nothing of the robot, the session or the bus; nothing here polls.
"""

from qtpy.QtCore import QPointF, QRectF, QSize, Qt, Signal
from qtpy.QtGui import QColor, QFont, QPainter, QPen
from qtpy.QtWidgets import QSizePolicy, QVBoxLayout, QWidget

from fluidics.robot.labware import AXIS_DIRECTIONS, WellRef

# The colors: the named wells, and the tip in each of its states.
ROLE_COLORS = {"priming": QColor("#4a90e2"), "clean-up": QColor("#9b9b9b"), "park": QColor("#3cb371")}
TIP_RETRACTED = QColor("#2e8b57")
TIP_LOWERED = QColor("#d0021b")
TIP_UNKNOWN = QColor("#e0a800")

WELL_RADIUS_PITCH = 0.36       # a 6.4 mm well on a 9 mm pitch
HIT_RADIUS_PITCH = 0.45        # a click this close to a centre is that well; farther is nothing
TEXT_BAND_PX = 20              # the state line above the plates, the legend below


class DeckMapWindow(QWidget):
    """The map's own window, so it takes no room on the tab: a tool window
    that floats beside the main window, opened from the Robot tab and
    closed by the operator (closing hides it; the map lives on, fed as
    before, and reopens where it was)."""

    def __init__(self, deck_map, parent=None):
        super().__init__(parent, Qt.Tool)
        self.setWindowTitle("Deck map")
        layout = QVBoxLayout()
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(deck_map)
        self.setLayout(layout)
        self.resize(760, 480)


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
        """{role: WellRef}: "priming", "clean-up", "park" (any may be absent)."""
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
        state = "retracted" if self._retracted else "lowered"
        where = f" in {self._current_well.reservoir}/{self._current_well.well}" if self._current_well else ""
        return f"sipper: {state}{where}   x {x:.1f}  y {y:.1f}  z {z:.1f} mm"

    def legend(self):
        """The roles present, a well with two roles listed once with both."""
        combos = sorted({" + ".join(roles) for roles in self._roles.values()})
        return [f"{combo} well" for combo in combos]

    def pixel(self, x_mm, y_mm):
        """Deck mm to widget pixels: X to the right, Y up (the deck seen
        from above)."""
        scale, left, bottom = self._frame()
        (x0, _), (y0, _) = self._box()
        return left + (x_mm - x0) * scale, bottom - (y_mm - y0) * scale

    def wellAt(self, point):
        """The well whose centre is within the hit radius of `point` (a
        QPoint in widget pixels), or None: between wells is nowhere."""
        scale, _, _ = self._frame()
        px, py = float(point.x()), float(point.y())
        for name, placement in self._placements.items():
            radius = HIT_RADIUS_PITCH * placement.pitch_mm * scale
            for well in placement.geometry.wells():
                wx, wy = self.pixel(*placement.xy_mm(well))
                if (wx - px) ** 2 + (wy - py) ** 2 <= radius ** 2:
                    return WellRef(name, well)
        return None

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
        """(scale px/mm, left px, bottom px) fitting the box between the
        text bands, centred horizontally."""
        (x0, x1), (y0, y1) = self._box()
        width = max(self.width(), 1)
        height = max(self.height() - 2 * TEXT_BAND_PX, 1)
        scale = min(width / (x1 - x0), height / (y1 - y0))
        left = (width - (x1 - x0) * scale) / 2
        bottom = TEXT_BAND_PX + height - (height - (y1 - y0) * scale) / 2
        return scale, left, bottom

    # --- painting ---

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), self.palette().base())
        scale, _, _ = self._frame()
        self._paintState(painter)
        for name, placement in self._placements.items():
            self._paintPlate(painter, name, placement, scale)
        self._paintTip(painter, scale)
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

    def _paintPlate(self, painter, name, placement, scale):
        geometry = placement.geometry
        pitch = placement.pitch_mm
        radius = WELL_RADIUS_PITCH * pitch * scale
        corners = [xy for _, xy in placement.corners()]
        xs, ys = [c[0] for c in corners], [c[1] for c in corners]
        pad = 0.75 * pitch
        left, top = self.pixel(min(xs) - pad, max(ys) + pad)
        right, bottom = self.pixel(max(xs) + pad, min(ys) - pad)
        painter.setPen(QPen(QColor("#444"), 1.2))
        painter.setBrush(QColor("#f7f7f7"))
        painter.drawRoundedRect(QRectF(left, top, right - left, bottom - top), 6, 6)
        painter.setFont(QFont("Helvetica", 9, QFont.Bold))
        painter.setPen(QPen(QColor("#222")))
        painter.drawText(QPointF(left, top - 4), f"{name}  (dip {placement.dip_z_mm:g} mm)")
        for well in geometry.wells():
            cx, cy = self.pixel(*placement.xy_mm(well))
            roles = self._roles.get(WellRef(name, well), ())
            painter.setPen(QPen(QColor("#999"), 0.8))
            painter.setBrush(ROLE_COLORS[roles[0]] if roles else QColor("#ffffff"))
            painter.drawEllipse(QPointF(cx, cy), radius, radius)
            if len(roles) > 1:
                # Two roles on one well: the first fills it, the second rings it.
                painter.setPen(QPen(ROLE_COLORS[roles[1]], 2))
                painter.setBrush(Qt.NoBrush)
                painter.drawEllipse(QPointF(cx, cy), radius + 2, radius + 2)
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
            lx, ly = self.pixel(x - pitch * cx_dir, y - pitch * cy_dir)
            painter.drawText(QRectF(lx - 8, ly - 6, 16, 12), Qt.AlignCenter, letter)
        for column in range(geometry.columns):
            x, y = placement.xy_mm(f"A{column + 1}")
            lx, ly = self.pixel(x - pitch * rx_dir, y - pitch * ry_dir)
            painter.drawText(QRectF(lx - 8, ly - 6, 16, 12), Qt.AlignCenter, str(column + 1))

    def _paintTip(self, painter, scale):
        if self._position is None:
            return
        x, y, _ = self._position
        color = TIP_RETRACTED if self._retracted else TIP_LOWERED
        if self._current_well is not None and self._current_well.reservoir in self._placements:
            placement = self._placements[self._current_well.reservoir]
            wx, wy = self.pixel(*placement.xy_mm(self._current_well.well))
            ring = WELL_RADIUS_PITCH * placement.pitch_mm * scale + 4
            painter.setPen(QPen(color, 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(wx, wy), ring, ring)
        px, py = self.pixel(x, y)
        painter.setPen(QPen(QColor("#111"), 1))
        painter.setBrush(color)
        painter.drawEllipse(QPointF(px, py), 5, 5)

    def _paintLegend(self, painter):
        entries = [(label, ROLE_COLORS[label.split(" well")[0].split(" + ")[0]]) for label in self.legend()]
        entries += [("tip retracted", TIP_RETRACTED), ("tip lowered", TIP_LOWERED)]
        painter.setFont(QFont("Helvetica", 8))
        x = 6.0
        y = self.height() - TEXT_BAND_PX / 2
        for label, color in entries:
            painter.setPen(QPen(QColor("#999"), 0.8))
            painter.setBrush(color)
            painter.drawEllipse(QPointF(x + 4, y), 4.5, 4.5)
            painter.setPen(QPen(QColor("#333")))
            painter.drawText(QPointF(x + 13, y + 4), label)
            x += 14 + painter.fontMetrics().horizontalAdvance(label) + 14
