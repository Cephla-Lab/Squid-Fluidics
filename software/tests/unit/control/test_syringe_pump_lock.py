# tests/unit/control/test_syringe_pump_lock.py
"""Every Tecan round trip runs under the pump's serial lock -- and no wait
holds it.

The GUI's plunger poll runs on the Qt thread while a worker drives moves on
the same half-duplex link; before the lock, an interleaved exchange surfaced
as a spurious TecanAPITimeout that the manual tab masked as "operation
complete". These tests pin the contract deterministically: a spy lock stands
in for the RLock, and a driver fake that asserts the lock is held at every
entry point -- so a future method that touches self.syringe outside the lock
fails here, not on a rig.
"""

import pytest

from fluidics.control.syringe_pump import SyringePump


class SpyLock:
    """Context-manager stand-in for the RLock, recording held-ness."""

    def __init__(self):
        self.held = 0
        self.acquisitions = 0

    def __enter__(self):
        self.held += 1
        self.acquisitions += 1
        return self

    def __exit__(self, *exc):
        self.held -= 1
        return False


class AssertingSyringe:
    """Fails the test if any driver call arrives outside the serial lock."""

    def __init__(self, lock):
        self.lock = lock
        self.calls = []
        self.exec_time = 5

    def _entry(self, name):
        assert self.lock.held, f"{name} reached the driver outside the serial lock"
        self.calls.append(name)

    def getPlungerPos(self):
        self._entry("getPlungerPos")
        return 1500

    def setSpeed(self, code):
        self._entry("setSpeed")

    def delayExec(self, ms):
        self._entry("delayExec")

    def resetChain(self):
        self._entry("resetChain")

    def extract(self, port, volume):
        self._entry("extract")

    def dispense(self, port, volume):
        self._entry("dispense")

    def dispenseToWaste(self, retain_port=False):
        self._entry("dispenseToWaste")

    def executeChain(self, minimal_reset=True):
        self._entry("executeChain")
        return 0

    def _checkReady(self):
        self._entry("_checkReady")
        return True

    def terminateCmd(self):
        self._entry("terminateCmd")


def locked_pump():
    """A real SyringePump with the hardware left out of its __init__,
    following test_syringe_pump_interrupt's pattern."""
    pump = SyringePump.__new__(SyringePump)
    pump._serial_lock = SpyLock()
    pump.syringe = AssertingSyringe(pump._serial_lock)
    pump.volume = 5000
    pump.speed_code_limit = 10
    pump.range = 3000
    pump.chained_volume = 0
    pump._init_interrupt()
    return pump


class TestEveryRoundTripIsLocked:
    """AssertingSyringe raises on any unlocked driver entry, so each of these
    only has to drive the public method and confirm the call arrived."""

    @pytest.mark.parametrize("drive,expected", [
        (lambda p: p.get_plunger_position(), "getPlungerPos"),
        (lambda p: p.set_speed(10), "setSpeed"),
        (lambda p: p.set_wait(1), "delayExec"),
        (lambda p: p.reset_chain(), "resetChain"),
        (lambda p: p.extract(2, 100, 12), "extract"),
        (lambda p: p.dispense(3, 100, 12), "dispense"),
        (lambda p: p.dispense_to_waste(), "dispenseToWaste"),
        (lambda p: p.execute(), "executeChain"),
        (lambda p: p.abort(), "terminateCmd"),
        (lambda p: p.stop(), "terminateCmd"),
    ], ids=["plunger_pos", "set_speed", "set_wait", "reset_chain", "extract",
            "dispense", "dispense_to_waste", "execute", "abort", "stop"])
    def test_the_call_reaches_the_driver_under_the_lock(self, drive, expected):
        pump = locked_pump()
        drive(pump)
        assert expected in pump.syringe.calls
        # And nothing is left holding it.
        assert pump._serial_lock.held == 0

    def test_move_finished_polls_under_the_lock(self):
        pump = locked_pump()
        assert pump._move_finished() is True
        assert "_checkReady" in pump.syringe.calls
        assert pump._serial_lock.held == 0


class TestTheLockNeverSpansAMove:
    def test_execute_releases_the_lock_before_waiting(self):
        """Held across the wait, the lock would freeze the GUI's position
        poll for the whole move and make abort() queue behind it -- the two
        things the per-transaction design exists to keep live."""
        pump = locked_pump()
        held_during_wait = []
        pump.wait_for_stop = lambda t=0: held_during_wait.append(
            pump._serial_lock.held)
        pump.execute()
        assert held_during_wait == [0]
