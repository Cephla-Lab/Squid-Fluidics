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


def test_a_second_launch_on_a_held_ledger_touches_no_device(plate_sipper_config, built, tmp_path, monkeypatch):
    """The ledger's lock comes before any device is touched: a second
    launch on a rig already in use stops there, before it clears the MCU,
    homes the robot (lifting the first session's tip out of its well) or
    homes the valves. What it had constructed is still released."""
    import fluidics.devices as devices_module
    from fluidics.control.controller import FluidControllerSimulation
    from fluidics.robot import simulation as robot_simulation
    controllers, services, valves = [], [], []

    class RecordingController(FluidControllerSimulation):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            controllers.append(self)
            self.begun = False
            self.closes = 0

        def begin(self):
            self.begun = True
            return super().begin()

        def close(self):
            self.closes += 1
            return super().close()

    class RecordingService(robot_simulation.SimulatedService):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            services.append(self)

    real_valves = devices_module.SelectorValveSystem

    def recording_valves(*args, **kwargs):
        valves.append(True)
        return real_valves(*args, **kwargs)

    monkeypatch.setattr(devices_module, "FluidControllerSimulation", RecordingController)
    monkeypatch.setattr(robot_simulation, "SimulatedService", RecordingService)
    monkeypatch.setattr(devices_module, "SelectorValveSystem", recording_valves)
    path = tmp_path / "ledger.json"
    first = built(plate_sipper_config, simulation=True, instant=True, ledger_path=path)
    assert len(controllers) == len(services) == len(valves) == 1
    with pytest.raises(ReservoirInUse):
        build_devices(plate_sipper_config, simulation=True, instant=True, ledger_path=path)
    second = controllers[1]
    assert not second.begun and second.sent == [], "the MCU was neither begun nor cleared"
    assert len(services) == 1, "no stage service was opened: nothing connected, homed or moved"
    assert len(valves) == 1, "the valves were not homed"
    assert second.closes == 1, "what the failed launch had constructed is released"
    assert first.close() == []
    built(plate_sipper_config, simulation=True, instant=True, ledger_path=path)   # the lock was the first set's


def test_a_failure_after_the_robot_is_up_closes_it_without_moves_and_frees_the_ledger(plate_sipper_config, tmp_path, monkeypatch):
    """A stuck valve after the robot has homed: the robot is closed
    non-quiescently with the rest, and the ledger's lock, taken first, is
    released, so the next launch can take it."""
    import fluidics.devices as devices_module
    from fluidics.reservoirs import ReservoirLedger, reservoir_definitions
    closed = []
    real = devices_module._build_robot

    def tracking(*args, **kwargs):
        robot = real(*args, **kwargs)
        robot_close = robot.close
        robot.close = lambda quiescent=True: closed.append(quiescent) or robot_close(quiescent)
        return robot

    def stuck(*args, **kwargs):
        raise RuntimeError("current position is 3; expected 1")

    monkeypatch.setattr(devices_module, "_build_robot", tracking)
    monkeypatch.setattr(devices_module, "SelectorValveSystem", stuck)
    path = tmp_path / "ledger.json"
    with pytest.raises(RuntimeError, match="expected 1") as failed:
        build_devices(plate_sipper_config, simulation=True, instant=True, ledger_path=path)
    assert closed == [False], "the robot was closed, without moves"
    # The failed launch's frames live on in `failed`, so the lock is free
    # here only if the launch released it, not the garbage collector.
    ReservoirLedger.load(path, reservoir_definitions(plate_sipper_config)).close()
    assert failed.value is not None


