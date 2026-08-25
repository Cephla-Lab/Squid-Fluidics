# tests/integration/test_cancellation.py
"""The cancellation contract the abort redesign delivers, written first.

Today an aborted operation returns silently (`if self.sp.is_aborted: return`,
at dozens of sites), so the worker cannot tell "finished" from "stopped
halfway" and learns of the abort only through its own separate event. The
design (AI-docs: 2026-08-14-abort-cancellation-design) replaces that with one
signal and an unwinding raise from inside the device call.

The tests still marked xfail(strict=True) document what is missing; the moment
a change makes one pass it fails as XPASS, forcing the marker off in the same
change. The rest pin what already holds and must survive the redesign.
"""

import pytest

from fluidics.errors import AbortRequested
from fluidics.flow_monitor import FlowFault

from ..worker_helpers import record_run

# Every fluidic step type on each operations stack: both stacks share the
# pump's cancellation path, and every one of their exception wrappers must let
# a cancellation through.
FLUIDIC_STEPS = {
    "flow_cell": [
        {"type": "flow_reagent", "fluidic_port": 1, "flow_rate": 500, "volume": 500},
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

# Restricted to how today's gap actually fails -- "DID NOT RAISE" and a failed
# assertion -- so a fixture error or an unexpected exception from an operation
# is a real failure, not the expected one.
NOT_YET = pytest.mark.xfail(
    strict=True, raises=(AssertionError, pytest.fail.Exception),
    reason="operations still return silently on a prior abort; the redesign raises")


@pytest.fixture(params=list(SEQS))
def stack(request):
    """(ops, syringe_pump, sequence, config) for each operations stack."""
    ops, sp = request.getfixturevalue(f"{request.param}_rig")
    config = request.getfixturevalue(f"{request.param}_config")
    return ops, sp, SEQS[request.param], config


def errors_in(events):
    return [message for kind, *rest in events if kind == "error"
            for message in rest]


@pytest.mark.parametrize(
    "app, seq",
    [(app, seq) for app, steps in FLUIDIC_STEPS.items() for seq in steps],
    ids=lambda value: value["type"] if isinstance(value, dict) else value)
def test_an_abort_during_a_move_unwinds_by_raising(app, seq, request, during_move):
    """The operator presses Abort while the plunger is moving. The wait wakes,
    the device raises, and every wrapper on the way out lets it through -- the
    operation must not carry on to its next step or return as if finished."""
    ops, sp = request.getfixturevalue(f"{app}_rig")
    during_move(sp, sp.abort)
    with pytest.raises(AbortRequested):
        ops.process_sequence(seq)


@NOT_YET
def test_an_abort_before_the_operation_starts_raises(stack):
    """A latched abort must not let the next operation silently no-op its
    way through -- reset_chain, open the valve, queue a draw that the pump
    then refuses -- and return success."""
    ops, sp, seq, _ = stack
    sp.abort()
    with pytest.raises(AbortRequested):
        ops.process_sequence(seq)


@NOT_YET
def test_the_worker_learns_of_the_abort_from_the_operation(stack):
    """No second channel: the worker must not need its own event to notice
    that the operation it called was cancelled -- so it neither reports that
    sequence as completed nor starts the next one."""
    ops, sp, seq, config = stack
    sp.abort()
    events = record_run(ops, [seq, seq], config)
    assert ("progress", 1, "Completed") not in events
    assert ("progress", 2, "Started") not in events
    assert any("abort" in message.lower() for message in errors_in(events))


def test_a_flow_fault_reaches_the_operator_as_a_fault_not_an_abort(flow_cell_config):
    """Pinned before the taxonomy moves. Today this rests on FlowFault not
    deriving from AbortRequested, which the worker catches first; after the
    redesign, on the worker reporting by cause. Either way the operator must
    see the diagnosis, not 'aborted by user'."""
    class FaultingOps:
        def process_sequence(self, seq):
            raise FlowFault("inlet", expected_ul_min=500.0,
                            tolerance_fraction=0.2, measured_ul_min=12.0,
                            out_of_band_seconds=3.0, consecutive_samples=6)

    (message,) = errors_in(record_run(FaultingOps(), [SEQS["flow_cell"]],
                                      flow_cell_config))
    assert "inlet" in message and "12 µL/min" in message
    assert "abort" not in message.lower()
