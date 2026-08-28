# tests/unit/test_usage.py
"""ReagentUsage: the pump's draws, charged to the port that was open."""

import logging
from types import SimpleNamespace

import pytest

from fluidics.subscribers import Subscribers
from fluidics.usage import ReagentUsage


@pytest.fixture
def rig():
    """The three seams the ledger touches, as fakes: the pump's draws
    channel, the valve system's current port, the session's state."""
    draws = Subscribers("draws")
    state = Subscribers("state")
    valves = SimpleNamespace(
        current=3,
        get_current_port=lambda: rig_.valves.current,
        port_to_reagent=lambda port: {3: "DAPI"}.get(port),
    )
    config = SimpleNamespace(syringe_pump=SimpleNamespace(extract_port=2))
    pump = SimpleNamespace(draws=draws)
    usage = ReagentUsage(config, pump, valves, state)
    rig_ = SimpleNamespace(draws=draws, state=state, valves=valves, usage=usage)
    return rig_


class TestTheLedger:
    def test_a_reagent_draw_is_charged_to_the_open_port(self, rig):
        rig.draws.notify(2, 500)
        rig.valves.current = 5
        rig.draws.notify(2, 250)
        rig.draws.notify(2, 250)
        assert rig.usage.snapshot() == {3: 500, 5: 500}

    def test_only_the_reagent_path_counts(self, rig):
        """A draw through any other syringe port -- a chamber draw, air --
        is not reagent consumption."""
        rig.draws.notify(1, 500)
        assert rig.usage.snapshot() == {}

    def test_a_run_start_resets_and_a_manual_start_does_not(self, rig):
        rig.draws.notify(2, 500)
        rig.state.notify("manual")
        rig.state.notify(None)
        assert rig.usage.snapshot() == {3: 500}, "a manual move erased the view"
        rig.state.notify("run")
        assert rig.usage.snapshot() == {}, "the experiment did not start clean"

    def test_a_runs_totals_reach_the_log_at_its_end(self, rig, caplog):
        rig.state.notify("run")
        rig.draws.notify(2, 1500)
        with caplog.at_level(logging.INFO, logger="fluidics.usage"):
            rig.state.notify(None)
        assert "Reagent used this run: port 3 (DAPI): 1500 uL." in caplog.text

    def test_a_manual_moves_end_logs_nothing(self, rig, caplog):
        rig.draws.notify(2, 500)
        with caplog.at_level(logging.INFO, logger="fluidics.usage"):
            rig.state.notify("manual")
            rig.state.notify(None)
        assert "Reagent used" not in caplog.text

    def test_snapshot_is_a_copy(self, rig):
        rig.draws.notify(2, 500)
        rig.usage.snapshot().clear()
        assert rig.usage.snapshot() == {3: 500}
