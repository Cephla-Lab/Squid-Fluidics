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

from fluidics.control.syringe_pump import SyringePump, SyringePumpSimulation
from fluidics.errors import AbortRequested


def bare_pump(syringe, lock=None, **attrs):
    pump = SyringePump.__new__(SyringePump)
    pump.syringe = syringe
    pump._serial_lock = lock if lock is not None else threading.Lock()
    for name, value in {"volume": 5000, "speed_code_limit": 10, "range": 3000,
                        "plunger_pos": 0.5, **attrs}.items():
        setattr(pump, name, value)
    pump._init_run_control()
    return pump





def halt_on_cancel(pump):
    """Cancel, then wake the wait: the thread inside it halts the plunger --
    a terminateCmd round trip like any other -- and raises the cause."""
    pump.run_control.cancel()
    with pytest.raises(AbortRequested):
        pump.wait_for_stop(0)


def sim_pump(**kwargs):
    """The simulated pump the control tests use: 5000 uL, speed code limit 10,
    waste on port 1."""
    return SyringePumpSimulation(sn=None, syringe_ul=5000, speed_code_limit=10,
                                 waste_port=1, **kwargs)


class ScriptedSyringe:
    """A Tecan driver fake with a plunger: enough of XCaliburD for the pump to
    build, dispatch, halt and resume moves against.

    Every dispatch is recorded in `dispatched` as the calls that built it,
    e.g. ["setSpeed(10)", "extract(2, 300)"] -- so the pump's one-op-at-a-time
    dispatch shows as one entry per op, and a resumed remainder as the
    changePort + movePlungerAbs pair. `sim_state` is kept the way the driver
    keeps it: building an op advances the predicted plunger position and
    port, which is what the pump reads its resume target from; and, as in the
    driver, it is re-synced from the last *reading* (getPlungerPos), not from
    the plunger itself -- so a pump that skips the read works from a stale
    prediction. The plunger does not move on its own: a test sets `position`
    to say where it is. `ready` is what _checkReady answers.
    """

    def __init__(self, ready=True, position=1500, syringe_ul=5000, waste_port=1):
        self.ready = ready
        self.position = position
        self.syringe_ul = syringe_ul
        self.waste_port = waste_port
        self.terminated = 0
        self.reads = 0
        self.dispatched = []
        self._last_read = position
        self.sim_state = {"plunger_pos": position, "port": 1}
        self._building = []
        self.exec_time = 0

    def _ulToSteps(self, volume_ul):
        return int(volume_ul * 3000 / self.syringe_ul)

    def getPlungerPos(self):
        self.reads += 1
        self._last_read = self.position
        return self.position

    def updateSimState(self):
        self.sim_state["plunger_pos"] = self._last_read

    def _add(self, call, seconds=0):
        self._building.append(call)
        self.exec_time += seconds

    def setSpeed(self, code):
        self._add(f"setSpeed({code})")

    def changePort(self, port):
        self.sim_state["port"] = port
        self._add(f"changePort({port})")

    def movePlungerAbs(self, position):
        self.sim_state["plunger_pos"] = position
        self._add(f"movePlungerAbs({position})", 5)

    def extract(self, port, volume):
        self.sim_state["port"] = port
        self.sim_state["plunger_pos"] += self._ulToSteps(volume)
        self._add(f"extract({port}, {volume})", 5)

    def dispense(self, port, volume):
        self.sim_state["port"] = port
        self.sim_state["plunger_pos"] -= self._ulToSteps(volume)
        self._add(f"dispense({port}, {volume})", 5)

    def dispenseToWaste(self, retain_port=False):
        self.sim_state["port"] = self.waste_port
        self.sim_state["plunger_pos"] = 0
        self._add("dispenseToWaste()", 5)

    def resetChain(self, **kwargs):
        self._building = []
        self.exec_time = 0
        self.updateSimState()

    def executeChain(self, minimal_reset=True):
        self.dispatched.append(self._building)
        self.resetChain()
        return 0

    def _checkReady(self):
        return self.ready

    def terminateCmd(self):
        self.terminated += 1
