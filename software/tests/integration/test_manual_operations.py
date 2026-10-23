# tests/integration/test_manual_operations.py
"""ManualOperations: the operator's verbs, against the simulated rig.

Each verb is one move on one device, blocking, with no sequence arithmetic
around it -- what a script or the manual tab asks for is what moves.
"""

import pytest

from fluidics.control.config import gantry, load_config
from fluidics.errors import AbortRequested, ReservoirShortfall, ReservoirWriteError, StageError
from fluidics.manual_operations import ManualOperations
from fluidics.reservoirs import reservoir_definitions
from fluidics.robot.stage import MIN_TRAVEL_Z_MM
from fluidics.vocabulary import sipper_vocabulary

from ..conftest import HYB, PUSH


@pytest.fixture
def rig(open_chamber_config, instant_devices):
    devices = instant_devices(open_chamber_config)
    return devices, ManualOperations(devices)


@pytest.fixture
def flow_cell(flow_cell_config, instant_devices):
    devices = instant_devices(flow_cell_config)
    return devices, ManualOperations(devices)


@pytest.fixture
def sipper(plate_sipper_config, instant_devices):
    devices = instant_devices(plate_sipper_config)
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

def test_a_manual_extract_through_the_sipper_needs_a_well(sipper):
    """Review focus 4: the manual tab cannot pull air through the sipper."""
    _, manual = sipper
    manual.open_port(1)
    with pytest.raises(RuntimeError, match="not in a well"):
        manual.extract(2, 100, 1000)
    manual.go_to_well("ssct/A1")
    manual.extract(2, 100, 1000)            # fine with the tip in a well
    manual.retract()
    with pytest.raises(RuntimeError, match="not in a well"):
        manual.extract(2, 100, 1000)
    manual.extract(1, 100, 1000)            # another syringe port never reaches the sipper


def test_a_sipper_draw_with_do_not_lower_tip_on_names_the_switch(sipper):
    """With the switch on, Go to well stops above the well: the refusal
    names the switch, not the move the operator just made."""
    _, manual = sipper
    manual.set_lower_tip(False)
    manual.go_to_well("ssct/A1")
    manual.open_port(1)
    with pytest.raises(RuntimeError, match="Do not lower tip is on"):
        manual.extract(2, 100, 1000)
    manual.set_lower_tip(True)


def test_robot_verbs_refuse_on_a_rig_without_a_robot(flow_cell):
    _, manual = flow_cell
    with pytest.raises(RuntimeError, match="no robot"):
        manual.retract()


def test_a_taught_position_is_kept_to_the_hundredth_of_a_millimetre(plate_sipper_config, sipper):
    """Jogs of 0.1 mm add up with float error: the config, and so the file,
    reads 9.9 and 6.3, not 9.899999999999999 and 6.300000000000001."""
    devices, manual = sipper
    manual.teach_a1("probes", (9.899999999999999, 1.4000000000000001))
    manual.teach_dip("probes", 49.00000000000001)
    manual.teach_travel(6.300000000000001)
    probes = gantry(plate_sipper_config).reservoirs[1]
    assert tuple(probes.a1_xy_mm) == (9.9, 1.4) and probes.dip_z_mm == 49.0
    assert gantry(plate_sipper_config).travel_z_mm == 6.3


def test_teaching_changes_the_live_placement_and_the_config_together(plate_sipper_config, sipper):
    """Codex finding 11: a taught A1 is where the next go-to-well goes,
    and what save_config will write."""
    devices, manual = sipper
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


def test_jog_refuses_xy_below_travel_height(plate_sipper_config, sipper):
    _, manual = sipper
    manual.jog("z", gantry(plate_sipper_config).travel_z_mm + 3.0)
    with pytest.raises(StageError, match="travel height"):
        manual.jog("x", 1.0)


def test_teach_travel_refuses_a_height_homing_may_park_below(plate_sipper_config, sipper):
    """Home, then "Set travel height here", would teach the 0.1 to 0.5 mm
    the drives park at, and the next launch's homing refuses any Z that
    parks below it. Refused before the robot or the config changes."""
    devices, manual = sipper
    travel = gantry(plate_sipper_config).travel_z_mm
    with pytest.raises(ValueError, match=r"homing.*1 mm or more"):
        manual.teach_travel(0.2)
    assert devices.robot._stage.travel_z_mm == travel
    assert gantry(plate_sipper_config).travel_z_mm == travel
    manual.teach_travel(MIN_TRAVEL_Z_MM)          # the floor itself is a travel height


