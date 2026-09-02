# tests/unit/control/test_selector_valve.py

import pytest

from fluidics.control._def import CMD_SET
from fluidics.control.config import load_config
from fluidics.errors import AbortRequested, DeviceError, RunControl
from fluidics.control.controller import FluidControllerSimulation
from fluidics.control.selector_valve import SelectorValveSystem


def _make_valve_system(config_path, run_control=None):
    config = load_config(str(config_path))
    fc = FluidControllerSimulation(serial_number="test")
    # Before SelectorValveSystem homes the valves: under the real clock a
    # paced homing spends its full second per valve, and no test here pins
    # the pacing -- they pin the gates and the routing.
    fc.COMMAND_SECONDS = 0
    return SelectorValveSystem(fc, config, run_control)


@pytest.fixture
def flow_cell_system(fixtures_dir):
    """SelectorValveSystem with 3 valves (flow cell config)."""
    return _make_valve_system(fixtures_dir / "flow_cell_config.yaml")


@pytest.fixture
def open_chamber_system(fixtures_dir):
    """SelectorValveSystem with 1 valve (open chamber config)."""
    return _make_valve_system(fixtures_dir / "open_chamber_config.yaml")


class TestSelectorValveSystemInit:
    def test_flow_cell_port_count(self, flow_cell_system):
        # 3 valves with 10 ports each: (10-1) + (10-1) + 10 = 28
        assert flow_cell_system.available_port_number == 28

    def test_the_count_is_the_shared_config_arithmetic(
            self, flow_cell_system, fixtures_dir):
        """available_port_count is the same formula without hardware attached
        -- the pre-run sequence check uses it, so the two must be one."""
        from fluidics.control.config import available_port_count
        config = load_config(str(fixtures_dir / "flow_cell_config.yaml"))
        assert flow_cell_system.available_port_number == available_port_count(config)

    def test_open_chamber_port_count(self, open_chamber_system):
        # 1 valve with 10 ports: 10
        assert open_chamber_system.available_port_number == 10


class TestAStuckValve:
    def test_it_is_reported_by_name_with_what_to_check(self, flow_cell_system, monkeypatch):
        """Fail-fast is the policy; the report has to carry the valve and
        the remedy, and be a type the bring-up dialog catches -- not a bare
        RuntimeError traceback. And the object keeps the position the
        readback actually gave, not the one it hoped for."""
        valve = flow_cell_system.valves[0]
        monkeypatch.setattr(valve, "get_current_position", lambda: 1)
        with pytest.raises(DeviceError, match="Selector valve 0.*expected 2.*free to rotate"):
            valve.open(2)
        assert valve.position == 1


class TestPortToReagent:
    def test_known_mapping(self, flow_cell_system):
        assert flow_cell_system.port_to_reagent(1) == "reagent x"
        assert flow_cell_system.port_to_reagent(25) == "buffer 1"

    def test_out_of_range_returns_none(self, flow_cell_system):
        assert flow_cell_system.port_to_reagent(999) is None

    def test_unmapped_port_returns_none(self, open_chamber_system):
        # port_2 has no name mapping in open chamber config
        assert open_chamber_system.port_to_reagent(2) is None


class TestTubingFluidAmounts:
    def test_flow_cell_tubing_to_valve(self, flow_cell_system):
        # Port 1 is on valve 0: common(800) + valve_0(0) = 800
        assert flow_cell_system.get_tubing_fluid_amount_to_valve(1) == 800
        # Port 10 is on valve 1: common(800) + valve_1(200) = 1000
        assert flow_cell_system.get_tubing_fluid_amount_to_valve(10) == 1000
        # Port 19 is on valve 2: common(800) + valve_2(340) = 1140
        assert flow_cell_system.get_tubing_fluid_amount_to_valve(19) == 1140

    def test_open_chamber_tubing_to_valve(self, open_chamber_system):
        # Single valve: common(300) + valve_0(0) = 300
        assert open_chamber_system.get_tubing_fluid_amount_to_valve(1) == 300

    def test_tubing_to_port(self, flow_cell_system):
        assert flow_cell_system.get_tubing_fluid_amount_to_port(1) == 700
        assert flow_cell_system.get_tubing_fluid_amount_to_port(10) == 600
        assert flow_cell_system.get_tubing_fluid_amount_to_port(19) == 450


class TestGetPortNames:
    def test_flow_cell_names_count(self, flow_cell_system):
        names = flow_cell_system.get_port_names()
        assert len(names) == 28

    def test_names_format(self, flow_cell_system):
        """Each entry carries its own port number: the list holds only the
        ports the rig offers, so an index is not a port."""
        names = flow_cell_system.get_port_names()
        assert names[0] == (1, "Port 1: reagent x")
        assert names[24] == (25, "Port 25: buffer 1")

    def test_open_chamber_unmapped_port(self, open_chamber_system):
        names = open_chamber_system.get_port_names()
        # port_2 has no name mapping -> just "Port 2: "
        assert names[1] == (2, "Port 2: ")

    def test_a_port_with_no_tubing_volume_is_not_offered(self, flow_cell_system):
        """The cascade can address more positions than the rig has lines
        on; a position with no tubing volume would open onto nothing."""
        sv = flow_cell_system.config.reagent_selection.selector_valves
        del sv.tubing_fluid_amount_ul["port_25"]
        offered = [port for port, _label in flow_cell_system.get_port_names()]
        assert 25 not in offered
        assert offered[-1] == 28, "the ports after the gap kept their numbers"

    def test_only_the_canonical_spelling_names_a_port(self, flow_cell_system):
        """The model takes any string as a key, but only `port_key`'s
        spelling is ever read back -- `get_tubing_fluid_amount_to_port`
        looks up `port_1` and gets None for anything else. A port offered
        on the strength of `1` or `port_01` would have no volume when a
        draw asked for one, and `port_01` would duplicate `port_1`."""
        sv = flow_cell_system.config.reagent_selection.selector_valves
        del sv.tubing_fluid_amount_ul["port_25"]
        sv.tubing_fluid_amount_ul.update({"25": 100, "port_025": 100,
                                          "port_x": 100})
        offered = [port for port, _label in flow_cell_system.get_port_names()]
        assert 25 not in offered
        assert offered.count(1) == 1


