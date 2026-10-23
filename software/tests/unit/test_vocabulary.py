import pytest

from fluidics.control.config import gantry
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
    gantry(plate_sipper_config).reservoirs[1].well_names = {"A1": "hyb R01"}    # the sample names none
    v = sipper_vocabulary(plate_sipper_config)
    assert v.well_names("probes")[0] == ("A1", "A1: hyb R01")
    assert v.well_names("ssct")[0] == ("A1", "A1")


def test_placements_follow_the_config_including_the_plate_axes(plate_sipper_config):
    v = sipper_vocabulary(plate_sipper_config)
    x, y = v.reservoirs["ssct"].a1_xy_mm                            # wherever A1 is taught
    assert v.placements()["ssct"].xy_mm("A2") == (x, y + 9.0)       # columns along +y
    assert v.placements()["ssct"].xy_mm("B1") == (x + 9.0, y)       # rows along +x
    v.reservoirs["ssct"].a1_xy_mm = (103.0, 2.0)
    assert v.placement("ssct").xy_mm("A1") == (103.0, 2.0), "built from the config as it is now"


def test_reservoir_definitions_cover_wells_and_tracked_bottles(plate_sipper_config):
    from fluidics.reservoirs import reservoir_definitions
    defs = reservoir_definitions(plate_sipper_config)
    loaded = plate_sipper_config.reagent_selection.selector_valves.loaded_volume_ul
    assert defs["port_4"] == {"loaded_ul": loaded["port_4"]}
    assert defs["port_5"] == {"loaded_ul": loaded["port_5"]}
    assert defs["ssct/H12"] == {"loaded_ul": gantry(plate_sipper_config).reservoirs[2].loaded_ul}
    assert "port_3" not in defs and len(defs) == 2 + 2 * 96
