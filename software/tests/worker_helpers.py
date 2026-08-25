# tests/worker_helpers.py
"""Run an ExperimentWorker synchronously and record what it reports."""

from fluidics.experiment_worker import ExperimentWorker


def record_run(ops, sequences, config, run_control=None):
    """Construct and run, returning the flat callback record:
    ("estimate", n), ("progress", num, status), ("make_safe",),
    ("error", message), ("finished",). The CLI runs the loop on a thread
    only to keep its console alive; the loop itself is synchronous.
    """
    events = []
    worker = ExperimentWorker(ops, sequences, config, callbacks={
        "on_estimate": lambda t, n: events.append(("estimate", n)),
        "update_progress":
            lambda index, num, status: events.append(("progress", num, status)),
        "make_safe": lambda: events.append(("make_safe",)),
        "on_error": lambda message: events.append(("error", message)),
        "on_finished": lambda: events.append(("finished",)),
    }, run_control=run_control)
    worker.run()
    return events


def errors_in(events):
    """The on_error messages in a record_run event list."""
    return [message for kind, *rest in events if kind == "error" for message in rest]