def test_teach_travel_refuses_a_height_a_plates_agitation_would_reach_and_changes_nothing(
        plate_sipper_config, sipper):
    """The config's rule (travel above every dip less the amplitude), with
    the robot in a well: refused, the robot, its witness and the config
    as they were."""
    devices, manual = sipper
    g = gantry(plate_sipper_config)
    travel = g.travel_z_mm
    shallowest = min(g.reservoirs.values(), key=lambda r: r.dip_z_mm)
    other = next(r for r in g.reservoirs.values() if r is not shallowest)
    manual.go_to_well(f"{other.name}/A1")         # every plate is checked, not the tip's alone
    with pytest.raises(ValueError, match=f"plate '{shallowest.name}'.*agitation stroke"):
        # The top of the shallowest plate's stroke: only its agitation reaches it.
        manual.teach_travel(shallowest.dip_z_mm - g.agitate_amplitude_mm)
    assert devices.robot._stage.travel_z_mm == travel
    assert gantry(plate_sipper_config).travel_z_mm == travel
    assert devices.robot.current_well is not None


def test_teach_travel_rechecks_the_sippers_installed_placements(plate_sipper_config, sipper):
    """The live placements are what go_to_well and agitate use: a plate
    installed on the sipper shallower than the config has it (a library
    caller's set_placement) is checked against the new travel height too,
    before anything changes."""
    devices, manual = sipper
    travel = gantry(plate_sipper_config).travel_z_mm
    shallow = sipper_vocabulary(plate_sipper_config).placement("probes")
    shallow.dip_z_mm = 20.0                       # agitates up to 15 mm
    devices.robot.set_placement("probes", shallow)
    with pytest.raises(StageError, match="plate 'probes'.*agitation"):
        manual.teach_travel(16.0)                 # fine by the config's dips, not by the sipper's
    assert devices.robot._stage.travel_z_mm == travel
    assert gantry(plate_sipper_config).travel_z_mm == travel


def test_a_reload_of_the_plate_the_tip_is_in_is_refused_until_retract(plate_sipper_config, built, tmp_path):
    """With no park well the tip rests in a well between steps: lifting that
    plate with the tip in it bends the tip. A refill of that plate, or of
    everything, is refused naming the well and the remedy; the other plate's
    goes through; after Retract so does this one's."""
    devices = built(plate_sipper_config, simulation=True, instant=True,
                    volume_tracking_path=tmp_path / "volumes.json")
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
    assert devices.reservoirs.remaining("ssct/A2") == gantry(plate_sipper_config).reservoirs[2].loaded_ul


def test_without_the_witness_a_plate_reload_waits_for_a_retracted_tip(plate_sipper_config, built, tmp_path):
    """Copilot review: a jog, a halt or a fault reset clears the witness
    without lifting the tip. With no well to name, only a position known
    to be retracted lets a plate (or everything) reload; a bottle is never
    under the tip."""
    devices = built(plate_sipper_config, simulation=True, instant=True,
                    volume_tracking_path=tmp_path / "volumes.json")
    manual = ManualOperations(devices)
    manual.go_to_well("ssct/A2")
    manual.jog("z", -1.0)                          # 1 mm up: the witness is gone, the tip is not
    assert devices.robot.current_well is None and not devices.robot.is_retracted
    for key in ("ssct", "probes", "all", "ssct/A1"):
        with pytest.raises(RuntimeError, match="not known to be retracted.*Retract"):
            manual.refill(key)
    manual.refill("port_4")                        # a bottle is never under the tip
    assert "port_4" not in devices.reservoirs.undeclared
    manual.go_to_well("ssct/A2")
    devices.make_safe()                            # an aborted run's halt clears the witness too
    with pytest.raises(RuntimeError, match="not known to be retracted"):
        manual.refill("ssct")
    manual.retract()
    manual.refill("all")
    assert not devices.reservoirs.undeclared


# --- loaded volumes: the config file and the volume tracker together ---

@pytest.fixture
def sipper_on_file(plate_sipper_config_on_file, instant_devices):
    """(config, devices, manual) over a copy of the sipper sample, so the
    verb's save lands in the test's tmp_path."""
    devices = instant_devices(plate_sipper_config_on_file)
    return plate_sipper_config_on_file, devices, ManualOperations(devices)


def _agree(config, devices):
    """The time-zero gate's twin is built from the config and restored from
    the live volume tracker, so the two must define the same volumes."""
    return reservoir_definitions(config) == devices.reservoirs.snapshot()["definitions"]


