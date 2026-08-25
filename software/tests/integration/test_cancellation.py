# tests/integration/test_cancellation.py
"""The cancellation contract, delivered.

An aborted operation used to return silently (`if self.sp.is_aborted:
return`, at 38 sites), so the worker could not tell "finished" from "stopped
halfway" and learned of the abort only through its own separate event. Now
there is one signal, and the raise comes from inside the device call (design:
AI-docs 2026-08-14-abort-cancellation-design). These tests were written first,
as xfail(strict=True), and lost their markers in the PR that made them pass.
"""

import pytest

from fluidics.errors import AbortRequested
from fluidics.flow_monitor import FlowFault
from fluidics.sequences import APPLICATION_SEQUENCES

from ..worker_helpers import errors_in, record_run

# Every fluidic step type on each operations stack: both stacks share the
# pump's cancellation path, and every one of their exception wrappers must let
# a cancellation through.
FLUIDIC_STEPS = {
    "flow_cell": [
        {"type": "flow_reagent", "fluidic_port": 1, "flow_rate": 500, "volume": 500},
        # More than the half-full 5000 uL fixture syringe holds, so the draw
        # first empties it to waste -- the one nested wrapper, which a cancel
        # unwinds through twice.
        {"name": "flow_reagent-full-syringe", "type": "flow_reagent",
         "fluidic_port": 1, "flow_rate": 500, "volume": 3000},
        {"type": "priming", "fluidic_port": 1, "flow_rate": 500, "volume": 500},
        {"type": "clean_up", "fluidic_port": 1, "flow_rate": 500, "volume": 500},
    ],
    "open_chamber": [
        {"type": "add_reagent", "fluidic_port": 3, "flow_rate": 1000, "volume": 1000},
        {"type": "clear_and_add_reagent", "fluidic_port": 3, "flow_rate": 1000, "volume": 1000},
        {"type": "wash_constant_flow", "fluidic_port": 6, "flow_rate": 1000, "volume": 1000},
        {"type": "priming", "fluidic_port": 10, "flow_rate": 1000, "volume": 1000},
        {"type": "clean_up", "fluidic_port": 10, "flow_rate": 1000, "volume": 1000},
    ],
}
# One representative step per stack, for the tests that need only one.
SEQS = {app: steps[0] for app, steps in FLUIDIC_STEPS.items()}

APPLICATION_NAMES = {"flow_cell": "Flow Cell", "open_chamber": "Open Chamber"}


@pytest.mark.parametrize("app", list(FLUIDIC_STEPS))
def test_the_step_table_covers_every_fluidic_type_of_the_application(app):
    """A fluidic type added to the application without a row here would be
    a wrapper nobody polices."""
    covered = {seq["type"] for seq in FLUIDIC_STEPS[app]}
    assert covered == set(APPLICATION_SEQUENCES[APPLICATION_NAMES[app]]) - {"set_temperature"}


@pytest.fixture(params=list(SEQS))
def stack(request):
    """(ops, syringe_pump, sequence, config) for each operations stack."""
    ops, sp = request.getfixturevalue(f"{request.param}_rig")
    config = request.getfixturevalue(f"{request.param}_config")
    return ops, sp, SEQS[request.param], config


@pytest.mark.parametrize(
    "app, seq",
    [(app, seq) for app, steps in FLUIDIC_STEPS.items() for seq in steps],
    ids=lambda value: value.get("name", value["type"]) if isinstance(value, dict) else value)
def test_an_abort_during_a_move_unwinds_by_raising(app, seq, request, during_move):
    """The operator presses Abort while the plunger is moving. The wait wakes,
    the device raises, and every wrapper on the way out lets it through -- the
    operation must not carry on to its next step or return as if finished."""
    ops, sp = request.getfixturevalue(f"{app}_rig")
    during_move(sp, sp.run_control.cancel)
    with pytest.raises(AbortRequested):
        ops.process_sequence(seq)


def test_an_abort_before_the_operation_starts_raises(stack):
    """A latched abort must not let the next operation silently no-op its
    way through -- nor move a valve on a run that is over: the operation
    checks the signal before it touches anything."""
    ops, sp, seq, _ = stack
    # Spy on the real open_port rather than replacing it: the valve system's
    # own entry check is what refuses, so a stub would bypass the thing under
    # test and record a move that production would not make.
    moved = []
    original = ops.sv.open_port
    ops.sv.open_port = lambda port: (original(port), moved.append(port))
    sp.run_control.cancel()
    with pytest.raises(AbortRequested):
        ops.process_sequence(seq)
    assert moved == []


def test_the_worker_learns_of_the_abort_from_the_operation(stack):
    """No second channel: the worker must not need its own event to notice
    that the operation it called was cancelled -- so it neither reports that
    sequence as completed nor starts the next one."""
    ops, sp, seq, config = stack
    sp.run_control.cancel()
    events = record_run(ops, [seq, seq], config)
    assert ("progress", 1, "Completed") not in events
    assert ("progress", 2, "Started") not in events
    assert any("abort" in message.lower() for message in errors_in(events))


def test_a_flow_fault_reaches_the_operator_as_a_fault_not_an_abort(flow_cell_config):
    """The worker reports by cause: a fault carries its diagnosis, and the
    operator never sees 'aborted by user' for something they did not do."""
    class FaultingOps:
        def process_sequence(self, seq):
            raise FlowFault("inlet", expected_ul_min=500.0,
                            tolerance_fraction=0.2, measured_ul_min=12.0,
                            out_of_band_seconds=3.0, consecutive_samples=6)

    (message,) = errors_in(record_run(FaultingOps(), [SEQS["flow_cell"]],
                                      flow_cell_config))
    assert "inlet" in message and "12 µL/min" in message
    assert "abort" not in message.lower()
