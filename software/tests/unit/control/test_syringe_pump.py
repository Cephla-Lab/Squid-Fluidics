# tests/unit/control/test_syringe_pump.py
import pytest
from fluidics.control.syringe_pump import SyringePump, SyringePumpSimulation


def _make_sim_with_real_speed_code(speed_code_limit=10):
    """A simulated pump. Both classes inherit the conversions from SpeedCodes,
    so the simulation's answers are the real pump's answers -- this used to
    require binding SyringePump's method onto the instance by hand.
    """
    return SyringePumpSimulation(sn=None, syringe_ul=5000,
                                 speed_code_limit=speed_code_limit, waste_port=1)


class TestSpeedCodesIsShared:
    """The simulation used to stub flow_rate_to_speed_code as `return 20`, so a
    simulated run ran every sequence at one rate no matter what it asked for --
    and anything reasoning about the actual rate measured against a number the
    simulation had invented.
    """

    def test_the_simulation_honours_the_requested_rate(self):
        sim = _make_sim_with_real_speed_code()
        assert sim.flow_rate_to_speed_code(500) != sim.flow_rate_to_speed_code(5000)

    @pytest.mark.parametrize("rate", [500, 2000, 5000, 10000, 60000])
    def test_the_simulation_agrees_with_the_real_pump(self, rate):
        sim = _make_sim_with_real_speed_code()
        real = SyringePump.flow_rate_to_speed_code.__get__(sim)
        assert sim.flow_rate_to_speed_code(rate) == real(rate)

    def test_the_simulation_keeps_its_speed_code_limit(self):
        """It used to discard the constructor argument, so the clamp that keeps
        a run below a dangerous rate did nothing in simulation."""
        assert _make_sim_with_real_speed_code(speed_code_limit=17).speed_code_limit == 17


class TestSpeedSecMapping:
    def test_mapping_length(self):
        assert len(SyringePump.SPEED_SEC_MAPPING) == 41  # speed codes 0-40

    def test_mapping_monotonically_increasing(self):
        mapping = SyringePump.SPEED_SEC_MAPPING
        for i in range(1, len(mapping)):
            assert mapping[i] >= mapping[i - 1], f"Not monotonic at index {i}"

    def test_simulation_has_same_mapping(self):
        assert SyringePump.SPEED_SEC_MAPPING == SyringePumpSimulation.SPEED_SEC_MAPPING


class TestFlowRateToSpeedCode:
    @pytest.fixture
    def pump_sim(self):
        return _make_sim_with_real_speed_code(speed_code_limit=10)

    def test_exact_speed_code_match(self, pump_sim):
        """When target time exactly matches a mapping entry, return that code."""
        # speed code 0: 1.25 sec -> flow_rate = 5000*60/1.25 = 240000 ul/min
        # speed code 12: 5.00 sec -> flow_rate = 5000*60/5000 = 60000 ul/min
        code = pump_sim.flow_rate_to_speed_code(60000)
        # target_time = 5000*60/60000 = 5.0, matches SPEED_SEC_MAPPING[12]
        assert code == 12

    def test_very_fast_rate_returns_speed_code_limit(self, pump_sim):
        """Flow rate faster than speed_code_limit → clamp to limit."""
        code = pump_sim.flow_rate_to_speed_code(999999)
        assert code == pump_sim.speed_code_limit

    def test_very_slow_rate_returns_max_code(self, pump_sim):
        """Flow rate slower than all mappings → return last code (40)."""
        code = pump_sim.flow_rate_to_speed_code(1)  # very slow
        assert code == 40

    def test_returns_closest_code(self, pump_sim):
        """Binary search finds the closest speed code."""
        code = pump_sim.flow_rate_to_speed_code(5000)
        # target_time = 5000*60/5000 = 60.0
        # SPEED_SEC_MAPPING[28] = 66.67, [27] = 60.00 → exact match
        assert code == 27

    def test_all_codes_reachable(self, pump_sim):
        """Every speed code from limit to 40 should be reachable by some flow rate.

        flow_rate_to_speed_code computes target_time = volume * 60 / rate and
        compares directly against SPEED_SEC_MAPPING, so we derive rates from
        that formula, which get_flow_rate now inverts exactly.
        """
        pump_sim.speed_code_limit = 0
        mapping = SyringePump.SPEED_SEC_MAPPING
        seen = set()
        for sec in mapping:
            rate = pump_sim.volume * 60 / sec
            seen.add(pump_sim.flow_rate_to_speed_code(rate))
        assert len(seen) == 41


