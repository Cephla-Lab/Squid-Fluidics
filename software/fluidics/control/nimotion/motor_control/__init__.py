"""
Motor control module

Provides servo motor control functionality, including the state machine,
register definitions, motion control, and more.
"""

from .motor import (
    LimitSwitchError,
    MotionInterruptedError,
    Motor,
    MotorError,
    MotorStateError,
    MotorStatus,
    MotorTimeoutError,
    MoveIncompleteError,
)
from .registers import (
    ControlWordBits,
    HomingMethod,
    OperationMode,
    RegisterAccess,
    RegisterDef,
    Registers,
    StatusWordBits,
)
from .state_machine import (
    DriveCommand,
    DriveState,
    StateMachine,
    decode_status_word,
    decode_status_word_flags,
    get_command_control_word,
    get_transition_path,
    is_fault,
    is_operation_enabled,
    is_target_reached,
    is_warning,
)

__all__ = [
    # Motor
    "Motor",
    "MotorStatus",
    "MotorError",
    "MotorStateError",
    "MotorTimeoutError",
    "MotionInterruptedError",
    "LimitSwitchError",
    "MoveIncompleteError",
    # Registers
    "Registers",
    "RegisterDef",
    "RegisterAccess",
    "ControlWordBits",
    "StatusWordBits",
    "OperationMode",
    "HomingMethod",
    # State Machine
    "DriveState",
    "DriveCommand",
    "StateMachine",
    "decode_status_word",
    "decode_status_word_flags",
    "get_command_control_word",
    "get_transition_path",
    "is_fault",
    "is_warning",
    "is_target_reached",
    "is_operation_enabled",
]
