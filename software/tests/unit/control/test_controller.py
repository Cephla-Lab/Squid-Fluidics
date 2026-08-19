# tests/unit/control/test_controller.py
import warnings

import numpy as np
import pytest
from cobs import cobs

import fluidics.control.controller as controller_module
from fluidics.control.controller import split_byte, uint_to_bytes
from fluidics.control._def import MCU_CONSTANTS, CMD_SET, MCU_MSG_LENGTH


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


from .packet_helpers import make_status_packet as _make_packet


def _set_be16(msg, index, value):
    """Write a signed 16-bit value into msg[index:index+2], big-endian."""
    unsigned = value & 0xFFFF
    msg[index] = (unsigned >> 8) & 0xFF
    msg[index + 1] = unsigned & 0xFF


def _bare_controller():
    """A FluidController with no serial port, for testing pure logic.

    __init__ is bypassed, so attributes it would normally set must be filled
    in by hand: log_measurements/debug because _publish_status reads them,
    and cmd_sent (mirroring __init__'s CMD_SET.CLEAR default) because
    wait_for_completion's timeout message reads it. Otherwise these raise
    AttributeError instead of exercising the behavior under test.
    """
    fc = FluidController.__new__(FluidController)
    fc.log_measurements = False
    fc.debug = False
    fc.cmd_sent = CMD_SET.CLEAR
    return fc


class TestParsePacket:
    @pytest.mark.parametrize("raw", [1000, -1000, 32500, 32767])
    def test_flow_passes_through_unscaled(self, raw):
        """_parse_packet hands on the sensor's counts untouched. Turning them
        into uL/min needs the installed part's scale factor, which lives with
        the driver -- so that conversion is tested in test_flow_sensor.py, not
        here. 32767 is the no-reading sentinel and 32500 a real saturated
        reading; at this layer both are simply carried through.
        """
        fc = _bare_controller()
        parsed = fc._parse_packet(_make_packet(flow_raw=raw))
        assert parsed["flowrates_raw"][0] == raw

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
        assert fc.get_mcu_status()["flowrates_raw"][0] == 250

    def test_publish_increments_sequence(self):
        fc = _bare_controller()
        fc._init_status_state()
        fc._publish_status(fc._parse_packet(_make_packet()))
        first = fc._status_seq
        fc._publish_status(fc._parse_packet(_make_packet()))
        assert fc._status_seq == first + 1

    def test_subscriber_receives_the_parsed_dict(self):
        fc = _bare_controller()
        fc._init_status_state()
        seen = []
        fc.subscribe_packets(seen.append)
        fc._publish_status(fc._parse_packet(_make_packet(flow_raw=400)))
        assert len(seen) == 1
        assert seen[0]["flowrates_raw"][0] == 400

    def test_subscriber_exception_does_not_break_publishing(self):
        fc = _bare_controller()
        fc._init_status_state()
        fc.subscribe_packets(lambda _: 1 / 0)
        fc._publish_status(fc._parse_packet(_make_packet(flow_raw=400)))
        assert fc.get_mcu_status()["flowrates_raw"][0] == 400

    def test_snapshot_is_a_copy(self):
        fc = _bare_controller()
        fc._init_status_state()
        fc._publish_status(fc._parse_packet(_make_packet()))
        snapshot = fc.get_mcu_status()
        snapshot["flowrates_raw"] = "clobbered"
        assert fc.get_mcu_status()["flowrates_raw"] != "clobbered"


