"""
CiA402 state machine module

Implements the servo drive state machine defined by the CiA402 standard.
"""

from enum import Enum
from typing import List, Tuple

from .registers import ControlWordBits, StatusWordBits


class DriveState(Enum):
    """Drive state"""

    NOT_READY_TO_SWITCH_ON = "Not ready to switch on"
    SWITCH_ON_DISABLED = "Switch on disabled"
    READY_TO_SWITCH_ON = "Ready to switch on"
    SWITCHED_ON = "Switched on"
    OPERATION_ENABLED = "Operation enabled"
    QUICK_STOP_ACTIVE = "Quick stop active"
    FAULT_REACTION_ACTIVE = "Fault reaction active"
    FAULT = "Fault"
    UNKNOWN = "Unknown"


class DriveCommand(Enum):
    """Drive command"""

    SHUTDOWN = "Shutdown"
    SWITCH_ON = "Switch on"
    ENABLE_OPERATION = "Enable operation"
    DISABLE_OPERATION = "Disable operation"
    DISABLE_VOLTAGE = "Disable voltage"
    QUICK_STOP = "Quick stop"
    FAULT_RESET = "Fault reset"


# State transition table: (current state, command) -> (target state, control word)
# Based on the CiA402 state machine diagram
STATE_TRANSITIONS = {
    # From Switch on disabled
    (DriveState.SWITCH_ON_DISABLED, DriveCommand.SHUTDOWN): (
        DriveState.READY_TO_SWITCH_ON,
        ControlWordBits.CMD_SHUTDOWN,
    ),
    # From Ready to switch on
    (DriveState.READY_TO_SWITCH_ON, DriveCommand.SWITCH_ON): (
        DriveState.SWITCHED_ON,
        ControlWordBits.CMD_SWITCH_ON,
    ),
    (DriveState.READY_TO_SWITCH_ON, DriveCommand.DISABLE_VOLTAGE): (
        DriveState.SWITCH_ON_DISABLED,
        ControlWordBits.CMD_DISABLE_VOLTAGE,
    ),
    # From Switched on
    (DriveState.SWITCHED_ON, DriveCommand.ENABLE_OPERATION): (
        DriveState.OPERATION_ENABLED,
        ControlWordBits.CMD_ENABLE_OPERATION,
    ),
    (DriveState.SWITCHED_ON, DriveCommand.SHUTDOWN): (
        DriveState.READY_TO_SWITCH_ON,
        ControlWordBits.CMD_SHUTDOWN,
    ),
    (DriveState.SWITCHED_ON, DriveCommand.DISABLE_VOLTAGE): (
        DriveState.SWITCH_ON_DISABLED,
        ControlWordBits.CMD_DISABLE_VOLTAGE,
    ),
    # From Operation enabled
    (DriveState.OPERATION_ENABLED, DriveCommand.DISABLE_OPERATION): (
        DriveState.SWITCHED_ON,
        ControlWordBits.CMD_DISABLE_OPERATION,
    ),
    (DriveState.OPERATION_ENABLED, DriveCommand.SHUTDOWN): (
        DriveState.READY_TO_SWITCH_ON,
        ControlWordBits.CMD_SHUTDOWN,
    ),
    (DriveState.OPERATION_ENABLED, DriveCommand.DISABLE_VOLTAGE): (
        DriveState.SWITCH_ON_DISABLED,
        ControlWordBits.CMD_DISABLE_VOLTAGE,
    ),
    (DriveState.OPERATION_ENABLED, DriveCommand.QUICK_STOP): (
        DriveState.QUICK_STOP_ACTIVE,
        ControlWordBits.CMD_QUICK_STOP,
    ),
    # From Quick stop active
    (DriveState.QUICK_STOP_ACTIVE, DriveCommand.DISABLE_VOLTAGE): (
        DriveState.SWITCH_ON_DISABLED,
        ControlWordBits.CMD_DISABLE_VOLTAGE,
    ),
    (DriveState.QUICK_STOP_ACTIVE, DriveCommand.ENABLE_OPERATION): (
        DriveState.OPERATION_ENABLED,
        ControlWordBits.CMD_ENABLE_OPERATION,
    ),
    # From Fault
    (DriveState.FAULT, DriveCommand.FAULT_RESET): (
        DriveState.SWITCH_ON_DISABLED,
        ControlWordBits.FAULT_RESET,
    ),
}


