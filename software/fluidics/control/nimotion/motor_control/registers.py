"""
NiMotion servo motor register definitions

Register addresses defined based on the CiA402 standard and the NiMotion
servo motor manual. Register addresses use the Modbus mapping format.
"""

from dataclasses import dataclass
from enum import IntEnum
from typing import Dict, Optional


class RegisterAccess(IntEnum):
    """Register access type"""

    READ_ONLY = 0
    READ_WRITE = 1
    WRITE_ONLY = 2


@dataclass
class RegisterDef:
    """Register definition"""

    address: int
    """Modbus address"""

    name: str
    """Register name"""

    description: str
    """Description"""

    size: int = 1
    """Number of registers (1=16-bit, 2=32-bit)"""

    access: RegisterAccess = RegisterAccess.READ_WRITE
    """Access type"""

    signed: bool = False
    """Whether the value is signed"""

    object_index: Optional[int] = None
    """Object index (CiA402)"""

    object_subindex: Optional[int] = None
    """Object sub-index"""


class Registers:
    """
    NiMotion servo motor register address definitions

    There is no formula from object index to Modbus address; every address
    here must match the object dictionary in the NiMotion Modbus manual
    (documents/, revision B06, chapter 10).
    """

    # ==================== Control mode ====================

    # Control mode selection (2002h:01h): 0 = CiA402; 1-4 = NiMotion modes
    CONTROL_MODE = RegisterDef(
        address=0x00B1,
        name="ControlMode",
        description="Control mode selection (0 = CiA402)",
        object_index=0x2002,
        object_subindex=0x01,
    )

    # ==================== CiA402 control registers ====================

    # Control word (6040h)
    CONTROL_WORD = RegisterDef(
        address=0x0380,
        name="ControlWord",
        description="Control word",
        object_index=0x6040,
        object_subindex=0x00,
    )

    # Status word (6041h)
    STATUS_WORD = RegisterDef(
        address=0x0381,
        name="StatusWord",
        description="Status word",
        access=RegisterAccess.READ_ONLY,
        object_index=0x6041,
        object_subindex=0x00,
    )

    # Modes of operation setting (6060h)
    # Note: Per the NiMotion manual, 6060h maps to Modbus address 0x03C2
    MODES_OF_OPERATION = RegisterDef(
        address=0x03C2,
        name="ModesOfOperation",
        description="Modes of operation setting",
        object_index=0x6060,
        object_subindex=0x00,
    )

    # Modes of operation display (6061h)
    # Note: Per the NiMotion manual, 6061h maps to Modbus address 0x03C3
    MODES_OF_OPERATION_DISPLAY = RegisterDef(
        address=0x03C3,
        name="ModesOfOperationDisplay",
        description="Modes of operation display",
        access=RegisterAccess.READ_ONLY,
        object_index=0x6061,
        object_subindex=0x00,
    )

    # ==================== Position control registers ====================

    # Target position (607Ah) - 32-bit
    # Per the NiMotion manual: 607Ah → Modbus 0x03E7
    TARGET_POSITION = RegisterDef(
        address=0x03E7,
        name="TargetPosition",
        description="Target position",
        size=2,
        signed=True,
        object_index=0x607A,
        object_subindex=0x00,
    )

    # Actual position (6064h) - 32-bit
    # Per the NiMotion manual: 6064h → Modbus 0x03C8
    POSITION_ACTUAL_VALUE = RegisterDef(
        address=0x03C8,
        name="PositionActualValue",
        description="Actual position",
        size=2,
        access=RegisterAccess.READ_ONLY,
        signed=True,
        object_index=0x6064,
        object_subindex=0x00,
    )

    # Position following error (60F4h) - 32-bit
    # Per the NiMotion manual: 60F4h → Modbus 0x0440
    FOLLOWING_ERROR_ACTUAL = RegisterDef(
        address=0x0440,
        name="FollowingErrorActual",
        description="Position following error",
        size=2,
        access=RegisterAccess.READ_ONLY,
        signed=True,
        object_index=0x60F4,
        object_subindex=0x00,
    )

    # ==================== Velocity control registers ====================

    # Target velocity (60FFh) - 32-bit
    # Per the NiMotion manual: 60FFh → Modbus 0x0448
    TARGET_VELOCITY = RegisterDef(
        address=0x0448,
        name="TargetVelocity",
        description="Target velocity",
        size=2,
        signed=True,
        object_index=0x60FF,
        object_subindex=0x00,
    )

    # Actual velocity (606Ch) - 32-bit
    # Per the NiMotion manual: 606Ch → Modbus 0x03D5
    VELOCITY_ACTUAL_VALUE = RegisterDef(
        address=0x03D5,
        name="VelocityActualValue",
        description="Actual velocity",
        size=2,
        access=RegisterAccess.READ_ONLY,
        signed=True,
        object_index=0x606C,
        object_subindex=0x00,
    )

    # Profile velocity (6081h) - 32-bit
    # Per the NiMotion manual: 6081h → Modbus 0x03F8
    PROFILE_VELOCITY = RegisterDef(
        address=0x03F8,
        name="ProfileVelocity",
        description="Profile velocity",
        size=2,
        object_index=0x6081,
        object_subindex=0x00,
    )

    # ==================== Acceleration/deceleration registers ====================

    # Profile acceleration (6083h) - 32-bit
    # Per the NiMotion manual: 6083h → Modbus 0x03FC
    PROFILE_ACCELERATION = RegisterDef(
        address=0x03FC,
        name="ProfileAcceleration",
        description="Profile acceleration",
        size=2,
        object_index=0x6083,
        object_subindex=0x00,
    )

    # Profile deceleration (6084h) - 32-bit
    # Per the NiMotion manual: 6084h → Modbus 0x03FE
    PROFILE_DECELERATION = RegisterDef(
        address=0x03FE,
        name="ProfileDeceleration",
        description="Profile deceleration",
        size=2,
        object_index=0x6084,
        object_subindex=0x00,
    )

    # Quick stop deceleration (6085h) - 32-bit
    # Per the NiMotion manual: 6085h → Modbus 0x0400
    QUICK_STOP_DECELERATION = RegisterDef(
        address=0x0400,
        name="QuickStopDeceleration",
        description="Quick stop deceleration",
        size=2,
        object_index=0x6085,
        object_subindex=0x00,
    )

    # ==================== Motion limits ====================
    # B06 5.3: 6081h is limited by 607Fh, 6083h by 60C5h, 6084h by 60C6h, and
    # 500000 is the factory setting of all three. The X stage's drive capped
    # 6083h-6085h when they were written, but kept a 6081h above its 607Fh.

    # Max profile velocity (607Fh) - 32-bit
    MAX_PROFILE_VELOCITY = RegisterDef(
        address=0x03F4,
        name="MaxProfileVelocity",
        description="Max profile velocity",
        size=2,
        object_index=0x607F,
        object_subindex=0x00,
    )

    # Max acceleration (60C5h) - 32-bit
    MAX_ACCELERATION = RegisterDef(
        address=0x043B,
        name="MaxAcceleration",
        description="Max acceleration",
        size=2,
        object_index=0x60C5,
        object_subindex=0x00,
    )

    # Max deceleration (60C6h) - 32-bit
    MAX_DECELERATION = RegisterDef(
        address=0x043D,
        name="MaxDeceleration",
        description="Max deceleration",
        size=2,
        object_index=0x60C6,
        object_subindex=0x00,
    )

    # ==================== Torque control registers ====================

    # Target torque (6071h)
    # Per the NiMotion manual: 6071h → Modbus 0x03DB
    TARGET_TORQUE = RegisterDef(
        address=0x03DB,
        name="TargetTorque",
        description="Target torque",
        signed=True,
        object_index=0x6071,
        object_subindex=0x00,
    )

    # Actual torque (6077h)
    # Per the NiMotion manual: 6077h → Modbus 0x03E3
    TORQUE_ACTUAL_VALUE = RegisterDef(
        address=0x03E3,
        name="TorqueActualValue",
        description="Actual torque",
        access=RegisterAccess.READ_ONLY,
        signed=True,
        object_index=0x6077,
        object_subindex=0x00,
    )

    # ==================== Homing control registers ====================

    # Homing method (6098h)
    # Per the NiMotion manual: 6098h → Modbus 0x0416
    HOMING_METHOD = RegisterDef(
        address=0x0416,
        name="HomingMethod",
        description="Homing method",
        signed=True,
        object_index=0x6098,
        object_subindex=0x00,
    )

    # Homing high speed (6099h:01) - 32-bit
    # Per the NiMotion manual: 6099h:01 → Modbus 0x0417
    HOMING_SPEED_HIGH = RegisterDef(
        address=0x0417,
        name="HomingSpeedHigh",
        description="Homing high speed (search for switch)",
        size=2,
        object_index=0x6099,
        object_subindex=0x01,
    )

    # Homing low speed (6099h:02) - 32-bit
    # Per the NiMotion manual: 6099h:02 → Modbus 0x0419
    HOMING_SPEED_LOW = RegisterDef(
        address=0x0419,
        name="HomingSpeedLow",
        description="Homing low speed (search for home)",
        size=2,
        signed=True,
        object_index=0x6099,
        object_subindex=0x02,
    )

    # Homing acceleration (609Ah) - 32-bit
    # Per the NiMotion manual: 609Ah → Modbus 0x041B
    HOMING_ACCELERATION = RegisterDef(
        address=0x041B,
        name="HomingAcceleration",
        description="Homing acceleration",
        size=2,
        object_index=0x609A,
        object_subindex=0x00,
    )

    # Homing timeout (2005h:1Ch)
    # Per the NiMotion manual: 2005h:1Ch → Modbus 0x012E
    # Important: a default value of 0 causes homing to time out immediately!
    HOMING_TIMEOUT = RegisterDef(
        address=0x012E,
        name="HomingTimeout",
        description="Homing timeout (ms)",
        object_index=0x2005,
        object_subindex=0x1C,
    )

    # Blocking homing torque threshold (2007h:13h)
    # Per the NiMotion manual: 2007h:13h → Modbus 0x0170
    BLOCKING_TORQUE = RegisterDef(
        address=0x0170,
        name="BlockingTorque",
        description="Blocking homing torque threshold (0.1% units)",
        object_index=0x2007,
        object_subindex=0x13,
    )

    # Blocking detection time (2007h:15h)
    # Per the NiMotion manual: 2007h:15h → Modbus 0x0172
    BLOCKING_TIME = RegisterDef(
        address=0x0172,
        name="BlockingTime",
        description="Blocking detection time (ms)",
        object_index=0x2007,
        object_subindex=0x15,
    )

    # ==================== DI configuration registers ====================

    # DI1 function (2003h:03h)
    DI1_FUNCTION = RegisterDef(
        address=0x00D5,
        name="DI1Function",
        description="DI1 function number",
        object_index=0x2003,
        object_subindex=0x03,
    )

    # DI1 logic (2003h:04h)
    DI1_LOGIC = RegisterDef(
        address=0x00D6,
        name="DI1Logic",
        description="DI1 logic (0=active low, 1=active high)",
        object_index=0x2003,
        object_subindex=0x04,
    )

    # DI2 function (2003h:05h)
    DI2_FUNCTION = RegisterDef(
        address=0x00D7,
        name="DI2Function",
        description="DI2 function number",
        object_index=0x2003,
        object_subindex=0x05,
    )

    # DI2 logic (2003h:06h)
    DI2_LOGIC = RegisterDef(
        address=0x00D8,
        name="DI2Logic",
        description="DI2 logic",
        object_index=0x2003,
        object_subindex=0x06,
    )

    # DI3 function (2003h:07h)
    DI3_FUNCTION = RegisterDef(
        address=0x00D9,
        name="DI3Function",
        description="DI3 function number",
        object_index=0x2003,
        object_subindex=0x07,
    )

    # DI3 logic (2003h:08h)
    DI3_LOGIC = RegisterDef(
        address=0x00DA,
        name="DI3Logic",
        description="DI3 logic",
        object_index=0x2003,
        object_subindex=0x08,
    )

    # DI physical state monitor (200Bh:05h)
    DI_PHYSICAL_STATE = RegisterDef(
        address=0x01E2,
        name="DIPhysicalState",
        description="DI physical state (bit0=DI1, bit1=DI2, bit2=DI3)",
        access=RegisterAccess.READ_ONLY,
        object_index=0x200B,
        object_subindex=0x05,
    )

    # ==================== DO configuration registers ====================

    # DO1 function (2004h:01h)
    DO1_FUNCTION = RegisterDef(
        address=0x00F8,
        name="DO1Function",
        description="DO1 function number (0=input, 1=output, 5=brake)",
        object_index=0x2004,
        object_subindex=0x01,
    )

    # DO1 logic (2004h:02h)
    DO1_LOGIC = RegisterDef(
        address=0x00F9,
        name="DO1Logic",
        description="DO1 logic (0=active low, 1=active high)",
        object_index=0x2004,
        object_subindex=0x02,
    )

    # DO physical state monitor (200Bh:06h)
    DO_PHYSICAL_STATE = RegisterDef(
        address=0x01E3,
        name="DOPhysicalState",
        description="DO physical state (bit0=DO1)",
        access=RegisterAccess.READ_ONLY,
        object_index=0x200B,
        object_subindex=0x06,
    )

    # Communication DO control (2031h:02h)
    DO_CONTROL = RegisterDef(
        address=0x0374,
        name="DOControl",
        description="Communication DO output control (bit0=DO1)",
        object_index=0x2031,
        object_subindex=0x02,
    )

    # ==================== Digital inputs/outputs ====================

    # Digital inputs (60FDh)
    # Per the NiMotion manual: 60FDh → Modbus 0x0447
    DIGITAL_INPUTS = RegisterDef(
        address=0x044C,
        name="DigitalInputs",
        description="Digital input state",
        size=2,
        access=RegisterAccess.READ_ONLY,
        object_index=0x60FD,
        object_subindex=0x00,
    )

    # Digital outputs (60FEh)
    # Per the NiMotion manual: 60FEh → Modbus 0x0449
    DIGITAL_OUTPUTS = RegisterDef(
        address=0x044E,
        name="DigitalOutputs",
        description="Physical output state",
        size=2,
        access=RegisterAccess.READ_ONLY,
        object_index=0x60FE,
        object_subindex=0x01,
    )

    # ==================== Parameter storage / supply ====================

    # Save all parameters (1010h:01): write 0x65766173 ("save"); reads 1 when done
    SAVE_PARAMETERS = RegisterDef(
        address=0x0026,
        name="SaveParameters",
        description="Save all parameters to flash",
        size=2,
        object_index=0x1010,
        object_subindex=0x01,
    )

    # Rated voltage (2000h:07h), 1 V
    RATED_VOLTAGE = RegisterDef(
        address=0x0063,
        name="RatedVoltage",
        description="Rated supply voltage (V)",
        object_index=0x2000,
        object_subindex=0x07,
    )

    # DC bus voltage (200Bh:15h), 0.1 V
    BUS_VOLTAGE = RegisterDef(
        address=0x01F7,
        name="BusVoltage",
        description="DC bus voltage (0.1 V)",
        access=RegisterAccess.READ_ONLY,
        object_index=0x200B,
        object_subindex=0x15,
    )

    # ==================== Error code ====================

    # Error code (603Fh)
    ERROR_CODE = RegisterDef(
        address=0x037F,
        name="ErrorCode",
        description="Error code",
        access=RegisterAccess.READ_ONLY,
        object_index=0x603F,
        object_subindex=0x00,
    )

    # ==================== Communication configuration ====================

    # Slave address (200Ch:02)
    SLAVE_ADDRESS = RegisterDef(
        address=0x0230,
        name="SlaveAddress",
        description="Modbus slave address",
        object_index=0x200C,
        object_subindex=0x02,
    )

    # Baud rate (200Ch:03)
    BAUDRATE = RegisterDef(
        address=0x0231,
        name="Baudrate",
        description="Communication baud rate",
        object_index=0x200C,
        object_subindex=0x03,
    )

    # ==================== Encoder configuration ====================

    # Encoder resolution - increment (608Fh:01) - 32-bit
    # Per the NiMotion manual: 608Fh:01 → Modbus 0x0406
    ENCODER_RESOLUTION_NUM = RegisterDef(
        address=0x0406,
        name="EncoderResolutionNum",
        description="Encoder resolution numerator (encoder_increment)",
        size=2,
        object_index=0x608F,
        object_subindex=0x01,
    )

    # Encoder resolution - motor turns (608Fh:02) - 32-bit
    # Per the NiMotion manual: 608Fh:02 → Modbus 0x0408
    ENCODER_RESOLUTION_DEN = RegisterDef(
        address=0x0408,
        name="EncoderResolutionDen",
        description="Encoder resolution denominator (motor_turns)",
        size=2,
        object_index=0x608F,
        object_subindex=0x02,
    )

    # Gear ratio - motor turns (6091h:01) - 32-bit
    # Per the NiMotion manual: 6091h:01 → Modbus 0x040C
    GEAR_RATIO_NUM = RegisterDef(
        address=0x040E,
        name="GearRatioNum",
        description="Gear ratio numerator (motor_revolutions)",
        size=2,
        object_index=0x6091,
        object_subindex=0x01,
    )

    # Gear ratio - shaft turns (6091h:02) - 32-bit
    # Per the NiMotion manual: 6091h:02 → Modbus 0x040E
    GEAR_RATIO_DEN = RegisterDef(
        address=0x0410,
        name="GearRatioDen",
        description="Gear ratio denominator (shaft_revolutions)",
        size=2,
        object_index=0x6091,
        object_subindex=0x02,
    )

    # Polarity (607Eh) - 16-bit
    # Per the NiMotion manual: 607Eh → Modbus 0x03F3
    # Bit7: position polarity (0=forward, 1=reverse)
    # Bit6: velocity polarity (0=forward, 1=reverse)
    POSITION_WINDOW = RegisterDef(
        address=0x03CD,
        name="PositionWindow",
        description="Position window: target reached when within this many user units",
        size=2,
        object_index=0x6067,
        object_subindex=0x00,
    )

    POLARITY = RegisterDef(
        address=0x03F3,
        name="Polarity",
        description="Polarity setting (bit7=position, bit6=velocity)",
        size=1,
        object_index=0x607E,
        object_subindex=0x00,
    )

    # ==================== Stepper bring-up (T5 tilt, manual B03) ====================

    # Absolute position in encoder units (6063h); 6064h x 6091h = 6063h
    POSITION_ACTUAL_INTERNAL = RegisterDef(
        address=0x03C6,
        name="PositionActualInternal",
        description="Absolute position (encoder units)",
        size=2,
        access=RegisterAccess.READ_ONLY,
        signed=True,
        object_index=0x6063,
        object_subindex=0x00,
    )

    POSITION_RANGE_MIN = RegisterDef(
        address=0x03E9,
        name="PositionRangeMin",
        description="Position range limit, minimum (user units)",
        size=2,
        signed=True,
        object_index=0x607B,
        object_subindex=0x01,
    )

    POSITION_RANGE_MAX = RegisterDef(
        address=0x03EB,
        name="PositionRangeMax",
        description="Position range limit, maximum (user units)",
        size=2,
        signed=True,
        object_index=0x607B,
        object_subindex=0x02,
    )

    # Home offset (607Ch): where homing puts 0; method 35 makes "here" read this value
    HOME_OFFSET = RegisterDef(
        address=0x03ED,
        name="HomeOffset",
        description="Home offset (user units)",
        size=2,
        signed=True,
        object_index=0x607C,
        object_subindex=0x00,
    )

    # Software position limits (607Dh); exceeding one raises 0x8613, which pauses
    # and locks the axis without entering the Fault state (B03 table 8-1)
    SOFTWARE_LIMIT_MIN = RegisterDef(
        address=0x03EF,
        name="SoftwareLimitMin",
        description="Software position limit, minimum (user units)",
        size=2,
        signed=True,
        object_index=0x607D,
        object_subindex=0x01,
    )

    SOFTWARE_LIMIT_MAX = RegisterDef(
        address=0x03F1,
        name="SoftwareLimitMax",
        description="Software position limit, maximum (user units)",
        size=2,
        signed=True,
        object_index=0x607D,
        object_subindex=0x02,
    )

    # Position restore after power-up (2005h:18h, B03 6.7; not in the object
    # dictionary): 0 multi-turn absolute, 1 within a single turn
    POSITION_RESTORE_MODE = RegisterDef(
        address=0x012A,
        name="PositionRestoreMode",
        description="Position restore: 0 multi-turn, 1 single turn",
        object_index=0x2005,
        object_subindex=0x18,
    )

    # Cut point of single-turn restore (2005h:1Dh, B03 6.7); size and units unconfirmed
    RESTORE_BLOCKING_POSITION = RegisterDef(
        address=0x012F,
        name="RestoreBlockingPosition",
        description="Single-turn restore blocking position (cut point)",
        object_index=0x2005,
        object_subindex=0x1D,
    )

    ENCODER_BITS = RegisterDef(
        address=0x0075,
        name="EncoderBits",
        description="Encoder absolute bits",
        size=2,
        object_index=0x2000,
        object_subindex=0x19,
    )

    UNDER_VOLTAGE_POINT = RegisterDef(
        address=0x0094,
        name="UnderVoltagePoint",
        description="DC bus under-voltage point (1 V)",
        object_index=0x2001,
        object_subindex=0x10,
    )

    OVER_VOLTAGE_POINT = RegisterDef(
        address=0x0092,
        name="OverVoltagePoint",
        description="DC bus over-voltage point (1 V)",
        object_index=0x2001,
        object_subindex=0x0E,
    )

    FIRMWARE_VERSION = RegisterDef(
        address=0x0053,
        name="FirmwareVersion",
        description="Firmware version",
        size=2,
        access=RegisterAccess.READ_ONLY,
        object_index=0x1018,
        object_subindex=0x03,
    )


