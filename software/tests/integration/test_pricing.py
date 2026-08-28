# tests/integration/test_pricing.py
"""price_run: a run priced by replaying it against the simulated twin.

Property-based against the fixture configs: the chains carry the timing, so
the assertions bound the bill rather than re-deriving it -- re-deriving would
just copy the tariff back out.
"""

import time

import pytest

from fluidics.control.controller import FluidControllerSimulation
from fluidics.pricing import (SET_TEMPERATURE_SECONDS, VALVE_MOVE_SECONDS,
                              price_run)

from .conftest import FLOW_CELL_STEP

DRAW_SECONDS = 500 / 500 * 60          # the step's own draw: 500 uL at 500 uL/min


class TestTheBill:
    def test_a_draw_is_priced_from_its_chain_plus_the_tariff(self, flow_cell_config):
        seconds, n = price_run(flow_cell_config, [FLOW_CELL_STEP])
        assert n == 1
        assert seconds >= DRAW_SECONDS + VALVE_MOVE_SECONDS, "the draw itself is missing"
        assert seconds <= DRAW_SECONDS + 30, "more overhead than a valve turn explains"

    def test_repeats_are_replayed_not_multiplied(self, flow_cell_config):
        one, _ = price_run(flow_cell_config, [FLOW_CELL_STEP])
        two, n = price_run(flow_cell_config, [dict(FLOW_CELL_STEP, repeat=2)])
        assert n == 2
        assert two > one * 1.5, "the second repeat priced as free"

    def test_incubation_is_charged_per_repeat(self, flow_cell_config):
        base, _ = price_run(flow_cell_config, [FLOW_CELL_STEP])
        with_incubation, _ = price_run(
            flow_cell_config, [dict(FLOW_CELL_STEP, incubation_time=2)])
        assert with_incubation == pytest.approx(base + 120)

    def test_set_temperature_is_the_declared_constant(self, flow_cell_config):
        seconds, n = price_run(flow_cell_config,
                               [{"type": "set_temperature", "temperature": 37}])
        assert (seconds, n) == (SET_TEMPERATURE_SECONDS, 1)

    def test_the_dumps_the_operations_insert_are_billed(self, open_chamber_config):
        """add_reagent dumps the syringe before drawing; a naive
        volume-over-rate price misses every dump and turnover chain."""
        seq = {"type": "add_reagent", "fluidic_port": 3, "flow_rate": 1000,
               "volume": 1000}
        seconds, _ = price_run(open_chamber_config, [seq])
        naive = 1000 / 1000 * 60
        # The margin holds the metered 10 s drain aspirate plus the dumps and
        # turnover draws (fast: they run at the speed-code limit) and valves.
        assert seconds > naive + 10, "the dumps and the drain went unbilled"
        assert seconds < naive + 60, "more overhead than the operation explains"


class TestChainSeconds:
    def test_a_dump_is_priced_at_what_is_held_when_it_runs(self):
        """2500 uL mid-stroke, plus the 1000 just drawn: the dump moves 3500,
        at its own (limit) rate -- not zero, not the syringe's full volume."""
        from fluidics.pricing import _chain_seconds
        from ..unit.control.pump_helpers import sim_pump
        pump = sim_pump()
        chains = [[("extract", 2, 1000, 40), ("dispense_to_waste", 10)]]
        expected = (1000 / pump.get_flow_rate(40) * 60
                    + 3500 / pump.get_flow_rate(10) * 60)
        assert _chain_seconds(chains, pump) == pytest.approx(expected)


class TestTheReplayIsSafe:
    def test_hardware_is_never_touched_even_for_a_real_run(self, flow_cell_config,
                                                           monkeypatch):
        import fluidics.devices as devices_module

        def hardware(*args, **kwargs):
            raise AssertionError("pricing constructed a real device")

        monkeypatch.setattr(devices_module, "FluidController", hardware)
        monkeypatch.setattr(devices_module, "SyringePump", hardware)
        seconds, _ = price_run(flow_cell_config, [FLOW_CELL_STEP])
        assert seconds > 0

    def test_it_prices_in_milliseconds_not_run_time(self, open_chamber_config, real_clock):
        """The drain's ten-second aspiration and a 30-minute incubation are
        metered, never slept; the simulation's pacing is switched off for
        the replay and restored after."""
        seq = {"type": "add_reagent", "fluidic_port": 3, "flow_rate": 1000,
               "volume": 500, "incubation_time": 30}
        started = time.monotonic()
        seconds, _ = price_run(open_chamber_config, [seq])
        assert time.monotonic() - started < 2, "the replay slept for real"
        assert seconds > 30 * 60, "the incubation went unbilled"
        assert FluidControllerSimulation.COMMAND_SECONDS == 1, "the pacing was not restored"

    def test_an_unpriceable_run_falls_back_and_never_raises(self, flow_cell_config, caplog):
        bad = dict(FLOW_CELL_STEP, fluidic_port=99)
        import logging
        with caplog.at_level(logging.WARNING, logger="fluidics"):
            seconds, n = price_run(flow_cell_config, [bad, FLOW_CELL_STEP])
        assert n == 2 and seconds > 0
        assert "Could not price" in caplog.text


class TestTheWorkerReportsThePrice:
    def test_the_estimate_the_gui_hears_is_the_replayed_price(
            self, flow_cell_config, instant_devices):
        from fluidics.devices import build_operations, build_worker
        devices = instant_devices(flow_cell_config)
        priced, _ = price_run(flow_cell_config, [FLOW_CELL_STEP])
        heard = []
        build_worker(devices, build_operations(flow_cell_config, devices),
                     [FLOW_CELL_STEP],
                     callbacks={"on_estimate": lambda t, n: heard.append((t, n))})
        assert heard == [(priced, 1)]
