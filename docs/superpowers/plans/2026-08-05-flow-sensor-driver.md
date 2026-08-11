# Flow Sensor Driver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read the SLF3S-0600F flow sensor continuously in software, plot it live in the GUI, and record it to CSV.

**Architecture:** The Teensy already streams the flow reading in bytes 23–24 of every 30-byte status packet, so no firmware or protocol change is needed. We add a single reader thread inside `FluidController` that becomes the only thing touching the serial port, publishing each parsed packet to shared state that every existing caller reads from. A `FlowSensor` driver subscribes to that stream, maps the `INT16_MAX` error sentinel to `None`, and feeds a per-sensor GUI tab modelled on the existing temperature widget.

**Tech Stack:** Python 3.10, pydantic v2, PyQt5, matplotlib, pytest. No new dependencies.

## Global Constraints

- **Run all commands from `software/`.** `pytest` is configured via `software/pyproject.toml` with `testpaths = ["tests/unit", "tests/integration"]`.
- **No new dependencies.** Everything needed is already imported somewhere in the tree.
- **I²C indices 1 and 2 only.** Index 0 is `Wire`, shared with the selector valves, whose driver emits a general-call transaction after every command (`RheoLink.cpp:161-162`). Excluded by design.
- **Phase 1 accepts exactly one configured sensor.** Two require a firmware change that is out of scope here.
- **The packet stays 30 bytes.** `MCU_MSG_LENGTH` is unchanged. No firmware edits in this plan at all.
- **Never start a background thread inside a constructor.** `tests/conftest.py` installs an autouse `_fast_clock` fixture that patches `time.sleep`, `time.time`, and `threading.Event.wait` process-wide; a live thread under a fake clock spins unboundedly. Follow the existing `TCMControllerSimulation` precedent — build the thread in `__init__`, start it from an explicit call.
- **Scale factor is 10.** µL/min = raw / 10.0, via `MCU_CONSTANTS.SCALE_FACTOR_FLOW`.
- **Raw 32767 is an error sentinel, never a flow rate.** The sensor's real output limit is ±3250 µL/min (raw ±32500), so the sentinel is distinguishable from saturation.

---

### Task 1: Config schema for flow sensors

**Files:**
- Modify: `software/fluidics/control/config.py`
- Modify: `software/tests/fixtures/flow_cell_config.yaml`
- Test: `software/tests/unit/control/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `FlowSensorConfig` with fields `index: int`, `name: str`, `monitor: str`, `ramp_up_seconds: float`, `tolerance_fraction: float`, `max_flow_rate_ul_min: float`. `FluidicsConfig.flow_sensors: Optional[List[FlowSensorConfig]]`, defaulting to `None`.

- [ ] **Step 1: Write the failing tests**

Append to `software/tests/unit/control/test_config.py`:

```python
class TestFlowSensorConfig:
    def test_absent_section_is_none(self):
        config = FluidicsConfig(**_make_config_dict())
        assert config.flow_sensors is None

    def test_minimal_sensor_takes_defaults(self):
        config = FluidicsConfig(**_make_config_dict(
            flow_sensors=[{"index": 1, "name": "syringe_draw"}]
        ))
        sensor = config.flow_sensors[0]
        assert sensor.index == 1
        assert sensor.name == "syringe_draw"
        assert sensor.monitor == "off"
        assert sensor.ramp_up_seconds == 3.0
        assert sensor.tolerance_fraction == 0.3
        assert sensor.max_flow_rate_ul_min == 2000

    def test_explicit_values_override_defaults(self):
        config = FluidicsConfig(**_make_config_dict(
            flow_sensors=[{
                "index": 2, "name": "waste_line", "monitor": "stop",
                "ramp_up_seconds": 1.5, "tolerance_fraction": 0.1,
                "max_flow_rate_ul_min": 1500,
            }]
        ))
        sensor = config.flow_sensors[0]
        assert sensor.monitor == "stop"
        assert sensor.ramp_up_seconds == 1.5
        assert sensor.tolerance_fraction == 0.1
        assert sensor.max_flow_rate_ul_min == 1500

    @pytest.mark.parametrize("bad_index", [0, 3, -1])
    def test_index_must_be_1_or_2(self, bad_index):
        with pytest.raises(ValidationError):
            FluidicsConfig(**_make_config_dict(
                flow_sensors=[{"index": bad_index, "name": "s"}]
            ))

    def test_unknown_monitor_mode_rejected(self):
        with pytest.raises(ValidationError):
            FluidicsConfig(**_make_config_dict(
                flow_sensors=[{"index": 1, "name": "s", "monitor": "halt"}]
            ))

    @pytest.mark.parametrize("field,bad_value", [
        ("ramp_up_seconds", 0),
        ("ramp_up_seconds", -1),
        ("tolerance_fraction", 0),
        ("tolerance_fraction", 1.5),
        ("max_flow_rate_ul_min", 0),
    ])
    def test_out_of_range_values_rejected(self, field, bad_value):
        with pytest.raises(ValidationError):
            FluidicsConfig(**_make_config_dict(
                flow_sensors=[{"index": 1, "name": "s", field: bad_value}]
            ))

    def test_duplicate_index_rejected(self):
        with pytest.raises(ValidationError, match="index"):
            FluidicsConfig(**_make_config_dict(flow_sensors=[
                {"index": 1, "name": "a"},
                {"index": 1, "name": "b"},
            ]))

    def test_duplicate_name_rejected(self):
        with pytest.raises(ValidationError, match="name"):
            FluidicsConfig(**_make_config_dict(flow_sensors=[
                {"index": 1, "name": "same"},
                {"index": 2, "name": "same"},
            ]))

    def test_two_sensors_rejected_in_phase_1(self):
        with pytest.raises(ValidationError, match="one flow sensor"):
            FluidicsConfig(**_make_config_dict(flow_sensors=[
                {"index": 1, "name": "a"},
                {"index": 2, "name": "b"},
            ]))

    def test_empty_list_rejected(self):
        with pytest.raises(ValidationError):
            FluidicsConfig(**_make_config_dict(flow_sensors=[]))

    def test_fixture_config_has_flow_sensor(self, fixtures_dir):
        config = load_config(str(fixtures_dir / "flow_cell_config.yaml"))
        assert config.flow_sensors is not None
        assert config.flow_sensors[0].index == 1
