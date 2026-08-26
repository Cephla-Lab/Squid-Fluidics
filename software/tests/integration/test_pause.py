# tests/integration/test_pause.py
"""Pause end to end: a held run starts nothing new, and carries on afterwards.

Real clock, because the property is that time passes and the operation does
not proceed -- the fake clock's instant waits cannot show that. The
simulation's own durations are switched off: what is under test is the gate,
not how long a simulated valve or move takes.
"""

import pytest

from fluidics.errors import AbortRequested

from .conftest import FLOW_CELL_STEP

SEQ = FLOW_CELL_STEP


@pytest.fixture
def instant_rig(flow_cell_rig):
    """The rig with its simulated durations switched off: a valve command's
    second and the pump's five-second move estimate would otherwise be spent
    for real here. The gates run untouched -- execute() passes its checkpoint
    before it waits."""
    ops, sp = flow_cell_rig
    ops.sv.fc.COMMAND_SECONDS = 0
    sp.wait_for_stop = lambda t=0: None
    return ops, sp


def test_a_paused_run_starts_nothing_new_and_resumes(instant_rig, holds_while_paused):
    ops, sp = instant_rig

    def nothing_has_moved():
        assert sp.executed == [], "a paused run moved liquid"

    holds_while_paused(ops.run_control, lambda: ops.process_sequence(SEQ),
                       while_held=nothing_has_moved)
    assert sp.executed, "the run did not carry on after the resume"


def test_an_abort_while_held_unwinds_without_a_resume(instant_rig, real_clock,
                                                      run_in_background):
    """The operator paused, thought better of it, and pressed Abort."""
    ops, sp = instant_rig
    ops.run_control.pause()

    finished, error = run_in_background(lambda: ops.process_sequence(SEQ))
    assert not finished.wait(0.02)

    ops.run_control.cancel()
    assert finished.wait(2), "Abort while paused did not unwind the operation"
    assert isinstance(error[0], AbortRequested), error
    assert sp.executed == []
