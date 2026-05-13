import threading
import time

import serial
from serial.tools import list_ports


class TCMController:
    """Driver for the TCM temperature controller (1- or 2-channel variant).

    Channels are addressed 1-based (channel=1 → wire module "TC1").
    target_temperatures and actual_temperatures are 0-indexed lists of
    length `channels`.
    """

    def __init__(self, sn, channels=2, tolerance_celsius=1.0,
                 stabilization_timeout_seconds=300, baud_rate=57600, timeout=0.5):
        if channels not in (1, 2):
            raise ValueError(f"channels must be 1 or 2, got {channels}")

        port = [p.device for p in list_ports.comports() if sn == p.serial_number]
        if not port:
            raise ValueError(f"No device found with serial number: {sn}")

        self.serial = serial.Serial(port[0], baudrate=baud_rate, timeout=timeout)
        self.serial_lock = threading.Lock()

        self.channels = channels
        self.tolerance_celsius = tolerance_celsius
        self.stabilization_timeout_seconds = stabilization_timeout_seconds

        self.target_temperatures = [self._read_target(c) for c in range(1, channels + 1)]
        self.actual_temperatures = [0.0] * channels
        self.output_enabled = [self._read_output_enabled(c) for c in range(1, channels + 1)]

        self.temperature_updating_callback = None
        self.terminate_temperature_updating_thread = False
        self.actual_temp_updating_thread = threading.Thread(
            target=self._update_loop, daemon=True
        )

        self.is_aborted = False

        print(
            f"Temperature controller initialized: serial_number={sn}, "
            f"channels={channels}, port={port[0]}"
        )

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
        with self.serial_lock:
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
        print("Save target temperature: ", response)

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

    # --- background polling ---

    def _update_loop(self):
        while not self.terminate_temperature_updating_thread:
            time.sleep(1)
            for c in range(1, self.channels + 1):
                self.actual_temperatures[c - 1] = self.get_actual_temperature(c)
            if self.temperature_updating_callback is not None:
                try:
                    self.temperature_updating_callback(list(self.actual_temperatures))
                except TypeError:
                    print("Temperature read callback failed")

    # --- lifecycle ---

    def close(self):
        self.terminate_temperature_updating_thread = True
        if self.actual_temp_updating_thread.is_alive():
            self.actual_temp_updating_thread.join()
        if self.serial.is_open:
            self.serial.close()

    def abort(self):
        self.is_aborted = True

    def reset_abort(self):
        self.is_aborted = False


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

        self.temperature_updating_callback = None
        self.terminate_temperature_updating_thread = False
        self.actual_temp_updating_thread = threading.Thread(
            target=self._update_loop, daemon=True
        )

        self.is_aborted = False

        print(f"Temperature controller (simulation) initialized: channels={channels}")

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

    def _update_loop(self):
        while not self.terminate_temperature_updating_thread:
            time.sleep(1)
            if self.temperature_updating_callback is not None:
                try:
                    self.temperature_updating_callback(list(self.actual_temperatures))
                except TypeError:
                    print("Temperature read callback failed")

    def close(self):
        self.terminate_temperature_updating_thread = True
        if self.actual_temp_updating_thread.is_alive():
            self.actual_temp_updating_thread.join()

    def abort(self):
        self.is_aborted = True

    def reset_abort(self):
        self.is_aborted = False
