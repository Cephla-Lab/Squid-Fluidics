"""
Serial port scanner tests
"""

from unittest.mock import MagicMock, patch

from fluidics.control.nimotion.serial_comm.port_scanner import PortInfo, PortScanner


class TestPortInfo:
    """Test PortInfo data class"""

    def test_basic_creation(self) -> None:
        """Test basic creation"""
        info = PortInfo(
            device="/dev/ttyUSB0",
            description="USB Serial",
            hwid="USB VID:PID=0403:6001",
        )
        assert info.device == "/dev/ttyUSB0"
        assert info.description == "USB Serial"

    def test_display_name_with_description(self) -> None:
        """Test display name with description"""
        info = PortInfo(
            device="/dev/ttyUSB0",
            description="USB Serial Port",
            hwid="",
        )
        assert info.display_name == "/dev/ttyUSB0 - USB Serial Port"

    def test_display_name_without_description(self) -> None:
        """Test display name without description"""
        info = PortInfo(
            device="/dev/ttyUSB0",
            description="n/a",
            hwid="",
        )
        assert info.display_name == "/dev/ttyUSB0"

    def test_is_usb_true(self) -> None:
        """Test USB device recognition"""
        info = PortInfo(
            device="/dev/ttyUSB0",
            description="",
            hwid="",
            vid=0x0403,
            pid=0x6001,
        )
        assert info.is_usb is True

    def test_is_usb_false(self) -> None:
        """Test non-USB device"""
        info = PortInfo(
            device="/dev/ttyS0",
            description="",
            hwid="",
        )
        assert info.is_usb is False

    def test_str(self) -> None:
        """Test string representation"""
        info = PortInfo(
            device="COM3",
            description="USB Serial",
            hwid="",
        )
        assert str(info) == "COM3 - USB Serial"

    def test_from_list_port_info(self) -> None:
        """Test creation from ListPortInfo"""
        mock_info = MagicMock()
        mock_info.device = "/dev/ttyUSB0"
        mock_info.description = "Test Device"
        mock_info.hwid = "USB VID:PID=1234:5678"
        mock_info.manufacturer = "Test Mfg"
        mock_info.product = "Test Product"
        mock_info.serial_number = "12345"
        mock_info.vid = 0x1234
        mock_info.pid = 0x5678

        port_info = PortInfo.from_list_port_info(mock_info)

        assert port_info.device == "/dev/ttyUSB0"
        assert port_info.description == "Test Device"
        assert port_info.manufacturer == "Test Mfg"
        assert port_info.vid == 0x1234
        assert port_info.pid == 0x5678


