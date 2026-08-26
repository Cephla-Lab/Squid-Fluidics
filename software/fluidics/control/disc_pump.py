import logging
from ._def import CMD_SET, MCU_CONSTANTS

from ..errors import RunControl

_logger = logging.getLogger(__name__)


class DiscPump():
    def __init__(self, fluid_controller, run_control=None):
        self.fc = fluid_controller
        self.run_control = run_control if run_control is not None else RunControl()
        self.fc.send_command(CMD_SET.INITIALIZE_DISC_PUMP, MCU_CONSTANTS.TTP_MAX_PW)
        _logger.info("Disc pump initialized.")

    def _set_power(self, power):
        # A power command completes within one MCU status interval (~60 ms);
        # the 30 s default would only ever cost time on a dead MCU -- and
        # make_safe waits on this before the operator hears why the run ended.
        self.fc.send_command_blocking(CMD_SET.SET_PUMP_PWR_OPEN_LOOP, power, timeout=2)

    def aspirate(self, time_s):
        """Full power for time_s seconds of running time, then off.

        A pause switches the pump off and the remainder resumes with it, so a
        run held for ten minutes does not drain the chamber for ten minutes.
        Raises the run's cause if cancelled before starting or mid-way -- the
        latter after switching off.
        """
        remaining = time_s
        while remaining > 0:
            self.start(1.0)          # gates: a paused run holds before it powers
            try:
                remaining -= self.run_control.run_for(remaining)
            finally:
                self.stop()

    def start(self, power_percentage):
        """Power the pump until stop(). Holds while the run is paused and
        raises if it is cancelled, rather than powering it for the moment
        before the caller's next checked call unwinds."""
        self.run_control.checkpoint()
        self._set_power(power_percentage * MCU_CONSTANTS.TTP_MAX_PW)

    def stop(self):
        # Deliberately unchecked: it runs on the unwind path (the drain's own
        # finally) and from DeviceSet.make_safe, both after the cancel.
        self._set_power(0)
