import pytest

from fluidics.robot.labware import WellRef
from fluidics.vocabulary import sipper_vocabulary


def test_no_vocabulary_without_a_gantry(flow_cell_config):
    assert sipper_vocabulary(flow_cell_config) is None


def test_resolve_uses_the_default_reservoir_and_checks_the_plate(plate_sipper_config):
    v = sipper_vocabulary(plate_sipper_config)
    assert v.sipper_port == 1
    assert v.resolve("A3") == WellRef("probes", "A3")
    assert v.resolve("ssct/H12") == WellRef("ssct", "H12")
    for bad in ("water/A1", "A13", "ssct/I1"):
        with pytest.raises(ValueError):
            v.resolve(bad)


def test_well_names_carry_the_labels(plate_sipper_config):
    v = sipper_vocabulary(plate_sipper_config)
    assert v.well_names("probes")[0] == ("A1", "A1: hyb R01")
    assert v.well_names("ssct")[0] == ("A1", "A1")


def test_placements_follow_the_config_including_the_plate_axes(plate_sipper_config):
    v = sipper_vocabulary(plate_sipper_config)
    assert v.placements()["ssct"].xy_mm("A2") == (102.5, 10.5)     # columns along +y
    assert v.placements()["ssct"].xy_mm("B1") == (111.5, 1.5)      # rows along +x
    v.reservoirs["ssct"].a1_xy_mm = (103.0, 2.0)
    assert v.placement("ssct").xy_mm("A1") == (103.0, 2.0), "built from the config as it is now"


def test_reservoir_definitions_cover_wells_and_tracked_bottles(plate_sipper_config):
    from fluidics.reservoirs import reservoir_definitions
    defs = reservoir_definitions(plate_sipper_config)
    assert defs["port_25"] == {"loaded_ul": 500000, "residual_ul": 20000}
    assert defs["ssct/H12"] == {"loaded_ul": 1800, "residual_ul": 50}
    assert "port_26" not in defs and len(defs) == 1 + 2 * 96
