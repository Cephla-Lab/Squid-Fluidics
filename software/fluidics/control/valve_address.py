"""Selector-valve I2C addresses, judged the way the firmware judges them.

Addresses are the 8-bit write form that IDEX documents (RheoLink protocol
2321383F: factory address 0x0E; the 'N' command takes 0x0E-0xFE, even only)
and that firmware/_defs.h SELECTORVALVE_ADDRS stores; RheoLink::begin shifts
right by one for the Wire library. Every constant here mirrors the firmware;
tests/unit/control/test_firmware_mirror.py checks them.
"""

from ._def import CMD_SET, COMMAND_STATUS
from ..errors import DeviceError

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


def check_readdress_request(from_addr, to_addr):
    """Raise ValueError if moving a valve from from_addr to to_addr cannot be
    right, before anything is sent."""
    for flag, addr in (("--from", from_addr), ("--to", to_addr)):
        if not is_valid_valve_address(addr):
            raise ValueError(
                f"{flag} 0x{addr:02X} is not a RheoLink address: even, "
                f"0x{VALVE_ADDR_MIN:02X}-0x{VALVE_ADDR_MAX:02X}")
    if from_addr == to_addr:
        raise ValueError("--from and --to are the same address")
    if conflicts_with_flow_sensor(to_addr):
        raise ValueError(
            f"0x{to_addr:02X} is the flow sensor's address on the wire; pick another")


def readdress_valve(fc, from_addr, to_addr, ports, power_cycle):
    """Move the one connected valve from from_addr to to_addr and prove it.

    fc is a started FluidController. power_cycle(message) must return once
    the operator has reset the valve: IDEX applies a new address only after
    its driver board resets (RheoLink protocol 2321383F). Returns the
    position the valve reports at its new address.
    """
    check_readdress_request(from_addr, to_addr)

    status = fc.send_command_blocking(CMD_SET.SET_ROTARY_ADDRESS, from_addr, to_addr)
    if status == COMMAND_STATUS.CMD_INVALID:
        raise DeviceError(
            "The controller refused the re-address: it is busy, a running valve "
            f"already uses 0x{to_addr:02X}, or its firmware predates "
            "SET_ROTARY_ADDRESS -- reflash with `pio run -t upload`.")
    if status != COMMAND_STATUS.COMPLETED_WITHOUT_ERRORS:
        raise DeviceError(
            f"No valve answered at 0x{from_addr:02X} -- check its signal cable, "
            "its 24 V power, and its current address.")

    power_cycle(
        f"The valve accepted 0x{to_addr:02X}. Unplug its 24 V power cable, "
        "wait 5 s, plug it back in, then press Enter.")

    status = fc.send_command_blocking(CMD_SET.INITIALIZE_ROTARY, 0, ports, to_addr)
    if status != COMMAND_STATUS.COMPLETED_WITHOUT_ERRORS:
        raise DeviceError(
            f"The valve does not answer at 0x{to_addr:02X} after the power cycle "
            f"(MCU status {status}).")
    position = fc.get_mcu_status()["selector_valves_pos"][0]
    if not 1 <= position <= ports:
        raise DeviceError(
            f"The valve answers at 0x{to_addr:02X} but reports {position}, not a "
            f"position in 1-{ports}.")
    return position
