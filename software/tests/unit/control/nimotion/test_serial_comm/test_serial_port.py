"""
Serial port class tests
"""

from unittest.mock import MagicMock, patch

import pytest
import serial

from fluidics.control.nimotion.serial_comm.exceptions import (
    PortAccessError,
    PortConfigError,
    PortNotFoundError,
    PortNotOpenError,
    PortOpenError,
    ReadTimeoutError,
    WriteError,
)
from fluidics.control.nimotion.serial_comm.serial_port import SerialPort


class TestSerialPortInit:
    """Test SerialPort initialization"""

    def test_default_config(self) -> None:
        """Test default config"""
        port = SerialPort()
        assert port.port_name is None
        assert port.baudrate == 115200
        assert port.is_open is False

    def test_custom_config(self) -> None:
        """Test custom config"""
        port = SerialPort(
            port="/dev/ttyUSB0",
            baudrate=9600,
            timeout=0.5,
        )
        assert port.port_name == "/dev/ttyUSB0"
        assert port.baudrate == 9600
        assert port.timeout == 0.5


class TestSerialPortOpen:
    """Test opening the serial port"""

    def test_open_success(self) -> None:
        """Test successful open"""
        port = SerialPort(port="/dev/ttyUSB0")

        with patch("serial.Serial") as mock_serial:
            mock_instance = MagicMock()
            mock_instance.is_open = True
            mock_serial.return_value = mock_instance

            port.open()

            assert port.is_open is True
            mock_serial.assert_called_once()

    def test_open_with_port_parameter(self) -> None:
        """Test open with port parameter"""
        port = SerialPort()

        with patch("serial.Serial") as mock_serial:
            mock_instance = MagicMock()
            mock_instance.is_open = True
            mock_serial.return_value = mock_instance

            port.open("/dev/ttyUSB0")

            assert port.port_name == "/dev/ttyUSB0"
            assert port.is_open is True

    def test_open_no_port_name(self) -> None:
        """Test that an exception is raised when no port name is given"""
        port = SerialPort()

        with pytest.raises(PortOpenError) as exc_info:
            port.open()
        assert "No serial port name specified" in str(exc_info.value)

    def test_open_already_open(self) -> None:
        """Test opening again when already open"""
        port = SerialPort(port="/dev/ttyUSB0")

        with patch("serial.Serial") as mock_serial:
            mock_instance = MagicMock()
            mock_instance.is_open = True
            mock_serial.return_value = mock_instance

            port.open()
            port.open()  # Should not raise, only warn

            # Serial should only be called once
            mock_serial.assert_called_once()

    def test_open_port_not_found(self) -> None:
        """Test port not found"""
        port = SerialPort(port="/dev/ttyUSB99")

        with patch("serial.Serial") as mock_serial:
            mock_serial.side_effect = serial.SerialException("no such file")

            with pytest.raises(PortNotFoundError):
                port.open()

    def test_open_permission_denied(self) -> None:
        """Test permission denied"""
        port = SerialPort(port="/dev/ttyUSB0")

        with patch("serial.Serial") as mock_serial:
            mock_serial.side_effect = serial.SerialException("permission denied")

            with pytest.raises(PortAccessError):
                port.open()

    def test_open_other_error(self) -> None:
        """Test other errors"""
        port = SerialPort(port="/dev/ttyUSB0")

        with patch("serial.Serial") as mock_serial:
            mock_serial.side_effect = serial.SerialException("device busy")

            with pytest.raises(PortOpenError):
                port.open()


class TestSerialPortClose:
    """Test closing the serial port"""

    def test_close_open_port(self) -> None:
        """Test closing an open port"""
        port = SerialPort(port="/dev/ttyUSB0")

        with patch("serial.Serial") as mock_serial:
            mock_instance = MagicMock()
            mock_instance.is_open = True
            mock_serial.return_value = mock_instance

            port.open()
            port.close()

            mock_instance.close.assert_called_once()
            assert port.is_open is False

    def test_close_not_open_port(self) -> None:
        """Test closing a port that is not open"""
        port = SerialPort(port="/dev/ttyUSB0")
        port.close()  # Should not raise


