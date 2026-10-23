"""
Axis configuration module tests
"""

import dataclasses
import math

import pytest

from fluidics.control.nimotion.high_level_api.axis_config import (
    DEFAULT_AXIS_CONFIGS,
    AxisConfig,
    AxisName,
    get_all_axis_configs,
    get_axis_config,
)


class TestAxisName:
    """Test axis name enum"""

    def test_values(self) -> None:
        """Test enum values"""
        assert AxisName.Z4.value == "Z4"
        assert AxisName.Y2.value == "Y2"
        assert AxisName.Z3.value == "Z3"
        assert AxisName.X1.value == "X1"
        assert AxisName.T5.value == "T5"

    def test_iteration(self) -> None:
        """Test iteration"""
        axes = list(AxisName)
        assert len(axes) == 5


class TestAxisConfig:
    """Test axis configuration"""

    def test_stroke(self) -> None:
        """Test stroke calculation"""
        config = AxisConfig(
            name=AxisName.Z4,
            slave_id=1,
            model="TEST",
            motor_power=100,
            has_brake=False,
            ball_screw_lead=10.0,
            max_velocity=100.0,
            stroke_min=10.0,
            stroke_max=110.0,
            positioning_accuracy=0.01,
            repeat_accuracy=0.005,
            max_payload_horizontal=10.0,
            max_payload_vertical=5.0,
        )

        assert config.stroke == 100.0

    def test_unit_conversion(self) -> None:
        """Test unit conversion"""
        config = AxisConfig(
            name=AxisName.Z4,
            slave_id=1,
            model="TEST",
            motor_power=100,
            has_brake=False,
            ball_screw_lead=10.0,  # 10mm/rev
            max_velocity=100.0,
            stroke_min=0.0,
            stroke_max=100.0,
            positioning_accuracy=0.01,
            repeat_accuracy=0.005,
            max_payload_horizontal=10.0,
            max_payload_vertical=5.0,
            encoder_resolution=10000,  # 10000 pulses/rev
        )

        # displacement per pulse = 10mm / 10000 = 0.001mm
        assert config.mm_per_pulse == 0.001

        # pulses per mm = 10000 / 10 = 1000
        assert config.pulses_per_mm == 1000.0

        # 25mm = 25000 pulses
        assert config.mm_to_pulses(25.0) == 25000

        # 10000 pulses = 10mm
        assert config.pulses_to_mm(10000) == 10.0

    def test_velocity_conversion(self) -> None:
        """Test velocity unit conversion"""
        config = AxisConfig(
            name=AxisName.Z4,
            slave_id=1,
            model="TEST",
            motor_power=100,
            has_brake=False,
            ball_screw_lead=10.0,
            max_velocity=100.0,
            stroke_min=0.0,
            stroke_max=100.0,
            positioning_accuracy=0.01,
            repeat_accuracy=0.005,
            max_payload_horizontal=10.0,
            max_payload_vertical=5.0,
            encoder_resolution=10000,
        )

        # 100mm/s = 100000 pulses/s
        assert config.velocity_mm_to_pulses(100.0) == 100000

        # 50000 pulses/s = 50mm/s
        assert config.velocity_pulses_to_mm(50000) == 50.0

        # 606Ch reports motor rpm: 3000 rpm on a 10 mm lead = 500 mm/s
        assert config.rpm_to_mm_s(3000) == 500.0

    def test_velocity_follows_position_polarity(self) -> None:
        """606Ch is in motor direction; with position polarity 0x80 the sign flips"""
        config = dataclasses.replace(get_axis_config(AxisName.Z3), driver_polarity=0x00)
        assert config.ball_screw_lead == 10.0
        assert config.rpm_to_mm_s(120) == 20.0
        config.driver_polarity = 0x80
        assert config.rpm_to_mm_s(-120) == 20.0
        config.driver_polarity = 0xC0  # velocity polarity too: not measured, left as read
        assert config.rpm_to_mm_s(120) == 20.0

    def test_position_validation(self) -> None:
        """Test position validation"""
        config = AxisConfig(
            name=AxisName.Z4,
            slave_id=1,
            model="TEST",
            motor_power=100,
            has_brake=False,
            ball_screw_lead=10.0,
            max_velocity=100.0,
            stroke_min=10.0,
            stroke_max=100.0,
            positioning_accuracy=0.01,
            repeat_accuracy=0.005,
            max_payload_horizontal=10.0,
            max_payload_vertical=5.0,
        )

        assert config.is_position_valid(10.0) is True
        assert config.is_position_valid(50.0) is True
        assert config.is_position_valid(100.0) is True
        assert config.is_position_valid(5.0) is False
        assert config.is_position_valid(105.0) is False


