# tests/unit/control/test_config.py
import json
import re

import pytest

from fluidics.control.config import default_config_path
from pydantic import ValidationError

from fluidics.control.config import (
    FluidicsConfig,
    SelectorValvesConfig,
    load_config,
    save_config,
    assign_loaded_volumes,
    save_loaded_volumes,
    convert_legacy_config,
)


def _make_config_dict(**overrides):
    """Build a minimal FluidicsConfig dict, with overrides applied on top."""
    base = {
        "config_version": "2.0",
        "microcontroller": {"serial_number": "X"},
        "syringe_pump": {
            "serial_number": "X", "volume_ul": 1000,
            "ports_allowed": [1], "waste_port": 1,
            "extract_port": 1, "speed_code_limit": 10,
        },
        "reagent_selection": {
            "selector_valves": {
                "valve_ids": [0], "number_of_ports": {0: 10},
                "tubing_fluid_amount_to_valve_ul": {0: 0},
                "tubing_fluid_amount_ul": {"port_1": 100},
            },
            "common_tubing_fluid_amount_ul": 100,
        },
        "application": "Flow Cell",
    }
    # Apply top-level overrides
    for key, value in overrides.items():
        if "." in key:
            # Support dotted keys like "syringe_pump.volume_ul"
            section, field = key.split(".", 1)
            base[section][field] = value
        else:
            base[key] = value
    return base


class TestFluidicsConfigLoading:
    def test_load_flow_cell_config(self, fixtures_dir):
        config = load_config(str(fixtures_dir / "flow_cell_config.yaml"))
        assert config.application == "Flow Cell"
        assert config.syringe_pump.volume_ul == 5000
        assert config.config_version == "2.0"

    def test_load_open_chamber_config(self, fixtures_dir):
        config = load_config(str(fixtures_dir / "open_chamber_config.yaml"))
        assert config.application == "Open Chamber"
        assert config.samples.chamber_volume_ul == 1300
        assert config.sample_selection_inlet.common_tubing_fluid_amount_ul == 900

    def test_flow_cell_has_no_open_chamber_fields(self, fixtures_dir):
        config = load_config(str(fixtures_dir / "flow_cell_config.yaml"))
        assert config.sample_selection_inlet is None
        assert config.samples is None
        assert config.temperature_controller is None

    def test_invalid_application_rejected(self):
        with pytest.raises(ValidationError):
            FluidicsConfig(**_make_config_dict(application="Invalid"))

    def test_syringe_volume_must_be_positive(self):
        with pytest.raises(ValidationError, match="volume_ul"):
            FluidicsConfig(**_make_config_dict(**{"syringe_pump.volume_ul": 0}))

    def test_speed_code_limit_range(self):
        """speed_code_limit must be 0-40."""
        with pytest.raises(ValidationError, match="speed_code_limit"):
            FluidicsConfig(**_make_config_dict(**{"syringe_pump.speed_code_limit": 41}))


class TestSelectorValvesValidator:
    def test_mismatched_valve_ids_rejected(self):
        """number_of_ports keys must match valve_ids."""
        with pytest.raises(ValidationError, match="don't match valve_ids"):
            SelectorValvesConfig(
                valve_ids=[0, 1],
                number_of_ports={0: 10},  # missing valve 1
                tubing_fluid_amount_to_valve_ul={0: 0, 1: 100},
                tubing_fluid_amount_ul={"port_1": 100},
            )

    def test_extra_keys_in_number_of_ports_rejected(self):
        with pytest.raises(ValidationError, match="don't match valve_ids"):
            SelectorValvesConfig(
                valve_ids=[0],
                number_of_ports={0: 10, 1: 10},  # extra valve 1
                tubing_fluid_amount_to_valve_ul={0: 0},
                tubing_fluid_amount_ul={"port_1": 100},
            )

    def _valves(self, **overrides):
        kwargs = dict(valve_ids=[0], number_of_ports={0: 10},
                      tubing_fluid_amount_to_valve_ul={0: 0},
                      tubing_fluid_amount_ul={"port_1": 100})
        kwargs.update(overrides)
        return SelectorValvesConfig(**kwargs)

    @pytest.mark.parametrize("key", ["1", "port_01", "port_x", "port_99"])
    def test_a_key_that_names_no_port_is_rejected(self, key):
        """These keys decide which ports the rig offers -- a tubing volume
        is how the config says a port is plumbed, and only `port_<n>` is
        ever read back. So a typo used to remove a port: `port_2s` left
        port 2 with no volume and every consumer stopped offering it, with
        nothing pointing at the config line."""
        with pytest.raises(ValidationError, match="name no port"):
            self._valves(tubing_fluid_amount_ul={"port_1": 100, key: 100})

    def test_a_name_mapping_key_that_names_no_port_is_rejected(self):
        """The rename dialog writes this file; a stale key from a rig with
        more valves would sit there unread."""
        with pytest.raises(ValidationError, match="name no port"):
            self._valves(name_mapping={"port_50": "DAPI"})

    def test_the_reach_spans_the_whole_cascade(self):
        """Ports are numbered across the cascade, not per valve: two
        10-port valves reach port 19, so port_19 is a real key."""
        sv = self._valves(valve_ids=[0, 1], number_of_ports={0: 10, 1: 10},
                          tubing_fluid_amount_to_valve_ul={0: 0, 1: 200},
                          tubing_fluid_amount_ul={"port_19": 100})
        assert sv.tubing_fluid_amount_ul == {"port_19": 100}

    def test_valid_multi_valve_config(self):
        sv = SelectorValvesConfig(
            valve_ids=[0, 1],
            number_of_ports={0: 10, 1: 10},
            tubing_fluid_amount_to_valve_ul={0: 0, 1: 200},
            tubing_fluid_amount_ul={"port_1": 100},
        )
        assert sv.valve_ids == [0, 1]


