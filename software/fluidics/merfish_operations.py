import logging
from .errors import Cancelled, OperationError
from .flow_monitor import DrawGuard
from . import sequence_utils

_logger = logging.getLogger(__name__)


class MERFISHOperations():
    def __init__(self, config, syringe_pump, selector_valves,
                 temperature_controller=None, flow_sensors=None, on_warning=None,
                 run_control=None):
        self.config = config
        self.sp = syringe_pump
        self.sv = selector_valves
        self.tc = temperature_controller
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
        # A pause parks here, before the sensors are armed. Not inside: the
        # sensor publishes on every MCU packet whether the pump is moving or
        # not, so a parked run would read as no flow and a `stop` sensor would
        # cancel it with a fault nobody caused.
        self.run_control.checkpoint()
        with DrawGuard(self.flow_sensors,
                       expected_ul_min=self.sp.get_flow_rate(speed_code),
                       run_control=self.run_control,
                       log=self.on_warning), self.run_control.no_hold():
            self.sp.execute()

    def process_sequence(self, sequence):
        _logger.debug("Running: %s", sequence)
        seq_type = sequence['type']

        if seq_type == "flow_reagent":
            self.flow_reagent(
                sequence['fluidic_port'],
                sequence['flow_rate'],
                sequence['volume'],
                sequence.get('fill_tubing_with'))
        elif seq_type in ("priming", "clean_up"):
            self.priming_or_clean_up(
                sequence['fluidic_port'],
                sequence['flow_rate'],
                sequence['volume'],
                sequence.get('use_ports'))
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

    def flow_reagent(self, port, flow_rate, volume, fill_tubing_with_port):
        """
        Flow reagent from {port}. Finally, fill the tubings before sample with reagent from {fill_tubing_with_port}.
        Only the ports on the last selector valve should be used for {fill_tubing_with_port}, usually a common buffer.

        Both draws run under a DrawGuard. The dispense-to-waste inside
        _empty_syringe_pump_on_full does not: it goes out the waste port, not
        through the flow cell, so the sensors would read nothing and every full
        syringe would fault.
        """
        speed_code = self.sp.flow_rate_to_speed_code(flow_rate)
        try:
            self.sp.reset_chain()
            self._empty_syringe_pump_on_full(volume)
            self.sv.open_port(port)
            self.sp.extract(self.extract_port, volume, speed_code)
            self._guarded_execute(speed_code)
            if fill_tubing_with_port:
                self.sv.open_port(int(fill_tubing_with_port))
                self._empty_syringe_pump_on_full(self.sv.get_tubing_fluid_amount_to_valve(fill_tubing_with_port))
                self.sp.extract(self.extract_port, self.sv.get_tubing_fluid_amount_to_valve(fill_tubing_with_port), speed_code)
                self._guarded_execute(speed_code)

        except Cancelled:
            # The run ending on purpose -- an abort, or a flow fault raised on
            # the draw with its sensor, band and measurement. The wrapper
            # below would flatten either into an OperationError string and
            # report it as a failed step.
            raise
        except Exception as e:
            raise OperationError(f"Error in flow_reagent from port: {port}: {str(e)}")

    def priming_or_clean_up(self, port, flow_rate, volume, use_ports=None):
        """
        Fill the tubings from reagents to selector valves with the corresponding reagents. Finally, fill the tubings before
        syringe pump with {volume} of the reagent from {port}.
        This method should work for both priming and cleaning. For priming, use a wash buffer for {port}; for cleaning, use water
        for all ports.
        """
        speed_code = self.sp.flow_rate_to_speed_code(flow_rate)
        try:
            self.sp.reset_chain()
            self.sp.dispense_to_waste()
            self.sp.execute()
            for i in range(1, self.sv.available_port_number + 1):
                if use_ports is not None and i not in use_ports:
                    continue
                volume_to_port = self.sv.get_tubing_fluid_amount_to_port(i)
                if volume_to_port:
                    self.sv.open_port(i)
                    self.sp.extract(self.extract_port, volume_to_port, speed_code)
                    self.sp.dispense_to_waste()
                    self.sp.execute()
                    # There could be a lot of air in a flow cell system, which may delay the stabilization of the liquid flow.
                    # So we wait a second here for the flow to stabilize -- on
                    # the run's signal, so a cancel raises out of it rather
                    # than being noticed a second later.
                    self.run_control.delay(1)

            self.sv.open_port(port)
            self.sp.extract(self.extract_port, volume, speed_code)
            self.sp.execute()
        except Cancelled:
            raise
        except Exception as e:
            raise OperationError(f"Error in priming_or_clean_up: {str(e)}")
