# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Python control software for the Fluidics v2 microfluidics system. Provides a PyQt5 GUI and CLI for automated liquid handling experiments (Flow Cell, Open Chamber). Communicates with a Teensy 4.1 microcontroller over serial.

## Commands

```bash
# GUI
python gui.py    # --config <yaml|json>; defaults to ./config.yaml then ./config.json, then the last file picked

# CLI experiment runner
python run_sequences.py --path sample_sequences/merfish-experiment.yaml --config sample_config/flow_cell_config.yaml
python run_sequences.py --path sample_sequences/merfish-experiment.yaml --config sample_config/flow_cell_config.yaml --simulation
# Plate sipper rig. Only after reloading a plate or refilling a bottle, add --refilled <port_25 | plate name | all>
# (repeatable) to declare that reservoir full; on every other run the volume tracker's own count stands
python run_sequences.py --path sample_sequences/seqfish-plate-sipper.yaml --config sample_config/plate_sipper_config.yaml

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
# Robot bench check: home, jog, teach A1 and the dip on each plate, visit the corners, agitate, retract
python -m tests.hardware.robot_check --config ../config.yaml
```

Uses pytest. Hardware scripts in `tests/hardware/` are excluded from the default test run and need a connected rig (`manual_check --simulation` and `robot_check --simulation` check the scripts themselves). Run them with `python -m` from `software/` so `fluidics` imports without the package installed. Use `--simulation` for software-only CLI testing.

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

### Temperature Controller

`TCMController` speaks the Yexian TCM's ASCII protocol at 57600 baud: `TC<n>:PARAM?` is answered `TC<n>:PARAM=value`, a set or save with `CMD:REPLY=<code>` (1 set OK, 8 save OK, anything else raises out of `send_command`). Channels are 1-based and name the module on the wire (`TC1`, `TC2`). No checksum or address suffix is used.

- **A reply is checked before it is believed.** Numeric readings go through `_query(param, module)`, which raises `ValueError` unless the reply is for the parameter asked. Never slice a reply by position: `"TC1:TCACTCUR=-0.00"[17:]` is a plausible `0.0` °C. `send_command` also drops whatever is waiting before each write, so an answer that arrives after its command timed out is not read by the next one. `_read_output_enabled` is the one reader still outside `_query` — routing it there would turn a `TCSW?` timeout at bring-up from "output off" into a failed bring-up.
- **`_query` takes the module, not the channel.** `_module()` raises `ValueError` for a channel the unit lacks, and callers that ride out a bad reply catch `ValueError`, so they resolve the module before their `try` — a wrong channel must raise, as it does in simulation, not pass for an unanswered read.
- **Two readers, two policies.** The display's poll reads temperature, `TCACTVOL` and `TCACTCUR` through `_read_polled`, which returns `None` and warns when a read *starts* failing (not every second); on `None` the last temperature stands and the tab shows `--`. Raising there would end the poll thread and the plot with it. A run reads through `get_actual_temperature`, where an error reply still raises, so a unit that refuses fails the run.
- **Polling is consumer-driven.** `start()` is called by the GUI, not at bring-up; a headless run pays no polling traffic. The subscriber payload is the list of temperatures and stays that — an embedder subscribes to it — so the tab reads `output_voltages`, `output_currents` and `output_enabled` off the driver when a publish arrives.
- **Open: reply framing.** `send_command` reads with pyserial's `readline()`, which waits for `\n`; the vendor manual says every reply ends in `\r` alone. If a unit follows its manual, each command waits out the 0.5 s timeout (and a poll is three commands per channel). The fix — `read_until(b"\r")` plus the manual's 50 ms gap between commands, which the timeout currently supplies by accident — changes run-path timing and wants a hardware check. `time python -m tests.hardware.diagnose_temperature_controller --sn <serial>` tells the two apart: 15 queries take about 8 s with `\r` only, about 1 s with `\r\n`.

### Experiment Execution Flow

