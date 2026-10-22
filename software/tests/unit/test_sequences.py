# tests/unit/test_sequences.py
import pytest
import yaml
from pydantic import ValidationError

from fluidics.sequences import (
    APPLICATION_SEQUENCES,
    SEQUENCE_TYPES,
    SEQUENCE_TYPE_LABELS,
    SIPPER_TYPES,
    SequenceListAdapter,
    check_ports_against_config,
    check_types_against_application,
    sequence_type_problem,
    types_for_rig,
    load_sequences,
    save_sequences_yaml,
    get_included_sequences,
    get_fields_for_type,
)


class TestSequenceModels:
    def test_a_push_names_a_well_and_a_flow_rate_and_nothing_else(self):
        """The volume is the config's and the port the sipper's: neither is
        the row's to give; dips are optional."""
        [seq] = SequenceListAdapter.validate_python([
            {"type": "push", "well": "ssct/A1", "flow_rate": 2000}])
        assert (seq.well, seq.flow_rate, seq.dip_cycles) == ("ssct/A1", 2000, 0)
        for extra in ({"volume": 490}, {"fluidic_port": 1}):
            with pytest.raises(ValidationError):
                SequenceListAdapter.validate_python([
                    {"type": "push", "well": "ssct/A1", "flow_rate": 2000, **extra}])
        with pytest.raises(ValidationError, match="well"):
            SequenceListAdapter.validate_python([{"type": "push", "flow_rate": 2000}])

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

    def test_the_sipper_only_types_are_derived_from_the_models(self):
        """A type whose rows name a well and carry no port draws through
        the sipper and nowhere else; nothing declares it."""
        assert SIPPER_TYPES == {"push"}
        assert SIPPER_TYPES <= set(APPLICATION_SEQUENCES["Flow Cell"])
        from fluidics.sequences import _CSV_NAME_TO_TYPE
        assert "Push" not in _CSV_NAME_TO_TYPE, "a CSV's columns cannot express a push"

    def test_a_rig_without_a_sipper_is_offered_no_sipper_only_type(self, plate_sipper_config):
        from fluidics.vocabulary import sipper_vocabulary
        assert "push" not in types_for_rig("Flow Cell", None)
        assert types_for_rig("Flow Cell", sipper_vocabulary(plate_sipper_config)) \
            == APPLICATION_SEQUENCES["Flow Cell"]
        assert types_for_rig("Open Chamber", None) == APPLICATION_SEQUENCES["Open Chamber"]


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


# --- wells on a sipper rig (spec §6, Task B6) ---

from fluidics.sequences import (sequence_problem as _sequence_problem,
                                sequence_well_problems, validate_sequences as _validate)
from fluidics.vocabulary import sipper_vocabulary as _vocabulary

HYB = {"type": "flow_reagent", "fluidic_port": 1, "well": "A1", "flow_rate": 2000, "volume": 100}
PUSH = {"type": "push", "well": "ssct/A1", "flow_rate": 2000}


class TestWellFields:
    def test_the_fields_are_optional_and_default(self):
        [seq] = SequenceListAdapter.validate_python([{"type": "flow_reagent", "fluidic_port": 2,
                                                      "flow_rate": 500, "volume": 500}])
        assert seq.well is None and seq.dip_cycles == 0

    def test_a_file_without_them_is_written_without_them(self, tmp_path):
        path = tmp_path / "s.yaml"
        save_sequences_yaml([{"type": "flow_reagent", "fluidic_port": 2, "flow_rate": 500,
                              "volume": 500}], str(path))
        assert "well" not in path.read_text() and "dip_cycles" not in path.read_text()
        assert load_sequences(str(path))[0]["well"] is None

    def test_every_port_or_well_valued_field_is_registered(self):
        from fluidics.sequences import PORT_FIELDS, SEQUENCE_TYPES, WELL_FIELDS
        for model in SEQUENCE_TYPES.values():
            for name in model.model_fields:
                if name.endswith("port") or name.endswith("_with") or "well" in name:
                    assert name in PORT_FIELDS or name in WELL_FIELDS, name


