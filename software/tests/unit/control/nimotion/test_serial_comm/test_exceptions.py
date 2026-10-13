"""
Serial port exception class tests
"""

from fluidics.control.nimotion.serial_comm.exceptions import (
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


class TestSerialCommError:
    """Test SerialCommError base class"""

    def test_basic_message(self) -> None:
        """Test basic message"""
        error = SerialCommError("test error")
        assert str(error) == "test error"
        assert error.message == "test error"
        assert error.details == {}

    def test_with_details(self) -> None:
        """Test with details"""
        error = SerialCommError("test error", details={"key": "value"})
        assert error.details == {"key": "value"}


class TestSerialPortError:
    """Test SerialPortError"""

    def test_with_port_name(self) -> None:
        """Test with port name"""
        error = SerialPortError("port error", port_name="/dev/ttyUSB0")
        assert error.port_name == "/dev/ttyUSB0"
        assert error.details["port_name"] == "/dev/ttyUSB0"


class TestPortNotFoundError:
    """Test PortNotFoundError"""

    def test_message_format(self) -> None:
        """Test message format"""
        error = PortNotFoundError("/dev/ttyUSB0")
        assert "ttyUSB0" in str(error)
        assert error.port_name == "/dev/ttyUSB0"


class TestPortOpenError:
    """Test PortOpenError"""

    def test_without_reason(self) -> None:
        """Test without reason"""
        error = PortOpenError("/dev/ttyUSB0")
        assert "ttyUSB0" in str(error)
        assert error.reason == ""

    def test_with_reason(self) -> None:
        """Test with reason"""
        error = PortOpenError("/dev/ttyUSB0", "device busy")
        assert "device busy" in str(error)
        assert error.reason == "device busy"


class TestPortAccessError:
    """Test PortAccessError"""

    def test_message_format(self) -> None:
        """Test message format"""
        error = PortAccessError("/dev/ttyUSB0")
        assert "permission" in str(error)
        assert error.port_name == "/dev/ttyUSB0"


class TestPortNotOpenError:
    """Test PortNotOpenError"""

    def test_without_port_name(self) -> None:
        """Test without port name"""
        error = PortNotOpenError()
        assert "not open" in str(error)

    def test_with_port_name(self) -> None:
        """Test with port name"""
        error = PortNotOpenError("/dev/ttyUSB0")
        assert "ttyUSB0" in str(error)


class TestReadTimeoutError:
    """Test ReadTimeoutError"""

    def test_basic_timeout(self) -> None:
        """Test basic timeout"""
        error = ReadTimeoutError(timeout=0.5)
        assert "0.5" in str(error)
        assert error.timeout == 0.5
        assert error.expected_bytes == 0
        assert error.received_bytes == 0

    def test_with_bytes_info(self) -> None:
        """Test with bytes info"""
        error = ReadTimeoutError(timeout=1.0, expected_bytes=10, received_bytes=5)
        assert "10" in str(error)
        assert "5" in str(error)
        assert error.expected_bytes == 10
        assert error.received_bytes == 5


class TestWriteError:
    """Test WriteError"""

    def test_default_message(self) -> None:
        """Test default message"""
        error = WriteError()
        assert "Write failed" in str(error)

    def test_with_bytes_count(self) -> None:
        """Test with bytes count"""
        error = WriteError("write error", bytes_to_write=100)
        assert error.bytes_to_write == 100


class TestPortConfigError:
    """Test PortConfigError"""

    def test_with_parameter(self) -> None:
        """Test with parameter info"""
        error = PortConfigError("invalid baud rate", parameter="baudrate", value="999999")
        assert error.parameter == "baudrate"
        assert error.value == "999999"
