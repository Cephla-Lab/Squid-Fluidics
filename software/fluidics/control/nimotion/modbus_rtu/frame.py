"""
Modbus RTU frame building and parsing module

Provides functionality for building, parsing, and validating Modbus RTU frames.
"""

import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import List, Tuple

from .crc import append_crc, calculate_crc, verify_crc
from .exceptions import CRCError, FrameError, ModbusExceptionResponse


class FunctionCode(IntEnum):
    """Modbus function code"""

    READ_COILS = 0x01
    READ_DISCRETE_INPUTS = 0x02
    READ_HOLDING_REGISTERS = 0x03
    READ_INPUT_REGISTERS = 0x04
    WRITE_SINGLE_COIL = 0x05
    WRITE_SINGLE_REGISTER = 0x06
    WRITE_MULTIPLE_COILS = 0x0F
    WRITE_MULTIPLE_REGISTERS = 0x10

    @property
    def is_read(self) -> bool:
        """Whether this is a read function code"""
        return self in (
            FunctionCode.READ_COILS,
            FunctionCode.READ_DISCRETE_INPUTS,
            FunctionCode.READ_HOLDING_REGISTERS,
            FunctionCode.READ_INPUT_REGISTERS,
        )

    @property
    def is_write(self) -> bool:
        """Whether this is a write function code"""
        return self in (
            FunctionCode.WRITE_SINGLE_COIL,
            FunctionCode.WRITE_SINGLE_REGISTER,
            FunctionCode.WRITE_MULTIPLE_COILS,
            FunctionCode.WRITE_MULTIPLE_REGISTERS,
        )


@dataclass
class ModbusRequest:
    """Modbus request frame"""

    slave_id: int
    """Slave address (1-247, 0 for broadcast)"""

    function_code: int
    """Function code"""

    data: bytes
    """Data portion"""

    def to_bytes(self) -> bytes:
        """
        Build the complete request frame (including CRC)

        Returns:
            Complete frame bytes
        """
        frame = bytes([self.slave_id, self.function_code]) + self.data
        return append_crc(frame)

    @property
    def is_broadcast(self) -> bool:
        """Whether this is a broadcast request"""
        return self.slave_id == 0

    def __repr__(self) -> str:
        return (
            f"ModbusRequest(slave_id={self.slave_id}, "
            f"fc=0x{self.function_code:02X}, data={self.data.hex()})"
        )


@dataclass
class ModbusResponse:
    """Modbus response frame"""

    slave_id: int
    """Slave address"""

    function_code: int
    """Function code"""

    data: bytes
    """Data portion"""

    is_exception: bool = False
    """Whether this is an exception response"""

    exception_code: int = 0
    """Exception code (valid only when is_exception=True)"""

    @classmethod
    def from_bytes(cls, raw_data: bytes, validate_crc: bool = True) -> "ModbusResponse":
        """
        Parse a response frame from byte data

        Args:
            raw_data: Raw response data (including CRC)
            validate_crc: Whether to validate the CRC

        Returns:
            ModbusResponse instance

        Raises:
            FrameError: Frame format error
            CRCError: CRC check failed
        """
        if len(raw_data) < 5:  # Minimum frame: slave_id + fc + 1 byte data + 2 bytes CRC
            raise FrameError(
                f"Response frame too short: need at least 5 bytes, got {len(raw_data)}",
                raw_data=raw_data,
            )

        if validate_crc:
            if not verify_crc(raw_data):
                # Calculate the expected CRC
                expected_crc = calculate_crc(raw_data[:-2])
                received_crc = raw_data[-2] | (raw_data[-1] << 8)
                raise CRCError(expected_crc, received_crc)

        slave_id = raw_data[0]
        function_code = raw_data[1]
        data = raw_data[2:-2]  # Strip the slave address, function code, and CRC

        # Check whether this is an exception response
        is_exception = (function_code & 0x80) != 0
        exception_code = 0

        if is_exception:
            if len(data) >= 1:
                exception_code = data[0]

        return cls(
            slave_id=slave_id,
            function_code=function_code,
            data=data,
            is_exception=is_exception,
            exception_code=exception_code,
        )

    def raise_for_exception(self) -> None:
        """
        Raise an exception if this is an exception response

        Raises:
            ModbusExceptionResponse: When the response is an exception response
        """
        if self.is_exception:
            raise ModbusExceptionResponse(
                function_code=self.function_code,
                exception_code=self.exception_code,
                slave_id=self.slave_id,
            )

    def get_registers(self) -> List[int]:
        """
        Get the register values from the response

        Used for read-register responses (FC=0x03, 0x04).

        Returns:
            List of register values (16-bit unsigned integers)

        Raises:
            FrameError: Data format error
        """
        if len(self.data) < 1:
            raise FrameError("Response data is empty", raw_data=self.data)

        byte_count = self.data[0]
        register_data = self.data[1:]

        if len(register_data) != byte_count:
            raise FrameError(
                f"Data length mismatch: declared {byte_count} bytes, " f"got {len(register_data)}",
                raw_data=self.data,
            )

        if byte_count % 2 != 0:
            raise FrameError(
                f"Register data length must be even: {byte_count}",
                raw_data=self.data,
            )

        registers = []
        for i in range(0, byte_count, 2):
            value = (register_data[i] << 8) | register_data[i + 1]
            registers.append(value)

        return registers

    def __repr__(self) -> str:
        if self.is_exception:
            return (
                f"ModbusResponse(slave_id={self.slave_id}, "
                f"fc=0x{self.function_code:02X}, exception_code=0x{self.exception_code:02X})"
            )
        return (
            f"ModbusResponse(slave_id={self.slave_id}, "
            f"fc=0x{self.function_code:02X}, data={self.data.hex()})"
        )


