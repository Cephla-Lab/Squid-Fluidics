# tests/unit/test_sequences.py
import pytest
import yaml
from pydantic import ValidationError

from fluidics.sequences import (
    APPLICATION_SEQUENCES,
    SEQUENCE_TYPES,
    SEQUENCE_TYPE_LABELS,
    SequenceListAdapter,
    load_sequences,
    save_sequences_yaml,
    get_included_sequences,
    get_fields_for_type,
)


class TestSequenceModels:
    def test_flow_reagent_valid(self):
        seq = SequenceListAdapter.validate_python([
            {"type": "flow_reagent", "fluidic_port": 1, "flow_rate": 5000, "volume": 2000}
        ])
        assert seq[0].type == "flow_reagent"
        assert seq[0].repeat == 1  # default
        assert seq[0].include is True  # default
        assert seq[0].incubation_time == 0  # default

    def test_set_temperature_valid(self):
        seq = SequenceListAdapter.validate_python([
            {"type": "set_temperature", "temperature": 37.5}
        ])
        assert seq[0].temperature == 37.5

    def test_set_temperature_no_fluidic_fields(self):
        """set_temperature shouldn't accept fluidic_port."""
        with pytest.raises(ValidationError):
            SequenceListAdapter.validate_python([
                {"type": "set_temperature", "temperature": 37, "fluidic_port": 1}
            ])

    def test_unknown_type_rejected(self):
        with pytest.raises(ValidationError):
            SequenceListAdapter.validate_python([
                {"type": "nonexistent", "fluidic_port": 1, "flow_rate": 100, "volume": 100}
            ])

    def test_extra_fields_rejected(self):
        """extra='forbid' catches typos."""
        with pytest.raises(ValidationError):
            SequenceListAdapter.validate_python([
                {"type": "flow_reagent", "fluidic_port": 1, "flow_rate": 5000,
                 "volume": 2000, "typo_field": 123}
            ])

    def test_missing_required_field_rejected(self):
        with pytest.raises(ValidationError):
            SequenceListAdapter.validate_python([
                {"type": "flow_reagent", "fluidic_port": 1}  # missing flow_rate, volume
            ])

    def test_fill_tubing_with_optional(self):
        seq = SequenceListAdapter.validate_python([
            {"type": "flow_reagent", "fluidic_port": 1, "flow_rate": 5000, "volume": 2000}
        ])
        assert seq[0].fill_tubing_with is None

    def test_priming_no_fill_tubing_with(self):
        """Priming model doesn't have fill_tubing_with field."""
        with pytest.raises(ValidationError):
            SequenceListAdapter.validate_python([
                {"type": "priming", "fluidic_port": 1, "flow_rate": 5000,
                 "volume": 2000, "fill_tubing_with": 5}
            ])

    @pytest.mark.parametrize("field,value", [
        ("fluidic_port", 0),  # ge=1
        ("flow_rate", 0),     # gt=0
        ("volume", -1),       # gt=0
        ("repeat", 0),        # ge=1
        ("incubation_time", -1),  # ge=0
    ])
    def test_field_constraints(self, field, value):
        data = {"type": "flow_reagent", "fluidic_port": 1, "flow_rate": 5000, "volume": 2000}
        data[field] = value
        with pytest.raises(ValidationError):
            SequenceListAdapter.validate_python([data])


