"""
Axis Configuration Module

Defines the axis configuration and parameters for the XYG321-A 3-axis platform.
"""

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Dict, Optional

if TYPE_CHECKING:
    pass


class AxisName(Enum):
    """Axis name enumeration"""

    Z4 = "Z4"
    Y2 = "Y2"
    Z3 = "Z3"
    X1 = "X1"


@dataclass
class AxisConfig:
    """Axis configuration data class"""

    name: AxisName
    """Axis name"""

    slave_id: int
    """Modbus slave address"""

    model: str
    """Motor model"""

    motor_power: int
    """Motor power (W)"""

    has_brake: bool
    """Whether a brake is present"""

    ball_screw_lead: float
    """Ball screw lead (mm/rev)"""

    max_velocity: float
    """Maximum velocity (mm/s)"""

    stroke_min: float
    """Minimum stroke value (mm)"""

    stroke_max: float
    """Maximum stroke value (mm)"""

    positioning_accuracy: float
    """Positioning accuracy (mm)"""

    repeat_accuracy: float
    """Repeat positioning accuracy (mm)"""

    max_payload_horizontal: float
    """Maximum horizontal payload (kg)"""

    max_payload_vertical: float
    """Maximum vertical payload (kg)"""

    # Encoder parameters (608Fh)
    encoder_resolution: int = 10000
    """Encoder resolution numerator - pulses per revolution (608Fh:01)"""

    encoder_resolution_denominator: int = 1
    """Encoder resolution denominator - motor revolutions (608Fh:02)"""

    # Gear ratio parameters (6091h)
    gear_ratio_numerator: int = 1
    """Gear ratio numerator - motor shaft revolutions (6091h:01)"""

    gear_ratio_denominator: int = 1
    """Gear ratio denominator - drive shaft revolutions (6091h:02)"""

    # Default motion parameters
    default_velocity: float = 100.0
    """Default velocity (mm/s)"""

    default_acceleration: float = 500.0
    """Default acceleration (mm/s²)"""

    default_deceleration: float = 500.0
    """Default deceleration (mm/s²); also used by Halt (Stop, jog release)"""

    quick_stop_deceleration: float = 1000.0
    """Quick stop (E-stop) deceleration (mm/s²)"""

    # Homing parameters
    homing_velocity_high: float = 50.0
    """Homing high speed (mm/s)"""

    homing_velocity_low: float = 10.0
    """Homing low speed (mm/s)"""

    homing_acceleration: float = 100.0
    """Homing acceleration (mm/s²)"""

    homing_method: Optional[int] = None
    """Homing method (HomingMethod enum value)"""

    homing_timeout: int = 60000
    """Homing timeout (ms), default 60 seconds"""

    driver_polarity: int = 0x00
    """Driver polarity (607Eh), used to reverse direction in hardware
    - 0x00: position forward, velocity forward
    - 0x40: position forward, velocity reversed
    - 0x80: position reversed, velocity forward
    - 0xC0: position reversed, velocity reversed
    """

    # DI configuration (digital inputs)
    di1_function: Optional[int] = None
    """DI1 function number (None=not configured)"""

    di1_logic: int = 0
    """DI1 logic (0=active low, 1=active high)"""

    di2_function: Optional[int] = None
    """DI2 function number"""

    di2_logic: int = 0
    """DI2 logic"""

    di3_function: Optional[int] = None
    """DI3 function number"""

    di3_logic: int = 0
    """DI3 logic"""

    # Stall homing parameters
    blocking_torque: int = 300
    """Stall homing torque threshold (0.1% units, 300=30%)"""

    blocking_time: int = 500
    """Stall detection time (ms)"""

    # Brake configuration (only valid when has_brake=True)
    brake_do_number: int = 1
    """DO port number used by the brake (default DO1)"""

    brake_do_logic: int = 1
    """Brake DO logic (0=release on low level, 1=release on high level)"""

    brake_release_delay_ms: int = 500
    """Brake release delay (ms)"""

    brake_auto_control: bool = True
    """Whether to enable automatic brake control (True=follows motor enable state)"""

    @property
    def stroke(self) -> float:
        """Effective stroke (mm)"""
        return self.stroke_max - self.stroke_min

    @property
    def mm_per_pulse(self) -> float:
        """Displacement per pulse (mm/pulse)"""
        return self.ball_screw_lead / self.encoder_resolution

    @property
    def pulses_per_mm(self) -> float:
        """Pulses per millimeter (pulse/mm)"""
        return self.encoder_resolution / self.ball_screw_lead

    def mm_to_pulses(self, mm: float) -> int:
        """
        Convert millimeters to pulses

        Args:
            mm: value in millimeters

        Returns:
            value in pulses
        """
        return int(mm * self.pulses_per_mm)

    def pulses_to_mm(self, pulses: int) -> float:
        """
        Convert pulses to millimeters

        Args:
            pulses: value in pulses

        Returns:
            value in millimeters
        """
        return pulses * self.mm_per_pulse

    def velocity_mm_to_pulses(self, velocity_mm_s: float) -> int:
        """
        Velocity: convert mm/s to pulse/s

        Args:
            velocity_mm_s: velocity (mm/s)

        Returns:
            velocity (pulse/s)
        """
        return int(velocity_mm_s * self.pulses_per_mm)

    def velocity_pulses_to_mm(self, velocity_pulses_s: int) -> float:
        """
        Velocity: convert pulse/s to mm/s

        Args:
            velocity_pulses_s: velocity (pulse/s)

        Returns:
            velocity (mm/s)
        """
        return velocity_pulses_s * self.mm_per_pulse

    def rpm_to_mm_s(self, rpm: float) -> float:
        """
        Convert motor speed to linear speed, signed like the position

        The actual-velocity register 606Ch reports motor rpm (unless 2006h:12h
        is changed), not user units/s, and in the motor's own direction:
        position polarity (607Eh bit 7) does not apply to it. Measured on the
        Y stage with polarity 0x80: -120 rpm while the position increased.
        With 0xC0 (velocity polarity as well) the reading is left as is;
        that case has not been measured.

        Args:
            rpm: motor speed (rpm)

        Returns:
            linear speed (mm/s), positive while the position increases
        """
        sign = -1.0 if (self.driver_polarity & 0xC0) == 0x80 else 1.0
        return sign * rpm * self.ball_screw_lead / 60.0

    def is_position_valid(self, position_mm: float) -> bool:
        """
        Check whether the position is within the valid stroke

        Args:
            position_mm: position (mm)

        Returns:
            whether the position is valid
        """
        return self.stroke_min <= position_mm <= self.stroke_max


