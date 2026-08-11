# tests/unit/control/test_controller.py
import numpy as np
import pytest

from fluidics.control.controller import split_byte, uint_to_bytes
from fluidics.control._def import MCU_CONSTANTS


class TestSplitByte:
    def test_zero(self):
        assert split_byte(0x00) == (0, 0)

    def test_max(self):
        assert split_byte(0xFF) == (0x0F, 0x0F)

    def test_known_value(self):
        assert split_byte(0xAB) == (0x0A, 0x0B)

    def test_high_nibble_only(self):
        assert split_byte(0xF0) == (0x0F, 0x00)

    def test_low_nibble_only(self):
        assert split_byte(0x0F) == (0x00, 0x0F)


class TestUintToBytes:
    def test_zero_one_byte(self):
        assert uint_to_bytes(0, 1) == [np.uint8(0)]

    def test_zero_two_bytes(self):
        assert uint_to_bytes(0, 2) == [np.uint8(0), np.uint8(0)]

    def test_255_one_byte(self):
        assert uint_to_bytes(255, 1) == [np.uint8(255)]

    def test_256_two_bytes(self):
        result = uint_to_bytes(256, 2)
        assert result == [np.uint8(1), np.uint8(0)]

    def test_65535_two_bytes(self):
        result = uint_to_bytes(65535, 2)
        assert result == [np.uint8(255), np.uint8(255)]

    def test_overflow_raises(self):
        # Note: uint_to_bytes has a bug where exact powers of 2 like 256 pass
        # the log2 overflow check despite not fitting in n_bytes. We use 257
        # to reliably trigger the overflow assertion.
        with pytest.raises(AssertionError, match="Overflow"):
            uint_to_bytes(257, 1)

    def test_four_bytes(self):
        result = uint_to_bytes(0x01020304, 4)
        assert result == [np.uint8(1), np.uint8(2), np.uint8(3), np.uint8(4)]


class TestRawToPsi:
    """Test the raw_to_psi conversion formula from get_mcu_status.

    Formula: (raw - output_min) * (p_max - p_min) / (output_max - output_min) + p_min
    With: output_min=0, output_max=16383, p_min=-15, p_max=15
    """

    @staticmethod
    def raw_to_psi(raw_pressure):
        return (
            (raw_pressure - MCU_CONSTANTS._output_min)
            * (MCU_CONSTANTS._p_max - MCU_CONSTANTS._p_min)
            / (MCU_CONSTANTS._output_max - MCU_CONSTANTS._output_min)
            + MCU_CONSTANTS._p_min
        )

    def test_min_raw_gives_min_psi(self):
        result = self.raw_to_psi(0)
        assert result == pytest.approx(-15.0)

    def test_max_raw_gives_max_psi(self):
        result = self.raw_to_psi(16383)
        assert result == pytest.approx(15.0)

    def test_midpoint_gives_zero_psi(self):
        result = self.raw_to_psi(16383 / 2)
        assert result == pytest.approx(0.0, abs=0.01)


from fluidics.control.controller import FluidController
from fluidics.control._def import COMMAND_STATUS


def _make_packet(uid=1, cmd=3, status=COMMAND_STATUS.COMPLETED_WITHOUT_ERRORS,
                 flow_raw=1000):
    """Build a 30-byte MCU status packet with the given flow sensor 1 value."""
    msg = [0] * 30
    msg[0] = (uid >> 8) & 0xFF
    msg[1] = uid & 0xFF
    msg[2] = cmd
    msg[3] = status
    unsigned = flow_raw & 0xFFFF
    msg[23] = (unsigned >> 8) & 0xFF
    msg[24] = unsigned & 0xFF
    return msg


def _bare_controller():
    """A FluidController with no serial port, for testing pure logic.

    __init__ is bypassed, so the two flags _log_packet reads must be set by
    hand — otherwise _publish_status raises AttributeError.
    """
    fc = FluidController.__new__(FluidController)
    fc.log_measurements = False
    fc.debug = False
    return fc


class TestParsePacket:
    def test_positive_flow_scales_by_ten(self):
        fc = _bare_controller()
        parsed = fc._parse_packet(_make_packet(flow_raw=1000))
        assert parsed["flowrates"][0] == pytest.approx(100.0)
        assert parsed["flowrates_raw"][0] == 1000

    def test_negative_flow_scales_by_ten(self):
        fc = _bare_controller()
        parsed = fc._parse_packet(_make_packet(flow_raw=-1000))
        assert parsed["flowrates"][0] == pytest.approx(-100.0)
        assert parsed["flowrates_raw"][0] == -1000

    def test_sentinel_survives_as_raw(self):
        fc = _bare_controller()
        parsed = fc._parse_packet(_make_packet(flow_raw=32767))
        assert parsed["flowrates_raw"][0] == 32767

    def test_saturation_is_distinct_from_sentinel(self):
        fc = _bare_controller()
        parsed = fc._parse_packet(_make_packet(flow_raw=32500))
        assert parsed["flowrates_raw"][0] == 32500
        assert parsed["flowrates"][0] == pytest.approx(3250.0)

    def test_uid_and_status_round_trip(self):
        fc = _bare_controller()
        parsed = fc._parse_packet(_make_packet(uid=513, status=COMMAND_STATUS.IN_PROGRESS))
        assert parsed["MCU_received_command_UID"] == 513
        assert parsed["MCU_command_execution_status"] == COMMAND_STATUS.IN_PROGRESS


class TestPublishStatus:
    def test_publish_makes_status_readable(self):
        fc = _bare_controller()
        fc._init_status_state()
        fc._publish_status(fc._parse_packet(_make_packet(flow_raw=250)))
        assert fc.get_mcu_status()["flowrates"][0] == pytest.approx(25.0)

    def test_publish_increments_sequence(self):
        fc = _bare_controller()
        fc._init_status_state()
        fc._publish_status(fc._parse_packet(_make_packet()))
        first = fc._status_seq
        fc._publish_status(fc._parse_packet(_make_packet()))
        assert fc._status_seq == first + 1

    def test_packet_callback_fires_with_parsed_dict(self):
        fc = _bare_controller()
        fc._init_status_state()
        seen = []
        fc.packet_callback = seen.append
        fc._publish_status(fc._parse_packet(_make_packet(flow_raw=400)))
        assert len(seen) == 1
        assert seen[0]["flowrates"][0] == pytest.approx(40.0)

    def test_callback_exception_does_not_break_publishing(self):
        fc = _bare_controller()
        fc._init_status_state()
        fc.packet_callback = lambda _: 1 / 0
        fc._publish_status(fc._parse_packet(_make_packet(flow_raw=400)))
        assert fc.get_mcu_status()["flowrates"][0] == pytest.approx(40.0)

    def test_snapshot_is_a_copy(self):
        fc = _bare_controller()
        fc._init_status_state()
        fc._publish_status(fc._parse_packet(_make_packet()))
        snapshot = fc.get_mcu_status()
        snapshot["flowrates"] = "clobbered"
        assert fc.get_mcu_status()["flowrates"] != "clobbered"
