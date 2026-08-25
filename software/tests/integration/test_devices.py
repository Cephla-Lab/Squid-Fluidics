# tests/integration/test_devices.py
"""The single bring-up both entry points share.

build_devices is the one place hardware gets constructed, started, and (via
DeviceSet.close) released; these tests pin what the two entry points used to
each spell out by hand -- which classes come up per application, how a
survivable failure degrades, and that teardown runs everything even when a
step fails.
"""

from types import SimpleNamespace

import pytest

import fluidics.devices as devices_module
from fluidics.control.config import TemperatureControllerConfig
from fluidics.control.controller import FluidControllerSimulation
from fluidics.control.disc_pump import DiscPump
from fluidics.control.flow_sensor import FlowSensorSimulation
from fluidics.control.selector_valve import SelectorValveSystem
from fluidics.control.syringe_pump import SyringePumpSimulation
from fluidics.control.temperature_controller import TCMControllerSimulation
from fluidics.devices import DeviceSet, build_devices, build_operations, build_worker
from fluidics.errors import AbortRequested, RunControl
from fluidics.merfish_operations import MERFISHOperations
from fluidics.open_chamber_operations import OpenChamberOperations


class RecordingIssues:
    def __init__(self):
        self.issues = []

    def __call__(self, kind, message):
        self.issues.append((kind, message))


class TestBuildDevicesSimulation:
    def test_flow_cell_builds_the_flow_cell_stack(self, flow_cell_config, built):
        devices = built(flow_cell_config, simulation=True)
        assert isinstance(devices.controller, FluidControllerSimulation)
        assert isinstance(devices.syringe_pump, SyringePumpSimulation)
        assert isinstance(devices.selector_valves, SelectorValveSystem)
        assert devices.disc_pump is None
        # The fixture config declares one sensor and no temperature controller.
        assert len(devices.flow_sensors) == 1
        assert isinstance(devices.flow_sensors[0], FlowSensorSimulation)
        assert devices.temperature_controller is None

    def test_open_chamber_gets_a_disc_pump_and_no_sensors(self, open_chamber_config, built):
        devices = built(open_chamber_config, simulation=True)
        assert isinstance(devices.disc_pump, DiscPump)
        assert devices.flow_sensors == []

    def test_a_configured_temperature_controller_is_built(self, flow_cell_config, built):
        flow_cell_config.temperature_controller = TemperatureControllerConfig(
            serial_number="TC1", channels=1)
        devices = built(flow_cell_config, simulation=True)
        assert isinstance(devices.temperature_controller, TCMControllerSimulation)
        assert devices.temperature_controller.channels == 1

    def test_no_issues_reported_on_a_clean_bringup(self, flow_cell_config, built):
        issues = RecordingIssues()
        built(flow_cell_config, simulation=True, on_issue=issues)
        assert issues.issues == []


