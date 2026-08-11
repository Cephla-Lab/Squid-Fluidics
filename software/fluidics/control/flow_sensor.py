"""Driver for the Sensirion SLF3X liquid flow sensor on the Teensy's I2C bus.

The firmware streams the reading in every status packet, so this class does
not poll: it subscribes to the controller's packet callback and republishes
scaled values to its own subscribers.
"""

import threading
import time

from ._def import CMD_SET, COMMAND_STATUS, MCU_CONSTANTS

# SLF3X::read() pre-fills its output with INT16_MAX and returns early when the
# sensor was never initialized or the I2C read short-reads. An absent sensor
# therefore streams 32767, which scales to a plausible-looking 3276.7 uL/min.
# The real output limit is +/-3250 uL/min (raw +/-32500), so the sentinel never
# collides with a genuine saturated reading.
INVALID_RAW = 32767

MEDIUM_WATER = MCU_CONSTANTS.MEDIUM_WATER
PERFORM_CRC = True


class FlowSensor:
    """One SLF3X flow sensor.

    index is the I2C bus (1 = Wire1, 2 = Wire2). packet_slot is which pair of
    flow bytes in the status packet carries this sensor: slot 0 is bytes 23-24,
    slot 1 is bytes 25-26. They differ because the current firmware has a single
    sensor object and always transmits it in slot 0, whichever bus it sits on.
    """

    def __init__(self, fluid_controller, index, name, packet_slot=0):
        self.fc = fluid_controller
        self.index = index
        self.name = name
        self.packet_slot = packet_slot

        self._latest = None
        self._lock = threading.Lock()
        self._subscribers = []

        # Stored once so close() can find it again: a bound method is a new
        # object on every attribute access (a.m is a.m is False in CPython),
        # so re-deriving self._on_packet in close() and comparing with `is`
        # would never match what was assigned here.
        self._packet_handler = self._on_packet
        self.fc.packet_callback = self._packet_handler

    def begin(self):
        """Initialize the sensor on the MCU. Raises if the MCU reports failure."""
        status = self.fc.send_command_blocking(
            CMD_SET.INITIALIZE_FLOW_SENSOR, self.index, MEDIUM_WATER, PERFORM_CRC)
        # A real FluidController's send_command_blocking always returns an int
        # (wait_for_completion() returns a status or raises TimeoutError), so
        # this branch is unreachable on hardware. FluidControllerSimulation has
        # no MCU to report a status and returns None; treat that as success
        # rather than "unknown" so begin() works against the simulation too.
        if status is not None and status != COMMAND_STATUS.COMPLETED_WITHOUT_ERRORS:
            raise RuntimeError(
                f"Flow sensor '{self.name}' on index {self.index} failed to "
                f"initialize (MCU status {status}). Check that the sensor is "
                f"connected to the matching I2C bus."
            )
        print(f"Flow sensor '{self.name}' initialized on I2C index {self.index}.")

    @property
    def latest_flow_ul_min(self):
        """Most recent reading in uL/min, or None if invalid or not yet seen."""
        with self._lock:
            return self._latest

    def subscribe(self, callback):
        """Register callback(flow_ul_min: float | None, timestamp: float)."""
        self._subscribers.append(callback)

    def close(self):
        self._subscribers = []
        if getattr(self.fc, "packet_callback", None) is self._packet_handler:
            self.fc.packet_callback = None

    def _on_packet(self, parsed):
        raw = parsed["flowrates_raw"][self.packet_slot]
        flow = None if raw == INVALID_RAW else parsed["flowrates"][self.packet_slot]

        with self._lock:
            self._latest = flow

        timestamp = time.time()
        for callback in list(self._subscribers):
            try:
                callback(flow, timestamp)
            except Exception as e:
                print(f"Flow sensor subscriber failed: {e}")


class FlowSensorSimulation:
    """Simulation counterpart.

    Publishes whatever `simulated_flow_ul_min` is set to, so tests can drive
    steady, low-flow, and dead-sensor streams. Set it to None to simulate the
    invalid-reading case. Like TCMControllerSimulation, the thread is built but
    not started; callers start it explicitly.
    """

    def __init__(self, fluid_controller=None, index=1, name="sim", packet_slot=0):
        self.fc = fluid_controller
        self.index = index
        self.name = name
        self.packet_slot = packet_slot

        self.simulated_flow_ul_min = 500.0
        self._subscribers = []

        self.terminate_reading_thread = False
        self.reading_thread = threading.Thread(target=self._reading_loop, daemon=True)

        print(f"Simulated flow sensor '{name}' on I2C index {index}.")

    def begin(self):
        pass

    @property
    def latest_flow_ul_min(self):
        return self.simulated_flow_ul_min

    def subscribe(self, callback):
        self._subscribers.append(callback)

    def close(self):
        self.terminate_reading_thread = True
        if self.reading_thread.is_alive():
            self.reading_thread.join(timeout=2)
        self._subscribers = []

    def _reading_loop(self):
        while not self.terminate_reading_thread:
            time.sleep(0.06)
            timestamp = time.time()
            for callback in list(self._subscribers):
                try:
                    callback(self.simulated_flow_ul_min, timestamp)
                except Exception as e:
                    print(f"Flow sensor subscriber failed: {e}")
