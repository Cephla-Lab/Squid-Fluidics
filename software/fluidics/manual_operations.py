"""The manual tab's verbs, as a headless object.

`ManualOperations(devices)` gives a script -- or the GUI -- the moves an
operator makes by hand: turn the selector valves to a reagent, draw or push a
volume through the syringe pump, empty it to waste, run the drain for a
while. Each verb blocks until the hardware has finished and raises whatever
the driver raises; the GUI runs them off its own thread. None of it is a
sequence: no tubing arithmetic, no overflow protection, no flow guard -- an
operator asking for 300 uL gets 300 uL.

The verbs go through the same gated driver calls a run does, on the
DeviceSet's RunControl, so devices.abort() stops a manual move the way it
stops a run.
"""

import logging

_logger = logging.getLogger(__name__)


class ManualOperations:
    def __init__(self, devices):
        self.devices = devices
        self.sp = devices.syringe_pump
        self.sv = devices.selector_valves
        self.dp = devices.disc_pump

    # --- what the rig offers ---

    def flow_rates(self):
        """The flow rates the syringe pump can be asked for, in uL/min,
        fastest first: one per speed code the rig's limit allows."""
        return [self.sp.get_flow_rate(code)
                for code in range(self.sp.speed_code_limit,
                                  len(self.sp.SPEED_SEC_MAPPING))]

    def held_volume_ul(self):
        """What the syringe holds now, read from the plunger."""
        return self.sp.get_plunger_position() * self.sp.volume

    # --- the verbs ---

    def open_port(self, port):
        """Turn the selector valves to reagent `port`."""
        _logger.info("Manual: selector valve to port %d.", port)
        self.sv.open_port(port)

    def extract(self, port, volume_ul, flow_rate_ul_min, on_started=None):
        """Draw `volume_ul` in through syringe-pump `port` at `flow_rate_ul_min`."""
        _logger.info("Manual: extract %s uL through port %d at %s uL/min.",
                     volume_ul, port, flow_rate_ul_min)
        self._move(lambda code: self.sp.extract(port, volume_ul, code),
                   flow_rate_ul_min, on_started)

    def dispense(self, port, volume_ul, flow_rate_ul_min, on_started=None):
        """Push `volume_ul` out through syringe-pump `port` at `flow_rate_ul_min`."""
        _logger.info("Manual: dispense %s uL through port %d at %s uL/min.",
                     volume_ul, port, flow_rate_ul_min)
        self._move(lambda code: self.sp.dispense(port, volume_ul, code),
                   flow_rate_ul_min, on_started)

    def empty_to_waste(self, on_started=None):
        """Push whatever the syringe holds out to waste, as fast as the rig allows."""
        _logger.info("Manual: empty the syringe to waste.")
        self._move(lambda code: self.sp.dispense_to_waste(), None, on_started)

    def aspirate(self, seconds, on_started=None):
        """Run the drain pump at full power for `seconds` (Open Chamber rigs)."""
        if self.dp is None:
            raise RuntimeError("this rig has no disc pump to aspirate with")
        _logger.info("Manual: aspirate for %s s.", seconds)
        if on_started is not None:
            on_started(seconds)
        self.dp.aspirate(seconds)

    def _move(self, queue, flow_rate_ul_min, on_started):
        """One syringe move: a fresh queue, the op, then the wait.

        `on_started`, if given, gets the pump's own time estimate once the
        move is queued and before it runs -- what a progress bar needs. The
        queue is reset first so a move never inherits ops a failed one left.
        """
        self.sp.reset_chain()
        code = (None if flow_rate_ul_min is None
                else self.sp.flow_rate_to_speed_code(flow_rate_ul_min))
        estimate = queue(code)
        if on_started is not None:
            on_started(estimate)
        self.sp.execute()
