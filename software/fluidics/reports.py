"""The written record of each run: one JSON file per run_id.

`RunReports` is a subscriber beside the usage ledger and nothing more --
no operation writes into it, so dropping it (or never building it)
changes no operations code. It pairs RunStarted's plan with RunEnded by
run_id, reads the ledger's totals at the ending, and writes what a
post-mortem or a lab notebook wants -- outcome, elapsed time, the plan
with its estimates, the sequences as run, per-port reagent use, the
run's warnings -- to `<reports directory>/<run_id>.json`, beside the
rolling log.

Everything is collected synchronously in the event dispatch, where the
facts still hold: RunEnded arrives inside the session's end transition,
and a later subscriber in the same delivery (the GUI's resume offer) may
chain the next run -- whose RunStarted resets the very ledger this
report reads. Only the disk is off-thread: the writer thread is
short-lived and non-daemon, so a dispatch never waits on a file and an
interpreter shutting down right after a run still finishes the write.

The warnings in the record are whatever the system's warnings channel
carried between the run's two boundary events. That window is the run's
by structure, not convention: the channel's one publisher today is draw
protection, which only speaks inside a run's draws (a manual move
cannot overlap a run, and publishes nothing here anyway) -- wiring a
new publisher into system.warnings widens what every report attests.

The record's schema: `format` is 1 and bumps only when an existing
field changes meaning -- additions do not. `row` is the starting
caller's own numbering for a source sequence (the GUI's tree rows, the
CLI's file order); it joins `plan` to `sequences` within one report and
means nothing beyond it -- consumers key on position and label, as
fluidics.events prescribes. run_id (UTC second + a per-process counter)
is filename-safe and unique within a process; two processes ending
same-second runs would collide on the name and the later write wins --
accepted until run identity is ever shared across processes.
"""

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

from .events import RunEnded, RunStarted, plan_seconds
from .run_log import get_default_log_directory

_logger = logging.getLogger(__name__)


def default_report_directory():
    return get_default_log_directory() / "reports"


def _utc(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


class RunReports:
    def __init__(self, run_events, usage, warnings, directory=None):
        """directory: where the reports go; None resolves to
        default_report_directory() at each write, so construction never
        touches the disk."""
        self._directory = directory
        self._usage = usage
        self._lock = threading.Lock()
        self._run = None                 # the run in flight, else None
        self._writers = []
        run_events.subscribe(self._on_event)
        warnings.subscribe(self._on_warning)

    def wait(self, timeout=None):
        """Block until every started write has finished; True when none is
        still running. For a caller about to read the files -- a test, or
        a script collecting its own record. `timeout` bounds the whole
        wait, not each write."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            writers = list(self._writers)
        for writer in writers:
            writer.join(None if deadline is None else
                        max(0.0, deadline - time.monotonic()))
        return not any(writer.is_alive() for writer in writers)

    # --- subscribers (the starter's, the job's, the sensor reader's) ---

    def _on_warning(self, message):
        with self._lock:
            if self._run is not None:
                self._run["warnings"].append(
                    {"at": _utc(time.time()), "message": message})

    def _on_event(self, event):
        if isinstance(event, RunStarted):
            with self._lock:
                self._run = {"run_id": event.run_id,
                             "started": _utc(time.time()),
                             "plan": event.plan,
                             "warnings": []}
        elif isinstance(event, RunEnded):
            self._write_off_thread(self._collect(event))

    def _collect(self, event):
        """The whole report, gathered in the dispatch."""
        with self._lock:
            # Take the stash only for its own ending: a mismatched id
            # (two starters raced; the loser announces a failed ending
            # under the winner's stash) must leave the surviving run's
            # record intact.
            if self._run is not None and self._run["run_id"] == event.run_id:
                run, self._run = self._run, None
            else:
                run = None
        if run is None:
            # An ending with no matching start -- events wired up
            # mid-run, or the raced starter above. Still worth a record.
            _logger.warning("Run %s ended with no matching start on record; "
                            "writing what the ending carries.", event.run_id)
            run = {"run_id": event.run_id, "started": None, "plan": (),
                   "warnings": []}
        plan = run["plan"]
        # One entry per source row (a resumed tail repeats rows and may
        # open mid-repeat); every repeat carries the same sequence dict.
        by_row = {entry.row: entry.sequence for entry in plan}
        at = None if event.position is None else plan[event.position]
        return {
            "format": 1,
            "run_id": event.run_id,
            "outcome": event.outcome,
            "message": event.message,
            "started": run["started"],
            "ended": _utc(time.time()),
            "elapsed_seconds": event.elapsed_seconds,
            "estimated_seconds": plan_seconds(plan),
            "sequences": [{"row": row, **seq} for row, seq in by_row.items()],
            "plan": [{"position": position, "row": entry.row,
                      "label": entry.label, "repeat": entry.repeat,
                      "repeats": entry.repeats,
                      "estimated_seconds": entry.duration_seconds}
                     for position, entry in enumerate(plan)],
            "ended_at": None if at is None else {
                "position": event.position, "row": at.row,
                "label": at.label, "repeat": at.repeat,
                "repeats": at.repeats},
            "reagent_used_ul": [
                {"port": port, "reagent": name, "volume_ul": used}
                for port, name, used in self._usage.rows()],
            "warnings": run["warnings"],
        }

    # --- the disk, off the dispatching thread ---

    def _write_off_thread(self, report):
        writer = threading.Thread(target=self._write, args=(report,),
                                  name="run-report", daemon=False)
        # Started before it is listed: wait() joins whatever it snapshots,
        # and joining a thread that never started raises.
        writer.start()
        with self._lock:
            self._writers = [w for w in self._writers if w.is_alive()]
            self._writers.append(writer)

    def path_for(self, run_id):
        """Where that run's record goes -- the one spelling of the name,
        for the writer below and any collector reading back."""
        directory = (Path(self._directory) if self._directory is not None
                     else default_report_directory())
        return directory / f"{run_id}.json"

    def _write(self, report):
        path = self.path_for(report["run_id"])
        directory = path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
            # Dump to a sibling temp file and swap it in whole: a write
            # that dies midway must not leave a truncated record at the
            # destination -- or destroy one already there.
            fd, tmp = tempfile.mkstemp(dir=directory, suffix=".part")
            try:
                with open(fd, "w", encoding="utf-8") as f:
                    # default=str: a report is a record -- an unforeseen
                    # value lands as its string, never costs the file.
                    json.dump(report, f, indent=2, default=str)
            except BaseException:
                os.unlink(tmp)
                raise
            os.replace(tmp, path)
            _logger.info("Run report: %s", path)
        except OSError:
            _logger.exception("The run report %s could not be written.", path)
