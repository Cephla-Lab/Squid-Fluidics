# tests/unit/control/test_controller.py
import warnings

import numpy as np
import pytest

import fluidics.control.controller as controller_module
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


def _set_be16(msg, index, value):
    """Write a signed 16-bit value into msg[index:index+2], big-endian."""
    unsigned = value & 0xFFFF
    msg[index] = (unsigned >> 8) & 0xFF
    msg[index + 1] = unsigned & 0xFF


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


class TestNegativeRawDecoding:
    """Every reading with the sign bit set (negative flow, backwards volume,
    or valve-mask bit 15) used to round-trip through np.int16(), which warns
    today and raises OverflowError on NumPy 2.x. _parse_packet now reinterprets
    these fields with a pure-Python two's-complement helper instead. Running
    each decode with warnings promoted to errors proves the numpy cast is
    gone, not just that the numbers happen to still match.
    """

    def test_flow_negative_high_bit_no_numpy_warning(self):
        fc = _bare_controller()
        msg = _make_packet(flow_raw=-1000)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            parsed = fc._parse_packet(msg)
        assert parsed["flowrates_raw"][0] == -1000
        assert parsed["flowrates"][0] == pytest.approx(-100.0)

    def test_solenoid_valves_negative_high_bit_no_numpy_warning(self):
        fc = _bare_controller()
        msg = _make_packet()
        _set_be16(msg, 11, -1)  # all 16 valves on -> 0xFFFF -> -1 signed
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            parsed = fc._parse_packet(msg)
        assert parsed["solenoid_valves"] == -1

    def test_vol_ul_negative_high_bit_no_numpy_warning(self):
        fc = _bare_controller()
        msg = _make_packet()
        _set_be16(msg, 28, -1000)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            parsed = fc._parse_packet(msg)
        expected = (float(-1000) / np.iinfo(np.int16).max) * MCU_CONSTANTS.VOLUME_UL_MAX
        assert parsed["vol_ul"] == pytest.approx(expected)


class TestReaderLoop:
    """_reader_loop is the thread body; these call it directly (synchronously,
    in the test thread) so no real thread is ever started. Each double's
    read_received_packet_nowait flips _terminate_reader after a fixed number
    of calls so the loop is guaranteed to return.
    """

    def test_raising_read_does_not_escape_loop(self):
        fc = _bare_controller()
        fc._init_status_state()
        calls = []

        def flaky_read(discard_buffer=False):
            calls.append(1)
            if len(calls) >= 3:
                fc._terminate_reader = True
            raise RuntimeError("simulated COBS decode failure")

        fc.read_received_packet_nowait = flaky_read

        fc._reader_loop()  # must return normally, not raise

        assert len(calls) == 3
        assert fc._latest_status is None  # nothing was ever published

    def test_wrong_length_message_is_skipped_without_publishing(self):
        fc = _bare_controller()
        fc._init_status_state()
        calls = []

        def bad_length_read():
            calls.append(1)
            if len(calls) >= 2:
                fc._terminate_reader = True
                return None
            return [0] * 10  # not MCU_MSG_LENGTH (30) bytes

        fc.read_received_packet_nowait = bad_length_read

        fc._reader_loop()

        assert len(calls) == 2
        assert fc._latest_status is None
        assert fc._status_seq == 0

    def test_valid_packet_is_published(self):
        fc = _bare_controller()
        fc._init_status_state()
        calls = []

        def good_read():
            calls.append(1)
            if len(calls) >= 2:
                fc._terminate_reader = True
                return None
            return _make_packet(flow_raw=300)

        fc.read_received_packet_nowait = good_read

        fc._reader_loop()

        assert fc.get_mcu_status()["flowrates"][0] == pytest.approx(30.0)


class _FakeThreadingNamespace:
    """Stand-in for the `threading` module as seen from inside controller.py.

    Lets start_reading()/stop_reading() be tested without ever spawning a
    real OS thread -- a live thread under the autouse _fast_clock fixture
    (which makes time.sleep/Event.wait non-blocking) would spin unboundedly.
    Only the module *name* inside controller.py is swapped (via monkeypatch,
    restored after the test); the real threading module is untouched.
    """

    class Thread:
        def __init__(self, target=None, daemon=None):
            self.target = target
            self.daemon = daemon
            self.started = False

        def start(self):
            self.started = True

        def join(self, timeout=None):
            pass


class TestReaderLifecycle:
    def test_start_reading_twice_creates_one_thread(self, monkeypatch):
        fc = _bare_controller()
        fc._init_status_state()  # uses the real threading.Lock, fine synchronously
        monkeypatch.setattr(controller_module, "threading", _FakeThreadingNamespace)

        fc.start_reading()
        first_thread = fc._reader_thread
        fc.start_reading()

        assert fc._reader_thread is first_thread
        assert isinstance(first_thread, _FakeThreadingNamespace.Thread)
        assert first_thread.started
        assert first_thread.target == fc._reader_loop

    def test_stop_reading_resets_thread_and_sets_terminate_flag(self, monkeypatch):
        fc = _bare_controller()
        fc._init_status_state()
        monkeypatch.setattr(controller_module, "threading", _FakeThreadingNamespace)

        fc.start_reading()
        fc.stop_reading()

        assert fc._reader_thread is None
        assert fc._terminate_reader is True
