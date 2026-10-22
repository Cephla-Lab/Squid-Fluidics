"""The pump asks before it dispatches an extract (spec D6)."""
import json
from contextlib import closing

import pytest

from fluidics.errors import ReservoirOverdraw
from fluidics.reservoirs import ReservoirLedger

from .pump_helpers import sim_pump


@pytest.fixture
def pump():
    return sim_pump()


def test_an_authorizer_that_raises_refuses_the_draw_before_anything_moved(pump, monkeypatch):
    asked, dispatched = [], []
    start = pump._start

    def spy(op):
        dispatched.append(op)
        return start(op)

    def refuse(port, volume_ul):
        asked.append((port, volume_ul))
        raise RuntimeError("no")

    # The simulation's _start records nothing, so executed_ops alone would
    # pass with the authorizers moved after the dispatch.
    monkeypatch.setattr(pump, "_start", spy)
    pump.draw_authorizers.append(refuse)
    pump.extract(2, 500, 5)
    with pytest.raises(RuntimeError, match="no"):
        pump.execute()
    assert asked == [(2, 500)]
    assert dispatched == [], "the refused draw was dispatched"
    assert pump.executed_ops == []


def test_an_authorizer_runs_before_the_dispatch_and_only_for_extracts(pump):
    order = []
    pump.draw_authorizers.append(lambda port, ul: order.append(("authorize", port, ul)))
    pump.draws.subscribe(lambda port, ul: order.append(("draw", port, ul)))
    pump.extract(2, 500, 5)
    pump.dispense(3, 200, 5)
    pump.execute()
    assert order == [("authorize", 2, 500), ("draw", 2, 500)]


def test_an_attached_ledger_refuses_an_overdraw_before_the_pump_moves(pump, tmp_path):
    path = tmp_path / "ledger.json"
    defs = {"port_25": {"loaded_ul": 5000, "residual_ul": 200}}
    path.write_text(json.dumps({"definitions": defs,
                                "reservoirs": {"port_25": {"dispatched_ul": 4500}},
                                "undeclared": []}))
    with closing(ReservoirLedger.load(path, defs)) as ledger:
        ledger.attach(pump, sipper_port=1, extract_port=2, current_port=lambda: 25,
                      current_well=lambda: None)
        pump.extract(2, 500, 5)
        with pytest.raises(ReservoirOverdraw, match="port_25.*500.*300"):
            pump.execute()
        assert pump.executed_ops == []
        assert json.loads(path.read_text())["reservoirs"]["port_25"]["dispatched_ul"] == 4500
