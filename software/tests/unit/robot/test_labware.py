"""Plate geometry and well references: pure values, no hardware."""
import pytest

from fluidics.robot.labware import (PlateGeometry, PlatePlacement, WellRef,
                                    expand_wells, parse_ref, well_key)


G96 = PlateGeometry.from_well_count(96)


class TestReferences:
    def test_a_bare_well_is_on_the_default_reservoir(self):
        assert parse_ref("A3", "probes") == WellRef("probes", "A3")

    def test_a_qualified_well_names_its_reservoir(self):
        assert parse_ref("ssct/H12", "probes") == WellRef("ssct", "H12")

    def test_a_key_round_trips(self):
        assert well_key(WellRef("ssct", "A3")) == "ssct/A3"

    @pytest.mark.parametrize("text", ["", "A", "3", "I1", "A13", "a1", "ssct/", "/A1", "a/b/A1", "A1 ", "A1\n"])
    def test_bad_spellings_are_refused_with_the_rule(self, text):
        with pytest.raises(ValueError, match="well"):
            parse_ref(text, "probes")


class TestGeometry:
    def test_96_wells_are_8_by_12(self):
        assert (G96.rows, G96.columns) == (8, 12)

    def test_wells_run_column_major(self):
        wells = G96.wells()
        assert wells[:9] == ["A1", "B1", "C1", "D1", "E1", "F1", "G1", "H1", "A2"]
        assert wells[-1] == "H12" and len(wells) == 96

    def test_index_is_row_then_column_zero_based(self):
        assert G96.index("A1") == (0, 0)
        assert G96.index("H12") == (7, 11)

    def test_the_corners(self):
        assert G96.corners() == ["A1", "A12", "H1", "H12"]

    def test_a_range_expands_column_major_and_mixes_with_singles(self):
        assert expand_wells(["A1:C1", "H12"], G96) == ["A1", "B1", "C1", "H12"]
        assert expand_wells(["G12:H12"], G96) == ["G12", "H12"]

    def test_a_trailing_newline_is_not_a_well(self):
        assert not G96.is_well("A1\n")
        with pytest.raises(ValueError, match="not a well name"):
            expand_wells(["A1\n"], G96)
        with pytest.raises(ValueError, match="not a well name"):
            expand_wells(["A1:B1\n"], G96)

    def test_a_backwards_or_unknown_range_is_refused(self):
        with pytest.raises(ValueError):
            expand_wells(["C1:A1"], G96)
        with pytest.raises(ValueError):
            expand_wells(["A1:Z1"], G96)


class TestPlacement:
    def test_a1_is_where_it_was_taught(self):
        plate = PlatePlacement((12.5, 8.0), 55.0, G96)
        assert plate.xy_mm("A1") == (12.5, 8.0)

    def test_by_default_columns_run_along_x_and_rows_along_y_at_the_pitch(self):
        plate = PlatePlacement((12.5, 8.0), 55.0, G96)
        assert plate.xy_mm("A2") == (21.5, 8.0)
        assert plate.xy_mm("B1") == (12.5, 17.0)
        assert plate.xy_mm("H12") == (12.5 + 11 * 9.0, 8.0 + 7 * 9.0)

    def test_a_plate_with_its_long_side_along_y_has_columns_along_y_and_rows_along_x(self):
        """The XYG321-A layout (spec §2.1): two plates side by side along
        X, each with its 12 columns running along Y."""
        plate = PlatePlacement((12.0, 1.5), 50.0, G96, column_axis="+y", row_axis="+x")
        assert plate.xy_mm("A2") == (12.0, 10.5)
        assert plate.xy_mm("B1") == (21.0, 1.5)
        assert plate.xy_mm("H12") == (12.0 + 7 * 9.0, 1.5 + 11 * 9.0)

    def test_a_negative_axis_runs_the_other_way(self):
        plate = PlatePlacement((100.0, 100.0), 50.0, G96, column_axis="-x", row_axis="-y")
        assert plate.xy_mm("A2") == (91.0, 100.0)
        assert plate.xy_mm("B1") == (100.0, 91.0)

    def test_the_axes_must_be_known_and_perpendicular(self):
        with pytest.raises(ValueError, match="perpendicular"):
            PlatePlacement((0.0, 0.0), 50.0, G96, column_axis="+x", row_axis="-x")
        with pytest.raises(ValueError, match="column_axis"):
            PlatePlacement((0.0, 0.0), 50.0, G96, column_axis="x")

    def test_well_spacing_multiplies_the_pitch(self):
        plate = PlatePlacement((0.0, 0.0), 55.0, G96, well_spacing=2)
        assert plate.xy_mm("A2") == (18.0, 0.0)

    def test_the_corners_come_with_their_coordinates(self):
        plate = PlatePlacement((10.0, 20.0), 50.0, G96)
        assert plate.corners() == [("A1", (10.0, 20.0)), ("A12", (109.0, 20.0)),
                                   ("H1", (10.0, 83.0)), ("H12", (109.0, 83.0))]

    def test_an_unknown_well_is_refused(self):
        plate = PlatePlacement((0.0, 0.0), 55.0, G96)
        with pytest.raises(ValueError):
            plate.xy_mm("A13")
