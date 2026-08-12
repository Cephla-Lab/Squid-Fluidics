# Flow Cell Temperature Controller Support — Design

**Date:** 2026-05-03
**Branch:** `flow-cell-temperature-controller`

## Problem

Today the Fluidics v2 software only supports the 2-channel TCM temperature controller, and only on the Open Chamber pathway. We have a second physical device — a 1-channel variant of the same TCM controller — that we want to use with the Flow Cell pathway. The wire protocol is identical to the 2-channel device; only the channel count and physical wiring differ.

Currently:

- `fluidics/control/temperature_controller.py` hard-codes 2 channels (`target_temperature_ch1`/`ch2`, `t1`/`t2`, `'TC1'`/`'TC2'` strings).
- `fluidics/sequences.py` `APPLICATION_SEQUENCES["Flow Cell"]` does not include `set_temperature`.
- `MERFISHOperations` has no `set_temperature` handler; only `OpenChamberOperations` does.
- `gui.py` `TemperatureControlWidget` is hard-coded for 2 channels with parallel `_1`/`_2` member variables.
- The `OpenChamberOperations.set_temperature` polling loop logs a warning and continues silently on timeout — a quiet failure mode.

## Goals

1. Support 1-channel and 2-channel TCM controllers via a single unified driver class.
2. Allow either application (Flow Cell, Open Chamber) to use a temperature controller of either channel count.
3. Add `set_temperature` as a valid Flow Cell sequence type, with the same semantics as Open Chamber.
4. Make the temperature stabilization tolerance and timeout configurable.
5. On timeout, raise `OperationError` so the experiment stops, instead of logging and continuing.
6. Refactor the GUI temperature widget to render N channels via a per-channel sub-widget, removing the `_1`/`_2` duplication.
7. Update local `software/config.yaml` to include a 1-channel temperature controller block.

## Non-goals

- Per-sequence overrides for tolerance / timeout (YAGNI; add later if needed).
- Changing the wire protocol or framing.
- Touching hardware test scripts in `tests/hardware/`.
- Committing `software/config.yaml` (untracked local-config file).

## Design

### 1. Config schema

Update `TemperatureControllerConfig` in `fluidics/control/config.py`:

```python
class TemperatureControllerConfig(BaseModel):
    serial_number: str
    channels: Literal[1, 2] = 2
    tolerance_celsius: float = Field(default=1.0, gt=0)
    stabilization_timeout_seconds: float = Field(default=300, gt=0)
```

- `channels` defaults to `2` so existing Open Chamber YAML files (which omit the field) load unchanged.
- Legacy JSON conversion (`convert_legacy_config`) needs no changes — old open-chamber JSONs still produce the right schema, with `channels` picked up from the default.

### 2. Unified `TCMController`

Replace `TCMController` and `TCMControllerSimulation` in `fluidics/control/temperature_controller.py` with a single class parameterized by channel count.

**Public API:**

```python
class TCMController:
    def __init__(self, sn, channels=2, tolerance_celsius=1.0,
                 stabilization_timeout_seconds=300, baud_rate=57600, timeout=0.5):
        ...

    # 1-based channel index in [1, self.channels]
    def set_target_temperature(self, channel: int, t: float) -> None: ...
    def get_target_temperature(self, channel: int) -> float: ...
    def get_actual_temperature(self, channel: int) -> float: ...
    def save_target_temperature(self, channel: int) -> None: ...

    def close(self) -> None:
        """Terminate polling thread, join, and close serial port."""

    # Attributes:
    # channels: int
    # tolerance_celsius: float
    # stabilization_timeout_seconds: float
    # target_temperatures: list[float]   # length == channels
    # actual_temperatures: list[float]   # length == channels
    # is_aborted: bool
    # temperature_updating_callback: Optional[Callable[[list[float]], None]]
```

**Internal mapping:** `channel: int → "TC{channel}"` for the wire commands. Channel range-check (`1 <= channel <= self.channels`) raises `ValueError`. The wire protocol error path (response status not `1` or `8`) is unchanged.

**Background thread:** `update_temperature` loops while `terminate_temperature_updating_thread` is False, sleeps 1 s, polls every channel, updates `actual_temperatures`, and fires the callback once with the full list.

**Callback signature change:** from `(t1, t2)` to `(temps: list[float])` of length `channels`. The GUI widget is rewritten in section 4.

**`TCMControllerSimulation`:** mirrors the same constructor and methods. Tracks target per channel and returns the latest target as the actual reading (so the stabilization helper terminates promptly in tests). Defaults: `target_temperatures = [10.0] * channels`, `actual_temperatures` follows the latest target after each `set_target_temperature` call.

### 3. Shared sequence helper

New file `fluidics/sequence_utils.py`:

