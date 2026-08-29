"""The run's boundary events, and the plan they refer to.

One channel (`RunSession.events`) carries typed facts about run
boundaries; continuous state (paused, at rest, the clock) stays with
`RunSession.snapshot()`. Consumers -- the GUI, the CLI's exit code, the
usage ledger -- subscribe to the one channel instead of each holding its
own callback wiring.

The plan is the one expansion of "each sequence × its repeats": the
estimate produces it, the worker iterates it, and events refer to its
entries by position -- so the count the display shows, the durations the
countdown re-anchors on, and the rows the highlight lands on cannot
drift apart, because they are the same object. `row` is the entry's
index in the sequence list the plan was built from -- for a GUI run
that is the *filtered* selection, not the tree: a consumer that owns a
larger list (the GUI's model, with unchecked rows) must translate
through its own snapshot of which rows ran before touching it.
"""

from collections import namedtuple

PlanEntry = namedtuple(
    "PlanEntry", "row sequence repeat repeats label duration_seconds")

# Published by the worker as the run goes; `position` indexes the plan.
RunStarted = namedtuple("RunStarted", "run_id plan")
SequenceStarted = namedtuple("SequenceStarted", "run_id position")
Incubating = namedtuple("Incubating", "run_id position minutes")
SequenceCompleted = namedtuple("SequenceCompleted", "run_id position")

# Published by the session once the rig is free (the same deferral the
# early-end report has always had), so a dialog painted on it sees an idle
# rig. outcome is one of "finished" | "stopped" | "failed"; position is the
# plan entry that was in flight when the run ended early (None when it
# finished) -- the resume offer's input. Deliberately carries no usage
# totals: consumers read system.usage, which the session does not know.
RunEnded = namedtuple(
    "RunEnded", "run_id outcome message elapsed_seconds position")
