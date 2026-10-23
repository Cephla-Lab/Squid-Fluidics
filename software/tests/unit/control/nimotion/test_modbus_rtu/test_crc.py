"""
CRC-16 module tests
"""

import pytest

from fluidics.control.nimotion.modbus_rtu.crc import (
    append_crc,
    calculate_crc,
    calculate_crc_bytes,
    extract_crc,
    verify_crc,
)


class TestCalculateCRC:
    """Test CRC calculation"""

    def test_known_values(self) -> None:
        """Test known values"""
        # Modbus RTU standard test vector
        # slave=0x01, function=0x03, address=0x0000, quantity=0x0001
        data = bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x01])
        crc = calculate_crc(data)
        assert crc == 0x0A84

    def test_empty_data(self) -> None:
        """Test empty data"""
        crc = calculate_crc(b"")
        assert crc == 0xFFFF  # initial value

    def test_single_byte(self) -> None:
        """Test single byte"""
        crc = calculate_crc(bytes([0x00]))
        assert isinstance(crc, int)
        assert 0 <= crc <= 0xFFFF


class TestCalculateCRCBytes:
    """Test CRC byte calculation"""

    def test_output_format(self) -> None:
        """Test output format (low byte first)"""
        data = bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x01])
        crc_bytes = calculate_crc_bytes(data)

        assert len(crc_bytes) == 2
        # CRC = 0x0A84, low byte=0x84, high byte=0x0A
        assert crc_bytes[0] == 0x84
        assert crc_bytes[1] == 0x0A


class TestVerifyCRC:
    """Test CRC verification"""

    def test_valid_frame(self) -> None:
        """Test valid frame"""
        # Frame with a valid CRC
        data = bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x01])
        frame = append_crc(data)

        assert verify_crc(frame) is True

    def test_invalid_frame(self) -> None:
        """Test invalid frame"""
        # Frame with a wrong CRC
        frame = bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x01, 0xFF, 0xFF])

        assert verify_crc(frame) is False

    def test_too_short(self) -> None:
        """Test frame that is too short"""
        frame = bytes([0x01, 0x03])
        assert verify_crc(frame) is False


class TestAppendCRC:
    """Test appending CRC"""

    def test_append_crc(self) -> None:
        """Test appending CRC"""
        data = bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x01])
        frame = append_crc(data)

        assert len(frame) == len(data) + 2
        assert frame[:-2] == data
        assert verify_crc(frame) is True

    def test_roundtrip(self) -> None:
        """Test roundtrip"""
        original = bytes([0x01, 0x06, 0x03, 0x80, 0x00, 0x0F])
        frame = append_crc(original)

        assert verify_crc(frame) is True
        payload, crc = extract_crc(frame)
        assert payload == original


class TestExtractCRC:
    """Test extracting CRC"""

    def test_extract(self) -> None:
        """Test extraction"""
        data = bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x01])
        frame = append_crc(data)

        payload, crc = extract_crc(frame)

        assert payload == data
        assert crc == calculate_crc(data)

    def test_too_short(self) -> None:
        """Test data that is too short"""
        with pytest.raises(ValueError):
            extract_crc(bytes([0x01, 0x02]))
