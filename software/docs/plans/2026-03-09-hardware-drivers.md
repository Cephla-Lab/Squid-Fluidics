# Hardware Drivers Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Create fluidics-native drivers for the NiMotion servo motor and INHECO Teleshake heater-shaker, with simulation classes and tests.

**Architecture:** The servo motor gets a simplified 2-file implementation (modbus_rtu.py + servo_motor.py) collapsed from the OPS 4-layer package. The heater shaker gets a vendored copy of the OPS protocol/device code plus a thin fluidics-style wrapper. Both follow existing fluidics patterns: simulation classes in the same file, abort/reset_abort pattern, thread-safe communication.

**Tech Stack:** pyserial (servo RS485 Modbus RTU), hid/hidapi (heater shaker USB HID), pytest

**Reference:** OPS source at `/home/squid/Documents/claude-work/OPS/` — servo in `ServoMotor/servo_service/`, heater shaker in `inheco-teleshake/inheco_teleshake/`

---

## Part 1: Servo Motor Driver

### Task 1: Create Modbus RTU communication module

**Files:**
- Create: `fluidics/control/modbus_rtu.py`
- Test: `tests/unit/control/test_modbus_rtu.py`

This file consolidates the OPS `modbus_rtu/` package (4 files: crc.py, frame.py, exceptions.py, client.py) and `serial_comm/serial_port.py` into a single module with these components:

**Step 1: Write the failing test**

Create `tests/unit/control/test_modbus_rtu.py` with tests for:

