"""Sequence loading, validation, and saving."""

from __future__ import annotations

import re
from typing import Annotated, Literal, Optional, Union, get_args

import yaml
from pydantic import BaseModel, ConfigDict, Discriminator, Field, TypeAdapter


# --- Pydantic Models ---


class SequenceBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None  # custom user label
    repeat: int = Field(default=1, ge=1)
    include: bool = True
    incubation_time: float = Field(default=0, ge=0)


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

    with open(path, "w") as f:
        yaml.safe_dump({"sequences": reordered}, f, default_flow_style=False, sort_keys=False)


def get_included_sequences(sequences: list[dict]) -> list[dict]:
    """Return only sequences where include is True."""
    return [seq for seq in sequences if seq.get("include", True)]


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
