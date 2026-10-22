"""Fluidics system configuration: pydantic models, legacy conversion, and loading."""

from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Literal, Optional, Tuple

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from ..files import atomic_write
from ..robot.labware import RESERVOIR_NAME, PlateGeometry, agitation_problem
from ..robot.stage import MIN_TRAVEL_Z_MM


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
    # Optional per port: a bulk bottle the reservoir ledger tracks (spec D6)
    # and what its tube end cannot reach. A port without an entry is not
    # tracked, which is how every rig behaved before.
    loaded_volume_ul: Optional[Dict[str, int]] = None
    residual_volume_ul: Optional[Dict[str, int]] = None

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

    @model_validator(mode='after')
    def _check_port_keys(self):
        """The port dicts are keyed by one spelling: `port_<n>`, within the
        cascade's reach.

        These keys decide which ports the rig offers -- a tubing volume is
        how the config says a port is plumbed -- and only this spelling is
        ever read back. So a typo used to *remove* a port: `port_2s` left
        port 2 with no volume, and the GUI, the editor and the pre-run
        check all stopped offering it, with nothing pointing at the config
        line. Say so at load instead.
        """
        reach = sum(self.number_of_ports[v] - 1 for v in self.valve_ids) + 1
        allowed = {f"port_{port}" for port in range(1, reach + 1)}
        for field_name in ('tubing_fluid_amount_ul', 'name_mapping',
                           'loaded_volume_ul', 'residual_volume_ul'):
            keys = getattr(self, field_name) or {}
            unknown = sorted(set(keys) - allowed)
            if unknown:
                raise ValueError(
                    f"{field_name} has keys that name no port on this "
                    f"cascade: {', '.join(unknown)}. Ports are spelled "
                    f"port_1..port_{reach}."
                )
        return self

    @model_validator(mode='after')
    def _check_tracked_bottles(self):
        """A tracked bottle hangs on a plumbed port, holds more than its
        tube end cannot reach, and a residual names a tracked bottle.

        Defined after _check_port_keys so pydantic runs it second: a key
        beyond the cascade's reach is reported as such, not as unplumbed."""
        loaded = self.loaded_volume_ul or {}
        residual = self.residual_volume_ul or {}
        for key in residual:
            if key not in loaded:
                raise ValueError(f"residual_volume_ul.{key}: no loaded_volume_ul for it")
        for key, volume in loaded.items():
            if not self.tubing_fluid_amount_ul.get(key):
                raise ValueError(f"loaded_volume_ul.{key}: not a plumbed port "
                                 "(no tubing_fluid_amount_ul entry)")
            left = residual.get(key, 0)
            if left < 0:
                raise ValueError(f"residual_volume_ul.{key} must not be negative")
            if volume <= left:
                raise ValueError(f"loaded_volume_ul.{key} ({volume}) must exceed its residual ({left})")
        return self


PlateAxis = Literal["+x", "-x", "+y", "-y"]    # a stage direction a plate's columns or rows run along


