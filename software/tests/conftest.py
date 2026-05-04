import threading
import time as _time
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir():
    return FIXTURES_DIR


@pytest.fixture(autouse=True)
def _fast_clock(monkeypatch):
    """Patch time.sleep, time.time, and Event.wait so tests run instantly.

    sleep() advances the fake clock instead of blocking.
    time() returns the fake clock value, so timeouts expire immediately.
    Event.wait() advances the fake clock by the timeout instead of blocking
    (used by DiscPump.aspirate).

    Must patch both the time module AND each module that did
    'from time import sleep/time' (since those hold a direct reference).
    """
    fake_time = [_time.time()]

    def fake_sleep(seconds):
        fake_time[0] += seconds

    def fake_time_fn():
        return fake_time[0]

    def fake_event_wait(self, timeout=None):
        if timeout is not None:
            fake_time[0] += timeout
        return self.is_set()

    # Patch the time module itself
    monkeypatch.setattr("time.sleep", fake_sleep)
    monkeypatch.setattr("time.time", fake_time_fn)

    # Patch modules that use 'from time import sleep' or 'from time import time'
    monkeypatch.setattr("fluidics.merfish_operations.sleep", fake_sleep)
    monkeypatch.setattr("fluidics.open_chamber_operations.sleep", fake_sleep)
    monkeypatch.setattr("fluidics.open_chamber_operations.time", fake_time_fn, raising=False)
    monkeypatch.setattr("fluidics.control.controller.sleep", fake_sleep)
    monkeypatch.setattr("fluidics.control.controller.time", fake_time_fn)
    monkeypatch.setattr("fluidics.sequence_utils.sleep", fake_sleep, raising=False)
    monkeypatch.setattr("fluidics.sequence_utils.time", fake_time_fn, raising=False)

    # Patch threading.Event.wait (used by DiscPump.aspirate)
    monkeypatch.setattr(threading.Event, "wait", fake_event_wait)
