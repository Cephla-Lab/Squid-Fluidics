# tests/unit/control/test_valve_address.py
import pytest

from fluidics.control.valve_address import (
    DEFAULT_VALVE_ADDRESSES, MAX_VALVES, is_valid_valve_address)


class TestValveAddressRules:
    @pytest.mark.parametrize("addr", [0x0E, 0x10, 0x1A, 0xFE])
    def test_the_idex_range_is_valid(self, addr):
        assert is_valid_valve_address(addr)

    @pytest.mark.parametrize("addr", [0x00, 0x0C, 0x0F, 0x11, 0xFF, 0x100, -2])
    def test_odd_or_out_of_range_is_invalid(self, addr):
        assert not is_valid_valve_address(addr)

    def test_the_defaults_are_six_valid_distinct_addresses(self):
        assert len(DEFAULT_VALVE_ADDRESSES) == MAX_VALVES == 6
        assert all(is_valid_valve_address(a) for a in DEFAULT_VALVE_ADDRESSES)
        assert len(set(DEFAULT_VALVE_ADDRESSES)) == MAX_VALVES


from fluidics.control.valve_address import conflicts_with_flow_sensor


class TestFlowSensorCollision:
    def test_0x10_is_the_flow_sensor_on_the_wire(self):
        assert conflicts_with_flow_sensor(0x10)

    @pytest.mark.parametrize("addr", [0x0E, 0x12, 0x14, 0x16, 0x18, 0x1A])
    def test_other_addresses_are_clear(self, addr):
        assert not conflicts_with_flow_sensor(addr)


from fluidics.control.valve_address import effective_address


class TestEffectiveAddress:
    def test_the_slot_default_without_a_configured_address(self):
        assert effective_address(1) == 0x10
        assert effective_address(1, {0: 0x1C}) == 0x10

    def test_a_configured_address_wins(self):
        assert effective_address(1, {1: 0x1A}) == 0x1A


from fluidics.control._def import CMD_SET, COMMAND_STATUS
from fluidics.control.valve_address import check_readdress_request, readdress_valve
from fluidics.errors import DeviceError

OK = COMMAND_STATUS.COMPLETED_WITHOUT_ERRORS


class FakeController:
    """Answers each blocking command with the next queued status."""

    def __init__(self, statuses, position=1):
        self.statuses = list(statuses)
        self.commands = []
        self.position = position

    def send_command_blocking(self, command, *args, timeout=30):
        self.commands.append((command,) + args)
        return self.statuses.pop(0)

    def get_mcu_status(self):
        return {"selector_valves_pos": [self.position, 0, 0, 0, 0, 0]}


class TestCheckReaddressRequest:
    @pytest.mark.parametrize("from_addr,to_addr,message", [
        (0x0F, 0x1A, "--from 0x0F is not a RheoLink address"),
        (0x10, 0x101, "--to 0x101 is not a RheoLink address"),
        (0x10, 0x10, "the same address"),
        (0x1A, 0x10, "the flow sensor's address"),
    ])
    def test_bad_requests_are_refused_before_any_command(self, from_addr, to_addr, message):
        with pytest.raises(ValueError, match=message):
            check_readdress_request(from_addr, to_addr)


class TestReaddressValve:
    def test_the_whole_exchange(self):
        fc = FakeController([OK, OK], position=1)
        prompts = []
        assert readdress_valve(fc, 0x10, 0x1A, 10, prompts.append) == 1
        assert fc.commands == [(CMD_SET.SET_ROTARY_ADDRESS, 0x10, 0x1A),
                               (CMD_SET.INITIALIZE_ROTARY, 0, 10, 0x1A)]
        assert len(prompts) == 1 and "power" in prompts[0]

    def test_old_firmware_is_told_to_reflash(self):
        fc = FakeController([COMMAND_STATUS.CMD_INVALID])
        with pytest.raises(DeviceError, match="reflash"):
            readdress_valve(fc, 0x10, 0x1A, 10, lambda _: None)

    def test_no_valve_at_from(self):
        fc = FakeController([COMMAND_STATUS.CMD_EXECUTION_ERROR])
        with pytest.raises(DeviceError, match="No valve answered at 0x10"):
            readdress_valve(fc, 0x10, 0x1A, 10, lambda _: None)

    def test_silent_after_the_power_cycle(self):
        fc = FakeController([OK, COMMAND_STATUS.CMD_EXECUTION_ERROR])
        with pytest.raises(DeviceError, match="does not answer at 0x1A"):
            readdress_valve(fc, 0x10, 0x1A, 10, lambda _: None)

    def test_an_answer_that_is_not_a_position(self):
        fc = FakeController([OK, OK], position=66)
        with pytest.raises(DeviceError, match="reports 66"):
            readdress_valve(fc, 0x10, 0x1A, 10, lambda _: None)


class TestReaddressScriptArguments:
    def test_hex_addresses_parse(self):
        import importlib.util, pathlib
        path = pathlib.Path(__file__).resolve().parents[3] / "readdress_valve.py"
        spec = importlib.util.spec_from_file_location("readdress_valve", path)
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        args = script.parse_args(["--from", "0x10", "--to", "0x1A"])
        assert (args.from_addr, args.to_addr, args.ports) == (0x10, 0x1A, 10)