class TestConvertLegacyConfig:
    def test_flow_cell_conversion(self, fixtures_dir):
        """Legacy MERFISH JSON converts to valid Flow Cell YAML config."""
        with open(fixtures_dir / "legacy_flow_cell_config.json") as f:
            old = json.load(f)

        new = convert_legacy_config(old)
        config = FluidicsConfig(**new)

        assert config.application == "Flow Cell"
        assert config.config_version == "2.0"
        assert config.reagent_selection.common_tubing_fluid_amount_ul == 800
        # Verify tubing decomposition: common(800) + per_valve == original total
        sv = config.reagent_selection.selector_valves
        assert sv.tubing_fluid_amount_to_valve_ul[0] == 0    # 800 - 800
        assert sv.tubing_fluid_amount_to_valve_ul[1] == 200   # 1000 - 800
        assert sv.tubing_fluid_amount_to_valve_ul[2] == 340   # 1140 - 800

    def test_open_chamber_conversion(self, fixtures_dir):
        """Legacy Open Chamber JSON converts to valid config."""
        with open(fixtures_dir / "legacy_open_chamber_config.json") as f:
            old = json.load(f)

        new = convert_legacy_config(old)
        config = FluidicsConfig(**new)

        assert config.application == "Open Chamber"
        assert config.sample_selection_inlet.common_tubing_fluid_amount_ul == 900
        assert config.samples.chamber_volume_ul == 1300
        # temperature_controller with use_temperature_controller=False should NOT appear
        assert config.temperature_controller is None

    def test_merfish_becomes_flow_cell(self, fixtures_dir):
        with open(fixtures_dir / "legacy_flow_cell_config.json") as f:
            old = json.load(f)
        new = convert_legacy_config(old)
        assert new['application'] == 'Flow Cell'

    def test_empty_reagent_names_filtered(self):
        old = {
            "application": "MERFISH",
            "microcontroller": {"serial_number": "X"},
            "syringe_pump": {
                "serial_number": "X", "volume_ul": 1000,
                "ports_allowed": [1], "waste_port": 1,
                "extract_port": 1, "speed_code_limit": 10,
            },
            "selector_valves": {
                "valve_ids_allowed": [0],
                "number_of_ports": {"0": 10},
                "tubing_fluid_amount_to_valve_ul": {"0": 100},
                "reagent_name_mapping": {"port_1": "buffer", "port_2": "", "port_3": None},
                "tubing_fluid_amount_to_port_ul": {"port_1": 100},
            },
        }
        new = convert_legacy_config(old)
        name_mapping = new['reagent_selection']['selector_valves'].get('name_mapping', {})
        assert "port_1" in name_mapping
        assert "port_2" not in name_mapping
        assert "port_3" not in name_mapping


class TestTemperatureControllerConfig:
    def _config_with_tc(self, **tc_overrides):
        tc = {"serial_number": "TC-X"}
        tc.update(tc_overrides)
        return _make_config_dict(temperature_controller=tc)

    def test_defaults_populated(self):
        cfg = FluidicsConfig(**self._config_with_tc())
        assert cfg.temperature_controller.channels == 2
        assert cfg.temperature_controller.tolerance_celsius == 1.0
        assert cfg.temperature_controller.stabilization_timeout_seconds == 300

    def test_explicit_values_override_defaults(self):
        cfg = FluidicsConfig(**self._config_with_tc(
            channels=1, tolerance_celsius=0.5, stabilization_timeout_seconds=120,
        ))
        tc = cfg.temperature_controller
        assert tc.channels == 1
        assert tc.tolerance_celsius == 0.5
        assert tc.stabilization_timeout_seconds == 120

    def test_channels_must_be_1_or_2(self):
        with pytest.raises(ValidationError):
            FluidicsConfig(**self._config_with_tc(channels=3))

    def test_tolerance_must_be_positive(self):
        with pytest.raises(ValidationError):
            FluidicsConfig(**self._config_with_tc(tolerance_celsius=0))

    def test_timeout_must_be_positive(self):
        with pytest.raises(ValidationError):
            FluidicsConfig(**self._config_with_tc(stabilization_timeout_seconds=0))