class ReservoirConfig(_StrictModel):
    """One plate the sipper serves (spec §3): where A1 was taught, how deep
    the tip dips, which stage axis the columns and the rows run along, and
    what every well holds."""
    name: str
    type: Literal["plate"]
    number_of_wells: Literal[96]
    well_spacing: int = Field(default=1, ge=1)       # pitch multiplier; 1 = every well
    a1_xy_mm: Tuple[float, float]
    dip_z_mm: float
    # No default: the only deck runs its columns along +y and its rows along
    # +x, so a block that leaves these out is refused at load rather than
    # placed on a guess the corner reach check cannot catch.
    column_axis: PlateAxis      # columns 1..12 run along this
    row_axis: PlateAxis         # rows A..H run along this
    loaded_ul: int = Field(gt=0)
    residual_ul: int = Field(ge=0)
    well_names: Optional[Dict[str, str]] = None
    loaded_ul_by_well: Optional[Dict[str, int]] = None

    @property
    def geometry(self):
        """The plate's wells, from number_of_wells."""
        return PlateGeometry.from_well_count(self.number_of_wells)

    @model_validator(mode='after')
    def _check_wells_and_volumes(self):
        if not re.fullmatch(RESERVOIR_NAME, self.name):
            raise ValueError(f"reservoir name {self.name!r}: letters, digits, _ and - only")
        if self.name == "all" or re.fullmatch(r"port_\d+", self.name):
            raise ValueError(f"reservoir name {self.name!r} is reserved (all, port_N)")
        if self.column_axis[1] == self.row_axis[1]:
            raise ValueError(f"{self.name}: column_axis and row_axis must be perpendicular")
        geometry = self.geometry
        for field_name in ('well_names', 'loaded_ul_by_well'):
            for well in (getattr(self, field_name) or {}):
                if not geometry.is_well(well):
                    raise ValueError(f"{self.name}.{field_name}: {well} is not a well on this plate")
        if self.loaded_ul <= self.residual_ul:
            raise ValueError(f"{self.name}: loaded_ul must exceed residual_ul")
        for well, loaded in (self.loaded_ul_by_well or {}).items():
            if loaded <= self.residual_ul:
                raise ValueError(f"{self.name}.loaded_ul_by_well.{well} must exceed residual_ul")
        return self


class GantryConfig(_StrictModel):
    """The XYZ robot that carries the sipper (spec §3), keyed as You Yan
    wrote it: `reagent_selection.nimotion_gantry_system`."""
    selector_valve_port: int = Field(ge=1)
    push_volume_ul: int = Field(gt=0)        # what a push row draws: the tip-to-flow-cell volume
    serial_number: Optional[str] = None
    port: Optional[str] = None
    baudrate: int = 115200
    axes: Dict[Literal["x", "y", "z"], str]
    travel_z_mm: float = Field(ge=MIN_TRAVEL_Z_MM)      # homing parks Z up to 0.5 mm past 0
    xy_velocity_mm_s: float = Field(gt=0)
    z_velocity_mm_s: float = Field(gt=0)
    default_reservoir: str
    reservoirs: Dict[int, ReservoirConfig]
    priming_well: str
    clean_up_well: str
    agitate_amplitude_mm: float = Field(gt=0)

    @model_validator(mode='after')
    def _check_names_and_wells(self):
        from ..vocabulary import SipperVocabulary      # function-time: vocabulary imports this module
        if (self.serial_number is None) == (self.port is None):
            raise ValueError("nimotion_gantry_system: give exactly one of serial_number or port")
        if set(self.axes) != {"x", "y", "z"}:
            raise ValueError("nimotion_gantry_system.axes needs x, y and z")
        if len(set(self.axes.values())) != 3:
            raise ValueError("nimotion_gantry_system.axes must map x, y and z to three distinct axes")
        names = [r.name for r in self.reservoirs.values()]
        if len(set(names)) != len(names):
            raise ValueError("reservoir names must be unique")
        by_name = {r.name: r for r in self.reservoirs.values()}
        if self.default_reservoir not in by_name:
            raise ValueError(f"default_reservoir {self.default_reservoir!r} names no reservoir")
        vocabulary = SipperVocabulary(self.selector_valve_port, self.default_reservoir, by_name)
        for field_name in ('priming_well', 'clean_up_well'):
            try:
                vocabulary.resolve(getattr(self, field_name))
            except ValueError as e:
                raise ValueError(f"{field_name}: {e}") from None
        for r in by_name.values():
            problem = agitation_problem(r.name, r.dip_z_mm, self.agitate_amplitude_mm, self.travel_z_mm)
            if problem is not None:
                raise ValueError(problem)
        return self