```

Note `test_duplicate_index_rejected` and `test_duplicate_name_rejected` use two entries, which the Phase 1 limit also rejects. Order the validator so duplicate checks run *before* the count check, so those tests fail for the reason they claim.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/control/test_config.py::TestFlowSensorConfig -v`
Expected: FAIL — `TypeError` / `ValidationError` about unexpected keyword `flow_sensors`.

- [ ] **Step 3: Add the model**

In `software/fluidics/control/config.py`, add after `TemperatureControllerConfig`:

```python
class FlowSensorConfig(BaseModel):
    """One SLF3X flow sensor on the Teensy's I2C bus.

    index is the I2C bus the sensor sits on (1 = Wire1, 2 = Wire2). Bus 0
    is excluded: it is shared with the selector valves, whose driver emits
    a general-call transaction after every command.

    The monitor fields are per-sensor tuning consumed by draw protection in
    the operations layer. Nothing reads them yet.
    """
    index: Literal[1, 2]
    name: str
    monitor: Literal["off", "warn", "stop"] = "off"
    ramp_up_seconds: float = Field(default=3.0, gt=0)
    tolerance_fraction: float = Field(default=0.3, gt=0, le=1)
    max_flow_rate_ul_min: float = Field(default=2000, gt=0)
```

Add the field to `FluidicsConfig`, immediately after `temperature_controller`:

```python
    flow_sensors: Optional[List[FlowSensorConfig]] = Field(default=None, min_length=1)
```

Add the cross-entry validator to `FluidicsConfig`:

```python
    @model_validator(mode='after')
    def _check_flow_sensors(self):
        if self.flow_sensors is None:
            return self

        indices = [s.index for s in self.flow_sensors]
        if len(set(indices)) != len(indices):
            raise ValueError("flow_sensors entries must have unique index values")

        names = [s.name for s in self.flow_sensors]
        if len(set(names)) != len(names):
            raise ValueError("flow_sensors entries must have unique name values")

        if len(self.flow_sensors) > 1:
            raise ValueError(
                "only one flow sensor is supported; a second requires firmware "
                "that populates packet bytes 25-26"
            )
        return self
```

- [ ] **Step 4: Add a sensor to the test fixture**

Append to `software/tests/fixtures/flow_cell_config.yaml`, before the final `application:` line:

```yaml
flow_sensors:
  - index: 1
    name: syringe_draw
    monitor: warn
```

- [ ] **Step 5: Run the full unit suite**

Run: `python -m pytest tests/unit -v`
Expected: PASS, including every pre-existing test.

- [ ] **Step 6: Commit**

```bash
git add software/fluidics/control/config.py software/tests/unit/control/test_config.py software/tests/fixtures/flow_cell_config.yaml
git commit -m "feat(config): add flow_sensors section"
```

---

### Task 2: Extract packet parsing and add a reader thread

**Files:**
- Modify: `software/fluidics/control/controller.py`
- Test: `software/tests/unit/control/test_controller.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `FluidController._parse_packet(msg) -> dict` — pure, no I/O. Returns the same dict `get_mcu_status()` used to build, plus a new `"flowrates_raw": [int, int]` key holding the signed int16 values before scaling.
  - `FluidController._publish_status(parsed: dict) -> None` — stores under lock, increments `_status_seq`, invokes `packet_callback`.
  - `FluidController.packet_callback: Optional[Callable[[dict], None]]` — set by consumers, fired once per packet.
  - `FluidController.start_reading() -> None` / `stop_reading() -> None`.
  - `get_mcu_status()` returns a snapshot dict of the most recent packet.
  - `FluidControllerSimulation` gains no-op `start_reading()` / `stop_reading()` and a `packet_callback` attribute.

- [ ] **Step 1: Write the failing tests**

Append to `software/tests/unit/control/test_controller.py`:

```python
from fluidics.control.controller import FluidController
from fluidics.control._def import COMMAND_STATUS


def _make_packet(uid=1, cmd=3, status=COMMAND_STATUS.COMPLETED_WITHOUT_ERRORS,
                 flow_raw=1000):
    """Build a 30-byte MCU status packet with the given flow sensor 1 value."""
    msg = [0] * 30
    msg[0] = (uid >> 8) & 0xFF
    msg[1] = uid & 0xFF
    msg[2] = cmd
    msg[3] = status
    unsigned = flow_raw & 0xFFFF
    msg[23] = (unsigned >> 8) & 0xFF
    msg[24] = unsigned & 0xFF
    return msg


def _bare_controller():
    """A FluidController with no serial port, for testing pure logic.

    __init__ is bypassed, so the two flags _log_packet reads must be set by
    hand — otherwise _publish_status raises AttributeError.
    """
    fc = FluidController.__new__(FluidController)
    fc.log_measurements = False
    fc.debug = False
    return fc


class TestParsePacket:
    def test_positive_flow_scales_by_ten(self):
        fc = _bare_controller()
        parsed = fc._parse_packet(_make_packet(flow_raw=1000))
        assert parsed["flowrates"][0] == pytest.approx(100.0)
        assert parsed["flowrates_raw"][0] == 1000

    def test_negative_flow_scales_by_ten(self):
        fc = _bare_controller()
        parsed = fc._parse_packet(_make_packet(flow_raw=-1000))
        assert parsed["flowrates"][0] == pytest.approx(-100.0)
        assert parsed["flowrates_raw"][0] == -1000

    def test_sentinel_survives_as_raw(self):
        fc = _bare_controller()
        parsed = fc._parse_packet(_make_packet(flow_raw=32767))
        assert parsed["flowrates_raw"][0] == 32767

    def test_saturation_is_distinct_from_sentinel(self):
        fc = _bare_controller()
        parsed = fc._parse_packet(_make_packet(flow_raw=32500))
        assert parsed["flowrates_raw"][0] == 32500
        assert parsed["flowrates"][0] == pytest.approx(3250.0)

    def test_uid_and_status_round_trip(self):
        fc = _bare_controller()
        parsed = fc._parse_packet(_make_packet(uid=513, status=COMMAND_STATUS.IN_PROGRESS))
        assert parsed["MCU_received_command_UID"] == 513
        assert parsed["MCU_command_execution_status"] == COMMAND_STATUS.IN_PROGRESS


