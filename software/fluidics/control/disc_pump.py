import logging
from ._def import CMD_SET, MCU_CONSTANTS

from ..errors import RunControl

_logger = logging.getLogger(__name__)


class DiscPump():
    def __init__(self, fluid_controller, run_control=None):
        self.fc = fluid_controller
        # Shared with every other device of the run when built through
        # build_devices; private when constructed alone.
        self.run_control = run_control if run_control is not None else RunControl()
        self._is_started = False
        self.fc.send_command(CMD_SET.INITIALIZE_DISC_PUMP, MCU_CONSTANTS.TTP_MAX_PW)
        _logger.info("Disc pump initialized.")

    def abort(self):
        if self._is_started:
            self.stop()
        self.run_control.cancel()

    def reset_abort(self):
        self.run_control.reset()

    def _set_power(self, power):
        self.fc.send_command(CMD_SET.SET_PUMP_PWR_OPEN_LOOP, power)
        self.fc.wait_for_completion()

    def aspirate(self, time_s):
        """Full power for time_s seconds, then off. Raises the run's cause if
        cancelled -- before starting (a pre-tripped signal used to produce a
        full-power pulse followed at once by power 0) or mid-way, after the
        pump has been switched off."""
        self.run_control.check()
        self._set_power(MCU_CONSTANTS.TTP_MAX_PW)
        try:
            self.run_control.wait(time_s)
        finally:
            self._set_power(0)
        self.run_control.check()

    def start(self, power_percentage):
        self._set_power(power_percentage * MCU_CONSTANTS.TTP_MAX_PW)
        self._is_started = True

    def stop(self):
        self._set_power(0)
        self._is_started = False
