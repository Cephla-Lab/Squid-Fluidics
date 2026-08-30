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
drift apart, because they are the same object. `row` is the caller's
identity for the entry's source sequence: plan_run sets it to the
sequence's index in the list it was handed, and a caller whose own list
is larger may relabel before starting the run -- the GUI remaps rows to
its model (which holds unchecked rows too) so highlight and resume land
on the right tree row. Anyone else -- a report, a log -- keys on
`position` or `label`, never `row`: it is the caller's coordinate, and
a record that carries it carries the starter's own numbering, meaning
nothing to anyone else.
"""

from collections import namedtuple

PlanEntry = namedtuple(
    "PlanEntry", "row sequence repeat repeats label duration_seconds")

# Published by the worker as the run goes; `position` indexes the plan.
RunStarted = namedtuple("RunStarted", "run_id plan")
SequenceStarted = namedtuple("SequenceStarted", "run_id position")
Incubating = namedtuple("Incubating", "run_id position minutes")
SequenceCompleted = namedtuple("SequenceCompleted", "run_id position")

# Published by the session with the rig already reading idle -- but from
# inside the end's one transition, before the state channel announces it,
# so a chained start can never precede the ending it follows. A subscriber
# therefore must be brief and must not block on the session (its waiters
# wake only after this dispatch; session.wait() here deadlocks). outcome is
# one of "finished" | "stopped" | "failed"; position is the plan entry in
# flight when the run ended early (None when it finished) -- the resume
# offer's input; elapsed_seconds is the run's running time, held spans
# excluded -- captured by the worker because the signal's clock is reset
# before this is published, so no subscriber could derive it. Deliberately
# carries no usage totals: consumers read system.usage, which the session
# does not know.
RunEnded = namedtuple(
    "RunEnded", "run_id outcome message elapsed_seconds position")


def plan_seconds(entries):
    """The summed estimate of `entries` -- the whole plan, or its tail."""
    return sum(entry.duration_seconds for entry in entries)


def repeat_suffix(entry):
    """`", repeat k/n"` when the entry is one repeat of several, else "".
    The one spelling for naming an entry's repeat to the operator -- the
    worker's log tag and the GUI's resume offer must say the same thing."""
    return (f", repeat {entry.repeat}/{entry.repeats}"
            if entry.repeats > 1 else "")
