"""MERFISHOperations with a sipper: move, then open the port, then draw."""
import pytest

from fluidics.devices import build_operations
from fluidics.errors import OperationError, ReservoirError, ReservoirNoWell, ReservoirOverdraw
from fluidics.robot.labware import WellRef


@pytest.fixture
def rig(plate_sipper_config, built, tmp_path):
    devices = built(plate_sipper_config, simulation=True, instant=True,
                    ledger_path=tmp_path / "ledger.json")
    devices.reservoirs.refill("all")
    for sensor in devices.flow_sensors:
        sensor.close()
    return devices, build_operations(plate_sipper_config, devices)


def test_a_well_row_moves_the_sipper_then_opens_its_port_then_draws(rig):
    devices, ops = rig
    order = []
    devices.robot.moves.subscribe(lambda pos: order.append(("move", pos)))
    open_port = devices.selector_valves.open_port
    devices.selector_valves.open_port = lambda port: order.append(("port", port)) or open_port(port)
    ops.process_sequence({"type": "flow_reagent", "fluidic_port": 1, "well": "A1",
                          "flow_rate": 2000, "volume": 100})
    port_at = order.index(("port", 1))
    last_move = max(i for i, (kind, _) in enumerate(order) if kind == "move")
    assert port_at > last_move, "the port opens only after the selection's last move"
    assert order[port_at - 1] == ("move", (12.0, 1.5, 50.0)), "that move ends at dip depth in A1"
    assert devices.robot.current_well == WellRef("probes", "A1")
    assert devices.reservoirs.remaining("probes/A1") == 110 - 5 - 100


def test_a_bulk_row_after_a_well_row_charges_the_bottle_not_the_well(rig):
    """Codex finding 1: the tip stays in its well while the valve moves on."""
    devices, ops = rig
    ops.process_sequence({"type": "flow_reagent", "fluidic_port": 1, "well": "ssct/A1",
                          "flow_rate": 2000, "volume": 100})
    ops.process_sequence({"type": "flow_reagent", "fluidic_port": 25,
                          "flow_rate": 2000, "volume": 300})
    assert devices.reservoirs.remaining("ssct/A1") == 1650
    assert devices.reservoirs.remaining("port_25") == 480000 - 300


def test_dips_run_before_the_draw_while_the_well_is_full(rig):
    devices, ops = rig
    events = []
    devices.robot.moves.subscribe(lambda pos: events.append(("z", pos[2])))
    devices.syringe_pump.draws.subscribe(lambda port, ul: events.append(("draw", ul)))
    ops.process_sequence({"type": "flow_reagent", "fluidic_port": 1, "well": "ssct/A1",
                          "flow_rate": 5000, "volume": 1000, "dip_cycles": 2})
    zs = [z for kind, z in events if kind == "z"]
    assert zs[-5:] == [52.0, 47.0, 52.0, 47.0, 52.0]      # dip depth, up 5 mm, back, twice
    last_z = max(i for i, (kind, _) in enumerate(events) if kind == "z")
    assert events.index(("draw", 1000)) > last_z, "the draw starts only after the last dip ends"
    assert devices.robot.current_well == WellRef("ssct", "A1")


def test_an_overdraw_is_refused_before_the_tip_moves(rig):
    devices, ops = rig
    hyb = {"type": "flow_reagent", "fluidic_port": 1, "well": "A1", "flow_rate": 2000, "volume": 100}
    ops.process_sequence(hyb)                      # 5 uL usable left in A1
    moved = []
    devices.robot.moves.subscribe(moved.append)
    with pytest.raises(ReservoirOverdraw, match="probes/A1"):
        ops.process_sequence(hyb)
    assert moved == []
    with pytest.raises(ReservoirError, match="split"):
        ops.process_sequence(dict(hyb, well="A2", volume=1000))
    assert moved == []


def test_a_bottle_overdraw_is_refused_at_the_pump_before_it_moves(rig):
    """Codex finding 2: bulk draws are gated too."""
    devices, ops = rig
    with pytest.raises(ReservoirError, match="port_25"):
        ops.process_sequence({"type": "flow_reagent", "fluidic_port": 25,
                              "flow_rate": 5000, "volume": 480001})
    drawn = [op for op in devices.syringe_pump.executed_ops if op[0] == "extract"]
    assert drawn == [], "the syringe may have emptied to waste first; it drew nothing"


def test_priming_parks_the_sipper_in_the_priming_well_for_its_port(rig):
    devices, ops = rig
    seen = []
    devices.syringe_pump.draws.subscribe(lambda port, ul: seen.append(devices.robot.current_well))
    ops.process_sequence({"type": "priming", "fluidic_port": 25, "flow_rate": 5000, "volume": 800})
    assert WellRef("ssct", "H11") in seen
    assert devices.reservoirs.remaining("ssct/H11") == 1750 - 250   # the sipper port's tip-to-valve volume
    assert devices.reservoirs.remaining("port_25") == 480000 - 700 - 800


def test_the_pump_refuses_the_sipper_port_with_the_tip_in_no_well(rig):
    devices, ops = rig
    devices.selector_valves.open_port(1)
    devices.robot.retract()
    devices.syringe_pump.reset_chain()
    devices.syringe_pump.extract(2, 100, 5)
    with pytest.raises(ReservoirNoWell):
        devices.syringe_pump.execute()


def test_a_stage_fault_becomes_an_operation_error_until_reset(rig):
    devices, ops = rig
    devices.robot._stage._service.fault("Z3", after_polls=2, error_code=0x2310)
    with pytest.raises(OperationError, match="Z3"):
        ops.process_sequence({"type": "flow_reagent", "fluidic_port": 1, "well": "A1",
                              "flow_rate": 2000, "volume": 100})
    assert devices.robot.current_well is None
    devices.robot._stage.reset_faults()            # the Robot tab's verb; the rig closes clean
