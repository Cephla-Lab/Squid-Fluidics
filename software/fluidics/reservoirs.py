"""What every reagent source has left, on disk (spec §2.4, D6).

One entry per reservoir with a declared volume: a bulk bottle on a valve
port (`port_25`) or a plate well (`ssct/A1`). The pump asks the ledger
before every extract it dispatches (SyringePump.draw_authorizers): the
ledger refuses a draw that would not fit, charges the one that does and
writes the file, all before the plunger moves, so a crash or a write that
fails can only leave the file reading high, never low. The key is the
routed valve port's bottle, or the sipper's current well when the sipper's
port is the one routed; a draw through the sipper's port with the tip in
no well is refused here too, the last line of the no-air rule. Restored
at bring-up entry by entry: an entry whose definition matches the file
keeps its balance; one that changed, is new, or has no file is undeclared,
and nothing tracked is drawn until its refill is declared -- a ledger
never guesses. One process at a time holds a rig's ledger (a lock file).
"""

import hashlib
import json
import logging
import os
import threading
from pathlib import Path

from .errors import (ReservoirDrawTooLarge, ReservoirInUse, ReservoirNoWell,
                     ReservoirOverdraw, ReservoirUndeclared, ReservoirWriteError)
from .files import atomic_write

_logger = logging.getLogger(__name__)

FORMAT = 2


class ReservoirLedger:
    def __init__(self, definitions, path=None, declared=True):
        self._defs = {key: {"loaded_ul": int(d["loaded_ul"]), "residual_ul": int(d["residual_ul"])}
                      for key, d in definitions.items()}
        self._dispatched = {key: 0.0 for key in self._defs}
        self._path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._lockfile = None
        self._undeclared = set() if declared else set(self._defs)
        self._sipper_port = self._extract_port = None
        self._current_port = self._current_well = None

    # --- wiring ---

    def attach(self, pump, sipper_port, extract_port, current_port, current_well):
        """Authorize the pump's draws. current_port() is the valve system's
        routed port; current_well() is the sipper's well key or None;
        sipper_port is the one port whose draws come from a well."""
        self._sipper_port, self._extract_port = sipper_port, extract_port
        self._current_port, self._current_well = current_port, current_well
        pump.draw_authorizers.append(self.authorize)

    def key_for_draw(self):
        """The reservoir the next draw comes from, by where the valve and
        the sipper are now; ReservoirNoWell for the sipper's port with the
        tip in no well."""
        port = self._current_port()
        if port == self._sipper_port:
            well = self._current_well()
            if well is None:
                raise ReservoirNoWell(f"port {port} is the sipper's and the sipper is not in a well")
            return well
        return f"port_{port}"

    def authorize(self, pump_port, volume_ul):
        """The pump's pre-dispatch call: refuse, or charge and write."""
        if pump_port != self._extract_port:
            return
        key = self.key_for_draw()
        with self._lock:
            self._check(key, volume_ul)
            if key not in self._defs:
                return
            self._dispatched[key] += volume_ul
            try:
                self._write()
            except Exception as e:
                self._dispatched[key] -= volume_ul
                raise ReservoirWriteError(
                    f"the reservoir ledger could not be written ({e}); the draw from {key} "
                    "was not started") from e
            _logger.info("%s: %.0f uL dispatched, %.0f uL left", key, volume_ul,
                         self._remaining(key))

    # --- reading ---

    @property
    def declared(self):
        return not self._undeclared

    @property
    def undeclared(self):
        with self._lock:
            return sorted(self._undeclared)

    def _remaining(self, key):
        d = self._defs[key]
        return d["loaded_ul"] - self._dispatched[key] - d["residual_ul"]

    def remaining(self, key):
        with self._lock:
            return self._remaining(key)

    def check(self, key, volume_ul):
        """Refuse before a draw that would not fit, or that no loading
        could fit; the operations ask before moving the sipper."""
        with self._lock:
            self._check(key, volume_ul)

    def _check(self, key, volume_ul):
        if key not in self._defs:
            return
        if self._undeclared:
            raise ReservoirUndeclared(
                "the reservoir ledger is undeclared for " + ", ".join(sorted(self._undeclared))
                + ": declare refills (Plate reloaded / Bottle refilled, or --refilled) first")
        d = self._defs[key]
        usable = d["loaded_ul"] - d["residual_ul"]
        if volume_ul > usable:
            raise ReservoirDrawTooLarge(key, volume_ul, usable)
        if volume_ul > self._remaining(key):
            raise ReservoirOverdraw(key, volume_ul, self._remaining(key))

    def rows(self):
        """(key, dispatched_ul, remaining_ul) for every touched reservoir."""
        with self._lock:
            return [(key, self._dispatched[key], self._remaining(key))
                    for key in sorted(self._defs) if self._dispatched[key]]

    # --- state ---

    def snapshot(self):
        with self._lock:
            return self._snapshot()

    def _snapshot(self):
        return {"format": FORMAT, "definitions": dict(self._defs),
                "reservoirs": {key: {"dispatched_ul": used}
                               for key, used in self._dispatched.items() if used},
                "undeclared": sorted(self._undeclared)}

    def restore(self, snapshot):
        """Balances and the undeclared set from a snapshot of this same
        ledger (the gate's replay copy)."""
        with self._lock:
            for key, entry in snapshot.get("reservoirs", {}).items():
                if key in self._dispatched:
                    self._dispatched[key] = float(entry["dispatched_ul"])
            self._undeclared = {key for key in snapshot.get("undeclared", []) if key in self._defs}

    def refill(self, key):
        """`all`, a reservoir name (every well of it), or one key: full
        again, and declared."""
        with self._lock:
            keys = ([k for k in self._defs if key == "all" or k == key
                     or k.startswith(key + "/")])
            if not keys:
                raise ValueError(f"nothing to refill named {key!r}")
            for k in keys:
                self._dispatched[k] = 0.0
                self._undeclared.discard(k)
            _logger.info("Refilled: %s", ", ".join(keys))
            self._write()

    # --- the disk ---

    def _write(self):
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with atomic_write(self._path) as f:
            json.dump(self._snapshot(), f, indent=1)

    @classmethod
    def load(cls, path, definitions):
        """A ledger over `definitions`, holding the lock on `path`: entries
        whose definition matches the file are restored, every other entry
        is undeclared (a missing, unreadable or corrupt file: all of them)."""
        path = Path(path)
        ledger = cls(definitions, path=path, declared=False)
        ledger._lockfile = _acquire_lock(path)
        try:
            on_disk = json.loads(path.read_text())
            if not isinstance(on_disk, dict):
                raise ValueError("not a ledger")
        except FileNotFoundError:
            _logger.warning("No reservoir ledger at %s: every reservoir is undeclared until "
                            "its refill is declared.", path)
            return ledger
        except (OSError, ValueError) as e:
            _logger.error("The reservoir ledger at %s cannot be read (%s): every reservoir is "
                          "undeclared until its refill is declared.", path, e)
            return ledger
        disk_defs = on_disk.get("definitions", {})
        disk_rows = on_disk.get("reservoirs", {})
        disk_undeclared = set(on_disk.get("undeclared", []))
        for key, d in ledger._defs.items():
            if disk_defs.get(key) == d:
                ledger._dispatched[key] = float(disk_rows.get(key, {}).get("dispatched_ul", 0))
                if key not in disk_undeclared:
                    ledger._undeclared.discard(key)
        if ledger._undeclared:
            _logger.warning("The reservoir ledger at %s does not cover %s: undeclared until "
                            "their refills are declared.", path, ", ".join(sorted(ledger._undeclared)))
        return ledger

    def close(self):
        """Release the lock; the ledger may not be used after this."""
        handle, self._lockfile = self._lockfile, None
        if handle is not None:
            _release_lock(handle)