def test_a_robot_that_cannot_home_fails_the_launch_closed_without_moves(plate_sipper_config, monkeypatch):
    """_build_robot's own failure path: a Z fault during homing raises out
    of build_devices naming the axis; the builder closes its stage without
    a move (halt, brake, disable, disconnect), and the controller and the
    sensors started before it are released."""
    import fluidics.devices as devices_module
    from fluidics.control.controller import FluidControllerSimulation
    from fluidics.robot import simulation as robot_simulation
    services, controllers, started = [], [], []

    class FaultingService(robot_simulation.SimulatedService):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.fault("Z3", after_polls=1, error_code=0x2310)
            services.append(self)

    class RecordingController(FluidControllerSimulation):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            controllers.append(self)
            self.closes = 0

        def close(self):
            self.closes += 1
            return super().close()

    original = devices_module.start_flow_sensors

    def spying_start(controller, config, simulation):
        sensors = original(controller, config, simulation)
        started.extend(sensors)
        return sensors

    monkeypatch.setattr(robot_simulation, "SimulatedService", FaultingService)
    monkeypatch.setattr(devices_module, "FluidControllerSimulation", RecordingController)
    monkeypatch.setattr(devices_module, "start_flow_sensors", spying_start)
    with pytest.raises(DeviceError, match="axis Z3: drive fault during homing"):
        build_devices(plate_sipper_config, simulation=True, instant=True)
    (service,) = services
    assert "move_to" not in [verb for verb, _ in service.log], "nothing moved"
    assert {axis for verb, axis in service.log if verb == "stop"} == {"X1", "Y2", "Z3"}, "halted"
    assert service.log[-5:] == [("engage_brake", "Z3"), ("disable", "X1"), ("disable", "Y2"),
                                ("disable", "Z3"), ("disconnect", None)]
    assert not service.connected
    assert len(started) == 1 and all(s._stop_reading.is_set() for s in started)
    assert [c.closes for c in controllers] == [1]


def test_a_ctrl_c_while_z_homes_closes_the_launch_without_moves(plate_sipper_config, tmp_path, monkeypatch):
    """A KeyboardInterrupt while Z homes -- the bench's likeliest moment
    for one -- unwinds like any bring-up failure: the stage is closed
    without a move (halt, brake, disable, disconnect), everything started
    before it is released, the ledger's lock too, and the interrupt goes
    on."""
    import fluidics.devices as devices_module
    from fluidics.control.controller import FluidControllerSimulation
    from fluidics.reservoirs import ReservoirLedger, reservoir_definitions
    from fluidics.robot import simulation as robot_simulation
    services, controllers, started = [], [], []

    class InterruptedService(robot_simulation.SimulatedService):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.on_poll = self._interrupt_once
            services.append(self)

        def _interrupt_once(self):
            self.on_poll = None        # the halt's re-read must not be interrupted too
            raise KeyboardInterrupt

    class RecordingController(FluidControllerSimulation):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            controllers.append(self)
            self.closes = 0

        def close(self):
            self.closes += 1
            return super().close()

    original = devices_module.start_flow_sensors

    def spying_start(controller, config, simulation):
        sensors = original(controller, config, simulation)
        started.extend(sensors)
        return sensors

    monkeypatch.setattr(robot_simulation, "SimulatedService", InterruptedService)
    monkeypatch.setattr(devices_module, "FluidControllerSimulation", RecordingController)
    monkeypatch.setattr(devices_module, "start_flow_sensors", spying_start)
    path = tmp_path / "ledger.json"
    with pytest.raises(KeyboardInterrupt):
        build_devices(plate_sipper_config, simulation=True, instant=True, ledger_path=path)
    (service,) = services
    assert ("home", "Z3") in service.log and ("home", "X1") not in service.log, "Z was homing"
    assert "move_to" not in [verb for verb, _ in service.log], "nothing moved"
    assert service.log[-5:] == [("engage_brake", "Z3"), ("disable", "X1"), ("disable", "Y2"),
                                ("disable", "Z3"), ("disconnect", None)]
    assert not service.connected
    assert len(started) == 1 and all(s._stop_reading.is_set() for s in started)
    assert [c.closes for c in controllers] == [1]
    ReservoirLedger.load(path, reservoir_definitions(plate_sipper_config)).close()


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
