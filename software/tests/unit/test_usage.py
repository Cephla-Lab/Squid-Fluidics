# tests/unit/test_usage.py
"""ReagentUsage: the pump's draws, charged to the port that was open."""

import logging
from types import SimpleNamespace

import pytest

from fluidics.events import RunEnded, RunStarted
from fluidics.subscribers import Subscribers
from fluidics.usage import ReagentUsage


@pytest.fixture
def rig():
    """The three seams the ledger touches, as fakes: the pump's draws
    channel, the valve system's current port, the run events channel."""
    draws = Subscribers("draws")
    events = Subscribers("events")
    valves = SimpleNamespace(
        current=3,
        port_to_reagent=lambda port: {3: "DAPI"}.get(port),
    )
    valves.get_current_port = lambda: valves.current
    config = SimpleNamespace(syringe_pump=SimpleNamespace(extract_port=2))
    pump = SimpleNamespace(draws=draws)
    usage = ReagentUsage(config, pump, valves, events)

    def started():
        events.notify(RunStarted("run-1", ()))

    def ended():
        events.notify(RunEnded("run-1", "finished", None, 0.0, None))

    return SimpleNamespace(draws=draws, run_started=started, run_ended=ended,
                           valves=valves, usage=usage)


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

    def test_a_run_start_resets_the_view(self, rig):
        rig.draws.notify(2, 500)     # a manual draw between runs stays visible
        assert rig.usage.snapshot() == {3: 500}
        rig.run_started()
        assert rig.usage.snapshot() == {}, "the experiment did not start clean"

    def test_a_runs_totals_reach_the_log_at_its_end_with_its_id(self, rig, caplog):
        rig.run_started()
        rig.draws.notify(2, 1500)
        with caplog.at_level(logging.INFO, logger="fluidics.usage"):
            rig.run_ended()
        assert "Reagent used (run-1): port 3 (DAPI): 1500 uL." in caplog.text

    def test_snapshot_is_a_copy(self, rig):
        rig.draws.notify(2, 500)
        rig.usage.snapshot().clear()
        assert rig.usage.snapshot() == {3: 500}
