# tests/integration/test_manual_operations.py
"""ManualOperations: the operator's verbs, against the simulated rig.

Each verb is one move on one device, blocking, with no sequence arithmetic
around it -- what a script or the manual tab asks for is what moves.
"""

import pytest

from fluidics.errors import AbortRequested
from fluidics.manual_operations import ManualOperations


@pytest.fixture
def rig(open_chamber_config, instant_devices):
    devices = instant_devices(open_chamber_config)
    return devices, ManualOperations(devices)


@pytest.fixture
def flow_cell(flow_cell_config, instant_devices):
    devices = instant_devices(flow_cell_config)
    return devices, ManualOperations(devices)


class TestTheVerbs:
    def test_open_port_turns_the_valves(self, rig):
        devices, manual = rig
        manual.open_port(3)
        assert devices.selector_valves.get_current_port() == 3

    def test_extract_draws_exactly_what_was_asked(self, rig):
        """No tubing arithmetic, no overflow dump: 300 uL asked, 300 uL drawn,
        at the code the rate quantizes to."""
        devices, manual = rig
        sp = devices.syringe_pump
        held = sp.get_current_volume()
        manual.extract(2, 300, 500)
        code = sp.flow_rate_to_speed_code(500)
        assert sp.executed == [[("extract", 2, 300, code)]]
        assert sp.get_current_volume() == held + 300

    def test_dispense_pushes_exactly_what_was_asked(self, rig):
        devices, manual = rig
        sp = devices.syringe_pump
        held = sp.get_current_volume()
        manual.dispense(3, 200, 1000)
        assert sp.executed == [[("dispense", 3, 200, sp.flow_rate_to_speed_code(1000))]]
        assert sp.get_current_volume() == held - 200

    def test_empty_to_waste_empties_at_the_rigs_limit(self, rig):
        devices, manual = rig
        sp = devices.syringe_pump
        manual.empty_to_waste()
        assert sp.executed == [[("dispense_to_waste", sp.speed_code_limit)]]
        assert sp.get_current_volume() == 0

    def test_aspirate_runs_the_drain_for_the_time_asked(self, rig, real_clock):
        devices, manual = rig
        powered = []
        devices.disc_pump._set_power = powered.append
        manual.aspirate(0.02)
        assert powered[0] > 0 and powered[-1] == 0

    def test_aspirate_without_a_disc_pump_says_so(self, flow_cell):
        devices, manual = flow_cell
        with pytest.raises(RuntimeError, match="no disc pump"):
            manual.aspirate(1)

    def test_a_move_starts_from_an_empty_queue(self, rig):
        """A failed operation may have left ops queued; a manual move must
        not carry them along."""
        devices, manual = rig
        sp = devices.syringe_pump
        sp.extract(2, 1000, 10)          # stale, never executed
        manual.dispense(3, 200, 1000)
        assert sp.executed_ops == [("dispense", 3, 200, sp.flow_rate_to_speed_code(1000))]


class TestOnStarted:
    """What a progress bar needs: the pump's own estimate, once the move is
    queued and before it runs."""

    def test_the_estimate_arrives_before_the_move(self, rig):
        devices, manual = rig
        sp = devices.syringe_pump
        seen = []
        manual.extract(2, 300, 500,
                       on_started=lambda s: seen.append((s, list(sp.executed))))
        assert seen == [(sp.ESTIMATE_SECONDS, [])]

    def test_aspirate_reports_its_own_duration(self, rig, real_clock):
        devices, manual = rig
        devices.disc_pump._set_power = lambda power: None
        seen = []
        manual.aspirate(0.01, on_started=seen.append)
        assert seen == [0.01]


class TestWhatTheRigOffers:
    def test_flow_rates_are_the_pumps_codes_within_the_limit_fastest_first(self, rig):
        devices, manual = rig
        sp = devices.syringe_pump
        rates = manual.flow_rates()
        assert rates[0] == sp.get_flow_rate(sp.speed_code_limit)
        assert rates[-1] == sp.get_flow_rate(40)
        assert rates == sorted(rates, reverse=True)
        # Each rate round-trips to the code it came from, so the GUI can
        # offer rates and the verbs can take them.
        assert [sp.flow_rate_to_speed_code(r) for r in rates] == list(
            range(sp.speed_code_limit, 41))

    def test_the_valve_reads_come_through_the_verbs_object(self, rig):
        """The tab and a script ask ManualOperations, not a driver."""
        devices, manual = rig
        manual.open_port(3)
        assert manual.current_port() == 3
        assert manual.port_names() == devices.selector_valves.get_port_names()

    def test_held_volume_reads_the_plunger_or_the_pumps_last_reading(self, rig):
        devices, manual = rig
        sp = devices.syringe_pump
        manual.extract(2, 300, 500)
        reads = sp.get_plunger_position
        counted = []
        sp.get_plunger_position = lambda: counted.append(True) or reads()
        assert manual.held_volume_ul(refresh=False) == sp.get_current_volume()
        assert counted == [], "an idle display read the wire"
        assert manual.held_volume_ul() == sp.get_current_volume()
        assert counted == [True]