def test_a_plate_name_sets_every_well_of_it_in_the_file_and_the_tracker(sipper_on_file):
    config, devices, manual = sipper_on_file
    ul = gantry(config).reservoirs[1].loaded_ul + 100     # any volume but the one loaded
    ssct = gantry(config).reservoirs[2].loaded_ul
    manual.set_loaded_volume(["probes"], ul)
    tracker = devices.reservoirs
    assert {tracker.loaded(key) for key in tracker.tracked if key.startswith("probes/")} == {ul}
    assert len([key for key in tracker.tracked if key.startswith("probes/")]) == 96
    assert tracker.loaded("ssct/A1") == ssct, "the other plate is not touched"
    assert gantry(load_config(config.source_path)).reservoirs[1].loaded_ul == ul
    assert _agree(config, devices)
    assert reservoir_definitions(load_config(config.source_path)) == reservoir_definitions(config)


def test_a_selection_of_a_whole_plate_is_the_plate_and_a_few_wells_are_overrides(sipper_on_file):
    """The file says a whole plate once; the bottle in the same selection is
    tracked from then on."""
    config, devices, manual = sipper_on_file
    bottles = dict(config.reagent_selection.selector_valves.loaded_volume_ul)   # port_3 not among them
    every = [f"probes/{well}" for well in gantry(config).reservoirs[1].geometry.wells()]
    manual.set_loaded_volume(every, 200)
    probes = gantry(load_config(config.source_path)).reservoirs[1]
    assert (probes.loaded_ul, probes.loaded_ul_by_well) == (200, None)
    manual.set_loaded_volume(["ssct/A1", "ssct/B1", "port_3"], 1000)
    reloaded = load_config(config.source_path)
    assert gantry(reloaded).reservoirs[2].loaded_ul_by_well == {"A1": 1000, "B1": 1000}
    assert reloaded.reagent_selection.selector_valves.loaded_volume_ul == dict(bottles, port_3=1000)
    assert devices.reservoirs.loaded("port_3") == 1000
    assert devices.reservoirs.undeclared == ["port_3"], "newly tracked: a reset declares it"
    assert _agree(config, devices)


def test_the_dedicated_wells_must_hold_the_sippers_line(sipper_on_file):
    """Priming and clean-up each draw the sipper's line full from their
    well: set_dedicated_well's rule holds as the volume moves, for the well
    and for its plate, before anything changes."""
    config, devices, manual = sipper_on_file
    line = config.reagent_selection.selector_valves.tubing_fluid_amount_ul["port_1"]   # tip to the valve
    ssct = gantry(config).reservoirs[2].loaded_ul
    before = open(config.source_path, encoding="utf-8").read()
    for keys, ul, said in ((["ssct/H11"], line - 1, "ssct/H11 is the priming well"),
                           (["ssct/H12"], 100, "ssct/H12 is the clean-up well"),
                           (["ssct"], 100, "ssct/H11 is the priming well")):
        with pytest.raises(ValueError, match=f"{said}: it must hold at least the {line} uL the "
                                             "sipper's line takes to prime"):
            manual.set_loaded_volume(keys, ul)
        assert manual.loaded_volume_refusal(keys, ul).startswith(said)
        assert manual.loaded_volume_refusal(keys) is None, "the keys alone are fine"
    assert open(config.source_path, encoding="utf-8").read() == before
    assert devices.reservoirs.loaded("ssct/H11") == ssct and _agree(config, devices)
    manual.set_loaded_volume(["ssct/H11"], line)          # the line itself is enough
    assert devices.reservoirs.loaded("ssct/H11") == line


@pytest.mark.parametrize("key, said", [
    ("port_1", "port_1 is the sipper's port, not a bottle"),
    ("water", "water names no tracked reservoir or plumbed bottle on this rig"),
    ("probes/Z9", "probes/Z9 names no tracked reservoir or plumbed bottle on this rig"),
    ("port_7", "port_7 names no tracked reservoir or plumbed bottle on this rig"),
])
def test_a_key_that_names_no_reservoir_is_refused_before_any_value(sipper_on_file, key, said):
    config, devices, manual = sipper_on_file
    loaded = gantry(config).reservoirs[1].loaded_ul
    assert manual.loaded_volume_refusal([key]).startswith(said)
    with pytest.raises(ValueError, match=said):
        manual.set_loaded_volume(["probes/A1", key], 500)
    assert devices.reservoirs.loaded("probes/A1") == loaded
    assert gantry(load_config(config.source_path)).reservoirs[1].loaded_ul_by_well is None


