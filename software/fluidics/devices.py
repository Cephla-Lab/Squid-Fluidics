"""One place to build, start, and tear down the hardware stack.

Both entry points (gui.py and run_sequences.py) used to spell out the same
bring-up by hand -- sim/real twice each -- and had already drifted: the GUI
degraded gracefully when the temperature controller or flow sensors failed to
come up, the CLI crashed; the two teardowns closed devices in different
orders. This module is the single copy, with the GUI's behaviour as the
reference.

`on_issue(kind, message)` is how a degraded bring-up is reported without this
module knowing about Qt: the GUI shows a QMessageBox naming the tab that will
be missing, the CLI takes the default and prints. It is called only for
failures that are survivable by design (no temperature controller, no flow
sensors); anything else raises.
"""

import logging
from functools import partial

from .control._def import CMD_SET
from .control.controller import FluidController, FluidControllerSimulation
from .control.disc_pump import DiscPump
from .control.flow_sensor import start_flow_sensors
from .control.selector_valve import SelectorValveSystem
from .control.syringe_pump import SyringePump, SyringePumpSimulation
from .control.temperature_controller import TCMController, TCMControllerSimulation
from .errors import RunControl
from .experiment_worker import ExperimentWorker
from .merfish_operations import MERFISHOperations
from .open_chamber_operations import OpenChamberOperations


# The vocabulary of survivable bring-up failures, passed to on_issue as its
# `kind`. Constants rather than bare literals so the GUI's per-kind dialog
# hints key on the same names the emit sites use.
ISSUE_TEMPERATURE_CONTROLLER = "temperature_controller"
ISSUE_FLOW_SENSORS = "flow_sensors"


_logger = logging.getLogger(__name__)


def _print_issue(kind, message):
    # WARNING: reaches the console (stderr) and the run log -- a degraded
    # bring-up is exactly the notice that must survive an unattended run.
    _logger.warning(message)


def _run_shielded(steps, doing="closing devices"):
    """Run every step, shielding each from the others' failures.

    Teardown's loop: a step that cannot run now never gets another chance, so
    one failure must skip nothing but itself -- and, on the bring-up failure
    path, must not replace the original error being raised. Failures go to
    stderr as they happen; the list of exceptions is returned for callers with
    an exit code to honour.
    """
    errors = []
    for step in steps:
        try:
            step()
        except Exception as e:
            errors.append(e)
            _logger.error("Error while %s: %s", doing, e)
    return errors


class DeviceSet:
    """The devices one experiment session runs on, and their teardown.

    Attributes are what the entry points already call them: controller,
    syringe_pump, selector_valves, disc_pump (None outside Open Chamber),
    temperature_controller (None when absent or failed), flow_sensors (possibly
    empty), and run_control -- the one cancellation signal every waiting
    device shares. Built by build_devices(); nothing else should construct one.
    """

    def __init__(self, config, controller, syringe_pump, selector_valves,
                 disc_pump, temperature_controller, flow_sensors, run_control):
        self.config = config
        self.run_control = run_control
        self.controller = controller
        self.syringe_pump = syringe_pump
        self.selector_valves = selector_valves
        self.disc_pump = disc_pump
        self.temperature_controller = temperature_controller
        self.flow_sensors = flow_sensors
        self._closed = False

    def abort(self):
        """Cancel the run: one signal, no device I/O on this thread.

        The GUI's Abort button and the CLI's Ctrl+C call this from threads
        that own no serial port. Every device that waits shares run_control
        and stops itself when its wait wakes; the worker waits on it too.
        """
        self.run_control.cancel()

    def reset(self):
        """Clear the cancellation once the run that raised it has ended, before
        anything else uses the devices (the GUI's manual tab)."""
        self.run_control.reset()

    def make_safe(self):
        """Leave nothing running once a run has ended early -- abort or failure
        alike; the failure is the unattended case. Halts the syringe pump
        (already halted after a cancel, not after a failure mid-move), TEC
        output off on every channel, drain pump off. Shielded per step;
        returns the exceptions raised, already logged, for the worker to
        report."""
        steps = [self.syringe_pump.stop]
        tc = self.temperature_controller
        if tc is not None:
            steps.extend(partial(tc.set_output_enabled, c, False)
                         for c in range(1, tc.channels + 1))
        if self.disc_pump is not None:
            steps.append(self.disc_pump.stop)
        return _run_shielded(steps, doing="making the rig safe")

    def close(self, empty_syringe=None):
        """Release every device, tolerating failures along the way.

        Each step runs even if an earlier one fails: teardown is the one place
        where "keep going" beats "unwind", because whatever cannot be closed
        now will never get another chance. Order matters at the ends --
        sensors detach from the controller's packet stream before the
        controller stops its reader thread, and the controller goes last
        because that thread owns the MCU port.

        Returns the list of exceptions the steps raised (empty when everything
        closed, and on every call after the first), so a caller with an exit
        code can refuse to report a run whose teardown failed as clean. Each
        failure is also printed to stderr as it happens.

        empty_syringe: dispense the syringe to waste before closing, the
        parking behaviour the GUI has always had on exit. Default (None)
        derives it from the application: Flow Cell empties, Open Chamber does
        not. The abort latch is cleared first so an aborted run still parks.
        """
        if self._closed:
            return []
        self._closed = True

        if empty_syringe is None:
            empty_syringe = self.config.application == "Flow Cell"

        # One call per step, so a failure skips nothing but itself: one dead
        # sensor must not strand its siblings. The cancellation is cleared
        # before the park-to-waste close, which is a move of its own.
        steps = []
        if self.temperature_controller is not None:
            steps.append(self.temperature_controller.close)
        steps.extend(sensor.close for sensor in self.flow_sensors)
        steps.append(self.run_control.reset)
        steps.append(lambda: self.syringe_pump.close(empty_syringe))
        steps.append(self.controller.close)

        return _run_shielded(steps)