1. YAML config defines hardware serial numbers, valve IDs, reagent mappings, tubing volumes
2. YAML sequences define operations as typed dicts with a `type` discriminator field (legacy CSV also supported)
3. `config.application` (`"Flow Cell"` or `"Open Chamber"`) selects the operations class
4. `ExperimentWorker` iterates the sequence list, calling `process_sequence()` on the operations class
5. `FluidicsSystem` (`fluidics/system.py`) is what an entry point holds: `build(config, simulation)` assembles the devices, the operations, `ManualOperations` and the `RunSession`; `run()` starts a run through the session, which runs the worker on its own thread and owns the job's end; `close(timeout)` stops whatever job is running, then releases the devices. The worker reports through callbacks, and cancellation comes through the run's shared `RunControl` (`fluidics/errors.py`)

### Operations Classes

**`MERFISHOperations`** — syringe-pump-only flow cell system:
- `Flow Reagent` — extract reagent through selector valve, optionally fill tubing with buffer afterward
- `Priming` / `Clean Up` — prime all ports with their reagents, then fill tubing with wash buffer
- `Push` — on a plate-sipper rig, a `Flow Reagent` from the sipper's well with the config's `push_volume_ul` and no tubing fill: the row after a well draw that moves it into the flow cell

### Draw Protection

`Flow Reagent`'s two draws run under a `DrawGuard` (`fluidics/flow_monitor.py`), which watches each configured flow sensor against the pump's actual rate for the speed code. `FlowMonitor` is the rule — a pure function of `(flow, timestamp)` with a ramp-up window and a consecutive-sample debounce; the guard is the plumbing that arms sensors and, on a `stop` trip, cancels the run with the fault as its cause.

Per sensor, `monitor` is `off` (plot only), `warn` (log and carry on), or `stop` (halt the draw and raise `FlowFault`). Config sets the starting mode; the Flow Sensors tab switches it at runtime. Each draw reads the mode once when it arms.

Notices go to `MERFISHOperations(on_warning=...)`, which becomes the `DrawGuard`'s `log`. It defaults to the fluidics logger's WARNING (console + run log); the GUI passes a channel that marshals to the GUI thread and shows a non-modal line under the progress bar. A `warn` fault also lands in the flow CSV's Fault column when recording -- its own row, carrying the tripping sample's timestamp.

**Only Flow Cell is guarded.** `OpenChamberOperations` is never handed the sensors, so a `warn`/`stop` mode configured on an Open Chamber machine is inert. Bring-up says so and forces the mode to `off` (`build_devices` → `on_issue(ISSUE_DRAW_PROTECTION, …)`), so the GUI's bring-up dialog, a headless run's log and an embedder's issue list all carry it; `draw_protection_available(config)` is the one statement of the rule, and the Flow Sensors tab disables the per-sensor control from it.

Not guarded: the dispense-to-waste inside `_empty_syringe_pump_on_full`, and `Priming`/`Clean Up` — both move liquid out the waste port rather than through the flow cell, so the sensors would read nothing and every one would fault.

`FlowFault` is a `SafetyFault` — a `Cancelled`, sibling of `AbortRequested`, never a subclass of it — so the worker reports it with its diagnosis rather than as "aborted by user". The guard does no I/O from the reader thread: it cancels the run, the pump's wait wakes, halts the plunger on the sequence thread, and raises the fault out of `execute()`; the operations' `OperationError` wrappers let `Cancelled` through.

**`OpenChamberOperations`** — syringe pump + disc pump for open chamber:
- `Add Reagent` / `Clear Tubings and Add Reagent` — push reagent into chamber, disc pump aspirates waste
- `Wash with Constant Flow` — simultaneous syringe dispense + disc pump aspiration
- `Set Temperature <N>` — set temperature controller target and wait for stabilization

### Plate Sipper

A Flow Cell rig may carry an XYZ robot that moves a sipper on one selector-valve port over two 96-well plates, so a row can draw from a well instead of a bottle. The config block is `reagent_selection.nimotion_gantry_system`, keyed as You Yan wrote it; a rig without it runs exactly as before. `sample_config/plate_sipper_config.yaml` and `sample_sequences/seqfish-plate-sipper.yaml` are the documented pair.

