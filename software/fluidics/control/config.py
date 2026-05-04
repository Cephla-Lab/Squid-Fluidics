"""Fluidics system configuration: pydantic models, legacy conversion, and loading."""

from __future__ import annotations

import json
import os
from typing import Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, Field, model_validator


# --- Pydantic Models ---

class MicrocontrollerConfig(BaseModel):
    serial_number: str


class SyringePumpConfig(BaseModel):
    serial_number: str
    volume_ul: int = Field(gt=0)
    ports_allowed: List[int]
    waste_port: int
    extract_port: int
    dispense_port: Optional[int] = None
    speed_code_limit: int = Field(ge=0, le=40)


class SelectorValvesConfig(BaseModel):
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


class ReagentSelectionConfig(BaseModel):
    selector_valves: SelectorValvesConfig
    common_tubing_fluid_amount_ul: int


class SampleSelectionInletConfig(BaseModel):
    common_tubing_fluid_amount_ul: int


class SamplesConfig(BaseModel):
    chamber_volume_ul: int


class TemperatureControllerConfig(BaseModel):
    serial_number: str
    channels: Literal[1, 2] = 2
    tolerance_celsius: float = Field(default=1.0, gt=0)
    stabilization_timeout_seconds: float = Field(default=300, gt=0)


class FluidicsConfig(BaseModel):
    config_version: str
    microcontroller: MicrocontrollerConfig
    syringe_pump: SyringePumpConfig
    reagent_selection: ReagentSelectionConfig
    sample_selection_inlet: Optional[SampleSelectionInletConfig] = None
    samples: Optional[SamplesConfig] = None
    temperature_controller: Optional[TemperatureControllerConfig] = None
    application: Literal["Flow Cell", "Open Chamber"]


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
