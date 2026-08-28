# tests/unit/test_sequences_widget.py
"""SequencesWidget's model: the sequence list is a list of dicts, the tree
its view, and every edit routes back through it.

Constructs the real widget under Qt's offscreen platform (the shared qapp
fixture), against a fake system -- the model, its rendering, and the live
validation are this widget's own; the run path is pinned by the stubs in
test_gui_helpers and the session tests. Same-thread setText delivers
itemChanged synchronously, so no event loop needs to run.
"""

from types import SimpleNamespace

import pytest

import gui
from fluidics.control.config import available_port_count, load_config
from fluidics.sequences import SequenceListAdapter
from fluidics.subscribers import Subscribers

from .test_gui_helpers import FakeSession

FLOW = {"type": "flow_reagent", "fluidic_port": 1, "flow_rate": 500,
        "volume": 500}
TEMP = {"type": "set_temperature", "temperature": 37.0}


@pytest.fixture
def widget(qapp, fixtures_dir):
    config = load_config(str(fixtures_dir / "flow_cell_config.yaml"))
    system = SimpleNamespace(
        devices=SimpleNamespace(selector_valves=SimpleNamespace(
            get_port_names=lambda: [f"Port {i}" for i in range(1, 9)])),
        session=FakeSession(),
        warnings=Subscribers("test warnings"),
    )
    w = gui.SequencesWidget(config, system)
    yield w
    w.deleteLater()


def field_item(widget, row, fname):
    """The tree row rendering one field of one sequence."""
    top = widget.tree.topLevelItem(row)
    for j in range(top.childCount()):
        child = top.child(j)
        if child.data(0, gui.Qt.UserRole) == fname:
            return child
    raise AssertionError(f"no rendered row for {fname!r}")


class TestTheModelIsTheTruth:
    def test_what_goes_in_comes_back_out_validated(self, widget):
        widget.setSequences([dict(FLOW), dict(TEMP)])
        expected = [s.model_dump() for s in
                    SequenceListAdapter.validate_python([FLOW, TEMP])]
        assert widget.getSequences() == expected

    def test_a_field_at_its_default_still_gets_a_row(self, widget):
        """The old tree rendered only values that differed from the default,
        so a default could never be edited afterward."""
        widget.setSequences([dict(FLOW)])
        assert field_item(widget, 0, "incubation_time").text(1) == "0"
        assert field_item(widget, 0, "repeat").text(1) == "1"

    def test_editing_a_default_valued_field_lands_in_the_model(self, widget):
        widget.setSequences([dict(FLOW)])
        field_item(widget, 0, "incubation_time").setText(1, "5")
        assert widget.getSequences()[0]["incubation_time"] == 5.0

    def test_a_name_edit_lands_and_an_emptied_name_reads_as_the_type(self, widget):
        widget.setSequences([dict(FLOW)])
        top = widget.tree.topLevelItem(0)
        top.setText(0, "wash A")
        assert widget.getSequences()[0]["name"] == "wash A"
        top.setText(0, "")
        assert widget.getSequences()[0]["name"] is None
        assert top.text(0) == "Flow Reagent"

    def test_the_checkbox_is_the_include_field(self, widget):
        widget.setSequences([dict(FLOW), dict(TEMP)])
        widget.tree.topLevelItem(0).setCheckState(0, gui.Qt.Unchecked)
        assert widget._sequences[0]["include"] is False
        selected = widget.getSequences(selected_only=True)
        assert [s["type"] for s in selected] == ["set_temperature"]
        assert widget._includedRows() == [1]


