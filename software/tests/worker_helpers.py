# tests/worker_helpers.py
"""Run an ExperimentWorker synchronously and record what it publishes."""

from fluidics.events import (Incubating, RunStarted, SequenceCompleted,
                             SequenceStarted)
from fluidics.experiment_worker import ExperimentWorker
from fluidics.subscribers import Subscribers
from fluidics.time_estimate import plan_run


def plan_for(sequences, config=None, seconds=1.0):
    """A plan for `sequences` without a rig: every entry priced flat --
    worker tests exercise the loop, not the estimate."""
    from fluidics.events import PlanEntry
    plan = []
    for row, seq in enumerate(sequences):
        repeats = seq.get("repeat", 1)
        for repeat in range(1, repeats + 1):
            plan.append(PlanEntry(row, seq, repeat, repeats,
                                  seq.get("name") or seq["type"], seconds))
    return tuple(plan)


def record_run(ops, sequences, config, run_control=None):
    """Construct and run, returning (worker, events): the worker (whose
    outcome/message/ended_position say how it ended) and the flat list of
    published events -- ("started",), ("sequence", position, kind) for
    SequenceStarted ("started") / Incubating ("incubating") /
    SequenceCompleted ("completed") -- plus ("make_safe",) when the safety
    hook ran. The CLI runs the loop on a thread only to keep its console
    alive; the loop itself is synchronous.
    """
    events = []
    channel = Subscribers("test run events")

    def note(event):
        if isinstance(event, RunStarted):
            events.append(("started",))
        elif isinstance(event, SequenceStarted):
            events.append(("sequence", event.position + 1, "started"))
        elif isinstance(event, Incubating):
            events.append(("sequence", event.position + 1, "incubating"))
        elif isinstance(event, SequenceCompleted):
            events.append(("sequence", event.position + 1, "completed"))

    channel.subscribe(note)
    worker = ExperimentWorker(ops, plan_for(sequences), config,
                              events=channel,
                              make_safe=lambda: events.append(("make_safe",)) or [],
                              run_control=run_control)
    worker.run()
    return worker, events
