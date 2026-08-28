"""Per-port reagent usage, tallied from what the pump actually drew.

`ReagentUsage` joins the two owners of the facts: the pump publishes every
dispatched extract on its `draws` channel (it alone knows the volume), and
the valve system knows which fluidic port is routed when the draw runs --
a port cannot move mid-draw, so the join is sound. Only draws through the
syringe's reagent path (the configured extract_port) count: chamber draws
and every dispense are not reagent consumption, and a fill-tubing draw is
charged to whichever port is open for it.

The ledger is a subscriber and nothing else: no operation records into it,
so dropping it (or never building it) changes no operations code -- and a
future duration-operated pump joins by publishing its own draws in uL,
converted by its own calibration. Totals reset when a run starts ("from
the beginning of each experiment"); manual draws between runs stay in the
ambient view until then. At a run's end the totals go to the fluidics log,
so the record exists even when nothing was watching.
"""

import logging
import threading

_logger = logging.getLogger(__name__)


class ReagentUsage:
    def __init__(self, config, syringe_pump, selector_valves, session_state=None):
        self._extract_port = config.syringe_pump.extract_port
        self._valves = selector_valves
        self._lock = threading.Lock()
        self._used_ul = {}
        self._running = False    # a run is in progress; log totals at its end
        syringe_pump.draws.subscribe(self._on_draw)
        if session_state is not None:
            session_state.subscribe(self._on_state)

    def snapshot(self):
        """{fluidic port: uL drawn} since the last reset, a copy."""
        with self._lock:
            return dict(self._used_ul)

    def reset(self):
        with self._lock:
            self._used_ul = {}

    # --- subscribers (the pump's thread, the session's threads) ---

    def _on_draw(self, pump_port, volume_ul):
        if pump_port != self._extract_port:
            return                       # not the reagent path
        port = self._valves.get_current_port()
        with self._lock:
            self._used_ul[port] = self._used_ul.get(port, 0) + volume_ul

    def _on_state(self, kind):
        if kind == "run":
            # Notified on the starter's thread before the worker exists, so
            # the reset cannot race the run's first draw.
            self.reset()
            self._running = True
        elif kind is None and self._running:
            self._running = False
            self._log_totals()

    def _log_totals(self):
        totals = self.snapshot()
        if not totals:
            _logger.info("Reagent used this run: none drawn.")
            return
        parts = []
        for port in sorted(totals):
            name = self._valves.port_to_reagent(port)
            label = f"port {port} ({name})" if name else f"port {port}"
            parts.append(f"{label}: {totals[port]:.0f} uL")
        _logger.info("Reagent used this run: %s.", "; ".join(parts))
