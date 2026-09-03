"""Shared sequence helpers used by both flow cell and open chamber operations."""
import logging
import time


from .errors import OperationError

_logger = logging.getLogger(__name__)


def set_temperature(tc, target, run_control):
    """Drive every channel on `tc` to `target` and block until all channels
    are within tolerance or the timeout fires.

    On timeout, raises OperationError so the experiment worker stops. A
    cancelled run raises its cause -- before the first target is written, and
    out of the wait. If `tc` is None, logs a warning and returns.
    """
    if tc is None:
        _logger.warning("No temperature controller found. Skipping temperature control sequence.")
        return

    # A run that is over sets no new target, and a paused one waits here.
    run_control.checkpoint()
    for channel in range(1, tc.channels + 1):
        tc.set_target_temperature(channel, target)
        # The setpoint alone does not drive the TEC -- the output switch is a separate
        # command -- so assert it on, or the run just waits for a temperature that never moves.
        tc.set_output_enabled(channel, True)

    # Running seconds, not wall clock: each delay() returns after one second
    # of running time, so counting them *is* the clock -- and a pause stops it.
    # Comparing wall clock here would bring a run back from a ten-minute hold
    # straight into "failed to stabilize".
    running_seconds = 0.0
    while True:
        run_control.delay(1)
        running_seconds += 1
        # The reads are running time as well: they cannot be paused, and on a
        # controller that has stopped answering they are most of the loop --
        # half a second per channel at the serial timeout. Charging only the
        # delay would stretch a 300 s timeout to 600 s on two channels.
        read_started = time.monotonic()
        actuals = [tc.get_actual_temperature(c) for c in range(1, tc.channels + 1)]
        running_seconds += time.monotonic() - read_started
        if all(abs(t - target) <= tc.tolerance_celsius for t in actuals):
            return
        if running_seconds > tc.stabilization_timeout_seconds:
            raise OperationError(
                f"Temperature failed to stabilize within "
                f"{tc.stabilization_timeout_seconds}s "
                f"(target={target}, actual={actuals})"
            )
