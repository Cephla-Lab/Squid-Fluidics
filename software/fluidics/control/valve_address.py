"""Selector-valve I2C addresses, judged the way the firmware judges them.

Addresses are the 8-bit write form that IDEX documents (RheoLink protocol
2321383F: factory address 0x0E; the 'N' command takes 0x0E-0xFE, even only)
and that firmware/_defs.h SELECTORVALVE_ADDRS stores; RheoLink::begin shifts
right by one for the Wire library. Every constant here mirrors the firmware;
tests/unit/control/test_firmware_mirror.py checks them.
"""

# Mirrors SELECTORVALVE_ADDRS (firmware/_defs.h): slot i's address when the
# config gives none.
DEFAULT_VALVE_ADDRESSES = (0x0E, 0x10, 0x12, 0x14, 0x16, 0x18)
# Mirrors SELECTORVALVE_MAX.
MAX_VALVES = len(DEFAULT_VALVE_ADDRESSES)
# Mirror VALVE_ADDR_MIN / VALVE_ADDR_MAX (firmware/bus_rules.h).
VALVE_ADDR_MIN = 0x0E
VALVE_ADDR_MAX = 0xFE


def is_valid_valve_address(addr):
    """Even and within IDEX's range -- what the 'N' command accepts."""
    return VALVE_ADDR_MIN <= addr <= VALVE_ADDR_MAX and addr % 2 == 0


# Mirrors SLF3X_ADDRESS (firmware/SLF3X.h): the flow sensor's fixed 7-bit
# address. A sensor on J20 shares the valves' bus.
FLOW_SENSOR_ADDRESS = 0x08


def conflicts_with_flow_sensor(addr):
    """True when a valve at `addr` puts the flow sensor's address on the wire
    (0x10 >> 1 == 0x08)."""
    return (addr >> 1) == FLOW_SENSOR_ADDRESS


def effective_address(valve_id, i2c_addresses=None):
    """The address valve `valve_id` is driven at: its configured one, else
    its slot's default."""
    if i2c_addresses and valve_id in i2c_addresses:
        return i2c_addresses[valve_id]
    return DEFAULT_VALVE_ADDRESSES[valve_id]
