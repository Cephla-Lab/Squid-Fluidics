"""Every reagent draw, counted once; what every declared reservoir has
left, on disk (spec §2.4, D6).

The volume tracker is built on every rig, declared volumes or not, and is
the one tally of the pump's draws: the pump asks it before every extract it
dispatches (SyringePump.draw_authorizers), and only draws through the
syringe's reagent path (the configured extract port) count -- a chamber
draw, air and every dispense are not reagent consumption. The key is the
routed valve port's bottle (`port_25`), or the sipper's current well
(`ssct/A1`) when the sipper's port is the one routed; a draw through the
sipper's port with the tip in no well is refused here, the last line of
the no-air rule. The join of the pump's volume with the valves' port is
sound by structure, not convention: the pump asks synchronously on the
job thread, the only thread that moves valves, and the session admits one
job at a time, so the port cannot change between the question and the
dispatch. One charge keeps two figures: what each port has drawn since
the run started, a well's draw under the sipper's port (`used_rows`: the
report's per-port block and the run-end log line; zeroed when a run
starts, so a manual draw between runs stays in view until then), and what
each reservoir has given since its last reset -- for a tracked one the
file's own row, for an untracked bottle a figure held in memory alone.
`balances` reads the second per reservoir, beside what is loaded and left
where a volume is declared: the Reservoirs window's one read, and `rows`
is its tracked part, the report's and the gate's.

A declared volume -- a key under `loaded_volume_ul`, or every well of a
gantry's plates -- adds only a limit. For such a tracked key the volume
tracker refuses a draw that would not fit, charges the one that does and
writes the file, all before the plunger moves, so a crash or a write that
fails can only leave the file reading high, never low; an untracked key
is counted, never refused and never written. Tracked entries are restored
at bring-up entry by entry: an entry whose definition matches the file
keeps its balance; one that changed, is new, or has no file is undeclared,
and nothing tracked is drawn until its refill is declared -- volume
tracking never guesses. One process at a time holds a rig's volume
tracking file (a lock file), where there is a file at all: build_devices
decides that, and gives a rig that tracks nothing an in-memory volume
tracker.
"""

import hashlib
import json
import logging
import math
import numbers
import os
import threading
from contextlib import contextmanager, nullcontext
from pathlib import Path

from .control.config import gantry, port_key, port_of
from .errors import (ReservoirDrawTooLarge, ReservoirError, ReservoirInUse, ReservoirNoWell,
                     ReservoirOverdraw, ReservoirUndeclared, ReservoirWriteError)
from .events import RunEnded, RunStarted
from .files import atomic_write
from .robot.labware import G96, WellRef, plate_of, ref_of, well_key
from .run_log import get_default_log_directory

_logger = logging.getLogger(__name__)

FORMAT = 2


def _no_name(key):
    """What a volume tracker attached without the valves' or the plates'
    names calls every reservoir."""
    return None