class TestPublishStatus:
    def test_publish_makes_status_readable(self):
        fc = _bare_controller()
        fc._init_status_state()
        fc._publish_status(fc._parse_packet(_make_packet(flow_raw=250)))
        assert fc.get_mcu_status()["flowrates"][0] == pytest.approx(25.0)

    def test_publish_increments_sequence(self):
        fc = _bare_controller()
        fc._init_status_state()
        fc._publish_status(fc._parse_packet(_make_packet()))
        first = fc._status_seq
        fc._publish_status(fc._parse_packet(_make_packet()))
        assert fc._status_seq == first + 1

    def test_packet_callback_fires_with_parsed_dict(self):
        fc = _bare_controller()
        fc._init_status_state()
        seen = []
        fc.packet_callback = seen.append
        fc._publish_status(fc._parse_packet(_make_packet(flow_raw=400)))
        assert len(seen) == 1
        assert seen[0]["flowrates"][0] == pytest.approx(40.0)

    def test_callback_exception_does_not_break_publishing(self):
        fc = _bare_controller()
        fc._init_status_state()
        fc.packet_callback = lambda _: 1 / 0
        fc._publish_status(fc._parse_packet(_make_packet(flow_raw=400)))
        assert fc.get_mcu_status()["flowrates"][0] == pytest.approx(40.0)

    def test_snapshot_is_a_copy(self):
        fc = _bare_controller()
        fc._init_status_state()
        fc._publish_status(fc._parse_packet(_make_packet()))
        snapshot = fc.get_mcu_status()
        snapshot["flowrates"] = "clobbered"
        assert fc.get_mcu_status()["flowrates"] != "clobbered"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/control/test_controller.py -k "ParsePacket or PublishStatus" -v`
Expected: FAIL with `AttributeError: 'FluidController' object has no attribute '_parse_packet'`.

- [ ] **Step 3: Extract parsing out of `get_mcu_status`**

In `software/fluidics/control/controller.py`, add `import threading` at the top.

Move the body of `get_mcu_status` that decodes `msg` into a new method. Everything from `MCU_received_command_UID = ...` down to the `self.recorded_data = {...}` assignment becomes:

```python
    def _parse_packet(self, msg):
        '''Decode a 30-byte MCU status packet into a dict. Pure — no I/O.'''
        MCU_received_command_UID = (msg[0] << 8) + msg[1]
        MCU_received_command = msg[2]
        MCU_command_execution_status = msg[3]
        MCU_interal_program = msg[4]

        bubble_sensor_1_state, bubble_sensor_2_state = split_byte(msg[5])

        selector_valve_1_pos = msg[6]
        selector_valve_2_pos = msg[7]
        selector_valve_3_pos = msg[8]
        selector_valve_4_pos = msg[9]
        selector_valve_5_pos = msg[10]

        solenoid_valves = np.int16((int(msg[11]) << 8) + msg[12])

        measurement_pump_power = MCU_CONSTANTS.TTP_MAX_PW * float((int(msg[13]) << 8) + msg[14]) / np.iinfo(np.uint16).max

        _pressure_1_raw = (int(msg[15]) << 8) + msg[16]
        _pressure_2_raw = (int(msg[17]) << 8) + msg[18]
        _pressure_3_raw = (int(msg[19]) << 8) + msg[20]
        _pressure_4_raw = (int(msg[21]) << 8) + msg[22]

        def raw_to_psi(raw_pressure):
            return (raw_pressure - MCU_CONSTANTS._output_min) * (MCU_CONSTANTS._p_max - MCU_CONSTANTS._p_min) / (MCU_CONSTANTS._output_max - MCU_CONSTANTS._output_min) + MCU_CONSTANTS._p_min

        pressure_1 = raw_to_psi(_pressure_1_raw)
        pressure_2 = raw_to_psi(_pressure_2_raw)
        pressure_3 = raw_to_psi(_pressure_3_raw)
        pressure_4 = raw_to_psi(_pressure_4_raw)

        # Keep the raw int16 alongside the scaled value: 32767 is the SLF3X
        # "no reading" sentinel, and comparing raw ints beats comparing floats.
        flow_1_raw = int(np.int16((int(msg[23]) << 8) + msg[24]))
        flow_2_raw = int(np.int16((int(msg[25]) << 8) + msg[26]))
        flow_1 = float(flow_1_raw) / MCU_CONSTANTS.SCALE_FACTOR_FLOW
        flow_2 = float(flow_2_raw) / MCU_CONSTANTS.SCALE_FACTOR_FLOW

        MCU_CMD_time_elapsed = msg[27]

        vol_ul = (float(np.int16((int(msg[28]) << 8) + msg[29])) / np.iinfo(np.int16).max) * MCU_CONSTANTS.VOLUME_UL_MAX

        return {
            "MCU_received_command_UID": MCU_received_command_UID,
            "MCU_received_command": MCU_received_command,
            "MCU_command_execution_status": MCU_command_execution_status,
            "MCU_interal_program": MCU_interal_program,
            "bubble_sensor_states": [bubble_sensor_1_state, bubble_sensor_2_state],
            "MCU_CMD_time_elapsed": MCU_CMD_time_elapsed,
            "selector_valves_pos": [selector_valve_1_pos, selector_valve_2_pos, selector_valve_3_pos, selector_valve_4_pos, selector_valve_5_pos],
            "solenoid_valves": solenoid_valves,
            "measurement_pump_power": measurement_pump_power,
            "pressures": [pressure_1, pressure_2, pressure_3, pressure_4],
            "flowrates": [flow_1, flow_2],
            "flowrates_raw": [flow_1_raw, flow_2_raw],
            "vol_ul": vol_ul,
        }
