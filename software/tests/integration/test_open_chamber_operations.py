# tests/integration/test_open_chamber_operations.py
import pytest

from fluidics.open_chamber_operations import OpenChamberOperations


@pytest.fixture
def rig(open_chamber_hardware):
    config, sp, sv, dp, tc = open_chamber_hardware
    return OpenChamberOperations(config, sp, sv, dp, tc), sp


@pytest.fixture
def oc_ops(rig):
    return rig[0]


class TestOpenChamberVolumes:
    """The chains the pump actually executes, against the fixture config:
    2500 uL syringe starting half full, speed_code_limit 10, extract port 2,
    dispense port 3, waste port 1, tubing sv->sp 300, sp->oc 900, chamber
    1300. Every expected op below is hand-derived from the method's own
    documented assumptions -- especially add_reagent's overflow bookkeeping,
    the riskiest arithmetic in the layer. Volumes stay literal because they
    are what these tests pin; speed codes come via flow_rate_to_speed_code,
    whose mapping is tested elsewhere.
    """

    def test_add_reagent_without_fill(self, rig):
        ops, sp = rig
        ops.process_sequence({"type": "add_reagent", "fluidic_port": 3,
                              "flow_rate": 1000, "volume": 1000})
        c = sp.flow_rate_to_speed_code(1000)
        assert sp.executed == [
            [("dispense_to_waste", 10)],
            # volume - sp_to_oc = 100 into the syringe...
            [("extract", 2, 100, 10)],
            # ...pushed out, then the sp->oc tubing turned over.
            [("dispense", 3, 100, c),
             ("extract", 2, 900, 10),
             ("dispense", 3, 900, c)],
        ]

    def test_add_reagent_with_fill_pins_the_overflow_bookkeeping(self, rig):
        """chamber < sp_to_oc + sv_to_sp here (1000 requested vs 900 + 300),
        so 200 uL of overflow must go out the waste port mid-sequence."""
        ops, sp = rig
        ops.process_sequence({"type": "add_reagent", "fluidic_port": 3,
                              "flow_rate": 1000, "volume": 1000,
                              "fill_tubing_with": 5})
        c = sp.flow_rate_to_speed_code(1000)
        assert sp.executed == [
            [("dispense_to_waste", 10)],
            # max(volume - sp_to_oc - sv_to_sp, 0) = 0 from the reagent port.
            [("extract", 2, 0, 10)],
            # sv->sp of buffer in, overflow out through waste port 1.
            [("extract", 2, 300, 10), ("dispense", 1, 200, c)],
            [("dispense", 3, 100, c),
             ("extract", 2, 900, 10),
             ("dispense", 3, 900, c)],
        ]
        # Ending empty is what proves the overflow bookkeeping balanced --
        # the one held-volume assertion kept at this layer; the accounting
        # itself is unit-tested on the simulation.
        assert sp.get_current_volume() == 0

    def test_clear_and_add_reagent_turns_both_tubings_over(self, rig):
        ops, sp = rig
        ops.process_sequence({"type": "clear_and_add_reagent",
                              "fluidic_port": 3, "flow_rate": 1000,
                              "volume": 1000})
        c = sp.flow_rate_to_speed_code(1000)
        assert sp.executed == [
            [("dispense_to_waste", 10)],
            # Old buffer in the sv->sp tubing out to waste, reagent in.
            [("extract", 2, 300, 10), ("dispense_to_waste", 10),
             ("extract", 2, 700, 10)],
            [("extract", 2, 300, 10)],
            # Old liquid in the sp->oc tubing pushed through to the chamber.
            [("dispense", 3, 900, c)],
            [("dispense", 3, 100, c),
             ("extract", 2, 900, 10),
             ("dispense", 3, 900, c)],
        ]

    def test_wash_constant_flow_without_fill(self, rig):
        ops, sp = rig
        ops.process_sequence({"type": "wash_constant_flow", "fluidic_port": 6,
                              "flow_rate": 1000, "volume": 1000})
        c = sp.flow_rate_to_speed_code(1000)
        assert sp.executed == [
            [("dispense_to_waste", 10)],
            [("extract", 2, 700, 10)],
            [("extract", 2, 300, 10), ("dispense", 3, 1000, c)],
        ]

    def test_priming_uses_the_slower_priming_speed(self, rig):
        """Air in fresh tubing needs slow flow to stabilize, so the per-port
        draws run at the 8000 uL/min cap, not the sequence's rate."""
        ops, sp = rig
        ops.process_sequence({"type": "priming", "fluidic_port": 10,
                              "flow_rate": 1000, "volume": 1000})
        c = sp.flow_rate_to_speed_code(1000)
        priming = sp.flow_rate_to_speed_code(8000)
        # Dump, ten port chains, final fill-and-dispense.
        assert len(sp.executed) == 12
        assert sp.executed[1] == [("extract", 2, 300, priming),
                                  ("dispense_to_waste", 10)]
        assert sp.executed[-1] == [("extract", 2, 1000, priming),
                                   ("dispense", 3, 1000, c)]