class VolumeTracker:
    def __init__(self, definitions, path=None, declared=True):
        self._defs = {key: {"loaded_ul": int(d["loaded_ul"])} for key, d in definitions.items()}
        # reservoir key -> uL given since its last reset: every tracked key,
        # and an untracked bottle from its first draw (never on the file).
        self._dispatched = {key: 0.0 for key in self._defs}
        self._used_run = {}          # routed port -> uL drawn since the run started
        self._path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._lockfile = None
        self._undeclared = set() if declared else set(self._defs)
        self._sipper_port = self._extract_port = None
        self._current_port = self._current_well = None
        self._port_to_reagent = self._well_name = _no_name

    # --- wiring ---

    def attach(self, pump, sipper_port, extract_port, current_port, current_well,
               port_to_reagent=_no_name, well_name=_no_name):
        """Authorize and count the pump's draws. current_port() is the valve
        system's routed port; current_well() is the sipper's well key or
        None; sipper_port is the one port whose draws come from a well;
        port_to_reagent(port) is the valves' name for a port and
        well_name(key) the config's label for a well, either None, read
        by used_rows and balances."""
        self._sipper_port, self._extract_port = sipper_port, extract_port
        self._current_port, self._current_well = current_port, current_well
        self._port_to_reagent, self._well_name = port_to_reagent, well_name
        pump.draw_authorizers.append(self.authorize)

    def _route(self):
        """(key, the routed port) for the next draw, from one read of the
        valve and the sipper, so the key and the port it is counted under
        cannot disagree; ReservoirNoWell for the sipper's port with the tip
        in no well."""
        port = self._current_port()
        if port == self._sipper_port:
            well = self._current_well()
            if well is None:
                raise ReservoirNoWell(f"port {port} is the sipper's and the sipper is not in a well")
            return well, port
        return port_key(port), port

    def authorize(self, pump_port, volume_ul):
        """The pump's pre-dispatch call: refuse, or count the draw under
        its port -- and, for a tracked key, charge and write first."""
        if pump_port != self._extract_port:
            return
        key, port = self._route()
        if port == self._sipper_port and key not in self._defs:
            # Untracked is for bulk bottles. Every well of every plate is
            # defined, so a well the volume tracker does not know would be
            # a guess.
            raise ReservoirUndeclared(
                f"{key}: the sipper is in a well volume tracking does not define; "
                "the draw was not started")
        with self._lock:
            self._check(key, volume_ul)
            if volume_ul == 0:
                return      # no draw at all: nothing to count, charge or write
            if key in self._defs:
                with self._committed(f"the draw from {key} was not started"):
                    self._dispatched[key] += volume_ul
                _logger.info("%s: %.0f uL dispatched, %.0f uL left", key, volume_ul,
                             self._remaining(key))
            else:
                self._dispatched[key] = self._dispatched.get(key, 0.0) + volume_ul
            # Counted last, once the draw is authorized: a refused draw, a
            # write that failed included, leaves the per-run figure as it
            # was, with nothing to roll back. A well draw is counted under
            # the sipper's port, the port the valves name.
            self._used_run[port] = self._used_run.get(port, 0.0) + volume_ul

    # --- reading ---

    @property
    def tracked(self):
        """The keys with a declared volume, in key order; empty where the
        volume tracker is the tally alone and has nothing to refuse or
        declare."""
        return tuple(sorted(self._defs))

    @property
    def declared(self):
        return not self._undeclared

    @property
    def undeclared(self):
        with self._lock:
            return sorted(self._undeclared)

    def _remaining(self, key):
        return self._defs[key]["loaded_ul"] - self._dispatched[key]

    def remaining(self, key):
        with self._lock:
            return self._remaining(key)

    def loaded(self, key):
        """What tracked `key` holds when loaded: the most any one draw
        from it can take. The definitions are fixed at construction, so no
        lock."""
        return self._defs[key]["loaded_ul"]

    def check(self, key, volume_ul):
        """Refuse before a draw that would not fit, or that no loading
        could fit; the operations ask before moving the sipper."""
        with self._lock:
            self._check(key, volume_ul)

    def _check(self, key, volume_ul):
        # First, for every key: NaN fails every comparison, so it would pass
        # every check below and, charged, every check on every draw after
        # it -- and counted under an untracked port, poison that figure too.
        if (isinstance(volume_ul, bool) or not isinstance(volume_ul, numbers.Real)
                or not 0 <= volume_ul < math.inf):
            raise ReservoirError(f"{key}: {volume_ul!r} uL is not a volume that can be drawn "
                                 "(a finite number, 0 or more)")
        if key not in self._defs:
            return      # untracked: counted, never limited
        if volume_ul == 0:
            return      # not a draw: no liquid moves, so there is nothing to refuse
        if self._undeclared:
            raise ReservoirUndeclared(undeclared_message(self._undeclared, self._defs))
        loaded = self.loaded(key)
        if volume_ul > loaded:
            raise ReservoirDrawTooLarge(key, volume_ul, loaded)
        if volume_ul > self._remaining(key):
            raise ReservoirOverdraw(key, volume_ul, self._remaining(key), loaded)

    def rows(self):
        """(key, dispatched_ul, remaining_ul) for every tracked reservoir
        that has given anything since its last reset, in balances()' order:
        its rows filtered, not a second reading, so the report, the gate's
        notice and the Reservoirs window cannot disagree."""
        return [(key, accumulated, current)
                for key, _, accumulated, current, loaded, _ in self.balances()
                if loaded is not None and accumulated]

    def used_rows(self):
        """(port, reagent name or None, uL) per port drawn from since the
        run started, in port order (every well's draws under the sipper's
        port): the report's and the run-end log line's view. The names are
        read fresh here, so a rename shows on the next paint."""
        with self._lock:
            used = dict(self._used_run)
        return [(port, self._port_to_reagent(port), used[port]) for port in sorted(used)]

    def balances(self):
        """(key, reagent, accumulated_ul, current_ul or None, loaded_ul or
        None, undeclared) for every tracked reservoir and every untracked
        bottle drawn from since launch or its last reset: the Reservoirs
        window's one read. Accumulated is what the reservoir has given
        since its last reset; current is what is left of it and loaded its
        declared volume, both None for an untracked bottle, which declares
        none; undeclared is whether its volume is yet to be declared (a run
        that draws on anything tracked is refused until then). The bottles
        come first by port number, then the wells by plate and in the
        plate's own order. A bottle's reagent is the valves' name for its
        port; a well's is the config's label for it, else the sipper port's
        name -- read fresh, as used_rows reads them."""
        with self._lock:
            keys = sorted((key for key, ul in self._dispatched.items() if key in self._defs or ul),
                          key=_order)
            rows = [(key, self._dispatched[key],
                     *((self._remaining(key), self._defs[key]["loaded_ul"]) if key in self._defs
                       else (None, None)),
                     key in self._undeclared)
                    for key in keys]
        return [(key, self._reagent(key), *figures) for key, *figures in rows]

    def _reagent(self, key):
        if plate_of(key) is None:
            return self._port_to_reagent(port_of(key))
        name = self._well_name(key)
        return name if name is not None else self._port_to_reagent(self._sipper_port)

    # --- the run's boundaries (RunSession.events; the system subscribes) ---

    def on_run_event(self, event):
        """The per-run figure follows the run: it starts over at RunStarted
        ("from the beginning of each experiment") and goes to the log at
        RunEnded, so the record exists even when nothing was watching. A
        resumed run (the resume offer re-runs the tail as a fresh start)
        deliberately counts as a new experiment: the interrupted run's
        figures were logged at its end, and cross-run accumulation, if ever
        wanted, belongs with run identity."""
        if isinstance(event, RunStarted):
            # RunStarted fires from the worker's constructor, on the
            # starter's thread before the run's thread exists, so the reset
            # cannot race the run's first draw.
            with self._lock:
                self._used_run = {}
        elif isinstance(event, RunEnded):
            self._log_used(event.run_id)

    def _log_used(self, run_id):
        rows = self.used_rows()
        if not rows:
            _logger.info("Reagent used (%s): none drawn.", run_id)
            return
        parts = [(f"port {port} ({name})" if name else f"port {port}")
                 + f": {used:.0f} uL"
                 for port, name, used in rows]
        _logger.info("Reagent used (%s): %s.", run_id, "; ".join(parts))

    # --- state ---

    def snapshot(self):
        with self._lock:
            return self._snapshot()

    def _snapshot(self):
        return {"format": FORMAT, "definitions": dict(self._defs),
                "reservoirs": {key: {"dispatched_ul": used}
                               for key, used in self._dispatched.items() if used and key in self._defs},
                "undeclared": sorted(self._undeclared)}

    def restore(self, snapshot):
        """Balances and the undeclared set from a snapshot of this same
        volume tracker (the gate's replay copy)."""
        with self._lock:
            for key, entry in snapshot.get("reservoirs", {}).items():
                if key in self._defs:
                    self._dispatched[key] = float(entry["dispatched_ul"])
            self._undeclared = {key for key in snapshot.get("undeclared", []) if key in self._defs}

    def refill(self, *keys):
        """Reset each of `keys` -- `all`, a reservoir name (every well of
        it), or one key: its accumulated figure to 0 and, where a volume is
        declared, full again and declared, once the file says so. All of
        them in one write, or none: a reset the file cannot hold changes
        nothing (ReservoirWriteError), and one key that names nothing
        refuses them all before anything changes. An untracked bottle (one
        drawn from since launch) has nothing to fill or declare: its figure
        is zeroed in memory, with no write."""
        if not keys:
            raise ValueError("nothing to refill: name a reservoir, a plate, or all")
        with self._lock:
            resetting = []
            for key in keys:
                # A bottle key has no plate (plate_of is None), so a key that
                # is not a string -- None -- would match every bottle below.
                if not isinstance(key, str):
                    raise ValueError(f"nothing to refill named {key!r}")
                matched = [k for k in self._dispatched
                           if key == "all" or k == key or plate_of(k) == key]
                if not matched:
                    if key == "all" and not self._defs:
                        raise ValueError("this rig tracks no reservoir volumes, and no bottle "
                                         "has been drawn from since launch: nothing to reset")
                    raise ValueError(f"nothing to refill named {key!r}")
                resetting.extend(k for k in matched if k not in resetting)
            what = ", ".join(repr(key) for key in keys)
            written = (self._committed(f"the refill of {what} was not recorded")
                       if any(k in self._defs for k in resetting) else nullcontext())
            with written:
                for k in resetting:
                    self._dispatched[k] = 0.0
                    self._undeclared.discard(k)
            _logger.info("Refilled: %s", ", ".join(resetting))

    # --- the disk ---

    @contextmanager
    def _committed(self, what):
        """A change to the balances or the undeclared set that stands only
        once the file holds it: the body changes them, the file is written
        on the way out, and a write that fails puts both back and raises
        ReservoirWriteError ending in `what`. Called with _lock held."""
        balances, undeclared = dict(self._dispatched), set(self._undeclared)
        yield
        try:
            self._write()
        except Exception as e:
            self._dispatched.update(balances)
            self._undeclared = undeclared
            raise ReservoirWriteError(
                f"the volume tracking file could not be written ({e}); {what}") from e

    def _write(self):
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with atomic_write(self._path) as f:
            json.dump(self._snapshot(), f, indent=1)
            # Synced before the draw starts: a power cut after an unsynced
            # write can bring the old file back, and the file would read low.
            f.flush()
            os.fsync(f.fileno())
        _sync_directory(self._path.parent)

    @classmethod
    def load(cls, path, definitions):
        """A volume tracker over `definitions`, holding the lock on
        `path`: entries whose definition matches the file are restored,
        every other entry is undeclared (a missing, unreadable or corrupt
        file, one of the wrong shape included: all of them). Anything else
        that goes wrong releases the lock before it is raised."""
        path = Path(path)
        tracker = cls(definitions, path=path, declared=False)
        tracker._lockfile = _acquire_lock(path)
        try:
            tracker._reconcile(path)
        except BaseException:
            tracker.close()
            raise
        return tracker

    def _reconcile(self, path):
        """Restore from the file at `path` every entry it vouches for."""
        try:
            balances, declared = self._entries_from(json.loads(path.read_text()))
        except FileNotFoundError:
            _logger.warning("No volume tracking file at %s: every reservoir is undeclared "
                            "until its refill is declared.", path)
            return
        except (OSError, ValueError, TypeError) as e:
            _logger.error("The volume tracking file at %s cannot be read (%s): every "
                          "reservoir is undeclared until its refill is declared.", path, e)
            return
        self._dispatched.update(balances)
        self._undeclared -= declared
        if self._undeclared:
            _logger.warning("The volume tracking file at %s does not cover %s: undeclared "
                            "until their refills are declared.", path,
                            describe_keys(self._undeclared, self._defs))

    def _entries_from(self, on_disk):
        """(balances, declared) from a parsed volume tracking file: a
        balance for every entry whose definition matches, declared too
        unless the file has it undeclared. A balance no draw could have left (not a number
        from 0 to the loaded volume) is not restored, so that entry stays
        undeclared. ValueError for a file of the wrong shape: it is corrupt
        as a whole, and a corrupt file is not read in part."""
        if not isinstance(on_disk, dict):
            raise ValueError("not a volume tracking file")
        disk_defs = on_disk.get("definitions", {})
        disk_rows = on_disk.get("reservoirs", {})
        disk_undeclared = on_disk.get("undeclared", [])
        if not (isinstance(disk_defs, dict) and isinstance(disk_rows, dict)
                and isinstance(disk_undeclared, list)
                and all(isinstance(k, str) for k in disk_undeclared)):
            raise ValueError("not a volume tracking file")
        balances, declared = {}, set()
        for key, d in self._defs.items():
            if disk_defs.get(key) != d:
                continue
            row = disk_rows.get(key, {})
            if not isinstance(row, dict):
                raise ValueError(f"the entry for {key} is not a reservoir's row")
            used = row.get("dispatched_ul", 0)
            if isinstance(used, bool) or not isinstance(used, (int, float)):
                raise ValueError(f"the balance of {key} is not a number: {used!r}")
            # NaN fails every comparison, and so does an infinity here.
            if not 0 <= used <= d["loaded_ul"]:
                _logger.error("The volume tracking file's balance for %s (%r uL dispatched) "
                              "is not one a draw could have left: undeclared until its refill "
                              "is declared.", key, used)
                continue
            balances[key] = float(used)
            if key not in disk_undeclared:
                declared.add(key)
        return balances, declared

    def close(self):
        """Release the lock; the volume tracker may not be used after this."""
        handle, self._lockfile = self._lockfile, None
        if handle is not None:
            _release_lock(handle)


