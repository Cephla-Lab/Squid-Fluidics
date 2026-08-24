# tests/integration/test_cancellation.py
"""The cancellation contract the abort redesign will deliver -- written first.

Today an aborted operation returns silently: `if self.sp.is_aborted: return`
at 39 sites, so the worker cannot tell "finished" from "stopped halfway" and
learns of the abort only through its own separate event. The design
(AI-docs: 2026-08-14-abort-cancellation-design) replaces that with one
signal and an unwinding raise from inside the device call.

These tests encode that behaviour now. They are xfail(strict=True): they
document what is missing, and the moment the redesign makes them pass they
fail as XPASS, forcing the markers off in the same change. The last test is
not xfail -- it pins a property that already holds and must survive the
redesign: a flow fault is a fault, never reported as an operator abort.
"""

import pytest

from fluidics.experiment_worker import AbortRequested, ExperimentWorker
from fluidics.flow_monitor import FlowFault
from fluidics.merfish_operations import MERFISHOperations

SEQ = {"type": "flow_reagent", "fluidic_port": 1, "flow_rate": 500,
       "volume": 500}

NOT_YET = pytest.mark.xfail(
    strict=True,
    reason="operations still return silently on abort; the redesign raises")


@pytest.fixture
def rig(flow_cell_hardware):
    config, sp, sv = flow_cell_hardware
    return MERFISHOperations(config, sp, sv), sp


@NOT_YET
def test_an_abort_during_a_move_unwinds_by_raising(rig):
    """The operator presses Abort while the plunger is moving. The wait wakes
    (that part works today); the operation must then raise, not carry on to
    its next step or return as if it had finished."""
    ops, sp = rig
    original_wait = sp.wait_for_stop

    def abort_mid_move(t=0):
        sp.abort()
        return original_wait(t)

    sp.wait_for_stop = abort_mid_move
    with pytest.raises(AbortRequested):
        ops.process_sequence(SEQ)


@NOT_YET
def test_an_abort_before_the_operation_starts_raises(rig):
    """A latched abort must not let the next operation silently no-op its
    way through -- reset_chain, open the valve, queue a draw that the pump
    then refuses -- and return success."""
    ops, sp = rig
    sp.abort()
    with pytest.raises(AbortRequested):
        ops.process_sequence(SEQ)


@NOT_YET
def test_the_worker_learns_of_the_abort_from_the_operation(flow_cell_hardware):
    """No second channel: the worker must not need its own event to notice
    that the operation it called was cancelled."""
    config, sp, sv = flow_cell_hardware
    ops = MERFISHOperations(config, sp, sv)
    sp.abort()
    events = []
    worker = ExperimentWorker(ops, [dict(SEQ), dict(SEQ)], config, callbacks={
        "on_error": lambda message: events.append(("error", message)),
        "update_progress":
            lambda index, num, status: events.append(("progress", num, status)),
    })
    worker.run()
    assert ("progress", 1, "Completed") not in events
    assert any(kind == "error" and "abort" in message.lower()
               for kind, *rest in events for message in rest)


def test_a_flow_fault_is_never_reported_as_an_operator_abort():
    """Pinned before the taxonomy moves: FlowFault must stay a sibling of
    AbortRequested, never a subclass -- the worker's `except AbortRequested`
    runs first, and a subclass would be reported as 'aborted by user' with
    the diagnostic discarded."""
    assert not issubclass(FlowFault, AbortRequested)
