# tests/unit/test_sequence_list.py
"""SequenceList: the editor's model, without Qt.

No QApplication is constructed here, and none is needed -- which is the
point of the extraction: what a run would take, what is wrong with a row,
and every structural verb can be exercised (or driven by a script, or a
headless embedder) with no GUI in the process.
"""

import pytest

from fluidics.sequence_list import SequenceList
from fluidics.sequences import type_label

from ..conftest import FLOW, TEMP, in_a_fresh_interpreter


def flow_cell(*sequences):
    return SequenceList("Flow Cell", port_limit=24, sequences=sequences)


class TestWhatARunWouldTake:
    def test_what_goes_in_comes_back_out_validated(self):
        model = flow_cell(FLOW, TEMP)
        assert [s["type"] for s in model.validated()] == \
            ["flow_reagent", "set_temperature"]

    def test_the_caller_s_dicts_stay_the_caller_s(self):
        original = dict(FLOW)
        model = flow_cell(original)
        model.set_field(0, "volume", "750")
        assert original["volume"] == 500, "the model edited the caller's dict"

    def test_included_rows_in_model_order(self):
        model = flow_cell({"include": True}, {"include": False},
                          {"include": True}, {}, {"include": False})
        assert model.included_rows() == [0, 2, 3], "a bare row counts as included"

    def test_nothing_included_gives_no_rows(self):
        model = flow_cell({"include": False}, {"include": False})
        assert model.included_rows() == []

    def test_only_the_included_come_out_when_asked(self):
        model = flow_cell(FLOW, dict(TEMP, include=False))
        assert [s["type"] for s in model.validated(included_only=True)] == \
            ["flow_reagent"]


class TestTheVerdicts:
    def test_a_non_number_marks_the_row_and_blocks_the_run(self):
        model = flow_cell(FLOW)
        model.set_field(0, "volume", "abc")
        assert "volume" in model.problem(0)
        assert model.blocking_error().startswith("Sequence 1:")

    def test_a_port_the_rig_lacks_is_flagged(self):
        model = flow_cell(dict(FLOW, fluidic_port=99))
        assert "1..24" in model.problem(0)

    def test_a_wrong_application_row_is_flagged(self):
        model = flow_cell(dict(FLOW, type="add_reagent"))
        assert model.problem(0) is not None
        assert "Flow Cell" in model.problem(0)

    def test_an_unchecked_invalid_row_blocks_nothing(self):
        model = flow_cell(dict(FLOW, volume="abc", include=False), TEMP)
        assert model.problem(0) is not None
        assert model.blocking_error() is None

    def test_fixing_the_field_clears_the_verdict(self):
        model = flow_cell(FLOW)
        model.set_field(0, "volume", "abc")
        model.set_field(0, "volume", "750")
        assert model.problem(0) is None and model.blocking_error() is None

    def test_zero_is_a_value_not_an_empty_field(self):
        """Only the editor's empty cell means "unset". 0 is a real
        temperature and a real incubation time, and a caller driving the
        model headlessly has no empty string to offer."""
        model = SequenceList("Flow Cell", port_limit=24,
                             sequences=[dict(TEMP, incubation_time=5)])
        model.set_field(0, "temperature", 0)
        model.set_field(0, "incubation_time", 0.0)
        assert model[0]["temperature"] == 0 and model[0]["incubation_time"] == 0.0
        assert model.problem(0) is None, model.problem(0)
        assert model.validated()[0]["temperature"] == 0

    def test_an_emptied_cell_reads_as_unset(self):
        model = flow_cell(dict(FLOW, fill_tubing_with=3))
        model.set_field(0, "fill_tubing_with", "")
        assert model[0]["fill_tubing_with"] is None

    def test_the_row_holds_what_was_typed_not_what_validates(self):
        """A half-typed field must stay as typed -- rewriting it under the
        operator is how an edit becomes unfinishable."""
        model = flow_cell(FLOW)
        model.set_field(0, "volume", "75")
        assert model[0]["volume"] == "75"
        assert model.validated()[0]["volume"] == 75, "coerced on the way out"


class TestStructuralVerbs:
    def test_add_appends_and_names_its_row(self):
        model = flow_cell(FLOW)
        assert model.add(TEMP) == 1
        assert [s["type"] for s in model] == ["flow_reagent", "set_temperature"]

    def test_remove_says_what_to_select_next(self):
        model = flow_cell(FLOW, TEMP, FLOW)
        assert model.remove(1) == 1
        assert len(model) == 2
        assert model.remove(1) == 0, "removing the last row selects the one before"

    def test_duplicate_inserts_an_independent_copy_after(self):
        model = flow_cell(FLOW, TEMP)
        assert model.duplicate(0) == 1
        assert [s["type"] for s in model] == \
            ["flow_reagent", "flow_reagent", "set_temperature"]
        model.set_field(1, "volume", "900")
        assert model[0]["volume"] == 500, "the copy edited the original"

    def test_move_swaps_and_the_edges_hold(self):
        model = flow_cell(FLOW, TEMP)
        assert model.move(0, +1) == 1
        assert [s["type"] for s in model] == ["set_temperature", "flow_reagent"]
        assert model.move(1, +1) is None, "the bottom row cannot move down"
        assert model.move(0, -1) is None, "the top row cannot move up"

    def test_a_move_carries_the_row_itself(self):
        """The dicts are swapped, not their contents: anything holding a row
        by identity (the editor's open-state set) follows the move."""
        model = flow_cell(FLOW, TEMP)
        moved = model[0]
        model.move(0, +1)
        assert model[1] is moved

    def test_select_all_and_none(self):
        model = flow_cell(FLOW, TEMP)
        model.set_all_included(False)
        assert model.included_rows() == []
        model.set_all_included(True)
        assert model.included_rows() == [0, 1]

    def test_replace_drops_what_was_there(self):
        model = flow_cell(FLOW, TEMP)
        model.replace([TEMP])
        assert len(model) == 1 and model.problem(0) is None


class TestTheNameSentinel:
    def test_a_blank_name_is_no_name(self):
        model = flow_cell(dict(FLOW, name="prime"))
        assert model.set_name(0, "   ") == type_label(model[0])
        assert model[0]["name"] is None

    def test_typing_the_type_s_own_label_back_is_no_name(self):
        """The row titles itself from the type when unnamed, so that title
        typed back must not freeze into the file as a name."""
        model = flow_cell(FLOW)
        label = type_label(model[0])
        assert model.set_name(0, label) == label
        assert model[0]["name"] is None

    def test_a_real_name_sticks_and_titles_the_row(self):
        model = flow_cell(FLOW)
        assert model.set_name(0, "  prime  ") == "prime"
        assert model[0]["name"] == "prime"


def test_the_model_needs_no_qt():
    """The extraction's whole point, pinned: building and validating a
    list must not drag Qt into the process."""
    loaded = in_a_fresh_interpreter(
        "import sys; import fluidics.sequence_list as m; "
        "m.SequenceList('Flow Cell', 24, [{'type': 'priming', "
        "'fluidic_port': 1, 'flow_rate': 1, 'volume': 1}]).validated(); "
        "print([n for n in sys.modules if 'qt' in n.lower() or 'PyQt' in n])")
    assert loaded == "[]", f"Qt was imported: {loaded}"
