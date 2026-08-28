"""Fluidics system configuration: pydantic models, legacy conversion, and loading."""

from __future__ import annotations

import json
import os
from typing import Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


DEFAULT_CONFIG_PATHS = ("./config.yaml", "./config.json")


def default_config_path():
    """The rig's conventional local config, if one exists: ./config.yaml,
    then the legacy ./config.json (which load_config auto-converts)."""
    for path in DEFAULT_CONFIG_PATHS:
        if os.path.exists(path):
            return path
    return None


# --- Pydantic Models ---

class _StrictModel(BaseModel):
    """Unknown keys fail loudly instead of being silently dropped.

    The sequence models have forbidden extras since they were written; the
    config models silently ignored them, so a misspelled or unsupported key
    read as configured while changing nothing (a live example: a rig config
    carrying `microstep: true` under syringe_pump, a field nothing reads).
    Safety-adjacent knobs like tolerance_celsius and the flow-sensor monitor
    fields are one typo away from their defaults without this.
    """
    model_config = ConfigDict(extra="forbid")


class MicrocontrollerConfig(_StrictModel):
    serial_number: str


class SyringePumpConfig(_StrictModel):
    serial_number: str
    volume_ul: int = Field(gt=0)
    ports_allowed: List[int]
    waste_port: int
    extract_port: int
    dispense_port: Optional[int] = None
    speed_code_limit: int = Field(ge=0, le=40)


class SelectorValvesConfig(_StrictModel):
    valve_ids: List[int]
    number_of_ports: Dict[int, int]
    tubing_fluid_amount_to_valve_ul: Dict[int, int]
    name_mapping: Optional[Dict[str, str]] = None
    tubing_fluid_amount_ul: Dict[str, int]

    @model_validator(mode='after')
    def _check_valve_id_consistency(self):
        ids = set(self.valve_ids)
        for field_name in ('number_of_ports', 'tubing_fluid_amount_to_valve_ul'):
            keys = set(getattr(self, field_name).keys())
            if keys != ids:
                missing = ids - keys
                extra = keys - ids
                parts = []
                if missing:
                    parts.append(f"missing {missing}")
                if extra:
                    parts.append(f"extra {extra}")
                raise ValueError(
                    f"{field_name} keys don't match valve_ids: {', '.join(parts)}"
                )
        return self


class ReagentSelectionConfig(_StrictModel):
    selector_valves: SelectorValvesConfig
    common_tubing_fluid_amount_ul: int


class SampleSelectionInletConfig(_StrictModel):
    common_tubing_fluid_amount_ul: int


class SamplesConfig(_StrictModel):
    chamber_volume_ul: int


class TemperatureControllerConfig(_StrictModel):
    serial_number: str
    channels: Literal[1, 2] = 2
    tolerance_celsius: float = Field(default=1.0, gt=0)
    stabilization_timeout_seconds: float = Field(default=300, gt=0)


class FlowSensorConfig(_StrictModel):
    """One SLF3X flow sensor on the Teensy's I2C bus.

    index is the I2C bus the sensor sits on (1 = Wire1, 2 = Wire2). Bus 0
    is excluded: it is shared with the selector valves, whose driver emits
    a general-call transaction after every command.

    The monitor fields are per-sensor tuning for draw protection, consumed in
    the operations layer:

      off   read and plot only; the sensor never stops anything
      warn  log a fault and carry on -- the mode to run first on a new setup,
            to see what the rule would have fired on before it can halt a run
      stop  halt the draw and fail the sequence

    monitor is the starting mode only; the GUI switches it per sensor at
    runtime. Bad tuning is otherwise only discoverable by restarting a run.
    """
    index: Literal[1, 2]
    name: str
    monitor: Literal["off", "warn", "stop"] = "off"
    ramp_up_seconds: float = Field(default=3.0, gt=0)
    tolerance_fraction: float = Field(default=0.3, gt=0, le=1)
    max_flow_rate_ul_min: float = Field(default=2000, gt=0)


class FluidicsConfig(_StrictModel):
    config_version: str
    microcontroller: MicrocontrollerConfig
    syringe_pump: SyringePumpConfig
    reagent_selection: ReagentSelectionConfig
    sample_selection_inlet: Optional[SampleSelectionInletConfig] = None
    samples: Optional[SamplesConfig] = None
    temperature_controller: Optional[TemperatureControllerConfig] = None
    flow_sensors: Optional[List[FlowSensorConfig]] = Field(default=None, min_length=1)
    application: Literal["Flow Cell", "Open Chamber"]

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

        # Two is the hardware ceiling: slot i is transmitted at packet bytes
        # 23 + 2*i, and a third would grow the packet past MCU_MSG_LENGTH.
        # Unique indices already bound the count, so this only fires if the
        # Literal on `index` is ever widened.
        if len(self.flow_sensors) > 2:
            raise ValueError(
                "at most two flow sensors are supported; the status packet has "
                "room for two readings (bytes 23-24 and 25-26)"
            )
        return self


