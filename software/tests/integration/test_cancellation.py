# tests/integration/test_cancellation.py
"""The cancellation contract the abort redesign will deliver -- written first.

Today an aborted operation returns silently (`if self.sp.is_aborted: return`,
at dozens of sites), so the worker cannot tell "finished" from "stopped
halfway" and learns of the abort only through its own separate event. The
design (AI-docs: 2026-08-14-abort-cancellation-design) replaces that with one
signal and an unwinding raise from inside the device call.

These tests encode that behaviour now. They are xfail(strict=True): they
document what is missing, and the moment the redesign makes them pass they
fail as XPASS, forcing the markers off in the same change. The last test is
not xfail -- it pins behaviour that already holds and must survive the
redesign: a flow fault reaches the operator as a fault, never as an abort.
"""

import pytest

from fluidics.experiment_worker import AbortRequested
from fluidics.flow_monitor import FlowFault

from ..worker_helpers import record_run

SEQ = {"type": "flow_reagent", "fluidic_port": 1, "flow_rate": 500,
       "volume": 500}

NOT_YET = pytest.mark.xfail(
    strict=True,
    reason="operations still return silently on abort; the redesign raises")


def errors_in(events):
    return [message for kind, *rest in events if kind == "error"
            for message in rest]


@NOT_YET
def test_an_abort_during_a_move_unwinds_by_raising(flow_cell_rig, during_move):
    """The operator presses Abort while the plunger is moving. The wait wakes
    (that part works today); the operation must then raise, not carry on to
    its next step or return as if it had finished."""
    ops, sp = flow_cell_rig
    during_move(sp, sp.abort)
    with pytest.raises(AbortRequested):
        ops.process_sequence(SEQ)


@NOT_YET
def test_an_abort_before_the_operation_starts_raises(flow_cell_rig):
    """A latched abort must not let the next operation silently no-op its
    way through -- reset_chain, open the valve, queue a draw that the pump
    then refuses -- and return success."""
    ops, sp = flow_cell_rig
    sp.abort()
    with pytest.raises(AbortRequested):
        ops.process_sequence(SEQ)


@NOT_YET
def test_the_worker_learns_of_the_abort_from_the_operation(flow_cell_rig):
    """No second channel: the worker must not need its own event to notice
    that the operation it called was cancelled -- so it neither reports that
    sequence as completed nor starts the next one."""
    ops, sp = flow_cell_rig
    sp.abort()
    events = record_run(ops, [SEQ, SEQ], ops.config)
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

    (message,) = errors_in(record_run(FaultingOps(), [SEQ], flow_cell_config))
    assert "inlet" in message and "12 µL/min" in message
    assert "abort" not in message.lower()
