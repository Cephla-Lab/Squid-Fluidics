"""
Modbus RTU protocol module

Modbus RTU communication: frame building, CRC checking, and the client.
"""

from .client import ModbusClient
from .crc import append_crc, calculate_crc, calculate_crc_bytes, extract_crc, verify_crc
from .exceptions import (
    BroadcastError,
    CRCError,
    FrameError,
    FunctionCodeMismatchError,
    ModbusError,
    ModbusExceptionResponse,
    NoResponseError,
    ResponseError,
    SlaveIdMismatchError,
    TimeoutError,
)
from .frame import (
    FrameBuilder,
    FunctionCode,
    ModbusRequest,
    ModbusResponse,
    calculate_response_length,
    parse_write_response,
)

__all__ = [
    # Client
    "ModbusClient",
    # CRC
    "calculate_crc",
    "calculate_crc_bytes",
    "verify_crc",
    "append_crc",
    "extract_crc",
    # Frame
    "FunctionCode",
    "ModbusRequest",
    "ModbusResponse",
    "FrameBuilder",
    "calculate_response_length",
    "parse_write_response",
    # Exceptions
    "ModbusError",
    "ModbusExceptionResponse",
    "CRCError",
    "FrameError",
    "ResponseError",
    "SlaveIdMismatchError",
    "FunctionCodeMismatchError",
    "TimeoutError",
    "NoResponseError",
    "BroadcastError",
]