class TestSerialPortWrite:
    """Test serial port write"""

    def test_write_success(self) -> None:
        """Test successful write"""
        port = SerialPort(port="/dev/ttyUSB0")

        with patch("serial.Serial") as mock_serial:
            mock_instance = MagicMock()
            mock_instance.is_open = True
            mock_instance.write.return_value = 5
            mock_serial.return_value = mock_instance

            port.open()
            result = port.write(b"\x01\x02\x03\x04\x05")

            assert result == 5
            mock_instance.write.assert_called_once_with(b"\x01\x02\x03\x04\x05")

    def test_write_not_open(self) -> None:
        """Test write when not open"""
        port = SerialPort(port="/dev/ttyUSB0")

        with pytest.raises(PortNotOpenError):
            port.write(b"\x01")

    def test_write_error(self) -> None:
        """Test write error"""
        port = SerialPort(port="/dev/ttyUSB0")

        with patch("serial.Serial") as mock_serial:
            mock_instance = MagicMock()
            mock_instance.is_open = True
            mock_instance.write.side_effect = serial.SerialException("write error")
            mock_serial.return_value = mock_instance

            port.open()

            with pytest.raises(WriteError):
                port.write(b"\x01")


class TestSerialPortRead:
    """Test serial port read"""

    def test_read_success(self) -> None:
        """Test successful read"""
        port = SerialPort(port="/dev/ttyUSB0")

        with patch("serial.Serial") as mock_serial:
            mock_instance = MagicMock()
            mock_instance.is_open = True
            mock_instance.read.return_value = b"\x01\x02\x03"
            mock_serial.return_value = mock_instance

            port.open()
            result = port.read(3)

            assert result == b"\x01\x02\x03"

    def test_read_not_open(self) -> None:
        """Test read when not open"""
        port = SerialPort(port="/dev/ttyUSB0")

        with pytest.raises(PortNotOpenError):
            port.read(1)

    def test_read_all_success(self) -> None:
        """Test reading all data"""
        port = SerialPort(port="/dev/ttyUSB0")

        with patch("serial.Serial") as mock_serial:
            mock_instance = MagicMock()
            mock_instance.is_open = True
            mock_instance.in_waiting = 5
            mock_instance.read.return_value = b"\x01\x02\x03\x04\x05"
            mock_serial.return_value = mock_instance

            port.open()
            result = port.read_all()

            assert result == b"\x01\x02\x03\x04\x05"

    def test_read_until_success(self) -> None:
        """Test reading until the length is reached"""
        port = SerialPort(port="/dev/ttyUSB0")

        with patch("serial.Serial") as mock_serial:
            mock_instance = MagicMock()
            mock_instance.is_open = True
            # Simulate reading in two chunks
            mock_instance.read.side_effect = [b"\x01\x02", b"\x03\x04\x05"]
            mock_serial.return_value = mock_instance

            port.open()
            result = port.read_until(expected_length=5, timeout=1.0)

            assert result == b"\x01\x02\x03\x04\x05"

    def test_read_until_timeout(self) -> None:
        """Test read timeout"""
        port = SerialPort(port="/dev/ttyUSB0")

        with patch("serial.Serial") as mock_serial:
            mock_instance = MagicMock()
            mock_instance.is_open = True
            mock_instance.read.return_value = b""  # No data
            mock_serial.return_value = mock_instance

            port.open()

            with pytest.raises(ReadTimeoutError) as exc_info:
                port.read_until(expected_length=5, timeout=0.1)

            assert exc_info.value.expected_bytes == 5
            assert exc_info.value.received_bytes == 0


class TestSerialPortFlush:
    """Test buffer flushing"""

    def test_flush_input(self) -> None:
        """Test flushing the input buffer"""
        port = SerialPort(port="/dev/ttyUSB0")

        with patch("serial.Serial") as mock_serial:
            mock_instance = MagicMock()
            mock_instance.is_open = True
            mock_serial.return_value = mock_instance

            port.open()
            port.flush_input()

            mock_instance.reset_input_buffer.assert_called_once()

    def test_flush_output(self) -> None:
        """Test flushing the output buffer"""
        port = SerialPort(port="/dev/ttyUSB0")

        with patch("serial.Serial") as mock_serial:
            mock_instance = MagicMock()
            mock_instance.is_open = True
            mock_serial.return_value = mock_instance

            port.open()
            port.flush_output()

            mock_instance.reset_output_buffer.assert_called_once()