def test_a_failed_config_save_changes_nothing(sipper_on_file, monkeypatch):
    """The config file is written before the volume tracker: a save that
    fails leaves the tracker and the live config as they were."""
    import fluidics.control.config as config_module
    config, devices, manual = sipper_on_file
    loaded = gantry(config).reservoirs[1].loaded_ul

    def refuse(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(config_module, "save_config", refuse)
    with pytest.raises(OSError, match="disk full"):
        manual.set_loaded_volume(["probes"], loaded + 100)
    assert devices.reservoirs.loaded("probes/A1") == loaded
    assert gantry(config).reservoirs[1].loaded_ul == loaded
    assert _agree(config, devices)


def test_a_failed_volume_tracking_write_keeps_the_session_on_the_old_volume(sipper_on_file, monkeypatch):
    """The file has the new volume, but this session's config and volume
    tracker both keep the old one, so the gate and the pump read one figure,
    and the message says what the next launch does."""
    config, devices, manual = sipper_on_file
    tracker = devices.reservoirs
    loaded = gantry(config).reservoirs[1].loaded_ul

    def refuse():
        raise OSError("disk full")

    monkeypatch.setattr(tracker, "_write", refuse)
    with pytest.raises(ReservoirWriteError, match="next launch brings it up undeclared"):
        manual.set_loaded_volume(["probes"], loaded + 100)
    assert tracker.loaded("probes/A1") == loaded
    assert gantry(config).reservoirs[1].loaded_ul == loaded
    assert _agree(config, devices)
    assert gantry(load_config(config.source_path)).reservoirs[1].loaded_ul == loaded + 100


@pytest.mark.parametrize("volume", [0, -5, 1.5, True, "350"],
                         ids=["zero", "negative", "fraction", "bool", "text"])
def test_a_volume_that_is_not_a_whole_number_above_0_is_refused(sipper_on_file, volume):
    """Refused by the verb itself, naming the reservoirs, before the file or
    the tracker changes: without it 1.5 would be set as 1."""
    config, devices, manual = sipper_on_file
    loaded = gantry(config).reservoirs[1].loaded_ul
    with open(config.source_path, "rb") as f:
        before = f.read()
    said = manual.loaded_volume_refusal(["probes"], volume)
    assert said.startswith(f"every well of probes: {volume!r} uL is not a loaded volume"), said
    with pytest.raises(ValueError, match="is not a loaded volume"):
        manual.set_loaded_volume(["probes"], volume)
    with open(config.source_path, "rb") as f:
        assert f.read() == before
    assert devices.reservoirs.loaded("probes/A1") == loaded


def test_an_empty_selection_is_refused(sipper_on_file):
    _, _, manual = sipper_on_file
    with pytest.raises(ValueError, match="no reservoir named"):
        manual.set_loaded_volume([], 100)


def test_a_bottle_that_becomes_tracked_is_undeclared_until_its_reset(sipper_on_file):
    """What a bottle gave before it was tracked is not known: a reset
    declares it, as for a volume added to the config file."""
    config, devices, manual = sipper_on_file
    manual.set_loaded_volume(["port_3"], 50000)
    tracker = devices.reservoirs
    assert "port_3" in tracker.tracked and tracker.undeclared == ["port_3"]
    manual.refill("port_3")
    assert tracker.undeclared == [] and tracker.remaining("port_3") == 50000
    assert _agree(config, devices)


def test_a_volume_below_what_a_well_has_given_is_refused(sipper_on_file):
    config, devices, manual = sipper_on_file
    loaded = gantry(config).reservoirs[1].loaded_ul
    manual.go_to_well("probes/A1")
    manual.open_port(1)
    manual.extract(2, 100, 1000)
    with pytest.raises(ValueError, match="probes/A1 has given 100 uL since its last reset"):
        manual.set_loaded_volume(["probes"], 50)
    assert gantry(load_config(config.source_path)).reservoirs[1].loaded_ul == loaded
    manual.set_loaded_volume(["probes"], 150)
    assert devices.reservoirs.remaining("probes/A1") == 50, "accumulated stands"


def test_the_time_zero_gate_reads_the_new_volume(plate_sipper_config_on_file, make_system):
    """A run that overdraws ssct/A1 at its loaded 1800 uL fits once ssct/A1
    is set to hold what the run draws from it and 10 uL more: the gate's
    twin, built from the config, reads what the live volume tracker does."""
    config = plate_sipper_config_on_file
    system = make_system(config)
    wash = {"type": "flow_reagent", "fluidic_port": 1, "well": "ssct/A1", "flow_rate": 5000,
            "volume": 1000, "dip_cycles": 3}
    plan = system.plan([HYB, PUSH, wash, dict(wash, name="wash2")])
    with pytest.raises(ReservoirShortfall, match="ssct/A1"):
        system.check_reservoirs(plan)
    drawn = gantry(config).push_volume_ul + 2 * wash["volume"]     # the push, then the two washes
    system.manual.set_loaded_volume(["ssct/A1"], drawn + 10)
    assert ("ssct/A1", drawn, 10) in system.check_reservoirs(plan)
