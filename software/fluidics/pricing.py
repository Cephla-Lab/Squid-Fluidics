"""Price a run before it starts, by running it.

The estimate used to be a second, hand-maintained copy of the timing
knowledge (+60 s per set_temperature, +2 s per fluidic step) that had
drifted from what the operations do: it ignored the syringe-emptying
dumps, the tubing-turnover chains, the priming loop's per-port draws and
the quantized speed codes. price_run keeps no copy: it replays the
sequences against a simulated rig built from the same config -- the same
operations code queues the same chains, overflow dumps and all -- and
bills what actually got queued with an explicit tariff. Hardware is
never touched; a real run is priced on its simulated twin, in
milliseconds.
"""

import logging

from .control._def import CMD_SET
from .errors import RunControl

_logger = logging.getLogger(__name__)

# The tariff: what the recorded chains cannot carry themselves.
VALVE_MOVE_SECONDS = 2.0        # one SET_ROTARY_VALVE command, routing or homing
SET_TEMPERATURE_SECONDS = 60.0  # reaching a target depends on the rig; not replayed
FALLBACK_SEQUENCE_SECONDS = 60.0


class _MeteredRunControl(RunControl):
    """Run-level waits are metered, not slept: the drain's aspiration and the
    settle delays add their seconds to the bill and return at once. Hardware
    polls (wait/sleep) return at once and bill nothing -- the simulated moves
    they wait on are billed from the chains instead."""

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


def price_run(config, sequences):
    """(total_seconds, n_sequences) for a run of `sequences` under `config`.

    Never raises: a run the replay cannot price -- a sequence the simulated
    rig refuses, a config the simulation cannot build -- gets a flat
    fallback and a logged warning. Pricing must not stop a runnable run;
    validation is the entry points' job, done before this.
    """
    n_sequences = sum(seq.get("repeat", 1) for seq in sequences)
    try:
        total = _replay(config, sequences)
    except Exception as e:
        _logger.warning("Could not price the run by replay (%s); using a flat "
                        "fallback of %.0f s per sequence.", e, FALLBACK_SEQUENCE_SECONDS)
        total = sum((seq.get("incubation_time", 0) * 60 + FALLBACK_SEQUENCE_SECONDS)
                    * seq.get("repeat", 1) for seq in sequences)
    return total, n_sequences


def _chain_seconds(executed, pump):
    """The bill for the chains the operations queued: each op at its own
    speed code's rate. The fold mirrors the simulation's held-volume
    accounting -- starting mid-stroke as it does -- so a waste dump is
    priced at what is held when it runs, not at zero and not at full."""
    held = 0.5 * pump.volume
    seconds = 0.0
    for chain in executed:
        for op in chain:
            if op[0] == "extract":
                moved = op[2]
                held += moved
            elif op[0] == "dispense":
                moved = op[2]
                held -= moved
            else:
                moved = held
                held = 0.0
            seconds += moved / pump.get_flow_rate(op[-1]) * 60
    return seconds


def _replay(config, sequences):
    # Imported here: devices imports pricing for build_worker, and the
    # replay is the only place pricing needs the factory back.
    from .control.controller import FluidControllerSimulation
    from .devices import build_devices, build_operations

    control = _MeteredRunControl()
    paced = FluidControllerSimulation.COMMAND_SECONDS
    # The simulation's pacing is for watching it run; a price should take
    # milliseconds. Restored below; nothing else runs a job meanwhile (the
    # session prices inside start(), which refuses concurrent jobs).
    FluidControllerSimulation.COMMAND_SECONDS = 0
    try:
        devices = build_devices(config, simulation=True, run_control=control)
    finally:
        FluidControllerSimulation.COMMAND_SECONDS = paced
    try:
        ops = build_operations(config, devices)
        sp = devices.syringe_pump
        valve_moves = []
        send = devices.controller.send_command

        def counting(command, *args):
            if command == CMD_SET.SET_ROTARY_VALVE:
                valve_moves.append(args)
            return send(command, *args)

        devices.controller.send_command = counting

        seconds = 0.0
        for seq in sequences:
            for _ in range(seq.get("repeat", 1)):
                if seq["type"] == "set_temperature":
                    seconds += SET_TEMPERATURE_SECONDS
                else:
                    ops.process_sequence(seq)
                seconds += seq.get("incubation_time", 0) * 60

        seconds += _chain_seconds(sp.executed, sp)
        seconds += control.metered
        seconds += len(valve_moves) * VALVE_MOVE_SECONDS
        return seconds
    finally:
        devices.close()
