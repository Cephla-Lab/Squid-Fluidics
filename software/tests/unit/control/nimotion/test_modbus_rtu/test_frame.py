"""
Modbus frame module tests
"""

import pytest

from fluidics.control.nimotion.modbus_rtu.crc import append_crc
from fluidics.control.nimotion.modbus_rtu.exceptions import CRCError, FrameError, ModbusExceptionResponse
from fluidics.control.nimotion.modbus_rtu.frame import (
    FrameBuilder,
    FunctionCode,
    ModbusRequest,
    ModbusResponse,
    calculate_response_length,
)


class TestFunctionCode:
    """Test function code enum"""

    def test_read_functions(self) -> None:
        """Test read function codes"""
        assert FunctionCode.READ_HOLDING_REGISTERS.is_read is True
        assert FunctionCode.READ_INPUT_REGISTERS.is_read is True
        assert FunctionCode.WRITE_SINGLE_REGISTER.is_read is False

    def test_write_functions(self) -> None:
        """Test write function codes"""
        assert FunctionCode.WRITE_SINGLE_REGISTER.is_write is True
        assert FunctionCode.WRITE_MULTIPLE_REGISTERS.is_write is True
        assert FunctionCode.READ_HOLDING_REGISTERS.is_write is False


class TestModbusRequest:
    """Test ModbusRequest"""

    def test_to_bytes(self) -> None:
        """Test serialization"""
        request = ModbusRequest(
            slave_id=1,
            function_code=FunctionCode.READ_HOLDING_REGISTERS,
            data=bytes([0x03, 0x80, 0x00, 0x01]),
        )
        frame = request.to_bytes()

        assert frame[0] == 1  # slave_id
        assert frame[1] == 0x03  # function code
        assert frame[2:6] == bytes([0x03, 0x80, 0x00, 0x01])  # data
        assert len(frame) == 8  # data + CRC

    def test_is_broadcast(self) -> None:
        """Test broadcast detection"""
        broadcast = ModbusRequest(slave_id=0, function_code=0x03, data=b"")
        normal = ModbusRequest(slave_id=1, function_code=0x03, data=b"")

        assert broadcast.is_broadcast is True
        assert normal.is_broadcast is False


class TestModbusResponse:
    """Test ModbusResponse"""

    def test_from_bytes_read_response(self) -> None:
        """Test parsing a read response"""
        # Build a valid response: slave=1, fc=0x03, byte_count=2, data=[0x00, 0x27]
        payload = bytes([0x01, 0x03, 0x02, 0x00, 0x27])
        raw_data = append_crc(payload)

        response = ModbusResponse.from_bytes(raw_data)

        assert response.slave_id == 1
        assert response.function_code == 0x03
        assert response.is_exception is False
        assert response.data == bytes([0x02, 0x00, 0x27])

    def test_from_bytes_exception_response(self) -> None:
        """Test parsing an exception response"""
        # Exception response: slave=1, fc=0x83 (0x03 + 0x80), exception_code=0x02
        payload = bytes([0x01, 0x83, 0x02])
        raw_data = append_crc(payload)

        response = ModbusResponse.from_bytes(raw_data)

        assert response.slave_id == 1
        assert response.function_code == 0x83
        assert response.is_exception is True
        assert response.exception_code == 0x02

    def test_from_bytes_invalid_crc(self) -> None:
        """Test invalid CRC"""
        raw_data = bytes([0x01, 0x03, 0x02, 0x00, 0x27, 0xFF, 0xFF])

        with pytest.raises(CRCError):
            ModbusResponse.from_bytes(raw_data)

    def test_from_bytes_too_short(self) -> None:
        """Test frame that is too short"""
        raw_data = bytes([0x01, 0x03, 0x02])

        with pytest.raises(FrameError):
            ModbusResponse.from_bytes(raw_data)

    def test_get_registers(self) -> None:
        """Test getting register values"""
        # Response: 2 bytes of data, value=0x0027
        payload = bytes([0x01, 0x03, 0x02, 0x00, 0x27])
        raw_data = append_crc(payload)

        response = ModbusResponse.from_bytes(raw_data)
        registers = response.get_registers()

        assert len(registers) == 1
        assert registers[0] == 0x0027

    def test_get_registers_multiple(self) -> None:
        """Test getting multiple registers"""
        # Response: 4 bytes of data, values=[0x1234, 0x5678]
        payload = bytes([0x01, 0x03, 0x04, 0x12, 0x34, 0x56, 0x78])
        raw_data = append_crc(payload)

        response = ModbusResponse.from_bytes(raw_data)
        registers = response.get_registers()

        assert len(registers) == 2
        assert registers[0] == 0x1234
        assert registers[1] == 0x5678

    def test_raise_for_exception(self) -> None:
        """Test raising an exception"""
        payload = bytes([0x01, 0x83, 0x02])
        raw_data = append_crc(payload)

        response = ModbusResponse.from_bytes(raw_data)

        with pytest.raises(ModbusExceptionResponse) as exc_info:
            response.raise_for_exception()

        assert exc_info.value.exception_code == 0x02


class TestFrameBuilder:
    """Test frame builder"""

    def test_build_read_holding_registers(self) -> None:
        """Test building a read holding registers request"""
        request = FrameBuilder.build_read_holding_registers(
            slave_id=1,
            start_address=0x0380,
            quantity=1,
        )

        assert request.slave_id == 1
        assert request.function_code == FunctionCode.READ_HOLDING_REGISTERS
        assert request.data == bytes([0x03, 0x80, 0x00, 0x01])

    def test_build_read_holding_registers_invalid_quantity(self) -> None:
        """Test invalid quantity"""
        with pytest.raises(ValueError):
            FrameBuilder.build_read_holding_registers(1, 0x0000, 0)

        with pytest.raises(ValueError):
            FrameBuilder.build_read_holding_registers(1, 0x0000, 126)

    def test_build_write_single_register(self) -> None:
        """Test building a write single register request"""
        request = FrameBuilder.build_write_single_register(
            slave_id=1,
            address=0x0380,
            value=0x000F,
        )

        assert request.slave_id == 1
        assert request.function_code == FunctionCode.WRITE_SINGLE_REGISTER
        assert request.data == bytes([0x03, 0x80, 0x00, 0x0F])

    def test_build_write_multiple_registers(self) -> None:
        """Test building a write multiple registers request"""
        request = FrameBuilder.build_write_multiple_registers(
            slave_id=1,
            start_address=0x03BD,
            values=[0x0000, 0x1000],
        )

        assert request.slave_id == 1
        assert request.function_code == FunctionCode.WRITE_MULTIPLE_REGISTERS
        # address(2) + quantity(2) + byte_count(1) + data(4)
        assert len(request.data) == 9


class TestCalculateResponseLength:
    """Test response length calculation"""

    def test_read_holding_registers(self) -> None:
        """Test read holding registers response length"""
        length = calculate_response_length(FunctionCode.READ_HOLDING_REGISTERS, 2)
        # slave(1) + fc(1) + byte_count(1) + data(4) + CRC(2) = 9
        assert length == 9

    def test_write_single_register(self) -> None:
        """Test write single register response length"""
        length = calculate_response_length(FunctionCode.WRITE_SINGLE_REGISTER)
        # slave(1) + fc(1) + address(2) + value(2) + CRC(2) = 8
        assert length == 8
