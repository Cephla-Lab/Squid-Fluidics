# tests/unit/control/pump_helpers.py
"""Construct a real SyringePump with the hardware left out of its __init__.

Shared by the interrupt and lock tests, which each bring their own driver
fake but need the same scaffold. One helper rather than one per file, so a
new attribute added to SyringePump.__init__ is added here once instead of
failing each __new__ site with an unrelated-looking AttributeError -- adding
_serial_lock did exactly that to the interrupt tests.
"""

import threading

import pytest

from fluidics.control.syringe_pump import SyringePump
from fluidics.errors import AbortRequested


def bare_pump(syringe, lock=None, **attrs):
    pump = SyringePump.__new__(SyringePump)
    pump.syringe = syringe
    pump._serial_lock = lock if lock is not None else threading.Lock()
    for name, value in attrs.items():
        setattr(pump, name, value)
    pump._init_run_control()
    return pump


def halt_on_cancel(pump):
    """Cancel, then wake the wait: the thread inside it halts the plunger --
    a terminateCmd round trip like any other -- and raises the cause."""
    pump.run_control.cancel()
    with pytest.raises(AbortRequested):
        pump.wait_for_stop(0)
