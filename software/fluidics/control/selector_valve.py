import logging

from ..errors import RunControl
from ._def import CMD_SET
from .config import available_port_count

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
        _logger.debug("Valve %s: open port %s", self.id, port)
        self.fc.send_command(CMD_SET.SET_ROTARY_VALVE, self.id, port)
        # Interruptible on the run's path: a move the firmware retries around
        # its 2 s poll would otherwise hold an abort for seconds. Homing at
        # construction passes nothing -- there is no run yet.
        self.fc.wait_for_completion(run_control=run_control)
        current_position = self.get_current_position()
        if current_position != port:
            raise RuntimeError(f"current position is {current_position}; expected {port}")
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
        return name_mapping.get('port_' + str(port_index))

    def open_port(self, port_index):
        # No motion on a cancelled run: this is the last driver call that
        # started one without checking, and the operations sit between valve
        # moves and pump chains often enough that a cancel would otherwise
        # rotate a valve on its way out.
        self.run_control.check()
        if not 1 <= port_index <= self.available_port_number:
            # This used to be a silent return, which left whatever port was
            # last open selected -- the draw then pulled the wrong reagent
            # with nothing saying so.
            raise ValueError(
                f"Fluidic port {port_index} is out of range: this "
                f"configuration has ports 1..{self.available_port_number}")

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
        self.valves[-1].open(port_index - ports_processed, self.run_control)
        self.fc.wait_for_completion(run_control=self.run_control)
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
            'port_' + str(port_index))

    def get_port_names(self):
        names = []
        name_mapping = self.config.reagent_selection.selector_valves.name_mapping
        for i in range(1, self.available_port_number + 1):
            name = ''
            if name_mapping is not None:
                name = name_mapping.get('port_' + str(i), '')
            names.append('Port ' + str(i) + ': ' + name)
        return names

    def get_current_port(self):
        return self.current_port
