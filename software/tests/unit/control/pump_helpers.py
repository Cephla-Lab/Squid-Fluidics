# tests/unit/control/pump_helpers.py
"""Construct a real SyringePump with the hardware left out of its __init__.

Shared by the interrupt and lock tests, which each bring their own driver
fake but need the same scaffold. One helper rather than one per file, so a
new attribute added to SyringePump.__init__ is added here once instead of
failing each __new__ site with an unrelated-looking AttributeError -- adding
_serial_lock did exactly that to the interrupt tests.
"""

import threading

from fluidics.control.syringe_pump import SyringePump


def bare_pump(syringe, lock=None, **attrs):
    pump = SyringePump.__new__(SyringePump)
    pump.syringe = syringe
    pump._serial_lock = lock if lock is not None else threading.Lock()
    for name, value in attrs.items():
        setattr(pump, name, value)
    pump._init_interrupt()
    return pump
