# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Python control software for the Fluidics v2 microfluidics system. Provides a PyQt5 GUI and CLI for automated liquid handling experiments (Flow Cell, Open Chamber). Communicates with a Teensy 4.1 microcontroller over serial.

## Commands

```bash
# GUI
python gui.py

# CLI experiment runner
python run_sequences.py --path sample_sequences/merfish-experiment.yaml --config sample_config/flow_cell_config.yaml
python run_sequences.py --path sample_sequences/merfish-experiment.yaml --config sample_config/flow_cell_config.yaml --simulation

# Config conversion (legacy JSON → YAML v2.0)
python convert_config.py path/to/legacy_config.json

# Device discovery
python list_controllers.py

# Unit + integration tests (no hardware needed)
python -m pytest                       # All tests
python -m pytest tests/unit            # Unit tests only
python -m pytest tests/integration     # Integration tests (simulation classes)
python -m pytest -v                    # Verbose

# Hardware smoke: bring a rig up from its config and run each manual verb once
python -m tests.hardware.manual_check --config ../config.yaml
python -m tests.hardware.diagnose_temperature_controller --sn <serial>
```

Uses pytest. Hardware scripts in `tests/hardware/` are excluded from the default test run and need a connected rig (`manual_check --simulation` checks the script itself). Run them with `python -m` from `software/` so `fluidics` imports without the package installed. Use `--simulation` for software-only CLI testing.

The old `tests/hardware/startup.py` and `demo.py` (MCU-level exercises from before the syringe-pump architecture) are gone; git history has them.

**Dependencies:** declared in `pyproject.toml`; install with `pip install -e ".[test]"` from this directory

## Architecture

### Configuration System (v2.0)

Config files use YAML format with pydantic validation (`fluidics/control/config.py`). Key types: `FluidicsConfig` (root), `ReagentSelectionConfig`, `SelectorValvesConfig`, etc.

- `load_config(path)` handles both `.yaml` and `.json` files. If a `.json` is given, it auto-converts to `.yaml` v2.0 and loads that going forward.
- `convert_config.py` is a standalone CLI tool for batch conversion.
- `application` field: `"Flow Cell"` (formerly `"MERFISH"`) or `"Open Chamber"`

