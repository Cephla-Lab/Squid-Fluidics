# tests/integration/test_system.py
"""FluidicsSystem: the rig as one object, against the simulated devices."""

import logging
import threading

import pytest

from fluidics.merfish_operations import MERFISHOperations
from fluidics.open_chamber_operations import OpenChamberOperations
from fluidics.system import FluidicsSystem

from ..conftest import hears, wait_until
from .conftest import FLOW_CELL_STEP


class TestBuild:
    def test_one_rig_assembled_around_one_signal(self, system):
        assert isinstance(system.operations, MERFISHOperations)
        assert system.session.devices is system.devices
        assert system.session.control is system.devices.run_control
        assert system.operations.run_control is system.devices.run_control
        assert system.manual.sp is system.devices.syringe_pump

    def test_the_application_picks_the_operations(self, open_chamber_config):
        with FluidicsSystem.build(open_chamber_config, simulation=True) as system:
            assert isinstance(system.operations, OpenChamberOperations)

    def test_a_draw_protection_notice_reaches_the_channel_and_the_log(self, system, caplog):
        seen = []
        system.warnings.subscribe(seen.append)
        with caplog.at_level(logging.WARNING, logger="fluidics"):
            system.operations.on_warning("flow low on syringe_draw")
        assert seen == ["flow low on syringe_draw"]
        assert "flow low on syringe_draw" in caplog.text


class TestTheJob:
    def test_run_goes_through_the_session_with_the_systems_operations(self, system, real_clock):
        from fluidics.events import RunEnded
        reports = hears(system.session.events, RunEnded,
                        key=lambda event: event.outcome)
        system.run([FLOW_CELL_STEP])
        assert system.wait(5)
        assert wait_until(lambda: reports == ["finished"]), reports
        assert system.devices.syringe_pump.executed, "nothing moved"

    def test_run_manual_goes_through_the_session(self, system, real_clock):
        done = threading.Event()
        system.run_manual(lambda: system.manual.extract(2, 300, 500),
                          callbacks={"on_finished": done.set})
        assert done.wait(5) and system.wait(5)
        assert system.devices.syringe_pump.executed == [[("extract", 2, 300, 40)]]


class TestTheTimeZeroGate:
    """Every run passes the gate, whoever starts one. The GUI and the CLI
    check before they call; a script or an embedded application holding
    this object had nothing between it and the rig."""

    def test_a_port_the_rig_lacks_never_starts(self, system):
        beyond = dict(FLOW_CELL_STEP, fluidic_port=999)
        with pytest.raises(ValueError, match="out of range"):
            system.run([beyond])
        assert not system.busy, "the rig started on a sequence it cannot run"

    def test_a_wrong_application_type_never_starts(self, system):
        """The rig is a Flow Cell; an open-chamber row would degrade the
        estimate and fail inside the run instead."""
        with pytest.raises(ValueError, match="Flow Cell"):
            system.run([dict(FLOW_CELL_STEP, type="add_reagent")])
        assert not system.busy

    def test_a_plan_handed_in_alone_is_gated_too(self, system):
        """A resume hands back a plan and no sequences; the rows it
        carries are what will run, so they are what is checked."""
        from fluidics.events import PlanEntry
        beyond = dict(FLOW_CELL_STEP, fluidic_port=999)
        plan = (PlanEntry(0, beyond, 1, 1, "beyond", 1.0),)
        with pytest.raises(ValueError, match="out of range"):
            system.run(None, plan=plan)
        assert not system.busy

    def test_a_good_run_still_starts(self, system, real_clock):
        system.run([FLOW_CELL_STEP])
        assert system.wait(5)


class TestClose:
    def test_close_stops_a_live_job_before_the_devices_go(self, system, real_clock):
        system.devices.syringe_pump.ESTIMATE_SECONDS = 60
        reports = []
        system.run_manual(lambda: system.manual.extract(2, 300, 500),
                          callbacks={"on_stopped": lambda: reports.append("stopped")})
        assert wait_until(lambda: system.devices.syringe_pump.moving)
        busy_at_close = []
        close = system.devices.close
        system.devices.close = lambda *a, **k: busy_at_close.append(system.session.busy) or close(*a, **k)
        assert system.close(timeout=5) == []
        assert busy_at_close == [False], "the devices closed under a live job"
        assert reports == ["stopped"]

    def test_close_on_an_idle_system_just_closes(self, system):
        assert system.close() == []
        assert system.close() == [], "a second close is not clean"

    def test_a_job_that_will_not_stop_is_closed_under_with_a_word(self, system, real_clock, caplog):
        release = threading.Event()
        system.run_manual(release.wait)          # nothing gated: a cancel cannot wake it
        with caplog.at_level(logging.ERROR, logger="fluidics"):
            system.close(timeout=0.05)
        assert "did not stop" in caplog.text
        release.set()
        assert system.session.wait(5)

    def test_a_second_interrupt_inside_the_quiesce_still_releases_the_devices(
            self, system, real_clock):
        """The wait for the job is where a second Ctrl+C lands; the ports
        must be released all the same, with the interrupt going on after."""
        release = threading.Event()
        system.run_manual(release.wait)

        def interrupted(timeout=None):
            raise KeyboardInterrupt

        system.session.wait = interrupted
        closed = []
        close = system.devices.close
        system.devices.close = lambda *a, **k: closed.append(True) or close(*a, **k)
        with pytest.raises(KeyboardInterrupt):
            system.close()
        assert closed == [True], "the devices were not released"
        del system.session.wait     # the real wait is back for the teardown
        release.set()
        assert system.session.wait(5), "the job must end before teardown"

    def test_abort_and_busy_are_the_facades_too(self, system, real_clock):
        """A script's signal handler holds the system, not its session."""
        release = threading.Event()
        assert system.busy is False and system.abort() is False
        system.run_manual(release.wait)
        assert system.busy is True
        release.set()
        assert system.wait(5)

    def test_the_context_manager_closes(self, flow_cell_config):
        with FluidicsSystem.build(flow_cell_config, simulation=True) as system:
            pass
        assert system.close() == []              # already closed: nothing to report
