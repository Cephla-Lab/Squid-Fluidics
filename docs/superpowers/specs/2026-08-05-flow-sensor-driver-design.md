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

**Real MERFISH runs sit well inside the sensor's range.** Production sequences run at **500 µL/min**, comfortably under the 3250 µL/min output limit and under the 2000 µL/min full-scale figure — so draws are monitorable in practice. Note 500 µL/min is speed code 40, the slowest a 5000 µL syringe reaches, and `get_flow_rate(40)` returns exactly 500, so requested and actual coincide with no rounding.

The rates in `sample_sequences/merfish-experiment.yaml` (5000 and 10000 µL/min) are test values, not representative. They do exceed the sensor's range, which is why the fault rule still needs an out-of-range skip — but that path is the exception, not the normal case.

**Expected rate must come from the speed code, not the sequence.** `flow_rate_to_speed_code()` snaps the requested rate to a discrete code, so the pump's actual rate can differ from what the sequence asked for. The monitor compares against `sp.get_flow_rate(speed_code)`.

That function used to return mL/min while its own inverse took µL/min — a silent factor of 1000 sitting exactly where draw protection would compare measured against expected. Fixed in Phase 1: the pump API is µL/min throughout and the two round-trip, with a test asserting it for all 41 speed codes.

**`do_crc` currently does nothing.** On CRC failure `SLF3X::read()` sets bits in its *return value*, but `readings[]` already holds the corrupted data (written at `SLF3X.cpp:207-213`, before the check at `:219-225`), and both call sites — `controller_teensy41.ino:132` and `:486` — discard the return value. `PERFORM_CRC` in `_defs.h:37` is defined and never referenced.

**Reads are consuming, and there are two of them.** Per datasheet §3.1 a read returns the average of all 0.5 ms samples *since the previous readout*; per §3.3 the signaling flags latch until read, then clear. The firmware reads at both 20 ms (`:132`) and 60 ms (`:486`), so the averaging window behind any given packet varies, and the control loop consumes the flags before the transmit path sees them.

**Bus topology.** The SLF3X address is hard-wired to 0x08 (datasheet §4.1), so one sensor per I²C bus; the Teensy 4.1 has three. `_defs.h:29` puts `SELECTORVALVE_WIRE` on `Wire` and `:34` puts `SLF3X_WIRE0` on `Wire` — the same peripheral. `SLF3X::begin()` issues a real general-call reset (`SLF3X.cpp:114-118`) that every device on the bus sees, and `RheoLink.cpp:161-162` emits a zero-length general-call transaction after *every* valve command as a firmware workaround. **Index 0 is therefore excluded from scope**; Wire1 and Wire2 are private to flow sensors.

**Serial contention.** `get_mcu_status()` reads the port directly and mutates `self.read_buffer`; `wait_for_completion()` busy-polls it and `SelectorValve.open()` calls it. A background reading thread calling it concurrently would corrupt COBS frames.

## Goals

1. Optional `flow_sensors` config section declaring one or two sensors on I²C indices 1 and 2.
2. A single reader thread in `FluidController` that owns the serial port, eliminating the read race.
3. A `FlowSensor` driver exposing the live reading, with `INT16_MAX` mapped to "invalid".
4. A GUI tab per configured sensor: live readout, plot, CSV recording.
5. Simulation counterparts so `--simulation` and the test suite work without hardware.

This is the observability layer: it makes flow readable, plottable, and recordable. Acting on those readings is the next piece of work.

## Non-goals