def _acquire_lock(path):
    """An exclusive, process-wide lock on `<path>.lock`, or ReservoirInUse.
    Advisory and non-blocking, so two sessions on one rig cannot both run a
    ledger they would each believe to be the only writer."""
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise ReservoirInUse(f"the reservoir ledger {path} is in use by another process "
                             "(close that session first)") from None
    return handle


def _release_lock(handle):
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def default_ledger_path(config_path):
    """Beside the run reports, named by a hash of the config's absolute path,
    so two rigs on one machine never share a ledger."""
    from .reports import default_report_directory
    digest = hashlib.sha1(os.path.abspath(config_path).encode()).hexdigest()[:12]
    return default_report_directory().parent / "reservoirs" / f"{digest}.json"


def reservoir_definitions(config):
    """Every reservoir with a declared volume: tracked bottles by port key,
    every well of every plate by `reservoir/well`."""
    from .control.config import gantry
    from .robot.labware import PlateGeometry
    sv = config.reagent_selection.selector_valves
    defs = {}
    for key, loaded in (sv.loaded_volume_ul or {}).items():
        defs[key] = {"loaded_ul": loaded,
                     "residual_ul": (sv.residual_volume_ul or {}).get(key, 0)}
    g = gantry(config)
    if g is not None:
        for r in g.reservoirs.values():
            overrides = r.loaded_ul_by_well or {}
            for well in PlateGeometry.from_well_count(r.number_of_wells).wells():
                defs[f"{r.name}/{well}"] = {"loaded_ul": overrides.get(well, r.loaded_ul),
                                            "residual_ul": r.residual_ul}
    return defs
