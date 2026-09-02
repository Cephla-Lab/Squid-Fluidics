# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Fluidics v2 is a microfluidics control system for automated liquid handling experiments (MERFISH, open chamber). It consists of two subsystems:

- **`firmware/`** — Teensy 4.1 C++/Arduino firmware that drives hardware (pumps, valves, sensors)
- **`software/`** — Python control software with PyQt5 GUI and CLI for experiment automation

## Build & Run Commands

### Firmware (PlatformIO)

```bash
cd firmware
pio run                    # Build firmware
pio run -t upload          # Build and upload to Teensy 4.1
pio device monitor         # Serial monitor (2000000 baud)
```

Source files are in `firmware/` root (not `src/`), configured via `platformio.ini` with `src_dir = .`.

### Software (Python)

```bash
cd software
python gui.py                                          # Launch GUI (--config <yaml|json>; defaults to ./config.yaml then ./config.json, then the last file picked)
python run_sequences.py --path <yaml> --config <yaml>  # Run sequences from CLI
python list_controllers.py                             # Discover serial devices
python run_sequences.py --path sample_sequences/merfish-experiment.yaml --config sample_config/flow_cell_config.yaml --simulation  # Simulation mode (no hardware)
```

**Python dependencies:** declared in `software/pyproject.toml`; install with `pip install -e "software[test]"`

### Tests

```bash
cd software
python -m pytest                       # Run all unit + integration tests
python -m pytest tests/unit            # Unit tests only (fast, no hardware)
python -m pytest tests/integration     # Integration tests (uses simulation classes)
python -m pytest -v                    # Verbose output
python tests/hardware/startup.py       # Hardware: initialization/control loop test
python tests/hardware/demo.py          # Hardware: interactive demo
```

Uses pytest. Hardware test scripts in `tests/hardware/` require connected devices and are excluded from the default test run.

## Architecture

### Communication Protocol

The firmware and software communicate over serial at 2,000,000 baud using COBS (Consistent Overhead Byte Stuffing) framing via the PacketSerial library. Commands are 15 bytes (`MCU_CMD_LENGTH`), responses are 30 bytes (`MCU_MSG_LENGTH`). Command IDs are mirrored between:
- **Firmware:** `_defs.h` → `SerialCommands_t` enum
- **Software:** `fluidics/control/_def.py` → `CMD_SET` class

These must stay in sync. Same applies to `VALVE_POSITIONS`/`ValvesStates_t` and `COMMAND_STATUS`/`CommandExecution_t`.

### Software Module Structure

