"""
Modbus RTU exception definitions module

Defines the various exceptions that can occur during Modbus communication.
"""

from typing import Optional


class ModbusError(Exception):
    """Base exception class for Modbus communication"""

    def __init__(self, message: str, slave_id: Optional[int] = None):
        """
        Initialize the exception

        Args:
            message: Exception message
            slave_id: Slave address
        """
        super().__init__(message)
        self.message = message
        self.slave_id = slave_id

    def __str__(self) -> str:
        return self.message


class ModbusExceptionResponse(ModbusError):
    """Modbus exception response

    Raised when the slave returns an exception function code.
    """

    # Modbus standard exception codes
    EXCEPTION_CODES = {
        0x01: "Illegal function code",
        0x02: "Illegal data address",
        0x03: "Illegal data value",
        0x04: "Slave device failure",
        0x05: "Acknowledge (processing in progress)",
        0x06: "Slave device busy",
        0x08: "Memory parity error",
        0x0A: "Gateway path unavailable",
        0x0B: "Gateway target device failed to respond",
    }

    def __init__(
        self,
        function_code: int,
        exception_code: int,
        slave_id: Optional[int] = None,
    ):
        """
        Initialize

        Args:
            function_code: Function code (with exception flag)
            exception_code: Exception code
            slave_id: Slave address
        """
        original_fc = function_code & 0x7F
        exception_name = self.EXCEPTION_CODES.get(
            exception_code, f"Unknown exception (0x{exception_code:02X})"
        )
        message = (
            f"Modbus exception response: FC=0x{original_fc:02X}, "
            f"exception code=0x{exception_code:02X} ({exception_name})"
        )

        super().__init__(message, slave_id)
        self.function_code = function_code
        self.original_function_code = original_fc
        self.exception_code = exception_code
        self.exception_name = exception_name

    @property
    def is_illegal_function(self) -> bool:
        """Whether this is an illegal function code exception"""
        return self.exception_code == 0x01

    @property
    def is_illegal_address(self) -> bool:
        """Whether this is an illegal address exception"""
        return self.exception_code == 0x02

    @property
    def is_illegal_value(self) -> bool:
        """Whether this is an illegal data value exception"""
        return self.exception_code == 0x03

    @property
    def is_device_failure(self) -> bool:
        """Whether this is a device failure"""
        return self.exception_code == 0x04


class CRCError(ModbusError):
    """CRC checksum error"""

    def __init__(
        self,
        expected_crc: int,
        received_crc: int,
        slave_id: Optional[int] = None,
    ):
        """
        Initialize

        Args:
            expected_crc: Expected CRC
            received_crc: Actually received CRC
            slave_id: Slave address
        """
        message = f"CRC check failed: expected 0x{expected_crc:04X}, got 0x{received_crc:04X}"
        super().__init__(message, slave_id)
        self.expected_crc = expected_crc
        self.received_crc = received_crc


class FrameError(ModbusError):
    """Frame format error"""

    def __init__(
        self,
        message: str = "Invalid frame format",
        raw_data: Optional[bytes] = None,
        slave_id: Optional[int] = None,
    ):
        """
        Initialize

        Args:
            message: Error message
            raw_data: Raw data
            slave_id: Slave address
        """
        super().__init__(message, slave_id)
        self.raw_data = raw_data


class ResponseError(ModbusError):
    """Response error"""

    def __init__(
        self,
        message: str,
        expected_slave_id: Optional[int] = None,
        received_slave_id: Optional[int] = None,
        expected_fc: Optional[int] = None,
        received_fc: Optional[int] = None,
    ):
        """
        Initialize

        Args:
            message: Error message
            expected_slave_id: Expected slave address
            received_slave_id: Actually received slave address
            expected_fc: Expected function code
            received_fc: Actually received function code
        """
        super().__init__(message, expected_slave_id)
        self.expected_slave_id = expected_slave_id
        self.received_slave_id = received_slave_id
        self.expected_fc = expected_fc
        self.received_fc = received_fc


class SlaveIdMismatchError(ResponseError):
    """Slave address mismatch error"""

    def __init__(self, expected_slave_id: int, received_slave_id: int):
        """
        Initialize

        Args:
            expected_slave_id: Expected slave address
            received_slave_id: Actually received slave address
        """
        message = f"Slave address mismatch: expected {expected_slave_id}, got {received_slave_id}"
        super().__init__(
            message,
            expected_slave_id=expected_slave_id,
            received_slave_id=received_slave_id,
        )


class FunctionCodeMismatchError(ResponseError):
    """Function code mismatch error"""

    def __init__(
        self,
        expected_fc: int,
        received_fc: int,
        slave_id: Optional[int] = None,
    ):
        """
        Initialize

        Args:
            expected_fc: Expected function code
            received_fc: Actually received function code
            slave_id: Slave address
        """
        message = f"Function code mismatch: expected 0x{expected_fc:02X}, got 0x{received_fc:02X}"
        super().__init__(
            message,
            expected_slave_id=slave_id,
            expected_fc=expected_fc,
            received_fc=received_fc,
        )


class TimeoutError(ModbusError):
    """Modbus communication timeout"""

    def __init__(
        self,
        timeout: float,
        slave_id: Optional[int] = None,
        message: Optional[str] = None,
    ):
        """
        Initialize

        Args:
            timeout: Timeout in seconds
            slave_id: Slave address
            message: Custom message
        """
        if message is None:
            message = f"Modbus communication timeout ({timeout}s)"
            if slave_id is not None:
                message += f", slave address: {slave_id}"
        super().__init__(message, slave_id)
        self.timeout = timeout


class NoResponseError(ModbusError):
    """No-response error

    The slave did not respond to the request.
    """

    def __init__(self, slave_id: Optional[int] = None, retries: int = 0):
        """
        Initialize

        Args:
            slave_id: Slave address
            retries: Number of retries
        """
        message = "No response from slave"
        if slave_id is not None:
            message = f"No response from slave {slave_id}"
        if retries > 0:
            message += f" (retried {retries} times)"
        super().__init__(message, slave_id)
        self.retries = retries


class BroadcastError(ModbusError):
    """Broadcast error

    A broadcast request should not expect a response.
    """

    def __init__(self, message: str = "Broadcast request has no response"):
        """
        Initialize

        Args:
            message: Error message
        """
        super().__init__(message, slave_id=0)
