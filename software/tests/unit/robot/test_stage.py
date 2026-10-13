"""XYZStage over the simulated service: the rules, the poll, the halt."""
import pytest

from fluidics.errors import AbortRequested, DeviceError, RunControl, StageError
from fluidics.robot.simulation import SimulatedService
from fluidics.robot.stage import XYZStage

AXES = {"x": "X1", "y": "Y2", "z": "Z3"}
STROKES = {"X1": (0.0, 200.0), "Y2": (0.0, 102.0), "Z3": (0.0, 58.5)}


def build(instant=True):
    control = RunControl()
    service = SimulatedService(AXES.values(), STROKES, instant=instant)
    stage = XYZStage(service, AXES, travel_z_mm=0.0, xy_velocity_mm_s=50.0,
                     z_velocity_mm_s=20.0, run_control=control)
    return stage, service, control


# The suite's fake clock (conftest._fast_clock) advances by each poll's
# sleep, so a non-instant simulated service moves 50 ms of travel per poll,
# deterministically, and a timeout expires after exactly its polls.


@pytest.fixture
def rig():
    stage, service, control = build()
    stage.connect()
    stage.home()
    return stage, service, control


class TestRules:
    def test_after_homing_every_axis_is_read_and_the_stage_is_retracted_at_the_top(self, rig):
        stage, service, _ = rig
        assert stage.position() == (0.0, 0.0, 0.0)
        assert stage.is_retracted
        assert "move_to" not in [verb for verb, _ in service.log], "inside a stroke from 0: no move"

    def test_homing_clears_each_axis_off_its_switch_into_its_stroke(self):
        """The driver's strokes start 0.5 mm clear of the switches and homing
        parks short of that (0.2 mm here, 0.1 to 0.5 on the drives): left
        there, a jog of one axis would be refused for the others' targets.
        Z first, never below travel height, then X and Y as one leg."""
        control = RunControl()
        strokes = {"X1": (0.5, 200.0), "Y2": (0.5, 109.0), "Z3": (0.5, 58.5)}
        service = SimulatedService(AXES.values(), strokes, instant=True)
        stage = XYZStage(service, AXES, 1.0, 50.0, 20.0, control)
        stage.connect()
        stage.home()
        assert stage.position() == (0.5, 0.5, 0.5) and stage.is_retracted
        after_homing = service.log[[v for v, _ in service.log].index("home") + 3:]
        assert [axis for verb, axis in after_homing if verb == "move_to"] == ["Z3", "X1", "Y2"]
        stage.move_xy(1.5, 0.5)                   # a single-axis jog's shape: the other axis stays put

    def test_before_homing_the_position_is_unknown_and_xy_is_refused(self):
        stage, service, _ = build()
        stage.connect()
        assert stage.position() is None
        with pytest.raises(StageError, match="not homed"):
            stage.move_xy(10.0, 10.0)
        assert ("move_to", "X1") not in service.log

    def test_before_homing_z_is_refused_too(self):
        """Unhomed, Z's frame is unreferenced: even a retract could drive
        the tip toward the deck."""
        stage, service, _ = build()
        stage.connect()
        with pytest.raises(StageError, match="not homed"):
            stage.move_z(0.0)
        with pytest.raises(StageError, match="not homed"):
            stage.retract()
        assert "move_to" not in [verb for verb, _ in service.log]

    def test_xy_is_refused_while_the_tip_is_down(self, rig):
        stage, service, _ = rig
        stage.move_z(40.0)
        with pytest.raises(StageError, match="travel height"):
            stage.move_xy(10.0, 10.0)
        assert service.log[-1] != ("move_to", "X1"), "nothing was sent"

    def test_retracted_at_judges_a_published_position_on_its_own(self, rig):
        """For a paint that lands after the publish, when the stage may be
        mid-leg with its position unknown: the published Z decides."""
        stage, _, _ = rig
        assert stage.retracted_at((5.0, 5.0, stage.travel_z_mm))
        assert stage.retracted_at((5.0, 5.0, stage.travel_z_mm + 0.04))
        assert not stage.retracted_at((5.0, 5.0, stage.travel_z_mm + 1.0))
        assert not stage.retracted_at(None)

    def test_retracted_means_at_or_above_travel_height(self, rig):
        stage, _, _ = rig
        stage.move_z(0.0)
        assert stage.is_retracted
        stage.move_z(0.1)
        assert not stage.is_retracted

    def test_a_target_outside_the_stroke_is_refused_before_sending(self, rig):
        stage, service, _ = rig
        sent = len(service.log)
        with pytest.raises(StageError, match="stroke"):
            stage.move_z(70.0)
        assert len(service.log) == sent

    def test_check_reach_refuses_without_moving(self, rig):
        stage, service, _ = rig
        sent = len(service.log)
        stage.check_reach(x=200.0, y=102.0, z=58.5)
        with pytest.raises(StageError, match="Y2"):
            stage.check_reach(y=102.1)
        assert len(service.log) == sent

    def test_home_runs_z_first_then_x_and_y(self, rig):
        _, service, _ = rig
        homed = [axis for verb, axis in service.log if verb == "home"]
        assert homed == ["Z3", "X1", "Y2"]

    def test_xy_is_one_leg_dispatched_together(self, rig):
        stage, service, _ = rig
        before = len(service.log)
        stage.move_xy(30.0, 40.0)
        verbs = [entry for entry in service.log[before:] if entry[0] == "move_to"]
        assert [axis for _, axis in verbs] == ["X1", "Y2"]
        assert stage.position() == (30.0, 40.0, 0.0)


