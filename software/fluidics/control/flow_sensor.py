"""Driver for the Sensirion SLF3X liquid flow sensor on the Teensy's I2C bus.

The firmware streams the reading in every status packet, so this class does
not poll: it subscribes to the controller's packet callback and republishes
scaled values to its own subscribers.
"""
import logging

import threading
import time

from ._def import CMD_SET, COMMAND_STATUS, MCU_CONSTANTS
from ..subscribers import Subscribers

_logger = logging.getLogger(__name__)

# Counts per (uL/min) for the installed part. The firmware sends the sensor's
# raw int16 untouched, so this is the only place the wire value becomes a flow.
#
# SLF3S-1300F: datasheet Table 15 gives 500 counts per ml/min, i.e. 0.5 counts
# per uL/min, so uL/min = raw / 0.5 = raw * 2.
#
# This driver previously assumed the SLF3S-0600F's 10 counts per uL/min. That
# is a factor of 20, and a bench sweep on hardware measured exactly that:
# 19.6x, 19.8x, 19.7x, 20.0x low across 500-4000 uL/min. Applying the correct
# factor puts all four points within 2% of the commanded rate.
#
# If a rig is fitted with a different SLF3x part, this constant and
# SLF3X_SCALE_FACTOR_FLOW in the firmware are what change.
SCALE_FACTOR_COUNTS_PER_UL_MIN = 0.5

# SLF3X::read() pre-fills its output with INT16_MAX and returns early when the
# sensor was never initialized or the I2C read short-reads, so an absent sensor
# streams 32767. The 1300F's output limit is +/-65 ml/min, which the datasheet
# says it reports as raw +/-32500 -- so the sentinel still cannot collide with
# a genuine saturated reading.
INVALID_RAW = 32767

MEDIUM_WATER = MCU_CONSTANTS.MEDIUM_WATER
PERFORM_CRC = True


class FlowSensor:
    """One SLF3X flow sensor.

    index is the sensor's physical position on the board, which is its I2C bus
    (1 = Wire1, 2 = Wire2) and fixes which pair of packet bytes carries it:
    index 1 reads bytes 23-24, index 2 reads bytes 25-26. The firmware applies
    the same mapping, so the two stay in step.

    monitor/ramp_up_seconds/tolerance_fraction are draw-protection policy. The
    driver never consults them -- it carries them because the config declares
    them in this sensor's own block, and because both consumers (the operations
    layer, which reads them per draw, and the GUI, which writes `monitor` at
    runtime) already hold the sensor. A separate registry keyed by name would
    be one more thing to keep in step with no one left to benefit.
    """

    def __init__(self, fluid_controller, index, name,
                 monitor="off", ramp_up_seconds=3.0, tolerance_fraction=0.3):
        self.fc = fluid_controller
        self.index = index
        self.name = name
        self.monitor = monitor
        self.ramp_up_seconds = ramp_up_seconds
        self.tolerance_fraction = tolerance_fraction
        # Derived, not passed: index and slot must always agree, so carrying
        # them as independent arguments only creates a way for them to differ.
        self.packet_slot = index - 1

        self._latest = None
        self._lock = threading.Lock()
        self._subscribers = Subscribers(f"Flow sensor '{name}'")
        self._fault_subscribers = Subscribers(f"Flow sensor '{name}' faults")

        # Stored once so close() can unsubscribe the same object: a bound method
        # is a new object on every attribute access (a.m is a.m is False in
        # CPython), so re-deriving self._on_packet later and matching by
        # identity would never find what was registered here.
        self._packet_handler = self._on_packet
        self.fc.subscribe_packets(self._packet_handler)

    def begin(self):
        """Initialize the sensor on the MCU. Raises if the MCU reports failure.

        On failure the sensor unsubscribes itself before re-raising, so a caller
        that discards a failed sensor cannot leave a dead handler on the packet
        stream. Cleaning up here rather than at each call site means every
        caller gets it without remembering.
        """
        try:
            status = self.fc.send_command_blocking(
                CMD_SET.INITIALIZE_FLOW_SENSOR, self.index, MEDIUM_WATER, PERFORM_CRC)
            # A real FluidController's send_command_blocking always returns an
            # int (wait_for_completion() returns a status or raises
            # TimeoutError), so this branch is unreachable on hardware.
            # FluidControllerSimulation has no MCU to report a status and
            # returns None; treat that as success rather than "unknown" so
            # begin() works against the simulation too.
            if status is not None and status != COMMAND_STATUS.COMPLETED_WITHOUT_ERRORS:
                raise RuntimeError(
                    f"Flow sensor '{self.name}' on index {self.index} failed to "
                    f"initialize (MCU status {status}). Check that the sensor is "
                    f"connected to the matching I2C bus."
                )
        except Exception:
            self.close()
            raise
        _logger.info("Flow sensor %r initialized on I2C index %s.", self.name, self.index)

    def start(self):
        """Begin publishing. Nothing to do: the controller's reader thread
        already drives _on_packet. Exists so callers can start real and
        simulated sensors the same way."""

    @property
    def latest_flow_ul_min(self):
        """Most recent reading in uL/min, or None if invalid or not yet seen."""
        with self._lock:
            return self._latest

    def subscribe(self, callback):
        """Register callback(flow_ul_min: float | None, timestamp: float)."""
        self._subscribers.subscribe(callback)

    def unsubscribe(self, callback):
        self._subscribers.unsubscribe(callback)

    def subscribe_faults(self, callback):
        """Register callback(mode: str, fault: FlowFault, timestamp: float).

        The driver never detects faults itself -- draw protection does, per
        draw, and publishes each trip here so whoever records this sensor can
        file the verdict beside the readings. mode is the policy the draw was
        armed under ("warn" or "stop"); timestamp is the tripping sample's, on
        the same clock as the reading subscription.

        No unsubscribe: the one subscriber (the GUI widget) lives as long as
        the sensor, and close() drops the list. Add it when a caller needs it.
        """
        self._fault_subscribers.subscribe(callback)

    def unsubscribe_faults(self, callback):
        self._fault_subscribers.unsubscribe(callback)

    def notify_fault(self, mode, fault, timestamp):
        self._fault_subscribers.notify(mode, fault, timestamp)

    def close(self):
        """Detach from the packet stream and drop our own subscribers.

        Idempotent: unsubscribe_packets tolerates a handler that is already
        gone, which matters because begin()'s failure path calls close() and
        teardown calls it again.
        """
        self._subscribers.clear()
        self._fault_subscribers.clear()
        self.fc.unsubscribe_packets(self._packet_handler)

    def _on_packet(self, parsed):
        raw = parsed["flowrates_raw"][self.packet_slot]
        flow = None if raw == INVALID_RAW else raw / SCALE_FACTOR_COUNTS_PER_UL_MIN

        with self._lock:
            self._latest = flow

        self._subscribers.notify(flow, time.time())