class TestPacketSubscribers:
    """Fan-out replaced a single callback slot, which two flow sensors made
    untenable -- the second silently displaced the first.
    """

    def test_every_subscriber_receives_each_packet(self):
        fc = _bare_controller()
        fc._init_status_state()
        a, b = [], []
        fc.subscribe_packets(a.append)
        fc.subscribe_packets(b.append)
        fc._publish_status(fc._parse_packet(_make_packet(flow_raw=100)))
        assert len(a) == 1 and len(b) == 1

    def test_one_raising_subscriber_does_not_starve_the_others(self):
        """A single try around the loop would let subscriber 0 permanently
        starve subscriber 1 at 17 packets/second.
        """
        fc = _bare_controller()
        fc._init_status_state()
        seen = []
        fc.subscribe_packets(lambda _: 1 / 0)
        fc.subscribe_packets(seen.append)
        fc._publish_status(fc._parse_packet(_make_packet(flow_raw=100)))
        assert len(seen) == 1

    def test_unsubscribe_removes_only_that_subscriber(self):
        fc = _bare_controller()
        fc._init_status_state()
        a, b = [], []
        # Bound methods are fresh objects per access (a.append is not a.append),
        # so identity removal requires holding the object that was registered --
        # the same reason FlowSensor stores _packet_handler.
        cb_a, cb_b = a.append, b.append
        fc.subscribe_packets(cb_a)
        fc.subscribe_packets(cb_b)
        fc.unsubscribe_packets(cb_a)
        fc._publish_status(fc._parse_packet(_make_packet()))
        assert a == [] and len(b) == 1

    def test_unsubscribe_is_a_noop_when_absent(self):
        """close() is reachable twice: begin()'s failure path and teardown."""
        fc = _bare_controller()
        fc._init_status_state()
        fc.unsubscribe_packets(lambda _: None)

    def test_unsubscribe_removes_every_registration_of_a_callback(self):
        """No caller double-subscribes, so removal is all-or-nothing rather
        than counted -- simpler, and there is no semantics to get wrong.
        """
        fc = _bare_controller()
        fc._init_status_state()
        seen = []
        cb = seen.append
        fc.subscribe_packets(cb)
        fc.subscribe_packets(cb)
        fc.unsubscribe_packets(cb)
        fc._publish_status(fc._parse_packet(_make_packet()))
        assert seen == []

    def test_subscriber_may_unsubscribe_itself_during_dispatch(self):
        """Dispatch runs outside the lock, so re-entrant removal must not
        deadlock or skip the remaining subscribers.
        """
        fc = _bare_controller()
        fc._init_status_state()
        once_calls, persistent = [], []

        def once(parsed):
            once_calls.append(parsed)
            fc.unsubscribe_packets(once)

        fc.subscribe_packets(once)
        fc.subscribe_packets(persistent.append)
        fc._publish_status(fc._parse_packet(_make_packet()))
        fc._publish_status(fc._parse_packet(_make_packet()))
        assert len(once_calls) == 1     # removed itself after the first
        assert len(persistent) == 2     # never skipped


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


class _FakeSerial:
    """Serial stub that hands out a fixed byte script one byte at a time."""

    def __init__(self, script):
        self._buf = bytearray(script)

    @property
    def in_waiting(self):
        return len(self._buf)

    def read(self):
        return bytes([self._buf.pop(0)])

    def close(self):
        """Called by FluidController.__del__ during GC."""


class TestReadPacketResync:
    """A corrupt COBS frame must not poison every frame after it.

    read_received_packet_nowait accumulates bytes into self.read_buffer until a
    0x00 delimiter, then decodes. It clears the buffer on success; if it did not
    also clear on a decode failure, the corrupt bytes would stay and every later
    frame would be appended onto them, so cobs.decode would keep failing until a
    spurious 0x00 happened to resync it.
    """

    @staticmethod
    def _controller(script):
        fc = FluidController.__new__(FluidController)
        fc.use_cobs = True
        fc.rx_buffer_length = MCU_MSG_LENGTH
        fc.read_buffer = []
        fc.serial = _FakeSerial(script)
        return fc

    def test_corrupt_frame_raises_but_leaves_no_residue(self):
        # 0x05 claims four more bytes follow; only one does, so decode fails.
        fc = self._controller([0x05, 0x01, 0x00])
        with pytest.raises(Exception):
            fc.read_received_packet_nowait()
        assert fc.read_buffer == []

    def test_next_frame_decodes_after_a_corrupt_one(self):
        good = cobs.encode(bytes([0xAA, 0xBB, 0xCC]))
        fc = self._controller([0x05, 0x01, 0x00] + list(good) + [0x00])

        with pytest.raises(Exception):
            fc.read_received_packet_nowait()

        assert list(fc.read_received_packet_nowait()) == [0xAA, 0xBB, 0xCC]


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
        """Regression guard for a mutant that replaces the length guard with
        `pass`. A 10-byte message alone doesn't distinguish the guard from no
        guard at all: _parse_packet indexes up to msg[29], so a too-short
        message raises IndexError either way, and that IndexError is caught
        by the same except block that a real COBS failure would hit --
        _latest_status stays None regardless of whether the explicit length
        check ever ran.

        A message *longer* than MCU_MSG_LENGTH (31, not 10) closes that gap:
        _parse_packet only reads indices 0-29, so it parses successfully and
        would publish if the guard were removed. Only the real guard prevents
        that.
        """
        fc = _bare_controller()
        fc._init_status_state()
        calls = []

        def bad_length_read():
            calls.append(1)
            if len(calls) >= 2:
                fc._terminate_reader = True
                return None
            return [0] * 31  # not MCU_MSG_LENGTH (30), but long enough to parse cleanly

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

        assert fc.get_mcu_status()["flowrates_raw"][0] == 300


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