class TestTheRunsControl:
    def test_an_aborted_rig_refuses_a_manual_move(self, rig):
        """The verbs pass the same gates a run does, so the session's abort()
        stops a manual move too -- and reset() lets the next one through."""
        devices, manual = rig
        devices.run_control.cancel()
        with pytest.raises(AbortRequested):
            manual.extract(2, 300, 500)
        assert devices.syringe_pump.executed == []
        devices.run_control.reset()
        manual.extract(2, 300, 500)
        assert len(devices.syringe_pump.executed) == 1


# --- the sipper's manual verbs (spec §9, Task B7) ---

def test_a_manual_extract_through_the_sipper_needs_a_well(plate_sipper_config, built, tmp_path):
    """Review focus 4: the manual tab cannot pull air through the sipper."""
    devices = built(plate_sipper_config, simulation=True, instant=True,
                    ledger_path=tmp_path / "ledger.json")
    devices.reservoirs.refill("all")
    manual = ManualOperations(devices)
    manual.open_port(1)
    with pytest.raises(RuntimeError, match="not in a well"):
        manual.extract(2, 100, 1000)
    manual.go_to_well("ssct/A1")
    manual.extract(2, 100, 1000)            # fine with the tip in a well
    manual.retract()
    with pytest.raises(RuntimeError, match="not in a well"):
        manual.extract(2, 100, 1000)
    manual.extract(1, 100, 1000)            # another syringe port never reaches the sipper


def test_robot_verbs_refuse_on_a_rig_without_a_robot(flow_cell_config, built):
    manual = ManualOperations(built(flow_cell_config, simulation=True, instant=True))
    with pytest.raises(RuntimeError, match="no robot"):
        manual.retract()


def test_teaching_changes_the_live_placement_and_the_config_together(plate_sipper_config, built):
    """Codex finding 11: a taught A1 is where the next go-to-well goes,
    and what save_config will write."""
    from fluidics.control.config import gantry
    from fluidics.errors import StageError
    devices = built(plate_sipper_config, simulation=True, instant=True)
    manual = ManualOperations(devices)
    manual.teach_a1("probes", (13.0, 2.0))
    manual.teach_dip("probes", 49.0)
    manual.go_to_well("probes/A1")
    assert devices.robot.position() == (13.0, 2.0, 49.0)
    assert tuple(gantry(plate_sipper_config).reservoirs[1].a1_xy_mm) == (13.0, 2.0)
    with pytest.raises(StageError, match="out of reach"):
        manual.teach_a1("probes", (150.0, 2.0))          # row H would sit at x = 213
    assert tuple(gantry(plate_sipper_config).reservoirs[1].a1_xy_mm) == (13.0, 2.0), "refused: nothing changed"
    with pytest.raises(ValueError, match="agitation"):
        manual.teach_dip("probes", 4.0)
    manual.teach_travel(2.0)
    assert devices.robot._stage.travel_z_mm == 2.0
    assert gantry(plate_sipper_config).travel_z_mm == 2.0


def test_jog_refuses_xy_below_travel_height(plate_sipper_config, built):
    from fluidics.errors import StageError
    devices = built(plate_sipper_config, simulation=True, instant=True)
    manual = ManualOperations(devices)
    manual.jog("z", 10.0)
    with pytest.raises(StageError, match="travel height"):
        manual.jog("x", 1.0)


def test_teach_travel_refuses_a_height_homing_may_park_below(plate_sipper_config, built):
    """Home, then "Set travel height here", would teach the 0.1 to 0.5 mm
    the drives park at, and the next launch's homing refuses any Z that
    parks below it. Refused before the robot or the config changes."""
    from fluidics.control.config import gantry
    from fluidics.robot.stage import MIN_TRAVEL_Z_MM
    devices = built(plate_sipper_config, simulation=True, instant=True)
    manual = ManualOperations(devices)
    with pytest.raises(ValueError, match=r"homing.*1 mm or more"):
        manual.teach_travel(0.2)
    assert devices.robot._stage.travel_z_mm == 1.0
    assert gantry(plate_sipper_config).travel_z_mm == 1.0
    manual.teach_travel(MIN_TRAVEL_Z_MM)          # the floor itself is a travel height


