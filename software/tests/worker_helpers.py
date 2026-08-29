# tests/worker_helpers.py
"""Run an ExperimentWorker synchronously and record what it publishes."""

from fluidics.events import (Incubating, PlanEntry, RunStarted,
                             SequenceCompleted, SequenceStarted)
from fluidics.experiment_worker import ExperimentWorker
from fluidics.sequences import sequence_label
from fluidics.subscribers import Subscribers


def plan_for(sequences, seconds=1.0, rows=None):
    """A plan without a rig, for tests that exercise loops and displays
    rather than the pricing: `seconds` is a flat figure or one per entry,
    and `rows` overrides the default per-sequence indices (a GUI test's
    model rows)."""
    plan = []
    for row, seq in enumerate(sequences):
        repeats = seq.get("repeat", 1)
        for repeat in range(1, repeats + 1):
            duration = (seconds[len(plan)] if isinstance(seconds, (list, tuple))
                        else seconds)
            plan.append(PlanEntry(rows[row] if rows is not None else row,
                                  seq, repeat, repeats,
                                  sequence_label(seq), duration))
    return tuple(plan)


def record_run(ops, sequences, run_control=None):
    """Construct and run, returning (worker, events): the worker (whose
    outcome/message/ended_position say how it ended) and the flat list of
    published events -- ("started",) for RunStarted, then
    ("sequence", ordinal, kind) with a 1-based ordinal for SequenceStarted
    ("started") / Incubating ("incubating") / SequenceCompleted
    ("completed") -- plus ("make_safe",) when the safety hook ran. The CLI
    runs the loop on a thread only to keep its console alive; the loop
    itself is synchronous.
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
    worker = ExperimentWorker(ops, plan_for(sequences),
                              events=channel,
                              make_safe=lambda: events.append(("make_safe",)) or [],
                              run_control=run_control)
    worker.run()
    return worker, events
