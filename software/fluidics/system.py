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

    def run(self, sequences, callbacks=None):
        """Start a run of `sequences`; see RunSession.start."""
        self.session.start(sequences, self.operations, callbacks)

    def run_manual(self, verb, callbacks=None):
        """Start one manual verb; see RunSession.run_manual."""
        self.session.run_manual(verb, callbacks)

    def wait(self, timeout=None):
        """Block until the current job has ended; see RunSession.wait."""
        return self.session.wait(timeout)

    # --- shutdown ---

    def close(self, timeout=10):
        """Quiesce, then release: abort whichever job is running, wait up to
        `timeout` seconds for it to unwind, then close the devices. Closing
        under a live job would park the syringe and drop the ports beneath a
        thread still driving them. Returns the exceptions the close raised,
        as DeviceSet.close does; safe to call twice.
        """
        if self.session.busy:
            what = self.session.kind
            _logger.warning("Stopping the %s before closing the devices.", what)
            self.session.abort()
            if not self.session.wait(timeout):
                _logger.error("The %s did not stop within %s s; closing the devices under it.",
                              what, timeout)
        return self.devices.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False
