"""
Simulated RS-485 bus

SimulatedBus holds one SimulatedDrive per slave address and answers Modbus RTU
frames (FC 03, 04, 06, 10) with real CRCs, so everything above the serial port
runs unchanged. SimulatedSerial stands in for pyserial's Serial object inside
SerialPort when the port name is a simulator URL ("sim://").

One bus exists per URL and lives for the process, so disconnecting and
reconnecting finds the axes where they were.
"""

import struct
import threading
import time
from typing import Dict, Iterable, Optional

from ..high_level_api.axis_config import DEFAULT_AXIS_CONFIGS, AxisConfig
from ..modbus_rtu.crc import append_crc, verify_crc
from .drive import ILLEGAL_DATA_ADDRESS, ILLEGAL_DATA_VALUE, SimulatedDrive, SimulatedModbusError

SIMULATOR_PORT = "sim://"

ILLEGAL_FUNCTION = 0x01


def is_simulator_port(port: Optional[str]) -> bool:
    """Whether a port name selects the simulator instead of a serial port"""
    return bool(port) and str(port).lower().startswith(SIMULATOR_PORT)


class SimulatedBus:
    """Simulated drives on one bus. Thread-safe."""

    def __init__(
        self,
        axis_configs: Optional[Iterable[AxisConfig]] = None,
        time_scale: float = 1.0,
    ) -> None:
        """
        Args:
            axis_configs: axes on the bus (default: DEFAULT_AXIS_CONFIGS)
            time_scale: simulated seconds per real second (tests run faster)
        """
        self._lock = threading.RLock()
        self._time_scale = time_scale
        self._wall_start = time.monotonic()
        self._drives: Dict[int, SimulatedDrive] = {}
        self.ensure_axes(axis_configs or DEFAULT_AXIS_CONFIGS.values())

    def now(self) -> float:
        """Simulated time (s)"""
        return (time.monotonic() - self._wall_start) * self._time_scale

    def ensure_axes(self, axis_configs: Iterable[AxisConfig]) -> None:
        """Add a drive for every axis whose slave address has none yet"""
        with self._lock:
            for config in axis_configs:
                if config.slave_id not in self._drives:
                    self._drives[config.slave_id] = SimulatedDrive(config, now=self.now())

    def drive(self, slave_id: int) -> SimulatedDrive:
        """The drive at a slave address, advanced to now"""
        with self._lock:
            drive = self._drives[slave_id]
            drive.advance(self.now())
            return drive

    @property
    def slave_ids(self) -> list:
        return sorted(self._drives)

    def inject_fault(self, slave_id: int, code: int) -> None:
        with self._lock:
            self.drive(slave_id).inject_fault(code)

    def set_online(self, slave_id: int, online: bool) -> None:
        """An offline drive ignores every frame (as if unplugged)"""
        with self._lock:
            self._drives[slave_id].online = online

    def handle_frame(self, frame: bytes) -> bytes:
        """The response to one request frame (b"" when no drive answers)"""
        if len(frame) < 4 or not verify_crc(frame):
            return b""  # drives ignore frames with a bad CRC
        slave_id = frame[0]
        with self._lock:
            now = self.now()
            for each in self._drives.values():
                each.advance(now)
            if slave_id == 0:  # broadcast: every drive acts, none answers
                for each in self._drives.values():
                    if each.online:
                        self._execute(each, frame)
                return b""
            drive = self._drives.get(slave_id)
            if drive is None or not drive.online:
                return b""
            return append_crc(self._execute(drive, frame))

    @staticmethod
    def _execute(drive: SimulatedDrive, frame: bytes) -> bytes:
        """Response body (without CRC) for a frame addressed to `drive`"""
        slave_id, function = frame[0], frame[1]
        body = frame[2:-2]
        try:
            if function in (0x03, 0x04):
                if len(body) != 4:
                    raise SimulatedModbusError(ILLEGAL_DATA_VALUE)
                address, count = struct.unpack(">HH", body)
                if not 1 <= count <= 125:
                    raise SimulatedModbusError(ILLEGAL_DATA_VALUE)
                if address + count > 0x10000:
                    raise SimulatedModbusError(ILLEGAL_DATA_ADDRESS)
                values = drive.read(address, count)
                return bytes([slave_id, function, 2 * count]) + struct.pack(f">{count}H", *values)
            if function == 0x06:
                if len(body) != 4:
                    raise SimulatedModbusError(ILLEGAL_DATA_VALUE)
                address, value = struct.unpack(">HH", body)
                drive.write(address, [value])
                return frame[:6]
            if function == 0x10:
                if len(body) < 5:
                    raise SimulatedModbusError(ILLEGAL_DATA_VALUE)
                address, count, byte_count = struct.unpack(">HHB", body[:5])
                data = body[5:]
                if not 1 <= count <= 123 or byte_count != 2 * count or len(data) != byte_count:
                    raise SimulatedModbusError(ILLEGAL_DATA_VALUE)
                drive.write(address, list(struct.unpack(f">{count}H", data)))
                return frame[:6]
            raise SimulatedModbusError(ILLEGAL_FUNCTION)
        except SimulatedModbusError as e:
            return bytes([slave_id, function | 0x80, e.code])


_buses: Dict[str, SimulatedBus] = {}
_buses_lock = threading.Lock()


def get_bus(port: str = SIMULATOR_PORT) -> SimulatedBus:
    """The bus for a simulator URL, created with the default axes on first use"""
    key = port.lower()
    with _buses_lock:
        if key not in _buses:
            _buses[key] = SimulatedBus()
        return _buses[key]


def install_bus(port: str, bus: SimulatedBus) -> SimulatedBus:
    """Use `bus` for a simulator URL (replacing any existing one)"""
    if not is_simulator_port(port):
        raise ValueError(f"Not a simulator port: {port!r}")
    with _buses_lock:
        _buses[port.lower()] = bus
    return bus


def reset_buses() -> None:
    """Forget every bus: the next connection finds factory-fresh drives"""
    with _buses_lock:
        _buses.clear()


class SimulatedSerial:
    """
    The part of pyserial's Serial interface that SerialPort uses, connected
    to a SimulatedBus instead of a COM port.
    """

    def __init__(self, port: str, timeout: Optional[float] = None, **settings: object) -> None:
        self.port = port
        self.timeout = timeout
        self.write_timeout = settings.pop("write_timeout", None)
        self.baudrate = settings.pop("baudrate", 115200)
        self.bytesize = settings.pop("bytesize", 8)
        self.parity = settings.pop("parity", "N")
        self.stopbits = settings.pop("stopbits", 1)
        self._bus = get_bus(port)
        self._rx = bytearray()
        self._lock = threading.Lock()
        self.is_open = True

    @property
    def bus(self) -> SimulatedBus:
        return self._bus

    def write(self, data: bytes) -> int:
        response = self._bus.handle_frame(bytes(data))
        with self._lock:
            self._rx += response
        return len(data)

    def flush(self) -> None:
        pass

    def read(self, size: int = 1) -> bytes:
        with self._lock:
            if self._rx:
                chunk = bytes(self._rx[:size])
                del self._rx[:size]
                return chunk
        # Nothing to read: a silent drive costs the full timeout, as on the wire
        time.sleep(self.timeout or 0)
        return b""

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._rx)

    @property
    def out_waiting(self) -> int:
        return 0

    def reset_input_buffer(self) -> None:
        with self._lock:
            self._rx.clear()

    def reset_output_buffer(self) -> None:
        pass

    def send_break(self, duration: float = 0.25) -> None:
        pass

    def close(self) -> None:
        self.is_open = False