class TestPairingRules:
    def test_a_well_on_a_rig_without_a_sipper_is_refused(self, flow_cell_config):
        """Review focus 1."""
        with pytest.raises(ValueError, match="this rig has no sipper"):
            _validate([HYB], flow_cell_config)

    def test_a_well_needs_the_sipper_port_and_the_sipper_port_needs_a_well(self, plate_sipper_config):
        v = _vocabulary(plate_sipper_config)
        assert sequence_well_problems(dict(HYB, fluidic_port=2), v) == ["well=A1 needs fluidic_port=1 (the sipper)"]
        assert sequence_well_problems({**HYB, "well": None}, v) == ["fluidic_port=1 is the sipper: name a well"]
        assert sequence_well_problems(HYB, v) == []

    def test_a_push_row_is_the_sippers_and_names_only_its_well(self, plate_sipper_config):
        v = _vocabulary(plate_sipper_config)
        assert sequence_well_problems(PUSH, v) == []
        assert sequence_well_problems({"type": "push", "flow_rate": 2000}, v) == [
            "a push row draws through the sipper: name a well"]
        assert sequence_well_problems(dict(PUSH, well="water/A1"), v)[0].startswith("well=water/A1:")
        _validate([PUSH], plate_sipper_config)
        # A script's raw dict still carrying the old push row's port or
        # volume is refused, not drawn past: the gate never runs the model.
        for stray in ("fluidic_port", "volume"):
            message = f"{stray} is not a field of push rows"
            assert sequence_well_problems({**PUSH, stray: 25}, v) == [message]
            with pytest.raises(ValueError, match=message):
                _validate([{**PUSH, stray: 25}], plate_sipper_config)

    def test_a_push_row_on_a_rig_without_a_sipper_is_refused_with_or_without_its_well(
            self, flow_cell_config):
        """The type gate offers push on every Flow Cell rig; the well gate
        refuses the row where the rig has no sipper, a script's raw dict
        with no well included."""
        for row in (PUSH, {"type": "push", "flow_rate": 2000}):
            with pytest.raises(ValueError, match="this rig has no sipper"):
                _validate([row], flow_cell_config)

    def test_a_bare_set_temperature_row_on_a_sipper_rig_draws_no_well_complaint(
            self, plate_sipper_config):
        """The pairing is for rows with a port and for the sipper-only
        types; a row with neither is nobody's well row."""
        row = {"type": "set_temperature", "temperature": 37.0}
        assert sequence_well_problems(row, _vocabulary(plate_sipper_config)) == []
        _validate([row], plate_sipper_config)

    def test_a_sipper_row_carries_no_fill(self, plate_sipper_config):
        v = _vocabulary(plate_sipper_config)
        assert sequence_well_problems(dict(HYB, fill_tubing_with=25), v) == [
            "a sipper row cannot fill tubing: add a flow_reagent row from the paired well"]
        assert sequence_well_problems({"type": "flow_reagent", "fluidic_port": 25, "flow_rate": 500,
                                       "volume": 500, "fill_tubing_with": 1}, v) == [
            "fill_tubing_with=1 is the sipper: the sipper cannot fill tubing"]

    def test_dips_need_a_well_and_a_whole_number_and_references_must_resolve(self, plate_sipper_config):
        v = _vocabulary(plate_sipper_config)
        assert sequence_well_problems({"type": "flow_reagent", "fluidic_port": 25, "flow_rate": 500,
                                       "volume": 500, "dip_cycles": 2}, v) == ["dip_cycles needs a well"]
        assert sequence_well_problems(dict(HYB, dip_cycles=-1), v)[0].startswith("dip_cycles=-1")
        assert sequence_well_problems(dict(HYB, dip_cycles="2"), v)[0].startswith("dip_cycles='2'")
        assert sequence_well_problems(dict(HYB, well="water/A1"), v)[0].startswith("well=water/A1:")

    def test_priming_may_name_the_sipper_well(self, plate_sipper_config):
        v = _vocabulary(plate_sipper_config)
        prime = {"type": "priming", "fluidic_port": 25, "flow_rate": 5000, "volume": 800,
                 "sipper_well": "ssct/H10"}
        assert sequence_well_problems(prime, v) == []
        assert _sequence_problem(prime, "Flow Cell", (1, 25), sipper=v) is None
        assert _sequence_problem(dict(HYB, fluidic_port=25), "Flow Cell", (1, 25), sipper=v) \
            == "well=A1 needs fluidic_port=1 (the sipper)"

    def test_the_gate_names_the_row(self, plate_sipper_config):
        with pytest.raises(ValueError, match=r"sequence 0 \(hyb\).*needs fluidic_port=1"):
            _validate([dict(HYB, name="hyb", fluidic_port=24)], plate_sipper_config)

    def test_a_file_with_no_well_fields_validates_as_before(self, flow_cell_config):
        _validate([{"type": "flow_reagent", "fluidic_port": 2, "flow_rate": 500, "volume": 500,
                    "fill_tubing_with": 25}], flow_cell_config)

    def test_a_well_that_is_not_text_is_named_not_raised(self, plate_sipper_config):
        """A raw row reaches the gate unvalidated: a number where a well
        reference belongs is a named refusal, not a TypeError."""
        with pytest.raises(ValueError, match=r"sequence 0 .*well=5: must be a well reference"):
            _validate([dict(HYB, well=5)], plate_sipper_config)
        prime = {"type": "priming", "fluidic_port": 25, "flow_rate": 5000, "volume": 800,
                 "sipper_well": 7}
        with pytest.raises(ValueError, match=r"sequence 0 .*sipper_well=7: must be a well reference"):
            _validate([prime], plate_sipper_config)

    @pytest.mark.parametrize("row, message", [
        (dict(HYB, sipper_well="ssct/H10"), "sipper_well is not a field of flow_reagent rows"),
        ({"type": "priming", "fluidic_port": 1, "well": "A1", "dip_cycles": 3,
          "flow_rate": 5000, "volume": 800}, "dip_cycles is not a field of priming rows"),
        ({"type": "set_temperature", "temperature": 37.0, "fluidic_port": 1, "well": "A1"},
         "well is not a field of set_temperature rows"),
        (dict(PUSH, sipper_well="ssct/H10"), "sipper_well is not a field of push rows"),
    ])
    def test_a_well_field_the_row_type_lacks_is_refused(self, plate_sipper_config, row, message):
        """The model forbids these; the raw-dict gate must too, or the
        field is silently ignored at run time (a priming row's dips never
        happen). Named before the pairing rules."""
        from fluidics.control.config import available_ports
        v = _vocabulary(plate_sipper_config)
        assert sequence_well_problems(row, v)[0] == message
        with pytest.raises(ValueError, match=message):
            _validate([row], plate_sipper_config)
        assert _sequence_problem(row, plate_sipper_config.application,
                                 available_ports(plate_sipper_config), sipper=v) is not None


