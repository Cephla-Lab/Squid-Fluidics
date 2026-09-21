import logging
import threading

import pytest

from fluidics.control.temperature_controller import TCMControllerSimulation


class TestTCMControllerSimulation:
    def test_default_channels_is_2(self):
        tc = TCMControllerSimulation(sn=None)
        assert tc.channels == 2
        assert len(tc.target_temperatures) == 2
        assert len(tc.actual_temperatures) == 2

    def test_one_channel(self):
        tc = TCMControllerSimulation(sn=None, channels=1)
        assert tc.channels == 1
        assert len(tc.target_temperatures) == 1
        assert len(tc.actual_temperatures) == 1

    def test_set_target_updates_actual_in_simulation(self):
        tc = TCMControllerSimulation(sn=None, channels=2)
        tc.set_target_temperature(1, 37.5)
        assert tc.target_temperatures[0] == 37.5
        assert tc.actual_temperatures[0] == 37.5

    def test_set_target_only_updates_named_channel(self):
        tc = TCMControllerSimulation(sn=None, channels=2)
        tc.set_target_temperature(2, 50.0)
        # channel 1 untouched (still default 10.0)
        assert tc.target_temperatures[0] == 10.0
        assert tc.target_temperatures[1] == 50.0

    def test_get_target_temperature_returns_current_target(self):
        tc = TCMControllerSimulation(sn=None, channels=2)
        tc.set_target_temperature(1, 25.0)
        assert tc.get_target_temperature(1) == 25.0

    def test_get_actual_temperature_returns_simulated_actual(self):
        tc = TCMControllerSimulation(sn=None, channels=1)
        tc.set_target_temperature(1, 42.0)
        assert tc.get_actual_temperature(1) == 42.0

    def test_invalid_channel_raises(self):
        tc = TCMControllerSimulation(sn=None, channels=1)
        with pytest.raises(ValueError):
            tc.set_target_temperature(2, 25.0)

    def test_tolerance_and_timeout_stored(self):
        tc = TCMControllerSimulation(
            sn=None, channels=1,
            tolerance_celsius=0.5, stabilization_timeout_seconds=60,
        )
        assert tc.tolerance_celsius == 0.5
        assert tc.stabilization_timeout_seconds == 60

    def test_save_target_temperature_does_not_raise(self):
        tc = TCMControllerSimulation(sn=None, channels=2)
        tc.save_target_temperature(1)
        tc.save_target_temperature(2)

    def test_output_enabled_defaults_to_false(self):
        tc = TCMControllerSimulation(sn=None, channels=2)
        assert tc.output_enabled == [False, False]
        assert tc.get_output_enabled(1) is False
        assert tc.get_output_enabled(2) is False

    def test_set_output_enabled_only_updates_named_channel(self):
        tc = TCMControllerSimulation(sn=None, channels=2)
        tc.set_output_enabled(1, True)
        assert tc.output_enabled == [True, False]
        assert tc.get_output_enabled(1) is True
        assert tc.get_output_enabled(2) is False

    def test_set_output_enabled_coerces_truthy_to_bool(self):
        tc = TCMControllerSimulation(sn=None, channels=1)
        tc.set_output_enabled(1, 1)
        assert tc.output_enabled[0] is True
        tc.set_output_enabled(1, 0)
        assert tc.output_enabled[0] is False

    def test_output_enabled_invalid_channel_raises(self):
        tc = TCMControllerSimulation(sn=None, channels=1)
        with pytest.raises(ValueError):
            tc.set_output_enabled(2, True)
        with pytest.raises(ValueError):
            tc.get_output_enabled(2)

    def test_close_does_not_raise(self):
        tc = TCMControllerSimulation(sn=None, channels=1)
        tc.close()



# --- The readings channel ---
#
# The flow-sensor contract, pinned on both classes (why it exists: the
# start() docstring in the driver). _publish() is driven directly so nothing
# here starts the polling thread; the one lifecycle test that does closes it
# again.

