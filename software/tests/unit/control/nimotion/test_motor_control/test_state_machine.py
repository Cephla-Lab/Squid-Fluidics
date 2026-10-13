"""
CiA402 state machine tests
"""

from fluidics.control.nimotion.motor_control.registers import StatusWordBits
from fluidics.control.nimotion.motor_control.state_machine import (
    DriveCommand,
    DriveState,
    StateMachine,
    decode_status_word,
    decode_status_word_flags,
    get_transition_path,
    is_fault,
    is_operation_enabled,
    is_target_reached,
    is_warning,
)


class TestDecodeStatusWord:
    """Test status word decoding"""

    def test_switch_on_disabled(self) -> None:
        """Test Switch on disabled state"""
        status_word = 0x0040  # Bit 6 set
        state = decode_status_word(status_word)
        assert state == DriveState.SWITCH_ON_DISABLED

    def test_ready_to_switch_on(self) -> None:
        """Test Ready to switch on state"""
        status_word = 0x0021  # Bits 0, 5 set
        state = decode_status_word(status_word)
        assert state == DriveState.READY_TO_SWITCH_ON

    def test_switched_on(self) -> None:
        """Test Switched on state"""
        status_word = 0x0023  # Bits 0, 1, 5 set
        state = decode_status_word(status_word)
        assert state == DriveState.SWITCHED_ON

    def test_operation_enabled(self) -> None:
        """Test Operation enabled state"""
        status_word = 0x0027  # Bits 0, 1, 2, 5 set
        state = decode_status_word(status_word)
        assert state == DriveState.OPERATION_ENABLED

    def test_fault(self) -> None:
        """Test Fault state"""
        status_word = 0x0008  # Bit 3 (fault) set
        state = decode_status_word(status_word)
        assert state == DriveState.FAULT

    def test_quick_stop_active(self) -> None:
        """Test Quick stop active state"""
        status_word = 0x0007  # Bits 0, 1, 2 set (no quick stop bit)
        state = decode_status_word(status_word)
        assert state == DriveState.QUICK_STOP_ACTIVE


class TestDecodeStatusWordFlags:
    """Test status word flag decoding"""

    def test_all_flags(self) -> None:
        """Test all flags"""
        # Set multiple flags
        status_word = (
            StatusWordBits.READY_TO_SWITCH_ON
            | StatusWordBits.SWITCHED_ON
            | StatusWordBits.OPERATION_ENABLED
            | StatusWordBits.TARGET_REACHED
        )

        flags = decode_status_word_flags(status_word)

        assert flags["ready_to_switch_on"] is True
        assert flags["switched_on"] is True
        assert flags["operation_enabled"] is True
        assert flags["target_reached"] is True
        assert flags["fault"] is False
        assert flags["warning"] is False

    def test_fault_flag(self) -> None:
        """Test fault flag"""
        status_word = StatusWordBits.FAULT
        flags = decode_status_word_flags(status_word)
        assert flags["fault"] is True


class TestStatusHelpers:
    """Test status helper functions"""

    def test_is_target_reached(self) -> None:
        """Test target reached detection"""
        assert is_target_reached(StatusWordBits.TARGET_REACHED) is True
        assert is_target_reached(0x0000) is False

    def test_is_fault(self) -> None:
        """Test fault detection"""
        assert is_fault(StatusWordBits.FAULT) is True
        assert is_fault(0x0000) is False

    def test_is_warning(self) -> None:
        """Test warning detection"""
        assert is_warning(StatusWordBits.WARNING) is True
        assert is_warning(0x0000) is False

    def test_is_operation_enabled(self) -> None:
        """Test operation enable detection"""
        assert is_operation_enabled(StatusWordBits.OPERATION_ENABLED) is True
        assert is_operation_enabled(0x0000) is False


class TestGetTransitionPath:
    """Test state transition path"""

    def test_same_state(self) -> None:
        """Test same state"""
        path = get_transition_path(DriveState.OPERATION_ENABLED, DriveState.OPERATION_ENABLED)
        assert path == []

    def test_disabled_to_enabled(self) -> None:
        """Test from disabled to enabled"""
        path = get_transition_path(DriveState.SWITCH_ON_DISABLED, DriveState.OPERATION_ENABLED)

        assert len(path) == 3
        commands = [cmd for cmd, cw in path]
        assert DriveCommand.SHUTDOWN in commands
        assert DriveCommand.SWITCH_ON in commands
        assert DriveCommand.ENABLE_OPERATION in commands

    def test_enabled_to_switched_on(self) -> None:
        """Test from enabled to switched on"""
        path = get_transition_path(DriveState.OPERATION_ENABLED, DriveState.SWITCHED_ON)

        assert len(path) == 1
        assert path[0][0] == DriveCommand.DISABLE_OPERATION

    def test_fault_to_enabled(self) -> None:
        """Test from fault to enabled"""
        path = get_transition_path(DriveState.FAULT, DriveState.OPERATION_ENABLED)

        # Should reset the fault first, then run the normal enable sequence
        assert len(path) >= 4
        assert path[0][0] == DriveCommand.FAULT_RESET


class TestStateMachine:
    """Test state machine class"""

    def test_initial_state(self) -> None:
        """Test initial state"""
        sm = StateMachine()
        assert sm.current_state == DriveState.UNKNOWN
        assert sm.status_word == 0

    def test_update(self) -> None:
        """Test state update"""
        sm = StateMachine()

        # Update to Operation Enabled
        state = sm.update(0x0027)

        assert state == DriveState.OPERATION_ENABLED
        assert sm.current_state == DriveState.OPERATION_ENABLED
        assert sm.status_word == 0x0027

    def test_properties(self) -> None:
        """Test properties"""
        sm = StateMachine()
        sm.update(0x0027 | StatusWordBits.TARGET_REACHED)

        assert sm.is_operation_enabled is True
        assert sm.is_target_reached is True
        assert sm.is_fault is False

    def test_get_path_to(self) -> None:
        """Test getting transition path"""
        sm = StateMachine()
        sm.update(0x0040)  # Switch on disabled

        path = sm.get_path_to(DriveState.OPERATION_ENABLED)
        assert len(path) == 3

    def test_flags(self) -> None:
        """Test flags"""
        sm = StateMachine()
        sm.update(StatusWordBits.OPERATION_ENABLED | StatusWordBits.TARGET_REACHED)

        flags = sm.flags
        assert flags["operation_enabled"] is True
        assert flags["target_reached"] is True