class FrameBuilder:
    """Modbus frame builder"""

    @staticmethod
    def build_read_holding_registers(
        slave_id: int,
        start_address: int,
        quantity: int,
    ) -> ModbusRequest:
        """
        Build a read-holding-registers request (FC=0x03)

        Args:
            slave_id: Slave address
            start_address: Start address
            quantity: Number of registers to read (1-125)

        Returns:
            ModbusRequest
        """
        if not 1 <= quantity <= 125:
            raise ValueError(f"Read quantity must be between 1 and 125: {quantity}")

        data = struct.pack(">HH", start_address, quantity)
        return ModbusRequest(
            slave_id=slave_id,
            function_code=FunctionCode.READ_HOLDING_REGISTERS,
            data=data,
        )

    @staticmethod
    def build_read_input_registers(
        slave_id: int,
        start_address: int,
        quantity: int,
    ) -> ModbusRequest:
        """
        Build a read-input-registers request (FC=0x04)

        Args:
            slave_id: Slave address
            start_address: Start address
            quantity: Number of registers to read (1-125)

        Returns:
            ModbusRequest
        """
        if not 1 <= quantity <= 125:
            raise ValueError(f"Read quantity must be between 1 and 125: {quantity}")

        data = struct.pack(">HH", start_address, quantity)
        return ModbusRequest(
            slave_id=slave_id,
            function_code=FunctionCode.READ_INPUT_REGISTERS,
            data=data,
        )

    @staticmethod
    def build_write_single_register(
        slave_id: int,
        address: int,
        value: int,
    ) -> ModbusRequest:
        """
        Build a write-single-register request (FC=0x06)

        Args:
            slave_id: Slave address
            address: Register address
            value: Register value (0-65535)

        Returns:
            ModbusRequest
        """
        if not 0 <= value <= 0xFFFF:
            raise ValueError(f"Register value must be between 0 and 65535: {value}")

        data = struct.pack(">HH", address, value)
        return ModbusRequest(
            slave_id=slave_id,
            function_code=FunctionCode.WRITE_SINGLE_REGISTER,
            data=data,
        )

    @staticmethod
    def build_write_multiple_registers(
        slave_id: int,
        start_address: int,
        values: List[int],
    ) -> ModbusRequest:
        """
        Build a write-multiple-registers request (FC=0x10)

        Args:
            slave_id: Slave address
            start_address: Start address
            values: List of register values (each value 0-65535)

        Returns:
            ModbusRequest
        """
        quantity = len(values)
        if not 1 <= quantity <= 123:
            raise ValueError(f"Write quantity must be between 1 and 123: {quantity}")

        byte_count = quantity * 2

        # Build data: start address(2) + quantity(2) + byte count(1) + values(2*N)
        data = struct.pack(">HHB", start_address, quantity, byte_count)
        for value in values:
            if not 0 <= value <= 0xFFFF:
                raise ValueError(f"Register value must be between 0 and 65535: {value}")
            data += struct.pack(">H", value)

        return ModbusRequest(
            slave_id=slave_id,
            function_code=FunctionCode.WRITE_MULTIPLE_REGISTERS,
            data=data,
        )


def calculate_response_length(function_code: int, quantity: int = 0) -> int:
    """
    Calculate the expected response length

    Args:
        function_code: Function code
        quantity: Requested quantity (used for read requests)

    Returns:
        Expected number of response bytes (including CRC)
    """
    # Base length: slave_id(1) + fc(1) + CRC(2) = 4
    base_length = 4

    if function_code == FunctionCode.READ_HOLDING_REGISTERS:
        # Response: slave_id(1) + fc(1) + byte_count(1) + data(2*quantity) + CRC(2)
        return base_length + 1 + quantity * 2

    elif function_code == FunctionCode.READ_INPUT_REGISTERS:
        return base_length + 1 + quantity * 2

    elif function_code == FunctionCode.WRITE_SINGLE_REGISTER:
        # Response: slave_id(1) + fc(1) + address(2) + value(2) + CRC(2)
        return base_length + 4

    elif function_code == FunctionCode.WRITE_MULTIPLE_REGISTERS:
        # Response: slave_id(1) + fc(1) + address(2) + quantity(2) + CRC(2)
        return base_length + 4

    else:
        # Default minimum response length
        return 5


def parse_write_response(response: ModbusResponse) -> Tuple[int, int]:
    """
    Parse a write response

    Args:
        response: ModbusResponse instance

    Returns:
        (address, value or quantity)

    Raises:
        FrameError: Data format error
    """
    if len(response.data) < 4:
        raise FrameError(
            f"Write response data too short: need 4 bytes, got {len(response.data)}",
            raw_data=response.data,
        )

    address = (response.data[0] << 8) | response.data[1]
    value = (response.data[2] << 8) | response.data[3]

    return address, value