def build_devices(config, simulation=False, on_issue=_print_issue):
    """Construct and start the full stack for `config`. Returns a DeviceSet.

    Mirrors what the two entry points did, once: construct controller, syringe
    pump, and (optionally) temperature controller; begin() the controller and
    CLEAR the MCU; start the flow sensors; build the selector valves (which
    homes them, today a constructor side effect); attach the disc pump for
    Open Chamber.

    Degradation policy is the GUI's: a temperature controller that fails to
    come up on hardware, or flow sensors that fail in either mode, are
    reported through on_issue and left out rather than failing the launch.
    The controller, pump, and valves are not survivable -- those raise, after
    closing whatever had already started.
    """
    # Pick the classes once so the config-to-constructor mapping is written
    # once -- the sim/real copy drift this module exists to end.
    controller_cls = FluidControllerSimulation if simulation else FluidController
    pump_cls = SyringePumpSimulation if simulation else SyringePump
    tc_cls = TCMControllerSimulation if simulation else TCMController

    # One cancellation signal for the whole run -- see RunControl.
    run_control = RunControl()
    controller = controller_cls(config.microcontroller.serial_number)
    syringe_pump = pump_cls(
        sn=config.syringe_pump.serial_number,
        syringe_ul=config.syringe_pump.volume_ul,
        speed_code_limit=config.syringe_pump.speed_code_limit,
        waste_port=config.syringe_pump.waste_port,
        run_control=run_control)

    temperature_controller = None
    if config.temperature_controller is not None:
        tc_cfg = config.temperature_controller
        try:
            temperature_controller = tc_cls(
                sn=tc_cfg.serial_number,
                channels=tc_cfg.channels,
                tolerance_celsius=tc_cfg.tolerance_celsius,
                stabilization_timeout_seconds=tc_cfg.stabilization_timeout_seconds,
                run_control=run_control,
            )
        except Exception as e:
            # Survivable only on hardware, where the usual cause is a flaky
            # port or serial-number mismatch. In simulation there is no port
            # to be flaky: a failure is a real bug and raises.
            if simulation:
                raise
            on_issue(ISSUE_TEMPERATURE_CONTROLLER,
                     f"Failed to initialize temperature controller: {e}")

    flow_sensors = []
    try:
        controller.begin()
        controller.send_command(CMD_SET.CLEAR)

        try:
            flow_sensors = start_flow_sensors(controller, config, simulation)
        except Exception as e:
            # start_flow_sensors closes whatever it had already brought up, so
            # nothing is left subscribed to the packet stream. The message
            # names the sensor and its index.
            flow_sensors = []
            on_issue(ISSUE_FLOW_SENSORS, f"Failed to initialize flow sensors: {e}")

        selector_valves = SelectorValveSystem(controller, config)
        disc_pump = (DiscPump(controller, run_control)
                     if config.application == "Open Chamber" else None)
    except Exception:
        # A dead controller or a stuck valve is not survivable, but the pieces
        # already running must not outlive the failed launch: the controller's
        # reader thread holds the MCU port, and a sensor left subscribed would
        # keep a handler on a stream nobody owns. Shielded per step, so one
        # close failure cannot strand the rest or replace the bring-up error
        # this block re-raises. (The syringe pump is deliberately absent:
        # SyringePump.close() only drops a reference, so calling it here would
        # release nothing -- the port lives until the object is collected.)
        cleanups = [sensor.close for sensor in flow_sensors]
        if temperature_controller is not None:
            cleanups.append(temperature_controller.close)
        cleanups.append(controller.close)
        _run_shielded(cleanups)
        raise

    return DeviceSet(config, controller, syringe_pump, selector_valves,
                     disc_pump, temperature_controller, flow_sensors,
                     run_control)


def build_operations(config, devices, on_warning=None):
    """The operations class the application selects, wired to `devices`.

    on_warning is where draw-protection notices go (Flow Cell only); None
    keeps MERFISHOperations' default of print, which is all the CLI needs.
    """
    if config.application == "Flow Cell":
        return MERFISHOperations(config, devices.syringe_pump,
                                 devices.selector_valves,
                                 devices.temperature_controller,
                                 devices.flow_sensors,
                                 on_warning=on_warning)
    if config.application == "Open Chamber":
        return OpenChamberOperations(config, devices.syringe_pump,
                                     devices.selector_valves,
                                     devices.disc_pump,
                                     devices.temperature_controller)
    raise ValueError(f"Unsupported application: {config.application!r}")


def build_worker(devices, operations, sequences, callbacks=None):
    """Wire one run's worker to `devices`: the shared run_control, and the
    make_safe callback, which this function owns."""
    callbacks = dict(callbacks or {})
    if "make_safe" in callbacks:
        raise ValueError("build_worker supplies make_safe; do not pass one")
    callbacks["make_safe"] = devices.make_safe
    return ExperimentWorker(operations, sequences, devices.config, callbacks,
                            run_control=devices.run_control)