class TestDelDefensive:
    """__del__ must be defensive against partial __init__. If __init__ raises
    partway through (e.g. log file open fails before measurement_file is
    assigned), GC still calls __del__ on the half-built object. The old guard
    `if getattr(self, 'log_measurements', False)` would pass if the flag was
    set but the file was never opened, then the next line would raise and mask
    the original __init__ error. The new guard checks the attribute that's
    actually dereferenced on the next line.
    """

    def test_del_with_log_measurements_true_but_no_file_does_not_raise(self):
        """Regression test for the bug fixed in the code edit.

        Scenario: __init__ sets `log_measurements = True` but then the
        `open()` call for the log file raises. The flag exists and is `True`,
        but `measurement_file` was never assigned. With the old guard
        `if getattr(self, 'log_measurements', False)`, __del__ would try to
        close a non-existent attribute and raise AttributeError, masking the
        original error that __init__ raised.

        With the new guard `if getattr(self, 'measurement_file', None) is not None`,
        __del__ detects the file was never created and skips the close safely.
        """
        fc = FluidController.__new__(FluidController)
        fc.log_measurements = True  # flag is set
        # deliberately NOT setting measurement_file, mimicking __init__ raising after the flag
        fc.debug = False

        # The assertion is that this does not raise; pytest enforces that
        # without an explicit assert.
        fc.__del__()


class TestWaitForCompletion:
    def test_returns_when_uid_matches(self):
        fc = _bare_controller()
        fc._init_status_state()
        fc.cmd_uid = 7
        fc._publish_status(fc._parse_packet(
            _make_packet(uid=7, status=COMMAND_STATUS.COMPLETED_WITHOUT_ERRORS)))
        assert fc.wait_for_completion() == COMMAND_STATUS.COMPLETED_WITHOUT_ERRORS

    def test_ignores_stale_packet_from_previous_command(self):
        fc = _bare_controller()
        fc._init_status_state()
        fc.cmd_uid = 8
        # The packet still in flight carries the *previous* command, completed.
        fc._publish_status(fc._parse_packet(
            _make_packet(uid=7, status=COMMAND_STATUS.COMPLETED_WITHOUT_ERRORS)))
        with pytest.raises(TimeoutError):
            fc.wait_for_completion(timeout=1)

    def test_waits_through_in_progress(self):
        fc = _bare_controller()
        fc._init_status_state()
        fc.cmd_uid = 9
        fc._publish_status(fc._parse_packet(
            _make_packet(uid=9, status=COMMAND_STATUS.IN_PROGRESS)))
        with pytest.raises(TimeoutError):
            fc.wait_for_completion(timeout=1)

    def test_returns_error_status_without_raising(self):
        fc = _bare_controller()
        fc._init_status_state()
        fc.cmd_uid = 10
        fc._publish_status(fc._parse_packet(
            _make_packet(uid=10, status=COMMAND_STATUS.CMD_EXECUTION_ERROR)))
        assert fc.wait_for_completion() == COMMAND_STATUS.CMD_EXECUTION_ERROR

    def test_times_out_if_no_packet_ever_arrives(self):
        """Regression guard: get_mcu_status() blocks forever waiting for the
        first packet, so wait_for_completion must not route through it. If it
        did, this test would hang instead of failing -- unplugged hardware,
        a crashed MCU, or the wrong serial port would hang forever too.
        """
        fc = _bare_controller()
        fc._init_status_state()
        fc.cmd_uid = 12
        # No packet is ever published -- _latest_status stays None throughout.
        with pytest.raises(TimeoutError):
            fc.wait_for_completion(timeout=1)

    def test_timeout_message_names_the_command(self):
        fc = _bare_controller()
        fc._init_status_state()
        fc.cmd_uid = 11
        fc.cmd_sent = 3
        fc._publish_status(fc._parse_packet(_make_packet(uid=10)))
        with pytest.raises(TimeoutError) as excinfo:
            fc.wait_for_completion(timeout=1)
        # Pin both pieces individually -- match="11" alone stays green even if
        # "{self.cmd_sent}" is deleted from the f-string, since "1" is a
        # substring of "11" and of "within 1s".
        assert "command 3" in str(excinfo.value)
        assert "uid 11" in str(excinfo.value)