class TestHoming:
    def test_a_stale_attained_bit_at_the_start_does_not_count(self):
        """The drive's bit 12 lingers from the last homing: the simulated
        service reads it stale on the first poll. A stage that took that
        for completion would report homed with the axis where it was."""
        stage, service, _ = build()
        stage.connect()
        service._position["Z3"] = 30.0           # where a restart found the tip
        stage.home()
        assert stage.position() == (0.0, 0.0, 0.0)
        assert service._polls["Z3"] > 2, "the stage waited for the bit to clear and set again"

    def test_a_fault_during_homing_halts_and_names_the_axis(self):
        stage, service, _ = build()
        stage.connect()
        service.fault("Z3", after_polls=2, error_code=0x7306)
        with pytest.raises(DeviceError, match="Z3.*fault"):
            stage.home()
        assert ("stop", "Z3") in service.log
        assert not stage.is_retracted, "a failed homing leaves the stage unhomed"
        with pytest.raises(StageError, match="not homed"):
            stage.move_xy(1.0, 1.0)

    def test_an_axis_that_never_homes_times_out(self, monkeypatch):
        stage, service, _ = build()
        stage.connect()
        service.freeze("Z3")
        monkeypatch.setattr(service, "homing_timeout_s", lambda axis: 0.0)
        with pytest.raises(DeviceError, match="Z3.*did not complete"):
            stage.home()
        assert ("stop", "Z3") in service.log

    def test_z_that_homes_to_the_deck_end_is_refused(self):
        stage, service, _ = build()
        stage.connect()
        service._strokes["Z3"] = (58.5, 0.0)      # a homing that ends at the deck end
        with pytest.raises(DeviceError, match="homing method"):
            stage.home()

    def test_a_travel_height_above_where_homing_parks_is_refused_with_the_reason(self):
        """The drives park 0.1 to 0.5 mm past 0 after homing (nm-servo PR
        #12), so a travel height of 0 can never be reached: the refusal
        must say so, not only suspect the homing method."""
        stage, service, _ = build()
        stage.connect()
        service._strokes["Z3"] = (0.6, 58.5)      # the simulation parks 0.3 short of the stroke's start: at 0.3
        with pytest.raises(DeviceError, match="travel height 0.00.*homing parks"):
            stage.home()