```

The CSV logging block that formatted `line` moves into `_publish_status` (next step), because it should run once per packet.

- [ ] **Step 4: Add shared status state and the reader thread**

Add to `controller.py`:

```python
    def _init_status_state(self):
        '''Set up shared packet state. Called from __init__ and by tests.'''
        self._status_lock = threading.Lock()
        self._latest_status = None
        self._status_seq = 0
        self._reader_thread = None
        self._terminate_reader = False
        self.packet_callback = None

    def _publish_status(self, parsed):
        '''Store a parsed packet, bump the sequence, notify the subscriber.'''
        with self._status_lock:
            self._latest_status = parsed
            self._status_seq += 1
            self.recorded_data = parsed

        self._log_packet(parsed)

        callback = self.packet_callback
        if callback is not None:
            try:
                callback(parsed)
            except Exception as e:
                print_message(f"Packet callback failed: {e}")

    def _reader_loop(self):
        while not self._terminate_reader:
            try:
                msg = self.read_received_packet_nowait()
                if msg is None:
                    sleep(0.001)
                    continue
                if len(msg) != MCU_MSG_LENGTH:
                    continue
                self._publish_status(self._parse_packet(msg))
            except Exception as e:
                # A corrupt COBS frame raises from cobs.decode, and the port
                # raises during shutdown. Neither should kill the only thread
                # feeding every consumer — drop the frame and carry on.
                if not self._terminate_reader:
                    print_message(f"Reader thread error: {e}")
                sleep(0.001)

    def start_reading(self):
        '''Begin consuming packets. The reader thread owns the serial port.'''
        if self._reader_thread is not None:
            return
        self._terminate_reader = False
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def stop_reading(self):
        self._terminate_reader = True
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=2)
            self._reader_thread = None
```

Note `_reader_loop` calls `read_received_packet_nowait()` **without** `discard_buffer=True`. The thread consumes continuously, so there is no backlog to discard, and discarding would throw away packets the flow sensor needs.

Move the CSV/debug logging into its own method, taking the values from the parsed dict:

```python
    def _log_packet(self, d):
        if not (self.log_measurements or self.debug):
            return
        b1, b2 = d["bubble_sensor_states"]
        sv = d["selector_valves_pos"]
        p = d["pressures"]
        f = d["flowrates"]
        line = (f"{datetime.now().strftime('%m/%d %H:%M:%S')},"
                f"{d['MCU_received_command_UID']},"
                f"{d['MCU_received_command']},"
                f"{d['MCU_command_execution_status']},"
                f"{d['MCU_interal_program']},"
                f"{b1:>04b},{b2:>04b},"
                f"{d['MCU_CMD_time_elapsed']},"
                f"{sv[0]},{sv[1]},{sv[2]},{sv[3]},{sv[4]},"
                f"{d['solenoid_valves']:>016b},"
                f"{d['measurement_pump_power']:.2f},"
                f"{p[0]:.2f},{p[1]:.2f},{p[2]:.2f},{p[3]:.2f},"
                f"{f[0]:.2f},{f[1]:.2f},"
                f"{d['vol_ul']:.2f}\n")
        if self.log_measurements:
            self.measurement_file.write(line)
            self.counter_measurement_file_flush += 1
            if self.counter_measurement_file_flush >= 500:
                self.counter_measurement_file_flush = 0
                self.measurement_file.flush()
        if self.debug:
            print(line)
```

- [ ] **Step 5: Rewrite `get_mcu_status` as a snapshot read**

Replace the whole method body with:

```python
    def get_mcu_status(self):
        '''Return the most recent packet as a dict, waiting for the first one.'''
        while True:
            with self._status_lock:
                if self._latest_status is not None:
                    return dict(self._latest_status)
            sleep(0.001)
```

- [ ] **Step 6: Wire lifecycle into `FluidController`**

In `FluidController.__init__`, add `self._init_status_state()` immediately before the `super().__init__(...)` call.

In `FluidController.begin()` — which is inherited from `Microcontroller` — start the thread by overriding it:

```python
    def begin(self):
        super().begin()
        self.start_reading()
```

In `FluidController.__del__`, call `self.stop_reading()` before closing the serial port.

- [ ] **Step 7: Update the simulation**

In `FluidControllerSimulation.__init__`, add `self.packet_callback = None`. Add the two no-ops:

```python
    def start_reading(self):
        pass

    def stop_reading(self):
        pass
```

- [ ] **Step 8: Run the full suite**

Run: `python -m pytest tests/unit tests/integration -v`
Expected: PASS. Watch particularly for `tests/unit/control/test_selector_valve.py` and the integration tests, which exercise `get_mcu_status` through the simulation.

- [ ] **Step 9: Commit**

```bash
git add software/fluidics/control/controller.py software/tests/unit/control/test_controller.py
git commit -m "refactor(controller): single reader thread owns the serial port"
```

---

### Task 3: Match the command UID in `wait_for_completion`

**Files:**
- Modify: `software/fluidics/control/controller.py`
- Test: `software/tests/unit/control/test_controller.py`

**Interfaces:**
- Consumes: `_init_status_state()`, `_publish_status()`, `_parse_packet()` from Task 2.
- Produces: `wait_for_completion(timeout=30)` returning the status int, raising `TimeoutError` when no matching packet arrives.

This is the riskiest change in the plan: every command path depends on it. Without it, the reader thread from Task 2 makes `wait_for_completion` return the *previous* command's status almost every time, because a snapshot taken 1 ms after `send_command` is up to 60 ms old and necessarily predates the command.

- [ ] **Step 1: Write the failing tests**

Append to `software/tests/unit/control/test_controller.py`:

```python
class TestWaitForCompletion:
    def test_returns_when_uid_matches(self):
        fc = _bare_controller()
        fc._init_status_state()
        fc.cmd_uid = 7
        fc._publish_status(fc._parse_packet(
            _make_packet(uid=7, status=COMMAND_STATUS.COMPLETED_WITHOUT_ERRORS)))
        assert fc.wait_for_completion() == COMMAND_STATUS.COMPLETED_WITHOUT_ERRORS

    def test_ignores_stale_packet_from_previous_command(self):
        fc = _bare_controller()
        fc._init_status_state()
        fc.cmd_uid = 8
        # The packet still in flight carries the *previous* command, completed.
        fc._publish_status(fc._parse_packet(
            _make_packet(uid=7, status=COMMAND_STATUS.COMPLETED_WITHOUT_ERRORS)))
        with pytest.raises(TimeoutError):
            fc.wait_for_completion(timeout=1)

    def test_waits_through_in_progress(self):
        fc = _bare_controller()
        fc._init_status_state()
        fc.cmd_uid = 9
        fc._publish_status(fc._parse_packet(
            _make_packet(uid=9, status=COMMAND_STATUS.IN_PROGRESS)))
        with pytest.raises(TimeoutError):
            fc.wait_for_completion(timeout=1)

    def test_returns_error_status_without_raising(self):
        fc = _bare_controller()
        fc._init_status_state()
        fc.cmd_uid = 10
        fc._publish_status(fc._parse_packet(
            _make_packet(uid=10, status=COMMAND_STATUS.CMD_EXECUTION_ERROR)))
        assert fc.wait_for_completion() == COMMAND_STATUS.CMD_EXECUTION_ERROR

    def test_timeout_message_names_the_command(self):
        fc = _bare_controller()
        fc._init_status_state()
        fc.cmd_uid = 11
        fc.cmd_sent = 3
        fc._publish_status(fc._parse_packet(_make_packet(uid=10)))
        with pytest.raises(TimeoutError, match="11"):
            fc.wait_for_completion(timeout=1)


