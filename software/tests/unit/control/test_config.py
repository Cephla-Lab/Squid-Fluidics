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