- **Index 0 / the `Wire` bus.** Excluded because of the selector-valve general-call interaction above.
- **Draw protection behaviour** — the fault rule, arming around a draw, and which operation consults which sensor. The config fields are declared here (§1) so the schema lives in one place, but nothing reads them until the MERFISH operations design lands; see "Next" below.
- **Transmitting the signaling flags.** A firmware change, and out of scope entirely — see "Firmware work that is deliberately not part of this".
- **Changing `SLF3X_MAX_VAL_uL_MIN`** (3520 vs the datasheet's 3250). Firmware `:1244` multiplies by it and `controller.py:678` divides by it, so it cancels on the round trip and only bounds the maximum representable `FLUID_OUT_PID` setpoint — which the sensor cannot reach anyway. Cosmetic; leave it.
- **Making `medium` or `crc` configurable.** Both hardcoded — see §1.
- Open Chamber disc-pump flow control, and anything in `tests/hardware/`.

## Design

### 1. Config schema

One new optional section in `fluidics/control/config.py`:

```python
class FlowSensorConfig(BaseModel):
    index: Literal[1, 2]
    name: str
    monitor: Literal["off", "warn", "stop"] = "off"
    ramp_up_seconds: float = Field(default=3.0, gt=0)
    tolerance_fraction: float = Field(default=0.3, gt=0, le=1)
    max_flow_rate_ul_min: float = Field(default=2000, gt=0)
```

On `FluidicsConfig`: `flow_sensors: Optional[List[FlowSensorConfig]] = None`.

```yaml
flow_sensors:
  - index: 1
    name: syringe_draw
    monitor: warn            # off | warn | stop
    ramp_up_seconds: 3.0
    tolerance_fraction: 0.3
    max_flow_rate_ul_min: 2000
  - index: 2
    name: waste_line
    monitor: off
```

**Each sensor carries its own tuning; the schema still takes no position on roles.** `monitor`, `ramp_up_seconds`, `tolerance_fraction` and `max_flow_rate_ul_min` are per-sensor values because each sensor sits at a different point in the plumbing and sees different flow characteristics — a tolerance that suits the syringe line need not suit the waste line. Two sensors can end up with the same role (both `stop`) or different roles (one `stop`, one `off`), and the schema is neutral either way.

What stays out of config is the *binding*: which sensor a given MERFISH operation consults is decided in `merfish_operations.py`, selecting by `name`. There is deliberately no `sensor:` pointer here — an operation knows which sensor it depends on, and encoding that in config would only let the two disagree.

`monitor` defaults to `off`, so declaring a sensor is a purely additive, read-only act. Enforcement is always opted into explicitly.

Only the fields are defined here. The fault rule that consumes them, and the arming around a draw, belong to the MERFISH operations design — see "Next" below. Until that lands these values are inert.

`name` labels the GUI tab and the CSV filename, and is the handle the operations layer selects by. `index` matches the "I2C index" wording at `controller.py:380` and the `idx` convention `INITIALIZE_ROTARY` uses. **`max_flow_rate_ul_min` defaults to 2000** — exactly the datasheet's full-scale figure, above which accuracy degrades toward the ±3250 saturation point.

Validation: indices unique; `name`s unique. **More than one entry is rejected**, with a message stating that a second sensor requires firmware that populates packet bytes 25–26.

**Why `medium` is not configurable.** It selects the on-chip calibration field (`0x3608` water / `0x3615` IPA). Datasheet Tables 1 and 2 give both an identical ±2000 µL/min full scale and ±3250 µL/min output limit — identical range confirms an identical scale factor, so the firmware's hardcoded ÷10 is right either way. Only the error characteristics differ (±5% water vs ±10% IPA). These reagents are aqueous and `clean_up` uses water, so the driver hardcodes water. One line to add back if a solvent step ever appears.

**Why `crc` is not configurable.** It changes nothing that reaches the host (see Findings). The driver hardcodes `do_crc = True`, matching the intent of the dead `PERFORM_CRC` constant.

### 2. Serial ownership in `FluidController`

One reader thread becomes the only thing touching the port.

- New: `_status_lock`, `_latest_status` dict, `_status_seq` counter, `_reader_thread`, `start_reading()` / `stop_reading()`, and a `packet_callback` fired per packet.
- `_reader_loop()` performs the existing read-and-parse, stores under the lock, bumps `_status_seq`, fires the callback.
- `get_mcu_status()` returns a snapshot of `_latest_status`, blocking until the first packet arrives. Every existing caller — including `SelectorValve.get_current_position()` — works unchanged.
- The thread runs **unconditionally**, not only when a flow sensor is configured. One code path that is always exercised beats two that diverge.

**`wait_for_completion()` must also match the UID.** Today `discard_buffer=True` drains only while `in_waiting > 2 * rx_buffer_length`, so it can return a packet two frames — roughly 120 ms — old, with no guarantee it postdates the command just sent. That leaves a latent bug: a packet already in flight when the command went out still carries the *previous* command's `COMPLETED` status, so `wait_for_completion()` can return before the firmware has started. `SelectorValve.open()` then reads a position the valve has not moved to yet and raises `RuntimeError: current position is X; expected Y`.

Today that race usually loses, because the host polls faster than the firmware produces. A continuously-consuming reader thread makes it deterministic instead: `wait_for_completion()` no longer drains anything, so a call 1 ms after `send_command` reads a snapshot up to 60 ms old that necessarily predates the command.

So acceptance now requires `MCU_received_command_UID == self.cmd_uid`, plus a timeout (30 s) raising `OperationError` rather than hanging. The UID is already parsed at `controller.py:242` and the firmware already echoes it at `controller_teensy41.ino:424-425`, holding it across a long internal program. **This is the riskiest edit in the change and needs its own tests.**

One ordering subtlety the tests must pin: `CMD_SET.CLEAR` resets the counter at `controller.py:357`, but `send_command` increments `cmd_uid` *before* that branch and calls `add_uid_to_cmd` *after* it — so the command goes out with UID 0 and `self.cmd_uid == 0`, and matching works. That is correct by accident of statement order and would break silently if those lines were reordered.

**Alternative considered and rejected: a lock around `get_mcu_status()`.** Keep the direct read, wrap read-and-parse in a `threading.Lock`, and let the flow poller and `wait_for_completion()` take turns. Smaller blast radius, and no UID matching needed since the semantics are untouched. Rejected because it preserves the bug above rather than fixing it, and it still leaves two threads mutating `recorded_data` while each one's `discard_buffer=True` discards packets the other wanted. Starvation of the flow samples turned out not to be the deciding factor — `sp.execute()` talks to the syringe pump over its own port, so nothing polls the Teensy during a draw — but the correctness argument stands on its own.

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

- **Today:** the firmware has one sensor object and always transmits it in bytes 23–24, whichever bus it is on. Slot is `0` regardless of index.
- **If the firmware ever grows a sensor array:** slot becomes `index - 1`, so index 1 → bytes 23–24 and index 2 → bytes 25–26. Deterministic, independent of config ordering.

`FlowSensorSimulation` mirrors the API and runs its own thread, since `FluidControllerSimulation` has no packet stream. It publishes a settable `simulated_flow_ul_min` attribute, defaulting to a plausible steady value. Tests drive that attribute directly to produce low-flow, dropout, and sentinel streams, so the simulation stays a dumb source with no knowledge of any consumer.

### 4. GUI

`FlowSensorWidget`, built on the existing `MplCanvas` and modelled on `TemperatureChannelWidget`: live readout, plot with query-interval and window-size spin boxes, CSV record toggle. Invalid samples render as gaps rather than 3276.7 spikes.

A `FlowSensorControlWidget` container fans out one child per configured sensor, exactly as `TemperatureControlWidget` does per channel, and the tab is added only when `config.flow_sensors` is present — the same conditional as the temperature tab at `gui.py:1048`.

The widget subscribes through `FlowSensor.subscribe()` and knows nothing about draws or expected rates. Overlays that belong to draw protection — an expected-rate reference line, fault markers — are added with that work, not here.

### 5. Wiring

`gui.py` and `run_sequences.py` both construct sensors after `controller.begin()`, call `begin()` on each, then `controller.start_reading()`. Teardown closes sensors and stops the reader thread. `run_sequences.py` gets no new CLI flags — config drives everything.

### 6. Tests

**Unit — `tests/unit/control/test_config.py`:** `flow_sensors` loads and validates; `index: 0` and `index: 3` rejected; duplicate index and duplicate name rejected; two entries rejected in Phase 1; a config with no `flow_sensors` section still loads; `monitor` defaults to `off` and rejects unknown values; `ramp_up_seconds`, `tolerance_fraction` and `max_flow_rate_ul_min` take their defaults when omitted and reject out-of-range values. Existing legacy-conversion tests stay green.

**Unit — `tests/unit/control/test_flow_sensor.py`:** raw 32767 → `None`; raw 32500 → 3250.0 (saturated but valid); negative values scale correctly; `begin()` raises on `CMD_EXECUTION_ERROR`; subscribers receive each packet.

**Unit — `tests/unit/control/test_controller.py`:** `wait_for_completion()` ignores a stale packet carrying the previous UID; accepts the matching one; raises on timeout; `cmd_uid` is 0 on both sides after `CMD_SET.CLEAR`, so the first wait after a clear can match.

**Integration:** simulation-mode startup constructs sensors, starts the reader thread, and shuts down cleanly.

## Phasing

**Any firmware change belongs in Phase 1.** The firmware defines the contract the software is written against, so deferring a protocol change to a later phase means building the software twice — once against the interim packet layout and again after it moves. Phase 1 therefore either includes the firmware work or establishes that none is needed. It established the latter: the reading was already being transmitted, so Phase 1 shipped with zero firmware changes and the contract is settled. There is no firmware phase after this.

**Phase 1 — the driver. Complete.** One sensor on index 1 or 2, read from packet slot 0. No reflash, because the Teensy already streamed the reading in bytes 23–24 and nothing consumed it.

Phase 1 is done when, with a sensor connected and declared in config, you can:

- open the GUI and see a **Flow Sensors** tab with a live µL/min readout updating as flow changes
- watch that reading plotted against a rolling time window, with the window and refresh rate adjustable
- hit **Start Recording** and get a CSV containing every sample at the sensor's full 60 ms cadence, not the plot's refresh rate
- see an absent or failed sensor read as `invalid` and a plot gap, rather than a plausible-looking 3276.7 µL/min

Those traces are also the raw material for choosing Phase 2's tolerance and ramp-up values, which is why this phase comes first: you cannot pick a sensible fault threshold without first seeing what normal looks like on real hardware.

**Phase 2 — MERFISH draw protection. Software only.** The motivating requirement: stop a syringe draw when measured flow is wrong. Everything it needs already exists — one sensor, its readings, and the per-sensor tuning fields in config. Detailed in the next section.

### Firmware work that is deliberately not part of this

Two capabilities would require touching the firmware. Neither is needed for draw protection on a single sensor, so both are out of scope here rather than queued as a later phase. If either is ever wanted, it is its own piece of work with its own design — not a continuation of this one.

Clean design takes priority over staying compatible with pre-flow-sensor software. Firmware and software ship together to the same instrument, so a coordinated upgrade is cheap. Compatibility is worth taking when it is free, and not worth contorting the design for. The notes below say which it is in each case.

**A second sensor — clean and compatible coincide, so just take it.** Index the firmware's sensor array by **slot, not by bus**, and have the host name the slot explicitly: `INITIALIZE_FLOW_SENSOR` keeps its exact-size check, where `size == 6` means slot 0 (byte-identical to today's command) and `size == 7` takes the slot from `buffer[6]`. That is cleaner than deriving slot from bus index — the host states intent instead of the firmware inferring it — and it happens to be compatible for free, because old software emits unpadded 6-byte commands and so can only ever address slot 0. Slot *i* transmits at bytes `23 + 2i`. Only slot 0 feeds `global_flowrate_reading`, volume integration, and the `CLEAR_LINES` guard; slot 1 is telemetry. An unrequested or failed slot transmits `INT16_MAX`, matching what `SLF3X::read()` already produces and what the software already treats as invalid.

