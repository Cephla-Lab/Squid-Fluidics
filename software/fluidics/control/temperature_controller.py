import logging
import threading
import time

import serial

from ..subscribers import Subscribers
from .discovery import find_serial_port

_logger = logging.getLogger(__name__)


class TCMController:
    """Driver for the TCM temperature controller (1- or 2-channel variant).

    Channels are addressed 1-based (channel=1 → wire module "TC1").
    target_temperatures, actual_temperatures, output_voltages and
    output_currents are 0-indexed lists of length `channels`.
    """

    def __init__(self, sn, channels=2, tolerance_celsius=1.0,
                 stabilization_timeout_seconds=300, baud_rate=57600, timeout=0.5):
        if channels not in (1, 2):
            raise ValueError(f"channels must be 1 or 2, got {channels}")

        port = find_serial_port(sn, "Temperature controller")
        self.serial = serial.Serial(port, baudrate=baud_rate, timeout=timeout)
        self._serial_lock = threading.Lock()

        self.channels = channels
        self.tolerance_celsius = tolerance_celsius
        self.stabilization_timeout_seconds = stabilization_timeout_seconds

        self.target_temperatures = [self._read_target(c) for c in range(1, channels + 1)]
        self.actual_temperatures = [0.0] * channels
        self.output_enabled = [self._read_output_enabled(c) for c in range(1, channels + 1)]
        # What the TEC is actually driving, per channel: None until the poll
        # loop has read it, and whenever the unit will not answer.
        self.output_voltages = [None] * channels
        self.output_currents = [None] * channels
        self._reads_failed = set()

        self._subscribers = Subscribers("Temperature controller")
        self._terminate_polling = False
        self._polling_started = False
        self._polling_thread = threading.Thread(
            target=self._update_loop, daemon=True
        )


        _logger.info("Temperature controller initialized: serial_number=%s, "
                     "channels=%s, port=%s", sn, channels, port)

    # --- channel addressing helpers ---

    def _check_channel(self, channel):
        if not (1 <= channel <= self.channels):
            raise ValueError(
                f"channel must be in [1, {self.channels}], got {channel}"
            )

    def _module(self, channel):
        self._check_channel(channel)
        return f"TC{channel}"

    # --- wire protocol ---

    def send_command(self, command, module):
        with self._serial_lock:
            # Whatever is waiting answers a command that already timed out.
            # Left there it is read as this command's reply, and every reply
            # after it lands one command late.
            self.serial.reset_input_buffer()
            self.serial.write(f"{module}:{command}\r".encode())
            response = self.serial.readline().decode().strip()
            if response[:4] == "CMD:" and response[-1] != "1" and response[-1] != "8":
                raise Exception(f"Error from controller: {response}")
            return response

    def _read_target(self, channel):
        response = self.send_command("TCADJTEMP?", self._module(channel))
        return float(response[14:])

    def _read_output_enabled(self, channel):
        response = self.send_command("TCSW?", self._module(channel))
        return response.rsplit("=", 1)[-1].strip() == "1"

    def _query(self, param, channel):
        """The value text of the reply to `param`?, ValueError unless the
        reply is for that parameter.

        send_command drops what was waiting before it writes, but a reply
        that lands after that is still read by the next command. Sliced by
        position, a current is a plausible temperature -- "TC1:TCACTCUR=-0.00"
        reads as 0.0 -- so the reply says what it answers before it is
        believed.
        """
        module = self._module(channel)
        response = self.send_command(f"{param}?", module)
        if not response:
            raise ValueError("no reply (timeout)")
        head, _, value = response.rpartition("=")
        if head != f"{module}:{param}":
            raise ValueError(f"unexpected reply {response!r}")
        return value

    def _poll_failed(self, param, channel, error):
        # Warns when a polled read starts failing, not once a second.
        if (param, channel) not in self._reads_failed:
            self._reads_failed.add((param, channel))
            _logger.warning("Temperature controller channel %s: cannot "
                            "read %s: %s", channel, param, error)

    def _read_output(self, param, channel):
        """One of the TEC's output readings, or None if the unit would not
        give it.

        These are for the tab's readout, polled beside the temperatures, so
        a failure here must never raise into the poll loop: a firmware that
        lacks the parameter answers CMD:REPLY=2, and that would take the
        temperature plot down with it.
        """
        # Outside the try, as in get_actual_temperature: None means the unit
        # would not answer, and a wrong channel is not that.
        self._check_channel(channel)
        try:
            reading = float(self._query(param, channel))
        except Exception as e:
            self._poll_failed(param, channel, e)
            return None
        self._reads_failed.discard((param, channel))
        return reading

    # --- public API ---

    def get_target_temperature(self, channel):
        temp = self._read_target(channel)
        self.target_temperatures[channel - 1] = temp
        return temp

    def set_target_temperature(self, channel, t):
        self.send_command(f"TCADJTEMP={t}", self._module(channel))
        self.target_temperatures[channel - 1] = t

    def save_target_temperature(self, channel):
        response = self.send_command("TCADJTEMP!", self._module(channel))
        _logger.info("Save target temperature: %s", response)

    def get_output_enabled(self, channel):
        enabled = self._read_output_enabled(channel)
        self.output_enabled[channel - 1] = enabled
        return enabled

    def set_output_enabled(self, channel, on):
        self.send_command(f"TCSW={1 if on else 0}", self._module(channel))
        self.output_enabled[channel - 1] = bool(on)

    def get_actual_temperature(self, channel):
        # Outside the try: a wrong channel is the caller's mistake, not a bad
        # reply to ride out on the last value.
        self._check_channel(channel)
        try:
            temp = float(self._query("TCACTUALTEMP", channel))
        except ValueError:
            temp = self.actual_temperatures[channel - 1]
        return temp

    def get_output_voltage(self, channel):
        """The TEC's actual output voltage in volts, or None (see
        _read_output)."""
        return self._read_output("TCACTVOL", channel)

    def get_output_current(self, channel):
        """The TEC's actual output current in amps, signed -- cooling drives
        it the other way -- or None (see _read_output)."""
        return self._read_output("TCACTCUR", channel)

    # --- background polling and publishing ---

    def subscribe(self, callback):
        """Register callback(temps: list[float], one per channel) -- the
        flow-sensor contract; see start() for who runs the publisher."""
        self._subscribers.subscribe(callback)

    def unsubscribe(self, callback):
        self._subscribers.unsubscribe(callback)

    def start(self):
        """Begin polling and publishing actual temperatures, once a second;
        each poll also refreshes output_voltages and output_currents.

        Consumer-driven, not part of bring-up: the run path reads
        temperatures synchronously (sequence_utils.set_temperature), so a
        headless run never pays the polling serial traffic. The GUI starts
        it for its plots -- the driver still owns the thread; before this
        the GUI assigned a single callback slot and started the driver's
        private thread itself. Safe to call more than once; not restartable
        after close(). The guard is a flag so a second start() cannot
        double-start the same Thread object.
        """
        if self._polling_started:
            return
        self._polling_started = True
        self._polling_thread.start()

    def _update_loop(self):
        while not self._terminate_polling:
            time.sleep(1)
            self._poll_once()

    def _poll_once(self):
        for c in range(1, self.channels + 1):
            # During a set_temperature stabilization the run path polls
            # the same reads synchronously; both interleave safely on
            # _serial_lock, at the cost of doubled wire traffic.
            try:
                self.actual_temperatures[c - 1] = self.get_actual_temperature(c)
                self._reads_failed.discard(("TCACTUALTEMP", c))
            except Exception as e:
                # The last temperature stands. This loop feeds the display,
                # and raising here ends its thread; a run's own read of the
                # same parameter (sequence_utils.set_temperature) still
                # raises, so a unit that refuses fails the run rather than
                # holding it on a frozen value.
                self._poll_failed("TCACTUALTEMP", c, e)
            # Not part of the published payload, which stays the
            # temperatures: the tab reads these off the driver when a
            # publish arrives, as it does output_enabled.
            self.output_voltages[c - 1] = self.get_output_voltage(c)
            self.output_currents[c - 1] = self.get_output_current(c)
        self._publish()

    def _publish(self):
        # Also the seam tests drive, so nothing there needs the thread.
        self._subscribers.notify(list(self.actual_temperatures))

    # --- lifecycle ---

    def close(self):
        self._terminate_polling = True
        if self._polling_thread.is_alive():
            self._polling_thread.join()
        self._subscribers.clear()
        if self.serial.is_open:
            self.serial.close()



