# tests/integration/test_merfish_operations.py
import pytest

from fluidics.errors import AbortRequested, OperationError
from fluidics.merfish_operations import MERFISHOperations


class TestFlowCellVolumes:
    """The chains the pump actually executes, against the fixture config:
    5000 uL syringe starting half full, speed_code_limit 10, extract port 2,
    common tubing 800 + per-valve 0/200/340, per-port tubing 700/600/450/...

    These are the regression net for the operations rewrite: every number
    below is derived from the config by hand, so a change to the volume
    arithmetic fails here with the exact op that moved. Volumes stay literal
    because they are what these tests pin; speed codes come via
    flow_rate_to_speed_code, whose mapping is tested elsewhere.
    """

    def test_flow_reagent_draws_the_volume_through_the_extract_port(self, flow_cell_rig):
        ops, sp = flow_cell_rig
        ops.process_sequence({"type": "flow_reagent", "fluidic_port": 1,
                              "flow_rate": 5000, "volume": 500})
        code = sp.flow_rate_to_speed_code(5000)
        assert sp.executed == [[("extract", 2, 500, code)]]

    def test_flow_reagent_fill_tubing_draws_the_buffer_after_emptying(self, flow_cell_rig):
        """Port 25 sits on the third valve: 800 common + 340 = 1140 uL of
        tubing. The syringe holds 4500 after the reagent draw, so the fill
        draw must be preceded by a dump (4500 + 1140 > 95% of 5000)."""
        ops, sp = flow_cell_rig
        ops.process_sequence({"type": "flow_reagent", "fluidic_port": 2,
                              "flow_rate": 5000, "volume": 2000,
                              "fill_tubing_with": 25})
        code = sp.flow_rate_to_speed_code(5000)
        assert sp.executed == [
            [("extract", 2, 2000, code)],
            [("dispense_to_waste", 10)],
            [("extract", 2, 1140, code)],
        ]

    def test_a_draw_that_would_overflow_empties_the_syringe_first(self, flow_cell_rig):
        ops, sp = flow_cell_rig
        # 2500 held + 2400 > 4750 (95% of 5000): the dump must come first.
        ops.process_sequence({"type": "flow_reagent", "fluidic_port": 1,
                              "flow_rate": 5000, "volume": 2400})
        code = sp.flow_rate_to_speed_code(5000)
        assert sp.executed == [
            [("dispense_to_waste", 10)],
            [("extract", 2, 2400, code)],
        ]

    def test_priming_pulls_each_ports_tubing_volume(self, flow_cell_rig):
        ops, sp = flow_cell_rig
        ops.process_sequence({"type": "priming", "fluidic_port": 10,
                              "flow_rate": 5000, "volume": 2000})
        code = sp.flow_rate_to_speed_code(5000)
        # Initial dump, one chain per port 1..28, one final fill draw.
        assert len(sp.executed) == 30
        assert sp.executed[0] == [("dispense_to_waste", 10)]
        # Port 1 has 700 uL of tubing; port 10 (second valve) has 600.
        assert sp.executed[1] == [("extract", 2, 700, code),
                                  ("dispense_to_waste", 10)]
        assert sp.executed[10] == [("extract", 2, 600, code),
                                   ("dispense_to_waste", 10)]
        assert sp.executed[-1] == [("extract", 2, 2000, code)]
        # 9x700 + 9x600 + 6x450 + 2x700 + 2x900 of tubing, plus the fill.
        extracted = sum(op[2] for op in sp.executed_ops if op[0] == "extract")
        assert extracted == 17600 + 2000

    def test_priming_use_ports_restricts_the_loop(self, flow_cell_rig):
        ops, sp = flow_cell_rig
        ops.process_sequence({"type": "priming", "fluidic_port": 10,
                              "flow_rate": 5000, "volume": 2000,
                              "use_ports": [1, 2]})
        assert len(sp.executed) == 4  # dump, port 1, port 2, final draw

    def test_an_out_of_range_port_fails_before_anything_moves(self, flow_cell_rig):
        """The old silent no-op in open_port meant this drew 500 uL through
        whatever port was last open. The pre-run check catches the typo at
        time zero; this pins the backstop for sequences that reach the
        operations anyway."""
        ops, sp = flow_cell_rig
        with pytest.raises(OperationError, match="out of range"):
            ops.process_sequence({"type": "flow_reagent", "fluidic_port": 99,
                                  "flow_rate": 5000, "volume": 500})
        assert sp.executed == []


class TestProcessSequence:
    @pytest.fixture
    def ops(self, flow_cell_rig):
        return flow_cell_rig[0]

    def test_flow_reagent(self, ops):
        seq = {"type": "flow_reagent", "fluidic_port": 1, "flow_rate": 5000, "volume": 500}
        ops.process_sequence(seq)  # should not raise

    def test_flow_reagent_with_fill_tubing(self, ops):
        seq = {"type": "flow_reagent", "fluidic_port": 2, "flow_rate": 5000,
               "volume": 2000, "fill_tubing_with": 25}
        ops.process_sequence(seq)

    def test_priming(self, ops):
        seq = {"type": "priming", "fluidic_port": 10, "flow_rate": 5000, "volume": 2000}
        ops.process_sequence(seq)

    def test_clean_up(self, ops):
        seq = {"type": "clean_up", "fluidic_port": 10, "flow_rate": 10000, "volume": 2000}
        ops.process_sequence(seq)

    def test_unknown_type_raises(self, ops):
        seq = {"type": "nonexistent", "fluidic_port": 1, "flow_rate": 100, "volume": 100}
        with pytest.raises(ValueError, match="Unknown sequence type"):
            ops.process_sequence(seq)


class TestSetTemperature:
    @pytest.fixture
    def ops_with_tc(self, flow_cell_hardware_with_tc):
        config, sp, sv, tc = flow_cell_hardware_with_tc
        return MERFISHOperations(config, sp, sv, temperature_controller=tc)

    def test_set_temperature(self, ops_with_tc):
        seq = {"type": "set_temperature", "temperature": 37}
        ops_with_tc.process_sequence(seq)
        assert ops_with_tc.tc.target_temperatures == [37]

    def test_set_temperature_without_controller_no_raise(self, flow_cell_hardware):
        config, sp, sv = flow_cell_hardware
        ops = MERFISHOperations(config, sp, sv)
        seq = {"type": "set_temperature", "temperature": 37}
        ops.process_sequence(seq)  # should not raise


class TestACancelInThePrimingSettleWait:
    def test_no_further_port_is_opened(self, flow_cell_rig):
        """Priming waits a second per port for the flow to stabilize. On the
        run's signal: with a bare sleep the loop would carry on and move the
        next valve before the following execute raised."""
        ops, sp = flow_cell_rig
        opened = []
        ops.sv.open_port = lambda port: opened.append(port)
        original = sp.execute

        def cancel_after_the_first_ports_chain():
            original()
            # Chain 1 is the dump to waste; chain 2 is the first port's draw,
            # so the settle wait is the next checked call. Cancelling at the
            # dump instead would raise at that draw and pin nothing.
            if len(sp.executed) == 2:
                sp.run_control.cancel()

        sp.execute = cancel_after_the_first_ports_chain
        with pytest.raises(AbortRequested):
            ops.process_sequence({"type": "priming", "fluidic_port": 1,
                                  "flow_rate": 500, "volume": 500})
        assert len(opened) == 1
