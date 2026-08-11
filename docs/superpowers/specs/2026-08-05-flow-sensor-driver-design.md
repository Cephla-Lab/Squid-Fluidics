# Flow Sensor Driver — Design

**Date:** 2026-08-05
**Branch:** `flow-sensor-driver`

## Problem

The Teensy firmware already drives a Sensirion SLF3S-0600F liquid flow sensor and streams its reading in every status packet, but the software does nothing with it beyond writing a CSV column. We want to:

1. Enable the flow sensor from the config file.
2. Read it continuously in a background thread and plot it live in the GUI, the way the temperature controller works.
3. Use it to protect syringe-pump draws in MERFISH experiments — stop the draw and raise an error when the measured flow is wrong.

### What the firmware already does

Established by reading `firmware/SLF3X.{h,cpp}`, `firmware/controller_teensy41.ino`, `firmware/_defs.h`, and the
[SLF3S-0600F datasheet](https://sensirion.com/media/documents/C4F8D965/65290BC3/LQ_DS_SLF3S-0600F_Datasheet.pdf) (v1.2, Oct 2023).

- **Init:** `INITIALIZE_FLOW_SENSOR` (command 3), 6-byte payload — `buffer[3]` = I²C bus index (0=`Wire`, 1=`Wire1`, 2=`Wire2`), `buffer[4]` = medium (`0x08` water / `0x15` IPA), `buffer[5]` = `do_crc`. Returns `COMPLETED_WITHOUT_ERRORS` or `CMD_EXECUTION_ERROR`. The software-side encoder already exists at `controller.py:378`.
- **Streaming:** no read command. `sendStatusPacket()` (`controller_teensy41.ino:400`) fires every `TX_INTERVAL_MS` = 60 ms and packs the raw flow into **bytes 23–24** as a big-endian `int16`. Bytes 25–26 ("flow sensor 2") are hardcoded 0.
- **Scaling:** µL/min = raw / 10.0. Already parsed into `recorded_data['flowrates']` at `controller.py:272`. **The wire protocol needs no changes for a single sensor.**
- **Not transmitted:** the sensor also returns temperature and a signaling-flags word (air-in-line, high-flow, smoothing-active). Firmware uses the air-in-line bit internally for bubble debounce (`:133`) but neither temperature nor flags reach the host.

### Findings that shape the design

**`INT16_MAX` is an error sentinel, not a flow rate.** `SLF3X::read()` pre-fills its output with `INT16_MAX` and returns early when the sensor was never initialized (`init == false`) or the I²C read short-reads. An absent sensor therefore streams `0x7FFF`, which the current parser reports as a plausible-looking **3276.7 µL/min**. The driver must treat raw 32767 as invalid.

**Sentinel and saturation do not collide.** The datasheet gives the sensor output limit as **±3250 µL/min** (raw ±32500) and full scale as **±2000 µL/min**. The sentinel is 32767. They are 267 counts apart, so "saturated" and "no reading" are distinguishable.

**Monitoring cannot work at current sequence flow rates.** `sample_sequences/merfish-experiment.yaml` runs `flow_reagent` at 5000 µL/min and `clean_up` at 10000 µL/min — far above the 3250 µL/min output limit. Above that the reading is meaningless, so the monitor skips the check and warns rather than producing false faults.

**Expected rate must come from the speed code, not the sequence.** `flow_rate_to_speed_code()` snaps the requested rate to a discrete code, and `get_flow_rate()` returns **mL/min** (see the `"{rate} mL/min"` label at `gui.py:614`) while sequence `flow_rate` is **µL/min**. The monitor compares against `sp.get_flow_rate(speed_code) * 1000`.

**`do_crc` currently does nothing.** On CRC failure `SLF3X::read()` sets bits in its *return value*, but `readings[]` already holds the corrupted data (written at `SLF3X.cpp:207-213`, before the check at `:219-225`), and both call sites — `controller_teensy41.ino:132` and `:486` — discard the return value. `PERFORM_CRC` in `_defs.h:37` is defined and never referenced.

**Reads are consuming, and there are two of them.** Per datasheet §3.1 a read returns the average of all 0.5 ms samples *since the previous readout*; per §3.3 the signaling flags latch until read, then clear. The firmware reads at both 20 ms (`:132`) and 60 ms (`:486`), so the averaging window behind any given packet varies, and the control loop consumes the flags before the transmit path sees them.

**Bus topology.** The SLF3X address is hard-wired to 0x08 (datasheet §4.1), so one sensor per I²C bus; the Teensy 4.1 has three. `_defs.h:29` puts `SELECTORVALVE_WIRE` on `Wire` and `:34` puts `SLF3X_WIRE0` on `Wire` — the same peripheral. `SLF3X::begin()` issues a real general-call reset (`SLF3X.cpp:114-118`) that every device on the bus sees, and `RheoLink.cpp:161-162` emits a zero-length general-call transaction after *every* valve command as a firmware workaround. **Index 0 is therefore excluded from scope**; Wire1 and Wire2 are private to flow sensors.

**Serial contention.** `get_mcu_status()` reads the port directly and mutates `self.read_buffer`; `wait_for_completion()` busy-polls it and `SelectorValve.open()` calls it. A background reading thread calling it concurrently would corrupt COBS frames.

## Goals

1. Optional `flow_sensors` config section enabling one or two sensors on I²C indices 1 and 2.
2. A single reader thread in `FluidController` that owns the serial port, eliminating the read race.
3. A `FlowSensor` driver exposing the live reading, with `INT16_MAX` mapped to "invalid".
4. A `FlowMonitor` holding the fault rule as a pure, testable unit, with configurable ramp-up and tolerance.
5. A GUI tab per configured sensor: live readout, plot, CSV recording.
6. Simulation counterparts so `--simulation` and the test suite work without hardware.

## Non-goals

- **Index 0 / the `Wire` bus.** Excluded because of the selector-valve general-call interaction above.
- **MERFISH operations changes.** Designed separately; see "Next" below.
- **Transmitting the signaling flags.** Phase 2 at the earliest; requires the read consolidation.
- **Changing `SLF3X_MAX_VAL_uL_MIN`** (3520 vs the datasheet's 3250). Firmware `:1244` multiplies by it and `controller.py:678` divides by it, so it cancels on the round trip and only bounds the maximum representable `FLUID_OUT_PID` setpoint — which the sensor cannot reach anyway. Cosmetic; leave it.
- **Making `medium` or `crc` configurable.** Both hardcoded — see §1.
- Open Chamber disc-pump flow control, and anything in `tests/hardware/`.

## Design

### 1. Config schema

Two new optional sections in `fluidics/control/config.py`:

```python
class FlowSensorConfig(BaseModel):
    index: Literal[1, 2]
    name: str

class FlowMonitorConfig(BaseModel):
    mode: Literal["off", "warn", "stop"] = "warn"
    sensor: str
    ramp_up_seconds: float = Field(default=3.0, gt=0)
    tolerance_fraction: float = Field(default=0.3, gt=0, le=1)
    max_flow_rate_ul_min: float = Field(default=2000, gt=0)
```

On `FluidicsConfig`: `flow_sensors: Optional[List[FlowSensorConfig]] = None` and `flow_monitor: Optional[FlowMonitorConfig] = None`.

The full (Phase 2) shape, showing both sensors:

```yaml
flow_sensors:
  - index: 1
    name: syringe_draw
  - index: 2
    name: waste_line

flow_monitor:            # omit entirely to disable
  mode: warn             # off | warn | stop
  sensor: syringe_draw
  ramp_up_seconds: 3.0
  tolerance_fraction: 0.3
  max_flow_rate_ul_min: 2000
```

Phase 1 accepts a single entry, so the config that ships first looks like:

```yaml
flow_sensors:
  - index: 1
    name: syringe_draw

flow_monitor:
  mode: warn
  sensor: syringe_draw
```

with the remaining `flow_monitor` fields taking their schema defaults.

Validation: indices unique; `name`s unique; `flow_monitor.sensor` must match a configured `name`; `flow_monitor` without `flow_sensors` is an error. **Phase 1 additionally rejects more than one entry**, with a message stating that two sensors require the Phase 2 firmware.

`index` matches the "I2C index" wording at `controller.py:380` and the `idx` convention `INITIALIZE_ROTARY` uses.

**Why `medium` is not configurable.** It selects the on-chip calibration field (`0x3608` water / `0x3615` IPA). Datasheet Tables 1 and 2 give both an identical ±2000 µL/min full scale and ±3250 µL/min output limit — identical range confirms an identical scale factor, so the firmware's hardcoded ÷10 is right either way. Only the error characteristics differ (±5% water vs ±10% IPA). These reagents are aqueous and `clean_up` uses water, so the driver hardcodes water. One line to add back if a solvent step ever appears.

**Why `crc` is not configurable.** It changes nothing that reaches the host (see Findings). The driver hardcodes `do_crc = True`, matching the intent of the dead `PERFORM_CRC` constant.

**`max_flow_rate_ul_min` default of 2000** is exactly the datasheet's full-scale figure, above which accuracy degrades toward the ±3250 saturation point.

### 2. Serial ownership in `FluidController`

One reader thread becomes the only thing touching the port.

- New: `_status_lock`, `_latest_status` dict, `_status_seq` counter, `_reader_thread`, `start_reading()` / `stop_reading()`, and a `packet_callback` fired per packet.
- `_reader_loop()` performs the existing read-and-parse, stores under the lock, bumps `_status_seq`, fires the callback.
- `get_mcu_status()` returns a snapshot of `_latest_status`, blocking until the first packet arrives. Every existing caller — including `SelectorValve.get_current_position()` — works unchanged.
- The thread runs **unconditionally**, not only when a flow sensor is configured. One code path that is always exercised beats two that diverge.

**`wait_for_completion()` must also match the UID.** Today `discard_buffer=True` flushes to the newest packet, which partly masks a latent bug: a packet already in flight when the command was sent still carries the *previous* command's `COMPLETED` status. A continuously-consuming reader thread makes that worse, so acceptance now requires `MCU_received_command_UID == self.cmd_uid`, plus a timeout (30 s) raising `OperationError` instead of hanging forever. The UID is already parsed at `controller.py:242`. **This is the riskiest edit in the change and needs its own tests.**

Also add the raw `int16` alongside the scaled value in `recorded_data`, so the sentinel test is an exact `raw == 32767` rather than a float comparison.

`FluidControllerSimulation` gains no-op `start_reading()` / `stop_reading()` and the same attributes.

### 3. `FlowSensor`

New `fluidics/control/flow_sensor.py`:

```python
class FlowSensor:
    INVALID_RAW = 32767

    def __init__(self, fluid_controller, packet_slot: int, name: str): ...
    def begin(self) -> None:
        """Send INITIALIZE_FLOW_SENSOR; raise on CMD_EXECUTION_ERROR."""
    @property
    def latest_flow_ul_min(self) -> float | None: ...
    def subscribe(self, callback) -> None:
        """callback(flow_ul_min: float | None, timestamp: float)"""
    def close(self) -> None: ...
```

`begin()` failing loudly at startup is what catches a missing or miswired sensor, instead of letting it stream 3276.7 µL/min forever.

**`packet_slot` vs `index`.** The I²C index identifies the bus; the packet slot identifies which pair of bytes carries the reading. They differ by phase, and keeping them separate keeps the driver dumb — the wiring code in `gui.py` / `run_sequences.py` computes the slot:

- **Phase 1:** the firmware has one sensor object and always transmits it in bytes 23–24, whichever bus it is on. Slot is `0` regardless of index.
- **Phase 2:** slot is `index - 1`, so index 1 → bytes 23–24 and index 2 → bytes 25–26. Deterministic, independent of config ordering.

`FlowSensorSimulation` mirrors the API and runs its own thread, since `FluidControllerSimulation` has no packet stream. It publishes a settable `simulated_flow_ul_min` attribute (default: whatever rate the syringe pump was last asked for, so the nominal path passes). Tests drive that attribute directly to exercise the low-flow, dropout, and sentinel paths — the simulation holds no reference to the monitor, keeping the dependency one-way.

### 4. `FlowMonitor`

Pure fault logic, fed `(flow, timestamp)` samples. Armed around a draw via a context manager:

```python
with monitor.guard(expected_ul_min):
    sp.execute()
```

Rules:

- **Out of range** — if `expected > max_flow_rate_ul_min`, do not arm; log a warning naming the operation and rate. Under the current sample sequences this means the 5000 and 10000 µL/min steps run unmonitored.
- **Ramp-up** — for `ramp_up_seconds` after arming, accumulate samples but never fault.
- **Fault** — after ramp-up, `abs(measured)` outside `expected × (1 ± tolerance_fraction)` for 3 consecutive samples (≈180 ms at the 60 ms cadence).
- **Invalid** — sentinel samples also count toward a fault once past ramp-up: a sensor that died mid-draw means nothing can be verified.

Comparison uses magnitude, so sensor orientation in the line does not matter. `dispense_to_waste()` exits via the waste port while only `extract()` pulls through the extract port, so the sensor between selector valve and syringe sees draws only.

`mode` selects the consequence:

| mode | reads + plots | fault rule runs | acts on fault |
|---|---|---|---|
| `off` | yes | no | no |
| `warn` | yes | yes | logs "would have triggered", pump untouched |
| `stop` | yes | yes | stops the pump, raises `OperationError` |

`warn` exists because tolerance and ramp-up have to be tuned against real hardware before enforcement can be trusted, and reading a log beats eyeballing a chart.

### 5. GUI

`FlowSensorWidget`, built on the existing `MplCanvas` and modelled on `TemperatureChannelWidget`: live readout, plot with query-interval and window-size spin boxes, CSV record toggle. The expected rate draws as a dashed reference line while armed, invalid samples render as gaps rather than 3276.7 spikes, and faults are marked.

A `FlowSensorControlWidget` container fans out one child per configured sensor, exactly as `TemperatureControlWidget` does per channel, and the tab is added only when `config.flow_sensors` is present — the same conditional as the temperature tab at `gui.py:1048`.

### 6. Wiring

`gui.py` and `run_sequences.py` both construct sensors after `controller.begin()`, call `begin()` on each, then `controller.start_reading()`. Teardown closes sensors and stops the reader thread. `run_sequences.py` gets no new CLI flags — config drives everything.

### 7. Tests

**Unit — `tests/unit/control/test_config.py`:** `flow_sensors` / `flow_monitor` load and validate; defaults populate when omitted; `index: 0` and `index: 3` rejected; duplicate index and duplicate name rejected; `flow_monitor.sensor` naming an unknown sensor rejected; `flow_monitor` without `flow_sensors` rejected; two entries rejected in Phase 1. Existing legacy-conversion tests stay green.

**Unit — `tests/unit/control/test_flow_sensor.py`:** raw 32767 → `None`; raw 32500 → 3250.0 (saturated but valid); negative values scale correctly; `begin()` raises on `CMD_EXECUTION_ERROR`.

**Unit — `tests/unit/control/test_flow_monitor.py`:** the fault rule against synthetic sample streams — steady-good never faults; sustained-low faults after ramp-up; a dropout shorter than the debounce does not fault; a fault during ramp-up is suppressed; sentinel runs fault after ramp-up; `expected > max_flow_rate_ul_min` never arms; `warn` does not act while `stop` does.

**Unit — `tests/unit/control/test_controller.py`:** `wait_for_completion()` ignores a stale packet carrying the previous UID; accepts the matching one; raises on timeout.

**Integration:** a simulated fault stops a draw and surfaces as `OperationError` — added with the MERFISH work.

## Phasing

**Phase 1 — software only, no reflash.** Everything above, limited to one sensor on index 1 or 2, reading packet slot 0. Fully useful today, and it is what produces the `warn`-mode logs needed to tune `tolerance_fraction` and `ramp_up_seconds`.

**Phase 2 — firmware.** Sensor array following the existing `SSCX_QTY`/`SSCX_MAX` and `SELECTORVALVE_QTY`/`SELECTORVALVE_MAX` pattern; index maps to `Wire1`/`Wire2`; transmit loop fills bytes 23–24 and 25–26. **The packet stays 30 bytes** — two sensors fit the slots that already exist, so `MCU_MSG_LENGTH` is unchanged and there is no protocol bump. Also folds in the read consolidation (single read per cycle into globals, so the averaging window is consistent), CRC-failure sentinel substitution, and the `SLF3X_SMOOTHING_ON` bit fix (`_defs.h:45` has `1 << 4`; datasheet Table 9 says bit 5). Optionally transmits the flags word, which would need new bytes and *would* be a protocol bump — deferred, and only worth it if above-range detection proves necessary.

Phase 2's read consolidation retargets `:133` and `:294`, which feed the bang-bang and PID loops for the Open Chamber disc pump. Mechanical, but live control code — it needs a hardware smoke test on both applications.

## Next: MERFISH operations

Designed separately. One constraint already established: `sp.abort()` latches `is_aborted` until `reset_abort()`, and `flow_reagent` checks `if self.sp.is_aborted: return` after each `execute()` — so reusing the abort path would make a flow fault **silently return** rather than raise. The pump needs a non-latching `stop()` that terminates the current chain and lets `wait_for_stop()` exit, so the fault surfaces as an `OperationError` with a real diagnostic. Open: which operations to guard (`flow_reagent` alone, or `priming_or_clean_up` too, where air in the lines is expected and false positives are likely).

## Risk and rollback

- **`wait_for_completion()` UID matching** is the highest-risk change: every command path depends on it. Mitigated by dedicated unit tests and by the timeout converting a hang into a clear error.
- **Always-on reader thread** changes serial timing for all existing operations. It removes a race rather than adding one, but it touches every command path and wants a hardware smoke test.
- Config sections are optional and absent from existing YAML, so current configs load unchanged.
- `mode: warn` is the recommended starting point; nothing stops a pump until someone sets `stop`.
- Rollback: drop the branch. Phase 1 requires no firmware change, so there is nothing to un-flash.
