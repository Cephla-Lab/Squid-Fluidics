import logging
from .errors import Cancelled, OperationError
from . import sequence_utils

_logger = logging.getLogger(__name__)

class OpenChamberOperations():
    def __init__(self, config, syringe_pump, selector_valves, disc_pump,
                 temperature_controller=None, run_control=None):
        self.config = config
        self.sp = syringe_pump
        self.sv = selector_valves
        self.dp = disc_pump
        self.tc = temperature_controller
        # The run's cancellation signal, borrowed once here rather than read
        # off whichever device is in scope. build_operations passes the
        # DeviceSet's, which every device shares.
        self.run_control = run_control if run_control is not None else syringe_pump.run_control

        # Cache frequently used config values
        sp = self.config.syringe_pump
        self.extract_port = sp.extract_port
        self.dispense_port = sp.dispense_port
        self.waste_port = sp.waste_port
        self.speed_code_limit = sp.speed_code_limit
        self.syringe_volume_ul = sp.volume_ul
        self.tubing_sv_to_sp = self.config.reagent_selection.common_tubing_fluid_amount_ul
        self.tubing_sp_to_oc = self.config.sample_selection_inlet.common_tubing_fluid_amount_ul
        self.chamber_volume_ul = self.config.samples.chamber_volume_ul

    def process_sequence(self, sequence):
        _logger.debug("Running: %s", sequence)
        seq_type = sequence['type']

        if seq_type == "add_reagent":
            self.add_reagent(
                sequence['fluidic_port'],
                sequence['flow_rate'],
                sequence['volume'],
                sequence.get('fill_tubing_with'))
        elif seq_type == "clear_and_add_reagent":
            self.clear_and_add_reagent(
                sequence['fluidic_port'],
                sequence['flow_rate'],
                sequence['volume'],
                sequence.get('fill_tubing_with'))
        elif seq_type == "wash_constant_flow":
            self.wash_with_constant_flow(
                sequence['fluidic_port'],
                sequence['flow_rate'],
                sequence['volume'],
                sequence.get('fill_tubing_with'))
        elif seq_type == "priming":
            self.priming_or_clean_up(
                sequence['fluidic_port'],
                sequence['flow_rate'],
                sequence['volume'])
        elif seq_type == "clean_up":
            self.priming_or_clean_up(
                sequence['fluidic_port'],
                sequence['flow_rate'],
                sequence['volume'],
                clean_up=True)
        elif seq_type == "set_temperature":
            self.set_temperature(sequence['temperature'])
        else:
            raise ValueError(f"Unknown sequence type: {seq_type}")

    def clear_and_add_reagent(self, port, flow_rate, volume, fill_tubing_with_port):
        """
        Clear previous liquid in tubings by 1) dispensing sv_to_sp into waste and 2) dispensing sp_to_oc into sample and aspirate,
        then add reagent from {port}
        """
        speed_code = self.sp.flow_rate_to_speed_code(flow_rate)
        volume = min(self.chamber_volume_ul, volume)
        try:
            self.sp.reset_chain()
            self.sp.dispense_to_waste(self.speed_code_limit)
            self.sp.execute()
            self.sv.open_port(port)
            # Clear previous buffer in tubings (selector valve to syringe pump)
            self.sp.extract(self.extract_port, self.tubing_sv_to_sp, self.speed_code_limit)
            self.sp.dispense_to_waste(self.speed_code_limit)
            # Assume reagent volume is greater than 'tubing_sv_to_sp'
            self.sp.extract(self.extract_port, volume - self.tubing_sv_to_sp, self.speed_code_limit)
            self.sp.execute()
            if fill_tubing_with_port:
                self.sv.open_port(int(fill_tubing_with_port))
            # Draw all reagent into syringe
            self.sp.extract(self.extract_port, self.tubing_sv_to_sp, self.speed_code_limit)
            self.sp.execute()
            self.dp.aspirate(10)
            # Clear previous buffer in tubings (syringe pump to chamber)
            # Assume reagent volume is greater than 'tubing_sp_to_oc'
            self.sp.dispense(self.dispense_port, self.tubing_sp_to_oc, speed_code)
            self.sp.execute()
            self.dp.aspirate(10)
            # Push reagent to open chamber
            self.sp.dispense(self.dispense_port, volume - self.tubing_sp_to_oc, speed_code)
            self.sp.extract(self.extract_port, self.tubing_sp_to_oc, self.speed_code_limit)
            self.sp.dispense(self.dispense_port, self.tubing_sp_to_oc, speed_code)
            self.sp.execute()
        except Cancelled:
            raise
        except Exception as e:
            raise OperationError(f"Error in clear_and_add_reagent from port: {port}: {str(e)}")

    def add_reagent(self, port, flow_rate, volume, fill_tubing_with_port):
        """
        Add the reagent from {port} to the chamber, assuming tubings already contain the same reagent.
        """
        speed_code = self.sp.flow_rate_to_speed_code(flow_rate)
        volume = min(self.chamber_volume_ul, volume)
        try:
            self.sp.reset_chain()
            self.sp.dispense_to_waste(self.speed_code_limit)
            self.sp.execute()
            # Assume syringe_volume > (sp_to_oc + sv_to_sp) > sp_to_oc > sv_to_sp > overflow (sp_to_oc + sv_to_sp - chamber_volume)
            # and syringe_volume > chamber_volume > sp_to_oc > sv_to_sp > overflow (sp_to_oc + sv_to_sp - chamber_volume)
            # TODO: Make sure if this assumption is true in most cases. If not, we may need to update the sequence logic
            syringe_vol = 0
            if fill_tubing_with_port:
                self.sv.open_port(port)
                syringe_vol += max(volume - self.tubing_sp_to_oc - self.tubing_sv_to_sp, 0)
                self.sp.extract(self.extract_port, syringe_vol, self.speed_code_limit)
                self.sp.execute()
                self.sv.open_port(int(fill_tubing_with_port))
                # Draw sv_to_sp into syringe
                syringe_vol += self.tubing_sv_to_sp
                self.sp.extract(self.extract_port, self.tubing_sv_to_sp, self.speed_code_limit)
                # Discard overflow amount (in the case chamber_volume < sp_to_oc + sv_to_sp)
                overflow = max(self.tubing_sp_to_oc + self.tubing_sv_to_sp - volume, 0)
                syringe_vol -= overflow
                if overflow > 0:
                    self.sp.dispense(self.waste_port, overflow, speed_code)
                    self.sp.execute()
            else:
                self.sv.open_port(port)
                # Draw the amount needed into syringe (volume - sp_to_oc)
                syringe_vol = volume - self.tubing_sp_to_oc
                self.sp.extract(self.extract_port, syringe_vol, self.speed_code_limit)
                self.sp.execute()
            # Push reagent to sample
            self.sp.dispense(self.dispense_port, syringe_vol, speed_code)
            self.sp.extract(self.extract_port, self.tubing_sp_to_oc, self.speed_code_limit)
            self.sp.dispense(self.dispense_port, self.tubing_sp_to_oc, speed_code)
            # Clear previous liquid in open chamber
            self.dp.aspirate(10)
            self.sp.execute()
        except Cancelled:
            raise
        except Exception as e:
            raise OperationError(f"Error in add_reagent from port: {port}: {str(e)}")

    def wash_with_constant_flow(self, port, flow_rate, volume, fill_tubing_with_port):
        """
        Add reagent from {port} while draining with disc pump on the othe side to keep a constant flow in the chamber.
        """
        speed_code = self.sp.flow_rate_to_speed_code(flow_rate)
        volume = min(self.syringe_volume_ul, volume)
        try:
            self.sp.reset_chain()
            self.sp.dispense_to_waste(self.speed_code_limit)
            self.sp.execute()
            self.sv.open_port(port)
            # No need to clear previous liquid in tubings (sv_to_sp)
            self.sp.extract(self.extract_port, volume - self.tubing_sv_to_sp, self.speed_code_limit)
            self.sp.execute()
            if fill_tubing_with_port:
                self.sv.open_port(fill_tubing_with_port)
            self.sp.extract(self.extract_port, self.tubing_sv_to_sp, self.speed_code_limit)
            self.sp.dispense(self.dispense_port, volume, speed_code)
            # Push reagent to open chamber
            self._execute_under_drain()
            if fill_tubing_with_port:
                # Wash with additional amount of buffer in tubing sp_to_oc and fill with next reagent
                self.sp.extract(self.extract_port, self.tubing_sp_to_oc, self.speed_code_limit)
                self.sp.dispense(self.dispense_port, self.tubing_sp_to_oc, speed_code)
                self._execute_under_drain()
            # On the run's signal, so a cancel landing in this settle wait
            # raises out of the operation instead of returning normally.
            self.run_control.delay(1)
        except Cancelled:
            raise
        except Exception as e:
            raise OperationError(f"Error in wash_with_constant_flow from port: {port}: {str(e)}")

    def _execute_under_drain(self):
        """Run the queued chain with the drain pump aspirating. The drain runs
        across the whole dispense and follows the syringe into a pause: a
        drain pulling with no inflow would empty the chamber for the length
        of the hold. A failing execute must not leave it on either."""
        self.dp.start(0.3)      # gates: a pause already pending parks here, drain off
        try:
            with self.run_control.when_held(self.dp.stop, lambda: self.dp.start(0.3)):
                self.sp.execute()
        finally:
            self.dp.stop()

    def priming_or_clean_up(self, port, flow_rate, volume, use_ports=None, clean_up=False):
        """
        Fill the tubings from reagents to selector valves with the corresponding reagents. Finally, fill the tubings before
        syringe pump with {volume} of the reagent from {port}.
        This method should work for both priming and cleaning. For priming, use a wash buffer for {port}; for cleaning, use water
        for all ports.
        """
        speed_code = self.sp.flow_rate_to_speed_code(flow_rate)
        # We need to limit the flow rate for priming, because it takes time for flow to stabilize when there's air in the tubings.
        priming_speed_code_limit = self.sp.flow_rate_to_speed_code(8000)
        try:
            self.sp.reset_chain()
            self.sp.dispense_to_waste()
            self.sp.execute()  # TODO: needs some refactoring here
            # The ports the rig offers, not every position the cascade
            # can address: one with no tubing volume has no line on it.
            # This loop's own `if volume_to_port` used to be that rule's
            # third spelling.
            for i in self.sv.get_ports():
                if use_ports is not None and i not in use_ports:
                    continue
                volume_to_port = self.sv.get_tubing_fluid_amount_to_port(i)
                self.sv.open_port(i)
                self.sp.extract(self.extract_port, volume_to_port, priming_speed_code_limit)
                self.sp.dispense_to_waste()
                self.sp.execute()

            self.sv.open_port(port)
            self.sp.extract(self.extract_port, volume, priming_speed_code_limit)
            self.sp.dispense(self.dispense_port, volume, speed_code)
            self.sp.execute()
            if clean_up:
                self.dp.aspirate(20)
        except Cancelled:
            raise
        except Exception as e:
            raise OperationError(f"Error in priming_or_clean_up: {str(e)}")

    def set_temperature(self, target):
        sequence_utils.set_temperature(self.tc, target, self.run_control)
