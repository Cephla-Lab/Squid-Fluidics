"""
Core serial communication module

Provides thread-safe serial port read/write operations.
"""

import logging
import threading
import time
from typing import Optional, cast

import serial
from serial import SerialException

from .exceptions import (
    PortAccessError,
    PortConfigError,
    PortNotFoundError,
    PortNotOpenError,
    PortOpenError,
    ReadTimeoutError,
    WriteError,
)

logger = logging.getLogger(__name__)


class SerialPort:
    """
    Serial communication class

    Provides thread-safe serial port operations, including open/close,
    read/write, and configuration.
    """

    # Default configuration
    DEFAULT_BAUDRATE = 115200
    DEFAULT_BYTESIZE = serial.EIGHTBITS
    DEFAULT_PARITY = serial.PARITY_NONE
    DEFAULT_STOPBITS = serial.STOPBITS_ONE
    DEFAULT_TIMEOUT = 0.1  # 100ms
    DEFAULT_WRITE_TIMEOUT = 0.1

    def __init__(
        self,
        port: Optional[str] = None,
        baudrate: int = DEFAULT_BAUDRATE,
        bytesize: int = DEFAULT_BYTESIZE,
        parity: str = DEFAULT_PARITY,
        stopbits: float = DEFAULT_STOPBITS,
        timeout: float = DEFAULT_TIMEOUT,
        write_timeout: float = DEFAULT_WRITE_TIMEOUT,
    ) -> None:
        """
        Initialize the serial port

        Args:
            port: Serial port name (e.g. /dev/ttyUSB0 or COM1)
            baudrate: Baud rate
            bytesize: Data bits
            parity: Parity
            stopbits: Stop bits
            timeout: Read timeout (seconds)
            write_timeout: Write timeout (seconds)
        """
        self._port_name = port
        self._baudrate = baudrate
        self._bytesize = bytesize
        self._parity = parity
        self._stopbits = stopbits
        self._timeout = timeout
        self._write_timeout = write_timeout

        self._serial: Optional[serial.Serial] = None
        self._lock = threading.RLock()
        self._is_open = False

    @property
    def port_name(self) -> Optional[str]:
        """Get the serial port name"""
        return self._port_name

    @property
    def baudrate(self) -> int:
        """Get the baud rate"""
        return self._baudrate

    @property
    def is_open(self) -> bool:
        """Check whether the serial port is open"""
        with self._lock:
            return self._is_open and self._serial is not None and self._serial.is_open

    @property
    def timeout(self) -> float:
        """Get the read timeout"""
        return self._timeout

    @timeout.setter
    def timeout(self, value: float) -> None:
        """Set the read timeout"""
        self._timeout = value
        with self._lock:
            if self._serial is not None and self._serial.is_open:
                self._serial.timeout = value

    def open(self, port: Optional[str] = None) -> None:
        """
        Open the serial port

        Args:
            port: Serial port name (if not specified, uses the setting from the constructor)

        Raises:
            PortNotFoundError: Serial port does not exist
            PortAccessError: No permission to access
            PortOpenError: Failed to open
        """
        if port is not None:
            self._port_name = port

        if not self._port_name:
            raise PortOpenError("", "No serial port name specified")

        with self._lock:
            if self._is_open:
                logger.warning(f"Serial port {self._port_name} is already open")
                return

            # Imported here: the simulator imports this package
            from ..simulation import SimulatedSerial, is_simulator_port

            if is_simulator_port(self._port_name):
                simulated = SimulatedSerial(
                    self._port_name, timeout=self._timeout, write_timeout=self._write_timeout
                )
                self._serial = cast(serial.Serial, simulated)  # duck-typed stand-in
                self._is_open = True
                logger.info(f"Simulated serial port {self._port_name} opened")
                return

            try:
                self._serial = serial.Serial(
                    port=self._port_name,
                    baudrate=self._baudrate,
                    bytesize=self._bytesize,
                    parity=self._parity,
                    stopbits=self._stopbits,
                    timeout=self._timeout,
                    write_timeout=self._write_timeout,
                )
                self._is_open = True
                logger.info(
                    f"Serial port {self._port_name} opened "
                    f"(baud rate: {self._baudrate}, timeout: {self._timeout}s)"
                )

            except SerialException as e:
                error_msg = str(e).lower()
                if "no such file" in error_msg or "filenotfounderror" in error_msg:
                    raise PortNotFoundError(self._port_name) from e
                elif "permission" in error_msg or "access" in error_msg:
                    raise PortAccessError(self._port_name) from e
                else:
                    raise PortOpenError(self._port_name, str(e)) from e
            except PermissionError as e:
                raise PortAccessError(self._port_name) from e
            except FileNotFoundError as e:
                raise PortNotFoundError(self._port_name) from e
            except Exception as e:
                raise PortOpenError(self._port_name, str(e)) from e

    def close(self) -> None:
        """Close the serial port"""
        with self._lock:
            if self._serial is not None:
                try:
                    if self._serial.is_open:
                        self._serial.close()
                        logger.info(f"Serial port {self._port_name} closed")
                except Exception as e:
                    logger.error(f"Error closing serial port: {e}")
                finally:
                    self._serial = None
                    self._is_open = False

    def write(self, data: bytes) -> int:
        """
        Write data

        Args:
            data: Data to write

        Returns:
            Number of bytes actually written

        Raises:
            PortNotOpenError: Serial port not open
            WriteError: Write failed
        """
        with self._lock:
            if not self.is_open:
                raise PortNotOpenError(self._port_name)

            try:
                bytes_written = self._serial.write(data)  # type: ignore
                self._serial.flush()  # type: ignore
                logger.debug(f"Wrote {bytes_written} bytes: {data.hex()}")
                return bytes_written or 0

            except SerialException as e:
                raise WriteError(str(e), len(data)) from e
            except Exception as e:
                raise WriteError(str(e), len(data)) from e

    def read(self, size: int = 1) -> bytes:
        """
        Read the specified number of bytes

        Args:
            size: Number of bytes to read

        Returns:
            Data read

        Raises:
            PortNotOpenError: Serial port not open
        """
        with self._lock:
            if not self.is_open:
                raise PortNotOpenError(self._port_name)

            try:
                data = self._serial.read(size)  # type: ignore
                if data:
                    logger.debug(f"Read {len(data)} bytes: {data.hex()}")
                return data

            except Exception as e:
                logger.error(f"Error reading data: {e}")
                return b""

    def read_all(self) -> bytes:
        """
        Read all available data

        Returns:
            Data read

        Raises:
            PortNotOpenError: Serial port not open
        """
        with self._lock:
            if not self.is_open:
                raise PortNotOpenError(self._port_name)

            try:
                # Wait a short moment for data to arrive
                time.sleep(0.01)
                in_waiting = self._serial.in_waiting  # type: ignore
                if in_waiting > 0:
                    data = self._serial.read(in_waiting)  # type: ignore
                    logger.debug(f"Read all {len(data)} bytes: {data.hex()}")
                    return data
                return b""

            except Exception as e:
                logger.error(f"Error reading data: {e}")
                return b""

    def read_until(
        self,
        expected_length: int,
        timeout: Optional[float] = None,
    ) -> bytes:
        """
        Read data until the specified length is reached or timeout occurs

        Args:
            expected_length: Number of bytes expected to read
            timeout: Timeout duration (seconds); None uses the default timeout

        Returns:
            Data read

        Raises:
            PortNotOpenError: Serial port not open
            ReadTimeoutError: Read timeout
        """
        with self._lock:
            if not self.is_open:
                raise PortNotOpenError(self._port_name)

            if timeout is None:
                timeout = self._timeout

            data = b""
            start_time = time.time()

            while len(data) < expected_length:
                elapsed = time.time() - start_time
                if elapsed >= timeout:
                    raise ReadTimeoutError(
                        timeout=timeout,
                        expected_bytes=expected_length,
                        received_bytes=len(data),
                    )

                remaining = expected_length - len(data)
                # Set the remaining timeout
                self._serial.timeout = max(0.001, timeout - elapsed)  # type: ignore

                try:
                    chunk = self._serial.read(remaining)  # type: ignore
                    if chunk:
                        data += chunk
                except Exception as e:
                    logger.error(f"Error reading data: {e}")
                    break

            # Restore the original timeout
            self._serial.timeout = self._timeout  # type: ignore

            if data:
                logger.debug(f"Read {len(data)} bytes: {data.hex()}")

            return data

    def flush_input(self) -> None:
        """Clear the input buffer"""
        with self._lock:
            if self._serial is not None and self._serial.is_open:
                self._serial.reset_input_buffer()
                logger.debug("Input buffer cleared")

    def flush_output(self) -> None:
        """Clear the output buffer"""
        with self._lock:
            if self._serial is not None and self._serial.is_open:
                self._serial.reset_output_buffer()
                logger.debug("Output buffer cleared")

    def flush(self) -> None:
        """Clear all buffers"""
        self.flush_input()
        self.flush_output()

    @property
    def in_waiting(self) -> int:
        """Get the number of bytes waiting in the input buffer"""
        with self._lock:
            if self._serial is not None and self._serial.is_open:
                return self._serial.in_waiting
            return 0

    @property
    def out_waiting(self) -> int:
        """Get the number of bytes waiting in the output buffer"""
        with self._lock:
            if self._serial is not None and self._serial.is_open:
                return self._serial.out_waiting
            return 0

    def set_baudrate(self, baudrate: int) -> None:
        """
        Set the baud rate

        Args:
            baudrate: Baud rate

        Raises:
            PortConfigError: Configuration failed
        """
        valid_baudrates = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]
        if baudrate not in valid_baudrates:
            raise PortConfigError(
                f"Unsupported baud rate: {baudrate}",
                parameter="baudrate",
                value=str(baudrate),
            )

        self._baudrate = baudrate
        with self._lock:
            if self._serial is not None and self._serial.is_open:
                self._serial.baudrate = baudrate
                logger.info(f"Baud rate set to {baudrate}")

    def configure(
        self,
        baudrate: Optional[int] = None,
        bytesize: Optional[int] = None,
        parity: Optional[str] = None,
        stopbits: Optional[float] = None,
        timeout: Optional[float] = None,
    ) -> None:
        """
        Configure serial port parameters

        Args:
            baudrate: Baud rate
            bytesize: Data bits
            parity: Parity
            stopbits: Stop bits
            timeout: Timeout duration

        Raises:
            PortConfigError: Configuration failed
        """
        with self._lock:
            if baudrate is not None:
                self._baudrate = baudrate
                if self._serial:
                    self._serial.baudrate = baudrate

            if bytesize is not None:
                self._bytesize = bytesize
                if self._serial:
                    self._serial.bytesize = bytesize

            if parity is not None:
                self._parity = parity
                if self._serial:
                    self._serial.parity = parity

            if stopbits is not None:
                self._stopbits = stopbits
                if self._serial:
                    self._serial.stopbits = stopbits

            if timeout is not None:
                self._timeout = timeout
                if self._serial:
                    self._serial.timeout = timeout

            logger.info(
                f"Serial port configuration updated: baudrate={self._baudrate}, "
                f"bytesize={self._bytesize}, parity={self._parity}, "
                f"stopbits={self._stopbits}, timeout={self._timeout}"
            )

    def send_break(self, duration: float = 0.25) -> None:
        """
        Send a Break signal

        Args:
            duration: Duration (seconds)
        """
        with self._lock:
            if self._serial is not None and self._serial.is_open:
                self._serial.send_break(duration)
                logger.debug(f"Break signal sent ({duration}s)")

    def __enter__(self) -> "SerialPort":
        """Context manager entry"""
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # type: ignore
        """Context manager exit"""
        self.close()

    def __repr__(self) -> str:
        status = "open" if self.is_open else "closed"
        return f"SerialPort({self._port_name}, {self._baudrate}, {status})"