def _order(key):
    """Where a reservoir sits in balances(): the bottles by port number,
    then the wells by plate and, within one, column-major -- the plate's
    own order, the well pickers' too."""
    ref = ref_of(key)
    if ref is None:
        return (0, port_of(key) or 0, key)
    row, column = G96.index(ref.well)
    return (1, ref.reservoir, column, row)


def describe_keys(keys, defined):
    """`keys` as a message names them, in key order: a plate every one of
    whose wells is among them as "every well of <plate>", one with more
    than four of its wells among them by their count ("<n> wells of
    <plate>": a label stays a line or two on a rig of 192 wells), every
    other key as it is (a bottle, the few wells of a plate). `defined` is
    every tracked key: it says what a plate's wells are."""
    keys = set(keys)
    plates = {}
    for key in defined:
        plate = plate_of(key)
        if plate is not None:
            plates.setdefault(plate, set()).add(key)
    named = []
    for plate, wells in plates.items():
        among = wells & keys
        if among == wells:
            named.append((plate, f"every well of {plate}"))
        elif len(among) > 4:
            named.append((plate, f"{len(among)} wells of {plate}"))
        else:
            continue
        keys -= wells
    named.extend((key, key) for key in keys)
    return ", ".join(text for _, text in sorted(named))


def undeclared_message(undeclared, defined):
    """The refusal of a draw, or of a run at time zero, while anything
    tracked is undeclared."""
    return ("volume tracking is not declared for " + describe_keys(undeclared, defined)
            + ": reset them (Reservoirs… on the Run Experiments tab, or --refilled) first")


