# Flow Cell Temperature Controller Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow Flow Cell experiments to use a temperature controller (1-channel variant) by unifying the existing 2-channel `TCMController` into a single class parameterized by channel count, extracting the stabilization loop into a shared helper, and adding `set_temperature` to Flow Cell sequence types.

**Architecture:** Single `TCMController` class that takes `channels: 1 | 2` from config. The wire protocol is unchanged. A new `fluidics/sequence_utils.set_temperature` function holds the polling loop and is called from both `MERFISHOperations` and `OpenChamberOperations`. On stabilization timeout, it raises `OperationError` instead of logging silently. The GUI temperature widget is rewritten as a container plus per-channel sub-widget.

**Tech Stack:** Python 3, pydantic, PyQt5, pyserial, pytest.

**Branch:** `flow-cell-temperature-controller` (already created from `main`).

---

## Pre-task: Verify clean branch state

- [ ] **Step 1: Confirm branch and clean tree**

Run from `/home/squid/Documents/claude-work/Squid-Fluidics`:

```bash
git status
git branch --show-current
```

Expected: branch is `flow-cell-temperature-controller`. Working tree clean except for untracked `software/config.yaml` and the spec/plan in `docs/superpowers/`.

- [ ] **Step 2: Confirm tests pass on baseline**

Run from `/home/squid/Documents/claude-work/Squid-Fluidics/software`:

```bash
python -m pytest -q
```

