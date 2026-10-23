"""
High-Level API Module

Provides high-level interfaces for servo motor control.
"""

from .axis_config import (
    DEFAULT_AXIS_CONFIGS,
    AxisConfig,
    AxisName,
    get_all_axis_configs,
    get_axis_config,
)
from .servo_service import AxisStatus, ServoService

__all__ = [
    # Services
    "ServoService",
    "AxisStatus",
    # Axis configuration
    "AxisConfig",
    "AxisName",
    "DEFAULT_AXIS_CONFIGS",
    "get_axis_config",
    "get_all_axis_configs",
]