class TestPolling:
    def test_a_stale_target_reached_bit_does_not_end_a_leg_early(self):
        """The bit can still read reached from the previous move: the leg
        is done only when the position agrees. On the real clock the axis
        is seen in flight, position short of the target, bit reading set."""
        stage, service, _ = build(instant=False)
        stage.connect()
        stage.home()
        service.stale_target_reached = True
        seen = []
        service.on_poll = lambda: seen.append(service.position_of("Z3"))
        stage.move_z(2.0)                         # 2 mm at 20 mm/s: two polls
        assert stage.position()[2] == 2.0
        assert service.position_of("Z3") == pytest.approx(2.0)
        assert len(seen) >= 2
        assert any(0.0 < z < 2.0 for z in seen), "a poll saw the axis in flight"

    def test_a_leg_that_never_arrives_times_out_and_halts(self, rig):
        stage, service, _ = rig
        service.freeze("Z3")
        with pytest.raises(DeviceError, match="Z3"):
            stage.move_z(20.0)
        assert ("stop", "Z3") in service.log

    def test_a_fault_during_a_leg_halts_and_names_the_axis_until_reset(self, rig):
        stage, service, _ = rig
        service.fault("Z3", after_polls=2, error_code=0x7331)
        with pytest.raises(DeviceError, match="Z3.*7331"):
            stage.move_z(20.0)
        assert ("stop", "Z3") in service.log
        with pytest.raises(DeviceError, match="Z3.*7331"):
            stage.move_z(20.0)                     # still faulted: refused before dispatch
        stage.reset_faults()
        assert ("reset_fault", "Z3") in service.log
        reset = service.log.index(("reset_fault", "Z3"))
        assert ("enable", "Z3") in service.log[reset:], "the axis was enabled again"
        stage.move_z(20.0)
        assert stage.position()[2] == 20.0

    def test_an_axis_that_arrived_stays_watched_until_the_leg_ends(self):
        """Codex finding 2: X and Y are one leg, and X arrives first. A fault
        on X while Y still travels must end the leg, not let it succeed: the
        next Z leg reads only Z and would dip a faulted stage. With modelled
        motion (instant=False) under the suite's fake clock, X's 5 mm takes
        2 or 3 polls and Y's 80 mm about 32, so X's 10th read from here (the
        pre-check is the first) lands in between."""
        stage, service, _ = build(instant=False)
        stage.connect()
        stage.home()
        service.fault("X1", after_polls=10, error_code=0x7331)
        with pytest.raises(DeviceError, match="X1.*7331"):
            stage.move_xy(5.0, 80.0)
        assert ("stop", "X1") in service.log
        assert ("stop", "Y2") in service.log
        position = stage.position()
        assert position is not None, "the axes were re-read after the halt"
        assert position[0] == 5.0, "X had arrived when it faulted"
        assert 0.0 < position[1] < 80.0, "Y was halted in flight"
        sent = len(service.log)
        with pytest.raises(DeviceError, match="X1.*7331"):
            stage.move_xy(5.0, 80.0)               # still faulted: refused before dispatch
        assert "move_to" not in [verb for verb, _ in service.log[sent:]]
        stage.reset_faults()
        stage.move_xy(5.0, 80.0)
        assert stage.position() == (5.0, 80.0, 0.0)

    def test_a_cancel_during_a_leg_halts_every_started_axis(self):
        """Review focus 5: the poll raises the cancel on the job thread, the
        stage halts what it started, nothing is left to resume. On the real
        clock, so the leg spans polls (an instant leg ends on its first)."""
        stage, service, control = build(instant=False)
        stage.connect()
        stage.home()

        def cancel_once_moving():
            if ("move_to", "X1") in service.log:
                control.cancel(AbortRequested("stop"))
        service.on_poll = cancel_once_moving
        with pytest.raises(AbortRequested):
            stage.move_xy(30.0, 40.0)
        stopped = [axis for verb, axis in service.log if verb == "stop"]
        assert sorted(stopped) == ["X1", "Y2"]

    def test_a_cancel_latched_before_the_dispatch_sends_nothing(self, rig):
        """A cancel that lands while the pre-check reads the axes is caught
        before the dispatch: nothing moves, nothing needs stopping, and the
        position stays known."""
        stage, service, control = rig
        service.on_poll = lambda: control.cancel(AbortRequested("stop"))
        before = len(service.log)
        with pytest.raises(AbortRequested):
            stage.move_xy(30.0, 40.0)
        assert service.log[before:] == []
        assert stage.position() == (0.0, 0.0, 0.0)

    def test_an_early_end_re_reads_where_the_axes_stopped(self):
        """The witness after a halt is the drive's reading, not the leg's
        target or its start: an aborted dive down to 40 mm, stopped
        half-way, leaves Z at 20 and XY refused."""
        stage, service, control = build(instant=False)
        stage.connect()
        stage.home()

        def cancel_half_way():
            if service.position_of("Z3") >= 20.0:
                control.cancel(AbortRequested("stop"))
        service.on_poll = cancel_half_way
        with pytest.raises(AbortRequested):
            stage.move_z(40.0)                     # 40 mm at 20 mm/s: 40 polls
        z = stage.position()[2]
        assert 19.0 < z < 22.0, z
        assert not stage.is_retracted
        with pytest.raises(StageError, match="travel height"):
            stage.move_xy(1.0, 1.0)

    def test_a_port_error_from_a_read_halts_what_started(self, rig):
        """A driver or OSError is not a fluidics error, and must still halt
        every axis that may have started before it goes on."""
        stage, service, _ = rig
        service.fail_read("Y2", OSError("no response"), after_polls=2)   # the poll after the dispatch
        with pytest.raises(OSError):
            stage.move_xy(30.0, 40.0)
        stopped = [axis for verb, axis in service.log if verb == "stop"]
        assert sorted(stopped) == ["X1", "Y2"]

    def test_a_stop_that_fails_is_reported_after_every_axis_was_tried(self):
        stage, service, control = build(instant=False)
        stage.connect()
        stage.home()

        def cancel_once_moving():
            if ("move_to", "X1") in service.log:
                control.cancel(AbortRequested("stop"))
        service.on_poll = cancel_once_moving
        service.fail_stop("X1")
        with pytest.raises(DeviceError, match="could not stop axis X1") as info:
            stage.move_xy(30.0, 40.0)
        assert isinstance(info.value.__cause__, AbortRequested)
        assert ("stop", "Y2") in service.log, "the other axis was still stopped"

    def test_a_limit_stop_ends_the_leg_at_once_naming_the_position(self):
        """nm-servo PR #10, measured on the drives: a move that a limit
        switch ends leaves the drive enabled with alarm 8614h, and target
        reached never comes. The stage must not wait out the leg's timeout:
        it halts, re-reads, and says where the axis stopped."""
        stage, service, _ = build(instant=False)
        stage.connect()
        stage.home()
        service.limit_switch("Y2", at_mm=30.0)     # a switch inside the configured stroke
        polls = []
        service.on_poll = lambda: polls.append(service.position_of("Y2"))
        with pytest.raises(DeviceError, match="Y2.*60.00 mm.*limit switch.*30.00 mm"):
            stage.move_xy(10.0, 60.0)                 # Y: 60 mm at 50 mm/s; the timeout would be 3.4 s
        assert len(polls) < 40, "raised when the alarm showed, not at the timeout (68 polls)"
        assert ("stop", "Y2") in service.log
        assert stage.position() == (10.0, 30.0, 0.0)

    def test_a_move_that_ends_short_of_its_target_is_an_error_not_a_timeout(self):
        """X1's far switch flickers (nm-servo PR #10): the drive once
        cleared the limit and reported the target reached 2 mm short.
        Target reached with the position off, once the axis has been seen
        moving, ends the leg as an error; before that it is the stale bit."""
        stage, service, _ = build(instant=False)
        stage.connect()
        stage.home()
        service.end_short("X1", at_mm=148.0)
        with pytest.raises(DeviceError, match="X1.*150.00 mm.*ended at 148.00 mm"):
            stage.move_xy(150.0, 0.0)
        assert ("stop", "X1") in service.log
        assert stage.position() == (148.0, 0.0, 0.0)

    def test_position_is_published_after_each_leg_and_after_a_halt(self, rig):
        stage, _, _ = rig
        heard = []
        stage.moves.subscribe(heard.append)
        stage.move_z(10.0)
        stage.retract()
        stage.halt()
        assert heard == [(0.0, 0.0, 10.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)]

    def test_travel_time_is_counted_even_when_instant_with_xy_concurrent(self, rig):
        stage, service, _ = rig
        stage.move_z(20.0)            # 20 mm at 20 mm/s: 1 s
        stage.retract()               # 1 s
        stage.move_xy(50.0, 100.0)    # X 1 s, Y 2 s, together: 2 s
        assert service.travel_seconds == pytest.approx(4.0)


