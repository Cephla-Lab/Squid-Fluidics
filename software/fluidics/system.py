"""The rig as one object: the devices, the application's operations, the
manual verbs, and the one job on the rig, built from a config and closed
once.

    with FluidicsSystem.build(config, simulation=True) as system:
        system.manual.open_port(3)
        system.run(sequences, callbacks={"on_error": print})
        system.wait()
"""

import logging

from .devices import build_devices, build_operations
from .manual_operations import ManualOperations
from .run_session import RunSession
from .subscribers import Subscribers
from .time_estimate import estimate_run_time

_logger = logging.getLogger(__name__)


class FluidicsSystem:
    def __init__(self, config, devices):
        """Assemble the rig around a DeviceSet already brought up; build()
        is the usual way, doing the bring-up too."""
        self.devices = devices
        # Draw-protection notices from the operations (Flow Cell), for
        # whoever is watching -- the run log always is.
        self.warnings = Subscribers("fluidics warnings")
        self.warnings.subscribe(_logger.warning)
        self.operations = build_operations(config, devices,
                                           on_warning=self.warnings.notify)
        self.manual = ManualOperations(devices)
        self.session = RunSession(devices)

    @classmethod
    def build(cls, config, simulation=False, on_issue=None):
        """Bring the rig up for `config` and assemble it. What build_devices
        reports through on_issue (a degraded bring-up) passes straight
        through; what it raises (no controller, no pump) raises."""
        return cls(config, build_devices(config, simulation, on_issue=on_issue))

    # --- the one job ---

    def estimate(self, sequences):
        """(total_seconds, durations) for a run of `sequences` on this rig:
        what a confirm dialog shows, ready to hand back to run() so the run
        reports the same figures. See time_estimate.estimate_run_time."""
        return estimate_run_time(self.devices.config, sequences)

    def run(self, sequences, callbacks=None, durations=None):
        """Start a run of `sequences`; see RunSession.start. `durations`
        from estimate() rides along so the run is not priced twice."""
        self.session.start(sequences, self.operations, callbacks, durations)

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