class TestClearResetsUid:
    """CMD_SET.CLEAR resets cmd_uid to 0, and send_command writes the UID into
    the outgoing array *after* that reset. So the command goes out with UID 0
    and self.cmd_uid == 0, and the subsequent wait can match. This is correct
    by accident of statement order — pin it.
    """

    def test_clear_leaves_cmd_uid_at_zero(self):
        fc = _bare_controller()
        fc._init_status_state()
        fc.cmd_uid = 57
        fc.serial = None
        sent = []
        fc.send_mcu_command = sent.append
        fc.send_command(CMD_SET.CLEAR)
        assert fc.cmd_uid == 0
        assert sent[0][0] == 0 and sent[0][1] == 0
```

Add `from fluidics.control._def import CMD_SET` to the test imports.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/control/test_controller.py -k "WaitForCompletion or ClearResetsUid" -v`
Expected: FAIL — `test_ignores_stale_packet_from_previous_command` hangs or returns instead of raising, because the current implementation ignores the UID.

- [ ] **Step 3: Implement UID matching**

Replace `wait_for_completion` in `controller.py`:

```python
    def wait_for_completion(self, timeout=30):
        '''Block until the MCU reports a terminal status for the command we just sent.

        Matching on the UID matters: a packet already in flight when the command
        went out still carries the previous command's status, and accepting it
        would return before the firmware had even started.
        '''
        deadline = time() + timeout
        while True:
            data = self.get_mcu_status()
            if data['MCU_received_command_UID'] == self.cmd_uid:
                status = data['MCU_command_execution_status']
                if status != COMMAND_STATUS.IN_PROGRESS:
                    return status
            if time() > deadline:
                raise TimeoutError(
                    f"MCU did not complete command {self.cmd_sent} "
                    f"(uid {self.cmd_uid}) within {timeout}s"
                )
            sleep(0.005)
```

`time` and `sleep` are already imported at the top of the module, and `tests/conftest.py` patches both, so the timeout path completes instantly under test.

**Deliberate deviation from the spec:** the spec says this raises `OperationError`. That class lives in `fluidics/experiment_worker.py`, and importing it into `fluidics/control/controller.py` would invert the layering — the control layer would depend on the experiment layer. The builtin `TimeoutError` avoids that, and `ExperimentWorker.run` catches bare `Exception` (`experiment_worker.py:83`), so it still surfaces through the `on_error` callback with the same effect.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/control/test_controller.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest tests/unit tests/integration -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add software/fluidics/control/controller.py software/tests/unit/control/test_controller.py
git commit -m "fix(controller): match command UID in wait_for_completion"
```

---

### Task 4: `FlowSensor` driver

**Files:**
- Create: `software/fluidics/control/flow_sensor.py`
- Test: `software/tests/unit/control/test_flow_sensor.py`

**Interfaces:**
- Consumes: `FluidController.packet_callback` and `_publish_status` from Task 2; `FlowSensorConfig` from Task 1.
- Produces:
  - `FlowSensor(fluid_controller, index, name, packet_slot=0)` with `begin()`, `latest_flow_ul_min` property returning `float | None`, `subscribe(callback)` where callback takes `(flow_ul_min, timestamp)`, and `close()`.
  - `FlowSensorSimulation(fluid_controller=None, index=1, name="sim", packet_slot=0)` with the same API plus a settable `simulated_flow_ul_min` attribute and a `reading_thread` that callers start explicitly.
  - Module constant `INVALID_RAW = 32767`.

- [ ] **Step 1: Write the failing tests**

Create `software/tests/unit/control/test_flow_sensor.py`:

```python
# tests/unit/control/test_flow_sensor.py
import pytest

from fluidics.control.controller import FluidController
from fluidics.control.flow_sensor import FlowSensor, FlowSensorSimulation, INVALID_RAW
from fluidics.control._def import CMD_SET, COMMAND_STATUS


def _make_packet(flow_raw=1000, uid=1, status=COMMAND_STATUS.COMPLETED_WITHOUT_ERRORS):
    msg = [0] * 30
    msg[0] = (uid >> 8) & 0xFF
    msg[1] = uid & 0xFF
    msg[3] = status
    unsigned = flow_raw & 0xFFFF
    msg[23] = (unsigned >> 8) & 0xFF
    msg[24] = unsigned & 0xFF
    return msg


class FakeController:
    """Minimal stand-in that can publish packets to a FlowSensor."""

    def __init__(self, status=COMMAND_STATUS.COMPLETED_WITHOUT_ERRORS):
        self.packet_callback = None
        self.commands = []
        self._status = status

    def send_command_blocking(self, command, *args):
        self.commands.append((command, args))
        return self._status

    def publish(self, flow_raw):
        parsed = FluidController._parse_packet(self, _make_packet(flow_raw=flow_raw))
        if self.packet_callback is not None:
            self.packet_callback(parsed)