def decode_status_word(status_word: int) -> DriveState:
    """
    Decode the drive state from the status word

    Per the CiA402 standard, the state is determined by the following bits:
    - Bit 0: Ready to switch on
    - Bit 1: Switched on
    - Bit 2: Operation enabled
    - Bit 3: Fault
    - Bit 5: Quick stop (0=active)
    - Bit 6: Switch on disabled

    State patterns (x=don't care):
    | Status word pattern | State                  |
    |---------------------|------------------------|
    | xxxx xxxx x0xx 0000 | Not ready to switch on |
    | xxxx xxxx x1xx 0000 | Switch on disabled     |
    | xxxx xxxx x01x 0001 | Ready to switch on     |
    | xxxx xxxx x01x 0011 | Switched on            |
    | xxxx xxxx x01x 0111 | Operation enabled      |
    | xxxx xxxx x00x 0111 | Quick stop active      |
    | xxxx xxxx x0xx 1111 | Fault reaction active  |
    | xxxx xxxx x0xx 1000 | Fault                  |

    Args:
        status_word: Status word value

    Returns:
        Current drive state
    """
    # Extract the state-relevant bits (bits 0-3, 5, 6)
    state_bits = status_word & StatusWordBits.STATE_MASK

    # Check for fault state (Bit 3 = 1)
    if status_word & StatusWordBits.FAULT:
        # Fault reaction active: xxxx xxxx x0xx 1111
        if (state_bits & 0x004F) == 0x000F:
            return DriveState.FAULT_REACTION_ACTIVE
        # Fault: xxxx xxxx x0xx 1000
        return DriveState.FAULT

    # Check the other states by priority (using bit masks rather than exact matches)

    # Operation enabled: xxxx xxxx x01x 0111
    # Requires bits 0-2 = 111, bit 5 = 1, bit 6 = 0
    if (state_bits & 0x006F) == 0x0027:
        return DriveState.OPERATION_ENABLED

    # Quick stop active: xxxx xxxx x00x 0111
    # Requires bits 0-2 = 111, bit 5 = 0, bit 6 = 0
    if (state_bits & 0x006F) == 0x0007:
        return DriveState.QUICK_STOP_ACTIVE

    # Switched on: xxxx xxxx x01x 0011
    # Requires bits 0-1 = 11, bit 2 = 0, bit 5 = 1, bit 6 = 0
    if (state_bits & 0x006F) == 0x0023:
        return DriveState.SWITCHED_ON

    # Ready to switch on: xxxx xxxx x01x 0001
    # Requires bit 0 = 1, bits 1-2 = 0, bit 5 = 1, bit 6 = 0
    if (state_bits & 0x006F) == 0x0021:
        return DriveState.READY_TO_SWITCH_ON

    # Switch on disabled: xxxx xxxx x1xx 0000
    # Requires bits 0-3 = 0, bit 6 = 1, bit 5 can be any value
    if (state_bits & 0x004F) == 0x0040:
        return DriveState.SWITCH_ON_DISABLED

    # Not ready to switch on: xxxx xxxx x0xx 0000
    # Requires bits 0-3 = 0, bit 6 = 0 (bit 5 can be any value)
    if (state_bits & 0x004F) == 0x0000:
        return DriveState.NOT_READY_TO_SWITCH_ON

    return DriveState.UNKNOWN


