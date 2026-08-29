"""Per-port reagent usage, tallied from what the pump actually drew.

`ReagentUsage` joins the two owners of the facts: the pump publishes every
dispatched extract on its `draws` channel (it alone knows the volume), and
the valve system knows which fluidic port is routed when the draw runs.
The join is sound by structure, not convention: the publish fires
synchronously inside the pump's execute() on the job thread -- the only
thread that moves valves, and the session admits one job at a time -- so
the port cannot change between dispatch and charge. Only draws through
the syringe's reagent path (the configured extract_port) count: chamber
draws and every dispense are not reagent consumption, and a fill-tubing
draw is charged to whichever port is open for it.

The ledger is a subscriber and nothing else: no operation records into it,
so dropping it (or never building it) changes no operations code -- and a
future duration-operated pump joins by publishing its own draws in uL,
converted by its own calibration. Totals reset when a run starts ("from
the beginning of each experiment"); manual draws between runs stay in the
ambient view until then. A resumed run (the resume offer re-runs the
tail as a fresh start()) deliberately counts as a new experiment: the
interrupted run's totals were already logged at its end, and cross-run
accumulation, if ever wanted, belongs with run identity. At a run's end
the totals go to the fluidics log, so the record exists even when
nothing was watching.
"""

import logging
import threading

from .events import RunEnded, RunStarted

_logger = logging.getLogger(__name__)


class ReagentUsage:
    def __init__(self, config, syringe_pump, selector_valves, run_events):
        self._extract_port = config.syringe_pump.extract_port
        self._valves = selector_valves
        self._lock = threading.Lock()
        self._used_ul = {}
        syringe_pump.draws.subscribe(self._on_draw)
        run_events.subscribe(self._on_event)

    def snapshot(self):
        """{fluidic port: uL drawn} since the last reset, a copy."""
        with self._lock:
            return dict(self._used_ul)

    def rows(self):
        """(port, reagent name or None, uL) per used port, in port order --
        the one place the totals meet their names (read fresh here, so a
        rename shows on the next paint)."""
        totals = self.snapshot()
        return [(port, self._valves.port_to_reagent(port), totals[port])
                for port in sorted(totals)]

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

    def _on_event(self, event):
        if isinstance(event, RunStarted):
            # RunStarted fires from the worker's constructor, on the
            # starter's thread before the run's thread exists, so the reset
            # cannot race the run's first draw.
            self.reset()
        elif isinstance(event, RunEnded):
            self._log_totals(event.run_id)

    def _log_totals(self, run_id):
        rows = self.rows()
        if not rows:
            _logger.info("Reagent used (%s): none drawn.", run_id)
            return
        parts = [(f"port {port} ({name})" if name else f"port {port}")
                 + f": {used:.0f} uL"
                 for port, name, used in rows]
        _logger.info("Reagent used (%s): %s.", run_id, "; ".join(parts))