```python
from time import sleep, time
from .experiment_worker import OperationError

def set_temperature(tc, target: float) -> None:
    if tc is None:
        print("No temperature controller found. Skipping temperature control sequence.")
        return

    for channel in range(1, tc.channels + 1):
        tc.set_target_temperature(channel, target)

    start_time = time()
    while True:
        sleep(1)
        if all(abs(t - target) <= tc.tolerance_celsius for t in tc.actual_temperatures):
            return
        if tc.is_aborted:
            return
        if time() - start_time > tc.stabilization_timeout_seconds:
            raise OperationError(
                f"Temperature failed to stabilize within "
                f"{tc.stabilization_timeout_seconds}s "
                f"(target={target}, actual={tc.actual_temperatures})"
            )
```

**Wiring:**

- `OpenChamberOperations.set_temperature` is deleted. The `process_sequence` branch for `set_temperature` now calls `sequence_utils.set_temperature(self.tc, sequence['temperature'])`.
- `MERFISHOperations.__init__` gains an optional `temperature_controller=None` parameter (and stores it as `self.tc`).
- `MERFISHOperations.process_sequence` gains a `set_temperature` branch that calls the same helper.
- `run_sequences.py` and `gui.py` pass the temperature controller into `MERFISHOperations` (today they only pass it to `OpenChamberOperations`).

### 4. Sequence types

Update `fluidics/sequences.py`:

```python
APPLICATION_SEQUENCES = {
    "Flow Cell": ["flow_reagent", "priming", "clean_up", "set_temperature"],
    "Open Chamber": [...]  # unchanged
}
```

The existing `SetTemperatureSequence` model and `SEQUENCE_TYPE_LABELS` entry already exist and are reused as-is.

### 5. GUI widget refactor

Replace `TemperatureControlWidget` in `gui.py` with a thin container plus a per-channel sub-widget.

**`TemperatureChannelWidget(QWidget)`** — owns one channel's UI and state:

- Members: `temps`, `times`, `targets`, `query_interval`, `window_size`, `last_update`, `canvas`, `temp_label`, `temp_input`, `set_btn`, `save_btn`, `record_btn`, `interval_input`, `window_input`, optional `file`/`writer`.
- Knows its 1-based `channel` index and holds a reference to the shared `TCMController`.
- Methods: `update_reading(temp, current_time)`, `_update_plot()`, `set_temp_clicked()`, `save_temp_clicked()`, `toggle_record()`, `close()`.

**`TemperatureControlWidget`** becomes a small container:

- Iterates `range(1, controller.channels + 1)` and creates one `TemperatureChannelWidget` per channel in an `HBoxLayout`.
- Connects to `controller.temperature_updating_callback` once. The callback receives `temps: list[float]` and forwards `temps[i-1]` to each child widget.
- `closeEvent` iterates children to close their CSV files.

**Tab visibility:** unchanged — `FluidicsControlGUI.initUI` already adds the Temperature Control tab when `self.temperatureController is not None`. With Flow Cell now also able to instantiate a controller, the tab appears for it too.

**Cleanup centralization:** `FluidicsControlGUI.closeEvent` currently inlines `terminate_temperature_updating_thread = True`, thread join, and `serial.close()`. Replace those three lines with `self.temperatureController.close()`.

### 6. Tests

**Unit (`tests/unit/control/test_config.py`):**

- New: a Flow Cell config with a `temperature_controller` block (`channels=1`) loads and validates.
- New: `tolerance_celsius` and `stabilization_timeout_seconds` defaults populate when omitted; explicit values override.
- New: `channels` defaults to `2` when omitted; `channels: 3` is rejected.
- Existing legacy-conversion tests stay green.

**Unit (`tests/unit/test_sequences.py`):**

- New: assert `set_temperature` is in `APPLICATION_SEQUENCES["Flow Cell"]`.

**Integration (`tests/integration/`):**

- `test_open_chamber_operations.py`: existing `test_set_temperature` keeps passing under the new helper. Fixture instantiates `TCMControllerSimulation(channels=2)`.
- `test_merfish_operations.py`: new `test_set_temperature` mirroring the open-chamber test, using `TCMControllerSimulation(channels=1)` injected into the `MERFISHOperations` fixture.
- `conftest.py`: `merfish_ops` fixture gains a temperature_controller param; existing tests pass `None`.

**Hardware (`tests/hardware/`):** untouched.

### 7. Local config update

Append to `software/config.yaml` (before `application:`):

```yaml
temperature_controller:
  serial_number: CHANGE_ME
  channels: 1
  tolerance_celsius: 1.0
  stabilization_timeout_seconds: 300
application: Flow Cell
```

The tolerance and timeout lines are redundant with the schema defaults but explicit so they are visible as knobs. This file is untracked and is not committed.

### 8. Branch

All work happens on `flow-cell-temperature-controller`, branched from `main`. Code/test changes are committed; `software/config.yaml` edits are not.

## Risk and rollback

- Existing Open Chamber YAML configs continue to work because `channels` defaults to 2 and the wire protocol is unchanged.
- The callback signature change `(t1, t2) → (temps: list)` is a hard breaking change inside the module, but the only consumer is the `TemperatureControlWidget` which is rewritten in the same change.
- The "raise on timeout" change is intentional — failing loud surfaces miswired controllers or runaway thermal loads instead of letting an experiment continue at the wrong temperature.
- Rollback: drop the branch.
