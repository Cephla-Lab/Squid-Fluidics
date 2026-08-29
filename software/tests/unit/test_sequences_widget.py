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
from fluidics.control.config import available_port_count
from fluidics.sequences import SequenceListAdapter
from fluidics.subscribers import Subscribers

from .test_gui_helpers import FakeSession

FLOW = {"type": "flow_reagent", "fluidic_port": 1, "flow_rate": 500,
        "volume": 500}
TEMP = {"type": "set_temperature", "temperature": 37.0}


@pytest.fixture
def widget(qapp, flow_cell_config):
    config = flow_cell_config
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
        widget.setSequences([FLOW, TEMP])
        expected = [s.model_dump() for s in
                    SequenceListAdapter.validate_python([FLOW, TEMP])]
        assert widget.getSequences() == expected

    def test_a_field_at_its_default_still_gets_a_row(self, widget):
        """A default-valued field must still get an editable row -- a value
        the tree does not render is a value nobody can change."""
        widget.setSequences([FLOW])
        assert field_item(widget, 0, "incubation_time").text(1) == "0"
        assert field_item(widget, 0, "repeat").text(1) == "1"

    def test_editing_a_default_valued_field_lands_in_the_model(self, widget):
        widget.setSequences([FLOW])
        field_item(widget, 0, "incubation_time").setText(1, "5")
        assert widget.getSequences()[0]["incubation_time"] == 5.0

    def test_a_name_edit_lands_and_an_emptied_name_reads_as_the_type(self, widget):
        widget.setSequences([FLOW])
        top = widget.tree.topLevelItem(0)
        top.setText(0, "wash A")
        assert widget.getSequences()[0]["name"] == "wash A"
        top.setText(0, "")
        assert widget.getSequences()[0]["name"] is None
        assert top.text(0) == "Flow Reagent"

    def test_the_checkbox_is_the_include_field(self, widget):
        widget.setSequences([FLOW, TEMP])
        widget.tree.topLevelItem(0).setCheckState(0, gui.Qt.Unchecked)
        assert widget._sequences[0]["include"] is False
        selected = widget.getSequences(selected_only=True)
        assert [s["type"] for s in selected] == ["set_temperature"]

    def test_the_model_holds_what_the_operator_typed(self, widget):
        """The model is never rewritten by validation: the cell and the dict
        agree on the raw text, and getSequences coerces on the way out."""
        widget.setSequences([FLOW])
        field_item(widget, 0, "volume").setText(1, "750")
        assert widget._sequences[0]["volume"] == "750"
        assert widget.getSequences()[0]["volume"] == 750


class TestStructuralOps:
    def test_duplicate_inserts_an_independent_copy_after(self, widget):
        widget.setSequences([FLOW, TEMP])
        widget.tree.setCurrentItem(widget.tree.topLevelItem(0))
        widget.duplicateSequence()
        types = [s["type"] for s in widget.getSequences()]
        assert types == ["flow_reagent", "flow_reagent", "set_temperature"]
        # Independent: editing the copy leaves the original alone -- an
        # aliased duplicate would edit both rows at once.
        field_item(widget, 1, "volume").setText(1, "900")
        assert widget._sequences[0]["volume"] == 500
        assert widget._sequences[1]["volume"] == "900"

    def test_move_down_swaps_and_the_edges_hold(self, widget):
        widget.setSequences([FLOW, TEMP])
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
        widget.setSequences([FLOW, TEMP])
        # A selected child means its parent.
        widget.tree.setCurrentItem(widget.tree.topLevelItem(0).child(0))
        widget.removeSequence()
        assert [s["type"] for s in widget._sequences] == ["set_temperature"]

    def test_select_all_and_none_write_the_model(self, widget):
        widget.setSequences([FLOW, TEMP])
        widget.selectNone()
        assert widget._includedRows() == []
        widget.selectAll()
        assert widget._includedRows() == [0, 1]


