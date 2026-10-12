"""The pump asks before it dispatches an extract (spec D6)."""
import pytest

from fluidics.control.syringe_pump import SyringePumpSimulation


@pytest.fixture
def pump():
    return SyringePumpSimulation(sn="SIM", syringe_ul=5000, speed_code_limit=10, waste_port=3)


def test_an_authorizer_that_raises_refuses_the_draw_before_anything_moved(pump):
    asked = []

    def refuse(port, volume_ul):
        asked.append((port, volume_ul))
        raise RuntimeError("no")

    pump.draw_authorizers.append(refuse)
    pump.extract(2, 500, 5)
    with pytest.raises(RuntimeError, match="no"):
        pump.execute()
    assert asked == [(2, 500)]
    assert pump.executed_ops == []


def test_an_authorizer_runs_before_the_dispatch_and_only_for_extracts(pump):
    order = []
    pump.draw_authorizers.append(lambda port, ul: order.append(("authorize", port, ul)))
    pump.draws.subscribe(lambda port, ul: order.append(("draw", port, ul)))
    pump.extract(2, 500, 5)
    pump.dispense(3, 200, 5)
    pump.execute()
    assert order == [("authorize", 2, 500), ("draw", 2, 500)]