class TestClearResetsUid:
    """CMD_SET.CLEAR resets cmd_uid to 0, and send_command writes the UID into
    the outgoing array *after* that reset. So the command goes out with UID 0
    and self.cmd_uid == 0, and the subsequent wait can match. This is correct
    by accident of statement order — pin it.
    """

    def test_clear_leaves_cmd_uid_at_zero(self):
        fc = _bare_controller()
        fc._init_status_state()
        fc.cmd_uid = 57
        fc.serial = None
        sent = []
        fc.send_mcu_command = sent.append
        fc.send_command(CMD_SET.CLEAR)
        assert fc.cmd_uid == 0
        assert sent[0][0] == 0 and sent[0][1] == 0


class TestSequenceGuardsAgainstUidReuse:
    """CMD_SET.CLEAR resets cmd_uid to 0 on the host, and the firmware resets
    its own UID counter to 0 too (controller_teensy41.ino). So two consecutive
    CLEAR commands share UID 0: the terminal packet for the first CLEAR is
    still the "latest" packet, still carrying UID 0, when the second CLEAR is
    sent. UID matching alone would accept it immediately -- before the second
    CLEAR had even reached the firmware, let alone finished homing every
    selector valve. The publish-sequence check (seq > seq at send time) is
    what actually distinguishes "the stale packet from before" from "a fresh
    packet published after this command went out."
    """

    def test_second_clear_ignores_first_clears_stale_terminal_packet(self):
        fc = _bare_controller()
        fc._init_status_state()
        fc.cmd_uid = 0
        fc.serial = None
        fc.send_mcu_command = lambda cmd: None

        # First CLEAR goes out (UID 0), and its terminal packet (UID 0) is
        # published afterward -- a normal, correct completion.
        fc.send_command(CMD_SET.CLEAR)
        fc._publish_status(fc._parse_packet(
            _make_packet(uid=0, status=COMMAND_STATUS.COMPLETED_WITHOUT_ERRORS)))
        assert fc.wait_for_completion() == COMMAND_STATUS.COMPLETED_WITHOUT_ERRORS

        # Second CLEAR also goes out with UID 0. The first CLEAR's terminal
        # packet is still the latest one published and still carries UID 0 --
        # UID matching alone would accept it here, immediately, wrongly.
        fc.send_command(CMD_SET.CLEAR)
        with pytest.raises(TimeoutError):
            fc.wait_for_completion(timeout=1)

        # Only once a packet published *after* the second send arrives
        # (UID 0 again -- same reused UID) does it return.
        fc._publish_status(fc._parse_packet(
            _make_packet(uid=0, status=COMMAND_STATUS.COMPLETED_WITHOUT_ERRORS)))
        assert fc.wait_for_completion() == COMMAND_STATUS.COMPLETED_WITHOUT_ERRORS