class TestLiveValidation:
    def test_a_non_number_marks_the_row_and_blocks_the_run(self, widget):
        widget.setSequences([FLOW])
        assert widget.runButton.isEnabled()
        field_item(widget, 0, "volume").setText(1, "abc")
        assert "volume" in widget._invalid[0]
        assert widget.tree.topLevelItem(0).toolTip(0) == widget._invalid[0]
        assert not widget.runButton.isEnabled()
        assert "volume" in widget.runButton.toolTip()

    def test_a_port_the_rig_lacks_is_flagged(self, widget):
        widget.setSequences([FLOW])
        beyond = available_port_count(widget.config) + 1
        field_item(widget, 0, "fluidic_port").setText(1, str(beyond))
        assert f"fluidic_port={beyond}" in widget._invalid[0]

    def test_fixing_the_field_clears_the_verdict_and_frees_the_run(self, widget):
        widget.setSequences([FLOW])
        volume = field_item(widget, 0, "volume")
        volume.setText(1, "abc")
        volume.setText(1, "750")
        assert widget._invalid == {}
        assert widget.tree.topLevelItem(0).toolTip(0) == ""
        assert widget.runButton.isEnabled()
        assert widget.getSequences()[0]["volume"] == 750

    def test_the_edit_verbs_go_dead_while_a_job_runs_and_come_back(self, widget):
        """_running_rows is a positional snapshot: a mid-run move would walk
        the running highlight to the wrong row, so the structural verbs are
        dead while any job rides the list."""
        widget.setSequences([FLOW, TEMP])
        widget.session.kind = "run"
        widget._renderRunControls()
        for button in (widget.loadButton, widget.addButton, widget.removeButton,
                       widget.duplicateButton, widget.moveUpButton,
                       widget.moveDownButton):
            assert not button.isEnabled()
        assert widget.saveButton.isEnabled(), "reading the list stays allowed"
        widget.session.kind = None
        widget._renderRunControls()
        assert widget.moveUpButton.isEnabled()

    def test_an_unchecked_invalid_row_blocks_nothing(self, widget):
        """A run takes only the checked rows; a broken row left unchecked
        must not hold the valid ones hostage."""
        widget.setSequences([FLOW, TEMP])
        field_item(widget, 0, "volume").setText(1, "abc")
        assert not widget.runButton.isEnabled()
        widget.tree.topLevelItem(0).setCheckState(0, gui.Qt.Unchecked)
        assert widget.runButton.isEnabled()


class TestApplicationTypes:
    def test_a_wrong_application_row_is_flagged_and_blocks_the_run(self, widget):
        """The live paint speaks the same verdict as the run gate: this rig
        is Flow Cell, and an Open Chamber type must not wait until run time
        to be refused."""
        widget.setSequences([FLOW, {"type": "add_reagent", "fluidic_port": 2,
                                    "flow_rate": 500, "volume": 100}])
        assert "not a Flow Cell sequence type" in widget._invalid[1]
        assert not widget.runButton.isEnabled()
        widget.tree.setCurrentItem(widget.tree.topLevelItem(1))
        widget.removeSequence()
        assert widget.runButton.isEnabled()


class TestResumePreparesTheModel:
    def test_the_offer_rewrites_the_include_flags_through_the_model(
            self, widget, monkeypatch):
        """End to end through the real widget: a run over rows 0,2,3 that
        ends in flight on row 2 resumes as rows 2,3 -- row 1, never part of
        the run, keeps its own unchecked state."""
        monkeypatch.setattr(gui.QMessageBox, "question",
                            lambda *args: gui.QMessageBox.Yes)
        widget.setSequences([FLOW, dict(FLOW, include=False), FLOW, TEMP])
        widget.total_sequences = 3
        widget._running_rows = [0, 2, 3]
        widget._handle_progress(1, 2, gui.SEQUENCE_STARTED)
        widget._offerResume()
        assert widget._includedRows() == [2, 3]
        assert widget.tree.topLevelItem(0).checkState(0) == gui.Qt.Unchecked, \
            "the tree did not repaint from the model"
