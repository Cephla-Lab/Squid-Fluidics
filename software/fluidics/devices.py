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
what a bring-up survives by design -- a device left out (no temperature
controller, no flow sensors) or a configured mode switched off because
nothing would act on it (draw protection); anything else raises.
"""

import logging
from functools import partial

from .control._def import CMD_SET
from .control.config import gantry
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
from .reservoirs import ReservoirLedger, default_ledger_path, reservoir_definitions
from .robot.labware import parse_ref
from .robot.sipper import RobotSipper
from .robot.stage import POLL_SECONDS, XYZStage
from .vocabulary import sipper_vocabulary


# The vocabulary of what a degraded bring-up reports, passed to on_issue as
# its `kind`. Constants rather than bare literals so the GUI's per-kind dialog
# hints key on the same names the emit sites use.
ISSUE_TEMPERATURE_CONTROLLER = "temperature_controller"
ISSUE_FLOW_SENSORS = "flow_sensors"
ISSUE_DRAW_PROTECTION = "draw_protection"


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
    empty), robot (the RobotSipper, None without a gantry), reservoirs (the
    ReservoirLedger, None when the config tracks no volume), and run_control
    -- the one cancellation signal every waiting device shares; RunSession
    drives it (cancel, pause, reset), so an abort from anywhere reaches every
    device. Built by build_devices(); nothing else should construct one.
    """

    def __init__(self, config, controller, syringe_pump, selector_valves,
                 disc_pump, temperature_controller, flow_sensors, run_control,
                 robot=None, reservoirs=None):
        self.config = config
        self.run_control = run_control
        self.controller = controller
        self.syringe_pump = syringe_pump
        self.selector_valves = selector_valves
        self.disc_pump = disc_pump
        self.temperature_controller = temperature_controller
        self.flow_sensors = flow_sensors
        self.robot = robot                  # RobotSipper, or None without a gantry
        self.reservoirs = reservoirs        # ReservoirLedger, or None when nothing is tracked
        self._closed = False

    def make_safe(self):
        """Leave nothing running once a run has ended early -- abort or failure
        alike; the failure is the unattended case. Halts the syringe pump
        (already halted after a cancel, not after a failure mid-move), TEC
        output off on every channel, drain pump off. Shielded per step;
        returns the exceptions raised, already logged, for the worker to
        report."""
        steps = [self.syringe_pump.halt]
        if self.robot is not None:
            steps.append(self.robot.halt)
        tc = self.temperature_controller
        if tc is not None:
            steps.extend(partial(tc.set_output_enabled, c, False)
                         for c in range(1, tc.channels + 1))
        if self.disc_pump is not None:
            steps.append(self.disc_pump.stop)
        return _run_shielded(steps, doing="making the rig safe")

    def close(self, empty_syringe=None, quiescent=True):
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

        quiescent: False when a job is still running on these devices (the
        facade's close after its wait timed out). Then nothing moves: the
        abort latch stays set so the job cannot resume, the syringe is not
        parked, and the robot halts where it is and makes no move. A
        quiescent close leaves the robot where it is, the tip wet in its last well.

        Threading: a non-quiescent close halts the robot from this, the
        closer's, thread while the stuck job may still poll the same RS-485
        service. The vendored driver serializes each Modbus request and its
        response (the client's RLock in control/nimotion/modbus_rtu/client.py,
        over the RLock serial_comm/serial_port.py takes per read and write);
        how a halt interleaves with the job's polls on the drives is a bench
        item.
        """
        if self._closed:
            return []
        self._closed = True

        if empty_syringe is None:
            empty_syringe = self.config.application == "Flow Cell"

        # One call per step, so a failure skips nothing but itself: one dead
        # sensor must not strand its siblings. The reset comes before the
        # park-to-waste close, which is a move of its own: that move passes a
        # gate, so on a paused session it would hold here forever.
        steps = []
        if self.temperature_controller is not None:
            steps.append(self.temperature_controller.close)
        steps.extend(sensor.close for sensor in self.flow_sensors)
        if quiescent:
            steps.append(self.run_control.reset)
        if self.robot is not None:
            # After the reset, before the syringe parks (the order kept from
            # when the robot parked on close). The robot makes no move either
            # way: quiescent, the tip rests wet in its last well; otherwise
            # it halts. The robot's own shielded steps are folded into
            # this one.
            steps.append(lambda: _raise_first(self.robot.close(quiescent=quiescent)))
        steps.append(lambda: self.syringe_pump.close(empty_syringe if quiescent else False))
        steps.append(self.controller.close)
        if self.reservoirs is not None:
            steps.append(self.reservoirs.close)

        return _run_shielded(steps)


def _raise_first(errors):
    if errors:
        raise errors[0]


def _build_robot(config, simulation, run_control, instant):
    """A connected, homed, retracted RobotSipper, or raise. Partial state is
    torn down here (the stage's non-quiescent close), the way the
    flow-sensor starter closes what it started."""
    g = gantry(config)
    vocabulary = sipper_vocabulary(config)
    names = dict(g.axes)
    if simulation:
        from .robot.simulation import XYG321A_STROKES_MM, SimulatedService
        strokes = {name: XYG321A_STROKES_MM.get(name, (0.0, 300.0)) for name in names.values()}
        service = SimulatedService(names.values(), strokes, instant=instant)
    else:
        from .robot._driver import open_service          # the one import site
        from .control.discovery import find_serial_port
        port = g.port or find_serial_port(g.serial_number, "RS-485 adapter")
        service, names = open_service(port, g.baudrate, names)
    stage = XYZStage(service, names, g.travel_z_mm, g.xy_velocity_mm_s,
                     g.z_velocity_mm_s, run_control,
                     poll_seconds=0.0 if instant else POLL_SECONDS)
    robot = RobotSipper(stage, vocabulary.placements(), g.default_reservoir,
                        g.agitate_amplitude_mm, park_xy_mm=g.park_xy_mm)
    try:
        stage.connect()
        robot.home()
    except BaseException:
        # A Ctrl+C while Z homes too: the drives must not stay enabled.
        stage.close(quiescent=False)
        raise
    return robot


def _build_ledger(config, simulation, ledger_path):
    """The ledger for everything this config tracks, or None when it tracks
    nothing. A simulated rig gets a fresh in-memory ledger unless a path
    is given (the tests', the CLI's --ledger)."""
    definitions = reservoir_definitions(config)
    if not definitions:
        return None
    if simulation and ledger_path is None:
        return ReservoirLedger(definitions)
    path = ledger_path or default_ledger_path(config.source_path or "in-memory")
    return ReservoirLedger.load(path, definitions)


def build_devices(config, simulation=False, on_issue=None, run_control=None,
                  instant=False, ledger_path=None):
    """Construct and start the full stack for `config`. Returns a DeviceSet.

    Mirrors what the two entry points did, once: take the reservoir
    ledger's lock (before any device is constructed, so a second launch on
    a rig in use stops there); construct controller, syringe pump, and
    (optionally) temperature controller; begin() the controller and CLEAR
    the MCU; start the flow sensors; build the robot when the config has a
    gantry (connected, homed, retracted: after the sensors, before the
    valves); build the selector valves (which homes them, today a
    constructor side effect); attach the disc pump for Open Chamber; attach
    the ledger to the pump's draws.

    Degradation policy is the GUI's: a temperature controller that fails to
    come up on hardware, or flow sensors that fail in either mode, are
    reported through on_issue and left out rather than failing the launch.
    So is draw protection configured where nothing will act on it
    (draw_protection_available): reported, and switched off.
    The controller, pump, valves, robot and ledger are not survivable --
    those raise, after closing whatever had already started (the robot
    without a move, the ledger's lock last).

    ledger_path: where the reservoir ledger lives. None: beside the run
    reports, named after the config; on a simulated rig, a fresh ledger in
    memory, every reservoir declared full.

    instant: switch the simulated rig's pacing off -- the controller's
    one-second commands and the pump's five-second moves take no time. For
    rigs built to be read rather than watched: the time estimate's replay,
    tests. Simulation only.
    """
    if instant and not simulation:
        raise ValueError("instant pacing is for simulated rigs only")
    if on_issue is None:
        on_issue = _print_issue
    # Pick the classes once so the config-to-constructor mapping is written
    # once -- the sim/real copy drift this module exists to end.
    controller_cls = FluidControllerSimulation if simulation else FluidController
    pump_cls = SyringePumpSimulation if simulation else SyringePump
    tc_cls = TCMControllerSimulation if simulation else TCMController

    # One cancellation signal for the whole run -- see RunControl.
    run_control = run_control if run_control is not None else RunControl()

    controller = None
    syringe_pump = None
    temperature_controller = None
    flow_sensors = []
    robot = None
    reservoirs = None
    try:
        # The ledger's lock first, before any device is constructed: a
        # second launch on a rig already in use stops here, before it talks
        # to the pump (the real pump's constructor sends it commands, and a
        # pump that answers "not initialized" is initialized by the vendor
        # driver), clears the MCU, homes the robot (lifting the first
        # session's tip out of its well) or homes the valves. A ledger it
        # cannot lock fails the launch with nothing to release; a file it
        # cannot read loads as undeclared and the launch goes on.
        reservoirs = _build_ledger(config, simulation, ledger_path)

        controller = controller_cls(config.microcontroller.serial_number)
        syringe_pump = pump_cls(
            sn=config.syringe_pump.serial_number,
            syringe_ul=config.syringe_pump.volume_ul,
            speed_code_limit=config.syringe_pump.speed_code_limit,
            waste_port=config.syringe_pump.waste_port,
            run_control=run_control)
        if instant:
            # On the instances, before bring-up -- the valves' homing waits are
            # paced too -- so a concurrently simulated rig keeps its own pacing.
            controller.COMMAND_SECONDS = 0
            syringe_pump.ESTIMATE_SECONDS = 0

        if config.temperature_controller is not None:
            tc_cfg = config.temperature_controller
            try:
                temperature_controller = tc_cls(
                    sn=tc_cfg.serial_number,
                    channels=tc_cfg.channels,
                    tolerance_celsius=tc_cfg.tolerance_celsius,
                    stabilization_timeout_seconds=tc_cfg.stabilization_timeout_seconds,
                )
            except Exception as e:
                # Survivable only on hardware, where the usual cause is a flaky
                # port or serial-number mismatch. In simulation there is no port
                # to be flaky: a failure is a real bug and raises.
                if simulation:
                    raise
                on_issue(ISSUE_TEMPERATURE_CONTROLLER,
                         f"Failed to initialize temperature controller: {e}")

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
        _switch_off_inert_draw_protection(config, flow_sensors, on_issue)

        # The robot before the valves: its own port, no Teensy dependence,
        # and a wrong adapter fails before the valves spend their homing
        # time. Not survivable: a sipper that cannot home cannot select.
        if gantry(config) is not None:
            robot = _build_robot(config, simulation, run_control, instant)

        selector_valves = SelectorValveSystem(controller, config, run_control)
        disc_pump = (DiscPump(controller, run_control)
                     if config.application == "Open Chamber" else None)

        # Attached once the draws it authorizes can be routed: it reads the
        # valves' port and the sipper's well.
        if reservoirs is not None:
            reservoirs.attach(
                syringe_pump, None if robot is None else gantry(config).selector_valve_port,
                config.syringe_pump.extract_port, selector_valves.get_current_port,
                (lambda: None) if robot is None else
                (lambda: None if robot.current_well is None
                 else f"{robot.current_well.reservoir}/{robot.current_well.well}"))
    except BaseException:
        # A pump that is not found, a dead controller, a stuck valve, or a
        # Ctrl+C (an operator watching Z home) is not survivable, but the
        # pieces already running must not outlive the failed launch: the
        # controller's reader thread holds the MCU port, and a sensor left
        # subscribed would keep a handler on a stream nobody owns. Only what
        # was constructed is closed, since a constructor can be what failed.
        # Shielded per step, so one close failure cannot strand the rest or
        # replace the bring-up error this block re-raises. (The syringe pump
        # is deliberately absent: SyringePump.close() only drops a reference,
        # so calling it here would release nothing -- the port lives until
        # the object is collected.)
        cleanups = [sensor.close for sensor in flow_sensors]
        if temperature_controller is not None:
            cleanups.append(temperature_controller.close)
        if robot is not None:
            cleanups.append(lambda: robot.close(quiescent=False))
        if controller is not None:
            cleanups.append(controller.close)
        if reservoirs is not None:
            # The lock last, as DeviceSet.close releases it: the rig stays
            # held until every port this launch opened is released.
            cleanups.append(reservoirs.close)
        _run_shielded(cleanups)
        raise

    return DeviceSet(config, controller, syringe_pump, selector_valves,
                     disc_pump, temperature_controller, flow_sensors,
                     run_control, robot=robot, reservoirs=reservoirs)


def _switch_off_inert_draw_protection(config, flow_sensors, on_issue):
    """A warn/stop mode on an application that never arms the sensors would
    leave the operator believing a draw is protected when nothing is watching
    it: switch it off and say so. Here rather than in a window, so a headless
    run and an embedder hear it too."""
    if draw_protection_available(config):
        return
    configured = [sensor.name for sensor in flow_sensors if sensor.monitor != "off"]
    if not configured:
        return
    for sensor in flow_sensors:
        sensor.monitor = "off"
    on_issue(ISSUE_DRAW_PROTECTION,
             f"Draw protection is configured for {', '.join(configured)} but "
             f"is only available for the Flow Cell application. The sensors "
             f"will read and plot; they will not stop a draw.")


def draw_protection_available(config):
    """Whether anything will act on a flow sensor's warn/stop mode: only the
    Flow Cell operations are handed the sensors (build_operations)."""
    return config.application == "Flow Cell"


def build_operations(config, devices, on_warning=None):
    """The operations class the application selects, wired to `devices`.

    on_warning is where draw-protection notices go (Flow Cell only); the
    system passes its warnings channel, and None keeps MERFISHOperations'
    default of the fluidics logger -- the fallback for direct construction.
    """
    if config.application == "Flow Cell":
        return MERFISHOperations(config, devices.syringe_pump,
                                 devices.selector_valves,
                                 devices.temperature_controller,
                                 devices.flow_sensors,
                                 on_warning=on_warning,
                                 run_control=devices.run_control,
                                 robot=devices.robot,
                                 reservoirs=devices.reservoirs)
    if config.application == "Open Chamber":
        # Not handed the flow sensors: draw_protection_available says so to
        # everyone else. Arm them here and that predicate changes with it.
        return OpenChamberOperations(config, devices.syringe_pump,
                                     devices.selector_valves,
                                     devices.disc_pump,
                                     devices.temperature_controller,
                                     run_control=devices.run_control)
    raise ValueError(f"Unsupported application: {config.application!r}")


def build_worker(devices, operations, plan, run_id, events):
    """Wire one run's worker to `devices`: the shared run_control and the
    make_safe hook (which this function owns). The plan, the run's id and
    the events channel come from whoever starts the run
    (RunSession.start)."""
    return ExperimentWorker(operations, plan,
                            run_id=run_id, events=events,
                            make_safe=devices.make_safe,
                            run_control=devices.run_control)
