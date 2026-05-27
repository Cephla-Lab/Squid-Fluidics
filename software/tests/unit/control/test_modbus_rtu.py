import struct

import pytest

from fluidics.control.modbus_rtu import (
    ModbusError,
    ModbusRTUClient,
    build_read_registers_frame,
    build_write_multiple_registers_frame,
    build_write_register_frame,
    calculate_crc,
)


class TestCalculateCRC:
    def test_known_value(self):
        # Standard Modbus CRC test vector: slave=1, FC=3, addr=0, count=1
        # CRC integer = 0x0A84, wire bytes = [0x84, 0x0A]
        data = bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x01])
        crc = calculate_crc(data)
        assert crc == 0x0A84
        # Verify wire byte order (low byte first)
        assert crc & 0xFF == 0x84
        assert (crc >> 8) & 0xFF == 0x0A

    def test_empty_data(self):
        assert calculate_crc(b"") == 0xFFFF

    def test_single_byte(self):
        crc = calculate_crc(b"\x00")
        assert isinstance(crc, int)
        assert 0 <= crc <= 0xFFFF

    def test_bytearray_input(self):
        data = bytearray([0x01, 0x03, 0x00, 0x00, 0x00, 0x01])
        assert calculate_crc(data) == 0x0A84


class TestBuildReadRegistersFrame:
    def test_structure(self):
        frame = build_read_registers_frame(slave_id=1, address=0x0000, count=1)
        assert frame[0] == 0x01  # slave_id
        assert frame[1] == 0x03  # FC
        assert frame[2:4] == struct.pack(">H", 0x0000)  # address
        assert frame[4:6] == struct.pack(">H", 1)  # count
        assert len(frame) == 8  # 1+1+2+2+2(CRC)

    def test_crc_appended(self):
        frame = build_read_registers_frame(slave_id=1, address=0x0000, count=1)
        payload = frame[:-2]
        crc = calculate_crc(payload)
        assert frame[-2] == crc & 0xFF
        assert frame[-1] == (crc >> 8) & 0xFF

    def test_address_encoding(self):
        frame = build_read_registers_frame(slave_id=2, address=0x1234, count=3)
        assert frame[2:4] == b"\x12\x34"
        assert frame[4:6] == b"\x00\x03"


class TestBuildWriteRegisterFrame:
    def test_structure(self):
        frame = build_write_register_frame(slave_id=1, address=0x0010, value=0x00FF)
        assert frame[0] == 0x01  # slave_id
        assert frame[1] == 0x06  # FC
        assert frame[2:4] == struct.pack(">H", 0x0010)
        assert frame[4:6] == struct.pack(">H", 0x00FF)
        assert len(frame) == 8

    def test_crc_appended(self):
        frame = build_write_register_frame(slave_id=1, address=0x0010, value=0x00FF)
        payload = frame[:-2]
        crc = calculate_crc(payload)
        assert frame[-2] == crc & 0xFF
        assert frame[-1] == (crc >> 8) & 0xFF


class TestBuildWriteMultipleRegistersFrame:
    def test_structure(self):
        frame = build_write_multiple_registers_frame(
            slave_id=1, address=0x0000, values=[0x1234, 0x5678]
        )
        assert frame[0] == 0x01  # slave_id
        assert frame[1] == 0x10  # FC
        assert frame[2:4] == struct.pack(">H", 0x0000)  # address
        assert frame[4:6] == struct.pack(">H", 2)  # count
        assert frame[6] == 4  # byte count
        assert frame[7:9] == struct.pack(">H", 0x1234)
        assert frame[9:11] == struct.pack(">H", 0x5678)
        assert len(frame) == 13  # 1+1+2+2+1+4+2(CRC)

    def test_crc_appended(self):
        frame = build_write_multiple_registers_frame(
            slave_id=1, address=0x0000, values=[0x0001]
        )
        payload = frame[:-2]
        crc = calculate_crc(payload)
        assert frame[-2] == crc & 0xFF
        assert frame[-1] == (crc >> 8) & 0xFF


class TestModbusError:
    def test_basic(self):
        err = ModbusError("test error")
        assert str(err) == "test error"
        assert err.slave_id is None

    def test_with_slave_id(self):
        err = ModbusError("test", slave_id=5)
        assert err.slave_id == 5

    def test_is_exception(self):
        with pytest.raises(ModbusError):
            raise ModbusError("boom")


class TestModbusRTUClient:
    def test_not_connected_by_default(self):
        client = ModbusRTUClient()
        assert not client.is_connected

    def test_read_register_not_connected_raises(self):
        client = ModbusRTUClient()
        with pytest.raises(ModbusError, match="not connected"):
            client.read_register(slave_id=1, address=0)

    def test_write_register_not_connected_raises(self):
        client = ModbusRTUClient()
        with pytest.raises(ModbusError, match="not connected"):
            client.write_register(slave_id=1, address=0, value=0)

    def test_read_register_32bit_not_connected_raises(self):
        client = ModbusRTUClient()
        with pytest.raises(ModbusError, match="not connected"):
            client.read_register_32bit(slave_id=1, address=0)

    def test_write_register_32bit_not_connected_raises(self):
        client = ModbusRTUClient()
        with pytest.raises(ModbusError, match="not connected"):
            client.write_register_32bit(slave_id=1, address=0, value=0)

    def test_context_manager(self):
        with ModbusRTUClient() as client:
            assert not client.is_connected
        # disconnect called without error even when not connected

    def test_disconnect_when_not_connected(self):
        client = ModbusRTUClient()
        client.disconnect()  # should not raise

    def test_default_parameters(self):
        client = ModbusRTUClient()
        assert client._baudrate == 115200
        assert client._timeout == 0.5
        assert client._retries == 3
