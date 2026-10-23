# tests/unit/control/test_pump_channels.py
"""The pump's held_volume channel: what displays paint instead of polling
the serial line. (What it draws is asked of its draw_authorizers before
each dispatch: test_syringe_pump_authorize.py.)

Publish-and-forget by design: no subscriber, no effect -- so these tests
subscribe and pin what arrives, when."""

import pytest

from .pump_helpers import ScriptedSyringe, bare_pump, sim_pump


@pytest.fixture
def pump():
    return sim_pump()


class TestHeldVolume:
    def test_every_reading_is_published(self, pump):
        heard = []
        pump.held_volume.subscribe(heard.append)
        pump.extract(2, 100, 10)
        pump.execute()
        assert heard, "no reading was published for the move"
        assert heard[-1] == pump.get_current_volume()

    def test_the_real_pumps_mid_chain_readings_are_published_too(self):
        """_start and _resume read the plunger directly (under the serial
        lock); those readings must reach the channel like any other, or a
        display goes stale between the ops of a chain."""
        pump = bare_pump(ScriptedSyringe(position=1500))
        heard = []
        pump.held_volume.subscribe(heard.append)
        pump.extract(2, 300, 10)
        pump.dispense(1, 100, 10)
        pump.execute()
        # One reading per op start plus the chain-end reading.
        assert len(heard) >= 3, heard