def test_teach_travel_refuses_a_height_a_plates_agitation_would_reach_and_changes_nothing(
        plate_sipper_config, built):
    """The config's rule (travel above every dip less the amplitude), with
    the robot in a well: refused, the robot, its witness and the config
    as they were."""
    from fluidics.control.config import gantry
    devices = built(plate_sipper_config, simulation=True, instant=True)
    manual = ManualOperations(devices)
    manual.go_to_well("ssct/A1")
    with pytest.raises(ValueError, match="probes's agitation stroke"):
        manual.teach_travel(45.0)                 # probes: dip 50 less 5
    assert devices.robot._stage.travel_z_mm == 1.0
    assert gantry(plate_sipper_config).travel_z_mm == 1.0
    assert devices.robot.current_well is not None


def test_teach_travel_rechecks_the_sippers_installed_placements(plate_sipper_config, built):
    """The live placements are what go_to_well and agitate use: a plate
    installed on the sipper shallower than the config has it (a library
    caller's set_placement) is checked against the new travel height too,
    before anything changes."""
    from fluidics.control.config import gantry
    from fluidics.errors import StageError
    devices = built(plate_sipper_config, simulation=True, instant=True)
    manual = ManualOperations(devices)
    shallow = manual._vocabulary.placement("probes")
    shallow.dip_z_mm = 20.0                       # agitates up to 15 mm
    devices.robot.set_placement("probes", shallow)
    with pytest.raises(StageError, match="plate 'probes'.*agitation"):
        manual.teach_travel(16.0)                 # fine by the config (dip 50), not by the sipper
    assert devices.robot._stage.travel_z_mm == 1.0
    assert gantry(plate_sipper_config).travel_z_mm == 1.0


def test_a_reload_of_the_plate_the_tip_is_in_is_refused_until_retract(plate_sipper_config, built, tmp_path):
    """With no park well the tip rests in a well between steps: lifting that
    plate with the tip in it bends the tip. A refill of that plate, or of
    everything, is refused naming the well and the remedy; the other plate's
    goes through; after Retract so does this one's."""
    devices = built(plate_sipper_config, simulation=True, instant=True,
                    ledger_path=tmp_path / "ledger.json")
    manual = ManualOperations(devices)
    manual.go_to_well("ssct/A2")
    with pytest.raises(RuntimeError, match="tip is in ssct/A2.*Retract"):
        manual.refill("ssct")
    with pytest.raises(RuntimeError, match="tip is in ssct/A2.*Retract"):
        manual.refill("all")
    with pytest.raises(RuntimeError, match="tip is in ssct/A2.*Retract"):
        manual.refill("ssct/A1")                  # another well of that plate: it was lifted too
    assert "ssct/A2" in devices.reservoirs.undeclared, "nothing was declared"
    manual.refill("probes")
    assert "probes/A1" not in devices.reservoirs.undeclared
    manual.retract()
    manual.refill("ssct")
    assert devices.reservoirs.remaining("ssct/A2") == 1750


def test_without_the_witness_a_plate_reload_waits_for_a_retracted_tip(plate_sipper_config, built, tmp_path):
    """Copilot review: a jog, a halt or a fault reset clears the witness
    without lifting the tip. With no well to name, only a position known
    to be retracted lets a plate (or everything) reload; a bottle is never
    under the tip."""
    devices = built(plate_sipper_config, simulation=True, instant=True,
                    ledger_path=tmp_path / "ledger.json")
    manual = ManualOperations(devices)
    manual.go_to_well("ssct/A2")
    manual.jog("z", -1.0)                          # Z 51: the witness is gone, the tip is not
    assert devices.robot.current_well is None and not devices.robot.is_retracted
    for key in ("ssct", "probes", "all", "ssct/A1"):
        with pytest.raises(RuntimeError, match="not known to be retracted.*Retract"):
            manual.refill(key)
    manual.refill("port_25")                       # a bottle is never under the tip
    assert "port_25" not in devices.reservoirs.undeclared
    manual.go_to_well("ssct/A2")
    devices.make_safe()                            # an aborted run's halt clears the witness too
    with pytest.raises(RuntimeError, match="not known to be retracted"):
        manual.refill("ssct")
    manual.retract()
    manual.refill("all")
    assert not devices.reservoirs.undeclared
