# tests/integration/test_manual_operations.py
"""ManualOperations: the operator's verbs, against the simulated rig.

Each verb is one move on one device, blocking, with no sequence arithmetic
around it -- what a script or the manual tab asks for is what moves.
"""

import pytest

from fluidics.errors import AbortRequested
from fluidics.manual_operations import ManualOperations


@pytest.fixture
def rig(open_chamber_config, built):
    devices = built(open_chamber_config, simulation=True)
    devices.controller.COMMAND_SECONDS = 0
    return devices, ManualOperations(devices)


@pytest.fixture
def flow_cell(flow_cell_config, built):
    devices = built(flow_cell_config, simulation=True)
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
        """The verbs pass the same gates a run does, so devices.abort() stops
        a manual move too -- and reset() lets the next one through."""
        devices, manual = rig
        devices.abort()
        with pytest.raises(AbortRequested):
            manual.extract(2, 300, 500)
        assert devices.syringe_pump.executed == []
        devices.reset()
        manual.extract(2, 300, 500)
        assert len(devices.syringe_pump.executed) == 1