**Above-range detection — a real tradeoff.** It needs the SLF3X signaling flags transmitted; they are read at the packet site today and thrown away, so the data is already in hand and the change costs no extra I²C traffic. Two options:

- *Grow the packet.* Structurally the cleaner extension, but it breaks both software versions, and the newer failure is the worse one: old software asserts on length and dies loudly on the first packet, while current software hits `continue` at `controller.py:398`, silently discards every frame, and surfaces a misleading `TimeoutError` about an unrelated command.
- *Use bytes 19–22.* Keeps the packet at 30. Those bytes are genuinely unreachable rather than merely unused — `SSCX_QTY` is 0, so the fill loop hard-zeros 15–22, and pressure sensors 3–4 cannot be wired on any board. **Do not touch bytes 15–18**: they belong to pressure sensors 1–2, which are physically wired on most boards and one `_defs.h` edit away from being live.

The second option is worth taking despite being less tidy, for a reason unrelated to compatibility: the packet has **no version field anywhere**, so a firmware/software skew currently fails silently. Spending one of those four bytes on a layout version is worth more than the fourth flag byte, and this is the last cheap chance to add one.

**Delete the two `if (false)` debug blocks at `controller_teensy41.ino:465-483` in the same commit.** They write bytes 19–22. Left dormant while flags occupy those bytes, a future developer flipping one to `true` would inject PID error terms into what the host decodes as air-in-line.

