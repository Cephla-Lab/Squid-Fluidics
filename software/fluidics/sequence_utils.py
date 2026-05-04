"""Shared sequence helpers used by both flow cell and open chamber operations."""

from time import sleep, time

from .experiment_worker import OperationError


def set_temperature(tc, target):
    """Drive every channel on `tc` to `target` and block until all channels
    are within tolerance, abort is requested, or timeout fires.

    On timeout, raises OperationError so the experiment worker stops.
    If `tc` is None, prints a warning and returns.
    """
    if tc is None:
        print("No temperature controller found. Skipping temperature control sequence.")
        return

    for channel in range(1, tc.channels + 1):
        tc.set_target_temperature(channel, target)

    start_time = time()
    while True:
        sleep(1)
        if tc.is_aborted:
            return
        actuals = [tc.get_actual_temperature(c) for c in range(1, tc.channels + 1)]
        if all(abs(t - target) <= tc.tolerance_celsius for t in actuals):
            return
        if time() - start_time > tc.stabilization_timeout_seconds:
            raise OperationError(
                f"Temperature failed to stabilize within "
                f"{tc.stabilization_timeout_seconds}s "
                f"(target={target}, actual={actuals})"
            )