from fluidics.control.controller import Subscribers
from fluidics.control.temperature_controller import TCMController


class ScriptedSerial:
    """Answers each query from `replies`; anything else times out (b"").

    The input is a buffer of lines, as the port's is: a reply waits there
    until it is read, and `late()` puts one there that nobody is waiting
    for -- the answer to a command that already timed out.

    A read hands back one line, which is the port when replies end "\r\n".
    When they end "\r" alone, as the manual has it, pyserial's readline
    returns everything waiting at once: a late reply then spoils one read
    and no more. The driver has to be right for both."""

    def __init__(self, replies):
        self.replies = replies
        self.written = []
        self._buffer = []

    def late(self, line):
        self._buffer.append(line)

    def reset_input_buffer(self):
        self._buffer.clear()

    def write(self, data):
        self.written.append(data)
        reply = self.replies.get(data)
        if reply:
            self._buffer.append(reply)

    def readline(self):
        return self._buffer.pop(0) if self._buffer else b""


def scripted_tcm(replies, channels=1):
    """A TCMController without hardware, over a scripted wire. The one place
    the driver is built by __new__, so an attribute __init__ gains is added
    here once -- pump_helpers.bare_pump, for this driver."""
    tcm = TCMController.__new__(TCMController)
    tcm.serial = ScriptedSerial(replies)
    tcm._serial_lock = threading.Lock()
    tcm.channels = channels
    tcm.actual_temperatures = [0.0] * channels
    tcm.output_voltages = [None] * channels
    tcm.output_currents = [None] * channels
    tcm._reads_failed = set()
    tcm._subscribers = Subscribers("Temperature controller")
    return tcm


class TestReadingsChannel:
    def test_subscriber_receives_each_publish(self):
        tc = TCMControllerSimulation(channels=2)
        seen = []
        tc.subscribe(seen.append)
        tc.actual_temperatures = [21.0, 37.0]
        tc._publish()
        assert seen == [[21.0, 37.0]]

    def test_the_payload_is_a_copy_not_the_live_list(self):
        tc = TCMControllerSimulation(channels=2)
        seen = []
        tc.subscribe(seen.append)
        tc._publish()
        tc.actual_temperatures[0] = 99.0
        assert seen[0][0] != 99.0

    def test_unsubscribed_callback_is_not_called(self):
        tc = TCMControllerSimulation(channels=2)
        seen = []
        callback = seen.append   # held once: unsubscribe matches by identity
        tc.subscribe(callback)
        tc.unsubscribe(callback)
        tc._publish()
        assert seen == []

    def test_a_failing_subscriber_does_not_break_others(self):
        tc = TCMControllerSimulation(channels=2)
        seen = []
        tc.subscribe(lambda temps: (_ for _ in ()).throw(RuntimeError("bad")))
        tc.subscribe(seen.append)
        tc._publish()
        assert len(seen) == 1

    def test_close_drops_subscribers(self):
        tc = TCMControllerSimulation(channels=2)
        seen = []
        tc.subscribe(seen.append)
        tc.close()
        tc._publish()
        assert seen == []

    def test_the_real_class_publishes_the_same_way(self):
        """The channel lives identically on both classes; the real one is
        built here without hardware, the interrupt-test way."""
        tcm = scripted_tcm({})
        tcm.actual_temperatures = [42.0]
        seen = []
        tcm.subscribe(seen.append)
        tcm._publish()
        assert seen == [[42.0]]


# --- The TEC's output voltage and current ---
#
# Polled beside the temperatures for the tab's readout. The real class is
# built without hardware, as above, over a scripted wire: what matters is that
# a unit which will not answer these costs the readout, never the poll loop.


