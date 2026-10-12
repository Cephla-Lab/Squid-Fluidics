"""RobotSipper: wells to coordinates, the current-well witness, reach."""
import pytest

from fluidics.errors import AbortRequested, RunControl, StageError
from fluidics.robot.labware import PlateGeometry, PlatePlacement, WellRef
from fluidics.robot.simulation import SimulatedService
from fluidics.robot.sipper import RobotSipper
from fluidics.robot.stage import XYZStage

AXES = {"x": "X1", "y": "Y2", "z": "Z3"}
STROKES = {"X1": (0.0, 200.0), "Y2": (0.0, 102.0), "Z3": (0.0, 58.5)}
G96 = PlateGeometry.from_well_count(96)
# The XYG321-A layout: two plates side by side along X, columns along Y.
PROBES = PlatePlacement((12.0, 1.5), 50.0, G96, column_axis="+y", row_axis="+x")
SSCT = PlatePlacement((102.5, 1.5), 52.0, G96, column_axis="+y", row_axis="+x")


def build(instant=True):
    control = RunControl()
    service = SimulatedService(AXES.values(), STROKES, instant=instant)
    stage = XYZStage(service, AXES, 0.0, 50.0, 20.0, control)
    stage.connect()
    stage.home()
    robot = RobotSipper(stage, {"probes": PROBES, "ssct": SSCT}, "probes",
                        agitate_amplitude_mm=5.0, park_well=WellRef("ssct", "H11"))
    return robot, service, control


@pytest.fixture
def sipper():
    robot, service, _ = build()
    return robot, service


def test_go_to_well_retracts_travels_then_dips(sipper):
    robot, service = sipper
    robot.go_to_well(WellRef("ssct", "A2"))
    assert robot.position() == (102.5, 10.5, 52.0)
    assert robot.current_well == WellRef("ssct", "A2")


def test_moving_between_wells_lifts_first(sipper):
    robot, service = sipper
    robot.go_to_well(WellRef("probes", "A1"))
    before = len(service.log)
    robot.go_to_well(WellRef("probes", "B1"))
    legs = [axis for verb, axis in service.log[before:] if verb == "move_to"]
    assert legs == ["Z3", "X1", "Y2", "Z3"]      # up, across, down


def test_agitate_goes_up_by_the_amplitude_and_back_and_keeps_the_witness(sipper):
    robot, service = sipper
    robot.go_to_well(WellRef("ssct", "A1"))
    heard = []
    robot.moves.subscribe(heard.append)
    robot.agitate(2)
    assert [z for _, _, z in heard] == [47.0, 52.0, 47.0, 52.0]
    assert robot.current_well == WellRef("ssct", "A1")


def test_an_interrupted_agitation_leaves_no_witness():
    """Stopped above dip depth, the residual below the tip is not the one
    the ledger counted on: the well is no longer current."""
    robot, service, control = build(instant=False)
    robot.go_to_well(WellRef("ssct", "A1"))

    def cancel_on_the_way_up():
        if service.position_of("Z3") < 50.0:
            control.cancel(AbortRequested("stop"))
    service.on_poll = cancel_on_the_way_up
    with pytest.raises(AbortRequested):
        robot.agitate(3)                          # each stroke 5 mm at 20 mm/s: five polls
    assert robot.current_well is None
    assert 47.0 <= robot.position()[2] < 52.0


def test_agitate_needs_a_current_well(sipper):
    robot, _ = sipper
    with pytest.raises(StageError, match="not in a well"):
        robot.agitate(1)


def test_retract_park_halt_and_home_clear_the_current_well(sipper):
    robot, _ = sipper
    robot.go_to_well(WellRef("probes", "A1"))
    robot.retract()
    assert robot.current_well is None
    robot.park()
    assert robot.current_well == WellRef("ssct", "H11"), "a park well counts as a well"
    robot.halt()
    assert robot.current_well is None
    robot.go_to_well(WellRef("probes", "A1"))
    robot.home()
    assert robot.current_well is None


def test_an_unknown_reservoir_is_refused_before_moving(sipper):
    robot, service = sipper
    before = len(service.log)
    with pytest.raises(ValueError, match="reservoir"):
        robot.go_to_well(WellRef("water", "A1"))
    assert len(service.log) == before


def test_a_plate_the_stage_cannot_reach_is_refused_when_built():
    control = RunControl()
    service = SimulatedService(AXES.values(), STROKES, instant=True)
    stage = XYZStage(service, AXES, 0.0, 50.0, 20.0, control)
    landscape = PlatePlacement((140.0, 8.0), 52.0, G96)      # H12 would be at x = 239
    with pytest.raises(StageError, match="plate 'ssct'.*A12.*X1"):
        RobotSipper(stage, {"ssct": landscape}, "ssct", 5.0)
    too_deep = PlatePlacement((12.0, 1.5), 60.0, G96, column_axis="+y", row_axis="+x")
    with pytest.raises(StageError, match="Z3"):
        RobotSipper(stage, {"probes": too_deep}, "probes", 5.0)


def test_teaching_replaces_the_live_placement_and_clears_the_witness(sipper):
    robot, _ = sipper
    robot.go_to_well(WellRef("probes", "A1"))
    taught = PlatePlacement((13.0, 2.0), 51.0, G96, column_axis="+y", row_axis="+x")
    robot.set_placement("probes", taught)
    assert robot.current_well is None
    robot.go_to_well(WellRef("probes", "A1"))
    assert robot.position() == (13.0, 2.0, 51.0)
