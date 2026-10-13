"""
Modbus client tests
"""

import threading
import time
from typing import List, Optional

from fluidics.control.nimotion.modbus_rtu.client import ModbusClient
from fluidics.control.nimotion.modbus_rtu.crc import append_crc
from fluidics.control.nimotion.serial_comm import ReadTimeoutError


class FakeSlavePort:
    """
    Stands in for SerialPort with a slave that answers FC 0x03 reads.

    Each register reads back as its own address, so a response that ends up
    with the wrong request is detectable.
    """

    port_name = "FAKE"

    def __init__(self) -> None:
        self.is_open = True
        self._rx = bytearray()
        self._buf_lock = threading.Lock()

    def flush_input(self) -> None:
        with self._buf_lock:
            self._rx.clear()

    def write(self, frame: bytes) -> int:
        slave_id, function_code = frame[0], frame[1]
        address = int.from_bytes(frame[2:4], "big")
        quantity = int.from_bytes(frame[4:6], "big")
        body = bytes([slave_id, function_code, 2 * quantity]) + b"".join(
            ((address + i) & 0xFFFF).to_bytes(2, "big") for i in range(quantity)
        )
        time.sleep(0.001)  # bus turnaround; lets another thread run in between
        with self._buf_lock:
            self._rx += append_crc(body)
        return len(frame)

    def read_until(self, expected_length: int, timeout: Optional[float] = None) -> bytes:
        deadline = time.time() + (timeout or 0.1)
        while time.time() < deadline:
            with self._buf_lock:
                if len(self._rx) >= expected_length:
                    data = bytes(self._rx[:expected_length])
                    del self._rx[:expected_length]
                    return data
            time.sleep(0.0005)
        raise ReadTimeoutError(timeout or 0.1, expected_length, 0)

    def close(self) -> None:
        self.is_open = False


class TestModbusClientThreadSafety:
    """Concurrent requests must not see each other's responses"""

    def test_concurrent_reads_get_their_own_response(self) -> None:
        client = ModbusClient(
            serial_port=FakeSlavePort(),  # type: ignore[arg-type]
            timeout=0.05,
            retries=0,
            frame_interval=0.0005,
        )
        errors: List[str] = []

        def worker(base_address: int) -> None:
            for i in range(50):
                address = base_address + i
                try:
                    value = client.read_register(1, address)
                except Exception as e:  # noqa: BLE001 - collect everything for the assert
                    errors.append(f"0x{address:04X}: {e!r}")
                    continue
                if value != address:
                    errors.append(f"0x{address:04X}: got 0x{value:04X}")

        threads = [threading.Thread(target=worker, args=(base,)) for base in (0x0100, 0x0300)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
