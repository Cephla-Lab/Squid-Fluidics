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
                                    _op_seconds, estimate_run_time)

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

    def test_a_repeat_is_not_billed_for_the_chains_before_it(self, flow_cell_config):
        """The replay's counters advance with the record: each figure carries
        only what its own repeat queued, not a re-count of everything so
        far. (The last repeat can only be cheaper than the first -- the
        valve is already routed.)"""
        _, durations = estimate_run_time(
            flow_cell_config, [dict(FLOW_CELL_STEP, repeat=3)])
        assert durations[2] <= durations[0], "an earlier repeat's chains were re-billed"
        assert durations[2] >= DRAW_SECONDS

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


class TestOpSeconds:
    """The billing of one recorded op reads the pump's own accounting
    (_held_after); these pin the convention that accounting carries."""

    def test_a_dump_is_counted_at_what_is_held_when_it_runs(self):
        """3500 uL held when the dump runs: it moves 3500, at its own
        (limit) rate -- not zero, not the syringe's full volume."""
        pump = sim_pump()
        seconds, held = _op_seconds(pump, ("dispense_to_waste", 10), 3500)
        assert held == 0
        assert seconds == pytest.approx(3500 / pump.get_flow_rate(10) * 60)

    def test_an_extract_is_billed_at_its_own_volume_and_rate(self):
        pump = sim_pump()
        seconds, held = _op_seconds(pump, ("extract", 2, 1000, 40), 2500)
        assert held == 3500
        assert seconds == pytest.approx(1000 / pump.get_flow_rate(40) * 60)


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
                                                       real_clock):
        """The drain's ten-second aspiration and a 30-minute incubation are
        metered, never slept, and the replay's own rig is built instant --
        the drain's blocking commands carry no run_control, so a paced rig
        would really sleep a second per command. Its own rig only: the
        class-level pacing every other simulated rig shares is untouched."""
        seq = {"type": "add_reagent", "fluidic_port": 3, "flow_rate": 1000,
               "volume": 500, "incubation_time": 30}
        started = time.monotonic()
        seconds, _ = estimate_run_time(open_chamber_config, [seq])
        assert time.monotonic() - started < 2, "the replay slept for real"
        assert seconds > 30 * 60, "the incubation went uncounted"
        assert FluidControllerSimulation.COMMAND_SECONDS == 1, \
            "the estimate touched the pacing every simulated rig shares"

    def test_an_unestimatable_run_falls_back_and_never_raises(self, flow_cell_config, caplog):
        bad = dict(FLOW_CELL_STEP, fluidic_port=99)
        with caplog.at_level(logging.WARNING, logger="fluidics"):
            seconds, durations = estimate_run_time(flow_cell_config, [bad, FLOW_CELL_STEP])
        assert len(durations) == 2 and seconds > 0
        assert "Could not estimate" in caplog.text


class TestTheRunCarriesTheEstimate:
    def test_the_figures_the_gui_hears_are_the_ones_it_handed_in(
            self, flow_cell_config, instant_devices):
        """The GUI prices its confirm dialog and hands the same figures to
        the run; the worker relays them verbatim."""
        from fluidics.devices import build_operations, build_worker
        devices = instant_devices(flow_cell_config)
        _, durations = estimate_run_time(flow_cell_config, [FLOW_CELL_STEP])
        heard = []
        build_worker(devices, build_operations(flow_cell_config, devices),
                     [FLOW_CELL_STEP], callbacks={"on_estimate": heard.append},
                     durations=durations)
        assert heard == [durations]

    def test_a_run_started_without_figures_estimates_its_own(
            self, flow_cell_config, instant_devices):
        """The session is the one place every run passes through: a caller
        with no estimate in hand (the CLI) still gets the replayed figures,
        not zeros."""
        from fluidics.devices import build_operations
        from fluidics.run_session import RunSession
        devices = instant_devices(flow_cell_config)
        session = RunSession(devices)
        _, durations = estimate_run_time(flow_cell_config, [FLOW_CELL_STEP])
        heard = []
        session.start([FLOW_CELL_STEP], build_operations(flow_cell_config, devices),
                      callbacks={"on_estimate": heard.append})
        assert session.wait()
        assert heard == [durations]