Expected: all unit + integration tests pass (some integration tests will be slow under the existing simulation; that's fine for now).

---

## Task 1: Add `channels`, `tolerance_celsius`, `stabilization_timeout_seconds` to config

**Files:**
- Modify: `software/fluidics/control/config.py` (the `TemperatureControllerConfig` class, lines 68–69)
- Modify: `software/tests/unit/control/test_config.py` (add a test class)

- [ ] **Step 1: Add failing tests for new config fields**

Append this class to `software/tests/unit/control/test_config.py`:

```python
class TestTemperatureControllerConfig:
    def _config_with_tc(self, **tc_overrides):
        tc = {"serial_number": "TC-X"}
        tc.update(tc_overrides)
        return _make_config_dict(temperature_controller=tc)

    def test_defaults_populated(self):
        cfg = FluidicsConfig(**self._config_with_tc())
        assert cfg.temperature_controller.channels == 2
        assert cfg.temperature_controller.tolerance_celsius == 1.0
        assert cfg.temperature_controller.stabilization_timeout_seconds == 300

    def test_explicit_values_override_defaults(self):
        cfg = FluidicsConfig(**self._config_with_tc(
            channels=1, tolerance_celsius=0.5, stabilization_timeout_seconds=120,
        ))
        tc = cfg.temperature_controller
        assert tc.channels == 1
        assert tc.tolerance_celsius == 0.5
        assert tc.stabilization_timeout_seconds == 120

    def test_channels_must_be_1_or_2(self):
        with pytest.raises(ValidationError):
            FluidicsConfig(**self._config_with_tc(channels=3))

    def test_tolerance_must_be_positive(self):
        with pytest.raises(ValidationError):
            FluidicsConfig(**self._config_with_tc(tolerance_celsius=0))

    def test_timeout_must_be_positive(self):
        with pytest.raises(ValidationError):
            FluidicsConfig(**self._config_with_tc(stabilization_timeout_seconds=0))
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd software
python -m pytest tests/unit/control/test_config.py::TestTemperatureControllerConfig -v
```

Expected: 5 failures. `defaults_populated` fails because `channels`/`tolerance_celsius`/`stabilization_timeout_seconds` don't exist yet; the validation tests fail because pydantic accepts unknown values.

- [ ] **Step 3: Add the new fields to `TemperatureControllerConfig`**

Replace the class in `software/fluidics/control/config.py` (around line 68):

```python
class TemperatureControllerConfig(BaseModel):
    serial_number: str
    channels: Literal[1, 2] = 2
    tolerance_celsius: float = Field(default=1.0, gt=0)
    stabilization_timeout_seconds: float = Field(default=300, gt=0)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/unit/control/test_config.py -v
```

Expected: all tests pass, including the existing config tests.

- [ ] **Step 5: Commit**

```bash
git add software/fluidics/control/config.py software/tests/unit/control/test_config.py
git commit -m "feat(config): add channels, tolerance, timeout fields to TemperatureControllerConfig"
```

---

## Task 2: Rewrite `TCMController` and `TCMControllerSimulation` for N channels

This rewrite changes the public API. Existing call sites (open chamber operations, GUI widget, conftest, run_sequences) become broken until subsequent tasks fix them. Tests for the simulation are added in this task; integration tests for OpenChamberOperations will be repaired in Task 4.

**Files:**
- Replace: `software/fluidics/control/temperature_controller.py`
- Create: `software/tests/unit/control/test_temperature_controller.py`

- [ ] **Step 1: Write failing tests for the new simulation**

Create `software/tests/unit/control/test_temperature_controller.py`:

```python
import pytest

from fluidics.control.temperature_controller import TCMControllerSimulation


class TestTCMControllerSimulation:
    def test_default_channels_is_2(self):
        tc = TCMControllerSimulation(sn=None)
        assert tc.channels == 2
        assert len(tc.target_temperatures) == 2
        assert len(tc.actual_temperatures) == 2

    def test_one_channel(self):
        tc = TCMControllerSimulation(sn=None, channels=1)
        assert tc.channels == 1
        assert len(tc.target_temperatures) == 1
        assert len(tc.actual_temperatures) == 1

    def test_set_target_updates_actual_in_simulation(self):
        tc = TCMControllerSimulation(sn=None, channels=2)
        tc.set_target_temperature(1, 37.5)
        assert tc.target_temperatures[0] == 37.5
        assert tc.actual_temperatures[0] == 37.5

    def test_set_target_only_updates_named_channel(self):
        tc = TCMControllerSimulation(sn=None, channels=2)
        tc.set_target_temperature(2, 50.0)
        # channel 1 untouched (still default 10.0)
        assert tc.target_temperatures[0] == 10.0
        assert tc.target_temperatures[1] == 50.0

    def test_get_target_temperature_returns_current_target(self):
        tc = TCMControllerSimulation(sn=None, channels=2)
        tc.set_target_temperature(1, 25.0)
        assert tc.get_target_temperature(1) == 25.0

    def test_get_actual_temperature_returns_simulated_actual(self):
        tc = TCMControllerSimulation(sn=None, channels=1)
        tc.set_target_temperature(1, 42.0)
        assert tc.get_actual_temperature(1) == 42.0

    def test_invalid_channel_raises(self):
        tc = TCMControllerSimulation(sn=None, channels=1)
        with pytest.raises(ValueError):
            tc.set_target_temperature(2, 25.0)

    def test_tolerance_and_timeout_stored(self):
        tc = TCMControllerSimulation(
            sn=None, channels=1,
            tolerance_celsius=0.5, stabilization_timeout_seconds=60,
        )
        assert tc.tolerance_celsius == 0.5
        assert tc.stabilization_timeout_seconds == 60

    def test_save_target_temperature_does_not_raise(self):
        tc = TCMControllerSimulation(sn=None, channels=2)
        tc.save_target_temperature(1)
        tc.save_target_temperature(2)

    def test_close_does_not_raise(self):
        tc = TCMControllerSimulation(sn=None, channels=1)
        tc.close()

    def test_abort_flag(self):
        tc = TCMControllerSimulation(sn=None, channels=1)
        assert tc.is_aborted is False
        tc.abort()
        assert tc.is_aborted is True
        tc.reset_abort()
        assert tc.is_aborted is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/unit/control/test_temperature_controller.py -v
```

Expected: all fail with `AttributeError` or `TypeError` since the new API doesn't exist.

- [ ] **Step 3: Replace `software/fluidics/control/temperature_controller.py`**

Overwrite the entire file with:

```python
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

        self.temperature_updating_callback = None
        self.terminate_temperature_updating_thread = False
        self.actual_temp_updating_thread = threading.Thread(
            target=self._update_loop, daemon=True
        )

        self.is_aborted = False

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

        self.temperature_updating_callback = None
        self.terminate_temperature_updating_thread = False
        self.actual_temp_updating_thread = threading.Thread(
            target=self._update_loop, daemon=True
        )

        self.is_aborted = False

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
```

- [ ] **Step 4: Run unit tests to verify they pass**

```bash
python -m pytest tests/unit/control/test_temperature_controller.py -v
```

Expected: all 11 tests pass.

- [ ] **Step 5: Commit**

```bash
git add software/fluidics/control/temperature_controller.py \
        software/tests/unit/control/test_temperature_controller.py
git commit -m "refactor(temperature): unify TCMController for 1- or 2-channel devices"
```

Note: integration tests and the GUI are intentionally broken at this point — they reference the old API (`target_temperature_ch1`, `'TC1'` strings, `t1`/`t2`). They are repaired in Tasks 3–7.

---

## Task 3: Add `fluidics/sequence_utils.py` with `set_temperature` helper

**Files:**
- Create: `software/fluidics/sequence_utils.py`
- Create: `software/tests/unit/test_sequence_utils.py`

- [ ] **Step 1: Write failing tests for the helper**

Create `software/tests/unit/test_sequence_utils.py`:

```python
import pytest

from fluidics.control.temperature_controller import TCMControllerSimulation
from fluidics.experiment_worker import OperationError
from fluidics.sequence_utils import set_temperature


class _StuckController:
    """Test stub: targets are stored, but actuals never converge."""
    def __init__(self, channels, tolerance_celsius=1.0, stabilization_timeout_seconds=300):
        self.channels = channels
        self.tolerance_celsius = tolerance_celsius
        self.stabilization_timeout_seconds = stabilization_timeout_seconds
        self.target_temperatures = [0.0] * channels
        self.actual_temperatures = [0.0] * channels  # never matches a non-zero target
        self.is_aborted = False

    def set_target_temperature(self, channel, t):
        self.target_temperatures[channel - 1] = t


class TestSetTemperature:
    def test_none_controller_returns_silently(self, capsys):
        set_temperature(None, 37.0)  # should not raise
        out = capsys.readouterr().out
        assert "No temperature controller" in out

    def test_one_channel_converges_immediately(self):
        tc = TCMControllerSimulation(sn=None, channels=1)
        set_temperature(tc, 42.0)
        assert tc.target_temperatures == [42.0]
        assert tc.actual_temperatures == [42.0]

    def test_two_channel_sets_both_channels(self):
        tc = TCMControllerSimulation(sn=None, channels=2)
        set_temperature(tc, 30.0)
        assert tc.target_temperatures == [30.0, 30.0]
        assert tc.actual_temperatures == [30.0, 30.0]

    def test_timeout_raises_operation_error(self):
        tc = _StuckController(channels=1, stabilization_timeout_seconds=5)
        with pytest.raises(OperationError, match="failed to stabilize"):
            set_temperature(tc, 50.0)

    def test_abort_returns_silently(self):
        tc = _StuckController(channels=1, stabilization_timeout_seconds=5)
        tc.is_aborted = True
        set_temperature(tc, 50.0)  # should return without raising
        # target was still set on the controller before the abort check
        assert tc.target_temperatures == [50.0]
```

- [ ] **Step 2: Add `fluidics.sequence_utils` patches to the global conftest**

Modify `software/tests/conftest.py` — inside `_fast_clock`, add these two lines after the existing `monkeypatch.setattr` calls:

```python
    monkeypatch.setattr("fluidics.sequence_utils.sleep", fake_sleep, raising=False)
    monkeypatch.setattr("fluidics.sequence_utils.time", fake_time_fn, raising=False)
```

`raising=False` is needed because the module doesn't exist when conftest is collected the first time on a fresh checkout. (Pytest re-imports during the test run, so by the time the fixture runs, the module is importable.)

- [ ] **Step 3: Run tests to verify they fail**

```bash
python -m pytest tests/unit/test_sequence_utils.py -v
```

Expected: failures with `ModuleNotFoundError: fluidics.sequence_utils`.

- [ ] **Step 4: Create `software/fluidics/sequence_utils.py`**

```python
"""Shared sequence helpers used by both flow cell and open chamber operations."""

from time import sleep, time

from .experiment_worker import OperationError


def set_temperature(tc, target):
    """Drive every channel on `tc` to `target` and block until all channels
    are within tolerance, abort is requested, or timeout fires.

    On timeout, raises OperationError so the experiment worker stops.
    If `tc` is None, prints a warning and returns.
    """
    if tc is None:
        print("No temperature controller found. Skipping temperature control sequence.")
        return

    for channel in range(1, tc.channels + 1):
        tc.set_target_temperature(channel, target)

    start_time = time()
    while True:
        sleep(1)
        if tc.is_aborted:
            return
        if all(abs(t - target) <= tc.tolerance_celsius for t in tc.actual_temperatures):
            return
        if time() - start_time > tc.stabilization_timeout_seconds:
            raise OperationError(
                f"Temperature failed to stabilize within "
                f"{tc.stabilization_timeout_seconds}s "
                f"(target={target}, actual={tc.actual_temperatures})"
            )
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python -m pytest tests/unit/test_sequence_utils.py -v
```

Expected: all 5 tests pass.

- [ ] **Step 6: Commit**

```bash
git add software/fluidics/sequence_utils.py \
        software/tests/unit/test_sequence_utils.py \
        software/tests/conftest.py
git commit -m "feat(sequences): add shared set_temperature helper that raises on timeout"
```

---

## Task 4: Migrate `OpenChamberOperations` to use the helper; fix integration tests

**Files:**
- Modify: `software/fluidics/open_chamber_operations.py`
- Modify: `software/tests/integration/conftest.py`
- Modify: `software/tests/conftest.py`

- [ ] **Step 1: Update `OpenChamberOperations.set_temperature`**

In `software/fluidics/open_chamber_operations.py`:

1. Change the import line at top from `from time import sleep, time` to:

```python
from time import sleep
from .experiment_worker import AbortRequested, OperationError
from . import sequence_utils
```

2. Replace the `set_temperature` method (lines around 276–292) with:

```python
    def set_temperature(self, target):
        sequence_utils.set_temperature(self.tc, target)
```

3. The dispatch in `process_sequence` already calls `self.set_temperature(sequence['temperature'])` — leave it.

- [ ] **Step 2: Update integration `conftest.py` to pass channels=2 explicitly**

In `software/tests/integration/conftest.py`, change line 47:

```python
    tc = TCMControllerSimulation(channels=2)
```

(Explicit, even though it matches the default — keeps intent clear.)

- [ ] **Step 3: Update the global conftest's time-patch list**

In `software/tests/conftest.py`, the existing line:

```python
    monkeypatch.setattr("fluidics.open_chamber_operations.time", fake_time_fn)
```

needs to be removed because `open_chamber_operations` no longer imports `time`. Replace it with `raising=False` so it tolerates the absence:

```python
    monkeypatch.setattr("fluidics.open_chamber_operations.time", fake_time_fn, raising=False)
```

(Belt-and-braces — keeping it doesn't hurt if a future change re-introduces the import.)

- [ ] **Step 4: Run open chamber integration tests**

```bash
python -m pytest tests/integration/test_open_chamber_operations.py -v
```

Expected: all 9 tests pass, including `test_set_temperature` (which now converges immediately because the simulation reports actual=target).

- [ ] **Step 5: Commit**

```bash
git add software/fluidics/open_chamber_operations.py \
        software/tests/integration/conftest.py \
        software/tests/conftest.py
git commit -m "refactor(open_chamber): delegate set_temperature to shared helper"
```

---

## Task 5: Add `set_temperature` to Flow Cell sequence types

**Files:**
- Modify: `software/fluidics/sequences.py:102`
- Modify: `software/tests/unit/test_sequences.py`

- [ ] **Step 1: Write failing test**

Append to `class TestRegistryConsistency` in `software/tests/unit/test_sequences.py`:

```python
    def test_flow_cell_includes_set_temperature(self):
        assert "set_temperature" in APPLICATION_SEQUENCES["Flow Cell"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/unit/test_sequences.py::TestRegistryConsistency::test_flow_cell_includes_set_temperature -v
```

Expected: AssertionError (set_temperature is not currently in the Flow Cell list).

- [ ] **Step 3: Update `APPLICATION_SEQUENCES`**

In `software/fluidics/sequences.py`, change the Flow Cell entry (around line 102):

```python
APPLICATION_SEQUENCES: dict[str, list[str]] = {
    "Flow Cell": ["flow_reagent", "priming", "clean_up", "set_temperature"],
    "Open Chamber": [
        "add_reagent",
        "clear_and_add_reagent",
        "wash_constant_flow",
        "priming",
        "clean_up",
        "set_temperature",
    ],
}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/unit/test_sequences.py -v
```

Expected: all sequence tests pass.

- [ ] **Step 5: Commit**

```bash
git add software/fluidics/sequences.py software/tests/unit/test_sequences.py
git commit -m "feat(sequences): allow set_temperature in Flow Cell experiments"
```

---

## Task 6: Add `set_temperature` handling to `MERFISHOperations`

**Files:**
- Modify: `software/fluidics/merfish_operations.py`
- Modify: `software/tests/integration/conftest.py`
- Modify: `software/tests/integration/test_merfish_operations.py`

- [ ] **Step 1: Add a fixture and failing tests**

In `software/tests/integration/conftest.py`, append:

```python
@pytest.fixture
def flow_cell_hardware_with_tc(flow_cell_config):
    """Return (config, sp, sv, tc) for flow cell with a 1-channel temperature controller."""
    _fc, sp, sv = _make_sim_hardware(flow_cell_config)
    tc = TCMControllerSimulation(channels=1)
    return flow_cell_config, sp, sv, tc
```

In `software/tests/integration/test_merfish_operations.py`, append a new test class (under existing `TestProcessSequence`):

```python
class TestSetTemperature:
    @pytest.fixture
    def ops_with_tc(self, flow_cell_hardware_with_tc):
        config, sp, sv, tc = flow_cell_hardware_with_tc
        return MERFISHOperations(config, sp, sv, temperature_controller=tc)

    def test_set_temperature(self, ops_with_tc):
        seq = {"type": "set_temperature", "temperature": 37}
        ops_with_tc.process_sequence(seq)
        assert ops_with_tc.tc.target_temperatures == [37]

    def test_set_temperature_without_controller_no_raise(self, flow_cell_hardware):
        config, sp, sv = flow_cell_hardware
        ops = MERFISHOperations(config, sp, sv)
        seq = {"type": "set_temperature", "temperature": 37}
        ops.process_sequence(seq)  # should not raise
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/integration/test_merfish_operations.py::TestSetTemperature -v
```

Expected: failures — `MERFISHOperations.__init__()` does not accept `temperature_controller`, and `process_sequence` does not handle `set_temperature`.

- [ ] **Step 3: Update `MERFISHOperations`**

Replace the top of `software/fluidics/merfish_operations.py`:

```python
from time import sleep
from .experiment_worker import AbortRequested, OperationError
from . import sequence_utils


class MERFISHOperations():
    def __init__(self, config, syringe_pump, selector_valves, temperature_controller=None):
        self.config = config
        self.sp = syringe_pump
        self.sv = selector_valves
        self.tc = temperature_controller
        self.extract_port = self.config.syringe_pump.extract_port
        self.speed_code_limit = self.config.syringe_pump.speed_code_limit

    def process_sequence(self, sequence):
        print(sequence)
        seq_type = sequence['type']

        if seq_type == "flow_reagent":
            self.flow_reagent(
                sequence['fluidic_port'],
                sequence['flow_rate'],
                sequence['volume'],
                sequence.get('fill_tubing_with'))
        elif seq_type in ("priming", "clean_up"):
            self.priming_or_clean_up(
                sequence['fluidic_port'],
                sequence['flow_rate'],
                sequence['volume'],
                sequence.get('use_ports'))
        elif seq_type == "set_temperature":
            sequence_utils.set_temperature(self.tc, sequence['temperature'])
        else:
            raise ValueError(f"Unknown sequence type: {seq_type}")
```

(The rest of the file — `_empty_syringe_pump_on_full`, `flow_reagent`, `priming_or_clean_up` — is unchanged.)

- [ ] **Step 4: Run integration tests for MERFISH**

```bash
python -m pytest tests/integration/test_merfish_operations.py -v
```

Expected: all tests pass, including the two new `TestSetTemperature` cases.

- [ ] **Step 5: Commit**

```bash
git add software/fluidics/merfish_operations.py \
        software/tests/integration/conftest.py \
        software/tests/integration/test_merfish_operations.py
git commit -m "feat(flow_cell): handle set_temperature in MERFISHOperations"
```

---

## Task 7: Update `run_sequences.py` to pass the temperature controller to `MERFISHOperations`

**Files:**
- Modify: `software/run_sequences.py:97`

- [ ] **Step 1: Update the Flow Cell branch**

In `software/run_sequences.py`, change line 97 from:

```python
            experiment_ops = MERFISHOperations(config, syringePump, selectorValveSystem)
```

to:

```python
            experiment_ops = MERFISHOperations(config, syringePump, selectorValveSystem, temperatureController)
```

- [ ] **Step 2: Smoke-test in simulation**

```bash
cd software
python run_sequences.py --path sample_sequences/merfish-experiment.yaml \
                       --config sample_config/flow_cell_config.yaml \
                       --simulation
```

Expected: runs to completion without error. (Existing flow_cell_config.yaml has no `temperature_controller`, so `temperatureController` is `None` — `MERFISHOperations` accepts that.)

- [ ] **Step 3: Run the full pytest suite**

```bash
python -m pytest -q
```

Expected: every test passes.

- [ ] **Step 4: Commit**

```bash
git add software/run_sequences.py
git commit -m "feat(cli): pass temperature controller to MERFISHOperations"
```

---

## Task 8: Rewrite the GUI temperature widget

**Files:**
- Modify: `software/gui.py`

This task replaces `TemperatureControlWidget` (lines 796–1096) with a per-channel sub-widget plus a thin container, and centralizes controller cleanup via `TCMController.close()`.

- [ ] **Step 1: Replace `TemperatureControlWidget`**

In `software/gui.py`, delete the entire existing `class TemperatureControlWidget` (and its inner methods) starting at line 796 and ending at line 1096. Insert the following in its place:

```python
class TemperatureChannelWidget(QWidget):
    """One channel's worth of temperature UI: target/actual readout, plot,
    record toggle, query interval, window size."""

    reading_signal = pyqtSignal(float, float)  # (temp, current_time)

    def __init__(self, controller, channel, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.channel = channel  # 1-based

        self.temps = []
        self.times = []
        self.targets = []
        self.query_interval = 2
        self.window_size = 60
        self.last_update = 0
        self.file = None
        self.writer = None

        self.reading_signal.connect(self._on_reading)

        self._build_ui()
        self.temp_input.setText(f"{self.controller.target_temperatures[channel - 1]:.2f}")

    def _build_ui(self):
        layout = QVBoxLayout(self)

        control = QGroupBox(f"Channel {self.channel} Control")
        control_layout = QVBoxLayout()

        row = QHBoxLayout()
        self.temp_label = QLabel("0.0°C")
        self.temp_input = QLineEdit()
        self.set_btn = QPushButton("Set")
        self.save_btn = QPushButton("Save")
        row.addWidget(QLabel("Current:"))
        row.addWidget(self.temp_label)
        row.addWidget(QLabel("Target:"))
        row.addWidget(self.temp_input)
        row.addWidget(QLabel("°C"))
        row.addWidget(self.set_btn)
        row.addWidget(self.save_btn)
        control_layout.addLayout(row)
        control.setLayout(control_layout)

        plot_box = QGroupBox(f"Channel {self.channel} Plot")
        plot_layout = QVBoxLayout()

        plot_controls = QWidget()
        pc_layout = QHBoxLayout(plot_controls)
        pc_layout.addWidget(QLabel("Query Interval:"))
        self.interval_input = QSpinBox()
        self.interval_input.setMinimum(2)
        self.interval_input.setValue(2)
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

        layout.addWidget(control)
        layout.addWidget(plot_box)

        self.set_btn.clicked.connect(self._set_clicked)
        self.save_btn.clicked.connect(self._save_clicked)
        self.record_btn.clicked.connect(self._toggle_record)
        self.interval_input.valueChanged.connect(self._set_interval)
        self.window_input.valueChanged.connect(self._set_window)

    def _set_interval(self, value):
        self.query_interval = value

    def _set_window(self, value):
        self.window_size = value
        self._refresh_plot()

    def _on_reading(self, temp, current_time):
        if current_time - self.last_update < self.query_interval:
            return
        self.temp_label.setText(f"{temp:.1f}°C")
        target = self.controller.target_temperatures[self.channel - 1]
        self.temps.append(temp)
        self.targets.append(target)
        self.times.append(current_time)
        if self.writer is not None:
            self.writer.writerow([datetime.fromtimestamp(current_time), temp, target])
        while self.times and current_time - self.times[0] > self.window_size:
            self.times.pop(0)
            self.temps.pop(0)
            self.targets.pop(0)
        self._refresh_plot()
        self.last_update = current_time

    def _refresh_plot(self):
        if not self.temps or not self.times:
            return
        ax = self.canvas.axes
        ax.clear()
        ax.plot(self.times, self.temps, "b-", label="Actual")
        ax.plot(self.times, self.targets, "r--", label="Target")
        y_min = min(min(self.temps), min(self.targets))
        y_max = max(max(self.temps), max(self.targets))
        padding = (y_max - y_min) * 0.1 if y_max != y_min else 1.0
        ax.set_ylim([y_min - padding, y_max + padding])
        current_time = self.times[-1]
        ax.set_xlim([current_time - self.window_size, current_time])
        ax.set_xlabel("Seconds Ago")
        ax.set_ylabel("Temperature (°C)")
        ax.set_title(f"Channel {self.channel} Temperature")
        ax.grid(True)
        ax.legend()
        ax.set_xticklabels([f"{x:.0f}" for x in current_time - ax.get_xticks()])
        self.canvas.draw()

    def _set_clicked(self):
        try:
            t = float(self.temp_input.text())
            self.controller.set_target_temperature(self.channel, t)
        except ValueError:
            print(f"Invalid temperature for channel {self.channel}")

    def _save_clicked(self):
        self.controller.save_target_temperature(self.channel)

    def _toggle_record(self):
        if self.record_btn.text() == "Start Recording":
            self.record_btn.setText("Stop Recording")
            filename = f"temp_ch{self.channel}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            self.file = open(filename, "w", newline="")
            self.writer = csv.writer(self.file)
            self.writer.writerow(["Time", "Actual Temperature", "Target Temperature"])
        else:
            self.record_btn.setText("Start Recording")
            if self.file is not None:
                self.file.close()
                self.file = None
                self.writer = None

    def close_recording(self):
        if self.file is not None:
            self.file.close()
            self.file = None
            self.writer = None


class TemperatureControlWidget(QWidget):
    """Container that lays out one TemperatureChannelWidget per channel."""

    readings_signal = pyqtSignal(list)  # list[float] of length controller.channels

    def __init__(self, controller):
        super().__init__()
        self.controller = controller

        layout = QHBoxLayout(self)
        self.channel_widgets = []
        for c in range(1, controller.channels + 1):
            cw = TemperatureChannelWidget(controller, c)
            self.channel_widgets.append(cw)
            layout.addWidget(cw)

        self.readings_signal.connect(self._fanout)
        self.controller.temperature_updating_callback = self._on_callback
        self.controller.actual_temp_updating_thread.start()

    def _on_callback(self, temps):
        # Runs in the controller's polling thread; marshal to the GUI thread.
        self.readings_signal.emit(list(temps))

    def _fanout(self, temps):
        current_time = datetime.now().timestamp()
        for cw, t in zip(self.channel_widgets, temps):
            cw.reading_signal.emit(t, current_time)

    def closeEvent(self, event):
        for cw in self.channel_widgets:
            cw.close_recording()
        event.accept()
```

- [ ] **Step 2: Update `FluidicsControlGUI.closeEvent` to use `controller.close()`**

In `software/gui.py`, replace these lines (approx 1169–1172):

```python
        if self.temperatureController is not None:
            self.temperatureController.terminate_temperature_updating_thread = True
            self.temperatureController.actual_temp_updating_thread.join()
            self.temperatureController.serial.close()
```

with:

```python
        if self.temperatureController is not None:
            self.temperatureController.close()
```

- [ ] **Step 3: Smoke-test the GUI in simulation**

Use the existing local `software/config.yaml` (which has no `temperature_controller` yet). Then create a temporary one to verify the temperature tab renders for Flow Cell:

```bash
cd software
python gui.py --simulation
```

Expected: GUI launches and "Run Experiments" / "Settings and Manual Control" tabs work. No "Temperature Control" tab (config has no temp controller yet).

Edit `software/config.yaml` to add a temperature_controller block (Task 9 will do this anyway, but you can do it sooner to test). With `channels: 1`, relaunch the GUI and confirm:
- "Temperature Control" tab appears.
- It shows exactly one channel panel (not two).
- "Set" / "Save" / "Start Recording" buttons function.
- Closing the window does not hang (means `close()` works).

Then revert any temporary edits to `config.yaml` if you want to test the no-controller path again.

- [ ] **Step 4: Run the full test suite again**

```bash
python -m pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add software/gui.py
git commit -m "refactor(gui): per-channel temperature widget supports 1- or 2-channel devices"
```

---

## Task 9: Update local `software/config.yaml`

This file is untracked and is **not** committed.

- [ ] **Step 1: Insert `temperature_controller` block before the `application:` line**

Edit `software/config.yaml`:

```yaml
temperature_controller:
  serial_number: CHANGE_ME
  channels: 1
  tolerance_celsius: 1.0
  stabilization_timeout_seconds: 300
application: Flow Cell
```

- [ ] **Step 2: Verify it loads**

```bash
cd software
python -c "from fluidics.control.config import load_config; \
           c = load_config('config.yaml'); \
           print(c.application, c.temperature_controller)"
```

Expected output ends with something like `Flow Cell channels=1 ...`.

- [ ] **Step 3: Do not commit**

Confirm `git status` still shows `software/config.yaml` as untracked. No commit.

---

## Task 10: Final verification

- [ ] **Step 1: Full test suite**

```bash
cd software
python -m pytest -v
```

Expected: every test passes (unit + integration). Hardware tests in `tests/hardware/` are excluded by default.

- [ ] **Step 2: CLI simulation, Flow Cell with temperature**

Replace the placeholder serial number with any non-empty string and try the CLI in simulation (which uses `TCMControllerSimulation` regardless of the serial number):

```bash
python run_sequences.py \
    --path sample_sequences/merfish-experiment.yaml \
    --config config.yaml \
    --simulation
```

Expected: runs to completion. If `merfish-experiment.yaml` does not contain `set_temperature` steps yet, this only confirms wiring; you can drop a `set_temperature` step into a copy of the YAML to exercise the helper end-to-end.

- [ ] **Step 3: CLI simulation, Open Chamber regression**

```bash
python run_sequences.py \
    --path sample_sequences/open-chamber-experiment.yaml \
    --config sample_config/open_chamber_config.yaml \
    --simulation
```

Expected: open chamber sequences (including any `set_temperature` step) run unchanged.

- [ ] **Step 4: GUI simulation**

```bash
python gui.py --simulation
```

Expected: GUI launches with the Temperature Control tab showing a single channel panel for the Flow Cell config.

- [ ] **Step 5: Verify branch state**

```bash
git status
git log --oneline main..HEAD
```

Expected: clean tree (apart from untracked `software/config.yaml`); commit history shows the eight feature commits from Tasks 1–8.

---

## Self-review notes

- **Spec coverage:**
  - Section 1 (config) → Task 1.
  - Section 2 (controller rewrite) → Task 2.
  - Section 3 (helper + wiring) → Tasks 3, 4, 6, 7.
  - Section 4 (sequence types) → Task 5.
  - Section 5 (GUI) → Task 8.
  - Section 6 (tests) → covered inline in Tasks 1, 2, 3, 4, 5, 6.
  - Section 7 (local config.yaml) → Task 9.
  - Section 8 (branch) → Pre-task.
- **Naming consistency:** `target_temperatures` / `actual_temperatures` (lists), `channels` (int), `tolerance_celsius` / `stabilization_timeout_seconds` (floats), `set_target_temperature(channel: int, t)`. These match across Tasks 2, 3, 6, 7, 8.
- **Test stability between tasks:** integration tests are red after Task 2 and green again after Task 4. Unit tests stay green throughout.
