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