class TestFlowSensorConfig:
    def test_absent_section_is_none(self):
        config = FluidicsConfig(**_make_config_dict())
        assert config.flow_sensors is None

    def test_minimal_sensor_takes_defaults(self):
        config = FluidicsConfig(**_make_config_dict(
            flow_sensors=[{"index": 1, "name": "syringe_draw"}]
        ))
        sensor = config.flow_sensors[0]
        assert sensor.index == 1
        assert sensor.name == "syringe_draw"
        assert sensor.monitor == "off"
        assert sensor.ramp_up_seconds == 3.0
        assert sensor.tolerance_fraction == 0.3
        assert sensor.max_flow_rate_ul_min == 2000

    def test_explicit_values_override_defaults(self):
        config = FluidicsConfig(**_make_config_dict(
            flow_sensors=[{
                "index": 2, "name": "waste_line", "monitor": "off",
                "ramp_up_seconds": 1.5, "tolerance_fraction": 0.1,
                "max_flow_rate_ul_min": 1500,
            }]
        ))
        sensor = config.flow_sensors[0]
        assert sensor.monitor == "off"
        assert sensor.ramp_up_seconds == 1.5
        assert sensor.tolerance_fraction == 0.1
        assert sensor.max_flow_rate_ul_min == 1500

    @pytest.mark.parametrize("bad_index", [0, 3, -1])
    def test_index_must_be_1_or_2(self, bad_index):
        with pytest.raises(ValidationError):
            FluidicsConfig(**_make_config_dict(
                flow_sensors=[{"index": bad_index, "name": "s"}]
            ))

    def test_unknown_monitor_mode_rejected(self):
        with pytest.raises(ValidationError):
            FluidicsConfig(**_make_config_dict(
                flow_sensors=[{"index": 1, "name": "s", "monitor": "halt"}]
            ))

    def test_monitor_off_accepted(self):
        config = FluidicsConfig(**_make_config_dict(
            flow_sensors=[{"index": 1, "name": "s", "monitor": "off"}]
        ))
        assert config.flow_sensors[0].monitor == "off"

    @pytest.mark.parametrize("mode", ["warn", "stop"])
    def test_active_monitor_modes_accepted(self, mode):
        config = FluidicsConfig(**_make_config_dict(
            flow_sensors=[{"index": 1, "name": "s", "monitor": mode}]
        ))
        assert config.flow_sensors[0].monitor == mode

    @pytest.mark.parametrize("field,bad_value", [
        ("ramp_up_seconds", 0),
        ("ramp_up_seconds", -1),
        ("tolerance_fraction", 0),
        ("tolerance_fraction", 1.5),
        ("max_flow_rate_ul_min", 0),
    ])
    def test_out_of_range_values_rejected(self, field, bad_value):
        with pytest.raises(ValidationError):
            FluidicsConfig(**_make_config_dict(
                flow_sensors=[{"index": 1, "name": "s", field: bad_value}]
            ))

    def test_duplicate_index_rejected(self):
        with pytest.raises(ValidationError, match="index"):
            FluidicsConfig(**_make_config_dict(flow_sensors=[
                {"index": 1, "name": "a"},
                {"index": 1, "name": "b"},
            ]))

    def test_duplicate_name_rejected(self):
        with pytest.raises(ValidationError, match="name"):
            FluidicsConfig(**_make_config_dict(flow_sensors=[
                {"index": 1, "name": "same"},
                {"index": 2, "name": "same"},
            ]))

    def test_two_sensors_accepted(self):
        config = FluidicsConfig(**_make_config_dict(flow_sensors=[
            {"index": 1, "name": "syringe_draw"},
            {"index": 2, "name": "waste_line"},
        ]))
        assert [s.index for s in config.flow_sensors] == [1, 2]

    def test_a_lone_sensor_on_index_2_is_valid(self):
        """One sensor in board slot 2, nothing in slot 1. It reads packet
        bytes 25-26 while 23-24 carry the no-sensor sentinel -- a normal
        wiring, not an error.
        """
        config = FluidicsConfig(**_make_config_dict(
            flow_sensors=[{"index": 2, "name": "waste_line"}]
        ))
        assert config.flow_sensors[0].index == 2

    def test_empty_list_rejected(self):
        with pytest.raises(ValidationError):
            FluidicsConfig(**_make_config_dict(flow_sensors=[]))

    def test_fixture_config_has_flow_sensor(self, fixtures_dir):
        config = load_config(str(fixtures_dir / "flow_cell_config.yaml"))
        assert config.flow_sensors is not None
        assert config.flow_sensors[0].index == 1


