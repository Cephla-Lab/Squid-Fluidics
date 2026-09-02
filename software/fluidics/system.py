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
from .manual_operations import ManualOperations
from .reports import RunReports
from .run_session import RunSession
from .subscribers import Subscribers
from .sequences import validate_sequences
from .time_estimate import plan_run
from .usage import ReagentUsage

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
        # Per-port reagent totals, reset at each run's start and logged at
        # its end; the GUI's table and any future API read this one object.
        self.usage = ReagentUsage(config, devices.syringe_pump,
                                  devices.selector_valves, self.session.events)
        # The written record of each run, one JSON per run_id beside the
        # rolling log; built before any widget subscribes (RunSession.events
        # states the order rule).
        self.reports = RunReports(self.session.events, self.usage,
                                  self.warnings, directory=report_dir)

    @classmethod
    def build(cls, config, simulation=False, on_issue=None, report_dir=None):
        """Bring the rig up for `config` and assemble it. What build_devices
        reports through on_issue (a degraded bring-up) passes straight
        through; what it raises (no controller, no pump) raises."""
        return cls(config, build_devices(config, simulation, on_issue=on_issue),
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
        is not priced twice.

        The time-zero gate runs here, at the one place every run passes
        through: ports the rig has, types its application offers. The GUI
        and the CLI check before calling, so they can say it their own
        way, but a caller holding this object -- a script, an embedded
        application -- had no gate at all, and a port the rig lacks then
        surfaced from SelectorValveSystem mid-experiment, hours in, which
        is the failure the check exists to prevent.
        """
        validate_sequences(self._to_run(sequences, plan), self.devices.config)
        self.session.start(sequences, self.operations, plan=plan)

    @staticmethod
    def _to_run(sequences, plan):
        """What the run will actually execute -- the plan's rows whenever
        a plan was handed in, since RunSession.start runs the plan it is
        given and reads `sequences` only to build one it was not. A
        caller passing both (the GUI does, having priced the plan from
        those very sequences) is checked on what moves the rig."""
        if plan is not None:
            return [entry.sequence for entry in plan]
        return sequences or []

    def run_manual(self, verb, callbacks=None):
        """Start one manual verb; see RunSession.run_manual."""
        self.session.run_manual(verb, callbacks)

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
        try:
            if self.session.busy:
                what = self.session.kind
                _logger.warning("Stopping the %s before closing the devices.", what)
                self.session.abort()
                if not self.session.wait(timeout):
                    _logger.error("The %s did not stop within %s s; closing the devices under it.",
                                  what, timeout)
        finally:
            # Unconditional: a second Ctrl+C lands in the wait above, and the
            # ports must still be released -- the exception goes on afterwards.
            errors = self.devices.close()
        return errors

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False
