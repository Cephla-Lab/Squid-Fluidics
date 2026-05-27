# Peristaltic Pump Driver Design

## Overview

A Modbus RTU driver for OEM-AMCB209 peristaltic pumps used in the fluidics v2 system. Two pumps share a single RS485 bus (one for aspirate, one for dispense), differentiated by Modbus slave address and direction sign.

## Architecture

**File:** `fluidics/control/peristaltic_pump.py`

Reuses the existing `ModbusRTUClient` from `modbus_rtu.py`. The caller creates one client, connects it, and passes it to each `PeristalticPump` instance.

```python
client = ModbusRTUClient(port="/dev/ttyUSB0", baudrate=115200)
client.connect()

pump_dispense = PeristalticPump(client, slave_id=1, direction=Direction.CLOCKWISE)
pump_aspirate = PeristalticPump(client, slave_id=2, direction=Direction.COUNTER_CLOCKWISE)
```

### Classes

| Class | Purpose |
|---|---|
| `Direction` | Enum: `CLOCKWISE = 1`, `COUNTER_CLOCKWISE = -1` |
| `PeristalticPump` | Controls a single pump via shared `ModbusRTUClient` |
| `PeristalticPumpSimulation` | Simulation counterpart for testing |

No bus manager class. Thread safety is handled by `ModbusRTUClient._lock`.

### Why No Bus Manager

The OPS code has `RS485PumpBus` which owns the serial port and creates pump instances. Our design skips this because:
- `ModbusRTUClient` already manages the serial connection with thread-safe locking
- The servo motor uses the same `ModbusRTUClient` pattern — consistency is better
- The caller (run.py, experiment worker) handles connection lifecycle

## API

### PeristalticPump

```python
class PeristalticPump:
    def __init__(
        self,
        client: ModbusRTUClient,
        slave_id: int,
        direction: Direction = Direction.CLOCKWISE,
        max_speed: float = 150.0,
        default_acceleration_ms: int = 200,
        default_deceleration_ms: int = 200,
    ): ...

    # Speed / acceleration
    def set_speed(self, speed_rpm: float) -> None
    def set_acceleration(self, accel_ms: int, decel_ms: int) -> None

    # Manual start/stop
    def start(self) -> None
    def stop(self) -> None

    # Blocking convenience
    def run_for_duration(self, speed_rpm: float, duration_s: float) -> None

    # State
    @property
    def is_running(self) -> bool

    # Abort (experiment worker integration)
    def abort(self) -> None
    def reset_abort(self) -> None
```

### Direction Convention

The `direction` constructor parameter sets the sign applied to all speed values. Speed arguments are always positive. This maps naturally to the aspirate/dispense distinction:
- Dispense pump: `Direction.CLOCKWISE` — positive speed pushed forward
- Aspirate pump: `Direction.COUNTER_CLOCKWISE` — positive speed pulls backward

### Speed Units

Speed is in RPM (0.1 to 150.0). Internally converted to the register's 0.1 r/min unit (multiply by 10). The sign is applied from the `direction` parameter.

### run_for_duration Behavior

1. Sets speed (with direction sign)
2. Writes acceleration config
3. Sends start command
4. Sleeps for `duration_s`, checking `is_aborted` periodically
5. Sends stop command
6. If aborted during sleep, sends stop and raises `RuntimeError`

### Abort Behavior

`abort()` sets `is_aborted = True` and calls `stop()` (decelerated stop). `run_for_duration` checks `is_aborted` during its sleep loop and exits early.

## Register Map

All from the OEM-AMCB209 Modbus protocol:

| Register | Address | R/W | Description | Units |
|---|---|---|---|---|
| `STATUS` | 0x0007 | R | Motion status bits | bit flags |
| `CURRENT_SPEED` | 0x000C | R | Actual speed | 0.1 r/min (signed) |
| `JOG_SPEED` | 0x001D | R/W | Target speed | 0.1 r/min (signed) |
| `JOG_ACCEL` | 0x001E | R/W | Acceleration ramp | ms (0-2000) |
| `JOG_DECEL` | 0x001F | R/W | Deceleration ramp | ms (0-2000) |
| `MOTION_CMD` | 0x0027 | W | Motion command | bit flags |
| `AUX_CMD` | 0x002D | W | Auxiliary command | command code |

### Motion Command Bits

| Bit | Value | Name | Description |
|---|---|---|---|
| 1 | 0x0002 | `CMD_SPEED_START` | Start in speed mode |
| 8 | 0x0100 | `CMD_STOP` | Decelerated stop |

### Auxiliary Commands

| Value | Name | Description |
|---|---|---|
| 0x0012 | `AUX_ENABLE` | Enable motor (lock shaft) |

### Status Bits

| Bit | Value | Name | Description |
|---|---|---|---|
| 2 | 0x04 | `RUNNING` | Motor is running |
| 3 | 0x08 | `ALARM` | Alarm active |

## Simulation Class

`PeristalticPumpSimulation` mirrors the real class API with in-memory state tracking:
- Tracks speed, running state, aborted state
- `run_for_duration` sleeps with abort checks (same as real class)
- No `ModbusRTUClient` dependency

## Tests

Unit tests in `tests/unit/control/test_peristaltic_pump.py`:
- Direction enum values
- Simulation: set_speed, start/stop, is_running state transitions
- Simulation: run_for_duration completes
- Simulation: abort during run_for_duration raises RuntimeError
- Simulation: reset_abort clears state
- Simulation: speed clamping to max_speed with warning
- Simulation: zero/negative speed rejected

## Error Handling

- Speed validation: reject <= 0 or > max_speed (with warning + clamp for over-max, matching servo pattern)
- `ModbusError` propagated from `ModbusRTUClient` for communication failures
- `abort()` catches and logs exceptions from `stop()` (matching servo pattern)

## Signed 16-bit Speed Encoding

The JOG_SPEED register is a signed 16-bit value. For negative speeds (counter-clockwise), the value must be encoded as two's complement for Modbus unsigned register writes:
- Positive: write value directly (e.g., 1500 for 150.0 RPM clockwise)
- Negative: write `value + 65536` (e.g., 64036 for -150.0 RPM counter-clockwise)