class TestUnknownKeysAreRejected:
    """The config models forbid extras now, like the sequence models always
    have. Before this, a misspelled or unsupported key was silently dropped:
    a rig config carried `microstep: true` under syringe_pump for months --
    a field nothing reads -- and looked configured the whole time."""

    def test_a_top_level_unknown_key_names_itself(self):
        with pytest.raises(ValidationError, match="chamber_volume"):
            FluidicsConfig(**_make_config_dict(chamber_volume=1300))

    def test_a_nested_unknown_key_names_itself(self):
        with pytest.raises(ValidationError, match="microstep"):
            FluidicsConfig(**_make_config_dict(**{"syringe_pump.microstep": True}))

    def test_a_typoed_safety_knob_fails_instead_of_defaulting(self):
        config = _make_config_dict(
            temperature_controller={"serial_number": "T",
                                    "tolerance_celcius": 0.5})  # sic
        with pytest.raises(ValidationError, match="tolerance_celcius"):
            FluidicsConfig(**config)


class TestDefaultConfigPath:
    """The rig's conventional local config, shared by the GUI and the CLI:
    config.yaml, then the legacy config.json."""

    def test_yaml_wins_over_json(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config.yaml").write_text("")
        (tmp_path / "config.json").write_text("")
        assert default_config_path() == "./config.yaml"

    def test_the_legacy_json_serves_alone(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config.json").write_text("")
        assert default_config_path() == "./config.json"

    def test_none_when_the_directory_has_neither(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert default_config_path() is None


class TestSaveConfig:
    """save_config writes renames back to the rig's own file, preserving
    what the operator wrote there by hand."""

    @pytest.fixture
    def rig_yaml(self, tmp_path, fixtures_dir):
        """The flow-cell fixture with a hand-written comment planted in it,
        the way per-rig configs carry plumbing notes."""
        text = (fixtures_dir / "flow_cell_config.yaml").read_text()
        assert "ramp_up_seconds" not in text, "fixture grew the default; re-plant"
        text = text.replace("  dispense_port: null",
                            "  dispense_port: null   # not plumbed on this rig")
        path = tmp_path / "config.yaml"
        path.write_text("# reagent shelf A, re-plumbed 2026-08\n" + text)
        return str(path)

    def test_a_rename_lands_and_the_comments_survive(self, rig_yaml):
        config = load_config(rig_yaml)
        sv = config.reagent_selection.selector_valves
        sv.name_mapping = {"port_1": "DAPI"}
        written = save_config(config, rig_yaml)
        assert written == rig_yaml
        text = open(rig_yaml).read()
        assert "# reagent shelf A, re-plumbed 2026-08" in text, \
            "the operator's own notes were lost on save"
        reloaded = load_config(rig_yaml)
        assert reloaded.reagent_selection.selector_valves.name_mapping == \
            {"port_1": "DAPI"}

    def test_everything_else_survives_the_round_trip(self, rig_yaml):
        before = load_config(rig_yaml)
        before.reagent_selection.selector_valves.name_mapping = {"port_2": "wash"}
        save_config(before, rig_yaml)
        after = load_config(rig_yaml)
        assert after == before, "a rename changed something other than the names"

    def test_clearing_every_name_clears_the_mapping(self, rig_yaml):
        config = load_config(rig_yaml)
        config.reagent_selection.selector_valves.name_mapping = None
        save_config(config, rig_yaml)
        assert load_config(rig_yaml).reagent_selection.selector_valves.name_mapping is None

    def test_a_rename_writes_only_what_the_file_or_the_operator_set(self, rig_yaml):
        """The dump is exclude_unset: unset defaults must not creep into the
        file, and an explicit null line keeps its comment -- a full dump
        added the unset flow-sensor defaults and deleted the null line."""
        config = load_config(rig_yaml)
        config.reagent_selection.selector_valves.name_mapping = {"port_1": "DAPI"}
        save_config(config, rig_yaml)
        after = open(rig_yaml).read()
        assert "ramp_up_seconds" not in after, "an unset default crept into the file"
        assert re.search(r"dispense_port: null\s+# not plumbed on this rig", after), \
            "the explicit null line (or its comment) was dropped"

    def test_a_first_rename_adds_the_mapping_where_none_existed(self, rig_yaml):
        """Assignment marks the field as set: a rig whose config never named
        a port can still start."""
        lines = open(rig_yaml).read().splitlines()
        start = next(i for i, line in enumerate(lines) if "name_mapping:" in line)
        end = start + 1
        while end < len(lines) and lines[end].startswith("      port_"):
            end += 1     # only the mapping's own block, nothing that follows
        del lines[start:end]
        open(rig_yaml, "w").write("\n".join(lines) + "\n")
        config = load_config(rig_yaml)
        assert config.reagent_selection.selector_valves.name_mapping is None
        config.reagent_selection.selector_valves.name_mapping = {"port_2": "wash"}
        save_config(config, rig_yaml)
        assert load_config(rig_yaml).reagent_selection.selector_valves \
            .name_mapping == {"port_2": "wash"}

    def test_a_config_built_in_memory_refuses_to_save(self):
        with pytest.raises(ValueError, match="not loaded from a file"):
            save_config(FluidicsConfig(**_make_config_dict()))

    def test_a_save_that_dies_midway_leaves_the_rigs_file_whole(
            self, rig_yaml, tmp_path, monkeypatch):
        """The per-rig config is hand-maintained; a dump that fails --
        full disk, a crash -- must not truncate it or strand a temp."""
        import ruamel.yaml
        config = load_config(rig_yaml)
        config.reagent_selection.selector_valves.name_mapping = {"port_1": "DAPI"}
        before = open(rig_yaml).read()

        def dies_midway(self, document, stream):
            stream.write("application: half a")
            raise OSError("disk full")

        monkeypatch.setattr(ruamel.yaml.YAML, "dump", dies_midway)
        with pytest.raises(OSError, match="disk full"):
            save_config(config, rig_yaml)
        assert open(rig_yaml).read() == before
        assert [p.name for p in tmp_path.iterdir()] == ["config.yaml"], \
            "no wreckage beside the rig's file"

    def test_a_json_path_writes_the_sibling_yaml_and_leaves_the_json(
            self, tmp_path, fixtures_dir):
        """Loading a legacy JSON already wrote and used the sibling YAML;
        saving edits that file. The JSON stays as the operator's fallback."""
        import shutil
        json_path = str(tmp_path / "config.json")
        shutil.copy(fixtures_dir / "legacy_flow_cell_config.json", json_path)
        json_before = open(json_path).read()
        config = load_config(json_path)     # converts and writes config.yaml
        config.reagent_selection.selector_valves.name_mapping = {"port_3": "TCEP"}
        written = save_config(config)       # to source_path: the sibling YAML
        assert written == str(tmp_path / "config.yaml")
        assert open(json_path).read() == json_before
        assert load_config(written).reagent_selection.selector_valves \
            .name_mapping == {"port_3": "TCEP"}


# --- the plate sipper's gantry block (spec §3, Task B5) ---

import copy

import yaml as _yaml

from fluidics.control.config import gantry as _gantry
from tests.conftest import FIXTURES_DIR

# The shipped sample is the suite's sipper config (tests/conftest.py).
_SIPPER_SAMPLE = FIXTURES_DIR / "plate_sipper_config.yaml"

# Parsed once: the rule table edits a fresh deep copy per case, which is all
# its in-place edits need, instead of re-parsing the sample 25 times.
_SIPPER_YAML = _yaml.safe_load(_SIPPER_SAMPLE.read_text())


def _sipper_dict():
    return copy.deepcopy(_SIPPER_YAML)


def _gantry_block(d):
    """The sipper's block in the raw dict -- what gantry() is on the model."""
    return d["reagent_selection"]["nimotion_gantry_system"]


def _valves_block(d):
    return d["reagent_selection"]["selector_valves"]


class TestGantryConfig:
    def test_the_sample_loads_and_names_the_sipper_port(self, plate_sipper_config):
        g = _gantry(plate_sipper_config)
        assert g.selector_valve_port == 1
        assert g.push_volume_ul == 490
        assert sorted(r.name for r in g.reservoirs.values()) == ["probes", "ssct"]
        assert g.reservoirs[1].column_axis == "+y" and g.reservoirs[1].row_axis == "+x"

    @pytest.mark.parametrize("edit, message", [
        (lambda d: d.__setitem__("application", "Open Chamber"), "Flow Cell"),
        (lambda d: _gantry_block(d).__setitem__("selector_valve_port", 2), "plumbed"),
        (lambda d: _gantry_block(d).__setitem__("push_volume_ul", 0), "push_volume_ul"),
        (lambda d: _gantry_block(d).pop("push_volume_ul"), r"push_volume_ul\n\s+Field required"),
        (lambda d: _gantry_block(d)["reservoirs"][2].__setitem__("name", "probes"), "unique"),
        (lambda d: _gantry_block(d)["reservoirs"][2].__setitem__("name", "all"), "reserved"),
        (lambda d: _gantry_block(d)["reservoirs"][2].__setitem__("name", "port_25"), "reserved"),
        (lambda d: _gantry_block(d)["reservoirs"][2].__setitem__("row_axis", "-y"), "perpendicular"),
        (lambda d: _gantry_block(d).__setitem__("priming_well", "water/A1"), "water"),
        (lambda d: _gantry_block(d)["reservoirs"][1].__setitem__("loaded_ul", 0),
         r"reservoirs\.1\.loaded_ul\n\s+Input should be greater than 0"),
        (lambda d: _gantry_block(d)["reservoirs"][1].__setitem__("loaded_ul_by_well", {"B2": 0}),
         r"probes\.loaded_ul_by_well\.B2 \(0\) must be positive"),
        (lambda d: _gantry_block(d)["reservoirs"][1].__setitem__("loaded_ul_by_well", {"B2": -5}),
         r"probes\.loaded_ul_by_well\.B2 \(-5\) must be positive"),
        (lambda d: _gantry_block(d).__setitem__("agitate_amplitude_mm", 70.0), "stroke|well"),
        (lambda d: _gantry_block(d).__setitem__("homing_velocity_mm_s", 0),
         r"homing_velocity_mm_s\n\s+Input should be greater than 0"),
        (lambda d: _gantry_block(d).__setitem__("homing_acceleration_mm_s2", -200),
         r"homing_acceleration_mm_s2\n\s+Input should be greater than 0"),
        (lambda d: _gantry_block(d)["reservoirs"][1]["well_names"].__setitem__("Z9", "x"), "Z9"),
        (lambda d: _gantry_block(d).__setitem__("port", "COM5"), "exactly one"),
        (lambda d: _gantry_block(d)["axes"].__setitem__("y", "X1"), "distinct"),
        # Homing parks Z 0.1 to 0.5 mm past 0, and home() refuses a travel
        # height Z parks below: 1 mm is the floor (B7 review).
        (lambda d: _gantry_block(d).__setitem__("travel_z_mm", 0.0),
         r"travel_z_mm\n\s+Input should be greater than or equal to 1\b"),
        (lambda d: _valves_block(d)["loaded_volume_ul"].__setitem__("port_99", 1),
         "loaded_volume_ul has keys that name no port on this cascade: port_99"),
        (lambda d: _valves_block(d)["loaded_volume_ul"].__setitem__("port_2", 1000), "plumbed"),
        (lambda d: _valves_block(d)["loaded_volume_ul"].__setitem__("port_1", 1000), "sipper"),
        (lambda d: _valves_block(d)["loaded_volume_ul"].__setitem__("port_25", 0),
         r"loaded_volume_ul\.port_25 \(0\) must be positive"),
        (lambda d: _valves_block(d)["loaded_volume_ul"].__setitem__("port_25", -500),
         r"loaded_volume_ul\.port_25 \(-500\) must be positive"),
        # The plate axes have no default (B1 review ruling): the only deck is
        # columns +y / rows +x, so a block that leaves them out is refused
        # rather than placed on the opposite guess.
        (lambda d: _gantry_block(d)["reservoirs"][1].pop("column_axis"),
         r"reservoirs\.1\.column_axis\n\s+Field required"),
        (lambda d: _gantry_block(d)["reservoirs"][2].pop("row_axis"),
         r"reservoirs\.2\.row_axis\n\s+Field required"),
        (lambda d: [_gantry_block(d)["reservoirs"][1].pop(k) for k in ("column_axis", "row_axis")],
         r"(?s)reservoirs\.1\.column_axis\n\s+Field required.*reservoirs\.1\.row_axis\n\s+Field required"),
    ])
    def test_each_rule_refuses_at_load(self, edit, message):
        data = _sipper_dict()
        edit(data)
        with pytest.raises(ValueError, match=message):
            FluidicsConfig(**data)

    @pytest.mark.parametrize("plant, key", [
        (lambda d: _gantry_block(d)["reservoirs"][1].__setitem__("residual_ul", 5),
         "reagent_selection.nimotion_gantry_system.reservoirs.1.residual_ul"),
        (lambda d: _valves_block(d).__setitem__("residual_volume_ul", {"port_25": 20000}),
         "reagent_selection.selector_valves.residual_volume_ul"),
        (lambda d: _gantry_block(d)["reservoirs"][1].__setitem__("well_spacing", 9),
         "reagent_selection.nimotion_gantry_system.reservoirs.1.well_spacing"),
    ], ids=["plate-residual", "bottle-residual", "well-spacing"])
    def test_a_config_still_carrying_a_removed_key_is_refused_naming_it(self, tmp_path, plant, key):
        """A file written before these keys went fails to load, and the
        message names the key to delete."""
        data = _sipper_dict()
        plant(data)
        path = tmp_path / "config.yaml"
        path.write_text(_yaml.safe_dump(data), encoding="utf-8")
        with pytest.raises(ValueError, match=re.escape(key) + r"\n\s+Extra inputs are not permitted"):
            load_config(str(path))

    def test_the_homing_speeds_are_optional_and_the_sample_sets_both(self, plate_sipper_config):
        """Left out, each is None: the driver's own per-axis value."""
        g = _gantry(plate_sipper_config)
        assert (g.homing_velocity_mm_s, g.homing_acceleration_mm_s2) == (20, 200)
        data = _sipper_dict()
        for key in ("homing_velocity_mm_s", "homing_acceleration_mm_s2"):
            _gantry_block(data).pop(key)
        g = _gantry(FluidicsConfig(**data))
        assert (g.homing_velocity_mm_s, g.homing_acceleration_mm_s2) == (None, None)

    def test_the_block_is_optional_and_existing_configs_are_untouched(self, flow_cell_config):
        assert _gantry(flow_cell_config) is None
        assert flow_cell_config.reagent_selection.selector_valves.loaded_volume_ul is None

    _CORNER = "        a1_xy_mm: [12.0, 1.5]\n"
    _COMMENTED = "        a1_xy_mm: [12.0, 1.5]   # taught on the Robot tab\n"

    def _commented_copy(self, tmp_path):
        text = _SIPPER_SAMPLE.read_text()
        assert text.count(self._CORNER) == 1, "sample changed; re-plant the comment"
        path = tmp_path / "config.yaml"
        path.write_text(text.replace(self._CORNER, self._COMMENTED))
        return path

    def test_a_save_keeps_a_non_ascii_comment_byte_for_byte(self, tmp_path):
        """The file is UTF-8 both ways: read under the locale's code page
        and written as UTF-8, every save double-encoded a character such as
        the button's ellipsis in a comment."""
        path = tmp_path / "config.yaml"
        text = _SIPPER_SAMPLE.read_bytes().decode("utf-8").replace("\r\n", "\n")
        assert text.count(self._CORNER) == 1, "sample changed; re-plant the comment"
        text = text.replace(self._CORNER, "        a1_xy_mm: [12.0, 1.5]   # taught… 4 µL\n")
        path.write_bytes(text.encode("utf-8"))
        config = load_config(str(path))
        sv = config.reagent_selection.selector_valves
        sv.name_mapping = {**sv.name_mapping, "port_24": "wash"}
        save_config(config)
        assert "# taught… 4 µL" in path.read_bytes().decode("utf-8")

    def test_a_save_leaves_an_untouched_corner_and_its_comment_alone(self, tmp_path):
        """model_dump gives a1_xy_mm as a tuple and the file holds a list: a
        save that compared the two rewrote every plate's corner in block
        style and moved the comment after it onto the key line."""
        path = self._commented_copy(tmp_path)
        config = load_config(str(path))
        sv = config.reagent_selection.selector_valves
        sv.name_mapping = {**sv.name_mapping, "port_24": "wash"}
        save_config(config)
        saved = path.read_text()
        assert self._COMMENTED in saved
        assert "        a1_xy_mm: [102.5, 1.5]\n" in saved

    def test_a_taught_corner_is_written_in_place_and_keeps_its_comment(self, tmp_path):
        path = self._commented_copy(tmp_path)
        config = load_config(str(path))
        _gantry(config).reservoirs[1].a1_xy_mm = (13.0, 2.0)
        save_config(config)
        assert "        a1_xy_mm: [13.0, 2.0]   # taught on the Robot tab\n" in path.read_text()

    def test_teaching_values_round_trip_through_save_config(self, tmp_path):
        import shutil
        path = tmp_path / "config.yaml"
        shutil.copy(_SIPPER_SAMPLE, path)
        config = load_config(str(path))
        _gantry(config).reservoirs[1].a1_xy_mm = (13.0, 2.0)
        _gantry(config).reservoirs[1].dip_z_mm = 51.0
        _gantry(config).travel_z_mm = 1.0
        save_config(config)
        again = load_config(str(path))
        assert tuple(_gantry(again).reservoirs[1].a1_xy_mm) == (13.0, 2.0)
        assert _gantry(again).reservoirs[1].dip_z_mm == 51.0
        assert _gantry(again).travel_z_mm == 1.0


class TestSaveLoadedVolumes:
    """save_loaded_volumes writes the loaded volumes the Reservoirs window
    sets to the config file, and nothing else in it; assign_loaded_volumes
    puts them in the live config, which the verb does last."""

    @pytest.fixture
    def config(self, plate_sipper_config_on_file):
        return plate_sipper_config_on_file

    @staticmethod
    def _reloaded(config):
        return load_config(config.source_path)

    @staticmethod
    def _probes(config):
        return _gantry(config).reservoirs[1]

    @staticmethod
    def _ssct(config):
        return _gantry(config).reservoirs[2]

    def test_a_plates_volume_is_its_loaded_ul_in_the_file_then_in_the_live_config(self, config):
        save_loaded_volumes(config, {"probes": 350})
        assert self._probes(self._reloaded(config)).loaded_ul == 350
        assert self._probes(config).loaded_ul == 110, "the file alone: the live config waits"
        assign_loaded_volumes(config, {"probes": 350})
        assert self._probes(config).loaded_ul == 350
        assert self._ssct(self._reloaded(config)).loaded_ul == 1800

    def test_a_wells_volume_is_an_override_dropped_where_it_is_the_plates_own(self, config):
        save_loaded_volumes(config, {"ssct/A1": 1000, "ssct/B1": 900})
        assert self._ssct(self._reloaded(config)).loaded_ul_by_well == {"A1": 1000, "B1": 900}
        save_loaded_volumes(config, {"ssct/A1": 1800})            # the plate's own volume
        assert self._ssct(self._reloaded(config)).loaded_ul_by_well == {"B1": 900}
        save_loaded_volumes(config, {"ssct/B1": 1800})
        assert self._ssct(self._reloaded(config)).loaded_ul_by_well is None
        assert self._ssct(config).loaded_ul_by_well is None
        assert "loaded_ul_by_well" not in open(config.source_path, encoding="utf-8").read(), \
            "an emptied override map left a null or an empty mapping behind"

    def test_a_plates_volume_drops_its_overrides(self, config):
        save_loaded_volumes(config, {"ssct/A1": 1000})
        save_loaded_volumes(config, {"ssct": 1500})
        reloaded = self._ssct(self._reloaded(config))
        assert (reloaded.loaded_ul, reloaded.loaded_ul_by_well) == (1500, None)
        assert "loaded_ul_by_well" not in open(config.source_path, encoding="utf-8").read()

    def test_a_bottles_volume_is_its_loaded_volume_ul_entry(self, config):
        save_loaded_volumes(config, {"port_26": 50000, "port_25": 400000})
        expected = {"port_25": 400000, "port_26": 50000}
        assert self._reloaded(config).reagent_selection.selector_valves.loaded_volume_ul == expected
        assign_loaded_volumes(config, {"port_26": 50000, "port_25": 400000})
        assert config.reagent_selection.selector_valves.loaded_volume_ul == expected

    def test_a_first_bottle_volume_adds_the_mapping_where_none_existed(self, fixtures_dir, tmp_path):
        import shutil
        path = tmp_path / "config.yaml"
        shutil.copy(fixtures_dir / "flow_cell_config.yaml", path)
        config = load_config(str(path))
        save_loaded_volumes(config, {"port_2": 10000})
        assert load_config(str(path)).reagent_selection.selector_valves.loaded_volume_ul == \
            {"port_2": 10000}

    def test_the_comments_survive(self, config):
        path = config.source_path
        text = open(path, encoding="utf-8").read()
        assert text.count(TestGantryConfig._CORNER) == 1, "sample changed; re-plant the comment"
        with open(path, "w", encoding="utf-8") as f:
            f.write(text.replace(TestGantryConfig._CORNER, TestGantryConfig._COMMENTED))
        config = load_config(path)
        save_loaded_volumes(config, {"probes": 350, "port_26": 50000})
        saved = open(path, encoding="utf-8").read()
        assert TestGantryConfig._COMMENTED in saved
        assert "# Port 25's bottle is volume tracked, like every well:" in saved
        assert "port_1: 250           # sipper tip to the valve" in saved

    def test_pending_teaching_on_the_live_config_is_not_written(self, config):
        """Teaching assigns to the live config and is written only by the
        Teach window's Save: a volume set saves the volumes alone, and the
        live config keeps both."""
        self._probes(config).a1_xy_mm = (13.0, 2.0)
        _gantry(config).travel_z_mm = 3.0
        save_loaded_volumes(config, {"probes": 350})
        assign_loaded_volumes(config, {"probes": 350})
        reloaded = self._reloaded(config)
        assert tuple(self._probes(reloaded).a1_xy_mm) == (12.0, 1.5)
        assert _gantry(reloaded).travel_z_mm == 1.0
        assert self._probes(reloaded).loaded_ul == 350
        assert tuple(self._probes(config).a1_xy_mm) == (13.0, 2.0) and _gantry(config).travel_z_mm == 3.0
        assert self._probes(config).loaded_ul == 350

    def test_a_config_built_in_memory_has_no_file_to_write(self):
        config = FluidicsConfig(**_sipper_dict())
        assert config.source_path is None
        save_loaded_volumes(config, {"probes/B2": 50})            # placed, and nothing written
        assert _gantry(config).reservoirs[1].loaded_ul_by_well is None
        assign_loaded_volumes(config, {"probes/B2": 50})
        assert _gantry(config).reservoirs[1].loaded_ul_by_well == {"B2": 50}

    @pytest.mark.parametrize("changes, message", [
        ({"water": 5}, "water names no plate, well or plumbed bottle"),
        ({"probes/Z9": 5}, "probes/Z9 names no plate, well or plumbed bottle"),
        ({"port_1": 5000}, "port_1 names no plate, well or plumbed bottle"),      # the sipper's
        ({"port_2": 5000}, "port_2 names no plate, well or plumbed bottle"),      # not plumbed
        ({"probes": 0}, "probes: 0 uL is not a loaded volume"),
        ({"probes": 1.5}, "probes: 1.5 uL is not a loaded volume"),
        ({"probes": 350, "water": 5}, "water names no plate"),
    ], ids=["no-such-name", "no-such-well", "sippers-port", "unplumbed", "zero", "fraction",
            "one-bad-key-of-two"])
    def test_a_change_it_cannot_place_changes_nothing(self, config, changes, message):
        before = open(config.source_path, encoding="utf-8").read()
        with pytest.raises(ValueError, match=re.escape(message)):
            save_loaded_volumes(config, changes)
        assert open(config.source_path, encoding="utf-8").read() == before
        assert self._probes(config).loaded_ul == 110