# Platform predefined configurations
DEFAULT_AXIS_CONFIGS: Dict[AxisName, AxisConfig] = {
    AxisName.Z4: AxisConfig(
        name=AxisName.Z4,
        slave_id=4,
        model="PMMP4010D-485-OHEB-D04",
        motor_power=100,
        has_brake=True,
        brake_do_number=1,
        brake_do_logic=1,  # release on high level
        brake_release_delay_ms=500,
        brake_auto_control=True,
        ball_screw_lead=10.0,
        max_velocity=500.0,
        stroke_min=0.0,
        # ISS z_down needs 65 mm (61 mm measured); the lower limit switch is the physical protection
        stroke_max=65.0,
        positioning_accuracy=0.01,
        repeat_accuracy=0.005,
        max_payload_horizontal=20.0,
        max_payload_vertical=10.0,
        encoder_resolution=131072,  # 17-bit absolute encoder
        default_velocity=100.0,
        default_acceleration=500.0,
        default_deceleration=500.0,
        homing_velocity_high=20.0,
        homing_velocity_low=4.0,
        homing_acceleration=100.0,
        homing_method=18,  # POSITIVE_LIMIT_SWITCH: homes upward to the upper limit
        homing_timeout=60000,
        # Position polarity reversed, velocity normal: top = 0, position increases
        # downward; homing still moves up
        driver_polarity=0x80,
        # DI configuration
        di2_function=15,  # DI2 = negative limit switch
        di2_logic=0,  # active low
        di3_function=14,  # DI3 = positive limit switch
        di3_logic=0,  # active low
        # Stall homing parameters
        blocking_torque=300,  # 30%
        blocking_time=500,  # 500ms
    ),
    AxisName.Y2: AxisConfig(
        name=AxisName.Y2,
        slave_id=2,
        model="CFG5",
        motor_power=100,
        has_brake=False,
        ball_screw_lead=10.0,
        max_velocity=500.0,
        stroke_min=0,
        stroke_max=102,
        positioning_accuracy=0.015,
        repeat_accuracy=0.005,
        max_payload_horizontal=30.0,
        max_payload_vertical=15.0,
        # 17-bit motor; matches the value saved in the drive, so connecting does
        # not change 608Fh (changing it rescales and corrupts the position reading)
        encoder_resolution=131072,
        default_velocity=100.0,
        default_acceleration=500.0,
        default_deceleration=500.0,
        # 10 mm/s: the search overruns the origin switch by v^2 / 2a = 0.5 mm before
        # reversing (30 mm/s overran ~4.5 mm, with ~4.8 mm of room past the switch)
        homing_velocity_high=10.0,
        homing_velocity_low=5.0,
        homing_acceleration=100.0,
        homing_method=18,  # POSITIVE_LIMIT_SWITCH: home on the origin switch (DI3)
        homing_timeout=60000,  # 60-second homing timeout
        driver_polarity=0x80,  # position reversed: 0 at the origin, positive toward the far end
        # DI configuration (Y-axis limit switches)
        di2_function=15,  # DI2 = negative limit switch
        di2_logic=0,  # active low
        di3_function=14,  # DI3 = positive limit switch
        di3_logic=0,  # active low
        # Stall homing parameters
        blocking_torque=300,  # 30%
        blocking_time=500,  # 500ms
    ),
    AxisName.Z3: AxisConfig(
        name=AxisName.Z3,
        slave_id=3,
        model="CFG4",
        motor_power=100,
        has_brake=False,  # vertical, held by lead-screw friction when unpowered
        ball_screw_lead=10.0,
        max_velocity=500.0,
        stroke_min=0.0,
        stroke_max=58.5,  # lower switch closes at +59.7 mm after homing; stay clear of it
        positioning_accuracy=0.01,
        repeat_accuracy=0.005,
        max_payload_horizontal=20.0,
        max_payload_vertical=10.0,
        # 17-bit motor; matches the value saved in the drive, so connecting does
        # not change 608Fh (changing it rescales and corrupts the position reading)
        encoder_resolution=131072,
        driver_polarity=0x80,  # position reversed: 0 at the top, positive downward
        default_velocity=100.0,
        default_acceleration=500.0,
        default_deceleration=500.0,
        # 10 mm/s: the search overruns the upper switch by v^2 / 2a = 0.5 mm
        homing_velocity_high=10.0,
        homing_velocity_low=5.0,
        homing_acceleration=100.0,
        homing_method=18,  # POSITIVE_LIMIT_SWITCH: home on the upper switch (DI3)
        homing_timeout=60000,  # 60-second homing timeout
        # DI configuration (Z-axis limit switches, verified 2026-01-12)
        di2_function=15,  # DI2 = negative limit switch (lower limit)
        di2_logic=0,  # active low
        di3_function=14,  # DI3 = positive limit switch (upper limit)
        di3_logic=0,  # active low
        # Stall homing parameters
        blocking_torque=300,  # 30%
        blocking_time=500,  # 500ms
    ),
    AxisName.X1: AxisConfig(
        name=AxisName.X1,
        slave_id=1,
        model="PSMP (48 V, 200 W)",
        motor_power=200,
        has_brake=False,
        ball_screw_lead=20.0,  # 20 mm ball screw lead (Y and Z: 10 mm)
        max_velocity=500.0,
        stroke_min=0.0,
        stroke_max=200.0,  # far switch closes at +205.8 mm after homing; see CLAUDE.md
        positioning_accuracy=0.01,
        repeat_accuracy=0.005,
        max_payload_horizontal=30.0,
        max_payload_vertical=15.0,
        # Saved in the drive (2026-10-12). 17-bit encoder (the drive's 2000h:19h
        # reads 18); with the 20 mm lead this is 13107.2 units/mm, as on Y2/Z3.
        # At the factory 10000 the drive ran slow moves at the wrong speed
        # (1 mm/s as 2 mm/s) and unevenly. Matches the drive, so connecting never
        # changes 608Fh (a change rescales and corrupts the position reading).
        encoder_resolution=262144,
        driver_polarity=0x80,  # position reversed: 0 at the origin, positive toward the far end
        default_velocity=100.0,
        default_acceleration=500.0,
        default_deceleration=500.0,
        # 10 mm/s: the search overruns the origin switch by v^2 / 2a = 0.5 mm
        homing_velocity_high=10.0,
        homing_velocity_low=5.0,
        homing_acceleration=100.0,
        homing_method=18,  # POSITIVE_LIMIT_SWITCH: home on the origin switch (DI2)
        homing_timeout=60000,  # 60-second homing timeout
        # Limit switches: normally closed, on isolated inputs powered from an
        # external 24 V supply; a pressed switch (or no supply) reads 0
        di1_function=15,  # DI1 = negative limit switch (far end)
        di1_logic=0,  # active low
        di2_function=14,  # DI2 = positive limit switch (origin end)
        di2_logic=0,  # active low
        # Stall homing parameters
        blocking_torque=300,  # 30%
        blocking_time=500,  # 500ms
    ),
}


def get_axis_config(axis: AxisName) -> AxisConfig:
    """
    Get an axis configuration

    Args:
        axis: axis name

    Returns:
        axis configuration
    """
    return DEFAULT_AXIS_CONFIGS[axis]


def get_all_axis_configs() -> Dict[AxisName, AxisConfig]:
    """
    Get all axis configurations

    Returns:
        dictionary of axis configurations
    """
    return DEFAULT_AXIS_CONFIGS.copy()