def get_command_control_word(command: DriveCommand) -> int:
    """
    Get the control word corresponding to a command

    Args:
        command: Drive command

    Returns:
        Control word value
    """
    command_map = {
        DriveCommand.SHUTDOWN: ControlWordBits.CMD_SHUTDOWN,
        DriveCommand.SWITCH_ON: ControlWordBits.CMD_SWITCH_ON,
        DriveCommand.ENABLE_OPERATION: ControlWordBits.CMD_ENABLE_OPERATION,
        DriveCommand.DISABLE_OPERATION: ControlWordBits.CMD_DISABLE_OPERATION,
        DriveCommand.DISABLE_VOLTAGE: ControlWordBits.CMD_DISABLE_VOLTAGE,
        DriveCommand.QUICK_STOP: ControlWordBits.CMD_QUICK_STOP,
        DriveCommand.FAULT_RESET: ControlWordBits.FAULT_RESET,
    }
    return command_map.get(command, 0)


def get_transition_path(
    current_state: DriveState,
    target_state: DriveState,
) -> List[Tuple[DriveCommand, int]]:
    """
    Get the transition path from the current state to the target state

    Args:
        current_state: Current state
        target_state: Target state

    Returns:
        List of commands and control words [(command, control_word), ...]
    """
    if current_state == target_state:
        return []

    # A fault state must be reset first
    if current_state == DriveState.FAULT:
        reset = [(DriveCommand.FAULT_RESET, ControlWordBits.FAULT_RESET)]
        # After reset, the drive enters SWITCH_ON_DISABLED
        return reset + get_transition_path(DriveState.SWITCH_ON_DISABLED, target_state)

    # Define state levels (used to determine the transition direction)
    state_levels = {
        DriveState.NOT_READY_TO_SWITCH_ON: 0,
        DriveState.SWITCH_ON_DISABLED: 1,
        DriveState.READY_TO_SWITCH_ON: 2,
        DriveState.SWITCHED_ON: 3,
        DriveState.OPERATION_ENABLED: 4,
        DriveState.QUICK_STOP_ACTIVE: 4,
        DriveState.FAULT: 0,
        DriveState.FAULT_REACTION_ACTIVE: 0,
    }

    current_level = state_levels.get(current_state, 0)
    target_level = state_levels.get(target_state, 0)

    path: List[Tuple[DriveCommand, int]] = []

    if target_level > current_level:
        # Upward transition (enable direction)
        if current_state == DriveState.SWITCH_ON_DISABLED:
            path.append((DriveCommand.SHUTDOWN, ControlWordBits.CMD_SHUTDOWN))
            current_state = DriveState.READY_TO_SWITCH_ON

        if current_state == DriveState.READY_TO_SWITCH_ON and target_level >= 3:
            path.append((DriveCommand.SWITCH_ON, ControlWordBits.CMD_SWITCH_ON))
            current_state = DriveState.SWITCHED_ON

        if current_state == DriveState.SWITCHED_ON and target_level >= 4:
            path.append((DriveCommand.ENABLE_OPERATION, ControlWordBits.CMD_ENABLE_OPERATION))

    else:
        # Downward transition (disable direction)
        if current_state == DriveState.OPERATION_ENABLED:
            if target_level <= 3:
                path.append((DriveCommand.DISABLE_OPERATION, ControlWordBits.CMD_DISABLE_OPERATION))
                current_state = DriveState.SWITCHED_ON

        if current_state == DriveState.SWITCHED_ON:
            if target_level <= 2:
                path.append((DriveCommand.SHUTDOWN, ControlWordBits.CMD_SHUTDOWN))
                current_state = DriveState.READY_TO_SWITCH_ON

        if current_state == DriveState.READY_TO_SWITCH_ON:
            if target_level <= 1:
                path.append((DriveCommand.DISABLE_VOLTAGE, ControlWordBits.CMD_DISABLE_VOLTAGE))

        if current_state == DriveState.QUICK_STOP_ACTIVE:
            path.append((DriveCommand.DISABLE_VOLTAGE, ControlWordBits.CMD_DISABLE_VOLTAGE))
            if target_level > 1:
                remaining = get_transition_path(DriveState.SWITCH_ON_DISABLED, target_state)
                return path + remaining

    return path