class TestSurvivableFailures:
    """The GUI's degradation policy, now shared: a missing temperature
    controller or failed flow sensors are reported and left out, not fatal.
    """

    def test_a_failing_temperature_controller_degrades_on_hardware(
            self, flow_cell_config, monkeypatch, built):
        flow_cell_config.temperature_controller = TemperatureControllerConfig(
            serial_number="TC1")
        # Real mode, with the hardware classes stood in so only the TCM fails.
        monkeypatch.setattr(devices_module, "FluidController",
                            FluidControllerSimulation)
        monkeypatch.setattr(devices_module, "SyringePump", SyringePumpSimulation)

        def explode(**kwargs):
            raise IOError("no TCM on port")

        monkeypatch.setattr(devices_module, "TCMController", explode)

        issues = RecordingIssues()
        devices = built(flow_cell_config, simulation=False,
                        on_issue=issues)
        assert devices.temperature_controller is None
        assert [kind for kind, _ in issues.issues] == ["temperature_controller"]
        assert "no TCM on port" in issues.issues[0][1]

    def test_failing_flow_sensors_degrade_to_none_at_all(
            self, flow_cell_config, monkeypatch, built):
        def explode(controller, config, simulation):
            raise RuntimeError("sensor 1 failed to initialize")

        monkeypatch.setattr(devices_module, "start_flow_sensors", explode)
        issues = RecordingIssues()
        devices = built(flow_cell_config, simulation=True,
                        on_issue=issues)
        assert devices.flow_sensors == []
        assert [kind for kind, _ in issues.issues] == ["flow_sensors"]

    def test_an_unsurvivable_failure_closes_what_already_started(
            self, flow_cell_config, monkeypatch):
        """A stuck valve must not leave the sensors it outlived subscribed to
        a packet stream nobody owns -- nor the controller's reader thread
        holding the MCU port, nor the temperature controller's port open.

        The TCM's close raises here, proving each cleanup is shielded: the
        controller after it must still close, and the operator must still see
        the valve error, not the close error that happened while unwinding."""
        def explode(controller, config):
            raise RuntimeError("current position is 3; expected 1")

        monkeypatch.setattr(devices_module, "SelectorValveSystem", explode)

        def recording_subclass(base, fail_close=False):
            class Recording(base):
                last = None

                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    type(self).last = self
                    self.closes = 0

                def close(self):
                    self.closes += 1
                    if fail_close:
                        raise IOError("close failed")

            return Recording

        RecordingController = recording_subclass(FluidControllerSimulation)
        RecordingTCM = recording_subclass(TCMControllerSimulation,
                                          fail_close=True)

        monkeypatch.setattr(devices_module, "FluidControllerSimulation",
                            RecordingController)
        monkeypatch.setattr(devices_module, "TCMControllerSimulation",
                            RecordingTCM)
        flow_cell_config.temperature_controller = TemperatureControllerConfig(
            serial_number="TC1")

        started = []
        original = devices_module.start_flow_sensors

        def spying_start(controller, config, simulation):
            sensors = original(controller, config, simulation)
            started.extend(sensors)
            return sensors

        monkeypatch.setattr(devices_module, "start_flow_sensors", spying_start)
        with pytest.raises(RuntimeError, match="expected 1"):
            build_devices(flow_cell_config, simulation=True)
        assert len(started) == 1
        assert all(s.terminate_reading_thread for s in started)
        assert RecordingController.last.closes == 1
        assert RecordingTCM.last.closes == 1


class TestBuildWorker:
    def test_the_worker_waits_on_the_set_s_signal_and_makes_it_safe(self, flow_cell_config, built):
        """So neither entry point can forget half of the wiring: an abort that
        cannot reach the worker, or a TEC left on."""
        devices = built(flow_cell_config, simulation=True)
        on_error = lambda message: None
        worker = build_worker(devices, object(), [], callbacks={"on_error": on_error})
        assert worker.run_control is devices.run_control
        assert worker.callbacks["make_safe"] == devices.make_safe
        assert worker.callbacks["on_error"] is on_error

    def test_a_caller_s_make_safe_is_refused_not_silently_replaced(self, flow_cell_config, built):
        devices = built(flow_cell_config, simulation=True)
        with pytest.raises(ValueError, match="make_safe"):
            build_worker(devices, object(), [], callbacks={"make_safe": lambda: []})


class TestBuildOperations:
    def test_flow_cell_selects_merfish_operations(self, flow_cell_config, built):
        devices = built(flow_cell_config, simulation=True)
        notices = []
        channel = notices.append
        ops = build_operations(flow_cell_config, devices, on_warning=channel)
        assert isinstance(ops, MERFISHOperations)
        assert ops.flow_sensors == devices.flow_sensors
        assert ops.on_warning is channel

    def test_open_chamber_selects_open_chamber_operations(self, open_chamber_config, built):
        devices = built(open_chamber_config, simulation=True)
        ops = build_operations(open_chamber_config, devices)
        assert isinstance(ops, OpenChamberOperations)
        assert ops.dp is devices.disc_pump

    def test_an_unknown_application_raises(self, flow_cell_config, built):
        devices = built(flow_cell_config, simulation=True)
        flow_cell_config.application = "Petri Dish"
        with pytest.raises(ValueError, match="Petri Dish"):
            build_operations(flow_cell_config, devices)