- **`fluidics/control/controller.py`** — Core `FluidController` class wrapping serial communication; `FluidControllerSimulation` for testing without hardware
- **`fluidics/control/syringe_pump.py`** — Tecan XCalibur syringe pump control (uses `tecancavro/` submodule); has simulation class
- **`fluidics/control/selector_valve.py`** — `SelectorValveSystem` manages cascaded multi-port rotary valve routing with port-to-reagent mapping
- **`fluidics/control/temperature_controller.py`** — TCM temperature controller with CRC32 checksums; has simulation class
- **`fluidics/control/disc_pump.py`** — Peristaltic disc pump wrapper
- **`fluidics/control/_def.py`** — Shared constants (command IDs, valve positions, sensor params, PID limits)
- **`fluidics/sequences.py`** — Sequence loading/saving/validation with pydantic discriminated union models
- **`fluidics/merfish_operations.py`** — MERFISH experiment sequence logic
- **`fluidics/open_chamber_operations.py`** — Open chamber experiment sequence logic
- **`fluidics/manual_operations.py`** — `ManualOperations(devices)`: the manual tab's verbs (open port, extract, dispense, empty to waste, aspirate) as blocking, Qt-free methods; the GUI runs them off-thread, scripts call them directly
- **`fluidics/events.py`** — The run's boundary vocabulary: `PlanEntry` (the run plan — one entry per sequence repeat, priced, carrying its source row) and the typed events (`RunStarted`…`RunEnded`) published on `session.events`
- **`fluidics/experiment_worker.py`** — The run loop: iterates the run plan, publishes the boundary events, records its outcome for the session's RunEnded
- **`fluidics/usage.py`** — `ReagentUsage`: per-port reagent totals, fed by the pump's `draws` channel joined with the valve system's open port; resets at each run's start, logs totals at its end; `system.usage`
- **`fluidics/subscribers.py`** — `Subscribers`: the callback list with isolated dispatch that the MCU packet stream, the flow sensors, the TCM and the run session all publish through
- **`fluidics/time_estimate.py`** — `plan_run(config, sequences)`: the run plan, each entry priced by replaying the sequences against a simulated twin of the config; never touches hardware, falls back rather than blocking a run (`estimate_run_time` wraps it for pricing-only callers)
- **`fluidics/run_session.py`** — `RunSession(devices)`: the one job on the rig at a time (a run or a manual move) — starts it off the caller's thread, says whether the rig is busy, stops it, waits for it, resets the run's signal when it ends
- **`fluidics/sequence_list.py`** — `SequenceList`: the editor's model with no Qt in it — the rows as typed, each row's verdict, what a run would take (`included`, `validated`), and the verbs that change them. A script or an embedder can hold one without a QApplication
- **`fluidics/sensor_recorder.py`** — `SensorSeries`, the one sample buffer (producers append, a GUI reads a window on its own clock; the plots hold their series in these), plus `SensorRecorder`, a Qt-free long-format CSV for an embedding application. The standalone tabs record their own wide per-plot CSV instead — see the module docstring for why both exist
- **`fluidics/qt/`** — the importable Qt widgets (`support`, `sequence_editor`, `manual_control`, `sensor_plots`); `gui.py` is the standalone application that arranges them
- **`fluidics/system.py`** — `FluidicsSystem`: the rig as one object — `build(config, simulation)` brings the devices up and assembles `devices`, `operations`, `manual`, `session` and a `warnings` channel; `run()`, `run_manual()`, and `close(timeout)`, which stops whatever job is running before the devices go. What the GUI, the CLI and scripts hold. `run()` is where the time-zero gate lives (`validate_sequences`), so nothing starts unchecked whoever starts it

### Firmware Module Structure

- **`controller_teensy41.ino`** — Main firmware: serial command dispatch, control loops (bang-bang and PID), state machine
- **Hardware drivers:** `NXP33996` (solenoid valves), `OPX350` (bubble sensors), `RheoLink` (rotary valves), `SLF3X` (flow sensor, I2C), `SSCX` (pressure sensors, SPI), `TTP` (disc pump, UART), `AutoPID` (PID controller)
- **`_defs.h`** — Pin assignments, sensor parameters, hardware constants

### Experiment Flow

1. Config YAML defines hardware serial numbers, valve IDs, reagent mappings (legacy JSON auto-converts)
2. YAML sequences define operations as typed dicts with a `type` discriminator field (legacy CSV also supported)
3. `FluidicsSystem.run()` hands the list to the `RunSession`, whose `ExperimentWorker` iterates it, calling operation methods on `MERFISHOperations` or `OpenChamberOperations`
4. Operations dispatch on `sequence['type']` (snake_case strings like `flow_reagent`, `add_reagent`, `set_temperature`)

### Sequence Models

`fluidics/sequences.py` defines pydantic models using a discriminated union on the `type` field. Each sequence type has only the fields it needs:
- **Fluidic types** (`flow_reagent`, `add_reagent`, `clear_and_add_reagent`, `wash_constant_flow`, `priming`, `clean_up`): `fluidic_port`, `flow_rate`, `volume`; some also have `fill_tubing_with`
- **`set_temperature`**: `temperature` field only
- **All types** share: `name` (optional label), `repeat`, `include`, `incubation_time`

Per-application available types are defined in `APPLICATION_SEQUENCES` dict. Models use `extra='forbid'` to catch typos.

### Simulation Mode

All major hardware classes have `*Simulation` counterparts. Pass `--simulation` to `run_sequences.py` or toggle in GUI to run without connected hardware.

## Key Conventions

- Firmware library dependency: `PacketSerial` for COBS encoding (declared in `platformio.ini`)
- Syringe pump uses Tecan Cavro protocol via `fluidics/control/tecancavro/` — this is a vendored library
- Config files live in `sample_config/`, sequence files in `sample_sequences/` (YAML preferred, CSV supported for legacy)
- Hardware pin assignments are all in `firmware/_defs.h`
- The `application` field in config (`"Flow Cell"` or `"Open Chamber"`) selects which operations class and available sequence types to use