class TestDefaultConfigs:
    """Test default configurations"""

    def test_all_axes_configured(self) -> None:
        """Test that all axes have a configuration"""
        for axis in AxisName:
            assert axis in DEFAULT_AXIS_CONFIGS

    def test_get_axis_config(self) -> None:
        """Test getting an axis configuration"""
        config = get_axis_config(AxisName.Z3)

        assert config.name == AxisName.Z3
        assert config.slave_id == 3
        assert config.has_brake is False

    def test_get_all_axis_configs(self) -> None:
        """Test getting all configurations"""
        configs = get_all_axis_configs()

        assert len(configs) == 5
        assert AxisName.T5 in configs
        assert AxisName.X1 in configs
        assert AxisName.Z4 in configs
        assert AxisName.Y2 in configs
        assert AxisName.Z3 in configs

    def test_z4_axis_config(self) -> None:
        """Test Z4 axis configuration (values the ISS instrument depends on)"""
        config = get_axis_config(AxisName.Z4)

        assert config.slave_id == 4
        assert config.encoder_resolution == 131072
        assert config.pulses_per_mm == 13107.2
        assert config.homing_method == 18
        assert config.driver_polarity == 0x80
        assert config.stroke_max == 65.0
        assert config.has_brake is True

    def test_y_axis_config(self) -> None:
        """Test Y axis configuration"""
        config = get_axis_config(AxisName.Y2)

        assert config.slave_id == 2
        assert config.model == "CFG5"
        assert config.motor_power == 100
        # inside the switches: origin at -0.012 mm, far at +110.20 mm
        assert (config.stroke_min, config.stroke_max) == (0.5, 109.0)

    def test_x_axis_config(self) -> None:
        """X1: limit switches on DI1 (far) / DI2 (origin), 0 at the origin"""
        config = get_axis_config(AxisName.X1)

        assert config.slave_id == 1
        assert config.encoder_resolution == 131072  # as saved in the drive
        assert config.ball_screw_lead == 20.0
        assert config.stroke_max == 200.0
        assert (config.di1_function, config.di1_logic) == (15, 0)
        assert (config.di2_function, config.di2_logic) == (14, 0)
        assert config.di3_function is None
        assert config.homing_method == 18
        assert config.driver_polarity == 0x80
        assert config.has_brake is False

    def test_z_axis_config(self) -> None:
        """Test Z axis configuration"""
        config = get_axis_config(AxisName.Z3)

        assert config.slave_id == 3
        assert config.model == "CFG4"
        assert config.has_brake is False  # held by lead-screw friction
        # inside the switches: upper at -0.018..0.000 mm, lower at +59.7 mm
        assert (config.stroke_min, config.stroke_max) == (0.5, 58.5)


class TestT5:
    """The plate tilter's tilt motor (STM2851 stepper), spec section 5"""

    config = DEFAULT_AXIS_CONFIGS[AxisName.T5]

    def test_degrees(self) -> None:
        assert self.config.unit == "°"
        assert self.config.ball_screw_lead == 360.0
        assert self.config.pulses_to_mm(131072) == pytest.approx(360.0)
        assert self.config.mm_to_pulses(1.0) == 364
        assert self.config.velocity_mm_to_pulses(20.0) == 7281
        assert self.config.rpm_to_mm_s(10.0) == pytest.approx(60.0)

    def test_address_stroke_and_inputs(self) -> None:
        c = self.config
        assert c.slave_id == 5
        assert (c.stroke_min, c.stroke_max) == (-5.0, 30.0)
        assert c.homing_method is None
        assert c.has_brake is False
        assert (c.di1_function, c.di1_logic) == (31, 0)
        assert (c.di2_function, c.di3_function) == (0, 0)

    def test_drive_limits_in_counts(self) -> None:
        c = self.config
        assert (c.drive_limit_min, c.drive_limit_max) == (-7.0, 32.0)
        assert c.max_acceleration == 1000.0  # 60C5h / 60C6h, read back on connect
        assert c.limit_to_counts(-7.0) == -2548
        assert c.limit_to_counts(32.0) == 11650
        assert c.limit_to_counts(math.inf) == 2**31 - 1
        assert c.limit_to_counts(-math.inf) == -(2**31)
        assert c.limit_to_counts(1e12) == 2**31 - 1

    def test_restore_settings_wait_for_the_bench(self) -> None:
        assert self.config.position_restore_mode is None
        assert self.config.restore_blocking_position is None

    def test_limits_are_set_together(self) -> None:
        with pytest.raises(ValueError):
            dataclasses.replace(self.config, drive_limit_max=None)

    def test_servo_axes_are_unchanged(self) -> None:
        for axis in (AxisName.Z4, AxisName.Y2, AxisName.Z3, AxisName.X1):
            c = DEFAULT_AXIS_CONFIGS[axis]
            assert c.unit == "mm"
            assert c.position_restore_mode is None
            assert c.restore_blocking_position is None
            assert c.drive_limit_min is None and c.drive_limit_max is None
