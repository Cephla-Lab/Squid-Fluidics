"""
Serial port scanning module

Provides discovery of serial devices and retrieval of their information.
"""

import sys
from dataclasses import dataclass
from typing import List, Optional

import serial.tools.list_ports
from serial.tools.list_ports_common import ListPortInfo


@dataclass
class PortInfo:
    """Data class for serial port information"""

    device: str
    """Device path (e.g. /dev/ttyUSB0 or COM1)"""

    description: str
    """Device description"""

    hwid: str
    """Hardware ID"""

    manufacturer: Optional[str] = None
    """Manufacturer"""

    product: Optional[str] = None
    """Product name"""

    serial_number: Optional[str] = None
    """Serial number"""

    vid: Optional[int] = None
    """USB Vendor ID"""

    pid: Optional[int] = None
    """USB Product ID"""

    @classmethod
    def from_list_port_info(cls, info: ListPortInfo) -> "PortInfo":
        """
        Create a PortInfo from a serial.tools.list_ports ListPortInfo

        Args:
            info: ListPortInfo object

        Returns:
            PortInfo instance
        """
        return cls(
            device=info.device,
            description=info.description or "",
            hwid=info.hwid or "",
            manufacturer=info.manufacturer,
            product=info.product,
            serial_number=info.serial_number,
            vid=info.vid,
            pid=info.pid,
        )

    @property
    def display_name(self) -> str:
        """Get the name used for display"""
        if self.description and self.description != "n/a":
            return f"{self.device} - {self.description}"
        return self.device

    @property
    def is_usb(self) -> bool:
        """Whether this is a USB serial device"""
        return self.vid is not None and self.pid is not None

    def __str__(self) -> str:
        return self.display_name


class PortScanner:
    """
    Serial port scanner

    Used to discover available serial devices on the system.
    """

    # Common USB-RS485 adapter VID:PID pairs
    KNOWN_RS485_ADAPTERS = [
        (0x0403, 0x6001),  # FTDI FT232
        (0x0403, 0x6015),  # FTDI FT231X
        (0x067B, 0x2303),  # Prolific PL2303
        (0x10C4, 0xEA60),  # Silicon Labs CP210x
        (0x1A86, 0x7523),  # QinHeng CH340
        (0x1A86, 0x55D4),  # QinHeng CH9102
    ]

    def __init__(self) -> None:
        """Initialize the serial port scanner"""
        self._cached_ports: List[PortInfo] = []

    def scan_ports(self, include_links: bool = False) -> List[PortInfo]:
        """
        Scan for available serial ports on the system

        Args:
            include_links: Whether to include symbolic links (Linux only)

        Returns:
            List of PortInfo
        """
        ports: List[PortInfo] = []

        for port_info in serial.tools.list_ports.comports(include_links=include_links):
            ports.append(PortInfo.from_list_port_info(port_info))

        # Sort by device name
        ports.sort(key=lambda p: p.device)

        # Update cache
        self._cached_ports = ports

        return ports

    def get_available_ports(self) -> List[PortInfo]:
        """
        Get the list of available serial ports (using cache)

        Returns:
            List of PortInfo
        """
        if not self._cached_ports:
            return self.scan_ports()
        return self._cached_ports

    def refresh(self) -> List[PortInfo]:
        """
        Refresh the list of serial ports

        Returns:
            Updated list of PortInfo
        """
        return self.scan_ports()

    def is_port_available(self, port_name: str) -> bool:
        """
        Check whether the specified serial port is available

        Args:
            port_name: Serial port name

        Returns:
            Whether it is available
        """
        ports = self.scan_ports()
        return any(p.device == port_name for p in ports)

    def find_port(self, port_name: str) -> Optional[PortInfo]:
        """
        Find the specified serial port

        Args:
            port_name: Serial port name

        Returns:
            PortInfo or None
        """
        ports = self.scan_ports()
        for port in ports:
            if port.device == port_name:
                return port
        return None

    def find_rs485_adapters(self) -> List[PortInfo]:
        """
        Find possible RS-485 adapters

        Returns:
            List of PortInfo that may be RS-485 adapters
        """
        ports = self.scan_ports()
        adapters: List[PortInfo] = []

        for port in ports:
            if port.is_usb:
                for vid, pid in self.KNOWN_RS485_ADAPTERS:
                    if port.vid == vid and port.pid == pid:
                        adapters.append(port)
                        break

        return adapters

    def find_by_vid_pid(self, vid: int, pid: int) -> List[PortInfo]:
        """
        Find serial ports by VID:PID

        Args:
            vid: Vendor ID
            pid: Product ID

        Returns:
            List of matching PortInfo
        """
        ports = self.scan_ports()
        return [p for p in ports if p.vid == vid and p.pid == pid]

    def find_by_serial_number(self, serial_number: str) -> Optional[PortInfo]:
        """
        Find a serial port by serial number

        Args:
            serial_number: Device serial number

        Returns:
            Matching PortInfo or None
        """
        ports = self.scan_ports()
        for port in ports:
            if port.serial_number == serial_number:
                return port
        return None

    @staticmethod
    def get_platform_port_prefix() -> str:
        """
        Get the serial port prefix for the current platform

        Returns:
            Serial port prefix (e.g. '/dev/ttyUSB' or 'COM')
        """
        if sys.platform.startswith("linux"):
            return "/dev/ttyUSB"
        elif sys.platform == "darwin":
            return "/dev/cu."
        elif sys.platform == "win32":
            return "COM"
        return ""