**Tubing volume decomposition** — the old config stored total tubing distance per valve. The new config splits this:
- `reagent_selection.common_tubing_fluid_amount_ul` — shared tubing from last valve to sample (= old valve 0's value for flow cell, or old `tubing_fluid_amount_sv_to_sp_ul` for open chamber)
- `reagent_selection.selector_valves.tubing_fluid_amount_to_valve_ul` — per-valve delta above common
- `SelectorValveSystem.get_tubing_fluid_amount_to_valve()` returns `common + per_valve`, so total volumes are unchanged

**Open Chamber extra fields:**
- `sample_selection_inlet.common_tubing_fluid_amount_ul` — tubing from syringe pump to chamber (was `tubing_fluid_amount_sp_to_oc_ul`)
- `samples.chamber_volume_ul` — chamber volume (was top-level `chamber_volume_ul`)
- `temperature_controller` — optional, omit section if not used

### Serial Protocol

Communicates with Teensy at 2,000,000 baud using COBS framing. Commands are 15-byte fixed-length arrays, responses are 30 bytes. The first 2 bytes are a UID counter, byte 3 is the command ID, remaining bytes are command-specific parameters packed as big-endian integers.

`fluidics/control/_def.py` defines `CMD_SET`, `COMMAND_STATUS`, and `VALVE_POSITIONS` — these **must stay in sync** with `firmware/_defs.h` enums (`SerialCommands_t`, `CommandExecution_t`, `ValvesStates_t`).

### Hardware Abstraction Layers

Each hardware class has a `*Simulation` counterpart in the same file:

| Class | Simulation | File |
|---|---|---|
| `FluidController` | `FluidControllerSimulation` | `fluidics/control/controller.py` |
| `SyringePump` | `SyringePumpSimulation` | `fluidics/control/syringe_pump.py` |
| `TCMController` | `TCMControllerSimulation` | `fluidics/control/temperature_controller.py` |

`SelectorValveSystem` and `DiscPump` operate through `FluidController` commands (no separate simulation classes — they use the controller's simulation).

### Syringe Pump Command Chaining

The syringe pump uses a chain-based execution model:
1. `reset_chain()` — clear the command buffer
2. `extract(port, volume, speed_code)` / `dispense(port, volume, speed_code)` — queue commands
3. `execute()` — send the chain and block until done

Speed codes (0–40) map to stroke times via `SPEED_SEC_MAPPING`. Use `flow_rate_to_speed_code(ul_per_min)` to convert. `speed_code_limit` in config prevents dangerously fast operation. Higher speed code = slower flow.

### Selector Valve Cascading

`SelectorValveSystem` manages multiple rotary valves daisy-chained in series. Port addressing is linearized: ports 1–9 map to valve 0, ports 10–18 to valve 1, etc. The last port of each valve (except the final one) routes to the next valve in the chain. `open_port(port_index)` handles the routing automatically.

### Experiment Execution Flow

1. YAML config defines hardware serial numbers, valve IDs, reagent mappings, tubing volumes
2. YAML sequences define operations as typed dicts with a `type` discriminator field (legacy CSV also supported)
3. `config.application` (`"Flow Cell"` or `"Open Chamber"`) selects the operations class
4. `ExperimentWorker` iterates the sequence list, calling `process_sequence()` on the operations class
5. `RunSession` (`fluidics/run_session.py`) runs the worker on its own thread and owns the job's end; the worker reports through callbacks, and cancellation comes through the run's shared `RunControl` (`fluidics/errors.py`)

### Operations Classes

**`MERFISHOperations`** — syringe-pump-only flow cell system:
- `Flow Reagent` — extract reagent through selector valve, optionally fill tubing with buffer afterward
- `Priming` / `Clean Up` — prime all ports with their reagents, then fill tubing with wash buffer

### Draw Protection

`Flow Reagent`'s two draws run under a `DrawGuard` (`fluidics/flow_monitor.py`), which watches each configured flow sensor against the pump's actual rate for the speed code. `FlowMonitor` is the rule — a pure function of `(flow, timestamp)` with a ramp-up window and a consecutive-sample debounce; the guard is the plumbing that arms sensors and, on a `stop` trip, cancels the run with the fault as its cause.

Per sensor, `monitor` is `off` (plot only), `warn` (log and carry on), or `stop` (halt the draw and raise `FlowFault`). Config sets the starting mode; the Flow Sensors tab switches it at runtime. Each draw reads the mode once when it arms.

Notices go to `MERFISHOperations(on_warning=...)`, which becomes the `DrawGuard`'s `log`. It defaults to the fluidics logger's WARNING (console + run log); the GUI passes a channel that marshals to the GUI thread and shows a non-modal line under the progress bar. A `warn` fault also lands in the flow CSV's Fault column when recording.

**Only Flow Cell is guarded.** `OpenChamberOperations` is never handed the sensors, so a `warn`/`stop` mode configured on an Open Chamber machine is inert; the GUI says so at startup, forces the mode to `off`, and disables the per-sensor control.

Not guarded: the dispense-to-waste inside `_empty_syringe_pump_on_full`, and `Priming`/`Clean Up` — both move liquid out the waste port rather than through the flow cell, so the sensors would read nothing and every one would fault.

`FlowFault` is a `SafetyFault` — a `Cancelled`, sibling of `AbortRequested`, never a subclass of it — so the worker reports it with its diagnosis rather than as "aborted by user". The guard does no I/O from the reader thread: it cancels the run, the pump's wait wakes, halts the plunger on the sequence thread, and raises the fault out of `execute()`; the operations' `OperationError` wrappers let `Cancelled` through.

**`OpenChamberOperations`** — syringe pump + disc pump for open chamber:
- `Add Reagent` / `Clear Tubings and Add Reagent` — push reagent into chamber, disc pump aspirates waste
- `Wash with Constant Flow` — simultaneous syringe dispense + disc pump aspiration
- `Set Temperature <N>` — set temperature controller target and wait for stabilization

### GUI Structure

`gui.py` is a single-file PyQt5 application (~1200 lines) with tabs for sequence editing, hardware control, sensor monitoring, and real-time plotting. It instantiates the same hardware classes and `ExperimentWorker` as the CLI.

## Key Conventions

- `fluidics/control/tecancavro/` is a vendored library for Tecan Cavro syringe pump protocol — avoid modifying
- Config files in `sample_config/` (YAML), sequence files in `sample_sequences/` (YAML preferred, CSV supported for legacy)
- Cancellation: `DeviceSet.abort()` cancels the run's `RunControl` (no device I/O on the calling thread); every waiting device raises the cause on its own thread, so operations unwind by the raise -- there is no `is_aborted` polling. `DeviceSet.make_safe()` quiets the rig after any early end, and the worker's `_end_early` lifts any pending pause first (`RunControl.release()`), so nothing on the failure path can park. `RunSession` resets the signal when the job ends, before the caller's `on_finished`.
- One job on the rig: `RunSession.start(sequences, operations, callbacks)` for a run, `run_manual(verb, ...)` for a manual move; a second job while one runs is refused, and `Interruptible.execute()` refuses re-entry at driver depth as well. Both GUI tabs and the CLI go through the session -- nothing else owns a thread. `session.abort()` stops whichever job is running (the manual tab's Stop button); the main window disables the tab that did not start the job, and `closeEvent` asks before aborting a live job and waits for it to end before the devices close. Worker threads reach the Qt thread through `PostsToQtThread._post_event(method_name, *args)`.
- Pause: `DeviceSet.pause()`/`resume()` gate the same `RunControl`.
  - The gate: every driver call that starts motion passes `checkpoint()` first, so a valve or drain move in flight finishes and the next one holds. Run-level waits (incubation, settle, temperature stabilization, the drain's timed aspiration) use `delay()`/`run_for()`, which count *running* time, so a pause stops those clocks. Hardware polls use `wait()`/`sleep()` and deliberately ignore pause -- a command in flight must be waited out. Abort while paused unwinds: `cancel()` opens the gate.
  - The syringe pump stops where it is: it dispatches its queued ops one at a time, `wait_for_stop` wakes on a pause (`wait_interrupted()`), halts the plunger on the thread that owns the port, parks at the gate, and on resume re-issues what the op had left as an absolute move to the op's target -- fixed from a plunger reading taken before the op started, so no read of a decelerating pump decides how much liquid moves. `Interruptible.moving` is the pump's own word on whether a move is in flight, cleared before a halt is sent.
  - Around the pump: hardware committed alongside a pump call registers what to do about a park with `when_held(on_hold, on_release)` (the drain under a dispense; hooks run with the region detached, and `on_release` never runs for a cancel). The `DrawGuard` stands down while the pump is not `moving` and restarts its ramp-up at the first sample of the new move.
  - The GUI's Pause/Resume button distinguishes the two moments the operator cares about: `paused` (asked for, the move in flight still running) shows *pausing…*, `at_rest` (paused *and* a thread parked at a gate) shows *paused* and stops the run's countdown. The GUI polls those two properties on its one-second tick; the worker's own "Paused" progress status is log-only, since the worker cannot see a park that happens inside an operation -- it is blocked in `process_sequence` at the time.
- `send_command_blocking()` = `send_command()` + `wait_for_completion()` (polls MCU status until not `IN_PROGRESS`)
