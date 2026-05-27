import logging
import time
from enum import IntEnum

from .modbus_rtu import ModbusRTUClient

logger = logging.getLogger(__name__)


# --- Register Map (OEM-AMCB209) ---

class Reg:
    STATUS = 0x0007
    CURRENT_SPEED = 0x000C
    JOG_SPEED = 0x001D
    JOG_ACCEL = 0x001E
    JOG_DECEL = 0x001F
    MOTION_CMD = 0x0027
    AUX_CMD = 0x002D


class MotionCmd:
    SPEED_START = 0x0002
    STOP = 0x0100


class AuxCmd:
    ENABLE = 0x0012


class StatusBits:
    RUNNING = 0x04
    ALARM = 0x08


# --- Direction ---

class Direction(IntEnum):
    CLOCKWISE = 1
    COUNTER_CLOCKWISE = -1


# --- Constants ---

MAX_SPEED_RPM = 150.0
MIN_SPEED_RPM = 0.1
MAX_ACCEL_MS = 2000
ABORT_POLL_INTERVAL = 0.1


# --- PeristalticPump ---

class PeristalticPump:
    def __init__(
        self,
        client: ModbusRTUClient,
        slave_id: int,
        direction: Direction = Direction.CLOCKWISE,
        max_speed: float = MAX_SPEED_RPM,
        default_acceleration_ms: int = 200,
        default_deceleration_ms: int = 200,
    ):
        self._client = client
        self._slave_id = slave_id
        self._direction = direction
        self._max_speed = max_speed
        self._default_accel_ms = default_acceleration_ms
        self._default_decel_ms = default_deceleration_ms
        self._speed_rpm: float = 0.0
        self._running = False
        self.is_aborted = False

    # --- Speed / Acceleration ---

    def set_speed(self, speed_rpm: float) -> None:
        if speed_rpm <= 0:
            raise ValueError("Speed must be positive")

        if speed_rpm > self._max_speed:
            logger.warning(
                "Speed %.1f RPM exceeds max %.1f RPM, clamping",
                speed_rpm, self._max_speed,
            )
            speed_rpm = self._max_speed

        self._speed_rpm = speed_rpm
        raw = int(speed_rpm * 10 * self._direction)
        if raw < 0:
            raw = raw & 0xFFFF
        self._client.write_register(self._slave_id, Reg.JOG_SPEED, raw)

    def set_acceleration(self, accel_ms: int, decel_ms: int) -> None:
        accel_ms = max(0, min(MAX_ACCEL_MS, accel_ms))
        decel_ms = max(0, min(MAX_ACCEL_MS, decel_ms))
        self._client.write_register(self._slave_id, Reg.JOG_ACCEL, accel_ms)
        self._client.write_register(self._slave_id, Reg.JOG_DECEL, decel_ms)

    # --- Start / Stop ---

    def start(self) -> None:
        if self._speed_rpm == 0.0:
            raise RuntimeError("Speed not set — call set_speed() before start()")
        self._client.write_register(self._slave_id, Reg.MOTION_CMD, MotionCmd.SPEED_START)
        self._running = True

    def stop(self) -> None:
        self._client.write_register(self._slave_id, Reg.MOTION_CMD, MotionCmd.STOP)
        self._running = False

    # --- Blocking convenience ---

    def run_for_duration(self, speed_rpm: float, duration_s: float) -> None:
        self.set_speed(speed_rpm)
        self.set_acceleration(self._default_accel_ms, self._default_decel_ms)
        self.start()
        try:
            elapsed = 0.0
            while elapsed < duration_s:
                if self.is_aborted:
                    raise RuntimeError("Operation aborted")
                sleep_time = min(ABORT_POLL_INTERVAL, duration_s - elapsed)
                time.sleep(sleep_time)
                elapsed += sleep_time
        finally:
            self.stop()

    # --- State ---

    @property
    def is_running(self) -> bool:
        status = self._client.read_register(self._slave_id, Reg.STATUS)
        return bool(status & StatusBits.RUNNING)

    # --- Abort ---

    def abort(self) -> None:
        self.is_aborted = True
        try:
            self.stop()
        except Exception:
            logger.error("Failed to stop pump %d during abort", self._slave_id, exc_info=True)

    def reset_abort(self) -> None:
        self.is_aborted = False


# --- Simulation ---

class PeristalticPumpSimulation:
    def __init__(
        self,
        slave_id: int = 1,
        direction: Direction = Direction.CLOCKWISE,
        max_speed: float = MAX_SPEED_RPM,
        default_acceleration_ms: int = 200,
        default_deceleration_ms: int = 200,
    ):
        self._slave_id = slave_id
        self._direction = direction
        self._max_speed = max_speed
        self._default_accel_ms = default_acceleration_ms
        self._default_decel_ms = default_deceleration_ms
        self._speed_rpm: float = 0.0
        self._running = False
        self.is_aborted = False

    # --- Speed / Acceleration ---

    def set_speed(self, speed_rpm: float) -> None:
        if speed_rpm <= 0:
            raise ValueError("Speed must be positive")

        if speed_rpm > self._max_speed:
            logger.warning(
                "Speed %.1f RPM exceeds max %.1f RPM, clamping",
                speed_rpm, self._max_speed,
            )
            speed_rpm = self._max_speed

        self._speed_rpm = speed_rpm

    def set_acceleration(self, accel_ms: int, decel_ms: int) -> None:
        pass

    # --- Start / Stop ---

    def start(self) -> None:
        if self._speed_rpm == 0.0:
            raise RuntimeError("Speed not set — call set_speed() before start()")
        self._running = True

    def stop(self) -> None:
        self._running = False

    # --- Blocking convenience ---

    def run_for_duration(self, speed_rpm: float, duration_s: float) -> None:
        self.set_speed(speed_rpm)
        self.start()
        try:
            elapsed = 0.0
            while elapsed < duration_s:
                if self.is_aborted:
                    raise RuntimeError("Operation aborted")
                sleep_time = min(ABORT_POLL_INTERVAL, duration_s - elapsed)
                time.sleep(sleep_time)
                elapsed += sleep_time
        finally:
            self.stop()

    # --- State ---

    @property
    def is_running(self) -> bool:
        return self._running

    # --- Abort ---

    def abort(self) -> None:
        self.is_aborted = True
        self._running = False

    def reset_abort(self) -> None:
        self.is_aborted = False
