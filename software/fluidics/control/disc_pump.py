import logging
from ._def import CMD_SET, MCU_CONSTANTS

from ..errors import RunControl

_logger = logging.getLogger(__name__)


class DiscPump():
    def __init__(self, fluid_controller, run_control=None):
        self.fc = fluid_controller
        self.run_control = run_control if run_control is not None else RunControl()
        self._is_started = False
        self.fc.send_command(CMD_SET.INITIALIZE_DISC_PUMP, MCU_CONSTANTS.TTP_MAX_PW)
        _logger.info("Disc pump initialized.")

    def abort(self):
        """Cancel the run; no device I/O here. A running drain is switched
        off by whoever started it as the operation unwinds, and by
        DeviceSet.make_safe() afterwards, on the run's own thread."""
        self.run_control.cancel()

    def reset_abort(self):
        self.run_control.reset()

    def _set_power(self, power):
        self.fc.send_command_blocking(CMD_SET.SET_PUMP_PWR_OPEN_LOOP, power)

    def aspirate(self, time_s):
        """Full power for time_s seconds, then off. Raises the run's cause if
        cancelled before starting or mid-way -- the latter after switching off."""
        self.run_control.check()
        self._set_power(MCU_CONSTANTS.TTP_MAX_PW)
        try:
            self.run_control.sleep(time_s)
        finally:
            self._set_power(0)

    def start(self, power_percentage):
        self._set_power(power_percentage * MCU_CONSTANTS.TTP_MAX_PW)
        self._is_started = True

    def stop(self):
        self._set_power(0)
        self._is_started = False
