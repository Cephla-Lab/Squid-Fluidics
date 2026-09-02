"""Sequence loading, validation, and saving."""

from __future__ import annotations

import re
from typing import Annotated, Literal, Optional, Union, get_args

import yaml
from pydantic import (BaseModel, ConfigDict, Discriminator, Field, TypeAdapter,
                      ValidationError)

from .control.config import available_port_count, port_range_note
from .files import atomic_write


# --- Pydantic Models ---


class SequenceBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None  # custom user label
    repeat: int = Field(default=1, ge=1)
    include: bool = True
    incubation_time: float = Field(default=0, ge=0)
    round: Optional[str] = None  # free grouping label (Squid renders rounds; harmless elsewhere)


class FluidicSequence(SequenceBase):
    """Base for sequences that operate on a fluidic port."""

    fluidic_port: int = Field(ge=1)
    flow_rate: int = Field(gt=0)
    volume: int = Field(gt=0)


class FlowReagentSequence(FluidicSequence):
    type: Literal["flow_reagent"]
    fill_tubing_with: Optional[int] = None


class AddReagentSequence(FluidicSequence):
    type: Literal["add_reagent"]
    fill_tubing_with: Optional[int] = None


class ClearAndAddReagentSequence(FluidicSequence):
    type: Literal["clear_and_add_reagent"]
    fill_tubing_with: Optional[int] = None


class WashConstantFlowSequence(FluidicSequence):
    type: Literal["wash_constant_flow"]
    fill_tubing_with: Optional[int] = None


class PrimingSequence(FluidicSequence):
    type: Literal["priming"]


class CleanUpSequence(FluidicSequence):
    type: Literal["clean_up"]


class SetTemperatureSequence(SequenceBase):
    type: Literal["set_temperature"]
    temperature: float


Sequence = Annotated[
    Union[
        FlowReagentSequence,
        AddReagentSequence,
        ClearAndAddReagentSequence,
        WashConstantFlowSequence,
        PrimingSequence,
        CleanUpSequence,
        SetTemperatureSequence,
    ],
    Discriminator("type"),
]

SequenceListAdapter = TypeAdapter(list[Sequence])


# --- Type registry and per-application sequence lists ---


# Derive type registry from the Sequence union so it stays in sync automatically.
SEQUENCE_TYPES: dict[str, type[SequenceBase]] = {}
for _cls in get_args(get_args(Sequence)[0]):
    _type_field = _cls.model_fields["type"]
    _type_key = get_args(_type_field.annotation)[0]
    SEQUENCE_TYPES[_type_key] = _cls

SEQUENCE_TYPE_LABELS: dict[str, str] = {
    "flow_reagent": "Flow Reagent",
    "add_reagent": "Add Reagent",
    "clear_and_add_reagent": "Clear Tubings and Add Reagent",
    "wash_constant_flow": "Wash with Constant Flow",
    "priming": "Priming",
    "clean_up": "Clean Up",
    "set_temperature": "Set Temperature",
}

APPLICATION_SEQUENCES: dict[str, list[str]] = {
    "Flow Cell": ["flow_reagent", "priming", "clean_up", "set_temperature"],
    "Open Chamber": [
        "add_reagent",
        "clear_and_add_reagent",
        "wash_constant_flow",
        "priming",
        "clean_up",
        "set_temperature",
    ],
}

# Derive CSV name-to-type mapping from labels (inverse, excluding set_temperature
# which is handled via regex pattern matching in _load_csv).
_CSV_NAME_TO_TYPE: dict[str, str] = {
    v: k for k, v in SEQUENCE_TYPE_LABELS.items() if k != "set_temperature"
}


# --- Load / save functions ---


def load_sequences(path: str) -> list[dict]:
    """Load sequences from a YAML or CSV file.

    Dispatches to the appropriate loader based on file extension.
    Returns a list of validated sequence dicts.
    """
    if path.endswith((".yaml", ".yml")):
        return _load_yaml(path)
    elif path.endswith(".csv"):
        return _load_csv(path)
    else:
        raise ValueError(f"Unsupported file extension: {path}")


def _load_yaml(path: str) -> list[dict]:
    """Load sequences from a YAML file, validate, and return as dicts."""
    with open(path, "r") as f:
        data = yaml.safe_load(f)
    if data is None:
        return []
    raw = data.get("sequences", data) if isinstance(data, dict) else data
    validated = SequenceListAdapter.validate_python(raw)
    return [seq.model_dump() for seq in validated]