class TCMControllerSimulation:
    """Simulation counterpart. set_target_temperature immediately updates
    the corresponding actual reading, so the stabilization loop terminates
    on the first poll.
    """

    def __init__(self, sn=None, channels=2, tolerance_celsius=1.0,
                 stabilization_timeout_seconds=300, baud_rate=57600, timeout=0.5):
        if channels not in (1, 2):
            raise ValueError(f"channels must be 1 or 2, got {channels}")

        self.channels = channels
        self.tolerance_celsius = tolerance_celsius
        self.stabilization_timeout_seconds = stabilization_timeout_seconds

        self.target_temperatures = [10.0] * channels
        self.actual_temperatures = [10.0] * channels
        self.output_enabled = [False] * channels
        # Actual always sits on target here, so the TEC never has to drive.
        self.output_voltages = [0.0] * channels
        self.output_currents = [0.0] * channels

        self._subscribers = Subscribers("Temperature controller")
        self._terminate_polling = False
        self._polling_started = False
        self._polling_thread = threading.Thread(
            target=self._update_loop, daemon=True
        )


        _logger.info("Temperature controller (simulation) initialized: channels=%s", channels)

    def _check_channel(self, channel):
        if not (1 <= channel <= self.channels):
            raise ValueError(
                f"channel must be in [1, {self.channels}], got {channel}"
            )

    def send_command(self, command, module):
        pass

    def get_target_temperature(self, channel):
        self._check_channel(channel)
        return self.target_temperatures[channel - 1]

    def set_target_temperature(self, channel, t):
        self._check_channel(channel)
        self.target_temperatures[channel - 1] = t
        self.actual_temperatures[channel - 1] = t

    def save_target_temperature(self, channel):
        self._check_channel(channel)

    def get_output_enabled(self, channel):
        self._check_channel(channel)
        return self.output_enabled[channel - 1]

    def set_output_enabled(self, channel, on):
        self._check_channel(channel)
        self.output_enabled[channel - 1] = bool(on)

    def get_actual_temperature(self, channel):
        self._check_channel(channel)
        return self.actual_temperatures[channel - 1]

    def get_output_voltage(self, channel):
        self._check_channel(channel)
        return self.output_voltages[channel - 1]

    def get_output_current(self, channel):
        self._check_channel(channel)
        return self.output_currents[channel - 1]

    def subscribe(self, callback):
        self._subscribers.subscribe(callback)

    def unsubscribe(self, callback):
        self._subscribers.unsubscribe(callback)

    def start(self):
        if self._polling_started:
            return
        self._polling_started = True
        self._polling_thread.start()

    def _update_loop(self):
        while not self._terminate_polling:
            time.sleep(1)
            self._publish()

    def _publish(self):
        # Also the seam tests drive, so nothing there needs the thread.
        self._subscribers.notify(list(self.actual_temperatures))

    def close(self):
        self._terminate_polling = True
        if self._polling_thread.is_alive():
            self._polling_thread.join()
        self._subscribers.clear()