def _sync_directory(directory):
    """Make the rename itself durable (POSIX). Best effort: the file is
    already synced, and some filesystems refuse to sync a directory."""
    if os.name != "posix":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as e:
        _logger.debug("Could not sync the directory %s (%s).", directory, e)


def _acquire_lock(path):
    """An exclusive, process-wide lock on `<path>.lock`, or ReservoirInUse.
    Advisory and non-blocking, so two sessions on one rig cannot both write
    a volume tracking file each would believe it alone writes."""
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+")
    try:
        _flock(handle, acquire=True)
    except OSError:
        handle.close()
        raise ReservoirInUse(f"this rig's volume tracking file ({path}) is in use by another "
                             "process (close that session first)") from None
    return handle


def _release_lock(handle):
    try:
        _flock(handle, acquire=False)
    finally:
        handle.close()


def _flock(handle, acquire):
    """Take (without waiting) or release the one-byte advisory lock on
    `handle`, the platform's way; OSError when another process holds it."""
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), (fcntl.LOCK_EX | fcntl.LOCK_NB) if acquire else fcntl.LOCK_UN)


def default_volume_tracking_path(config_path):
    """Under the log directory, beside the run reports, named by a hash of
    the config's absolute path, so two rigs on one machine never share a
    volume tracking file."""
    digest = hashlib.sha1(os.path.abspath(config_path).encode()).hexdigest()[:12]
    return get_default_log_directory() / "reservoirs" / f"{digest}.json"


def reservoir_definitions(config):
    """Every reservoir with a declared volume: tracked bottles by port key,
    every well of every plate by its well_key (`reservoir/well`)."""
    sv = config.reagent_selection.selector_valves
    defs = {}
    for key, loaded in (sv.loaded_volume_ul or {}).items():
        defs[key] = {"loaded_ul": loaded}
    g = gantry(config)
    if g is not None:
        for r in g.reservoirs.values():
            overrides = r.loaded_ul_by_well or {}
            for well in r.geometry.wells():
                defs[well_key(WellRef(r.name, well))] = {
                    "loaded_ul": overrides.get(well, r.loaded_ul)}
    return defs