class TestClose:
    def test_a_quiescent_close_retracts_brakes_disables_and_disconnects(self, rig):
        stage, service, _ = rig
        stage.move_z(30.0)
        assert stage.close(quiescent=True) == []
        verbs = [entry[0] for entry in service.log]
        assert verbs[-6:] == ["move_to", "engage_brake", "disable", "disable", "disable", "disconnect"]

    def test_a_quiescent_close_told_not_to_retract_makes_no_move_and_still_shuts_down(self, rig):
        """What the sipper's close asks for: the tip stays put in its well."""
        stage, service, _ = rig
        stage.move_z(30.0)
        before = len(service.log)
        assert stage.close(quiescent=True, retract=False) == []
        verbs = [entry[0] for entry in service.log[before:]]
        assert verbs == ["engage_brake", "disable", "disable", "disable", "disconnect"]

    def test_a_close_under_a_live_job_makes_no_move(self, rig):
        stage, service, _ = rig
        stage.move_z(30.0)
        before = len(service.log)
        stage.close(quiescent=False)
        assert "move_to" not in [entry[0] for entry in service.log[before:]]
        assert ("stop", "Z3") in service.log[before:]

    def test_a_failed_step_is_returned_and_the_rest_still_run(self, rig):
        stage, service, _ = rig
        service.fail_stop("Y2")
        errors = stage.close(quiescent=False)
        assert len(errors) == 1 and "Y2" in str(errors[0])
        assert service.log[-1] == ("disconnect", None)