class FlowSensorSimulation:
    """Simulation counterpart.

    Publishes whatever `simulated_flow_ul_min` is set to, so tests can drive
    steady, low-flow, and dead-sensor streams. Set it to None to simulate the
    invalid-reading case. Like TCMControllerSimulation, the thread is built but
    not started; callers start it explicitly.
    """

    def __init__(self, fluid_controller=None, index=1, name="sim",
                 monitor="off", ramp_up_seconds=3.0, tolerance_fraction=0.3):
        self.fc = fluid_controller
        self.index = index
        self.name = name
        self.monitor = monitor
        self.ramp_up_seconds = ramp_up_seconds
        self.tolerance_fraction = tolerance_fraction
        self.packet_slot = index - 1

        self.simulated_flow_ul_min = 500.0
        self._subscribers = Subscribers(f"Flow sensor '{name}'")
        self._fault_subscribers = Subscribers(f"Flow sensor '{name}' faults")

        self._stop_reading = threading.Event()
        self.reading_thread = threading.Thread(target=self._reading_loop, daemon=True)

        _logger.info("Simulated flow sensor %r on I2C index %s.", name, index)

    def begin(self):
        pass

    def start(self):
        """Start the publish loop.

        Separate from begin() because the suite's fake clock turns this loop
        into a busy spin -- a test that calls begin() must not get a thread it
        did not ask for.
        """
        self.reading_thread.start()

    @property
    def latest_flow_ul_min(self):
        return self.simulated_flow_ul_min

    def subscribe(self, callback):
        self._subscribers.subscribe(callback)

    def unsubscribe(self, callback):
        self._subscribers.unsubscribe(callback)

    def subscribe_faults(self, callback):
        self._fault_subscribers.subscribe(callback)

    def unsubscribe_faults(self, callback):
        self._fault_subscribers.unsubscribe(callback)

    def notify_fault(self, mode, fault, timestamp):
        self._fault_subscribers.notify(mode, fault, timestamp)

    def close(self):
        self._stop_reading.set()
        if self.reading_thread.is_alive():
            self.reading_thread.join(timeout=2)
        self._subscribers.clear()
        self._fault_subscribers.clear()

    def _reading_loop(self):
        # An Event, not a sleep, so close() wakes the loop at once instead of
        # waiting out the tick -- a simulated rig is built and torn down per
        # test and per time estimate.
        while not self._stop_reading.wait(0.06):
            self._subscribers.notify(self.simulated_flow_ul_min, time.time())


def build_flow_sensors(fluid_controller, config, simulation=False):
    """Construct FlowSensor instances from config. Does not call begin().

    Slot is `index - 1`, matching the firmware: index is the physical board
    position, which is the I2C bus, which fixes the packet slot. Index 1 reads
    bytes 23-24, index 2 reads bytes 25-26.

    Deliberately derived from index rather than from position in the config
    list, so reordering the YAML cannot silently repoint a sensor at a
    different slot. A lone sensor declared at index 2 therefore reads slot 1
    while slot 0 carries the no-sensor sentinel -- a normal configuration, not
    an error.
    """
    if not config.flow_sensors:
        return []

    cls = FlowSensorSimulation if simulation else FlowSensor
    return [
        cls(fluid_controller, index=cfg.index, name=cfg.name,
            monitor=cfg.monitor, ramp_up_seconds=cfg.ramp_up_seconds,
            tolerance_fraction=cfg.tolerance_fraction)
        for cfg in config.flow_sensors
    ]


def start_flow_sensors(fluid_controller, config, simulation=False):
    """Construct, initialize and start every configured sensor.

    All or nothing. A sensor claims a slot on the controller's packet stream in
    its constructor, and only close() gives it back; if the second sensor fails
    to initialize, dropping the list leaves the first one subscribed for the
    life of the process with nothing holding a reference to unsubscribe it.

    Everything built is closed on the way out, including the sensor that
    failed. begin() does clean up after itself, but making that the only
    cleanup couples this to which line inside it raised -- and start() can
    raise too, at which point the sensor is fully live. close() is idempotent,
    so closing twice costs nothing and closing something never begun is fine.

    Both entry points call this so neither has to remember either half.
    """
    sensors = build_flow_sensors(fluid_controller, config, simulation)
    try:
        for sensor in sensors:
            sensor.begin()
            sensor.start()
    except Exception:
        for sensor in sensors:
            sensor.close()
        raise
    return sensors