class RecordingPump:
    """Appends to a shared event list so tests can assert cross-device order."""

    def __init__(self, events=None):
        self.events = events if events is not None else []

    def halt(self):
        self.events.append(("pump", "halt"))

    def close(self, to_waste=False):
        self.events.append(("pump", "close", to_waste))


class ClosableStub:
    def __init__(self, name="device", events=None, fail=False):
        self.name = name
        self.events = events if events is not None else []
        self.fail = fail
        self.closed = 0

    def close(self):
        self.closed += 1
        self.events.append((self.name, "close"))
        if self.fail:
            raise IOError("port already gone")


def device_set(config, pump=None, controller=None, tc=None, sensors=(),
               disc_pump=None, run_control=None):
    return DeviceSet(config, controller if controller is not None else ClosableStub(),
                     pump if pump is not None else RecordingPump(),
                     selector_valves=None, disc_pump=disc_pump,
                     temperature_controller=tc, flow_sensors=list(sensors),
                     run_control=run_control if run_control is not None else RunControl())


class TestDeviceSetAbort:
    """One signal, no device I/O on the calling thread: the GUI's Abort button
    and the CLI's Ctrl+C both cancel through here, and every device that
    waits stops itself when its wait wakes."""

    def test_abort_cancels_the_shared_signal_and_touches_no_device(self, flow_cell_config):
        # Bare objects: any call on a device would be an AttributeError.
        devices = device_set(flow_cell_config, pump=object(), tc=object(),
                             disc_pump=object())
        devices.abort()
        assert isinstance(devices.run_control.cause, AbortRequested)

    def test_reset_clears_it_for_the_next_run(self, flow_cell_config):
        devices = device_set(flow_cell_config)
        devices.abort()
        devices.reset()
        assert not devices.run_control.cancelled


class TestMakeSafe:
    """After a cancelled run has unwound: TEC output off on every channel (an
    abort ends the experiment), drain pump off. Called by the worker on its
    own thread; the syringe pump halted itself on the way out."""

    def test_halts_the_syringe_pump(self, flow_cell_config):
        """Already halted after a cancel; not after a failure mid-move."""
        pump = RecordingPump()
        device_set(flow_cell_config, pump=pump).make_safe()
        assert pump.events == [("pump", "halt")]

    def test_switches_every_tec_channel_off(self, flow_cell_config):
        tc = TCMControllerSimulation(channels=2)
        tc.set_output_enabled(1, True)
        tc.set_output_enabled(2, True)
        assert device_set(flow_cell_config, tc=tc).make_safe() == []
        assert tc.output_enabled == [False, False]

    def test_stops_the_drain_pump(self, open_chamber_config):
        stops = []
        device_set(open_chamber_config,
                   disc_pump=SimpleNamespace(stop=lambda: stops.append(True))).make_safe()
        assert stops == [True]

    def test_absent_devices_are_skipped(self, flow_cell_config):
        assert device_set(flow_cell_config).make_safe() == []

    def test_a_failing_channel_skips_nothing_and_is_returned(self, open_chamber_config):
        class StuckOutput:
            channels = 2

            def set_output_enabled(self, channel, on):
                raise IOError(f"channel {channel} not answering")

        stops = []
        errors = device_set(open_chamber_config, tc=StuckOutput(),
                            disc_pump=SimpleNamespace(stop=lambda: stops.append(True))).make_safe()
        assert [str(e) for e in errors] == ["channel 1 not answering",
                                            "channel 2 not answering"]
        assert stops == [True]