class TestOpenPort:
    def test_open_port_single_valve(self, open_chamber_system):
        open_chamber_system.open_port(5)
        assert open_chamber_system.get_current_port() == 5

    def test_open_port_multi_valve(self, flow_cell_system):
        flow_cell_system.open_port(1)
        assert flow_cell_system.get_current_port() == 1

        flow_cell_system.open_port(10)
        assert flow_cell_system.get_current_port() == 10

        flow_cell_system.open_port(19)
        assert flow_cell_system.get_current_port() == 19

    @pytest.mark.parametrize("port", [0, -1, 999])
    def test_open_port_out_of_range_raises_and_leaves_the_selection(
            self, open_chamber_system, port):
        """This used to be a silent no-op: the cascade kept the previous port
        selected and the next draw pulled the wrong reagent with nothing
        saying so. The pre-run check in sequences.py catches the typo first;
        this raise is the backstop for callers that bypass it."""
        open_chamber_system.open_port(5)
        with pytest.raises(ValueError, match=r"1\.\.10"):
            open_chamber_system.open_port(port)
        assert open_chamber_system.get_current_port() == 5

    def test_a_port_in_a_gap_is_refused_at_the_valve(self, open_chamber_system):
        """The backstop asks the same question the pre-run check asks. It
        gated on the cascade's reach, so a position nobody plumbed was
        rejected at time zero and accepted here -- and this is the last
        thing between a port number and a valve move."""
        sv = open_chamber_system.config.reagent_selection.selector_valves
        del sv.tubing_fluid_amount_ul["port_4"]
        open_chamber_system.open_port(5)
        with pytest.raises(ValueError, match=r"1\.\.3, 5\.\.10"):
            open_chamber_system.open_port(4)
        assert open_chamber_system.get_current_port() == 5

    def test_a_zero_tubing_volume_is_no_line_either(self, open_chamber_system):
        """Priming has always stepped over a zero volume. One rule, or the
        GUI offers a port that priming silently skips."""
        sv = open_chamber_system.config.reagent_selection.selector_valves
        sv.tubing_fluid_amount_ul["port_4"] = 0
        assert 4 not in [p for p, _ in open_chamber_system.get_port_names()]
        with pytest.raises(ValueError):
            open_chamber_system.open_port(4)


class TestACancelledRunMovesNoValve:
    """Port addressing walks the cascade valve by valve, so a cancel can land
    between two moves. Each move checks the run's signal before it sends, and
    every wait on the way through takes it (see wait_for_completion for why
    waiting one out is expensive)."""

    @pytest.fixture
    def system_and_commands(self, fixtures_dir):
        """A valve system whose outgoing rotary commands are recorded. The
        recording starts after construction, so the homing moves every valve
        makes on the way up are not counted."""
        control = RunControl()
        system = _make_valve_system(fixtures_dir / "flow_cell_config.yaml", control)
        sent = []
        original = system.fc.send_command

        def send_command(command, *args):
            if command == CMD_SET.SET_ROTARY_VALVE:
                sent.append(args)
            return original(command, *args)

        # Every valve holds this same controller, so one patch covers them all.
        system.fc.send_command = send_command
        return system, control, sent

    def test_a_cancelled_run_sends_no_command_at_all(self, system_and_commands):
        system, control, sent = system_and_commands
        control.cancel()
        with pytest.raises(AbortRequested):
            system.open_port(1)
        assert sent == []

    def test_a_cancel_between_moves_stops_the_cascade_before_the_next_command(
            self, system_and_commands):
        """Port 28 is on the last valve, so the first two are stepped through
        on the way. The cancel lands once the first move has completed -- the
        window an entry check alone would miss, since the next valve.open()
        would already have sent its command before its wait could raise."""
        system, control, sent = system_and_commands
        original = system.valves[0].open

        def open_then_cancel(port, run_control=None):
            original(port, run_control)
            control.cancel()          # the operator, between two moves

        system.valves[0].open = open_then_cancel
        with pytest.raises(AbortRequested):
            system.open_port(28)
        assert len(sent) == 1, "a cancelled run moved another valve"


class TestAPausedRunMovesNoValve:
    def test_the_move_waits_for_the_resume(self, fixtures_dir, holds_while_paused):
        """The valve in flight finishes; the next one holds at the gate."""
        control = RunControl()
        system = _make_valve_system(fixtures_dir / "flow_cell_config.yaml", control)
        holds_while_paused(control, lambda: system.open_port(1))
        assert system.current_port == 1
