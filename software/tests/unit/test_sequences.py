# tests/unit/test_sequences.py
import pytest
import yaml
from pydantic import ValidationError

from fluidics.sequences import (
    APPLICATION_SEQUENCES,
    SEQUENCE_TYPES,
    SEQUENCE_TYPE_LABELS,
    SequenceListAdapter,
    check_ports_against_config,
    check_types_against_application,
    sequence_problem,
    sequence_type_problem,
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

    def test_a_save_that_dies_midway_leaves_the_old_file_whole(
            self, tmp_path, monkeypatch):
        """The operator's sequence file must survive a dump that fails."""
        original = [{"type": "flow_reagent", "fluidic_port": 1,
                     "flow_rate": 1000, "volume": 500}]
        path = str(tmp_path / "out.yaml")
        save_sequences_yaml(original, path)
        before = open(path).read()

        def dies_midway(data, stream, **kwargs):
            stream.write("sequences: half a")
            raise OSError("disk full")

        monkeypatch.setattr("fluidics.sequences.yaml.safe_dump", dies_midway)
        with pytest.raises(OSError, match="disk full"):
            save_sequences_yaml(original, path)
        assert open(path).read() == before
        assert [p.name for p in tmp_path.iterdir()] == ["out.yaml"], \
            "no temp wreckage"


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


class TestCheckPortsAgainstConfig:
    """The upper bound lives in the rig config, not the sequence file, so the
    pydantic models cannot enforce it -- this check runs at both entry points
    before anything moves."""

    @pytest.fixture
    def config(self, fixtures_dir):
        from fluidics.control.config import load_config
        return load_config(str(fixtures_dir / "flow_cell_config.yaml"))  # 28 ports

    def _seq(self, **overrides):
        seq = {"type": "flow_reagent", "fluidic_port": 1, "flow_rate": 500,
               "volume": 100}
        seq.update(overrides)
        return seq

    def test_in_range_ports_pass(self, config):
        check_ports_against_config(
            [self._seq(fluidic_port=1), self._seq(fluidic_port=28)], config)

    def test_an_out_of_range_port_names_the_sequence(self, config):
        with pytest.raises(ValueError, match=r"1\.\.28") as excinfo:
            check_ports_against_config(
                [self._seq(), self._seq(fluidic_port=29, name="bad draw")],
                config)
        assert "sequence 1 (bad draw)" in str(excinfo.value)

    def test_port_zero_is_out_of_range(self, config):
        with pytest.raises(ValueError, match="fluidic_port=0"):
            check_ports_against_config([self._seq(fluidic_port=0)], config)

    def test_fill_tubing_with_is_checked_when_set(self, config):
        with pytest.raises(ValueError, match="fill_tubing_with=99"):
            check_ports_against_config(
                [self._seq(fill_tubing_with=99)], config)

    def test_falsy_fill_tubing_with_means_no_fill(self, config):
        # None from YAML, 0 from the GUI dialog's spinbox: both mean "skip".
        check_ports_against_config(
            [self._seq(fill_tubing_with=None), self._seq(fill_tubing_with=0)],
            config)

    def test_set_temperature_has_no_ports_to_check(self, config):
        check_ports_against_config(
            [{"type": "set_temperature", "temperature": 37}], config)

    def test_every_offender_is_listed_not_just_the_first(self, config):
        with pytest.raises(ValueError) as excinfo:
            check_ports_against_config(
                [self._seq(fluidic_port=30), self._seq(fill_tubing_with=31)],
                config)
        message = str(excinfo.value)
        assert "fluidic_port=30" in message
        assert "fill_tubing_with=31" in message


class TestCheckTypesAgainstApplication:
    """The types a rig offers live in the config's application; the union
    admits them all, so this gate runs at time zero."""

    def test_the_applications_own_types_pass(self, flow_cell_config):
        check_types_against_application(
            [{"type": "flow_reagent", "fluidic_port": 1, "flow_rate": 500,
              "volume": 100},
             {"type": "set_temperature", "temperature": 37}], flow_cell_config)

    def test_a_wrong_application_type_is_named_at_time_zero(self, flow_cell_config):
        with pytest.raises(ValueError) as caught:
            check_types_against_application(
                [{"type": "flow_reagent", "fluidic_port": 1, "flow_rate": 500,
                  "volume": 100},
                 {"type": "add_reagent", "fluidic_port": 2, "flow_rate": 500,
                  "volume": 100, "name": "stain"}], flow_cell_config)
        message = str(caught.value)
        assert "sequence 1 (stain)" in message
        assert "add_reagent" in message and "Flow Cell" in message

    def test_the_per_sequence_verdict_serves_the_live_paint(self, flow_cell_config):
        ok = {"type": "priming", "fluidic_port": 1, "flow_rate": 500,
              "volume": 100}
        assert sequence_type_problem(ok, flow_cell_config.application) is None
        wrong = sequence_type_problem({"type": "wash_constant_flow"},
                                      flow_cell_config.application)
        assert "not a Flow Cell sequence type" in wrong
        assert "flow_reagent" in wrong, "the message should say what is offered"


class TestRoundLabel:
    def test_round_label_is_accepted_and_survives_a_round_trip(self, tmp_path):
        rows = [
            {"type": "flow_reagent", "round": "R01", "fluidic_port": 1, "flow_rate": 500, "volume": 500},
            {"type": "priming", "round": "setup", "fluidic_port": 2, "flow_rate": 500, "volume": 500},
        ]
        validated = SequenceListAdapter.validate_python(rows)
        assert validated[0].round == "R01" and validated[1].round == "setup"
        path = str(tmp_path / "p.yaml")
        save_sequences_yaml(rows, path)
        again = load_sequences(path)
        assert again[0]["round"] == "R01" and again[1]["round"] == "setup"

    def test_round_is_optional_and_defaults_to_none(self):
        row = SequenceListAdapter.validate_python(
            [{"type": "flow_reagent", "fluidic_port": 1, "flow_rate": 500, "volume": 500}]
        )[0]
        assert row.round is None


class TestSequenceProblemWithoutALimit:
    """limit=None withholds the port verdict and nothing else."""

    def row(self, **fields):
        return dict({"type": "flow_reagent", "fluidic_port": 1,
                     "flow_rate": 500, "volume": 500}, **fields)

    def test_a_port_beyond_an_unknown_range_is_not_judged(self):
        beyond = self.row(fluidic_port=999)
        assert sequence_problem(beyond, "Flow Cell", None) is None
        assert sequence_problem(beyond, "Flow Cell", 24) is not None

    def test_the_models_own_floor_still_applies(self):
        """Only the upper bound is the rig's to know; a port below 1 is
        wrong on any rig."""
        assert "greater than or equal to 1" in sequence_problem(
            self.row(fluidic_port=0), "Flow Cell", None)

    def test_the_type_and_schema_stages_still_run(self):
        assert sequence_problem({"type": "no_such"}, "Flow Cell", None) is not None
        assert "flow_rate" in sequence_problem(
            self.row(flow_rate="x"), "Flow Cell", None)