class ControlWordBits:
    """Control word bit definitions"""

    SWITCH_ON = 0x0001  # Bit 0: switch on
    ENABLE_VOLTAGE = 0x0002  # Bit 1: enable voltage
    QUICK_STOP = 0x0004  # Bit 2: quick stop (0=active)
    ENABLE_OPERATION = 0x0008  # Bit 3: enable operation
    NEW_SET_POINT = 0x0010  # Bit 4: new set-point / PP mode start
    CHANGE_SET_IMMEDIATELY = 0x0020  # Bit 5: change set-point immediately
    ABS_REL = 0x0040  # Bit 6: absolute/relative position
    FAULT_RESET = 0x0080  # Bit 7: fault reset
    HALT = 0x0100  # Bit 8: halt

    # Control word command combinations
    CMD_SHUTDOWN = 0x0006  # Shutdown
    CMD_SWITCH_ON = 0x0007  # Switch on
    CMD_ENABLE_OPERATION = 0x000F  # Enable operation
    CMD_DISABLE_OPERATION = 0x0007  # Disable operation
    CMD_DISABLE_VOLTAGE = 0x0000  # Disable voltage
    CMD_QUICK_STOP = 0x0002  # Quick stop


class StatusWordBits:
    """Status word bit definitions"""

    READY_TO_SWITCH_ON = 0x0001  # Bit 0: ready to switch on
    SWITCHED_ON = 0x0002  # Bit 1: switched on
    OPERATION_ENABLED = 0x0004  # Bit 2: operation enabled
    FAULT = 0x0008  # Bit 3: fault
    VOLTAGE_ENABLED = 0x0010  # Bit 4: voltage enabled
    QUICK_STOP = 0x0020  # Bit 5: quick stop active
    SWITCH_ON_DISABLED = 0x0040  # Bit 6: switch on disabled
    WARNING = 0x0080  # Bit 7: warning
    REMOTE = 0x0200  # Bit 9: remote control
    TARGET_REACHED = 0x0400  # Bit 10: target reached
    INTERNAL_LIMIT_ACTIVE = 0x0800  # Bit 11: internal limit active
    SET_POINT_ACK = 0x1000  # Bit 12: set-point acknowledge (PP mode)
    FOLLOWING_ERROR = 0x2000  # Bit 13: following error

    # The meaning of bits 12/13 varies with the operation mode (CiA 402):
    #   PP mode: bit12=set-point acknowledge, bit13=following error
    #   HM mode: bit12=homing attained, bit13=homing error
    HOMING_ATTAINED = 0x1000  # Bit 12 (in homing mode): homing attained
    HOMING_ERROR = 0x2000  # Bit 13 (in homing mode): homing error

    # State machine state mask
    STATE_MASK = 0x006F  # Bits 0-3, 5, 6

    # State machine state values
    STATE_NOT_READY_TO_SWITCH_ON = 0x0000
    STATE_SWITCH_ON_DISABLED = 0x0040
    STATE_READY_TO_SWITCH_ON = 0x0021
    STATE_SWITCHED_ON = 0x0023
    STATE_OPERATION_ENABLED = 0x0027
    STATE_QUICK_STOP_ACTIVE = 0x0007
    STATE_FAULT_REACTION_ACTIVE = 0x000F
    STATE_FAULT = 0x0008