class TestOutputReadings:
    def test_voltage_and_current_are_parsed_from_the_reply(self):
        tcm = scripted_tcm({
            b"TC1:TCACTVOL?\r": b"TC1:TCACTVOL=3.21\r",
            # Signed: cooling drives the TEC the other way, and the rig
            # reports -0.00 idle.
            b"TC1:TCACTCUR?\r": b"TC1:TCACTCUR=-1.05\r",
        })
        assert tcm.get_output_voltage(1) == 3.21
        assert tcm.get_output_current(1) == -1.05

    def test_the_channel_picks_the_module_on_the_wire(self):
        tcm = scripted_tcm({b"TC2:TCACTVOL?\r": b"TC2:TCACTVOL=0.5\r"},
                           channels=2)
        assert tcm.get_output_voltage(2) == 0.5
        assert tcm.serial.written == [b"TC2:TCACTVOL?\r"]

    @pytest.mark.parametrize("reply", [
        b"CMD:REPLY=2\r",          # parameter not found on this firmware
        b"",                       # timeout
        b"TC1:TCACTVOL=garbage\r",
        # A late answer to an earlier query, landing after the flush: a
        # temperature is not a voltage.
        b"TC1:TCACTUALTEMP=24.87\r",
    ])
    def test_a_read_that_fails_is_none(self, reply):
        tcm = scripted_tcm({b"TC1:TCACTVOL?\r": reply})
        assert tcm.get_output_voltage(1) is None

    def test_a_failing_read_warns_once_not_every_poll(self, caplog):
        tcm = scripted_tcm({b"TC1:TCACTVOL?\r": b"CMD:REPLY=2\r"})
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                tcm.get_output_voltage(1)
        assert len([r for r in caplog.records if "TCACTVOL" in r.message]) == 1

    def test_a_read_that_recovers_warns_again_if_it_fails_again(self, caplog):
        tcm = scripted_tcm({b"TC1:TCACTVOL?\r": b""})
        with caplog.at_level(logging.WARNING):
            tcm.get_output_voltage(1)
            tcm.serial.replies[b"TC1:TCACTVOL?\r"] = b"TC1:TCACTVOL=1.0\r"
            assert tcm.get_output_voltage(1) == 1.0
            tcm.serial.replies[b"TC1:TCACTVOL?\r"] = b""
            tcm.get_output_voltage(1)
        assert len([r for r in caplog.records if "TCACTVOL" in r.message]) == 2

    def test_a_poll_stores_both_and_still_publishes_temperatures(self):
        """A unit that answers neither must not cost the temperature plot."""
        tcm = scripted_tcm({b"TC1:TCACTUALTEMP?\r": b"TC1:TCACTUALTEMP=24.87\r"})
        seen = []
        tcm.subscribe(seen.append)
        tcm._poll_once()
        assert seen == [[24.87]]
        assert tcm.output_voltages == [None]
        assert tcm.output_currents == [None]

    def test_a_poll_stores_what_the_unit_reports(self):
        tcm = scripted_tcm({
            b"TC1:TCACTUALTEMP?\r": b"TC1:TCACTUALTEMP=24.87\r",
            b"TC1:TCACTVOL?\r": b"TC1:TCACTVOL=3.21\r",
            b"TC1:TCACTCUR?\r": b"TC1:TCACTCUR=1.05\r",
        })
        seen = []
        tcm.subscribe(seen.append)
        tcm._poll_once()
        assert tcm.output_voltages == [3.21]
        assert tcm.output_currents == [1.05]
        # An embedder subscribes to this channel: the payload is still the
        # temperatures, whatever else the poll read.
        assert seen == [[24.87]]

    def test_the_simulation_reports_an_idle_output(self):
        tc = TCMControllerSimulation(channels=2)
        assert tc.output_voltages == [0.0, 0.0]
        assert tc.output_currents == [0.0, 0.0]
        assert tc.get_output_voltage(1) == 0.0
        assert tc.get_output_current(2) == 0.0


# --- A reply that is not the one asked for ---
#
# The poll puts three kinds of reply on the wire, and an answer that comes
# after its command timed out is read by whoever asks next. A current read
# as a temperature is a wrong number; an error reply read by the temperature
# query raises, which in the poll loop ends the display's thread.