def _load_csv(path: str) -> list[dict]:
    """Load sequences from a legacy CSV file, map to typed dicts, validate, and return."""
    import pandas as pd
    df = pd.read_csv(path)
    sequences = []
    for _, row in df.iterrows():
        seq_name = row["sequence_name"]

        # Handle "Set Temperature XX" pattern
        temp_match = re.match(r"^Set Temperature\s+([\d.]+)$", seq_name)
        if temp_match:
            seq_dict = {
                "type": "set_temperature",
                "temperature": float(temp_match.group(1)),
            }
        else:
            seq_type = _CSV_NAME_TO_TYPE.get(seq_name)
            if seq_type is None:
                raise ValueError(
                    f"Unknown CSV sequence_name: {seq_name!r}. "
                    f"Known names: {list(_CSV_NAME_TO_TYPE.keys())}"
                )
            seq_dict = {
                "type": seq_type,
                "fluidic_port": int(row["fluidic_port"]),
                "flow_rate": int(row["flow_rate"]),
                "volume": int(row["volume"]),
            }
            # Only add fill_tubing_with if the target model supports it
            model = SEQUENCE_TYPES[seq_type]
            if "fill_tubing_with" in model.model_fields:
                raw_val = row.get("fill_tubing_with")
                if pd.notna(raw_val) and int(raw_val) != 0:
                    seq_dict["fill_tubing_with"] = int(raw_val)

        # Common fields
        if "incubation_time" in row and pd.notna(row["incubation_time"]):
            val = float(row["incubation_time"])
            if val != 0:
                seq_dict["incubation_time"] = val

        if "repeat" in row and pd.notna(row["repeat"]):
            val = int(row["repeat"])
            if val != 1:
                seq_dict["repeat"] = val

        if "include" in row and pd.notna(row["include"]):
            val = bool(int(row["include"]))
            if not val:
                seq_dict["include"] = val

        sequences.append(seq_dict)

    validated = SequenceListAdapter.validate_python(sequences)
    return [seq.model_dump() for seq in validated]


def save_sequences_yaml(sequences: list[dict], path: str) -> None:
    """Validate sequences and write them to a YAML file.

    Fields with default values are excluded for cleaner output.
    The 'type' field is placed first in each entry.
    """
    validated = SequenceListAdapter.validate_python(sequences)
    dumped = [seq.model_dump(exclude_defaults=True) for seq in validated]

    # Reorder so 'type' comes first in each dict
    reordered = []
    for d in dumped:
        ordered = {}
        if "type" in d:
            ordered["type"] = d.pop("type")
        ordered.update(d)
        reordered.append(ordered)

    with atomic_write(path) as f:
        yaml.safe_dump({"sequences": reordered}, f, default_flow_style=False, sort_keys=False)


def sequence_port_problems(seq: dict, limit: int) -> list[str]:
    """The out-of-range port fields of one sequence, as "field=value"
    messages; empty when every port fits within `limit`.

    A falsy fill_tubing_with (None, or the GUI dialog's 0) means "no fill"
    and is skipped, matching how the operations interpret it.

    The port-valued fields are listed here by hand -- there is no model
    metadata to derive them from. A new sequence type that carries a port
    under another name must be added below, or it only fails at run time
    through open_port's backstop. This is the one copy of that list: the
    entry points' pre-run check and the GUI's live per-row validation both
    read it.
    """
    problems = []
    port = seq.get("fluidic_port")
    if port is not None and not 1 <= port <= limit:
        problems.append(f"fluidic_port={port}")
    fill = seq.get("fill_tubing_with")
    if fill and not 1 <= fill <= limit:
        problems.append(f"fill_tubing_with={fill}")
    return problems


def types_for_application(application: str) -> list[str]:
    """The sequence types a rig with `application` offers -- one policy for
    an unknown application (nothing), shared by the gate, the live paint,
    and the Add dialog."""
    return APPLICATION_SEQUENCES.get(application, [])


def label_for_type(seq_type: str) -> str:
    """A sequence type as the operator reads it, or the raw type if this
    rig has no label for it -- the one lookup over SEQUENCE_TYPE_LABELS."""
    return SEQUENCE_TYPE_LABELS.get(seq_type, seq_type)


def sequence_label(seq: dict) -> Optional[str]:
    """How a sequence is named in messages and logs: its own name, else
    its raw type. The machine-readable half of the pair -- what the
    editor shows a row is SequenceList.title(), which falls back to the
    type's operator-facing label instead."""
    return seq.get("name") or seq.get("type")


