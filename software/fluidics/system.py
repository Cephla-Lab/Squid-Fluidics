"""The rig as one object: the devices, the application's operations, the
manual verbs, and the one job on the rig, built from a config and closed
once.

    with FluidicsSystem.build(config, simulation=True) as system:
        system.manual.open_port(3)
        system.run(sequences)      # reports on system.session.events
        system.wait()
"""

import logging

from .devices import build_devices, build_operations
from .errors import OperationError
from .manual_operations import ManualOperations
from .reports import RunReports
from .run_session import RunSession
from .sequences import validate_sequences
from .subscribers import Subscribers
from .time_estimate import check_reservoirs, plan_run

_logger = logging.getLogger(__name__)


class FluidicsSystem:
    def __init__(self, config, devices, report_dir=None):
        """Assemble the rig around a DeviceSet already brought up; build()
        is the usual way, doing the bring-up too. report_dir: where the
        per-run reports go (None: beside the rolling log)."""
        self.devices = devices
        # Draw-protection notices from the operations (Flow Cell), for
        # whoever is watching -- the run log always is.
        self.warnings = Subscribers("fluidics warnings")
        self.warnings.subscribe(_logger.warning)
        self.operations = build_operations(config, devices,
                                           on_warning=self.warnings.notify)
        self.manual = ManualOperations(devices)
        self.session = RunSession(devices)
        # The ledger's per-run figure follows the run's boundaries (zeroed
        # at RunStarted, logged at RunEnded): subscribed first, before the
        # reports and before any widget that can chain the next run from a
        # RunEnded -- the order RunSession.events states for the system's
        # bookkeeping. The ledger is built before the session exists, so
        # the wiring is here rather than in attach.
        self.session.events.subscribe(devices.reservoirs.on_run_event)
        # The written record of each run, one JSON per run_id beside the
        # rolling log; built before any widget subscribes (RunSession.events
        # states the order rule).
        self.reports = RunReports(self.session.events, devices.reservoirs,
                                  self.warnings, directory=report_dir)

    @classmethod
    def build(cls, config, simulation=False, on_issue=None, report_dir=None, ledger_path=None):
        """Bring the rig up for `config` and assemble it. What build_devices
        reports through on_issue (a degraded bring-up) passes straight
        through; what it raises (no controller, no pump) raises.
        ledger_path: where the reservoir ledger lives (None: beside the run
        reports, named after the config)."""
        return cls(config, build_devices(config, simulation, on_issue=on_issue,
                                         ledger_path=ledger_path),
                   report_dir=report_dir)

    # --- the one job ---

    def plan(self, sequences):
        """The run plan for `sequences` on this rig (one PlanEntry per
        repeat, each priced): what a confirm dialog shows, ready to hand to
        run() so the dialog and the run cannot disagree."""
        return plan_run(self.devices.config, sequences)

    def run(self, sequences, plan=None):
        """Start a run of `sequences`; see RunSession.start. The run reports
        through session.events; `plan` from plan() rides along so the run
        is not priced twice, and is what gets checked when it is there --
        it is what the session executes. Gated by validate_sequences,
        which says who else checks and why, and then, on a rig that tracks
        a volume, by the reservoir gate (check_reservoirs), before the
        session starts.
        """
        if plan is None:
            if not sequences:
                raise ValueError("there is nothing to run")
            validate_sequences(sequences, self.devices.config)
            plan = self.plan(sequences)
        else:
            # A list, not a generator: validate_sequences walks it twice.
            validate_sequences([entry.sequence for entry in plan],
                               self.devices.config)
        # "Do not lower tip" (the Robot tab's switch for checking taught
        # positions over empty plates): the sipper would never dip, so no
        # well row could draw. Refused here, before the replay and before
        # anything moves, as a FluidicsError the start slot shows.
        robot = self.devices.robot
        if robot is not None and not robot.lower_tip:
            raise OperationError("Do not lower tip is on: the sipper would travel to its wells "
                                 "without dipping, so nothing could be drawn; switch it off to run")
        # The reservoir gate (spec §7): the plan replayed on a simulated
        # twin restored from the live ledger's snapshot (the live ledger is
        # only read), so a run that would draw from an emptied well or
        # bottle -- a fresh list or a resumed tail alike -- is refused
        # here, not at the pump hours in.
        self.check_reservoirs(plan)
        self.session.start(sequences, self.operations, plan=plan)

    def check_reservoirs(self, plan):
        """The per-reservoir report for `plan`, replayed on a twin restored
        from the live ledger's snapshot, which is only read; or the refusal
        time_estimate.check_reservoirs raises. Empty, with no replay, on a
        rig that tracks no volume: its ledger is the tally alone, with
        nothing to refuse, and a step the twin cannot replay must not turn
        into a refusal there (plan_run deliberately falls back)."""
        ledger = self.devices.reservoirs
        if not ledger.tracked:
            return []
        rows = check_reservoirs(plan, self.devices.config, ledger.snapshot())
        # The general notice at the start of a run, once, at INFO: what the
        # replay says each touched reservoir has left when the run is done.
        # A refusal above is the error; nothing here is a warning.
        _logger.info("Reservoirs after this run: %s.",
                     "; ".join(f"{key} {remaining:.0f} uL left" for key, _, remaining in rows)
                     if rows else "no tracked reservoir is drawn from")
        return rows

    def refill(self, *keys):
        """Declare reservoirs full, each `all`, a plate name, or one key, in
        one write. Through the manual verb, which refuses a plate the
        sipper's tip is in."""
        self.manual.refill(*keys)

    def run_manual(self, verb, callbacks=None, source=None):
        """Start one manual verb; see RunSession.run_manual."""
        self.session.run_manual(verb, callbacks, source=source)

    def wait(self, timeout=None):
        """Block until the current job has ended; see RunSession.wait."""
        return self.session.wait(timeout)

    def abort(self):
        """Stop whichever job is running; see RunSession.abort."""
        return self.session.abort()

    def pause(self):
        return self.session.pause()

    def resume(self):
        return self.session.resume()

    @property
    def busy(self):
        return self.session.busy

    def make_safe(self):
        """Leave nothing running after an early end; see DeviceSet.make_safe.
        For a caller driving the verbs on its own thread, where close()'s
        quiesce cannot see the work -- a script's interrupt handler."""
        return self.devices.make_safe()

    # --- shutdown ---

    def close(self, timeout=10):
        """Quiesce, then release: abort whichever job is running, wait up to
        `timeout` seconds for it to unwind, then close the devices. Closing
        under a live job would park the syringe and drop the ports beneath a
        thread still driving them. Returns the exceptions the close raised,
        as DeviceSet.close does; safe to call twice.
        """
        quiescent = True
        try:
            if self.session.busy:
                what = self.session.kind
                _logger.warning("Stopping the %s before closing the devices.", what)
                self.session.abort()
                if not self.session.wait(timeout):
                    _logger.error("The %s did not stop within %s s; closing the devices under it.",
                                  what, timeout)
                    quiescent = False
        finally:
            # Unconditional: a second Ctrl+C lands in the wait above, and the
            # ports must still be released -- the exception goes on afterwards.
            # Under a job that did not stop, the devices make no moves.
            errors = self.devices.close(quiescent=quiescent and not self.session.busy)
        return errors

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False