class ReagentSelectionConfig(_StrictModel):
    selector_valves: SelectorValvesConfig
    common_tubing_fluid_amount_ul: int
    nimotion_gantry_system: Optional[GantryConfig] = None


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

    # The resolved file this config was loaded from (always the YAML, even
    # when the operator pointed at a legacy JSON) -- what save_config writes
    # back to. Stamped by load_config; None on a config built in memory.
    _source_path: Optional[str] = PrivateAttr(default=None)

    @property
    def source_path(self):
        return self._source_path

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

    @model_validator(mode='after')
    def _check_gantry(self):
        g = self.reagent_selection.nimotion_gantry_system
        if g is None:
            return self
        if self.application != "Flow Cell":
            raise ValueError("nimotion_gantry_system is only supported with the Flow Cell application")
        if g.selector_valve_port not in available_ports(self):
            raise ValueError(
                f"selector_valve_port {g.selector_valve_port} is not a plumbed port: give it a "
                f"tubing_fluid_amount_ul entry (the sipper's tip-to-valve volume)")
        if port_key(g.selector_valve_port) in (self.reagent_selection.selector_valves.loaded_volume_ul or {}):
            raise ValueError(f"selector_valve_port {g.selector_valve_port} is the sipper's: its wells "
                             "are tracked by the plates, not by loaded_volume_ul")
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


def port_key(port: int) -> str:
    """The name_mapping key for fluidic port `port` -- the one spelling,
    read by the valve system and written by the GUI's rename dialog."""
    return f"port_{port}"


def port_of(key):
    """The inverse of port_key: the port a `port_N` key names, or None for
    any other key (a well's, `ssct/A1`)."""
    name, _, number = key.partition("_")
    return int(number) if name == "port" and number.isdigit() else None


def gantry(config):
    """The gantry block, or None on a rig without one -- the one spelling
    of where it lives."""
    return config.reagent_selection.nimotion_gantry_system


def save_config(config: FluidicsConfig, config_path: str = None) -> str:
    """Write `config` back to the file it was loaded from (or an explicit
    `config_path`); returns the path written.

    Round-tripped with ruamel.yaml so the file's own comments and layout
    survive -- a per-rig config is hand-maintained, and renaming a port
    must not cost the plumbing notes. The dump is exclude_unset: only what
    the file provided or the operator assigned is written. Two consequences
    for future editors: to persist a field the file never had, assign it --
    in-place mutation of a defaulted value is silently not written; and
    every assignment made to the config since load rides along with any
    save, so keep runtime state off the config object.
    """
    import ruamel.yaml      # deferred: ~7 ms of import for a rare operation

    if config_path is None:
        config_path = config.source_path
    if config_path is None:
        raise ValueError("this config was not loaded from a file; "
                         "pass config_path to save it")
    values = config.model_dump(exclude_unset=True)
    yaml_rt = ruamel.yaml.YAML()    # round-trip mode: keeps comments
    yaml_rt.preserve_quotes = True
    # Spell None as `null`, the way the configs write it -- ruamel's default
    # is an empty scalar, which would restyle every explicit null on save.
    yaml_rt.representer.add_representer(
        type(None),
        lambda representer, _: representer.represent_scalar(
            'tag:yaml.org,2002:null', 'null'))
    with open(config_path, encoding="utf-8") as f:
        document = yaml_rt.load(f)
    _update_yaml_node(document, values)
    with atomic_write(config_path) as f:
        yaml_rt.dump(document, f)
    return config_path


def _update_yaml_node(node, values):
    """Overwrite `node` (a ruamel mapping) with `values`, key by key,
    recursing into mappings so keys keep the comments attached to them.
    A value that did not change is left entirely alone -- the file's own
    text (quoting included) is authoritative for what was not edited, so a
    quoted `monitor: "off"` cannot come back as a bare YAML boolean."""
    # A key gone from a dict-valued field leaves the file: this is how a
    # cleared port name leaves name_mapping. (Model fields themselves cannot
    # disappear -- extra="forbid" means an unknown file key never loads.)
    for key in [k for k in node if k not in values]:
        del node[key]
    for key, value in values.items():
        # model_dump gives a tuple field (a plate's a1_xy_mm) as a tuple, and
        # the file holds it as a list: a list never equals a tuple, so compare
        # it as the file has it or every save rewrites it.
        as_loaded = list(value) if isinstance(value, tuple) else value
        if key in node and isinstance(value, dict) and isinstance(node[key], dict):
            _update_yaml_node(node[key], value)
        elif key not in node or node[key] != as_loaded:
            node[key] = _yaml_faithful(value)