class TestProcessSequence:
    def test_add_reagent(self, oc_ops):
        seq = {"type": "add_reagent", "fluidic_port": 3, "flow_rate": 1000, "volume": 1000}
        oc_ops.process_sequence(seq)

    def test_add_reagent_with_fill_tubing(self, oc_ops):
        seq = {"type": "add_reagent", "fluidic_port": 3, "flow_rate": 1000,
               "volume": 1000, "fill_tubing_with": 5}
        oc_ops.process_sequence(seq)

    def test_clear_and_add_reagent(self, oc_ops):
        seq = {"type": "clear_and_add_reagent", "fluidic_port": 3,
               "flow_rate": 1000, "volume": 1000}
        oc_ops.process_sequence(seq)

    def test_wash_constant_flow(self, oc_ops):
        seq = {"type": "wash_constant_flow", "fluidic_port": 6,
               "flow_rate": 1000, "volume": 1000}
        oc_ops.process_sequence(seq)

    def test_wash_constant_flow_with_fill_tubing(self, oc_ops):
        seq = {"type": "wash_constant_flow", "fluidic_port": 6,
               "flow_rate": 1000, "volume": 1000, "fill_tubing_with": 5}
        oc_ops.process_sequence(seq)

    def test_priming(self, oc_ops):
        seq = {"type": "priming", "fluidic_port": 10, "flow_rate": 1000, "volume": 1000}
        oc_ops.process_sequence(seq)

    def test_clean_up(self, oc_ops):
        seq = {"type": "clean_up", "fluidic_port": 10, "flow_rate": 1000, "volume": 1000}
        oc_ops.process_sequence(seq)

    def test_set_temperature(self, oc_ops):
        seq = {"type": "set_temperature", "temperature": 50}
        oc_ops.process_sequence(seq)

    def test_unknown_type_raises(self, oc_ops):
        seq = {"type": "nonexistent"}
        with pytest.raises(ValueError, match="Unknown sequence type"):
            oc_ops.process_sequence(seq)


class TestDrainPumpAndCleanUpGuards:
    """Two of the abort design's live bugs, pinned as fixed."""

    def test_a_failing_wash_dispense_still_stops_the_drain_pump(self, rig):
        """The drain pump runs across the whole dispense; before this, an
        exception from execute() left it aspirating with nobody to stop it."""
        ops, sp = rig
        drain = []
        ops.dp.start = lambda power: drain.append("start")
        ops.dp.stop = lambda: drain.append("stop")
        original = sp.execute

        def fail_under_drain():
            if drain == ["start"]:
                raise RuntimeError("pump went away")
            original()

        sp.execute = fail_under_drain
        with pytest.raises(Exception, match="pump went away"):
            ops.process_sequence({"type": "wash_constant_flow", "fluidic_port": 6,
                                  "flow_rate": 1000, "volume": 1000})
        assert drain == ["start", "stop"]

    def test_an_aborted_clean_up_skips_the_final_aspiration(self, rig):
        """Every sibling disc-pump call checks the latch first; the clean-up
        aspiration did not. A cancel latches the disc pump too, so its wait
        returned at once -- but the pump was still pulsed to full power."""
        ops, sp = rig
        aspirations = []
        ops.dp.aspirate = lambda seconds: aspirations.append(seconds)
        original = sp.execute

        def abort_after_the_final_chain():
            original()
            # Only the last chain dispenses to the chamber; an earlier abort
            # would return through one of the guards that already existed.
            if any(op[0] == "dispense" for op in sp.executed[-1]):
                sp.abort()
                ops.dp.abort()

        sp.execute = abort_after_the_final_chain
        ops.process_sequence({"type": "clean_up", "fluidic_port": 10,
                              "flow_rate": 1000, "volume": 1000})
        assert aspirations == []
