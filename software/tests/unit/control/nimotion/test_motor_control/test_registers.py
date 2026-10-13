"""
Register definition tests

Addresses are checked against the object dictionary of the NiMotion Modbus
manual, revision B06 (documents/NiMotion_Modbus_Communication_Manual_B06_zh.pdf,
chapter 10). There is no formula from object index to Modbus address.
"""

import re

import pytest

from fluidics.control.nimotion.motor_control.registers import RegisterDef, Registers

# (register, Modbus address, object index, sub-index) per manual B06
MANUAL_ADDRESSES = [
    (Registers.CONTROL_WORD, 0x0380, 0x6040, 0x00),
    (Registers.STATUS_WORD, 0x0381, 0x6041, 0x00),
    (Registers.ERROR_CODE, 0x037F, 0x603F, 0x00),  # 0x0382 is 6042h (VM target velocity)
    (Registers.MODES_OF_OPERATION, 0x03C2, 0x6060, 0x00),
    (Registers.MODES_OF_OPERATION_DISPLAY, 0x03C3, 0x6061, 0x00),
    (Registers.POSITION_ACTUAL_VALUE, 0x03C8, 0x6064, 0x00),
    (Registers.VELOCITY_ACTUAL_VALUE, 0x03D5, 0x606C, 0x00),
    (Registers.TARGET_POSITION, 0x03E7, 0x607A, 0x00),
    (Registers.PROFILE_VELOCITY, 0x03F8, 0x6081, 0x00),
    (Registers.PROFILE_ACCELERATION, 0x03FC, 0x6083, 0x00),
    (Registers.PROFILE_DECELERATION, 0x03FE, 0x6084, 0x00),
    (Registers.QUICK_STOP_DECELERATION, 0x0400, 0x6085, 0x00),
    (Registers.MAX_PROFILE_VELOCITY, 0x03F4, 0x607F, 0x00),
    (Registers.MAX_ACCELERATION, 0x043B, 0x60C5, 0x00),
    (Registers.MAX_DECELERATION, 0x043D, 0x60C6, 0x00),
    (Registers.ENCODER_RESOLUTION_NUM, 0x0406, 0x608F, 0x01),
    (Registers.ENCODER_RESOLUTION_DEN, 0x0408, 0x608F, 0x02),
    (Registers.GEAR_RATIO_NUM, 0x040E, 0x6091, 0x01),
    (Registers.GEAR_RATIO_DEN, 0x0410, 0x6091, 0x02),
    (Registers.HOMING_METHOD, 0x0416, 0x6098, 0x00),
    (Registers.HOMING_SPEED_HIGH, 0x0417, 0x6099, 0x01),
    (Registers.HOMING_SPEED_LOW, 0x0419, 0x6099, 0x02),
    (Registers.HOMING_ACCELERATION, 0x041B, 0x609A, 0x00),
    (Registers.DIGITAL_INPUTS, 0x044C, 0x60FD, 0x00),
    (Registers.DIGITAL_OUTPUTS, 0x044E, 0x60FE, 0x01),
    (Registers.TARGET_VELOCITY, 0x0448, 0x60FF, 0x00),
]


@pytest.mark.parametrize(
    "reg, address, index, subindex",
    MANUAL_ADDRESSES,
    ids=[r[0].name for r in MANUAL_ADDRESSES],
)
def test_address_matches_manual(reg: RegisterDef, address: int, index: int, subindex: int) -> None:
    assert (reg.address, reg.object_index, reg.object_subindex) == (address, index, subindex)


# Objects the T5 tilt work adds, per the PMM60L/STM-M/BLM/TSM Modbus manual B03
# (chapter 10; 2005h:18h and 1Dh from section 6.7, which the dictionary omits)
STEPPER_ADDRESSES = [
    (Registers.POSITION_ACTUAL_INTERNAL, 0x03C6, 0x6063, 0x00),
    (Registers.POSITION_RANGE_MIN, 0x03E9, 0x607B, 0x01),
    (Registers.POSITION_RANGE_MAX, 0x03EB, 0x607B, 0x02),
    (Registers.HOME_OFFSET, 0x03ED, 0x607C, 0x00),
    (Registers.SOFTWARE_LIMIT_MIN, 0x03EF, 0x607D, 0x01),
    (Registers.SOFTWARE_LIMIT_MAX, 0x03F1, 0x607D, 0x02),
    (Registers.POSITION_RESTORE_MODE, 0x012A, 0x2005, 0x18),
    (Registers.RESTORE_BLOCKING_POSITION, 0x012F, 0x2005, 0x1D),
    (Registers.ENCODER_BITS, 0x0075, 0x2000, 0x19),
    (Registers.UNDER_VOLTAGE_POINT, 0x0094, 0x2001, 0x10),
    (Registers.OVER_VOLTAGE_POINT, 0x0092, 0x2001, 0x0E),
    (Registers.FIRMWARE_VERSION, 0x0053, 0x1018, 0x03),
]


@pytest.mark.parametrize(
    "reg, address, index, subindex",
    STEPPER_ADDRESSES,
    ids=[r[0].name for r in STEPPER_ADDRESSES],
)
def test_stepper_address_matches_manual(
    reg: RegisterDef, address: int, index: int, subindex: int
) -> None:
    assert (reg.address, reg.object_index, reg.object_subindex) == (address, index, subindex)


def test_addresses_are_unique() -> None:
    defs = [reg for reg in vars(Registers).values() if isinstance(reg, RegisterDef)]
    assert len({reg.address for reg in defs}) == len(defs)