class TestFlowSensorReadings:
    def test_positive_reading(self):
        fc = FakeController()
        sensor = FlowSensor(fc, index=1, name="s")
        fc.publish(1000)
        assert sensor.latest_flow_ul_min == pytest.approx(100.0)

    def test_negative_reading(self):
        fc = FakeController()
        sensor = FlowSensor(fc, index=1, name="s")
        fc.publish(-1000)
        assert sensor.latest_flow_ul_min == pytest.approx(-100.0)

    def test_sentinel_maps_to_none(self):
        fc = FakeController()
        sensor = FlowSensor(fc, index=1, name="s")
        fc.publish(INVALID_RAW)
        assert sensor.latest_flow_ul_min is None

    def test_saturation_is_a_real_reading(self):
        fc = FakeController()
        sensor = FlowSensor(fc, index=1, name="s")
        fc.publish(32500)
        assert sensor.latest_flow_ul_min == pytest.approx(3250.0)

    def test_no_reading_before_first_packet(self):
        sensor = FlowSensor(FakeController(), index=1, name="s")
        assert sensor.latest_flow_ul_min is None

    def test_recovers_after_a_sentinel(self):
        fc = FakeController()
        sensor = FlowSensor(fc, index=1, name="s")
        fc.publish(INVALID_RAW)
        fc.publish(500)
        assert sensor.latest_flow_ul_min == pytest.approx(50.0)


class TestFlowSensorSubscribers:
    def test_subscriber_receives_each_reading(self):
        fc = FakeController()
        sensor = FlowSensor(fc, index=1, name="s")
        seen = []
        sensor.subscribe(lambda flow, ts: seen.append(flow))
        fc.publish(100)
        fc.publish(200)
        assert seen == [pytest.approx(10.0), pytest.approx(20.0)]

    def test_subscriber_sees_none_for_sentinel(self):
        fc = FakeController()
        sensor = FlowSensor(fc, index=1, name="s")
        seen = []
        sensor.subscribe(lambda flow, ts: seen.append(flow))
        fc.publish(INVALID_RAW)
        assert seen == [None]

    def test_failing_subscriber_does_not_break_others(self):
        fc = FakeController()
        sensor = FlowSensor(fc, index=1, name="s")
        seen = []
        sensor.subscribe(lambda flow, ts: 1 / 0)
        sensor.subscribe(lambda flow, ts: seen.append(flow))
        fc.publish(100)
        assert seen == [pytest.approx(10.0)]


class TestFlowSensorBegin:
    def test_sends_initialize_with_index_water_and_crc(self):
        fc = FakeController()
        sensor = FlowSensor(fc, index=2, name="s")
        sensor.begin()
        command, args = fc.commands[0]
        assert command == CMD_SET.INITIALIZE_FLOW_SENSOR
        assert args == (2, 0x08, True)

    def test_raises_when_mcu_reports_execution_error(self):
        fc = FakeController(status=COMMAND_STATUS.CMD_EXECUTION_ERROR)
        sensor = FlowSensor(fc, index=1, name="s")
        with pytest.raises(RuntimeError, match="index 1"):
            sensor.begin()


class TestFlowSensorPacketSlot:
    def test_slot_one_reads_bytes_25_26(self):
        fc = FakeController()
        sensor = FlowSensor(fc, index=2, name="s", packet_slot=1)
        fc.publish(1000)   # writes bytes 23-24 only; slot 1 stays zero
        assert sensor.latest_flow_ul_min == pytest.approx(0.0)


class TestFlowSensorSimulation:
    def test_default_reading_is_available(self):
        sim = FlowSensorSimulation()
        assert sim.latest_flow_ul_min is not None

    def test_settable_reading(self):
        sim = FlowSensorSimulation()
        sim.simulated_flow_ul_min = 123.0
        assert sim.latest_flow_ul_min == pytest.approx(123.0)

    def test_can_simulate_a_dead_sensor(self):
        sim = FlowSensorSimulation()
        sim.simulated_flow_ul_min = None
        assert sim.latest_flow_ul_min is None

    def test_begin_is_a_noop(self):
        FlowSensorSimulation().begin()

    def test_thread_is_not_started_on_construction(self):
        sim = FlowSensorSimulation()
        assert not sim.reading_thread.is_alive()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/control/test_flow_sensor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'fluidics.control.flow_sensor'`.

- [ ] **Step 3: Write the driver**

Create `software/fluidics/control/flow_sensor.py`:

```python
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

        self.fc.packet_callback = self._on_packet

    def begin(self):
        """Initialize the sensor on the MCU. Raises if the MCU reports failure."""
        status = self.fc.send_command_blocking(
            CMD_SET.INITIALIZE_FLOW_SENSOR, self.index, MEDIUM_WATER, PERFORM_CRC)
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
        if getattr(self.fc, "packet_callback", None) is self._on_packet:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/control/test_flow_sensor.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest tests/unit tests/integration -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add software/fluidics/control/flow_sensor.py software/tests/unit/control/test_flow_sensor.py