def is_target_reached(status_word: int) -> bool:
    """
    Check whether the target has been reached

    Args:
        status_word: Status word value

    Returns:
        Whether the target has been reached
    """
    return bool(status_word & StatusWordBits.TARGET_REACHED)


def is_fault(status_word: int) -> bool:
    """
    Check whether a fault is present

    Args:
        status_word: Status word value

    Returns:
        Whether a fault is present
    """
    return bool(status_word & StatusWordBits.FAULT)


def is_warning(status_word: int) -> bool:
    """
    Check whether a warning is present

    Args:
        status_word: Status word value

    Returns:
        Whether a warning is present
    """
    return bool(status_word & StatusWordBits.WARNING)


def is_operation_enabled(status_word: int) -> bool:
    """
    Check whether operation is enabled

    Args:
        status_word: Status word value

    Returns:
        Whether operation is enabled
    """
    return bool(status_word & StatusWordBits.OPERATION_ENABLED)


def decode_status_word_flags(status_word: int) -> dict:
    """
    Decode all flag bits of the status word

    Args:
        status_word: Status word value

    Returns:
        Dictionary of flag bits
    """
    return {
        "ready_to_switch_on": bool(status_word & StatusWordBits.READY_TO_SWITCH_ON),
        "switched_on": bool(status_word & StatusWordBits.SWITCHED_ON),
        "operation_enabled": bool(status_word & StatusWordBits.OPERATION_ENABLED),
        "fault": bool(status_word & StatusWordBits.FAULT),
        "voltage_enabled": bool(status_word & StatusWordBits.VOLTAGE_ENABLED),
        "quick_stop": bool(status_word & StatusWordBits.QUICK_STOP),
        "switch_on_disabled": bool(status_word & StatusWordBits.SWITCH_ON_DISABLED),
        "warning": bool(status_word & StatusWordBits.WARNING),
        "remote": bool(status_word & StatusWordBits.REMOTE),
        "target_reached": bool(status_word & StatusWordBits.TARGET_REACHED),
        "internal_limit_active": bool(status_word & StatusWordBits.INTERNAL_LIMIT_ACTIVE),
        "set_point_ack": bool(status_word & StatusWordBits.SET_POINT_ACK),
        "following_error": bool(status_word & StatusWordBits.FOLLOWING_ERROR),
    }


class StateMachine:
    """
    CiA402 state machine

    Encapsulates the state machine logic and provides state query and
    transition functionality.
    """

    def __init__(self) -> None:
        """Initialize the state machine"""
        self._current_state = DriveState.UNKNOWN
        self._status_word = 0

    @property
    def current_state(self) -> DriveState:
        """Get the current state"""
        return self._current_state

    @property
    def status_word(self) -> int:
        """Get the status word"""
        return self._status_word

    def update(self, status_word: int) -> DriveState:
        """
        Update the state

        Args:
            status_word: New status word value

        Returns:
            The updated state
        """
        self._status_word = status_word
        self._current_state = decode_status_word(status_word)
        return self._current_state

    def get_path_to(self, target_state: DriveState) -> List[Tuple[DriveCommand, int]]:
        """
        Get the transition path to the target state

        Args:
            target_state: Target state

        Returns:
            The transition path
        """
        return get_transition_path(self._current_state, target_state)

    @property
    def is_target_reached(self) -> bool:
        """Whether the target has been reached"""
        return is_target_reached(self._status_word)

    @property
    def is_fault(self) -> bool:
        """Whether a fault is present"""
        return is_fault(self._status_word)

    @property
    def is_warning(self) -> bool:
        """Whether a warning is present"""
        return is_warning(self._status_word)

    @property
    def is_operation_enabled(self) -> bool:
        """Whether operation is enabled"""
        return is_operation_enabled(self._status_word)

    @property
    def flags(self) -> dict:
        """Get all flag bits"""
        return decode_status_word_flags(self._status_word)

    def __repr__(self) -> str:
        return f"StateMachine(state={self._current_state.value}, status=0x{self._status_word:04X})"
