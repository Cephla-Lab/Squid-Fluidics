# tests/integration/test_time_estimate.py
"""estimate_run_time: a run estimated by replaying it against the simulated
twin, one figure per sequence.

Property-based against the fixture configs: the chains carry the timing, so
the assertions bound the figures rather than re-deriving them -- re-deriving
would just copy the tariff back out.
"""

import logging
import time

import pytest

from fluidics.control.controller import FluidControllerSimulation
from fluidics.time_estimate import (SET_TEMPERATURE_SECONDS, VALVE_MOVE_SECONDS,
                                    _chain_seconds, estimate_run_time)

from ..unit.control.pump_helpers import sim_pump
from .conftest import FLOW_CELL_STEP

DRAW_SECONDS = 500 / 500 * 60          # the step's own draw: 500 uL at 500 uL/min


class TestTheEstimate:
    def test_a_draw_is_estimated_from_its_chain_plus_the_fixed_costs(self, flow_cell_config):
        seconds, durations = estimate_run_time(flow_cell_config, [FLOW_CELL_STEP])
        assert len(durations) == 1 and sum(durations) == pytest.approx(seconds)
        assert seconds >= DRAW_SECONDS + VALVE_MOVE_SECONDS, "the draw itself is missing"
        assert seconds <= DRAW_SECONDS + 30, "more overhead than a valve turn explains"

    def test_each_repeat_gets_its_own_figure(self, flow_cell_config):
        seconds, durations = estimate_run_time(
            flow_cell_config, [dict(FLOW_CELL_STEP, repeat=2), FLOW_CELL_STEP])
        assert len(durations) == 3
        assert all(d > 0 for d in durations)
        assert sum(durations) == pytest.approx(seconds)

    def test_incubation_is_charged_to_its_own_sequence(self, flow_cell_config):
        _, plain = estimate_run_time(flow_cell_config, [FLOW_CELL_STEP, FLOW_CELL_STEP])
        _, with_incubation = estimate_run_time(
            flow_cell_config,
            [dict(FLOW_CELL_STEP, incubation_time=2), FLOW_CELL_STEP])
        assert with_incubation[0] == pytest.approx(plain[0] + 120)
        assert with_incubation[1] == pytest.approx(plain[1])

    def test_set_temperature_is_the_declared_constant(self, flow_cell_config):
        seconds, durations = estimate_run_time(
            flow_cell_config, [{"type": "set_temperature", "temperature": 37}])
        assert (seconds, durations) == (SET_TEMPERATURE_SECONDS, [SET_TEMPERATURE_SECONDS])

    def test_the_dumps_the_operations_insert_are_counted(self, open_chamber_config):
        """add_reagent dumps the syringe before drawing; a naive
        volume-over-rate figure misses every dump and turnover chain."""
        seq = {"type": "add_reagent", "fluidic_port": 3, "flow_rate": 1000,
               "volume": 1000}
        seconds, _ = estimate_run_time(open_chamber_config, [seq])
        naive = 1000 / 1000 * 60
        # The margin holds the metered 10 s drain aspirate plus the dumps and
        # turnover draws (fast: they run at the speed-code limit) and valves.
        assert seconds > naive + 10, "the dumps and the drain went uncounted"
        assert seconds < naive + 60, "more overhead than the operation explains"


class TestChainSeconds:
    def test_a_dump_is_counted_at_what_is_held_when_it_runs(self):
        """2500 uL mid-stroke, plus the 1000 just drawn: the dump moves 3500,
        at its own (limit) rate -- not zero, not the syringe's full volume."""
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
            raise AssertionError("the estimate constructed a real device")

        monkeypatch.setattr(devices_module, "FluidController", hardware)
        monkeypatch.setattr(devices_module, "SyringePump", hardware)
        seconds, _ = estimate_run_time(flow_cell_config, [FLOW_CELL_STEP])
        assert seconds > 0

    def test_it_estimates_in_milliseconds_not_run_time(self, open_chamber_config,
                                                       real_clock, monkeypatch):
        """The drain's ten-second aspiration and a 30-minute incubation are
        metered, never slept, and the pacing stays off for the *whole*
        replay -- the drain's blocking commands carry no run_control, so a
        paced replay would really sleep a second per command. Restored
        after. The controller's own sleep binding is made real here:
        real_clock restores time.sleep but not the module's from-import."""
        import time as time_module

        import fluidics.control.controller as controller_module
        monkeypatch.setattr(controller_module, "sleep", time_module.sleep)
        seq = {"type": "add_reagent", "fluidic_port": 3, "flow_rate": 1000,
               "volume": 500, "incubation_time": 30}
        started = time.monotonic()
        seconds, _ = estimate_run_time(open_chamber_config, [seq])
        assert time.monotonic() - started < 2, "the replay slept for real"
        assert seconds > 30 * 60, "the incubation went uncounted"
        assert FluidControllerSimulation.COMMAND_SECONDS == 1, "the pacing was not restored"

    def test_an_unestimatable_run_falls_back_and_never_raises(self, flow_cell_config, caplog):
        bad = dict(FLOW_CELL_STEP, fluidic_port=99)
        with caplog.at_level(logging.WARNING, logger="fluidics"):
            seconds, durations = estimate_run_time(flow_cell_config, [bad, FLOW_CELL_STEP])
        assert len(durations) == 2 and seconds > 0
        assert "Could not estimate" in caplog.text


class TestTheWorkerReportsTheEstimate:
    def test_the_figures_the_gui_hears_are_the_replayed_ones(
            self, flow_cell_config, instant_devices):
        from fluidics.devices import build_operations, build_worker
        devices = instant_devices(flow_cell_config)
        seconds, durations = estimate_run_time(flow_cell_config, [FLOW_CELL_STEP])
        heard = []
        build_worker(devices, build_operations(flow_cell_config, devices),
                     [FLOW_CELL_STEP],
                     callbacks={"on_estimate": lambda t, n, d: heard.append((t, n, d))})
        assert heard == [(seconds, 1, durations)]