Either change should fold in the fixes found while reading the firmware: read consolidation (a single read per cycle, so the averaging window is consistent), CRC-failure sentinel substitution, and the `SLF3X_SMOOTHING_ON` bit (`_defs.h:45` has `1 << 4`; datasheet Table 9 says bit 5). The read consolidation retargets `:133` and `:294`, which feed the bang-bang and PID loops for the Open Chamber disc pump — mechanical, but live control code, and it would need a hardware smoke test on both applications.

### Independent firmware bugs found while analysing this

None are caused by this work; all are worth fixing before anyone touches the packet.

- `uint16_t pressure_results[SSCX_QTY]` (`controller_teensy41.ino:125`) is a **zero-length array**, indexed out of bounds at four sites. `global_pressure_reading` and `global_vacuum_reading` are adjacent stack memory. Must be fixed before `SSCX_QTY` is ever raised to reclaim bytes 15–18.
- `SLF3X::begin()` sets `init = true` as its first statement, before any I²C traffic, and neither failure path clears it (`SLF3X.cpp:99, 122, 138`). The presence flag lies, so `CLEAR_LINES` currently passes on a rig with no flow sensor. Fixing it will make that guard start rejecting rigs where a missing sensor previously went unnoticed — ship it as its own change.
- `SELECTORVALVE_QTY` is 2, so bytes 8–10 are zero-filled, yet `sample_config/flow_cell_config.yaml` declares `valve_ids: [0, 1, 2]` and `SelectorValve.open()` raises on a position mismatch. Either the deployed flow-cell firmware is built with `SELECTORVALVE_QTY 3`, or that shipped sample config cannot run.
- `SLF3X_MAX_VAL_uL_MIN` is 3520 in both `_defs.h:40` and `_def.py:18`, but the datasheet gives ±3250 µL/min and the int16 encoding tops out at 3276.7 — which is also the invalid sentinel. So a genuine over-range reading may already be indistinguishable from "no sensor". The `HIGH_FLOW` flag is the disambiguator, which is a further argument for transmitting it.