class TestDeviceSetClose:
    def test_flow_cell_parks_the_syringe_empty(self, flow_cell_config):
        pump = RecordingPump()
        device_set(flow_cell_config, pump=pump).close()
        assert pump.events == [("pump", "close", True)]

    def test_open_chamber_does_not(self, open_chamber_config):
        pump = RecordingPump()
        device_set(open_chamber_config, pump=pump).close()
        assert pump.events == [("pump", "close", False)]

    def test_an_explicit_choice_beats_the_application_default(self, flow_cell_config):
        pump = RecordingPump()
        device_set(flow_cell_config, pump=pump).close(empty_syringe=False)
        assert pump.events == [("pump", "close", False)]

    def test_the_teardown_order_is_tc_sensors_pump_controller(self, flow_cell_config):
        """The docstring calls the ends load-bearing: sensors detach from the
        packet stream before the controller stops the reader thread, and the
        controller goes last because that thread owns the MCU port. Pin the
        whole sequence so a reorder cannot pass silently."""
        events = []
        devices = device_set(
            flow_cell_config,
            pump=RecordingPump(events),
            controller=ClosableStub("controller", events),
            tc=ClosableStub("tc", events),
            sensors=[ClosableStub("sensor", events)],
            run_control=SimpleNamespace(
                reset=lambda: events.append(("run_control", "reset"))))
        devices.close()
        assert events == [
            ("tc", "close"),
            ("sensor", "close"),
            ("run_control", "reset"),
            ("pump", "close", True),
            ("controller", "close"),
        ]

    def test_every_step_runs_even_when_one_fails_and_the_error_is_returned(
            self, flow_cell_config):
        """Teardown is the one place "keep going" beats "unwind": whatever
        cannot be closed now never gets another chance. The failure still
        reaches the caller, so a run with a wedged teardown cannot exit 0."""
        tc = ClosableStub(fail=True)
        controller = ClosableStub()
        sensor = ClosableStub()
        devices = device_set(flow_cell_config, controller=controller, tc=tc,
                             sensors=[sensor])
        errors = devices.close()
        assert tc.closed == 1
        assert sensor.closed == 1
        assert controller.closed == 1
        assert len(errors) == 1
        assert isinstance(errors[0], IOError)

    def test_a_clean_close_returns_no_errors(self, flow_cell_config):
        assert device_set(flow_cell_config).close() == []

    def test_close_is_idempotent(self, flow_cell_config):
        pump = RecordingPump()
        devices = device_set(flow_cell_config, pump=pump)
        devices.close()
        assert devices.close() == []
        assert pump.events == [("pump", "close", True)]


class TestRunControlInjection:
    """One signal for the whole run: every device that waits is handed the
    same object, so an abort from either entry point reaches all of them."""

    def test_the_syringe_pump_shares_the_set_s_run_control(self, flow_cell_config, built):
        devices = built(flow_cell_config, simulation=True)
        assert devices.syringe_pump.run_control is devices.run_control

    def test_the_disc_pump_shares_it_too(self, open_chamber_config, built):
        devices = built(open_chamber_config, simulation=True)
        assert devices.disc_pump.run_control is devices.run_control

    def test_the_temperature_controller_shares_it_too(self, flow_cell_config, built):
        flow_cell_config.temperature_controller = TemperatureControllerConfig(
            serial_number="TC1", channels=1)
        devices = built(flow_cell_config, simulation=True)
        assert devices.temperature_controller.run_control is devices.run_control

    def test_close_after_an_abort_resets_it_and_reports_no_errors(self, flow_cell_config, built):
        """The real pump's close parks to waste with a move of its own; with
        the signal still tripped that move would raise instead of parking."""
        devices = built(flow_cell_config, simulation=True)
        devices.abort()
        assert devices.close() == []
        assert not devices.run_control.cancelled