class TestSequenceLoadingYAML:
    def test_load_yaml(self, fixtures_dir):
        seqs = load_sequences(str(fixtures_dir / "valid_sequences.yaml"))
        assert len(seqs) > 0
        assert all("type" in s for s in seqs)

    def test_load_yaml_with_sequences_key(self, tmp_path):
        data = {"sequences": [
            {"type": "flow_reagent", "fluidic_port": 1, "flow_rate": 1000, "volume": 500}
        ]}
        path = tmp_path / "seqs.yaml"
        with open(path, "w") as f:
            yaml.safe_dump(data, f)
        seqs = load_sequences(str(path))
        assert len(seqs) == 1

    def test_load_yaml_bare_list(self, tmp_path):
        data = [{"type": "flow_reagent", "fluidic_port": 1, "flow_rate": 1000, "volume": 500}]
        path = tmp_path / "seqs.yaml"
        with open(path, "w") as f:
            yaml.safe_dump(data, f)
        seqs = load_sequences(str(path))
        assert len(seqs) == 1

    def test_load_empty_yaml(self, tmp_path):
        path = tmp_path / "empty.yaml"
        path.write_text("")
        seqs = load_sequences(str(path))
        assert seqs == []

    def test_unsupported_extension_raises(self):
        with pytest.raises(ValueError, match="Unsupported"):
            load_sequences("file.txt")


class TestSequenceLoadingCSV:
    def test_load_csv(self, fixtures_dir):
        seqs = load_sequences(str(fixtures_dir / "legacy_sequences.csv"))
        assert len(seqs) > 0
        types = [s["type"] for s in seqs]
        assert "flow_reagent" in types

    def test_csv_set_temperature_parsed(self, fixtures_dir):
        seqs = load_sequences(str(fixtures_dir / "legacy_sequences.csv"))
        temp_seqs = [s for s in seqs if s["type"] == "set_temperature"]
        assert len(temp_seqs) == 1
        assert temp_seqs[0]["temperature"] == 50.0


class TestSaveSequences:
    def test_round_trip(self, tmp_path):
        original = [
            {"type": "flow_reagent", "fluidic_port": 1, "flow_rate": 5000, "volume": 2000,
             "fill_tubing_with": 10, "incubation_time": 3},
            {"type": "set_temperature", "temperature": 37},
        ]
        path = str(tmp_path / "out.yaml")
        save_sequences_yaml(original, path)
        loaded = load_sequences(path)
        assert loaded[0]["type"] == "flow_reagent"
        assert loaded[0]["fluidic_port"] == 1
        assert loaded[1]["type"] == "set_temperature"
        assert loaded[1]["temperature"] == 37

    def test_defaults_excluded(self, tmp_path):
        original = [
            {"type": "flow_reagent", "fluidic_port": 1, "flow_rate": 1000, "volume": 500}
        ]
        path = str(tmp_path / "out.yaml")
        save_sequences_yaml(original, path)
        with open(path) as f:
            raw = yaml.safe_load(f)
        seq = raw["sequences"][0]
        assert "repeat" not in seq
        assert "include" not in seq
        assert "incubation_time" not in seq


class TestSequenceUtilities:
    def test_get_included_sequences(self):
        seqs = [
            {"type": "flow_reagent", "include": True},
            {"type": "priming", "include": False},
            {"type": "clean_up"},  # default True
        ]
        result = get_included_sequences(seqs)
        assert len(result) == 2

    def test_get_fields_for_type(self):
        fields = get_fields_for_type("flow_reagent")
        assert "fluidic_port" in fields
        assert "fill_tubing_with" in fields
        assert "type" not in fields

    def test_get_fields_for_unknown_type(self):
        with pytest.raises(ValueError, match="Unknown sequence type"):
            get_fields_for_type("nonexistent")


class TestRegistryConsistency:
    def test_all_labels_have_types(self):
        assert SEQUENCE_TYPE_LABELS.keys() <= SEQUENCE_TYPES.keys()

    def test_all_types_have_labels(self):
        assert SEQUENCE_TYPES.keys() <= SEQUENCE_TYPE_LABELS.keys()

    def test_application_sequences_are_valid_types(self):
        for app, seq_types in APPLICATION_SEQUENCES.items():
            for t in seq_types:
                assert t in SEQUENCE_TYPES, f"{t} not in SEQUENCE_TYPES"

    def test_flow_cell_includes_set_temperature(self):
        assert "set_temperature" in APPLICATION_SEQUENCES["Flow Cell"]
