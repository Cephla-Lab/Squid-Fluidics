"""DeckMapWidget: the two plates drawn where they are taught, the sipper's
tip painted where the stage says it is, colored by whether it is retracted
or lowered, and a click on a well that names the well (and nothing between
wells). Pure Qt over plain labware values; no robot, no session."""
import pytest
from qtpy.QtCore import QPoint, Qt
from qtpy.QtTest import QTest

from fluidics.robot.labware import G96, PlatePlacement, WellRef

EXTENTS = {"x": (0.0, 200.0), "y": (0.0, 102.0)}
PROBES = PlatePlacement((12.0, 1.5), 50.0, G96, column_axis="+y", row_axis="+x")
SSCT = PlatePlacement((102.5, 1.5), 52.0, G96, column_axis="+y", row_axis="+x")


@pytest.fixture
def deck(qapp):
    from fluidics.qt.deck_map import DeckMapWidget
    w = DeckMapWidget(EXTENTS)
    w.setPlacements({"probes": PROBES, "ssct": SSCT})
    w.setNamedWells({"priming": WellRef("ssct", "H11"), "clean-up": WellRef("ssct", "H12"),
                     "park": WellRef("ssct", "H11")})
    w.resize(640, 360)
    return w


def _at(w, placement, well):
    px, py = w.pixel(*placement.xy_mm(well))
    return QPoint(int(round(px)), int(round(py)))


def test_a_click_on_a_well_centre_names_it_and_between_wells_names_nothing(deck):
    assert deck.wellAt(_at(deck, PROBES, "C9")) == WellRef("probes", "C9")
    assert deck.wellAt(_at(deck, SSCT, "H12")) == WellRef("ssct", "H12")
    a1, b1 = PROBES.xy_mm("A1"), PROBES.xy_mm("B1")
    between = deck.pixel((a1[0] + b1[0]) / 2, (a1[1] + b1[1]) / 2)
    assert deck.wellAt(QPoint(int(between[0]), int(between[1]))) is None
    off_deck = deck.pixel(60.0, 60.0)              # the gap between the plates
    assert deck.wellAt(QPoint(int(off_deck[0]), int(off_deck[1]))) is None


def test_the_map_follows_the_plate_orientation(deck):
    """Columns along +y read upward on screen; rows along +x read to the right."""
    a1, a2, b1 = (_at(deck, PROBES, well) for well in ("A1", "A2", "B1"))
    assert a2.y() < a1.y() and a2.x() == a1.x()
    assert b1.x() > a1.x() and b1.y() == a1.y()


def test_the_state_line_reads_lowered_retracted_or_unknown(deck):
    deck.setPosition((30.0, 73.5, 50.0), retracted=False)
    deck.setCurrentWell(WellRef("probes", "C9"))
    assert deck.stateText() == "sipper: lowered in probes/C9   x 30.0  y 73.5  z 50.0 mm"
    deck.setPosition((30.0, 73.5, 1.0), retracted=True)
    deck.setCurrentWell(None)
    assert deck.stateText() == "sipper: retracted   x 30.0  y 73.5  z 1.0 mm"
    deck.setPosition(None, retracted=False)
    assert deck.stateText() == "sipper: position unknown (home or retract)"


def test_the_tip_is_painted_where_it_is_in_the_colour_of_its_state(deck):
    deck.setPosition((30.0, 73.5, 50.0), retracted=False)
    deck.setCurrentWell(WellRef("probes", "C9"))
    image = deck.grab().toImage()
    at = _at(deck, PROBES, "C9")
    lowered = image.pixelColor(at)
    assert lowered.red() > 150 and lowered.green() < 100, lowered.name()
    deck.setPosition((30.0, 73.5, 1.0), retracted=True)
    deck.setCurrentWell(None)
    retracted = deck.grab().toImage().pixelColor(at)
    assert retracted.green() > 100 and retracted.red() < 100, retracted.name()


def test_a_well_with_two_roles_is_listed_once_with_both(deck):
    assert deck.legend() == ["clean-up well", "priming + park well"]


def test_a_mouse_press_on_a_well_emits_it_and_elsewhere_emits_nothing(deck):
    heard = []
    deck.wellClicked.connect(lambda reservoir, well: heard.append((reservoir, well)))
    QTest.mouseClick(deck, Qt.LeftButton, pos=_at(deck, SSCT, "B3"))
    assert heard == [("ssct", "B3")]
    off_deck = deck.pixel(60.0, 60.0)
    QTest.mouseClick(deck, Qt.LeftButton, pos=QPoint(int(off_deck[0]), int(off_deck[1])))
    assert heard == [("ssct", "B3")]


def test_a_disabled_map_takes_no_clicks(deck):
    heard = []
    deck.wellClicked.connect(lambda reservoir, well: heard.append((reservoir, well)))
    deck.setEnabled(False)
    QTest.mouseClick(deck, Qt.LeftButton, pos=_at(deck, SSCT, "B3"))
    assert heard == []
