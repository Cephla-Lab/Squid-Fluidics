"""
NiMotion servo motor control service

Provides complete servo motor control functionality, including:
- Serial communication
- Modbus RTU protocol
- CiA402 state machine
- Multi-axis control

Usage example:
    from servo_service import ServoService, AxisName

    # Create the service and connect
    service = ServoService(port="/dev/ttyUSB0")
    service.connect()

    # Select an axis
    service.current_axis = AxisName.Z4

    # Enable the motor
    service.enable()

    # Move to a target position
    service.move_to(50.0)  # move to 50mm

    # Disconnect
    service.disconnect()
"""

from .high_level_api import AxisConfig, AxisName, AxisStatus, ServoService, get_axis_config
from .modbus_rtu import ModbusClient, ModbusError
from .motor_control import DriveState, HomingMethod, Motor, MotorError, MotorStatus, OperationMode
from .serial_comm import PortInfo, PortScanner, SerialCommError, SerialPort

__version__ = "0.1.0"

__all__ = [
    # Version
    "__version__",
    # High-level API
    "ServoService",
    "AxisStatus",
    "AxisConfig",
    "AxisName",
    "get_axis_config",
    # Motor control
    "Motor",
    "MotorStatus",
    "MotorError",
    "DriveState",
    "OperationMode",
    "HomingMethod",
    # Modbus
    "ModbusClient",
    "ModbusError",
    # Serial communication
    "SerialPort",
    "PortScanner",
    "PortInfo",
    "SerialCommError",
]
