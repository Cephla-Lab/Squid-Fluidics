import logging
from .control.config import gantry
from .errors import Cancelled, OperationError, ReservoirError
from .flow_monitor import DrawGuard
from .robot.labware import well_key
from .sequences import use_ports_problem
from .vocabulary import sipper_vocabulary
from . import sequence_utils

_logger = logging.getLogger(__name__)


class MERFISHOperations():
    def __init__(self, config, syringe_pump, selector_valves,
                 temperature_controller=None, flow_sensors=None, on_warning=None,
                 run_control=None, robot=None, reservoirs=None):
        self.config = config
        self.sp = syringe_pump
        self.sv = selector_valves
        self.tc = temperature_controller
        # The sipper's robot, None on a rig without a gantry (spec §5), and
        # the reservoir ledger, None only when the operations are built by
        # hand (fixtures, embedders): build_devices builds one on every rig.
        # The gantry block goes with the robot: its port is read here, its
        # wells and push volume where they are used. The vocabulary spells
        # the wells.
        self.robot = robot
        self.reservoirs = reservoirs
        self._gantry = gantry(config) if robot is not None else None
        self.sipper_port = None if self._gantry is None else self._gantry.selector_valve_port
        self.vocabulary = sipper_vocabulary(config)
        # The run's cancellation signal, borrowed once here rather than read
        # off whichever device is in scope. build_operations passes the
        # DeviceSet's, which every device shares.
        self.run_control = run_control if run_control is not None else syringe_pump.run_control
        self.flow_sensors = flow_sensors or []
        # Where a draw-protection notice goes. Defaults to the fluidics
        # logger's WARNING -- console and the run log -- which is all the CLI
        # needs; the GUI passes a channel the operator can see live, since a
        # `warn`-mode fault deliberately raises nothing.
        self.on_warning = on_warning or _logger.warning
        self.extract_port = self.config.syringe_pump.extract_port
        self.speed_code_limit = self.config.syringe_pump.speed_code_limit

    def _guarded_execute(self, speed_code):
        """Run the queued chain with the flow sensors watching it.

        A `stop` sensor's fault cancels the run with itself as the cause; the
        pump's wait wakes, halts the plunger and raises the fault out of
        execute(), here, on the sequence thread -- so the operation unwinds
        with the diagnosis intact.

        The expectation is the pump's actual rate for the code, not the rate
        the sequence asked for: flow_rate_to_speed_code quantizes to the 41
        available codes, so a sequence asking for 480 uL/min gets 500, and
        measuring against 480 would bias the whole band by the rounding.
        """
        # A pause already pending parks here, before the sensors are armed;
        # one that lands mid-draw stops the pump inside, and the guard stands
        # down on the pump's own word for as long as it is not moving.
        self.run_control.checkpoint()
        with DrawGuard(self.flow_sensors,
                       expected_ul_min=self.sp.get_flow_rate(speed_code),
                       run_control=self.run_control,
                       log=self.on_warning,
                       moving=lambda: self.sp.moving):
            self.sp.execute()

    def process_sequence(self, sequence):
        _logger.debug("Running: %s", sequence)
        seq_type = sequence['type']

        if seq_type == "flow_reagent":
            self.flow_reagent(
                sequence['fluidic_port'],
                sequence['flow_rate'],
                sequence['volume'],
                sequence.get('fill_tubing_with'),
                well=sequence.get('well'),
                dip_cycles=sequence.get('dip_cycles') or 0)
        elif seq_type == "push":
            self.push(sequence['flow_rate'], sequence['well'],
                      dip_cycles=sequence.get('dip_cycles') or 0)
        elif seq_type in ("priming", "clean_up"):
            self.priming_or_clean_up(
                sequence['fluidic_port'],
                sequence['flow_rate'],
                sequence['volume'],
                sequence.get('use_ports'),
                well=sequence.get('well'),
                sipper_well=sequence.get('sipper_well'),
                clean_up=(seq_type == "clean_up"))
        elif seq_type == "set_temperature":
            sequence_utils.set_temperature(self.tc, sequence['temperature'],
                                           self.run_control)
        else:
            raise ValueError(f"Unknown sequence type: {seq_type}")

    def _empty_syringe_pump_on_full(self, volume):
        if self.sp.get_current_volume() + self.sp.get_chained_volume() + volume > 0.95 * self.config.syringe_pump.volume_ul:
            try:
                self.sp.dispense_to_waste()
                self.sp.execute()
            except Cancelled:
                raise
            except Exception as e:
                raise OperationError(f"Failed to empty syringe pump: {str(e)}")

    def _select(self, port, well=None, volume_ul=None):
        """Route reagent `port`; for a well, move the sipper into it first
        (spec §5). The ledger's check here is asked before the sipper moves,
        so a draw that cannot fit never moves the robot; the pump asks the
        ledger again before it dispatches. The move ends before the valve
        opens, so the pump never runs with the tip out of liquid (D9)."""
        if well is not None:
            ref = self.vocabulary.resolve(well)
            if self.reservoirs is not None:
                self.reservoirs.check(well_key(ref), volume_ul)
            self.robot.go_to_well(ref)
        self.sv.open_port(port)

    def flow_reagent(self, port, flow_rate, volume, fill_tubing_with_port, well=None, dip_cycles=0):
        """
        Flow reagent from {port}. Finally, fill the tubings before sample with reagent from {fill_tubing_with_port}.
        Only the ports on the last selector valve should be used for {fill_tubing_with_port}, usually a common buffer.
        With `well`, the sipper is moved into it before its port opens, and
        `dip_cycles` dips run before the draw, while the well is full.

        Both draws run under a DrawGuard. The dispense-to-waste inside
        _empty_syringe_pump_on_full does not: it goes out the waste port, not
        through the flow cell, so the sensors would read nothing and every full
        syringe would fault.
        """
        speed_code = self.sp.flow_rate_to_speed_code(flow_rate)
        try:
            self.sp.reset_chain()
            self._empty_syringe_pump_on_full(volume)
            self._select(port, well, volume)
            if dip_cycles:
                self.robot.agitate(dip_cycles)
            self.sp.extract(self.extract_port, volume, speed_code)
            self._guarded_execute(speed_code)
            if fill_tubing_with_port:
                self.sv.open_port(int(fill_tubing_with_port))
                self._empty_syringe_pump_on_full(self.sv.get_tubing_fluid_amount_to_valve(fill_tubing_with_port))
                self.sp.extract(self.extract_port, self.sv.get_tubing_fluid_amount_to_valve(fill_tubing_with_port), speed_code)
                self._guarded_execute(speed_code)

        except (Cancelled, ReservoirError):
            # The run ending on purpose -- an abort, or a flow fault raised on
            # the draw with its sensor, band and measurement -- or the ledger
            # refusing a draw, typed for the gate's replay. The wrapper
            # below would flatten either into an OperationError string and
            # report it as a failed step.
            raise
        except Exception as e:
            raise OperationError(f"Error in flow_reagent from port: {port}: {str(e)}")

    def push(self, flow_rate, well, dip_cycles=0):
        """Draw the config's push volume (tip to flow cell) from `well`
        through the sipper, so what the previous row drew into the line is
        in the flow cell for this row's incubation. A flow_reagent from the
        sipper's port with the config's volume and no tubing fill, so it
        inherits the ledger's check before the move, the move-then-open
        order, the dips, the dump rule, the draw guard and the wrapper."""
        if self.robot is None:
            raise OperationError("push: this rig has no sipper")
        self.flow_reagent(self.sipper_port, flow_rate, self._gantry.push_volume_ul, None,
                          well=well, dip_cycles=dip_cycles)

    def priming_or_clean_up(self, port, flow_rate, volume, use_ports=None, well=None, sipper_well=None,
                            clean_up=False):
        """
        Fill the tubings from reagents to selector valves with the corresponding reagents. Finally, fill the tubings before
        syringe pump with {volume} of the reagent from {port}.
        This method should work for both priming and cleaning. For priming, use a wash buffer for {port}; for cleaning, use water
        for all ports. On a sipper rig, `clean_up` picks the well the sipper's port is primed from when
        `sipper_well` names none: the config's clean-up well rather than its priming well.
        """
        # A use_ports that leaves out the sipper's port is refused here, as
        # at time zero, before the pump or a valve moves: the sipper is never
        # skipped (D7), and the row is not given a draw it did not list.
        problem = use_ports_problem(use_ports, self.sipper_port)
        if problem is not None:
            raise OperationError(problem)
        speed_code = self.sp.flow_rate_to_speed_code(flow_rate)
        try:
            self.sp.reset_chain()
            self.sp.dispense_to_waste()
            self.sp.execute()
            # The ports the rig offers, not every position the cascade
            # can address: one with no tubing volume has no line on it.
            # This loop's own `if volume_to_port` used to be that rule's
            # third spelling.
            for i in self.sv.get_ports():
                if use_ports is not None and i not in use_ports:
                    continue
                volume_to_port = self.sv.get_tubing_fluid_amount_to_port(i)
                if i == self.sipper_port:
                    # The sipper's port is primed from a well: the configured
                    # one for this step type, or the row's override (D7).
                    default = self._gantry.clean_up_well if clean_up else self._gantry.priming_well
                    self._select(i, sipper_well or default, volume_to_port)
                else:
                    self.sv.open_port(i)
                self.sp.extract(self.extract_port, volume_to_port, speed_code)
                self.sp.dispense_to_waste()
                self.sp.execute()
                # There could be a lot of air in a flow cell system, which may delay the stabilization of the liquid flow.
                # So we wait a second here for the flow to stabilize -- on
                # the run's signal, so a cancel raises out of it rather
                # than being noticed a second later.
                self.run_control.delay(1)

            self._select(port, well, volume)
            self.sp.extract(self.extract_port, volume, speed_code)
            self.sp.execute()
        except (Cancelled, ReservoirError):
            raise
        except Exception as e:
            raise OperationError(f"Error in priming_or_clean_up: {str(e)}")
