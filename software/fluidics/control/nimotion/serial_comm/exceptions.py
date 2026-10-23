"""
Serial communication exception definitions module

Defines the various exceptions that can occur during serial communication.
"""

from typing import Optional


class SerialCommError(Exception):
    """Base exception class for serial communication"""

    def __init__(self, message: str, details: Optional[dict] = None):
        """
        Initialize exception

        Args:
            message: Exception message
            details: Dictionary of detailed information
        """
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        return self.message


class SerialPortError(SerialCommError):
    """Base class for serial port errors"""

    def __init__(self, message: str, port_name: Optional[str] = None):
        """
        Initialize serial port error

        Args:
            message: Error message
            port_name: Serial port name
        """
        super().__init__(message, details={"port_name": port_name})
        self.port_name = port_name


class PortNotFoundError(SerialPortError):
    """Serial port not found error"""

    def __init__(self, port_name: str):
        """
        Initialize

        Args:
            port_name: Serial port name
        """
        super().__init__(f"Serial port '{port_name}' not found", port_name)


class PortOpenError(SerialPortError):
    """Serial port open error"""

    def __init__(self, port_name: str, reason: str = ""):
        """
        Initialize

        Args:
            port_name: Serial port name
            reason: Error reason
        """
        msg = f"Cannot open serial port '{port_name}'"
        if reason:
            msg += f": {reason}"
        super().__init__(msg, port_name)
        self.reason = reason


class PortAccessError(SerialPortError):
    """Serial port access permission error"""

    def __init__(self, port_name: str):
        """
        Initialize

        Args:
            port_name: Serial port name
        """
        super().__init__(f"No permission to access serial port '{port_name}'", port_name)


class PortNotOpenError(SerialPortError):
    """Serial port not open error"""

    def __init__(self, port_name: Optional[str] = None):
        """
        Initialize

        Args:
            port_name: Serial port name
        """
        msg = "Serial port not open"
        if port_name:
            msg = f"Serial port '{port_name}' not open"
        super().__init__(msg, port_name)


class ReadTimeoutError(SerialCommError):
    """Read timeout error"""

    def __init__(self, timeout: float, expected_bytes: int = 0, received_bytes: int = 0):
        """
        Initialize

        Args:
            timeout: Timeout duration (seconds)
            expected_bytes: Number of bytes expected to read
            received_bytes: Number of bytes actually read
        """
        msg = f"Read timeout ({timeout}s)"
        if expected_bytes > 0:
            msg += f", expected {expected_bytes} bytes, received {received_bytes} bytes"
        super().__init__(
            msg,
            details={
                "timeout": timeout,
                "expected_bytes": expected_bytes,
                "received_bytes": received_bytes,
            },
        )
        self.timeout = timeout
        self.expected_bytes = expected_bytes
        self.received_bytes = received_bytes


class WriteError(SerialCommError):
    """Write error"""

    def __init__(self, message: str = "Write failed", bytes_to_write: int = 0):
        """
        Initialize

        Args:
            message: Error message
            bytes_to_write: Number of bytes attempted to write
        """
        super().__init__(message, details={"bytes_to_write": bytes_to_write})
        self.bytes_to_write = bytes_to_write


class PortConfigError(SerialCommError):
    """Serial port configuration error"""

    def __init__(self, message: str, parameter: Optional[str] = None, value: Optional[str] = None):
        """
        Initialize

        Args:
            message: Error message
            parameter: Parameter name
            value: Parameter value
        """
        super().__init__(message, details={"parameter": parameter, "value": value})
        self.parameter = parameter
        self.value = value
