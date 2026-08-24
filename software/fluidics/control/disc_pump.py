import logging
from ._def import CMD_SET, MCU_CONSTANTS
import threading

_logger = logging.getLogger(__name__)

class DiscPump():
    def __init__(self, fluid_controller):
        self.fc = fluid_controller
        self._is_started = False
        self._abort_event = threading.Event()
        self._abort_event.clear()
        self.fc.send_command(CMD_SET.INITIALIZE_DISC_PUMP, MCU_CONSTANTS.TTP_MAX_PW)
        _logger.info("Disc pump initialized.")

    def abort(self):
        if self._is_started:
            self.stop()
        self._abort_event.set()

    def reset_abort(self):
        self._abort_event.clear()

    def aspirate(self, time_s):
        self.fc.send_command(CMD_SET.SET_PUMP_PWR_OPEN_LOOP, MCU_CONSTANTS.TTP_MAX_PW)
        self.fc.wait_for_completion()
        self._abort_event.wait(time_s)
        self.fc.send_command(CMD_SET.SET_PUMP_PWR_OPEN_LOOP, 0)
        self.fc.wait_for_completion()

    def start(self, power_percentage):
        self.fc.send_command(CMD_SET.SET_PUMP_PWR_OPEN_LOOP, power_percentage * MCU_CONSTANTS.TTP_MAX_PW)
        self.fc.wait_for_completion()
        self._is_started = True

    def stop(self):
        self.fc.send_command(CMD_SET.SET_PUMP_PWR_OPEN_LOOP, 0)
        self.fc.wait_for_completion()
        self._is_started = False