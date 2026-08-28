# tests/unit/control/test_pump_channels.py
"""The pump's two channels: draws (the reagent ledger's feed) and
held_volume (what displays paint instead of polling the serial line).

Publish-and-forget by design: no subscriber, no effect -- so these tests
subscribe and pin what arrives, when."""

import pytest

from fluidics.errors import Cancelled

from .pump_helpers import sim_pump


@pytest.fixture
def pump():
    return sim_pump()


@pytest.fixture
def draws(pump):
    heard = []
    pump.draws.subscribe(lambda port, volume: heard.append((port, volume)))
    return heard


class TestDraws:
    def test_an_extract_publishes_its_port_and_volume_once(self, pump, draws):
        pump.extract(2, 750, 10)
        pump.execute()
        assert draws == [(2, 750)]

    def test_dispenses_and_dumps_publish_nothing(self, pump, draws):
        pump.dispense(1, 200, 10)
        pump.dispense_to_waste()
        pump.execute()
        assert draws == []

    def test_a_cancel_before_dispatch_charges_nothing(self, pump, draws):
        pump.extract(2, 750, 10)
        pump.run_control.cancel()
        with pytest.raises(Cancelled):
            pump.execute()
        assert draws == []

    def test_a_cancel_mid_draw_still_charges_the_full_volume(self, pump, draws,
                                                             during_move):
        """Generous on purpose: a cancelled draw moved something, and a
        total that reads slightly high beats one that silently undercounts."""
        during_move(pump, pump.run_control.cancel)
        pump.extract(2, 750, 10)
        with pytest.raises(Cancelled):
            pump.execute()
        assert draws == [(2, 750)]


class TestHeldVolume:
    def test_every_reading_is_published(self, pump):
        heard = []
        pump.held_volume.subscribe(heard.append)
        pump.extract(2, 100, 10)
        pump.execute()
        assert heard, "no reading was published for the move"
        assert heard[-1] == pump.get_current_volume()