git commit -m "feat(flow-sensor): add FlowSensor driver with sentinel handling"
```

---

### Task 5: GUI tab

**Files:**
- Modify: `software/gui.py`

**Interfaces:**
- Consumes: `FlowSensor.subscribe()` and `latest_flow_ul_min` from Task 4; `config.flow_sensors` from Task 1.
- Produces: `FlowSensorWidget(sensor)` and `FlowSensorControlWidget(sensors)` — the latter takes a list of sensors and is added as a tab.

There is no automated test for this task; PyQt widgets are not covered by the existing suite. Verification is by launching the GUI in simulation mode.

- [ ] **Step 1: Add the per-sensor widget**

In `software/gui.py`, insert after the `TemperatureControlWidget` class:

```python
class FlowSensorWidget(QWidget):
    """One flow sensor's readout, plot, and CSV recording."""

    reading_signal = pyqtSignal(object, float)  # (flow_ul_min or None, timestamp)

    def __init__(self, sensor, parent=None):
        super().__init__(parent)
        self.sensor = sensor

        self.flows = []
        self.times = []
        self.query_interval = 1
        self.window_size = 60
        self.last_update = 0
        self.file = None
        self.writer = None

        self.reading_signal.connect(self._on_reading)
        self._build_ui()
        self.sensor.subscribe(self._on_callback)

    def _build_ui(self):
        layout = QVBoxLayout(self)

        readout = QGroupBox(f"{self.sensor.name} (I2C index {self.sensor.index})")
        readout_layout = QHBoxLayout()
        self.flow_label = QLabel("--")
        readout_layout.addWidget(QLabel("Flow rate:"))
        readout_layout.addWidget(self.flow_label)
        readout_layout.addStretch()
        readout.setLayout(readout_layout)

        plot_box = QGroupBox("Plot")
        plot_layout = QVBoxLayout()

        plot_controls = QWidget()
        pc_layout = QHBoxLayout(plot_controls)
        pc_layout.addWidget(QLabel("Query Interval:"))
        self.interval_input = QSpinBox()
        self.interval_input.setMinimum(1)
        self.interval_input.setValue(1)
        self.interval_input.setSuffix(" s")
        pc_layout.addWidget(self.interval_input)
        pc_layout.addWidget(QLabel("Window Size:"))
        self.window_input = QSpinBox()
        self.window_input.setMinimum(10)
        self.window_input.setMaximum(3600)
        self.window_input.setValue(60)
        self.window_input.setSuffix(" s")
        pc_layout.addWidget(self.window_input)
        plot_layout.addWidget(plot_controls)

        self.canvas = MplCanvas(self, width=5, height=4, dpi=100)
        plot_layout.addWidget(self.canvas)

        self.record_btn = QPushButton("Start Recording")
        plot_layout.addWidget(self.record_btn)
        plot_box.setLayout(plot_layout)

        layout.addWidget(readout)
        layout.addWidget(plot_box)

        self.record_btn.clicked.connect(self._toggle_record)
        self.interval_input.valueChanged.connect(self._set_interval)
        self.window_input.valueChanged.connect(self._set_window)

    def _set_interval(self, value):
        self.query_interval = value

    def _set_window(self, value):
        self.window_size = value
        self._refresh_plot()

    def _on_callback(self, flow, timestamp):
        # Runs in the controller's reader thread; marshal to the GUI thread.
        self.reading_signal.emit(flow, timestamp)

    def _on_reading(self, flow, current_time):
        if current_time - self.last_update < self.query_interval:
            return

        if flow is None:
            self.flow_label.setText("invalid")
        else:
            self.flow_label.setText(f"{flow:.1f} µL/min")

        # None is appended as-is: matplotlib renders it as a gap, which is
        # what an invalid reading should look like rather than a 3276.7 spike.
        self.flows.append(flow)
        self.times.append(current_time)

        if self.writer is not None:
            self.writer.writerow([datetime.fromtimestamp(current_time),
                                  "" if flow is None else f"{flow:.2f}"])

        while self.times and current_time - self.times[0] > self.window_size:
            self.times.pop(0)
            self.flows.pop(0)

        self._refresh_plot()
        self.last_update = current_time

    def _refresh_plot(self):
        if not self.times:
            return
        ax = self.canvas.axes
        ax.clear()
        ax.plot(self.times, self.flows, "b-")

        valid = [f for f in self.flows if f is not None]
        if valid:
            y_min, y_max = min(valid), max(valid)
            padding = (y_max - y_min) * 0.1 if y_max != y_min else 1.0
            ax.set_ylim([y_min - padding, y_max + padding])

        current_time = self.times[-1]
        ax.set_xlim([current_time - self.window_size, current_time])
        ax.set_xlabel("Seconds Ago")
        ax.set_ylabel("Flow Rate (µL/min)")
        ax.set_title(self.sensor.name)
        ax.grid(True)
        ax.set_xticklabels([f"{x:.0f}" for x in current_time - ax.get_xticks()])
        self.canvas.draw()

    def _toggle_record(self):
        if self.record_btn.text() == "Start Recording":
            self.record_btn.setText("Stop Recording")
            filename = f"flow_{self.sensor.name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            self.file = open(filename, "w", newline="")
            self.writer = csv.writer(self.file)
            self.writer.writerow(["Time", "Flow Rate (uL/min)"])
        else:
            self.record_btn.setText("Start Recording")
            self.close_recording()

    def close_recording(self):
        if self.file is not None:
            self.file.close()
            self.file = None
            self.writer = None


class FlowSensorControlWidget(QWidget):
    """Container laying out one FlowSensorWidget per configured sensor."""

    def __init__(self, sensors):
        super().__init__()
        layout = QHBoxLayout(self)
        self.sensor_widgets = []
        for sensor in sensors:
            sw = FlowSensorWidget(sensor)
            self.sensor_widgets.append(sw)
            layout.addWidget(sw)

    def closeEvent(self, event):
        for sw in self.sensor_widgets:
            sw.close_recording()
        event.accept()
```

- [ ] **Step 2: Verify the widget imports cleanly**

Run: `python -c "import gui"`
Expected: no output, exit code 0. If `pyqtSignal`, `QGroupBox`, or `csv` is missing from the imports at the top of `gui.py`, add it — the temperature widget already uses all of them, so they should all be present.

- [ ] **Step 3: Commit**

```bash
git add software/gui.py
git commit -m "feat(gui): add flow sensor plotting widget"
```

---

### Task 6: Wire sensors into the GUI and CLI

**Files:**
- Modify: `software/gui.py`
- Modify: `software/run_sequences.py`
- Test: `software/tests/integration/test_flow_sensor_startup.py` (create)

**Interfaces:**
- Consumes: everything from Tasks 1–5.
- Produces: `build_flow_sensors(controller, config, simulation) -> list` in `fluidics/control/flow_sensor.py`, returning constructed (not yet begun) sensors, empty when `config.flow_sensors` is None.

- [ ] **Step 1: Write the failing integration test**

Create `software/tests/integration/test_flow_sensor_startup.py`:

```python
# tests/integration/test_flow_sensor_startup.py
from fluidics.control.controller import FluidControllerSimulation
from fluidics.control.flow_sensor import build_flow_sensors