class OperationMode(IntEnum):
    """Operation mode definitions (6060h)"""

    PROFILE_POSITION = 1  # PP: profile position mode
    VELOCITY = 2  # VL: velocity mode
    PROFILE_VELOCITY = 3  # PV: profile velocity mode
    PROFILE_TORQUE = 4  # PT: profile torque mode
    HOMING = 6  # HM: homing mode
    INTERPOLATED_POSITION = 7  # IP: interpolated position mode
    CYCLIC_SYNC_POSITION = 8  # CSP: cyclic synchronous position mode
    CYCLIC_SYNC_VELOCITY = 9  # CSV: cyclic synchronous velocity mode
    CYCLIC_SYNC_TORQUE = 10  # CST: cyclic synchronous torque mode


class HomingMethod(IntEnum):
    """
    Homing methods supported by the drive (6098h)

    NiMotion Modbus manual B06, section 5.7.4, tables 5-24 and 5-25. Methods
    19-30 follow CiA402; after homing the drive moves to the zero position.
    """

    NEGATIVE_LIMIT_SWITCH = 17
    POSITIVE_LIMIT_SWITCH = 18

    # Home switch only (19, 20 start in the positive direction; 21, 22 negative)
    HOME_SWITCH_19 = 19
    HOME_SWITCH_20 = 20
    HOME_SWITCH_21 = 21
    HOME_SWITCH_22 = 22

    # Home switch + positive limit switch
    HOME_SWITCH_POSITIVE_LIMIT_23 = 23
    HOME_SWITCH_POSITIVE_LIMIT_24 = 24
    HOME_SWITCH_POSITIVE_LIMIT_25 = 25
    HOME_SWITCH_POSITIVE_LIMIT_26 = 26

    # Home switch + negative limit switch
    HOME_SWITCH_NEGATIVE_LIMIT_27 = 27
    HOME_SWITCH_NEGATIVE_LIMIT_28 = 28
    HOME_SWITCH_NEGATIVE_LIMIT_29 = 29
    HOME_SWITCH_NEGATIVE_LIMIT_30 = 30

    CURRENT_POSITION_AS_HOME = 35

    # Blocking (stall) detection at the 6099h:02 speed; threshold 2007h:13h,
    # detection time 2007h:15h
    BLOCKING_POSITIVE = 37
    BLOCKING_NEGATIVE = 38


# Register address index
REGISTER_MAP: Dict[int, RegisterDef] = {
    reg.address: reg for name, reg in Registers.__dict__.items() if isinstance(reg, RegisterDef)
}