## Phase 2 detail: MERFISH draw protection

Designed separately from the driver because a sensor's role belongs to the operation that uses it, not to the device. Carried forward from this discussion:

**A `FlowMonitor` holding the fault rule** as a pure function of `(flow, timestamp)` samples — no controller, no thread, no clock — so it is unit-testable in isolation. Armed around a draw, probably as a context manager wrapping `sp.execute()`. The rule as sketched so far:

- **Out of range** — if the expected rate exceeds the sensor's `max_flow_rate_ul_min`, do not arm; log a warning naming the operation and rate. At the 500 µL/min production rate this never fires; it exists for the fast steps in the sample sequences and for any future protocol that runs above full scale.
- **Ramp-up** — for `ramp_up_seconds` after arming, accumulate samples but never fault.
- **Fault** — after ramp-up, `abs(measured)` outside `expected × (1 ± tolerance_fraction)` for 3 consecutive samples (≈180 ms at the 60 ms cadence).
- **Invalid** — sentinel samples count toward a fault once past ramp-up; a sensor that died mid-draw means nothing can be verified.

Comparison uses magnitude, so sensor orientation in the line does not matter. `dispense_to_waste()` exits via the waste port while only `extract()` pulls through the extract port, so a sensor between selector valve and syringe sees draws only.