PRIME = {"type": "priming", "fluidic_port": 25, "flow_rate": 5000, "volume": 800}
SKIPS_THE_SIPPER = "use_ports leaves out port 1 (the sipper): priming and clean-up never skip the sipper"


class TestSelectivePriming:
    """Codex finding 3: use_ports, a raw-dict option the models do not
    know, may not leave out the sipper's port, whose line would then hold
    air at the first draw (D7). Such a row is refused, not given a draw
    from a well it did not list."""

    @pytest.mark.parametrize("seq_type", ["priming", "clean_up"])
    def test_a_row_that_leaves_out_the_sipper_is_refused(self, plate_sipper_config, seq_type):
        v = _vocabulary(plate_sipper_config)
        row = dict(PRIME, type=seq_type)
        assert sequence_well_problems(dict(row, use_ports=[25]), v) == [SKIPS_THE_SIPPER]
        assert sequence_well_problems(dict(row, use_ports=[1, 25]), v) == []
        assert sequence_well_problems(dict(row, use_ports=(25, 1)), v) == []
        assert sequence_well_problems(dict(row, use_ports=None), v) == []

    def test_use_ports_must_be_a_list(self, plate_sipper_config):
        v = _vocabulary(plate_sipper_config)
        assert sequence_well_problems(dict(PRIME, use_ports=25), v) == ["use_ports=25: a list of ports"]
        assert sequence_well_problems(dict(PRIME, use_ports="1,25"), v) == [
            "use_ports='1,25': a list of ports"]

    def test_a_rig_without_a_sipper_keeps_selective_priming(self, flow_cell_config):
        assert sequence_well_problems(dict(PRIME, use_ports=[25]), None) == []
        assert sequence_well_problems(dict(PRIME, use_ports=25), None) == []
        _validate([dict(PRIME, use_ports=[25])], flow_cell_config)

    def test_the_gate_names_the_row(self, plate_sipper_config):
        with pytest.raises(ValueError, match=r"sequence 0 \(prime\): use_ports leaves out port 1 "
                                             r"\(the sipper\): priming and clean-up never skip"):
            _validate([dict(PRIME, name="prime", use_ports=[25])], plate_sipper_config)
