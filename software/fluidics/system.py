"""The rig as one object.

`FluidicsSystem` is what a script -- or the GUI -- holds: the devices, the
application's operations, the manual verbs, and the one job on the rig,
built from a config and closed once. It is assembly over the objects that
already exist (`DeviceSet`, the operations classes, `ManualOperations`,
`RunSession`); the reason it exists is that the two entry points each spelled
out the same bring-up and the same shutdown by hand.

    with FluidicsSystem.build(config, simulation=True) as system:
        system.manual.open_port(3)
        system.run(sequences, callbacks={"on_error": print})
        system.session.wait()
"""

import logging

from .devices import _print_issue, build_devices, build_operations
from .manual_operations import ManualOperations
from .run_session import RunSession
from .subscribers import Subscribers

_logger = logging.getLogger(__name__)


class FluidicsSystem:
    def __init__(self, devices, operations, manual, session, warnings):
        """Assembled; build() is the way to get one."""
        self.devices = devices
        self.operations = operations
        self.manual = manual
        self.session = session
        # Draw-protection notices from the operations (Flow Cell), for
        # whoever is watching -- the run log always is.
        self.warnings = warnings

    @classmethod
    def build(cls, config, simulation=False, on_issue=_print_issue):
        """Bring the rig up for `config` and assemble it. What build_devices
        reports through on_issue (a degraded bring-up) passes straight
        through; what it raises (no controller, no pump) raises."""
        devices = build_devices(config, simulation, on_issue=on_issue)
        warnings = Subscribers("fluidics warnings")
        warnings.subscribe(_logger.warning)
        operations = build_operations(config, devices, on_warning=warnings.notify)
        return cls(devices, operations, ManualOperations(devices), RunSession(devices), warnings)

    # --- the one job ---

    def run(self, sequences, callbacks=None):
        """Start a run of `sequences`; see RunSession.start."""
        self.session.start(sequences, self.operations, callbacks)

    def run_manual(self, verb, callbacks=None):
        """Start one manual verb; see RunSession.run_manual."""
        self.session.run_manual(verb, callbacks)

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
