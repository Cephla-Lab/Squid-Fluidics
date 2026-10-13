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


def test_a_fault_on_an_axis_that_arrived_stops_the_step_before_the_dip_and_the_draw(rig):
    """Codex finding 2: on the way to probes/H12 (75, 100.5) X arrives
    first, then faults while Y still travels. The step must fail there: the
    tip never dips, no well is set under it, the pump draws nothing. Motion
    is modelled, not instant, so the XY leg spans polls, and the stage
    polls at 0.05 s so each poll advances the suite's fake clock."""
    devices, ops = rig
    stage = devices.robot._stage
    service = stage._service
    service.instant = False
    stage._poll = 0.05
    read = service.read
    planted = []

    def read_planting_the_fault(axis):
        reading = read(axis)
        if axis == "Y2" and reading.position_mm >= 90.0 and not planted:
            planted.append(service.position_of("X1"))
            service.fault("X1", after_polls=0, error_code=0x7331)
        return reading
    service.read = read_planting_the_fault
    with pytest.raises(OperationError, match="X1.*7331"):
        ops.process_sequence({"type": "flow_reagent", "fluidic_port": 1, "well": "H12",
                              "flow_rate": 2000, "volume": 100})
    assert planted == [75.0], "X had arrived when it faulted"
    assert stage.is_retracted, "the tip stayed at travel height"
    assert devices.robot.current_well is None
    assert [op for op in devices.syringe_pump.executed_ops if op[0] == "extract"] == []
    stage.reset_faults()                           # the Robot tab's verb; the rig closes clean


SKIPS_THE_SIPPER = "use_ports leaves out port 1 (the sipper): priming and clean-up never skip the sipper"


@pytest.mark.parametrize("seq_type, use_ports, message", [
    ("priming", [25], SKIPS_THE_SIPPER),
    ("clean_up", [25], SKIPS_THE_SIPPER),
    ("priming", 25, "use_ports=25: a list of ports"),
], ids=["priming", "clean_up", "not_a_list"])
def test_selective_priming_that_leaves_out_the_sipper_is_refused_before_the_pump_moves(
        rig, seq_type, use_ports, message):
    """Codex finding 3: the sipper's port is never skipped (D7), or its
    line holds air at the first draw. A row whose use_ports leaves it out
    is refused before any pump or valve action, not given a draw from a
    well it did not list; so is a use_ports that is not a list."""
    devices, ops = rig
    before = list(devices.syringe_pump.executed_ops)
    opened = []
    open_port = devices.selector_valves.open_port
    devices.selector_valves.open_port = lambda port: opened.append(port) or open_port(port)
    with pytest.raises(OperationError) as refused:
        ops.process_sequence({"type": seq_type, "fluidic_port": 25, "flow_rate": 5000,
                              "volume": 800, "use_ports": use_ports})
    assert str(refused.value) == message
    assert devices.syringe_pump.executed_ops == before
    assert opened == []
    assert devices.reservoirs.remaining("ssct/H11") == 1750       # the priming well
    assert devices.reservoirs.remaining("ssct/H12") == 1750       # the clean-up well
    assert devices.reservoirs.remaining("port_25") == 480000


def test_selective_priming_that_lists_the_sipper_primes_it_from_the_priming_well(rig):
    devices, ops = rig
    opened = []
    open_port = devices.selector_valves.open_port
    devices.selector_valves.open_port = lambda port: opened.append(port) or open_port(port)
    ops.process_sequence({"type": "priming", "fluidic_port": 25, "flow_rate": 5000,
                          "volume": 800, "use_ports": [1, 25]})
    assert opened == [1, 25, 25], "the sipper's port, port 25, then the final draw: no other bottle"
    assert devices.reservoirs.remaining("ssct/H11") == 1750 - 250   # the sipper port's tip-to-valve volume
    assert devices.reservoirs.remaining("port_25") == 480000 - 700 - 800