**The three-state `monitor` mode**, defined per sensor in §1, selects the consequence:

| mode | reads + plots | fault rule runs | acts on fault |
|---|---|---|---|
| `off` | yes | no | no |
| `warn` | yes | yes | logs "would have triggered", pump untouched |
| `stop` | yes | yes | stops the pump, raises `OperationError` |

`warn` exists because tolerance and ramp-up have to be tuned against real hardware before enforcement can be trusted, and reading a log beats eyeballing a chart.

**The mode is switchable from the GUI, per sensor, at runtime.** Config supplies the startup value; the Flow Sensors tab lets an operator move a sensor between `off`, `warn` and `stop` without editing YAML and restarting. That is what makes the tuning loop practical — record with `off`, switch to `warn` and watch what would have fired, then move to `stop` for real runs, all in one session against the same rig.

Two consequences for the design. The mode has to live somewhere mutable and be read per draw rather than captured once at construction, so arming consults the current value. And the switch is session-only: it does not write back to config, so a restart returns to the declared value — which keeps the config file the source of truth for what an unattended run does.

**Expected rate comes from the speed code,** not the sequence: `sp.get_flow_rate(speed_code)`, now in µL/min like everything else.

**A non-latching pump stop is required.** `sp.abort()` latches `is_aborted` until `reset_abort()`, and `flow_reagent` checks `if self.sp.is_aborted: return` after each `execute()` — so reusing the abort path would make a flow fault **silently return** rather than raise. The pump needs a `stop()` that terminates the current chain and lets `wait_for_stop()` exit without latching, so the fault surfaces as an `OperationError` with a real diagnostic.

Deliberately *not* a change to `abort()` itself. The two mean different things — abort is "the operator cancelled the experiment", stop is "halt this chain, an error is being raised" — and the latching behaviour is correct for the first. The `abort` pattern is also a codebase-wide convention (`abort()` / `reset_abort()` / `is_aborted` across every hardware class), so reworking it would reach far outside draw protection. Adding `stop()` alongside is the narrower change; whether `abort` wants revisiting on its own merits is a separate discussion, deferred until after Phase 2 lands.

**Stop latency is accepted at ~0.5 s.** `wait_for_stop()` polls on a 0.5 s interval, so a fault raised on the reader thread takes up to that long to break the blocking `execute()` — roughly 17 µL of continued draw at 2000 µL/min, and about 4 µL at the 500 µL/min production rate. Confirmed acceptable, so the poll interval stays as it is. `terminateCmd()` still halts the plunger immediately; only the unblocking is bounded by the poll.

**Open question:** which operations to guard — `flow_reagent` alone, or `priming_or_clean_up` too, where air in the lines is expected and false positives are likely. Also whether an operation arms every sensor whose `monitor` is not `off`, or names the specific sensor it depends on.

## Risk and rollback

- **`wait_for_completion()` UID matching** is the highest-risk change: every command path depends on it. Mitigated by dedicated unit tests and by the timeout converting a hang into a clear error.
- **Always-on reader thread** changes serial timing for all existing operations. It removes a race rather than adding one, but it touches every command path and wants a hardware smoke test.
- The `flow_sensors` section is optional and absent from existing YAML, so current configs load unchanged.
- Nothing in this change can stop a pump or fail a sequence — the sensor is read-only until draw protection lands. The blast radius is confined to the two serial-path items above.
- Rollback: drop the branch. Phase 1 requires no firmware change, so there is nothing to un-flash.