class TestStructuralOps:
    def test_duplicate_inserts_an_independent_copy_after(self, widget):
        widget.setSequences([dict(FLOW), dict(TEMP)])
        widget.tree.setCurrentItem(widget.tree.topLevelItem(0))
        widget.duplicateSequence()
        types = [s["type"] for s in widget.getSequences()]
        assert types == ["flow_reagent", "flow_reagent", "set_temperature"]
        # Independent: editing the copy leaves the original alone.
        field_item(widget, 1, "volume").setText(1, "900")
        assert widget._sequences[0]["volume"] == 500
        assert widget._sequences[1]["volume"] == 900

    def test_a_duplicate_is_independent_even_before_it_validates(self, widget):
        """Validation replaces a valid row with its dump, which would mask a
        shared reference; an invalid row keeps its original dict, so a
        duplicate that aliased it would edit both rows at once."""
        widget.setSequences([dict(FLOW)])
        field_item(widget, 0, "volume").setText(1, "abc")
        widget.tree.setCurrentItem(widget.tree.topLevelItem(0))
        widget.duplicateSequence()
        field_item(widget, 1, "volume").setText(1, "900")
        assert widget._sequences[0]["volume"] == "abc"
        assert widget._sequences[1]["volume"] == 900

    def test_move_down_swaps_and_the_edges_hold(self, widget):
        widget.setSequences([dict(FLOW), dict(TEMP)])
        widget.tree.setCurrentItem(widget.tree.topLevelItem(0))
        widget.moveSequenceDown()
        assert [s["type"] for s in widget._sequences] == \
            ["set_temperature", "flow_reagent"]
        # The moved row stays selected, so a second press keeps moving it --
        # and at the edge, nothing happens.
        widget.moveSequenceDown()
        assert [s["type"] for s in widget._sequences] == \
            ["set_temperature", "flow_reagent"]
        widget.moveSequenceUp()
        assert [s["type"] for s in widget._sequences] == \
            ["flow_reagent", "set_temperature"]

    def test_remove_takes_the_selected_sequence_out(self, widget):
        widget.setSequences([dict(FLOW), dict(TEMP)])
        # A selected child means its parent, as the old tree behaved.
        widget.tree.setCurrentItem(widget.tree.topLevelItem(0).child(0))
        widget.removeSequence()
        assert [s["type"] for s in widget._sequences] == ["set_temperature"]

    def test_select_all_and_none_write_the_model(self, widget):
        widget.setSequences([dict(FLOW), dict(TEMP)])
        widget.selectNone()
        assert widget._includedRows() == []
        widget.selectAll()
        assert widget._includedRows() == [0, 1]


class TestLiveValidation:
    def test_a_non_number_marks_the_row_and_blocks_the_run(self, widget):
        widget.setSequences([dict(FLOW)])
        assert widget.runButton.isEnabled()
        field_item(widget, 0, "volume").setText(1, "abc")
        assert "volume" in widget._invalid[0]
        assert widget.tree.topLevelItem(0).toolTip(0) == widget._invalid[0]
        assert not widget.runButton.isEnabled()
        assert "volume" in widget.runButton.toolTip()

    def test_a_port_the_rig_lacks_is_flagged(self, widget):
        widget.setSequences([dict(FLOW)])
        beyond = available_port_count(widget.config) + 1
        field_item(widget, 0, "fluidic_port").setText(1, str(beyond))
        assert f"fluidic_port={beyond}" in widget._invalid[0]

    def test_fixing_the_field_clears_the_verdict_and_frees_the_run(self, widget):
        widget.setSequences([dict(FLOW)])
        volume = field_item(widget, 0, "volume")
        volume.setText(1, "abc")
        volume.setText(1, "750")
        assert widget._invalid == {}
        assert widget.tree.topLevelItem(0).toolTip(0) == ""
        assert widget.runButton.isEnabled()
        assert widget._sequences[0]["volume"] == 750

    def test_an_unchecked_invalid_row_blocks_nothing(self, widget):
        """A run takes only the checked rows; a broken row left unchecked
        must not hold the valid ones hostage."""
        widget.setSequences([dict(FLOW), dict(TEMP)])
        field_item(widget, 0, "volume").setText(1, "abc")
        assert not widget.runButton.isEnabled()
        widget.tree.topLevelItem(0).setCheckState(0, gui.Qt.Unchecked)
        assert widget.runButton.isEnabled()