```python
import pytest
from fluidics.control.modbus_rtu import (
    calculate_crc,
    ModbusRTUClient,
    ModbusError,
)

class TestCRC:
    def test_known_crc_value(self):
        # Modbus CRC of [0x01, 0x03, 0x00, 0x00, 0x00, 0x01] = 0x840A
        data = bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x01])
        assert calculate_crc(data) == 0x840A

    def test_verify_crc_valid(self):
        # Append CRC bytes (little-endian) and verify
        data = bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x01])
        crc = calculate_crc(data)
        frame = data + bytes([crc & 0xFF, (crc >> 8) & 0xFF])
        assert calculate_crc(frame) == 0  # full-frame CRC check = 0

    def test_crc_empty_data(self):
        assert calculate_crc(b"") == 0xFFFF  # initial value unchanged

class TestFrameBuilding:
    def test_read_holding_registers_frame(self):
        """FC=0x03: slave=1, addr=0x0381 (status word), qty=1"""
        from fluidics.control.modbus_rtu import build_read_registers_frame
        frame = build_read_registers_frame(slave_id=1, address=0x0381, count=1)
        assert frame[0] == 1       # slave
        assert frame[1] == 0x03    # FC
        assert frame[2:4] == b'\x03\x81'  # address big-endian
        assert frame[4:6] == b'\x00\x01'  # count big-endian
        assert len(frame) == 8     # 6 data + 2 CRC

    def test_write_single_register_frame(self):
        """FC=0x06: slave=1, addr=0x0380, value=0x000F"""
        from fluidics.control.modbus_rtu import build_write_register_frame
        frame = build_write_register_frame(slave_id=1, address=0x0380, value=0x000F)
        assert frame[0] == 1
        assert frame[1] == 0x06
        assert len(frame) == 8

    def test_write_multiple_registers_frame(self):
        """FC=0x10: slave=1, addr=0x03E7, values=[0x0000, 0x1000] (32-bit)"""
        from fluidics.control.modbus_rtu import build_write_multiple_registers_frame
        frame = build_write_multiple_registers_frame(
            slave_id=1, address=0x03E7, values=[0x0000, 0x1000]
        )
        assert frame[0] == 1
        assert frame[1] == 0x10
        assert frame[6] == 4  # byte count = 2 registers * 2 bytes

class TestModbusRTUClient:
    def test_read_register_not_connected(self):
        client = ModbusRTUClient()
        with pytest.raises(ModbusError, match="not connected"):
            client.read_register(1, 0x0381)

    def test_context_manager(self):
        client = ModbusRTUClient()
        # Should not raise even without connection
        with client:
            pass
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/control/test_modbus_rtu.py -v`
Expected: FAIL with ImportError (module doesn't exist yet)

**Step 3: Write the implementation**

Create `fluidics/control/modbus_rtu.py` by consolidating:
- CRC-16 table + `calculate_crc()` from OPS `modbus_rtu/crc.py`
- Frame building functions (read holding registers FC=0x03, write single FC=0x06, write multiple FC=0x10) from OPS `modbus_rtu/frame.py` — simplified to standalone functions instead of classes
- `ModbusError` as the single exception class (replaces the 8-class hierarchy in OPS)
- `ModbusRTUClient` class combining OPS `SerialPort` + `ModbusClient`:
  - `__init__(port=None, baudrate=115200, timeout=0.5, retries=3)`
  - `connect(port=None, baudrate=None)` — opens pyserial directly (no custom SerialPort wrapper)
  - `disconnect()`
  - `read_register(slave_id, address)` → int (16-bit)
  - `read_register_32bit(slave_id, address, signed=False)` → int
  - `write_register(slave_id, address, value)` — 16-bit via FC=0x06
  - `write_register_32bit(slave_id, address, value, signed=False)` — via FC=0x10
  - `_send_receive(frame, expected_response_len)` — low-level send/receive with retries, CRC verify
  - `threading.Lock()` for thread safety
  - Context manager support

Key simplifications vs OPS:
- Use `serial.Serial` directly instead of custom `SerialPort` class
- Single `ModbusError` exception instead of 8 subclasses
- Frame building as module-level functions, not a `FrameBuilder` class
- No `ModbusRequest`/`ModbusResponse` dataclasses — work with raw bytes
- Response parsing inline in `_send_receive`

Reference the OPS CRC table verbatim from `/home/squid/Documents/claude-work/OPS/ServoMotor/servo_service/modbus_rtu/crc.py` (lines 10-43).

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/control/test_modbus_rtu.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add fluidics/control/modbus_rtu.py tests/unit/control/test_modbus_rtu.py
git commit -m "feat: add Modbus RTU communication module for servo motor"
```

---

### Task 2: Create servo motor driver

**Files:**
- Create: `fluidics/control/servo_motor.py`
- Test: `tests/unit/control/test_servo_motor.py`

This file contains all servo motor control: CiA402 register definitions, state machine, motor control, axis config, multi-axis service, and simulation.

**Step 1: Write the failing tests**

Create `tests/unit/control/test_servo_motor.py`:

```python
import pytest
from fluidics.control.servo_motor import (
    ServoMotor,
    ServoMotorSimulation,
    AxisConfig,
    DEFAULT_AXIS_CONFIGS,
)

class TestAxisConfig:
    def test_z4_config_exists(self):
        assert "Z4" in DEFAULT_AXIS_CONFIGS

    def test_mm_to_pulses_roundtrip(self):
        config = DEFAULT_AXIS_CONFIGS["Z4"]
        mm = 30.0
        pulses = config.mm_to_pulses(mm)
        assert abs(config.pulses_to_mm(pulses) - mm) < 0.001

    def test_position_valid(self):
        config = DEFAULT_AXIS_CONFIGS["Z4"]
        assert config.is_position_valid(0.0)
        assert config.is_position_valid(59.0)
        assert not config.is_position_valid(-1.0)
        assert not config.is_position_valid(62.0)

    def test_velocity_conversion(self):
        config = DEFAULT_AXIS_CONFIGS["Z4"]
        mm_s = 10.0
        pulses_s = config.velocity_mm_to_pulses(mm_s)
        assert pulses_s > 0
        assert abs(config.velocity_pulses_to_mm(pulses_s) - mm_s) < 0.1

class TestServoMotorSimulation:
    def test_create(self):
        sim = ServoMotorSimulation()
        assert not sim.is_connected

    def test_connect_disconnect(self):
        sim = ServoMotorSimulation()
        sim.connect()
        assert sim.is_connected
        sim.disconnect()
        assert not sim.is_connected

    def test_enable_disable(self):
        sim = ServoMotorSimulation(axis_configs=DEFAULT_AXIS_CONFIGS)
        sim.connect()
        sim.enable("Z4")
        assert sim.is_enabled("Z4")
        sim.disable("Z4")
        assert not sim.is_enabled("Z4")

    def test_move_to(self):
        sim = ServoMotorSimulation(axis_configs=DEFAULT_AXIS_CONFIGS)
        sim.connect()
        sim.enable("Z4")
        sim.home("Z4")
        sim.move_to(30.0, axis="Z4")
        assert abs(sim.get_position("Z4") - 30.0) < 0.01

    def test_move_to_out_of_range(self):
        sim = ServoMotorSimulation(axis_configs=DEFAULT_AXIS_CONFIGS)
        sim.connect()
        sim.enable("Z4")
        sim.home("Z4")
        with pytest.raises(ValueError, match="range"):
            sim.move_to(100.0, axis="Z4")

    def test_jog_and_stop(self):
        sim = ServoMotorSimulation(axis_configs=DEFAULT_AXIS_CONFIGS)
        sim.connect()
        sim.enable("Z4")
        sim.jog(10.0, axis="Z4")  # should not raise
        sim.stop(axis="Z4")

    def test_abort(self):
        sim = ServoMotorSimulation(axis_configs=DEFAULT_AXIS_CONFIGS)
        sim.abort()
        assert sim.is_aborted
        sim.reset_abort()
        assert not sim.is_aborted

    def test_home(self):
        sim = ServoMotorSimulation(axis_configs=DEFAULT_AXIS_CONFIGS)
        sim.connect()
        sim.enable("Z4")
        sim.home("Z4")
        assert sim.is_homed("Z4")
        assert abs(sim.get_position("Z4")) < 0.01

    def test_set_speed(self):
        sim = ServoMotorSimulation(axis_configs=DEFAULT_AXIS_CONFIGS)
        sim.connect()
        sim.set_speed(50.0, axis="Z4")  # should not raise

    def test_context_manager(self):
        sim = ServoMotorSimulation()
        with sim:
            sim.connect()
            assert sim.is_connected
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/control/test_servo_motor.py -v`
Expected: FAIL with ImportError

**Step 3: Write the implementation**

Create `fluidics/control/servo_motor.py` with these components (in order within the file):

**3a. CiA402 register constants** — Extracted from OPS `motor_control/registers.py`. Include only the registers we use:
- `CONTROL_WORD` (0x0380), `STATUS_WORD` (0x0381)
- `MODES_OF_OPERATION` (0x03C2), `MODES_OF_OPERATION_DISPLAY` (0x03C3)
- `TARGET_POSITION` (0x03E7), `POSITION_ACTUAL_VALUE` (0x03C8)
- `TARGET_VELOCITY` (0x0448), `VELOCITY_ACTUAL_VALUE` (0x03D5)
- `PROFILE_VELOCITY` (0x03F8), `PROFILE_ACCELERATION` (0x03FC), `PROFILE_DECELERATION` (0x03FE)
- `HOMING_METHOD` (0x0416), `HOMING_SPEED_HIGH` (0x0417), `HOMING_SPEED_LOW` (0x0419), `HOMING_ACCELERATION` (0x041B), `HOMING_TIMEOUT` (0x012E)
- `BLOCKING_TORQUE` (0x0170), `BLOCKING_TIME` (0x0172)
- `ERROR_CODE` (0x0382)
- `ENCODER_RESOLUTION_NUM/DEN` (0x0406/0x0408), `GEAR_RATIO_NUM/DEN` (0x040C/0x040E)
- `POLARITY` (0x03F3)
- DI config registers (DI1-3 function/logic)
- DO config registers (DO1 function/logic/control)

Store as a simple class with integer constants (not RegisterDef dataclasses):
```python
class Reg:
    CONTROL_WORD = 0x0380
    STATUS_WORD = 0x0381
    # ... etc
```

**ControlWordBits** and **StatusWordBits** — Copy directly from OPS `registers.py` lines 538-588.

**OperationMode** and **HomingMethod** IntEnums — Copy from OPS `registers.py` lines 591-628.

**3b. CiA402 state machine** — From OPS `motor_control/state_machine.py`:
- `DriveState` enum
- `decode_status_word(status_word) → DriveState` function
- `get_transition_path(current, target) → list[(command, control_word)]` function
- No `StateMachine` class — just use the functions directly

**3c. AxisConfig dataclass** — Simplified from OPS `high_level_api/axis_config.py`:
```python
@dataclass
class AxisConfig:
    slave_id: int
    encoder_resolution: int = 10000
    gear_ratio_numerator: int = 1
    gear_ratio_denominator: int = 1
    ball_screw_lead: float = 10.0  # mm/rev
    stroke_min: float = 0.0        # mm
    stroke_max: float = 61.0       # mm
    default_velocity: float = 100.0    # mm/s
    default_acceleration: float = 500.0  # mm/s²
    default_deceleration: float = 500.0  # mm/s²
    max_velocity: float = 500.0    # mm/s
    homing_method: int = 17        # NEGATIVE_LIMIT_SWITCH
    homing_velocity_high: float = 20.0
    homing_velocity_low: float = 4.0
    homing_acceleration: float = 100.0
    homing_timeout: int = 60000    # ms
    velocity_polarity: int = 1
    driver_polarity: int = 0x00
    # DI config
    di1_function: int | None = None
    di1_logic: int = 0
    di2_function: int | None = None
    di2_logic: int = 0
    di3_function: int | None = None
    di3_logic: int = 0
    # Blocking/stall homing
    blocking_torque: int = 300
    blocking_time: int = 500
    # Brake
    has_brake: bool = False
    brake_do_logic: int = 1
    brake_release_delay_ms: int = 500

    # Unit conversion methods (same as OPS AxisConfig)
    @property
    def pulses_per_mm(self) -> float: ...
    def mm_to_pulses(self, mm: float) -> int: ...
    def pulses_to_mm(self, pulses: int) -> float: ...
    def velocity_mm_to_pulses(self, mm_s: float) -> int: ...
    def velocity_pulses_to_mm(self, pulses_s: int) -> float: ...
    def is_position_valid(self, mm: float) -> bool: ...
```

**DEFAULT_AXIS_CONFIGS** dict — Z4 config from OPS `axis_config.py` lines 234-270:
```python
DEFAULT_AXIS_CONFIGS = {
    "Z4": AxisConfig(
        slave_id=4,
        encoder_resolution=131072,  # 17-bit absolute encoder
        ball_screw_lead=10.0,
        stroke_min=0.0,
        stroke_max=61.0,
        default_velocity=100.0,
        default_acceleration=500.0,
        default_deceleration=500.0,
        max_velocity=500.0,
        homing_method=17,  # NEGATIVE_LIMIT_SWITCH
        homing_velocity_high=20.0,
        homing_velocity_low=4.0,
        homing_acceleration=100.0,
        homing_timeout=60000,
        velocity_polarity=-1,
        has_brake=True,
        brake_do_logic=1,
        brake_release_delay_ms=500,
        di2_function=15,  # negative limit
        di2_logic=0,
        di3_function=14,  # positive limit
        di3_logic=0,
        blocking_torque=300,
        blocking_time=500,
    ),
}
```

**3d. ServoMotor class** — Merges OPS `Motor` + `ServoService` into one class:
```python
class ServoMotor:
    def __init__(self, port=None, baudrate=115200, axis_configs=None, timeout=0.5, retries=3):
        self._client = ModbusRTUClient(port, baudrate, timeout, retries)
        self._axis_configs = axis_configs or DEFAULT_AXIS_CONFIGS.copy()
        self._homed = {name: False for name in self._axis_configs}
        self._enabled = {name: False for name in self._axis_configs}
        self.is_aborted = False

    # Connection
    def connect(self, port=None, baudrate=None): ...
    def disconnect(self): ...
    @property
    def is_connected(self) -> bool: ...

    # Internal: motor register access (replaces OPS Motor class)
    def _read_status_word(self, slave_id) -> int: ...
    def _write_control_word(self, slave_id, value): ...
    def _get_state(self, slave_id) -> DriveState: ...
    def _wait_for_state(self, slave_id, target, timeout): ...
    def _wait_for_motion(self, slave_id, timeout): ...

    # Initialization (from OPS ServoService.initialize_motor_parameters)
    def initialize_axis(self, axis): ...

    # State control
    def enable(self, axis=None): ...
    def disable(self, axis=None): ...
    def fault_reset(self, axis=None): ...
    def quick_stop(self, axis=None): ...
    def is_enabled(self, axis=None) -> bool: ...

    # Motion
    def move_to(self, position_mm, axis=None, velocity_mm_s=None, wait=True): ...
    def move_relative(self, distance_mm, axis=None, velocity_mm_s=None, wait=True): ...
    def jog(self, velocity_mm_s, axis=None): ...
    def stop(self, axis=None, wait=True): ...
    def home(self, axis=None, wait=True): ...

    # Status
    def get_position(self, axis=None) -> float: ...  # mm
    def get_velocity(self, axis=None) -> float: ...   # mm/s
    def is_homed(self, axis=None) -> bool: ...

    # Parameters
    def set_speed(self, velocity_mm_s, axis=None): ...
    def set_acceleration(self, accel_mm_s2, axis=None): ...
    def set_deceleration(self, decel_mm_s2, axis=None): ...

    # Abort
    def abort(self): ...
    def reset_abort(self): ...

    # Context manager
    def __enter__(self): ...
    def __exit__(self, *exc): ...
```

The axis parameter is a string key into `_axis_configs` (e.g. `"Z4"`). If None, uses the first configured axis.

**Implementation notes — pulling from OPS code:**
- `enable()`: Use `get_transition_path()` from state machine, same as OPS `Motor.enable()` (lines 249-285)
- `disable()`: Same as OPS `Motor.disable()` (lines 293-315)
- `fault_reset()`: Same as OPS `Motor.fault_reset()` (lines 326-353) — rising edge on bit 7
- `move_to()`: Validate range, convert mm→pulses via AxisConfig, then same as OPS `Motor.move_absolute()` (lines 483-525)
- `jog()`: Same as OPS `Motor.run_velocity()` (lines 614-638) — fake velocity mode via far target
- `stop()`: Same as OPS `Motor.stop()` (lines 639-677) — halt bit + read position + clear halt
- `home()`: Same as OPS `ServoService.home()` (lines 948-983) — set homing speeds from config, then OPS `Motor.home()` (lines 716-784)
- `initialize_axis()`: Same as OPS `ServoService.initialize_motor_parameters()` (lines 353-475) — encoder res, gear ratio, homing timeout, blocking params, DI config, brake auto-control

**3e. ServoMotorSimulation class**:
```python
class ServoMotorSimulation:
    def __init__(self, axis_configs=None):
        self._axis_configs = axis_configs or DEFAULT_AXIS_CONFIGS.copy()
        self._positions = {name: 0.0 for name in self._axis_configs}
        self._homed = {name: False for name in self._axis_configs}
        self._enabled = {name: False for name in self._axis_configs}
        self._connected = False
        self.is_aborted = False
```

Same interface as `ServoMotor`. `move_to()` updates `_positions[axis]` directly. `home()` sets position to 0 and `_homed[axis] = True`. `enable()`/`disable()` toggle `_enabled[axis]`. Range validation uses the same `AxisConfig.is_position_valid()`.

**Step 4: Run tests**

Run: `python -m pytest tests/unit/control/test_servo_motor.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add fluidics/control/servo_motor.py tests/unit/control/test_servo_motor.py
git commit -m "feat: add servo motor driver with CiA402 state machine and simulation"
```

---

### Task 3: Update conftest for servo motor time patching

**Files:**
- Modify: `tests/conftest.py`

**Step 1: Add time patches for the servo motor module**

The servo motor uses `time.sleep` and `time.time` for polling (wait_for_state, wait_for_motion). Add monkeypatch entries:

```python
# In _fast_clock fixture, add:
monkeypatch.setattr("fluidics.control.servo_motor.time", _time)  # import time module patching
```

Actually — the servo motor module will `import time` and use `time.sleep()` / `time.time()` (not `from time import sleep`), so the existing `monkeypatch.setattr("time.sleep", ...)` and `monkeypatch.setattr("time.time", ...)` should be sufficient. Only add explicit patches if the module uses `from time import sleep/time`.

Check after implementation whether additional patches are needed.

**Step 2: Commit if changes needed**

```bash
git add tests/conftest.py
git commit -m "fix: add time patches for servo motor tests"
```

---

## Part 2: Heater Shaker Driver

### Task 4: Vendor the inheco_teleshake package

**Files:**
- Create: `fluidics/control/inheco_teleshake/__init__.py`
- Create: `fluidics/control/inheco_teleshake/protocol.py`
- Create: `fluidics/control/inheco_teleshake/device.py`

**Step 1: Copy the OPS files**

Copy from `/home/squid/Documents/claude-work/OPS/inheco-teleshake/inheco_teleshake/`:
- `protocol.py` — verbatim, no changes needed
- `device.py` — verbatim, no changes needed
- `__init__.py` — create with public exports:

```python
from .device import Teleshake, TeleshakeError
from .protocol import (
    CmdType, ReportCmd, ActionCmd, SetCmd, Status,
    VENDOR_ID, PRODUCT_ID,
    error_description,
)
```

**Step 2: Verify import works**

Run: `python -c "from fluidics.control.inheco_teleshake import Teleshake, TeleshakeError"`
Expected: Success (may warn about missing `hid` package if not installed, but import should work)

**Step 3: Commit**

```bash
git add fluidics/control/inheco_teleshake/
git commit -m "feat: vendor inheco_teleshake USB HID protocol library"
```

---

### Task 5: Create heater shaker driver wrapper

**Files:**
- Create: `fluidics/control/heater_shaker.py`
- Test: `tests/unit/control/test_heater_shaker.py`

**Step 1: Write the failing tests**

```python
import pytest
from fluidics.control.heater_shaker import HeaterShaker, HeaterShakerSimulation

class TestHeaterShakerSimulation:
    def test_create(self):
        sim = HeaterShakerSimulation()
        assert not sim.is_connected

    def test_connect_disconnect(self):
        sim = HeaterShakerSimulation()
        sim.connect()
        assert sim.is_connected
        sim.disconnect()
        assert not sim.is_connected

    def test_temperature_control(self):
        sim = HeaterShakerSimulation()
        sim.connect()
        sim.set_target_temperature(37.0)
        assert sim.get_target_temperature() == 37.0
        sim.start_heating(45.0)
        assert sim.get_target_temperature() == 45.0
        sim.stop_heating()

    def test_get_actual_temperature(self):
        sim = HeaterShakerSimulation()
        sim.connect()
        temp = sim.get_actual_temperature()
        assert isinstance(temp, float)

    def test_shaking(self):
        sim = HeaterShakerSimulation()
        sim.connect()
        sim.start_shaking(300)
        assert sim.get_shaker_rpm() == 300
        sim.stop_shaking()
        assert sim.get_shaker_rpm() == 0

    def test_clamping(self):
        sim = HeaterShakerSimulation()
        sim.connect()
        sim.set_clamp(close=True)
        assert sim.get_clamp_status() == "closed"
        sim.set_clamp(close=False)
        assert sim.get_clamp_status() == "open"

    def test_abort(self):
        sim = HeaterShakerSimulation()
        sim.abort()
        assert sim.is_aborted
        sim.reset_abort()
        assert not sim.is_aborted

    def test_context_manager(self):
        sim = HeaterShakerSimulation()
        with sim:
            sim.connect()
            assert sim.is_connected

    def test_emergency_off(self):
        sim = HeaterShakerSimulation()
        sim.connect()
        sim.start_shaking(500)
        sim.start_heating(50.0)
        sim.emergency_off()
        assert sim.get_shaker_rpm() == 0

    def test_device_info(self):
        sim = HeaterShakerSimulation()
        sim.connect()
        assert isinstance(sim.get_serial_number(), int)
        assert isinstance(sim.get_firmware_version(), str)
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/control/test_heater_shaker.py -v`
Expected: FAIL with ImportError

**Step 3: Write the implementation**

Create `fluidics/control/heater_shaker.py`:

```python
class HeaterShaker:
    """INHECO Teleshake heater-shaker driver.

    Wraps the vendored inheco_teleshake library with fluidics patterns
    (connect/disconnect, abort, consistent interface).
    """

    def __init__(self, device_path=None):
        self._device_path = device_path  # HID device path (bytes), None = auto-detect
        self._device = None  # Teleshake instance
        self.is_aborted = False

    # Connection
    def connect(self, device_path=None): ...  # Creates Teleshake.open(path)
    def disconnect(self): ...                 # Calls self._device.close()
    @property
    def is_connected(self) -> bool: ...

    # Temperature
    def set_target_temperature(self, temp_c: float): ...
    def get_target_temperature(self) -> float: ...
    def get_actual_temperature(self, sensor=1) -> float: ...
    def start_heating(self, target_c: float): ...
    def stop_heating(self): ...

    # Shaking
    def start_shaking(self, rpm: int, clockwise=True): ...
    def stop_shaking(self, return_to=None, timeout=10.0): ...
    def set_shaker_rpm(self, rpm: int): ...
    def get_shaker_rpm(self) -> int: ...
    def get_shaker_target(self) -> int: ...

    # Clamping
    def set_clamp(self, close: bool, wait=False, timeout=10.0): ...
    def get_clamp_status(self) -> str: ...  # "open", "closed", "unknown"

    # Positioner (not in sequences, but available in driver)
    def set_positioner(self, enable: bool): ...
    def set_target_position(self, milli_degrees: int): ...
    def get_real_position(self) -> int: ...

    # Device info
    def get_serial_number(self) -> int: ...
    def get_firmware_version(self) -> str: ...
    def get_device_status(self) -> bool: ...  # True = ready
    def get_errors(self) -> list[int]: ...
    def clear_errors(self): ...

    # Safety
    def emergency_off(self): ...

    # Abort
    def abort(self): ...
    def reset_abort(self): ...

    # Context manager
    def __enter__(self): ...
    def __exit__(self, *exc): ...
```

Each method is a thin delegation to `self._device.<method>()`. The `connect()` method creates the `Teleshake` instance, `disconnect()` closes it.

**HeaterShakerSimulation** — same interface, no hardware:
```python
class HeaterShakerSimulation:
    def __init__(self, device_path=None):
        self._connected = False
        self._target_temp = 25.0
        self._shaker_rpm = 0
        self._clamp_status = "open"
        self.is_aborted = False

    def connect(self, device_path=None):
        self._connected = True

    # All methods: track state in instance variables, return sensible defaults
    def get_actual_temperature(self, sensor=1):
        return self._target_temp  # simulation returns target as actual

    def get_serial_number(self):
        return 12345

    def get_firmware_version(self):
        return "1.0.0"
    # ... etc
```

**Step 4: Run tests**

Run: `python -m pytest tests/unit/control/test_heater_shaker.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add fluidics/control/heater_shaker.py tests/unit/control/test_heater_shaker.py
git commit -m "feat: add heater shaker driver with simulation class"
```

---

## Execution Order

1. **Task 1** — modbus_rtu.py (servo dependency)
2. **Task 2** — servo_motor.py (depends on Task 1)
3. **Task 3** — conftest patches (if needed)
4. **Task 4** — vendor inheco_teleshake (independent of 1-3)
5. **Task 5** — heater_shaker.py (depends on Task 4)

Tasks 1-3 (servo) and Tasks 4-5 (heater shaker) are independent tracks.
Servo motor should be done first per user request.
