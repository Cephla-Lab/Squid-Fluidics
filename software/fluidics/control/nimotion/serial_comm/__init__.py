"""
Serial communication module

Provides serial device discovery, connection, and read/write functionality.
"""

from .exceptions import (
    PortAccessError,
    PortConfigError,
    PortNotFoundError,
    PortNotOpenError,
    PortOpenError,
    ReadTimeoutError,
    SerialCommError,
    SerialPortError,
    WriteError,
)
from .port_scanner import PortInfo, PortScanner
from .serial_port import SerialPort

__all__ = [
    # Exception classes
    "SerialCommError",
    "SerialPortError",
    "PortNotFoundError",
    "PortOpenError",
    "PortAccessError",
    "PortNotOpenError",
    "ReadTimeoutError",
    "WriteError",
    "PortConfigError",
    # Port scanning
    "PortInfo",
    "PortScanner",
    # Serial communication
    "SerialPort",
]
