import logging
import threading

from ..errors import RunControl
import time

import serial

from .controller import Subscribers
from .discovery import find_serial_port

_logger = logging.getLogger(__name__)


class TCMController:
    """Driver for the TCM temperature controller (1- or 2-channel variant).

    Channels are addressed 1-based (channel=1 → wire module "TC1").
    target_temperatures and actual_temperatures are 0-indexed lists of
    length `channels`.
    """

    def __init__(self, sn, channels=2, tolerance_celsius=1.0,
                 stabilization_timeout_seconds=300, baud_rate=57600, timeout=0.5,
                 run_control=None):
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

        self._subscribers = Subscribers("Temperature controller")
        self._terminate_polling = False
        self._polling_started = False
        self._polling_thread = threading.Thread(
            target=self._update_loop, daemon=True
        )

        self.run_control = run_control if run_control is not None else RunControl()

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
        response = self.send_command("TCACTUALTEMP?", self._module(channel))
        try:
            temp = float(response[17:])
        except ValueError:
            temp = self.actual_temperatures[channel - 1]
        return temp

    # --- background polling and publishing ---

    def subscribe(self, callback):
        """Register callback(temps: list[float], one per channel) -- the
        flow-sensor contract; see start() for who runs the publisher."""
        self._subscribers.subscribe(callback)

    def unsubscribe(self, callback):
        self._subscribers.unsubscribe(callback)

    def start(self):
        """Begin polling and publishing actual temperatures, once a second.

        Consumer-driven, not part of bring-up: the run path reads
        temperatures synchronously (sequence_utils.set_temperature), so a
        headless run never pays the polling serial traffic. The GUI starts
        it for its plots -- the driver still owns the thread; before this
        the GUI assigned a single callback slot and started the driver's
        private thread itself. Safe to call more than once; not restartable
        after close(). The guard is a flag, not is_alive(): under the test
        suite's patched Event.wait, Thread.start() can return before the
        bootstrap marks the thread alive, and a second start() would
        double-start the same Thread object.
        """
        if self._polling_started:
            return
        self._polling_started = True
        self._polling_thread.start()

    def _update_loop(self):
        while not self._terminate_polling:
            time.sleep(1)
            for c in range(1, self.channels + 1):
                # During a set_temperature stabilization the run path polls
                # the same reads synchronously; both interleave safely on
                # _serial_lock, at the cost of doubled wire traffic.
                self.actual_temperatures[c - 1] = self.get_actual_temperature(c)
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

    @property
    def is_aborted(self):
        return self.run_control.cancelled

    def abort(self):
        self.run_control.cancel()

    def reset_abort(self):
        self.run_control.reset()


class TCMControllerSimulation:
    """Simulation counterpart. set_target_temperature immediately updates
    the corresponding actual reading, so the stabilization loop terminates
    on the first poll.
    """

    def __init__(self, sn=None, channels=2, tolerance_celsius=1.0,
                 stabilization_timeout_seconds=300, baud_rate=57600, timeout=0.5,
                 run_control=None):
        if channels not in (1, 2):
            raise ValueError(f"channels must be 1 or 2, got {channels}")

        self.channels = channels
        self.tolerance_celsius = tolerance_celsius
        self.stabilization_timeout_seconds = stabilization_timeout_seconds

        self.target_temperatures = [10.0] * channels
        self.actual_temperatures = [10.0] * channels
        self.output_enabled = [False] * channels

        self._subscribers = Subscribers("Temperature controller")
        self._terminate_polling = False
        self._polling_started = False
        self._polling_thread = threading.Thread(
            target=self._update_loop, daemon=True
        )

        self.run_control = run_control if run_control is not None else RunControl()

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

    @property
    def is_aborted(self):
        return self.run_control.cancelled

    def abort(self):
        self.run_control.cancel()

    def reset_abort(self):
        self.run_control.reset()