class TestBuildFlowSensors:
    def test_returns_empty_when_not_configured(self, open_chamber_config):
        fc = FluidControllerSimulation(serial_number="test")
        assert build_flow_sensors(fc, open_chamber_config, simulation=True) == []

    def test_builds_one_sensor_from_flow_cell_config(self, flow_cell_config):
        fc = FluidControllerSimulation(serial_number="test")
        sensors = build_flow_sensors(fc, flow_cell_config, simulation=True)
        assert len(sensors) == 1
        assert sensors[0].name == "syringe_draw"
        assert sensors[0].index == 1

    def test_phase_1_sensor_reads_packet_slot_zero(self, flow_cell_config):
        fc = FluidControllerSimulation(serial_number="test")
        sensors = build_flow_sensors(fc, flow_cell_config, simulation=True)
        assert sensors[0].packet_slot == 0

    def test_simulated_sensors_begin_and_close_cleanly(self, flow_cell_config):
        fc = FluidControllerSimulation(serial_number="test")
        sensors = build_flow_sensors(fc, flow_cell_config, simulation=True)
        for s in sensors:
            s.begin()
        for s in sensors:
            s.close()
```

Note `open_chamber_config` has no `flow_sensors` section, which is why it exercises the None path.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/integration/test_flow_sensor_startup.py -v`
Expected: FAIL with `ImportError: cannot import name 'build_flow_sensors'`.

- [ ] **Step 3: Add the factory**

Append to `software/fluidics/control/flow_sensor.py`:

```python
def build_flow_sensors(fluid_controller, config, simulation=False):
    """Construct FlowSensor instances from config. Does not call begin().

    Phase 1: the firmware has a single sensor object and always transmits it
    in packet slot 0, whichever I2C bus it sits on. When the firmware grows a
    sensor array this becomes `index - 1`.
    """
    if not config.flow_sensors:
        return []

    cls = FlowSensorSimulation if simulation else FlowSensor
    return [
        cls(fluid_controller, index=cfg.index, name=cfg.name, packet_slot=0)
        for cfg in config.flow_sensors
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/integration/test_flow_sensor_startup.py -v`
Expected: PASS.

- [ ] **Step 5: Wire into the GUI**

In `software/gui.py`, add the import alongside the other control imports:

```python
from fluidics.control.flow_sensor import build_flow_sensors
```

In `FluidicsControlGUI.__init__`, add `self.flowSensors = []` next to `self.temperatureController = None`.

At the end of `initialize_hardware`, replace:

```python
        self.controller.begin()
        self.controller.send_command(CMD_SET.CLEAR)
```

with:

```python
        self.controller.begin()
        self.controller.send_command(CMD_SET.CLEAR)

        self.flowSensors = build_flow_sensors(self.controller, config, simulation)
        for sensor in self.flowSensors:
            try:
                sensor.begin()
            except Exception as e:
                msg = f"Failed to initialize flow sensor '{sensor.name}': {e}"
                print(msg)
                self.flowSensors = []
                QMessageBox.warning(
                    self, "Flow Sensor",
                    f"{msg}\n\nCheck that the sensor is connected to I2C index "
                    f"{sensor.index}. The Flow Sensor tab will not be available."
                )
                break
```

In `initUI`, after the temperature tab block, add:

```python
        if self.flowSensors:
            flowSensorTab = FlowSensorControlWidget(self.flowSensors)
            self.tabWidget.addTab(flowSensorTab, "Flow Sensors")
            for sensor in self.flowSensors:
                if hasattr(sensor, "reading_thread"):
                    sensor.reading_thread.start()
```

In `closeEvent`, before the syringe pump shutdown, add:

```python
        for sensor in self.flowSensors:
            sensor.close()
        self.controller.stop_reading()
```

- [ ] **Step 6: Wire into the CLI**

In `software/run_sequences.py`, add the import:

```python
from fluidics.control.flow_sensor import build_flow_sensors
```

Change `initialize_hardware` to build and begin sensors, and to return them. Replace the tail of the function:

```python
    controller.begin()
    controller.send_command(CMD_SET.CLEAR)

    flow_sensors = build_flow_sensors(controller, config, simulation)
    for sensor in flow_sensors:
        sensor.begin()

    return controller, syringePump, temperatureController, flow_sensors
```

Update the call site in `main`:

```python
        controller, syringePump, temperatureController, flowSensors = initialize_hardware(args.simulation, config)
```

Add `flowSensors = []` to the locals initialized at the top of `main`, alongside `syringePump = None`.

In the `finally` block, before the temperature controller shutdown:

```python
        for sensor in flowSensors:
            sensor.close()
```

- [ ] **Step 7: Verify both entry points in simulation**

Run: `python run_sequences.py --path sample_sequences/merfish-experiment.yaml --config tests/fixtures/flow_cell_config.yaml --simulation`
Expected: the run completes, and the startup output includes `Simulated flow sensor 'syringe_draw' on I2C index 1.`

Run: `python -c "import run_sequences, gui"`
Expected: no output, exit code 0.

- [ ] **Step 8: Run the full suite**

Run: `python -m pytest tests/unit tests/integration -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add software/gui.py software/run_sequences.py software/fluidics/control/flow_sensor.py software/tests/integration/test_flow_sensor_startup.py
git commit -m "feat: wire flow sensors into GUI and CLI startup"
```

---

## Manual hardware verification

Not automatable; run once on real hardware before merging.

- [ ] With no sensor physically connected, confirm `sensor.begin()` raises and the GUI shows the warning rather than plotting a steady 3276.7 µL/min.
- [ ] With a sensor on Wire1, confirm the Flow Sensor tab plots a live trace and that CSV recording produces a file with plausible values.
- [ ] Run a `flow_reagent` sequence and confirm the plot shows flow rising during the draw and returning to zero afterwards.
- [ ] Confirm selector valve moves still work — that exercises the `wait_for_completion` UID change on every command.
- [ ] Confirm a full `priming` sequence completes without a spurious `current position is X; expected Y` error.
