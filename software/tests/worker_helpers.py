# tests/worker_helpers.py
"""Run an ExperimentWorker synchronously and record what it reports."""

from fluidics.experiment_worker import ExperimentWorker


def record_run(ops, sequences, config):
    """Construct and run, returning the flat callback record:
    ("estimate", n), ("progress", num, status), ("error", message),
    ("finished",). The CLI runs the loop on a thread only to keep its
    console alive; the loop itself is synchronous.
    """
    events = []
    worker = ExperimentWorker(ops, sequences, config, callbacks={
        "on_estimate": lambda t, n: events.append(("estimate", n)),
        "update_progress":
            lambda index, num, status: events.append(("progress", num, status)),
        "on_error": lambda message: events.append(("error", message)),
        "on_finished": lambda: events.append(("finished",)),
    })
    worker.run()
    return events
