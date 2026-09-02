import logging

from ..errors import DeviceError, RunControl
from ._def import CMD_SET
from .config import (available_port_count, available_ports, port_key,
                     port_range_note)

_logger = logging.getLogger(__name__)


class SelectorValve():
    def __init__(self, fluid_controller, config, valve_id, initial_pos=1):
        self.fc = fluid_controller
        self.id = valve_id
        self.position = initial_pos
        self.config = config

        sv = self.config.reagent_selection.selector_valves
        self.tubing_fluid_amount_ul = sv.tubing_fluid_amount_to_valve_ul[valve_id]
        self.number_of_ports = sv.number_of_ports[valve_id]
        self.fc.send_command(CMD_SET.INITIALIZE_ROTARY, valve_id, self.number_of_ports)
        self.open(self.position)
        _logger.info("Selector valve id = %s initialized.", valve_id)

    def open(self, port, run_control=None):
        # Checked here rather than once per cascade: a cancel landing between
        # two moves would otherwise still send this one. Cancel only -- the
        # pause gate is one level up, in open_port: parking mid-cascade would
        # leave the path half-routed with current_port still naming the old
        # one. Homing at construction passes no signal, there being no run.
        if run_control is not None:
            run_control.check()
        _logger.debug("Valve %s: open port %s", self.id, port)
        self.fc.send_command(CMD_SET.SET_ROTARY_VALVE, self.id, port)
        self.fc.wait_for_completion(run_control=run_control)
        current_position = self.get_current_position()
        if current_position != port:
            self.position = current_position    # the truth the readback gave
            raise DeviceError(f"Selector valve {self.id}: at position "
                              f"{current_position}, expected {port} -- check "
                              "the valve is free to rotate")
        self.position = port

    def get_current_position(self):
        data = self.fc.get_mcu_status()
        return data['selector_valves_pos'][self.id]


class SelectorValveSystem():
    PORTS_PER_VALVE = 10

    def __init__(self, fluid_controller, config, run_control=None):
        self.fc = fluid_controller
        self.config = config
        self.run_control = run_control if run_control is not None else RunControl()
        rs = self.config.reagent_selection
        sv_config = rs.selector_valves
        self.common_tubing_fluid_amount_ul = rs.common_tubing_fluid_amount_ul
        self.valves = [None] * len(sv_config.valve_ids)
        for i, valve_id in enumerate(sv_config.valve_ids):
            self.valves[i] = SelectorValve(self.fc, self.config, valve_id, 1)
        self.available_port_number = available_port_count(config)
        self.current_port = 1

    def port_to_reagent(self, port_index):
        if port_index > self.available_port_number:
            return None
        name_mapping = self.config.reagent_selection.selector_valves.name_mapping
        if name_mapping is None:
            return None
        return name_mapping.get(port_key(port_index))

    def open_port(self, port_index):
        # The operator-meaningful boundary: a paused run holds before the
        # cascade starts, so it never rests with the path half-routed.
        self.run_control.checkpoint()
        ports = self.get_ports()
        if port_index not in ports:
            # This used to be a silent return, which left whatever port was
            # last open selected -- the draw then pulled the wrong reagent
            # with nothing saying so.
            #
            # Asked against the ports the rig offers, not the range the
            # cascade can address: a position with no line on it would open
            # onto nothing and draw air, and this is the last thing between
            # a port number and a valve move. Gating on the count let a port
            # in a gap through here after the pre-run check had rejected it.
            raise ValueError(
                f"Fluidic port {port_index} is out of range: "
                + port_range_note(ports))

        ports_processed = 0
        for valve in self.valves[:-1]:  # Process all valves except the last one
            ports_in_valve = valve.number_of_ports - 1
            if port_index > (ports_processed + ports_in_valve):
                valve.open(ports_in_valve + 1, self.run_control)  # Open the last port
                ports_processed += ports_in_valve
            else:
                valve.open(port_index - ports_processed, self.run_control)
                self.current_port = port_index
                return

        # If we get here, it's in the last valve
        # No trailing wait: open() has already waited for this move and read
        # the position back.
        self.valves[-1].open(port_index - ports_processed, self.run_control)
        self.current_port = port_index
        return

    def get_tubing_fluid_amount_to_valve(self, port_index):
        # Return the tubing fluid amount from selector valve to sample.
        # = common_tubing + per-valve amount (so total volume matches old config)
        ports_processed = 0
        for i, valve in enumerate(self.valves[:-1]):
            ports_in_valve = valve.number_of_ports - 1
            if port_index > (ports_processed + ports_in_valve):
                ports_processed += ports_in_valve
            else:
                return self.common_tubing_fluid_amount_ul + valve.tubing_fluid_amount_ul

        return self.common_tubing_fluid_amount_ul + self.valves[-1].tubing_fluid_amount_ul

    def get_tubing_fluid_amount_to_port(self, port_index):
        # Return the tubing fluid amount from reagent to selector valve port.
        return self.config.reagent_selection.selector_valves.tubing_fluid_amount_ul.get(
            port_key(port_index))

    def get_ports(self):
        """The ports this rig offers -- see config.available_ports. Not
        every position the cascade can address: one with no tubing volume
        has no line on it."""
        return available_ports(self.config)

    def get_port_names(self):
        """(port, label) for each port the rig offers, in order. The port
        rides along because the list has gaps wherever a position is
        unplumbed -- a caller must not take a list index for a port."""
        return [(port, f"Port {port}: {self.port_to_reagent(port) or ''}")
                for port in self.get_ports()]

    def get_current_port(self):
        return self.current_port
