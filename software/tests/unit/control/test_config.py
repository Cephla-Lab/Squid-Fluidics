# tests/unit/control/test_config.py
import json

import pytest
from pydantic import ValidationError

from fluidics.control.config import (
    FluidicsConfig,
    SelectorValvesConfig,
    load_config,
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