- **`fluidics/robot/`** — `labware.py` (plate geometry, `PlatePlacement`, `parse_ref` for `A3` or `ssct/A3`), `stage.py` (`XYZStage`, the three axes as one), `sipper.py` (`RobotSipper`, the verbs the operations call), `simulation.py` (`SimulatedService`: the driver's shape, motion modelled), `_driver.py` (`DriverService`, turning every driver or port error into a `DeviceError` naming the axis). The subpackage imports only the stdlib, `fluidics.errors`, `fluidics.subscribers`, and -- in `_driver.py` alone -- the driver; a fresh-interpreter test enforces it. `fluidics/vocabulary.py` is the one owner of how a well reference is spelled, for the validators, the editor and the operations.
- **Z and the deck.** Z is 0 at the retracted top and increases toward the deck; retracted means `z <= travel_z_mm`, and only while the stage is homed and its position known. XY moves only while retracted. Homing goes Z first; the drives park 0.1 to 0.5 mm past 0, so a travel height below 1 mm (`MIN_TRAVEL_Z_MM`) is refused at load and when taught. The driver's strokes start 0.5 mm clear of the origin switches, below where homing parks, so `home()` ends by clearing each axis into its stroke (Z first, then X and Y together); without that the first single-axis jog after homing would be refused for the other axes' targets. The plates sit side by side along X, long sides along Y, so each plate's columns run along +y and its rows along +x: `column_axis` and `row_axis` are required, since this deck is not the SBS default. Every plate's corners are checked against the strokes at bring-up and whenever a placement is taught.
- **The stage polls; it never uses the driver's blocking wait.** A leg dispatches every axis, then polls through `run_control.sleep` until each reports target reached within `TOLERANCE_MM`, reading every axis of the leg on every poll until the leg ends, so an axis that arrived first cannot fault unseen while the other still travels. It reads the drive's limit-switch alarm (8614h) itself, since a move a switch stops never reports target reached, and refuses a target reached while a limit input (60FDh) is active, as the driver's own wait does. Anything that ends a leg early halts every axis that may have started and re-reads the position before the error goes on.
- **Volume tracking** (`VolumeTracker` in `fluidics/reservoirs.py`, `devices.reservoirs`): the one tally of reagent draws, built on every rig, declared volumes or not. The pump asks it before every extract it dispatches (`SyringePump.draw_authorizers`), and it counts each draw twice: for the run, under the valves' routed port, a well's under the sipper's (`used_rows()`, the report's `reagent_used_ul`; zeroed at `RunStarted`, so a manual draw between runs stays in view until the next run starts), and under its reservoir, the routed port's bottle or the sipper's well, since that reservoir's last reset (`refill`), on every rig -- a tracked reservoir's figure on the file, an untracked bottle's in memory alone. `balances()` is the Reservoirs window's one read, and `rows()` (the report's reservoirs block, the gate's notice) is that read filtered, never a second reading, so the three cannot disagree; what each returns, and in what order, is on `VolumeTracker`. A declared volume (a key under `loaded_volume_ul`, or every well of a gantry's plates) adds only a limit: for such a tracked bottle (`port_25`) or well (`ssct/A1`) the volume tracker refuses a draw that would not fit, or charges it and writes the volume tracking file, before the plunger moves, so the file can only read high, never low; an untracked bottle is counted, never limited and never written. The file and its lock exist only where something is tracked -- a rig that tracks nothing, or a simulated rig given no `volume_tracking_path` (then a fresh volume tracker, every reservoir declared full), keeps its volume tracker in memory. One process holds a rig's volume tracking file (a lock file), and `build_devices` takes the lock before it constructs any device (the real pump's constructor talks to the pump), clears the MCU or homes the robot or the valves, so a second launch on a rig in use stops before anything moves. Restored at bring-up entry by entry: an entry whose definition changed, is new, or has no file is undeclared, and nothing tracked is drawn until its refill is declared (a reset in the Reservoirs window, `--refilled`). Volume tracking never guesses.
- **The gate in `run()`.** On a rig that tracks a volume, `FluidicsSystem.check_reservoirs` replays the plan on an instant simulated twin restored from the live volume tracker's snapshot (only read) and refuses, naming the step, a run that would overdraw a well or a bottle -- at time zero, not at the pump hours in; a run that passes logs, once, at INFO, what the replay says each touched reservoir will have left (`Reservoirs after this run: port_25 476000 uL left; ssct/A1 260 uL left.`, or that no tracked reservoir is drawn from), the general notice at the start of a run, not a warning. A rig that tracks nothing (`devices.reservoirs.tracked` is empty) skips the gate with no replay: its volume tracker has nothing to refuse, and a step the twin cannot replay must not become a refusal where `plan_run` deliberately falls back.
- **No air through the sipper.** `RobotSipper.current_well` is set only while the tip sits at dip depth in a well; retract, home, a halt and an agitation in progress clear it. A row with `well` moves the sipper in before the valve opens its port, `dip_cycles` dips run before the draw, and the volume tracker refuses a draw through the sipper's port with no well under the tip (`ReservoirNoWell`). Between steps and sessions the tip rests wet in the last step's well: a close makes no move (the lead screw holds Z, the next launch homes upward first); Retract lifts it before a plate swap, and a refill of the plate the tip is in (or of `all`) is refused until then, by the manual verb and, before the question, by the Reservoirs window's resets; without a witness (a jog, a halt, a fault reset or teaching clears it where the tip is) any plate's reload waits for a position known to be retracted. The Robot tab's "Do not lower tip" box makes Go to well stop retracted over the well (no witness, so no draw) and `FluidicsSystem.run` refuse at time zero, for checking taught positions over empty plates.
- **Sequences.** `well`, `dip_cycles` and `sipper_well` are optional fields on the existing types; `push` is the one sipper-only type (`SIPPER_TYPES`, derived from the models: a well and no port): the row after a well draw, drawing the config's `push_volume_ul` (tip to flow cell) from its well through the sipper, with no volume or port on the row and dips optional. `validate_sequences` checks the well fields against the rig's vocabulary; a push row on a rig without a sipper is refused as having no sipper, like any well row, and the Add dialog offers the type only with one (`types_for_rig`). On a sipper rig a priming or clean-up row's `use_ports` must include the sipper's port; the gate and the operation both refuse one that leaves it out (D7).
- **The dedicated wells.** `priming_well` and `clean_up_well` (the gantry block) are where a priming or clean-up row primes the sipper's port unless the row names its own `sipper_well`; `MERFISHOperations` reads them at the row, so a change holds from the next one. The Teach window sets them: `ManualOperations.set_dedicated_well(role, text)` (`role` as `DEDICATED_WELLS` names them, "priming" or "clean-up", the deck map's words) runs as a job, resolves the reference through the rig's vocabulary, so a plate or a well the rig has not got changes nothing, and writes the config's field as `plate/well`; Save writes it to the file with the rest of the teaching.
- **The Robot tab** (`fluidics/qt/robot_control.py`, on a rig with a robot only) is the run-time view of the stage: home, retract, reset a fault, STOP, "Do not lower tip", the deck map and go to a well -- movement only; what the wells hold is the Run Experiments tab's Reservoirs window. Teaching, with the jog it needs, is a tool window opened from the Stage row ("Teach…"): jog per axis, a plate picker, Set A1 / dip depth / travel height here, under them the priming and clean-up wells (a reservoir and a well picker each, prefilled from the config, and Set), and Save, which writes the taught values through `save_config`; its controls freeze like the tab's, the button that opens it does not. The deck map's marked wells are read from the config at build and again whenever a job ends, so a Set shows when it lands. One job on the rig greys every other tab: the session records a manual job's `source`, so the main window tells the Robot tab's jobs from the manual tab's. The deck map (`fluidics/qt/deck_map.py`, pure Qt over labware values) draws the deck as the operator faces it, a half turn from the stage's axes (X to the left, Y down, so plate 1's A1 sits top right): both plates where they are taught and the tip where the stage last published it, colored retracted or lowered with the current well ringed and the priming and clean-up wells marked; a click on a well is Go to well for that well and nothing between wells; it is fed by the tab and never polls.
- **Commands.** `run_sequences.py --refilled KEY` is for the run after a reload or a refill: it declares that reservoir full before the run, so passing it routinely would erase what volume tracking knows. `--volume-tracking-file PATH` places the volume tracking file (default: beside the run reports, named after the config, `<log dir>/reservoirs/<digest>.json`). `tests/hardware/robot_check.py` is the bench check: it teaches each plate at the console and reports every axis's settle error against `TOLERANCE_MM`.

