"""Shared sequence helpers used by both flow cell and open chamber operations."""
import logging

from time import time

from .errors import OperationError

_logger = logging.getLogger(__name__)


def set_temperature(tc, target):
    """Drive every channel on `tc` to `target` and block until all channels
    are within tolerance or the timeout fires.

    On timeout, raises OperationError so the experiment worker stops. A
    cancelled run raises its cause out of the wait. If `tc` is None, logs a
    warning and returns.
    """
    if tc is None:
        _logger.warning("No temperature controller found. Skipping temperature control sequence.")
        return

    for channel in range(1, tc.channels + 1):
        tc.set_target_temperature(channel, target)

    start_time = time()
    while True:
        tc.run_control.sleep(1)
        actuals = [tc.get_actual_temperature(c) for c in range(1, tc.channels + 1)]
        if all(abs(t - target) <= tc.tolerance_celsius for t in actuals):
            return
        if time() - start_time > tc.stabilization_timeout_seconds:
            raise OperationError(
                f"Temperature failed to stabilize within "
                f"{tc.stabilization_timeout_seconds}s "
                f"(target={target}, actual={actuals})"
            )