def sequence_type_problem(seq: dict, application: str) -> Optional[str]:
    """The type complaint for one sequence under `application`, or None.

    The pydantic models cannot decide this -- the union admits every type;
    which subset a rig offers lives in the config's application. One copy,
    read by the entry points' pre-run check and the GUI's live per-row
    validation, like sequence_port_problems for ports."""
    allowed = types_for_application(application)
    if seq.get("type") in allowed:
        return None
    return (f"{seq.get('type')}: not a {application} sequence type "
            f"(this rig offers {', '.join(allowed)})")


def check_types_against_application(sequences: list[dict], config) -> None:
    """Raise ValueError if a sequence's type is not offered by this rig's
    application.

    Until now only the GUI's Add dialog consulted APPLICATION_SEQUENCES: a
    wrong-application file passed loading and the port check, silently
    degraded the estimate to its fallback, and failed only at run time --
    mid-experiment, hours in. Both entry points call this before anything
    moves, next to check_ports_against_config.
    """
    problems = []
    for index, seq in enumerate(sequences):
        problem = sequence_type_problem(seq, config.application)
        if problem is not None:
            problems.append(f"sequence {index} ({sequence_label(seq)}): {problem}")
    if problems:
        raise ValueError("; ".join(problems))


def validate_sequences(sequences: list[dict], config) -> None:
    """Everything a run must pass at time zero, in one call: ports within
    the rig's range, types the rig's application offers.

    FluidicsSystem.run calls this, so every run passes it whoever starts
    one. The GUI and the CLI also call it before they get there, to say
    it their own way at the button and at the command line -- a check
    added here cannot be missing from any of them (types were, until
    #36; the system facade itself was, until this gate moved into it)."""
    check_ports_against_config(sequences, config)
    check_types_against_application(sequences, config)


def sequence_problem(seq: dict, application: str, limit: int) -> Optional[str]:
    """The verdict on one sequence, as a message or None -- the order the
    complaints are asked in, in one place.

    The type first: a wrong-application row is also union-valid, and for
    an unknown type this message beats the union's tag complaint. A pure
    question -- the caller's dict is never rewritten; the coercion happens
    on a copy here, and for real in SequenceListAdapter.
    """
    type_problem = sequence_type_problem(seq, application)
    if type_problem is not None:
        return type_problem
    try:
        validated = SequenceListAdapter.validate_python([seq])
    except ValidationError as e:
        first = e.errors()[0]
        field = ".".join(str(part) for part in first["loc"][2:]) or "sequence"
        return f"{field}: {first['msg']}"
    problems = sequence_port_problems(validated[0].model_dump(), limit)
    if problems:
        return "; ".join(problems) + f": {port_range_note(limit)}"
    return None


def check_ports_against_config(sequences: list[dict], config) -> None:
    """Raise ValueError if any sequence names a port the rig does not have.

    The pydantic models cannot do this -- the upper bound lives in the rig
    config, not the sequence file -- and until now nothing did: an
    out-of-range port survived loading and reached SelectorValveSystem at
    run time, hours into an experiment. Both entry points call this before
    anything moves, so a typo fails at time zero with the sequence named.
    """
    limit = available_port_count(config)
    problems = []
    for index, seq in enumerate(sequences):
        problems.extend(f"sequence {index} ({sequence_label(seq)}): {problem}"
                        for problem in sequence_port_problems(seq, limit))
    if problems:
        raise ValueError(f"Ports out of range -- {port_range_note(limit)}: "
                         + "; ".join(problems))


# Tied to the field it has to agree with, rather than spelled again.
_INCLUDE = TypeAdapter(SequenceBase.model_fields["include"].annotation)


def is_included(seq: dict) -> bool:
    """The include field, defaulting on -- the one spelling of what the
    editor's checkbox means and of what a run takes.

    Coerced the way the models coerce it, not by truthiness, so what a
    run takes cannot disagree with what the row validates as; a value
    even pydantic cannot read counts as included, where the row's own
    verdict stops it. Already-boolean values (every row the loaders and
    the checkbox produce) skip the adapter.
    """
    value = seq.get("include", True)
    if value is True or value is False:
        return value
    try:
        return _INCLUDE.validate_python(value)
    except ValidationError:
        return True


def get_included_sequences(sequences: list[dict]) -> list[dict]:
    """Return only sequences where include is True."""
    return [seq for seq in sequences if is_included(seq)]


def get_fields_for_type(seq_type: str) -> dict:
    """Return model fields for a given sequence type, excluding 'type'.

    Useful for GUI introspection to know which fields to display/edit.
    """
    model = SEQUENCE_TYPES.get(seq_type)
    if model is None:
        raise ValueError(
            f"Unknown sequence type: {seq_type!r}. "
            f"Known types: {list(SEQUENCE_TYPES.keys())}"
        )
    return {k: v for k, v in model.model_fields.items() if k != "type"}