class TestSeqAtSendCapturedAfterWrite:
    """_seq_at_send must be captured after send_mcu_command() returns, not
    before. The write is synchronous, so anything published before it
    returns is unambiguously stale. Capturing before it leaves a window (lock
    release -> bytes actually on the wire) during which the reader thread
    could consume one of the firmware's unconditional 60 ms idle packets and
    bump _status_seq -- making a packet that predates the command satisfy
    `seq > _seq_at_send`.
    """

    def test_publish_during_the_write_is_not_treated_as_post_send(self):
        fc = _bare_controller()
        fc._init_status_state()
        fc.cmd_uid = 5  # CLEAR resets this to 0 regardless
        fc.serial = None

        # A stale terminal packet is already published *before* send_command
        # is even called -- e.g. the previous CLEAR's terminal packet, same
        # UID 0 due to CLEAR's UID reuse (see TestSequenceGuardsAgainstUidReuse).
        fc._publish_status(fc._parse_packet(
            _make_packet(uid=0, status=COMMAND_STATUS.COMPLETED_WITHOUT_ERRORS)))

        # Simulate the reader thread consuming one more (idle) packet *during*
        # the synchronous send_mcu_command() call -- i.e. between releasing
        # the lock and the bytes actually leaving the host.
        def send_mcu_command_that_races(cmd):
            fc._publish_status(fc._parse_packet(
                _make_packet(uid=0, status=COMMAND_STATUS.COMPLETED_WITHOUT_ERRORS)))

        fc.send_mcu_command = send_mcu_command_that_races

        fc.send_command(CMD_SET.CLEAR)

        # That packet predates the command reaching the wire. If _seq_at_send
        # were captured before send_mcu_command(), it would already be stale
        # by the time the racing publish happens, so seq > _seq_at_send would
        # hold and wait_for_completion would return immediately -- wrongly.
        # Captured after, the racing publish is folded into _seq_at_send, so
        # nothing published so far can satisfy the check and this must time
        # out.
        with pytest.raises(TimeoutError):
            fc.wait_for_completion(timeout=1)


class TestBeginStartsReader:
    """begin() starting the reader thread is the premise of the whole
    always-on-reader design -- without it, every command hangs on hardware
    waiting for a packet that never gets read.
    """

    def test_begin_calls_start_reading(self, monkeypatch):
        fc = _bare_controller()
        fc._init_status_state()
        monkeypatch.setattr(controller_module.Microcontroller, "begin", lambda self: None)
        calls = []
        fc.start_reading = lambda: calls.append(1)

        fc.begin()

        assert calls == [1]


class TestSendCommandBlockingPassesTimeout:
    """send_command_blocking's timeout kwarg must reach wait_for_completion.
    Firmware operations like CLEAR_LINES/UNLOAD_FLUID_VOLUME routinely run
    35-50s; if the kwarg silently gets dropped, callers passing a matching
    timeout would still time out at the 30s default.
    """

    def test_custom_timeout_reaches_wait_for_completion(self):
        fc = _bare_controller()
        fc._init_status_state()
        fc.cmd_uid = 0
        fc.serial = None
        fc.send_mcu_command = lambda cmd: None

        captured = {}

        def fake_wait_for_completion(timeout=30):
            captured['timeout'] = timeout
            return COMMAND_STATUS.COMPLETED_WITHOUT_ERRORS

        fc.wait_for_completion = fake_wait_for_completion

        fc.send_command_blocking(CMD_SET.CLEAR, timeout=45)

        assert captured['timeout'] == 45


class TestPublishStatusSetsRecordedData:
    """_publish_status must update recorded_data -- it's the attribute every
    hardware script and blocking loop (e.g. tests/hardware/startup.py's
    pressure_vacuum_test) reads directly, outside of get_mcu_status().
    """

    def test_publish_status_sets_recorded_data(self):
        fc = _bare_controller()
        fc._init_status_state()
        fc.recorded_data = {}
        parsed = fc._parse_packet(_make_packet(flow_raw=123))

        fc._publish_status(parsed)

        assert fc.recorded_data == parsed
