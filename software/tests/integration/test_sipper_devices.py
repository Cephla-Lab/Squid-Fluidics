"""Bring-up and teardown with a gantry (spec §4)."""
import pytest

from fluidics.devices import build_devices
from fluidics.errors import DeviceError, ReservoirInUse


def test_a_gantry_rig_builds_a_homed_parked_sipper_and_a_declared_ledger(plate_sipper_config, built, tmp_path):
    devices = built(plate_sipper_config, simulation=True, instant=True,
                    ledger_path=tmp_path / "ledger.json")
    assert devices.robot is not None and devices.robot.current_well is None
    assert devices.robot.position() == (0.0, 0.0, 0.0)           # every axis read after homing
    assert devices.reservoirs is not None and devices.reservoirs.undeclared == sorted(
        devices.reservoirs._defs), "no file yet: undeclared until declared"
    devices.reservoirs.refill("all")
    assert devices.reservoirs.remaining("ssct/A1") == 1750


def test_a_simulated_rig_without_a_path_starts_declared(plate_sipper_config, built):
    devices = built(plate_sipper_config, simulation=True, instant=True)
    assert devices.reservoirs.declared


def test_a_rig_without_a_gantry_or_volumes_has_neither(flow_cell_config, built):
    devices = built(flow_cell_config, simulation=True, instant=True)
    assert devices.robot is None and devices.reservoirs is None


def test_the_robot_comes_up_before_the_valves_and_is_not_survivable(plate_sipper_config, monkeypatch):
    import fluidics.devices as devices_module
    order = []

    def failing(*args, **kwargs):
        order.append("robot")
        raise DeviceError("no stage")

    monkeypatch.setattr(devices_module, "_build_robot", failing)
    monkeypatch.setattr(devices_module.SelectorValveSystem, "__init__",
                        lambda self, *a, **k: order.append("valves"))
    with pytest.raises(DeviceError, match="no stage"):
        build_devices(plate_sipper_config, simulation=True, instant=True)
    assert order == ["robot"], "the valves were not homed after a dead robot"


def test_a_ledger_that_cannot_be_held_fails_the_launch_and_releases_the_robot(plate_sipper_config, built, tmp_path, monkeypatch):
    """The ledger is built inside the protected block: its failure tears
    down the stage, the sensors and the controller like any other."""
    import fluidics.devices as devices_module
    closed = []
    real = devices_module._build_robot

    def tracking(*args, **kwargs):
        robot = real(*args, **kwargs)
        robot_close = robot.close
        robot.close = lambda quiescent=True: closed.append(quiescent) or robot_close(quiescent)
        return robot

    monkeypatch.setattr(devices_module, "_build_robot", tracking)
    first = built(plate_sipper_config, simulation=True, instant=True, ledger_path=tmp_path / "ledger.json")
    with pytest.raises(ReservoirInUse):
        build_devices(plate_sipper_config, simulation=True, instant=True, ledger_path=tmp_path / "ledger.json")
    assert closed == [False], "the second launch's robot was closed, without moves"
    assert first.reservoirs is not None


def test_close_runs_the_robot_after_the_reset_and_before_the_syringe(plate_sipper_config, built, tmp_path):
    devices = built(plate_sipper_config, simulation=True, instant=True,
                    ledger_path=tmp_path / "ledger.json")
    calls = []
    devices.run_control.reset = lambda: calls.append("reset")
    robot_close = devices.robot.close
    devices.robot.close = lambda quiescent=True: calls.append(("robot", quiescent)) or robot_close(quiescent)
    syringe_close = devices.syringe_pump.close
    devices.syringe_pump.close = lambda *a, **k: calls.append(("syringe", a)) or syringe_close(*a, **k)
    assert devices.close() == []
    assert calls == ["reset", ("robot", True), ("syringe", (True,))]


def test_a_close_under_a_live_job_resets_nothing_and_moves_nothing(plate_sipper_config, built, tmp_path):
    devices = built(plate_sipper_config, simulation=True, instant=True,
                    ledger_path=tmp_path / "ledger.json")
    calls = []
    devices.run_control.reset = lambda: calls.append("reset")
    log = devices.robot._stage._service.log
    before = len(log)
    syringe_close = devices.syringe_pump.close
    devices.syringe_pump.close = lambda *a, **k: calls.append(("syringe", a)) or syringe_close(*a, **k)
    assert devices.close(quiescent=False) == []
    assert "reset" not in calls
    assert ("syringe", (False,)) in calls
    assert "move_to" not in [verb for verb, _ in log[before:]]


def test_make_safe_halts_the_robot(plate_sipper_config, built, tmp_path):
    devices = built(plate_sipper_config, simulation=True, instant=True,
                    ledger_path=tmp_path / "ledger.json")
    halted = []
    devices.robot.halt = lambda: halted.append(True)
    assert devices.make_safe() == []
    assert halted == [True]