class TestStaleReplies:
    def test_a_late_reply_is_dropped_not_read_by_the_next_command(self):
        """Dropped, not shifted: every query after it gets its own answer."""
        tcm = scripted_tcm({
            b"TC1:TCACTUALTEMP?\r": b"TC1:TCACTUALTEMP=24.87\r",
            b"TC1:TCACTVOL?\r": b"TC1:TCACTVOL=3.21\r",
        })
        tcm.serial.late(b"TC1:TCACTCUR=-0.00\r")
        assert tcm.get_actual_temperature(1) == 24.87
        assert tcm.get_output_voltage(1) == 3.21

    def test_a_reply_for_another_parameter_is_not_a_target(self):
        """"TC1:TCACTCUR=-0.00" sliced as a target is 0.0, and it seeds
        target_temperatures at bring-up."""
        tcm = scripted_tcm({b"TC1:TCADJTEMP?\r": b"TC1:TCACTCUR=-0.00\r"})
        with pytest.raises(ValueError, match="unexpected reply"):
            tcm._read_target(1)

    def test_the_target_is_read_from_its_own_reply(self):
        tcm = scripted_tcm({b"TC1:TCADJTEMP?\r": b"TC1:TCADJTEMP=25.01\r"})
        assert tcm._read_target(1) == 25.01

    def test_a_reply_for_another_parameter_is_not_a_temperature(self):
        """The one read a flush cannot save: the late reply lands after it.
        "TC1:TCACTCUR=-0.00" sliced as a temperature is 0.0."""
        tcm = scripted_tcm({b"TC1:TCACTUALTEMP?\r": b"TC1:TCACTCUR=-0.00\r"})
        tcm.actual_temperatures = [24.0]
        assert tcm.get_actual_temperature(1) == 24.0

    @pytest.mark.parametrize("reply", [
        b"CMD:REPLY=2\r",           # raises out of the read: ends the thread
        b"",                         # timeout
        b"TC1:TCACTCUR=-0.00\r",    # a late reply, landing after the flush
    ])
    def test_a_poll_keeps_the_last_temperature_and_says_so_once(
            self, reply, caplog):
        """A temperature that has stopped updating looks like a steady one;
        the log is the only place the difference shows."""
        tcm = scripted_tcm({b"TC1:TCACTUALTEMP?\r": reply})
        tcm.actual_temperatures = [24.0]
        seen = []
        tcm.subscribe(seen.append)
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                tcm._poll_once()
        assert seen == [[24.0]] * 3
        assert len([r for r in caplog.records
                    if "TCACTUALTEMP" in r.message]) == 1

    def test_a_run_s_own_read_still_raises(self):
        """Only the display's poll carries on. A run waiting on
        set_temperature must hear that the unit refused, not wait out its
        stabilization timeout on a frozen value."""
        tcm = scripted_tcm({b"TC1:TCACTUALTEMP?\r": b"CMD:REPLY=2\r"})
        with pytest.raises(Exception, match="CMD:REPLY=2"):
            tcm.get_actual_temperature(1)


@pytest.mark.parametrize("read", [
    "get_actual_temperature", "get_output_voltage", "get_output_current"])
def test_a_channel_the_unit_does_not_have_raises_as_the_simulation_does(
        read, caplog):
    """A caller's mistake, not a reply to ride out: keeping the last value,
    or None and a warning, would pass it off as the unit's."""
    tcm = scripted_tcm({}, channels=1)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ValueError, match="channel"):
            getattr(tcm, read)(2)
    assert tcm.serial.written == []
    assert caplog.records == []
    with pytest.raises(ValueError, match="channel"):
        getattr(TCMControllerSimulation(channels=1), read)(2)


class TestStart:
    def test_start_is_idempotent_and_close_joins(self):
        tc = TCMControllerSimulation(channels=1)
        tc.start()
        tc.start()   # a second call must not raise on the running thread
        tc.close()
        assert not tc._polling_thread.is_alive()
