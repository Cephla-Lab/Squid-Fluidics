"""
Modbus RTU client module

Provides Modbus RTU master communication functionality.
"""

import logging
import threading
import time
from typing import List, Optional

from ..serial_comm import ReadTimeoutError, SerialPort
from .exceptions import (
    CRCError,
    FrameError,
    FunctionCodeMismatchError,
    ModbusError,
    NoResponseError,
    SlaveIdMismatchError,
    TimeoutError,
)
from .frame import (
    FrameBuilder,
    FunctionCode,
    ModbusRequest,
    ModbusResponse,
    calculate_response_length,
)

logger = logging.getLogger(__name__)


class ModbusClient:
    """
    Modbus RTU client

    Provides thread-safe Modbus RTU communication functionality.
    """

    # Default configuration
    DEFAULT_TIMEOUT = 0.5  # 500ms
    DEFAULT_RETRIES = 3
    DEFAULT_FRAME_INTERVAL = 0.003  # 3ms (about 3.5 character times @115200)

    def __init__(
        self,
        serial_port: Optional[SerialPort] = None,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        frame_interval: float = DEFAULT_FRAME_INTERVAL,
    ) -> None:
        """
        Initialize the Modbus client

        Args:
            serial_port: Serial port instance (can be set later via connect)
            timeout: Response timeout (seconds)
            retries: Number of retries
            frame_interval: Frame interval (seconds)
        """
        self._serial = serial_port
        self._timeout = timeout
        self._retries = retries
        self._frame_interval = frame_interval
        # Held for a whole request/response exchange. SerialPort only locks
        # individual reads and writes, so without this two threads can flush
        # or consume each other's responses.
        self._lock = threading.RLock()

    @property
    def is_connected(self) -> bool:
        """Whether connected"""
        return self._serial is not None and self._serial.is_open

    @property
    def timeout(self) -> float:
        """Get the timeout setting"""
        return self._timeout

    @timeout.setter
    def timeout(self, value: float) -> None:
        """Set the timeout"""
        self._timeout = value

    @property
    def retries(self) -> int:
        """Get the number of retries"""
        return self._retries

    @retries.setter
    def retries(self, value: int) -> None:
        """Set the number of retries"""
        self._retries = max(0, value)

    def connect(
        self,
        port: Optional[str] = None,
        baudrate: int = 115200,
        serial_port: Optional[SerialPort] = None,
    ) -> None:
        """
        Connect to the slave

        Args:
            port: Serial port name
            baudrate: Baud rate
            serial_port: Existing serial port instance (if provided, port and baudrate are ignored)
        """
        if serial_port is not None:
            self._serial = serial_port
        else:
            self._serial = SerialPort(
                port=port,
                baudrate=baudrate,
                timeout=self._timeout,
            )

        if not self._serial.is_open:
            self._serial.open()

        logger.info(f"Modbus client connected: {self._serial.port_name}")

    def disconnect(self) -> None:
        """Disconnect"""
        if self._serial is not None:
            self._serial.close()
            logger.info("Modbus client disconnected")

    def send_request(
        self,
        request: ModbusRequest,
        expected_length: Optional[int] = None,
    ) -> ModbusResponse:
        """
        Send a request and receive the response

        Args:
            request: Modbus request
            expected_length: Expected response length (None to compute automatically)

        Returns:
            ModbusResponse

        Raises:
            ModbusError: Communication error
        """
        with self._lock:
            return self._send_request_locked(request, expected_length)

    def _send_request_locked(
        self,
        request: ModbusRequest,
        expected_length: Optional[int],
    ) -> ModbusResponse:
        """Body of send_request(); the caller holds self._lock."""
        if not self.is_connected:
            raise ModbusError("Not connected to slave")

        # Broadcast requests do not require a response
        if request.is_broadcast:
            self._send_frame(request.to_bytes())
            time.sleep(self._frame_interval)
            return ModbusResponse(
                slave_id=0,
                function_code=request.function_code,
                data=b"",
            )

        last_error: Optional[Exception] = None

        for attempt in range(self._retries + 1):
            try:
                # Clear the receive buffer
                self._serial.flush_input()  # type: ignore

                # Send the request
                frame = request.to_bytes()
                self._send_frame(frame)

                # Wait for the frame interval
                time.sleep(self._frame_interval)

                # Receive the response
                response = self._receive_response(
                    request.slave_id,
                    request.function_code,
                    expected_length,
                )

                return response

            except (ReadTimeoutError, CRCError, FrameError) as e:
                last_error = e
                logger.warning(
                    f"Modbus request failed (attempt {attempt + 1}/{self._retries + 1}): {e}"
                )
                if attempt < self._retries:
                    time.sleep(self._frame_interval * 2)
                continue

        # All retries failed
        if isinstance(last_error, ReadTimeoutError):
            raise NoResponseError(request.slave_id, self._retries)
        raise last_error or NoResponseError(request.slave_id, self._retries)

    def _send_frame(self, frame: bytes) -> None:
        """Send a frame"""
        if self._serial is None:
            raise ModbusError("Serial port not initialized")

        logger.debug(f"Send: {frame.hex()}")
        self._serial.write(frame)

    def _receive_response(
        self,
        expected_slave_id: int,
        expected_fc: int,
        expected_length: Optional[int] = None,
    ) -> ModbusResponse:
        """
        Receive a response

        Args:
            expected_slave_id: Expected slave address
            expected_fc: Expected function code
            expected_length: Expected response length

        Returns:
            ModbusResponse
        """
        if self._serial is None:
            raise ModbusError("Serial port not initialized")

        # First read the header (slave_id + fc)
        header = self._serial.read_until(2, timeout=self._timeout)
        if len(header) < 2:
            raise TimeoutError(self._timeout, expected_slave_id)

        slave_id = header[0]
        function_code = header[1]

        # Check the slave address
        if slave_id != expected_slave_id:
            raise SlaveIdMismatchError(expected_slave_id, slave_id)

        # Check whether this is an exception response
        is_exception = (function_code & 0x80) != 0

        if is_exception:
            # Exception response: read 1 byte exception code + 2 bytes CRC
            remaining = self._serial.read_until(3, timeout=self._timeout)
            raw_data = header + remaining
        else:
            # Normal response
            if expected_length is not None:
                # Read the remaining data
                remaining_length = expected_length - 2
                remaining = self._serial.read_until(remaining_length, timeout=self._timeout)
                raw_data = header + remaining
            else:
                # Decide the read strategy based on the function code
                raw_data = self._read_response_by_fc(header, function_code)

        logger.debug(f"Receive: {raw_data.hex()}")

        # Parse the response
        response = ModbusResponse.from_bytes(raw_data, validate_crc=True)

        # Check the function code (ignoring the exception flag)
        if not is_exception and (function_code & 0x7F) != (expected_fc & 0x7F):
            raise FunctionCodeMismatchError(expected_fc, function_code, slave_id)

        return response

    def _read_response_by_fc(self, header: bytes, function_code: int) -> bytes:
        """Read a response based on the function code"""
        if self._serial is None:
            raise ModbusError("Serial port not initialized")

        if function_code in (
            FunctionCode.READ_HOLDING_REGISTERS,
            FunctionCode.READ_INPUT_REGISTERS,
        ):
            # Read the byte count
            byte_count_data = self._serial.read_until(1, timeout=self._timeout)
            if len(byte_count_data) < 1:
                raise TimeoutError(self._timeout)

            byte_count = byte_count_data[0]
            # Read the data + CRC
            remaining = self._serial.read_until(byte_count + 2, timeout=self._timeout)
            return header + byte_count_data + remaining

        elif function_code in (
            FunctionCode.WRITE_SINGLE_REGISTER,
            FunctionCode.WRITE_MULTIPLE_REGISTERS,
        ):
            # Fixed length: address(2) + value/quantity(2) + CRC(2)
            remaining = self._serial.read_until(6, timeout=self._timeout)
            return header + remaining

        else:
            # For other function codes, try reading the default length
            remaining = self._serial.read_until(6, timeout=self._timeout)
            return header + remaining

    # ==================== High-level API ====================

    def read_holding_registers(
        self,
        slave_id: int,
        start_address: int,
        quantity: int,
    ) -> List[int]:
        """
        Read holding registers (FC=0x03)

        Args:
            slave_id: Slave address
            start_address: Start address
            quantity: Number of registers to read

        Returns:
            List of register values
        """
        request = FrameBuilder.build_read_holding_registers(slave_id, start_address, quantity)
        expected_length = calculate_response_length(FunctionCode.READ_HOLDING_REGISTERS, quantity)
        response = self.send_request(request, expected_length)
        response.raise_for_exception()
        return response.get_registers()

    def read_input_registers(
        self,
        slave_id: int,
        start_address: int,
        quantity: int,
    ) -> List[int]:
        """
        Read input registers (FC=0x04)

        Args:
            slave_id: Slave address
            start_address: Start address
            quantity: Number of registers to read

        Returns:
            List of register values
        """
        request = FrameBuilder.build_read_input_registers(slave_id, start_address, quantity)
        expected_length = calculate_response_length(FunctionCode.READ_INPUT_REGISTERS, quantity)
        response = self.send_request(request, expected_length)
        response.raise_for_exception()
        return response.get_registers()

    def write_single_register(
        self,
        slave_id: int,
        address: int,
        value: int,
    ) -> None:
        """
        Write a single register (FC=0x06)

        Args:
            slave_id: Slave address
            address: Register address
            value: Register value
        """
        request = FrameBuilder.build_write_single_register(slave_id, address, value)
        expected_length = calculate_response_length(FunctionCode.WRITE_SINGLE_REGISTER)
        response = self.send_request(request, expected_length)
        response.raise_for_exception()

    def write_multiple_registers(
        self,
        slave_id: int,
        start_address: int,
        values: List[int],
    ) -> None:
        """
        Write multiple registers (FC=0x10)

        Args:
            slave_id: Slave address
            start_address: Start address
            values: List of register values
        """
        request = FrameBuilder.build_write_multiple_registers(slave_id, start_address, values)
        expected_length = calculate_response_length(FunctionCode.WRITE_MULTIPLE_REGISTERS)
        response = self.send_request(request, expected_length)
        response.raise_for_exception()

    def read_register(self, slave_id: int, address: int) -> int:
        """
        Read a single holding register

        Args:
            slave_id: Slave address
            address: Register address

        Returns:
            Register value
        """
        values = self.read_holding_registers(slave_id, address, 1)
        return values[0]

    def write_register(self, slave_id: int, address: int, value: int) -> None:
        """
        Write a single register

        Args:
            slave_id: Slave address
            address: Register address
            value: Register value
        """
        self.write_single_register(slave_id, address, value)

    def read_register_32bit(self, slave_id: int, address: int, signed: bool = False) -> int:
        """
        Read a 32-bit register (2 consecutive registers)

        Args:
            slave_id: Slave address
            address: Start register address
            signed: Whether the value is a signed integer

        Returns:
            32-bit value
        """
        values = self.read_holding_registers(slave_id, address, 2)
        # NiMotion actually uses big-endian: low address = high word, high address = low word
        value = (values[0] << 16) | values[1]

        if signed and value >= 0x80000000:
            value -= 0x100000000

        return value

    def write_register_32bit(
        self,
        slave_id: int,
        address: int,
        value: int,
        signed: bool = False,
    ) -> None:
        """
        Write a 32-bit register (2 consecutive registers)

        Args:
            slave_id: Slave address
            address: Start register address
            value: 32-bit value
            signed: Whether the value is a signed integer
        """
        if signed and value < 0:
            value += 0x100000000

        high = (value >> 16) & 0xFFFF
        low = value & 0xFFFF

        # NiMotion actually uses big-endian: low address = high word, high address = low word
        self.write_multiple_registers(slave_id, address, [high, low])

    def __enter__(self) -> "ModbusClient":
        """Context manager entry"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # type: ignore
        """Context manager exit"""
        self.disconnect()