class TestMessages:
    def test_an_enum_axis_is_named_by_its_value(self):
        """The driver's AxisName is a plain Enum, whose str() is
        'AxisName.X1': every message names the axis as the drive label
        says it, 'axis X1'."""
        from enum import Enum

        class AxisName(Enum):
            X1 = "X1"
            Y2 = "Y2"
            Z3 = "Z3"

        axes = {"x": AxisName.X1, "y": AxisName.Y2, "z": AxisName.Z3}
        service = SimulatedService(axes.values(), {AxisName(name): stroke
                                                   for name, stroke in STROKES.items()},
                                   instant=True)
        stage = XYZStage(service, axes, 0.0, 50.0, 20.0, RunControl())
        stage.connect()
        stage.home()
        with pytest.raises(StageError) as refused:
            stage.move_xy(250.0, 10.0)
        assert str(refused.value) == "axis X1: 250.00 mm is outside the stroke 0.00..200.00 mm"
        service.fault(AxisName.Z3, after_polls=2, error_code=0x7331)    # in flight
        service.fail_stop(AxisName.Z3, OSError("no response"))
        with pytest.raises(DeviceError) as failed:
            stage.move_z(20.0)
        assert str(failed.value) == "could not stop axis Z3: no response"
        assert str(failed.value.__cause__) == "axis Z3: drive fault, error code 0x7331"
        assert "AxisName" not in str(refused.value) + str(failed.value) + str(failed.value.__cause__)
