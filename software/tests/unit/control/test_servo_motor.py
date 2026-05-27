import pytest

from fluidics.control.servo_motor import (
    DEFAULT_AXIS_CONFIGS,
    AxisConfig,
    ControlWordBits,
    DriveCommand,
    DriveState,
    ServoMotorSimulation,
    decode_status_word,
    get_transition_path,
)


class TestAxisConfig:
    def test_z_config_exists(self):
        assert "Z" in DEFAULT_AXIS_CONFIGS
        config = DEFAULT_AXIS_CONFIGS["Z"]
        assert config.slave_id == 4
        assert config.encoder_resolution == 131072
        assert config.has_brake is True

    def test_mm_to_pulses_roundtrip(self):
        config = AxisConfig(slave_id=1, encoder_resolution=10000, ball_screw_lead=10.0)
        pulses = config.mm_to_pulses(5.0)
        assert pulses == 5000
        mm = config.pulses_to_mm(5000)
        assert mm == pytest.approx(5.0)

    def test_position_valid(self):
        config = AxisConfig(slave_id=1, stroke_min=0.0, stroke_max=61.0)
        assert config.is_position_valid(0.0) is True
        assert config.is_position_valid(30.5) is True
        assert config.is_position_valid(61.0) is True

    def test_position_invalid(self):
        config = AxisConfig(slave_id=1, stroke_min=0.0, stroke_max=61.0)
        assert config.is_position_valid(-0.1) is False
        assert config.is_position_valid(61.1) is False

    def test_velocity_conversion(self):
        config = AxisConfig(slave_id=1, encoder_resolution=10000, ball_screw_lead=10.0)
        pulses = config.velocity_mm_to_pulses(100.0)
        assert pulses == 100000
        mm_s = config.velocity_pulses_to_mm(100000)
        assert mm_s == pytest.approx(100.0)


class TestDriveStateMachine:
    def test_decode_status_operation_enabled(self):
        # bits: 0,1,2 set (0x07), bit 5 set (0x20) -> 0x27
        status = 0x0627
        assert decode_status_word(status) == DriveState.OPERATION_ENABLED

    def test_decode_status_fault(self):
        # bit 3 set = fault
        status = 0x0008
        assert decode_status_word(status) == DriveState.FAULT

    def test_decode_status_switch_on_disabled(self):
        status = 0x0040
        assert decode_status_word(status) == DriveState.SWITCH_ON_DISABLED

    def test_decode_status_ready_to_switch_on(self):
        status = 0x0021
        assert decode_status_word(status) == DriveState.READY_TO_SWITCH_ON

    def test_decode_status_switched_on(self):
        status = 0x0023
        assert decode_status_word(status) == DriveState.SWITCHED_ON

    def test_decode_status_quick_stop_active(self):
        status = 0x0007
        assert decode_status_word(status) == DriveState.QUICK_STOP_ACTIVE

    def test_decode_status_fault_reaction_active(self):
        status = 0x000F
        assert decode_status_word(status) == DriveState.FAULT_REACTION_ACTIVE

    def test_transition_path_to_enabled(self):
        path = get_transition_path(DriveState.SWITCH_ON_DISABLED, DriveState.OPERATION_ENABLED)
        assert len(path) == 3
        commands = [cmd for cmd, _ in path]
        assert commands == [
            DriveCommand.SHUTDOWN,
            DriveCommand.SWITCH_ON,
            DriveCommand.ENABLE_OPERATION,
        ]

    def test_transition_path_same_state(self):
        path = get_transition_path(DriveState.OPERATION_ENABLED, DriveState.OPERATION_ENABLED)
        assert path == []

    def test_transition_path_from_fault(self):
        path = get_transition_path(DriveState.FAULT, DriveState.OPERATION_ENABLED)
        assert path[0] == (DriveCommand.FAULT_RESET, ControlWordBits.FAULT_RESET)
        assert len(path) == 4  # reset + shutdown + switch_on + enable


class TestServoMotorSimulation:
    def test_create(self):
        sim = ServoMotorSimulation()
        assert sim.is_aborted is False
        assert sim.is_connected is False

    def test_connect_disconnect(self):
        sim = ServoMotorSimulation()
        sim.connect()
        assert sim.is_connected is True
        sim.disconnect()
        assert sim.is_connected is False

    def test_enable_disable(self):
        sim = ServoMotorSimulation()
        assert sim.is_enabled() is False
        sim.enable()
        assert sim.is_enabled() is True
        sim.disable()
        assert sim.is_enabled() is False

    def test_move_to(self):
        sim = ServoMotorSimulation()
        assert sim.get_position() == 0.0
        sim.move_to(30.0)
        assert sim.get_position() == 30.0

    def test_move_to_out_of_range(self):
        sim = ServoMotorSimulation()
        with pytest.raises(ValueError, match="out of range"):
            sim.move_to(100.0)

    def test_jog_and_stop(self):
        sim = ServoMotorSimulation()
        sim.jog(50.0)
        sim.stop()

    def test_abort(self):
        sim = ServoMotorSimulation()
        assert sim.is_aborted is False
        sim.abort()
        assert sim.is_aborted is True

    def test_reset_abort(self):
        sim = ServoMotorSimulation()
        sim.abort()
        sim.reset_abort()
        assert sim.is_aborted is False

    def test_home(self):
        sim = ServoMotorSimulation()
        sim.move_to(30.0)
        assert sim.is_homed() is False
        sim.home()
        assert sim.get_position() == 0.0
        assert sim.is_homed() is True

    def test_set_speed(self):
        sim = ServoMotorSimulation()
        sim.set_speed(200.0)

    def test_context_manager(self):
        with ServoMotorSimulation() as sim:
            sim.connect()
            assert sim.is_connected is True
        assert sim.is_connected is False

    def test_move_relative(self):
        sim = ServoMotorSimulation()
        sim.move_to(10.0)
        sim.move_relative(5.0)
        assert sim.get_position() == 15.0

    def test_move_relative_out_of_range(self):
        sim = ServoMotorSimulation()
        sim.move_to(60.0)
        with pytest.raises(ValueError, match="out of range"):
            sim.move_relative(5.0)

    def test_fault_reset(self):
        sim = ServoMotorSimulation()
        sim.fault_reset()

    def test_quick_stop(self):
        sim = ServoMotorSimulation()
        sim.quick_stop()

    def test_get_velocity(self):
        sim = ServoMotorSimulation()
        assert sim.get_velocity() == 0.0

    def test_is_enabled(self):
        sim = ServoMotorSimulation()
        assert sim.is_enabled() is False
        sim.enable()
        assert sim.is_enabled() is True

    def test_initialize_axis(self):
        sim = ServoMotorSimulation()
        sim.initialize_axis()

    def test_set_acceleration(self):
        sim = ServoMotorSimulation()
        sim.set_acceleration(500.0)

    def test_set_deceleration(self):
        sim = ServoMotorSimulation()
        sim.set_deceleration(500.0)

    def test_unknown_axis(self):
        sim = ServoMotorSimulation()
        with pytest.raises(ValueError, match="Unknown axis"):
            sim.move_to(10.0, axis="NONEXISTENT")