class TestGetFlowRate:
    def test_known_values(self):
        """get_flow_rate returns volume * 60 / mapping[code], in uL/min."""
        p = SyringePumpSimulation(sn=None, syringe_ul=5000, speed_code_limit=10, waste_port=1)
        # speed code 0 -> 1.25 sec -> 5000*60/1.25 = 240000 uL/min
        assert p.get_flow_rate(0) == 240000.0
        # speed code 40 -> 600.0 sec -> 5000*60/600 = 500 uL/min,
        # which is the rate MERFISH runs at.
        assert p.get_flow_rate(40) == 500.0

    def test_get_flow_rate_inverts_flow_rate_to_speed_code(self):
        """The two are now on one scale, so they round-trip.

        Before this they differed by 1000x -- get_flow_rate in mL/min,
        flow_rate_to_speed_code in uL/min -- which is exactly the trap draw
        protection would have hit when comparing measured against expected.
        """
        p = _make_sim_with_real_speed_code(speed_code_limit=0)
        for code in range(41):
            assert p.flow_rate_to_speed_code(p.get_flow_rate(code)) == code

    def test_flow_rate_to_speed_code_round_trips(self):
        """flow_rate_to_speed_code round-trips against the mapping directly."""
        p = _make_sim_with_real_speed_code(speed_code_limit=0)
        mapping = SyringePump.SPEED_SEC_MAPPING
        for code in range(41):
            # Rate that produces target_time == mapping[code]
            rate = p.volume * 60 / mapping[code]
            recovered_code = p.flow_rate_to_speed_code(rate)
            assert recovered_code == code, f"Round-trip failed for code {code}: rate={rate}, recovered={recovered_code}"


class TestEffectiveSpeedCode:
    """One clamp shared by the real pump and the simulation, because the
    recorded chains are measured against it in tests: a private copy in
    either class could drift and the tests would follow the copy."""

    def test_none_means_as_fast_as_allowed(self):
        assert _make_sim_with_real_speed_code().effective_speed_code(None) == 10

    def test_a_faster_code_is_clamped_to_the_limit(self):
        # Lower code = faster stroke; the limit is a floor on the code.
        assert _make_sim_with_real_speed_code().effective_speed_code(3) == 10

    def test_a_slower_code_passes_through(self):
        assert _make_sim_with_real_speed_code().effective_speed_code(27) == 27


class TestSimulationAccounting:
    """The held-volume fold behind get_current_volume/get_chained_volume.

    The integration volume tests assert executed chains and lean on this
    accounting for the overflow-dump decisions, so its ordering semantics
    are pinned once here rather than re-asserted at every call site.
    """

    def _pump(self):
        return SyringePumpSimulation(sn=None, syringe_ul=1000,
                                     speed_code_limit=10, waste_port=3)

    def test_starts_half_full_like_the_old_constant_reported(self):
        assert self._pump().get_current_volume() == 500

    def test_execute_applies_the_chain_in_order(self):
        """A mid-chain waste dump empties what is held at that point, not at
        the end -- the ordering clear_and_add_reagent depends on."""
        p = self._pump()
        p.extract(2, 300, 10)
        p.dispense_to_waste()
        p.extract(2, 700, 10)
        p.execute()
        assert p.get_current_volume() == 700

    def test_chained_volume_is_the_queue_not_the_plunger(self):
        p = self._pump()
        p.extract(2, 300, 10)
        assert p.get_chained_volume() == 300
        p.dispense(3, 100, 10)
        assert p.get_chained_volume() == 200
        p.dispense_to_waste()
        assert p.get_chained_volume() == 0
        assert p.get_current_volume() == 500   # nothing executed yet

    def test_reset_chain_drops_the_queue(self):
        p = self._pump()
        p.extract(2, 300, 10)
        p.reset_chain()
        p.execute()
        assert p.get_current_volume() == 500
        assert p.get_chained_volume() == 0