def _yaml_faithful(value):
    """`value`, with any string plain YAML would reinterpret -- "off",
    "yes", "3", "" -- wrapped so it is written quoted: an operator may name
    a port anything, and the name must come back as the same string."""
    if isinstance(value, str):
        try:
            reread = yaml.safe_load(value)
        except yaml.YAMLError:
            reread = None
        if reread != value:
            from ruamel.yaml.scalarstring import DoubleQuotedScalarString
            return DoubleQuotedScalarString(value)
        return value
    if isinstance(value, dict):
        return {key: _yaml_faithful(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_yaml_faithful(inner) for inner in value]
    if isinstance(value, tuple):
        # A coordinate pair, written the way the configs spell it --
        # `[12.0, 1.5]` -- so a comment after it stays on its own line.
        from ruamel.yaml.comments import CommentedSeq
        seq = CommentedSeq(_yaml_faithful(inner) for inner in value)
        seq.fa.set_flow_style()
        return seq
    return value


def port_range_note(ports) -> str:
    """How the rig's ports are put to the operator, wherever they are
    named -- the time-zero gate, the editor's live verdict, and the valve
    system's own refusal at run time.

    The ports need not be contiguous: a position nobody plumbed leaves a
    gap, and the operator is told where the gaps are rather than a range
    that would admit them.
    """
    ports = sorted(ports)
    if not ports:
        return "this configuration has no plumbed ports"
    spans, start = [], ports[0]
    for previous, port in zip(ports, ports[1:] + [None]):
        if port != previous + 1:
            spans.append(f"{start}..{previous}" if start != previous else f"{start}")
            start = port
    return "this configuration has ports " + ", ".join(spans)


def available_port_count(config: FluidicsConfig) -> int:
    """How many positions the configured cascade can address.

    The last port of every valve except the final one routes to the next
    valve, so it is plumbing, not a reagent port. Written once here -- pure
    config arithmetic -- so SelectorValveSystem (with hardware attached) and
    the pre-run sequence check (without) cannot disagree about the range.

    This is the hardware's reach, not the rig's reagent ports: the valves
    are initialized with these counts. What is actually plumbed is
    available_ports().
    """
    sv = config.reagent_selection.selector_valves
    return sum(sv.number_of_ports[v] - 1 for v in sv.valve_ids) + 1


def available_ports(config: FluidicsConfig) -> tuple:
    """The ports this rig offers, in order -- the ones with a tubing
    volume measured for them.

    A port is plumbed or it is not, and the tubing volume is how the
    config says which: every draw through a port needs it, and Priming
    already skipped ports without one. A position the cascade can address
    but nobody has connected a line to is not a port the operator should
    be offered, nor one a sequence file may name -- it would open a valve
    onto nothing and draw air.

    Asked the other way round -- which of the reachable ports has an
    entry, rather than which entries look like ports -- because only
    `port_key`'s spelling is ever read back: `get_tubing_fluid_amount_to_port`
    looks up `port_1` and gets None for anything else. Parsing the keys
    instead would offer a port whose volume then reads as missing, and let
    `port_01` and `port_1` name the same one twice.

    A volume of zero is no line either, and is skipped: Priming and Clean
    Up have always read it that way, and one rule for "is plumbed" beats a
    port that the GUI offers, validation passes, and priming then silently
    steps over.
    """
    entries = config.reagent_selection.selector_valves.tubing_fluid_amount_ul
    return tuple(port for port in range(1, available_port_count(config) + 1)
                 if entries.get(port_key(port)))


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
            with open(config_path, encoding="utf-8") as f:
                old_data = json.load(f)
            new_data = convert_legacy_config(old_data)
            with atomic_write(yaml_path) as f:
                yaml.dump(new_data, f, default_flow_style=False, sort_keys=False)
            config_path = yaml_path

    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    config = FluidicsConfig(**data)
    # The resolved YAML, absolute: what save_config writes back to.
    config._source_path = os.path.abspath(config_path)
    return config
