# tests/unit/control/test_syringe_pump_lock.py
"""Every touch of the Tecan driver runs under the pump's serial lock -- and
no wait holds it. (Why the lock exists: the comment on _serial_lock in
SyringePump.__init__.)

Pinned deterministically: a spy lock stands in for the real one, and a driver
fake asserts the lock is held at every entry point -- so a method that
touches self.syringe outside the lock fails here, not on a rig. Enforcement
is per-table-row: a new driver-touching method must be added to the
parametrize below.
"""

import pytest


from .pump_helpers import ScriptedSyringe, bare_pump, halt_on_cancel


class SpyLock:
    """Context-manager stand-in for the serial lock, recording held-ness."""

    def __init__(self):
        self.held = 0

    def __enter__(self):
        self.held += 1
        return self

    def __exit__(self, *exc):
        self.held -= 1
        return False


class AssertingSyringe(ScriptedSyringe):
    """The scripted driver fake, failing the test if any call -- or a read of
    its chain state -- arrives outside the serial lock. Every public entry
    point is wrapped on the way out of attribute lookup, so a new driver call
    the pump learns to make is checked without being listed here."""

    def __init__(self, lock):
        super().__init__()
        self.lock = lock
        self.calls = []

    def _entry(self, name):
        assert self.lock.held, f"{name} reached the driver outside the serial lock"
        self.calls.append(name)

    def __getattribute__(self, name):
        attr = object.__getattribute__(self, name)
        if name.startswith("_") and name not in ("_checkReady", "_ulToSteps") \
                or name in ("lock", "calls"):
            return attr
        if name in ("exec_time", "sim_state"):
            # Driver state, not just wire traffic: reads are part of the
            # every-touch-under-the-lock invariant too.
            self._entry(name)
            return attr
        if callable(attr):
            def under_lock(*args, **kwargs):
                self._entry(name)
                return attr(*args, **kwargs)
            return under_lock
        return attr


def locked_pump():
    lock = SpyLock()
    return bare_pump(AssertingSyringe(lock), lock=lock)


class TestEveryRoundTripIsLocked:
    """AssertingSyringe raises on any unlocked driver entry, so each of these
    only has to drive the public method and confirm the call arrived."""

    @pytest.mark.parametrize("drive,expected", [
        pytest.param(lambda p: p.get_plunger_position(), "getPlungerPos",
                     id="plunger_pos"),
        pytest.param(lambda p: p.reset_chain(), "resetChain", id="reset_chain"),
        pytest.param(lambda p: p.extract(2, 100, 12), "extract", id="extract"),
        pytest.param(lambda p: p.dispense(3, 100, 12), "dispense", id="dispense"),
        pytest.param(lambda p: p.dispense_to_waste(), "dispenseToWaste",
                     id="dispense_to_waste"),
        pytest.param(lambda p: (p.extract(2, 100, 12), p.execute()),
                     "executeChain", id="execute"),
        pytest.param(lambda p: (p.extract(2, 100, 12), p.execute()),
                     "sim_state", id="resume_target_read"),
        pytest.param(lambda p: p._move_finished(), "_checkReady",
                     id="move_finished"),
        pytest.param(halt_on_cancel, "terminateCmd", id="halt_on_cancel"),
    ])
    def test_the_call_reaches_the_driver_under_the_lock(self, drive, expected):
        pump = locked_pump()
        drive(pump)
        assert expected in pump.syringe.calls
        # And nothing is left holding it.
        assert pump._serial_lock.held == 0


class TestTheLockNeverSpansAMove:
    def test_execute_releases_the_lock_before_waiting(self):
        """Held across the wait, the lock would freeze the GUI's position
        poll for the whole move and make a cancel's halt queue behind it -- the two
        things the per-transaction design exists to keep live."""
        pump = locked_pump()
        held_during_wait = []
        pump.wait_for_stop = lambda t=0: held_during_wait.append(
            pump._serial_lock.held)
        pump.extract(2, 100, 12)
        pump.execute()
        assert held_during_wait == [0]
