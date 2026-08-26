# tests/integration/test_pause.py
"""Pause end to end: a held run starts nothing new, and carries on afterwards.

Real clock, because the property is that time passes and the operation does
not proceed -- the fake clock's instant waits cannot show that. The
simulation's per-command second is switched off: what is under test is the
gate, not how long a simulated valve takes.
"""

import threading

import pytest

from fluidics.errors import AbortRequested

SEQ = {"type": "flow_reagent", "fluidic_port": 1, "flow_rate": 500, "volume": 500}


@pytest.fixture
def instant_rig(flow_cell_rig):
    """The rig with its simulated durations switched off: a valve command's
    second and the pump's five-second move estimate would otherwise be spent
    for real here, and neither is what these tests pin. The gates run
    untouched -- execute() passes its checkpoint before it waits."""
    ops, sp = flow_cell_rig
    ops.sv.fc.COMMAND_SECONDS = 0
    sp.wait_for_stop = lambda t=0: None
    return ops, sp


def run_in_background(call):
    finished = threading.Event()
    error = []

    def run():
        try:
            call()
        except BaseException as e:          # recorded, not swallowed
            error.append(e)
        finally:
            finished.set()

    threading.Thread(target=run, daemon=True).start()
    return finished, error


def test_a_paused_run_starts_nothing_new_and_resumes(instant_rig, real_clock):
    ops, sp = instant_rig
    ops.run_control.pause()

    finished, error = run_in_background(lambda: ops.process_sequence(SEQ))
    assert not finished.wait(0.05), "the operation ran through the pause"
    assert sp.executed == [], "a paused run moved liquid"

    ops.run_control.resume()
    assert finished.wait(2), "the operation did not finish after the resume"
    assert not error, error
    assert sp.executed, "the run did not carry on after the resume"


def test_an_abort_while_held_unwinds_without_a_resume(instant_rig, real_clock):
    """The operator paused, thought better of it, and pressed Abort."""
    ops, sp = instant_rig
    ops.run_control.pause()

    finished, error = run_in_background(lambda: ops.process_sequence(SEQ))
    assert not finished.wait(0.05)

    ops.run_control.cancel()
    assert finished.wait(2), "Abort while paused did not unwind the operation"
    assert isinstance(error[0], AbortRequested), error
    assert sp.executed == []