# --- Legacy JSON to v2.0 YAML Conversion ---

def convert_legacy_config(old: dict) -> dict:
    """Convert old JSON config dict to v2.0 config dict."""
    application = old['application']
    is_flow_cell = (application == 'MERFISH')

    new = {}
    new['config_version'] = '2.0'
    new['microcontroller'] = dict(old['microcontroller'])
    new['syringe_pump'] = dict(old['syringe_pump'])

    # Reagent selection (wraps old selector_valves)
    sv_old = old['selector_valves']
    sv_new = {}
    sv_new['valve_ids'] = sv_old['valve_ids_allowed']

    sv_new['number_of_ports'] = {
        int(k): v for k, v in sv_old['number_of_ports'].items() if v is not None
    }

    old_valve_amounts = {
        int(k): v for k, v in sv_old['tubing_fluid_amount_to_valve_ul'].items()
        if v is not None
    }

    if is_flow_cell:
        # common = valve 0's value; per-valve = old - common
        common_amount = old_valve_amounts.get(0, 0)
        new_valve_amounts = {k: v - common_amount for k, v in old_valve_amounts.items()}
    else:
        # per-valve stays as-is; common comes from tubing_fluid_amount_sv_to_sp_ul
        new_valve_amounts = old_valve_amounts
        common_amount = old.get('tubing_fluid_amount_sv_to_sp_ul', 0)

    sv_new['tubing_fluid_amount_to_valve_ul'] = new_valve_amounts

    name_mapping = {
        k: v for k, v in sv_old['reagent_name_mapping'].items()
        if v is not None and v != ''
    }
    if name_mapping:
        sv_new['name_mapping'] = name_mapping

    sv_new['tubing_fluid_amount_ul'] = {
        k: v for k, v in sv_old['tubing_fluid_amount_to_port_ul'].items()
        if v is not None
    }

    new['reagent_selection'] = {
        'selector_valves': sv_new,
        'common_tubing_fluid_amount_ul': common_amount,
    }

    # Open chamber specific sections
    if not is_flow_cell:
        sp_to_oc = old.get('tubing_fluid_amount_sp_to_oc_ul')
        if sp_to_oc is not None:
            new['sample_selection_inlet'] = {
                'common_tubing_fluid_amount_ul': sp_to_oc,
            }

        chamber_vol = old.get('chamber_volume_ul')
        if chamber_vol is not None:
            new['samples'] = {'chamber_volume_ul': chamber_vol}

        tc = old.get('temperature_controller')
        if tc and tc.get('use_temperature_controller'):
            new['temperature_controller'] = {'serial_number': tc['serial_number']}

    new['application'] = 'Flow Cell' if is_flow_cell else application
    return new


def available_port_count(config: FluidicsConfig) -> int:
    """How many fluidic ports the configured cascade offers.

    The last port of every valve except the final one routes to the next
    valve, so it is plumbing, not a reagent port. Written once here -- pure
    config arithmetic -- so SelectorValveSystem (with hardware attached) and
    the pre-run sequence check (without) cannot disagree about the range.
    """
    sv = config.reagent_selection.selector_valves
    return sum(sv.number_of_ports[v] - 1 for v in sv.valve_ids) + 1


# --- Config Loading ---

def load_config(config_path: str) -> FluidicsConfig:
    """Load config from YAML or JSON path. Auto-converts JSON to YAML v2.0."""
    base, ext = os.path.splitext(config_path)
    yaml_path = base + '.yaml'

    if ext == '.json':
        if os.path.exists(yaml_path):
            # YAML already exists alongside JSON — use it
            config_path = yaml_path
        else:
            # Convert JSON → YAML
            with open(config_path) as f:
                old_data = json.load(f)
            new_data = convert_legacy_config(old_data)
            with open(yaml_path, 'w') as f:
                yaml.dump(new_data, f, default_flow_style=False, sort_keys=False)
            config_path = yaml_path

    with open(config_path) as f:
        data = yaml.safe_load(f)

    return FluidicsConfig(**data)