### GUI Structure

`gui.py` is the standalone application: it builds the `FluidicsSystem`, lays the tabs out, and owns the window's lifecycle. The widgets themselves live in `fluidics/qt/`, so an embedding application (Squid's fluidics-protocol GUI) can import them without the standalone app:

- **`fluidics/qt/support.py`** — `PostsToQtThread` (the one cross-thread idiom: `_post_event(name, *args)` runs a method on the Qt thread), `GuiLogHandler` (feeds the run tab's log pane), `subscribe_until_detached`, `WellCombo` and `connect_click` (the widget helpers the editor and the Robot tab share), and the small dialog/format helpers
- **`fluidics/qt/sequence_editor.py`** — `SequencesWidget` (renders the sequence list, the run controls -- Reservoirs… among them, on every rig, live under a job since opening a window is not one -- and the log pane) and `AddSequenceDialog`
- **`fluidics/qt/reservoirs_window.py`** — `ReservoirsWindow(config, system, job_source)`, the tool window Reservoirs… opens: non-modal, owned by the Run tab (it goes with it), reading the volume tracker's `balances()` and nothing else, and repainting itself each second while it is shown and on every session state change, which it subscribes to itself. The GUI's one place to declare a refill: each reset runs the manual `refill` verb as one job labelled `job_source` (the Run tab passes `RUN_TAB_JOB_SOURCE`, so the main window keeps that tab live), its refusal (`refill_refusal`) said before the question, and the resets are live only while the rig is idle
- **`fluidics/qt/manual_control.py`** — `ManualControlWidget`: the valves, the syringe and the drain by hand; nothing about volumes
- **`fluidics/qt/robot_control.py`** — `RobotControlWidget`, the Robot tab: the stage's movement, the deck map and teaching (the dedicated wells among it), nothing about volumes (see Plate Sipper)
- **`fluidics/qt/sensor_plots.py`** — the live temperature and flow plots, their rolling windows and per-plot CSV recording; each temperature channel also shows the TEC's output voltage and current, live and unrecorded. `MplCanvas` stays legible when embedded short: constrained layout plus a `minimumSizeHint` measured from its own labels

Qt is reached through `qtpy` everywhere, `gui.py` included — two bindings in one process is a crash, and qtpy resolves it once (`QT_API`). PyQt5 and matplotlib are the `[gui]` extra, so the core package (sequences, control, worker, CLI) installs headless.

**Widgets that outlive nothing.** An embedded widget can be destroyed while the rig keeps running, so every subscription a widget makes is undone on `destroyed`: `subscribe_until_detached` for the device and session channels (`Subscribers.unsubscribe` deregisters by identity, so the bound method registered is the one handed back), and `GuiLogHandler.detach()` for the log handler, which logging would otherwise hold globally.

## Key Conventions

- `fluidics/control/tecancavro/` is a vendored library for Tecan Cavro syringe pump protocol — avoid modifying
- `fluidics/control/nimotion/` is a vendored copy of nm-servo's `servo_service` (see its `VENDORED.md`): fix upstream, re-copy, never edit in place
- Config files in `sample_config/` (YAML), sequence files in `sample_sequences/` (YAML preferred, CSV supported for legacy)
- Cancellation: `RunSession.abort()` cancels the run's `RunControl` (no device I/O on the calling thread); every waiting device raises the cause on its own thread, so operations unwind by the raise -- there is no `is_aborted` polling. `DeviceSet.make_safe()` quiets the rig after any early end, and the worker's `_end_early` lifts any pending pause first (`RunControl.release()`), so nothing on the failure path can park.
- One job on the rig: `RunSession` (`fluidics/run_session.py`) owns whichever is running -- a run or a manual move -- refuses a second, exposes its state (`busy`, `kind`, `paused`, `at_rest`, `cancelled`), and resets the signal when the job ends, atomically and before the caller's `on_finished`. Both GUI tabs and the CLI go through it (via `FluidicsSystem`); nothing else owns a thread. `Interruptible.execute()` refuses re-entry at driver depth as well. GUI wiring (the greyed tab, Stop, the close question, `PostsToQtThread`) is documented on the classes in `gui.py`.
- The run-time estimate comes from replay (`fluidics/time_estimate.py`): `build_worker` runs the sequences against a simulated twin of the config (without its flow sensors: they add nothing to the pump's time, and a bring-up finding about them must not be found again at every plan) and totals the recorded chains plus the declared fixed costs (valve moves, `set_temperature`) -- so dumps, tubing turnover and quantized speed codes estimate themselves -- one figure per sequence; a replay that fails logs and falls back rather than blocking the run. The GUI confirms the total with the operator before starting, and re-anchors the countdown at every sequence boundary: what remains is the estimate of the sequences not yet run, whatever the finished ones took. The display's elapsed clock is `RunControl.running_seconds()` -- wall time minus held spans, restarted per job by the session -- and the tick reads and paints it.
- Every job reports the same way: `on_stopped()` if the operator stopped it, `on_error(message)` if it failed or the instrument stopped it (a flow fault, with its diagnosis), and `on_finished()` last, always. An abort is a stop, never an error -- the GUI shows one dialog for it, not an Error and then a Finished. A run's callbacks also carry the worker's `update_progress` and `on_estimate`.
- Pause: `RunSession.pause()`/`resume()` gate the same `RunControl`.
  - The gate: every driver call that starts motion passes `checkpoint()` first, so a valve or drain move in flight finishes and the next one holds. Run-level waits (incubation, settle, temperature stabilization, the drain's timed aspiration) use `delay()`/`run_for()`, which count *running* time, so a pause stops those clocks. Hardware polls use `wait()`/`sleep()` and deliberately ignore pause -- a command in flight must be waited out. Abort while paused unwinds: `cancel()` opens the gate.
  - The syringe pump stops where it is: it dispatches its queued ops one at a time, `wait_for_stop` wakes on a pause (`wait_interrupted()`), halts the plunger on the thread that owns the port, parks at the gate, and on resume re-issues what the op had left as an absolute move to the op's target -- fixed from a plunger reading taken before the op started, so no read of a decelerating pump decides how much liquid moves. `Interruptible.moving` is the pump's own word on whether a move is in flight, cleared before a halt is sent.
  - Around the pump: hardware committed alongside a pump call registers what to do about a park with `when_held(on_hold, on_release)` (the drain under a dispense; hooks run with the region detached, and `on_release` never runs for a cancel). The `DrawGuard` stands down while the pump is not `moving` and restarts its ramp-up at the first sample of the new move.
  - The GUI's Pause/Resume button distinguishes the two moments the operator cares about: `paused` (asked for, the move in flight still running) shows *pausing…*, `at_rest` (paused *and* a thread parked at a gate) shows *paused* and stops the run's countdown. The GUI polls those two properties on its one-second tick; the worker's own "Paused" progress status is log-only, since the worker cannot see a park that happens inside an operation -- it is blocked in `process_sequence` at the time.
- `send_command_blocking()` = `send_command()` + `wait_for_completion()` (polls MCU status until not `IN_PROGRESS`)
