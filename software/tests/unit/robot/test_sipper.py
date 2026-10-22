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
    robot = RobotSipper(stage, {"probes": PROBES, "ssct": SSCT}, agitate_amplitude_mm=5.0)
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
    assert legs == ["Z3", "X1", "Z3"]            # up, across (Y is already there: no set-point), down


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


def test_retract_halt_and_home_clear_the_current_well(sipper):
    robot, _ = sipper
    robot.go_to_well(WellRef("probes", "A1"))
    robot.retract()
    assert robot.current_well is None
    robot.go_to_well(WellRef("probes", "A1"))
    robot.halt()
    assert robot.current_well is None
    robot.go_to_well(WellRef("probes", "A1"))
    robot.home()
    assert robot.current_well is None
    robot.go_to_well(WellRef("probes", "A1"))
    robot.jog("z", -1.0)
    assert robot.current_well is None and robot.position() == (12.0, 1.5, 49.0)
    robot.go_to_well(WellRef("probes", "A1"))
    robot.reset_faults()
    assert robot.current_well is None


def test_current_well_key_is_the_ledgers_spelling_of_the_witness(sipper):
    """What the ledger asks before a draw through the sipper's port: the
    well as a ledger key, or None with the tip in no well."""
    robot, _ = sipper
    robot.go_to_well(WellRef("probes", "A1"))
    assert robot.current_well_key() == "probes/A1"
    robot.retract()
    assert robot.current_well_key() is None


def test_with_lower_tip_off_go_to_well_stops_over_the_well_and_sets_no_witness(sipper):
    """"Do not lower tip": the move is retract and XY only, so taught
    positions can be checked over empty plates; no witness, so no draw."""
    robot, service = sipper
    robot.lower_tip = False
    before = len(service.log)
    robot.go_to_well(WellRef("ssct", "A2"))
    assert robot.position() == (102.5, 10.5, 0.0)
    assert robot.current_well is None
    assert "Z3" not in [axis for verb, axis in service.log[before:] if verb == "move_to"][1:]
    robot.lower_tip = True
    robot.go_to_well(WellRef("ssct", "A2"))
    assert robot.position() == (102.5, 10.5, 52.0) and robot.current_well == WellRef("ssct", "A2")


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
        RobotSipper(stage, {"ssct": landscape}, 5.0)
    too_deep = PlatePlacement((12.0, 1.5), 60.0, G96, column_axis="+y", row_axis="+x")
    with pytest.raises(StageError, match="Z3"):
        RobotSipper(stage, {"probes": too_deep}, 5.0)


def test_teaching_replaces_the_live_placement_and_clears_the_witness(sipper):
    robot, _ = sipper
    robot.go_to_well(WellRef("probes", "A1"))
    taught = PlatePlacement((13.0, 2.0), 51.0, G96, column_axis="+y", row_axis="+x")
    robot.set_placement("probes", taught)
    assert robot.current_well is None
    robot.go_to_well(WellRef("probes", "A1"))
    assert robot.position() == (13.0, 2.0, 51.0)


def test_a_quiescent_close_makes_no_move_and_leaves_the_tip_in_its_well_with_no_witness(sipper):
    """The tip waits wet in the last step's well between sessions (the
    lead screw holds Z unpowered; the next launch homes upward first), so
    the close does not retract; a closed robot vouches for no
    well."""
    robot, service = sipper
    robot.go_to_well(WellRef("probes", "A1"))
    before = len(service.log)
    assert robot.close(quiescent=True) == []
    assert "move_to" not in [verb for verb, _ in service.log[before:]]
    assert service.position_of("Z3") == pytest.approx(PROBES.dip_z_mm)
    assert robot.current_well is None
    assert not service.connected


def test_a_close_under_a_live_job_halts_where_it_is_and_leaves_no_witness(sipper):
    robot, service = sipper
    robot.go_to_well(WellRef("probes", "A1"))
    before = len(service.log)
    robot.close(quiescent=False)
    tail = service.log[before:]
    assert "move_to" not in [verb for verb, _ in tail]
    assert all(("stop", axis) in tail for axis in AXES.values())
    assert robot.current_well is None


def test_a_quiescent_close_on_an_unhomed_stage_makes_no_move_and_reports_nothing():
    control = RunControl()
    service = SimulatedService(AXES.values(), STROKES, instant=True)
    stage = XYZStage(service, AXES, 0.0, 50.0, 20.0, control)
    stage.connect()
    robot = RobotSipper(stage, {"probes": PROBES, "ssct": SSCT}, agitate_amplitude_mm=5.0)
    assert robot.close(quiescent=True) == []
    assert "move_to" not in [verb for verb, _ in service.log]


def test_a_plate_whose_agitation_stroke_reaches_travel_height_is_refused():
    """go_to_well vouches for a well at dip depth and agitate lifts the tip
    by the amplitude: a dip no more than an amplitude below travel height
    would agitate in air. Refused when the sipper is built and by
    set_placement, naming the plate; the old placement and the witness
    stay."""
    control = RunControl()
    service = SimulatedService(AXES.values(), STROKES, instant=True)
    stage = XYZStage(service, AXES, 10.0, 50.0, 20.0, control)
    at_travel = PlatePlacement((12.0, 1.5), 15.0, G96, column_axis="+y", row_axis="+x")   # 15 - 5 = 10
    with pytest.raises(StageError, match="plate 'probes'.*15.00 mm.*5 mm agitation.*10.00 mm"):
        RobotSipper(stage, {"probes": at_travel}, 5.0)
    stage.connect()
    stage.home()
    robot = RobotSipper(stage, {"probes": PROBES}, 5.0)
    robot.go_to_well(WellRef("probes", "A1"))
    for dip in (15.0, 12.0, 8.0):            # at travel height, inside it, above it
        shallow = PlatePlacement((12.0, 1.5), dip, G96, column_axis="+y", row_axis="+x")
        with pytest.raises(StageError, match="plate 'probes'.*agitation"):
            robot.set_placement("probes", shallow)
    assert robot.placements["probes"] is PROBES
    assert robot.current_well == WellRef("probes", "A1"), "a refused teach changes nothing"
    robot.set_placement("probes", PlatePlacement((12.0, 1.5), 15.5, G96,
                                                 column_axis="+y", row_axis="+x"))


def test_check_travel_refuses_a_height_a_plates_agitation_stroke_reaches(sipper):
    robot, service = sipper
    before = len(service.log)
    with pytest.raises(StageError, match="plate 'probes'.*50.00 mm.*5 mm agitation.*45.00 mm"):
        robot.check_travel(45.0)              # probes: 50 - 5 = 45
    robot.check_travel(44.5)
    assert len(service.log) == before, "a check makes no move"


def test_set_travel_refuses_before_anything_changes_and_clears_the_witness(sipper):
    robot, service = sipper
    robot.go_to_well(WellRef("probes", "A1"))
    with pytest.raises(StageError, match="plate 'probes'.*agitation"):
        robot.set_travel(45.0)                # probes: 50 - 5 = 45
    assert robot._stage.travel_z_mm == 0.0 and robot.current_well == WellRef("probes", "A1")
    robot.set_travel(2.0)
    assert robot._stage.travel_z_mm == 2.0 and robot.current_well is None