class TestPortScanner:
    """Test PortScanner"""

    def test_scan_ports_empty(self) -> None:
        """Test empty scan result"""
        scanner = PortScanner()

        with patch("serial.tools.list_ports.comports", return_value=[]):
            ports = scanner.scan_ports()
            assert ports == []

    def test_scan_ports_with_results(self) -> None:
        """Test scan with results"""
        scanner = PortScanner()

        mock_port1 = MagicMock()
        mock_port1.device = "/dev/ttyUSB0"
        mock_port1.description = "USB Serial 1"
        mock_port1.hwid = "USB VID:PID=0403:6001"
        mock_port1.manufacturer = None
        mock_port1.product = None
        mock_port1.serial_number = None
        mock_port1.vid = 0x0403
        mock_port1.pid = 0x6001

        mock_port2 = MagicMock()
        mock_port2.device = "/dev/ttyUSB1"
        mock_port2.description = "USB Serial 2"
        mock_port2.hwid = "USB VID:PID=1A86:7523"
        mock_port2.manufacturer = None
        mock_port2.product = None
        mock_port2.serial_number = None
        mock_port2.vid = 0x1A86
        mock_port2.pid = 0x7523

        with patch(
            "serial.tools.list_ports.comports",
            return_value=[mock_port2, mock_port1],
        ):
            ports = scanner.scan_ports()

            # Should be sorted by device name
            assert len(ports) == 2
            assert ports[0].device == "/dev/ttyUSB0"
            assert ports[1].device == "/dev/ttyUSB1"

    def test_is_port_available(self) -> None:
        """Test port availability check"""
        scanner = PortScanner()

        mock_port = MagicMock()
        mock_port.device = "/dev/ttyUSB0"
        mock_port.description = ""
        mock_port.hwid = ""
        mock_port.manufacturer = None
        mock_port.product = None
        mock_port.serial_number = None
        mock_port.vid = None
        mock_port.pid = None

        with patch("serial.tools.list_ports.comports", return_value=[mock_port]):
            assert scanner.is_port_available("/dev/ttyUSB0") is True
            assert scanner.is_port_available("/dev/ttyUSB1") is False

    def test_find_port(self) -> None:
        """Test finding a port"""
        scanner = PortScanner()

        mock_port = MagicMock()
        mock_port.device = "/dev/ttyUSB0"
        mock_port.description = "Test Port"
        mock_port.hwid = ""
        mock_port.manufacturer = None
        mock_port.product = None
        mock_port.serial_number = None
        mock_port.vid = None
        mock_port.pid = None

        with patch("serial.tools.list_ports.comports", return_value=[mock_port]):
            found = scanner.find_port("/dev/ttyUSB0")
            assert found is not None
            assert found.device == "/dev/ttyUSB0"

            not_found = scanner.find_port("/dev/ttyUSB1")
            assert not_found is None

    def test_find_rs485_adapters(self) -> None:
        """Test finding RS485 adapters"""
        scanner = PortScanner()

        # FTDI FT232 - known RS485 adapter
        mock_ftdi = MagicMock()
        mock_ftdi.device = "/dev/ttyUSB0"
        mock_ftdi.description = "FT232"
        mock_ftdi.hwid = ""
        mock_ftdi.manufacturer = None
        mock_ftdi.product = None
        mock_ftdi.serial_number = None
        mock_ftdi.vid = 0x0403
        mock_ftdi.pid = 0x6001

        # Unknown device
        mock_unknown = MagicMock()
        mock_unknown.device = "/dev/ttyUSB1"
        mock_unknown.description = "Unknown"
        mock_unknown.hwid = ""
        mock_unknown.manufacturer = None
        mock_unknown.product = None
        mock_unknown.serial_number = None
        mock_unknown.vid = 0x9999
        mock_unknown.pid = 0x9999

        with patch(
            "serial.tools.list_ports.comports",
            return_value=[mock_ftdi, mock_unknown],
        ):
            adapters = scanner.find_rs485_adapters()
            assert len(adapters) == 1
            assert adapters[0].device == "/dev/ttyUSB0"

    def test_find_by_vid_pid(self) -> None:
        """Test finding by VID:PID"""
        scanner = PortScanner()

        mock_port = MagicMock()
        mock_port.device = "/dev/ttyUSB0"
        mock_port.description = ""
        mock_port.hwid = ""
        mock_port.manufacturer = None
        mock_port.product = None
        mock_port.serial_number = None
        mock_port.vid = 0x1A86
        mock_port.pid = 0x7523

        with patch("serial.tools.list_ports.comports", return_value=[mock_port]):
            found = scanner.find_by_vid_pid(0x1A86, 0x7523)
            assert len(found) == 1
            assert found[0].device == "/dev/ttyUSB0"

            not_found = scanner.find_by_vid_pid(0x0000, 0x0000)
            assert len(not_found) == 0

    def test_find_by_serial_number(self) -> None:
        """Test finding by serial number"""
        scanner = PortScanner()

        mock_port = MagicMock()
        mock_port.device = "/dev/ttyUSB0"
        mock_port.description = ""
        mock_port.hwid = ""
        mock_port.manufacturer = None
        mock_port.product = None
        mock_port.serial_number = "ABC12345"
        mock_port.vid = None
        mock_port.pid = None

        with patch("serial.tools.list_ports.comports", return_value=[mock_port]):
            found = scanner.find_by_serial_number("ABC12345")
            assert found is not None
            assert found.device == "/dev/ttyUSB0"

            not_found = scanner.find_by_serial_number("XYZ")
            assert not_found is None

    def test_get_available_ports_caching(self) -> None:
        """Test caching behavior"""
        scanner = PortScanner()

        mock_port = MagicMock()
        mock_port.device = "/dev/ttyUSB0"
        mock_port.description = ""
        mock_port.hwid = ""
        mock_port.manufacturer = None
        mock_port.product = None
        mock_port.serial_number = None
        mock_port.vid = None
        mock_port.pid = None

        with patch(
            "serial.tools.list_ports.comports",
            return_value=[mock_port],
        ) as mock_comports:
            # First call
            ports1 = scanner.get_available_ports()
            assert len(ports1) == 1
            assert mock_comports.call_count == 1

            # Second call should use the cache
            ports2 = scanner.get_available_ports()
            assert len(ports2) == 1
            assert mock_comports.call_count == 1

            # refresh should rescan
            scanner.refresh()
            assert mock_comports.call_count == 2

    def test_get_platform_port_prefix(self) -> None:
        """Test platform prefix"""
        with patch("sys.platform", "linux"):
            assert PortScanner.get_platform_port_prefix() == "/dev/ttyUSB"

        with patch("sys.platform", "darwin"):
            assert PortScanner.get_platform_port_prefix() == "/dev/cu."

        with patch("sys.platform", "win32"):
            assert PortScanner.get_platform_port_prefix() == "COM"