class TestSerialPortConfig:
    """Test serial port configuration"""

    def test_set_baudrate_valid(self) -> None:
        """Test setting a valid baud rate"""
        port = SerialPort(port="/dev/ttyUSB0")

        with patch("serial.Serial") as mock_serial:
            mock_instance = MagicMock()
            mock_instance.is_open = True
            mock_serial.return_value = mock_instance

            port.open()
            port.set_baudrate(9600)

            assert port.baudrate == 9600
            assert mock_instance.baudrate == 9600

    def test_set_baudrate_invalid(self) -> None:
        """Test setting an invalid baud rate"""
        port = SerialPort()

        with pytest.raises(PortConfigError) as exc_info:
            port.set_baudrate(999999)

        assert exc_info.value.parameter == "baudrate"

    def test_configure(self) -> None:
        """Test configuring multiple parameters"""
        port = SerialPort(port="/dev/ttyUSB0")

        with patch("serial.Serial") as mock_serial:
            mock_instance = MagicMock()
            mock_instance.is_open = True
            mock_serial.return_value = mock_instance

            port.open()
            port.configure(baudrate=9600, timeout=0.5)

            assert port.baudrate == 9600
            assert port.timeout == 0.5

    def test_timeout_setter(self) -> None:
        """Test the timeout setter"""
        port = SerialPort(port="/dev/ttyUSB0")

        with patch("serial.Serial") as mock_serial:
            mock_instance = MagicMock()
            mock_instance.is_open = True
            mock_serial.return_value = mock_instance

            port.open()
            port.timeout = 0.5

            assert port.timeout == 0.5
            assert mock_instance.timeout == 0.5


class TestSerialPortProperties:
    """Test property access"""

    def test_in_waiting(self) -> None:
        """Test input buffer byte count"""
        port = SerialPort(port="/dev/ttyUSB0")

        with patch("serial.Serial") as mock_serial:
            mock_instance = MagicMock()
            mock_instance.is_open = True
            mock_instance.in_waiting = 10
            mock_serial.return_value = mock_instance

            port.open()
            assert port.in_waiting == 10

    def test_out_waiting(self) -> None:
        """Test output buffer byte count"""
        port = SerialPort(port="/dev/ttyUSB0")

        with patch("serial.Serial") as mock_serial:
            mock_instance = MagicMock()
            mock_instance.is_open = True
            mock_instance.out_waiting = 5
            mock_serial.return_value = mock_instance

            port.open()
            assert port.out_waiting == 5

    def test_in_waiting_closed(self) -> None:
        """Test input buffer when closed"""
        port = SerialPort()
        assert port.in_waiting == 0


class TestSerialPortContextManager:
    """Test context manager"""

    def test_context_manager(self) -> None:
        """Test the with statement"""
        with patch("serial.Serial") as mock_serial:
            mock_instance = MagicMock()
            mock_instance.is_open = True
            mock_serial.return_value = mock_instance

            with SerialPort(port="/dev/ttyUSB0") as port:
                assert port.is_open is True

            mock_instance.close.assert_called()


class TestSerialPortRepr:
    """Test string representation"""

    def test_repr_closed(self) -> None:
        """Test representation when closed"""
        port = SerialPort(port="/dev/ttyUSB0", baudrate=9600)
        assert "ttyUSB0" in repr(port)
        assert "9600" in repr(port)
        assert "closed" in repr(port)

    def test_repr_open(self) -> None:
        """Test representation when open"""
        port = SerialPort(port="/dev/ttyUSB0")

        with patch("serial.Serial") as mock_serial:
            mock_instance = MagicMock()
            mock_instance.is_open = True
            mock_serial.return_value = mock_instance

            port.open()
            assert "open" in repr(port)
