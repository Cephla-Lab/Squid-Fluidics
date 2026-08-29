"""Estimate a run's time before it starts, by running it.

estimate_run_time keeps no hand-maintained copy of the timing knowledge:
it replays the sequences against a simulated rig built from the same
config -- the same operations code queues the same chains, overflow dumps
and all -- and totals what actually got queued. Hardware is never touched;
a real run is estimated on its simulated twin, in milliseconds.

The estimate is per sequence (one figure per repeat, in run order), so a
display can re-anchor at every boundary: when a sequence completes, what
remains is the sum of the not-yet-run figures, whatever the finished
ones actually took.
"""

import logging

from .control._def import CMD_SET
from .devices import build_devices, build_operations
from .errors import RunControl
from .events import PlanEntry
from .sequences import sequence_label

_logger = logging.getLogger(__name__)

# The fixed costs the recorded chains cannot carry themselves.
VALVE_MOVE_SECONDS = 2.0        # one SET_ROTARY_VALVE command, routing or homing
SET_TEMPERATURE_SECONDS = 60.0  # reaching a target depends on the rig; not replayed
FALLBACK_SEQUENCE_SECONDS = 60.0


class _MeteredRunControl(RunControl):
    """Run-level waits are metered, not slept: the drain's aspiration and the
    settle delays add their seconds and return at once. Hardware polls
    (wait/sleep) return at once and add nothing -- the simulated moves they
    wait on are counted from the chains instead."""

    def __init__(self):
        super().__init__()
        self.metered = 0.0

    def run_for(self, seconds):
        self.check()
        self.metered += seconds
        return seconds

    def wait(self, timeout):
        return super().wait(0)

    def wait_interrupted(self, timeout):
        return super().wait_interrupted(0)


def plan_run(config, sequences):
    """The run plan for `sequences` under `config`: one PlanEntry per
    sequence repeat, in run order, each priced by the replay -- the one
    expansion the worker iterates, events refer to, and displays sum.

    Never raises: a run the replay cannot price -- a sequence the simulated
    rig refuses, a config the simulation cannot build -- gets a flat
    fallback per repeat and a logged warning. The plan must not stop a
    runnable run; validation is the entry points' job, done before this.
    """
    try:
        durations = _replay(config, sequences)
    except Exception as e:
        _logger.warning("Could not price the run by replay (%s); using a "
                        "flat fallback of %.0f s per sequence.",
                        e, FALLBACK_SEQUENCE_SECONDS)
        durations = []
        for seq in sequences:
            per_repeat = (seq.get("incubation_time", 0) * 60
                          + FALLBACK_SEQUENCE_SECONDS)
            durations += [per_repeat] * seq.get("repeat", 1)
    plan = []
    position = 0
    for row, seq in enumerate(sequences):
        repeats = seq.get("repeat", 1)
        for repeat in range(1, repeats + 1):
            plan.append(PlanEntry(row, seq, repeat, repeats,
                                  sequence_label(seq), durations[position]))
            position += 1
    return tuple(plan)


def estimate_run_time(config, sequences):
    """(total_seconds, durations) for a run of `sequences` under `config`
    -- the plan's figures, for callers that only price."""
    durations = [entry.duration_seconds for entry in plan_run(config, sequences)]
    return sum(durations), durations


def _op_seconds(pump, op, held_ul):
    """(seconds, held after) for one recorded op, billed at its own speed
    code's rate. The moved volume comes from the pump's own accounting
    (_held_after), so a waste dump is counted at what is held when it runs
    -- not at zero and not at full -- with no second copy of that
    convention here."""
    after = pump._held_after([op], held_ul)
    return abs(after - held_ul) / pump.get_flow_rate(op[-1]) * 60, after


def _replay(config, sequences):
    control = _MeteredRunControl()
    # instant: the simulation paces itself to feel real -- a second per
    # command, slept for real on the paths that carry no run_control (the
    # valves' homing, the drain's blocking commands). An estimate wants the
    # accounting, not the feel.
    devices = build_devices(config, simulation=True, run_control=control,
                            instant=True)
    try:
        ops = build_operations(config, devices)
        pump = devices.syringe_pump
        sent = devices.controller.sent
        # Bring-up (the homing moves, the initial CLEAR) is not the run's
        # time: every counter starts from what the build already spent.
        held = pump.get_current_volume()
        chains_counted = 0
        commands_counted = len(sent)
        chain_seconds = 0.0
        valve_moves = 0
        fixed = 0.0
        previous = 0.0
        durations = []
        for seq in sequences:
            for _ in range(seq.get("repeat", 1)):
                if seq["type"] == "set_temperature":
                    fixed += SET_TEMPERATURE_SECONDS
                else:
                    ops.process_sequence(seq)
                fixed += seq.get("incubation_time", 0) * 60
                # Bill what this repeat queued -- and only that: the records
                # grow, the counters remember how far the last repeat read.
                for chain in pump.executed[chains_counted:]:
                    for op in chain:
                        seconds, held = _op_seconds(pump, op, held)
                        chain_seconds += seconds
                chains_counted = len(pump.executed)
                valve_moves += sum(command == CMD_SET.SET_ROTARY_VALVE
                                   for command, *_ in sent[commands_counted:])
                commands_counted = len(sent)

                total = (fixed + control.metered + chain_seconds
                         + valve_moves * VALVE_MOVE_SECONDS)
                durations.append(total - previous)
                previous = total
        return durations
    finally:
        devices.close()
