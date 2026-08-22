# Fluidics v2

Automated liquid handling system for microfluidics experiments. Supports MERFISH flow cell and open chamber workflows with syringe pump, selector valves, disc pump, and sensor feedback.

## Hardware

- **Microcontroller:** Teensy 4.1
- **Syringe pump:** Tecan XCalibur (via serial)
- **Selector valves:** RheoLink rotary valves (I2C, up to 5 cascaded)
- **Disc pump:** TTP peristaltic pump (UART)
- **Sensors:** SLF3X flow sensor (I2C), SSCX pressure sensors (SPI), OPX350 bubble detectors
- **Solenoid valves:** NXP33996 driver (SPI)
- **Temperature controller:** Yexian M207 TCM (optional)

## Getting Started

### Firmware

Requires [PlatformIO](https://platformio.org/).

```bash
cd firmware
pio run                # Build
pio run -t upload      # Upload to Teensy 4.1
```

### Software

Requires Python 3.10+. Install the package and its dependencies (declared in `software/pyproject.toml`):

```bash
pip install -e "software[test]"
```

**1. Find serial numbers for connected devices:**

```bash
cd software
python list_controllers.py
```

**2. Create a configuration file** based on the examples in `sample_config/`:

- `flow_cell_config.yaml` — Flow cell setup
- `open_chamber_config.yaml` — Open chamber setup

Key fields to set:
- `microcontroller.serial_number` — Teensy serial number
- `syringe_pump.serial_number` — Syringe pump serial number
- `syringe_pump.volume_ul` — Syringe volume (e.g. 2500 or 5000)
- `syringe_pump.speed_code_limit` — Maximum speed code (lower = faster, range 1-40)
- `reagent_selection.selector_valves.valve_ids` — IDs of connected selector valves (e.g. `[0, 1, 2]`)
- `reagent_selection.selector_valves.number_of_ports` — Number of ports per valve
- `reagent_selection.selector_valves.name_mapping` — Port-to-reagent labels (shown in GUI)
- `reagent_selection.selector_valves.tubing_fluid_amount_to_valve_ul` — Tubing dead volume from selector valve to syringe pump
- `reagent_selection.selector_valves.tubing_fluid_amount_ul` — Tubing dead volume from reagent to selector valve port
- `application` — `"Flow Cell"` or `"Open Chamber"`

Open chamber configs additionally require:
- `samples.chamber_volume_ul` — Chamber volume
- `reagent_selection.common_tubing_fluid_amount_ul` — Common tubing volume
- `sample_selection_inlet.common_tubing_fluid_amount_ul` — Tubing volume: syringe pump to open chamber

**3. Launch the GUI:**

```bash
python gui.py
```

The GUI looks for `config.yaml` (or `config.json`) in the current directory.

Or run sequences from the command line:

```bash
python run_sequences.py --path path/to/sequences.yaml --config path/to/config.yaml
```

Use `--simulation` to run without connected hardware. Legacy CSV sequence files and JSON config files are also supported.

### Tests

pytest comes with the `[test]` extra above. CI runs the full suite plus a firmware compile on every PR.

```bash
cd software
python -m pytest                       # Run all unit + integration tests
python -m pytest tests/unit            # Unit tests only (fast, no hardware)
python -m pytest tests/integration     # Integration tests (uses simulation classes)
python -m pytest -v                    # Verbose output
```

Hardware test scripts in `tests/hardware/` require connected devices and are excluded from the default test run.

## Experiment Sequences

Experiments are defined as YAML files. Each sequence has a `type` field and only the fields relevant to that type. Example:

```yaml
sequences:
  - type: flow_reagent
    fluidic_port: 2
    flow_rate: 5000
    volume: 2000
    incubation_time: 3
    fill_tubing_with: 25

  - type: set_temperature
    temperature: 50
    include: false
```

All sequence types share these optional fields: `name` (custom label), `repeat` (default 1), `include` (default true), `incubation_time` (default 0).

### Flow Cell Sequence Types

| Type | Extra Fields | Description |
|------|-------------|-------------|
| `flow_reagent` | `fluidic_port`, `flow_rate`, `volume`, `fill_tubing_with` | Flow reagent from the specified port |
| `priming` | `fluidic_port`, `flow_rate`, `volume` | Prime all tubings with corresponding reagents |
| `clean_up` | `fluidic_port`, `flow_rate`, `volume` | Flush all tubings (typically with water) |

### Open Chamber Sequence Types

| Type | Extra Fields | Description |
|------|-------------|-------------|
| `add_reagent` | `fluidic_port`, `flow_rate`, `volume`, `fill_tubing_with` | Dispense reagent into chamber (tubings already filled) |
| `clear_and_add_reagent` | `fluidic_port`, `flow_rate`, `volume`, `fill_tubing_with` | Clear previous liquid from tubings, then dispense reagent |
| `wash_constant_flow` | `fluidic_port`, `flow_rate`, `volume`, `fill_tubing_with` | Flow reagent while aspirating with disc pump |
| `priming` | `fluidic_port`, `flow_rate`, `volume` | Prime all tubings |
| `clean_up` | `fluidic_port`, `flow_rate`, `volume` | Flush all tubings and aspirate chamber |
| `set_temperature` | `temperature` | Set temperature controller to target |

## Communication Protocol

The firmware and software communicate over serial at 2,000,000 baud using COBS framing. Commands are fixed at 15 bytes, responses at 30 bytes. The command definitions in `firmware/_defs.h` and `software/fluidics/control/_def.py` must be kept in sync.
