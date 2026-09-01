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
from qtpy.QtCore import Qt, QEvent
from qtpy.QtWidgets import QApplication
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
    # Actually destroy it: deleteLater only queues, and a widget that is
    # never destroyed keeps its subscriptions -- the log handler among
    # them -- attached for the rest of the session.
    w.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def field_item(widget, row, fname):
    """The tree row rendering one field of one sequence."""
    top = widget.tree.topLevelItem(row)
    for j in range(top.childCount()):
        child = top.child(j)
        if child.data(0, Qt.UserRole) == fname:
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
        widget.tree.topLevelItem(0).setCheckState(0, Qt.Unchecked)
        assert widget._model[0]["include"] is False
        selected = widget.getSequences(selected_only=True)
        assert [s["type"] for s in selected] == ["set_temperature"]

    def test_the_model_holds_what_the_operator_typed(self, widget):
        """The model is never rewritten by validation: the cell and the dict
        agree on the raw text, and getSequences coerces on the way out."""
        widget.setSequences([FLOW])
        field_item(widget, 0, "volume").setText(1, "750")
        assert widget._model[0]["volume"] == "750"
        assert widget.getSequences()[0]["volume"] == 750


class TestLogPane:
    """The run tab shows what the run log is saying, and exports what it
    is showing."""

    def test_a_logged_line_reaches_the_pane(self, widget, caplog):
        import logging
        # The app's own configure_console puts the logger at DEBUG; the
        # test session leaves it higher, so say what the app says.
        caplog.set_level(logging.DEBUG, logger="fluidics")
        logging.getLogger("fluidics").info("Sequence 1/3 (prime)")
        # The handler posts to the Qt thread; deliver what it queued.
        gui.QApplication.processEvents()
        assert "Sequence 1/3 (prime)" in widget.logView.toPlainText()

    def test_debug_chatter_stays_out_of_the_pane(self, widget, caplog):
        """The pane is the console's level, not the log file's: per-move
        valve traffic would bury the run's own narration."""
        import logging
        caplog.set_level(logging.DEBUG, logger="fluidics")
        logging.getLogger("fluidics").debug("Valve 0: open port 1")
        gui.QApplication.processEvents()
        assert "open port 1" not in widget.logView.toPlainText()

    def test_the_pane_keeps_only_its_last_lines(self, widget):
        for n in range(widget.LOG_LINES + 50):
            widget._handle_log_line(f"line {n}")
        text = widget.logView.toPlainText()
        assert "line 0" not in text and f"line {widget.LOG_LINES + 49}" in text

    def test_export_writes_exactly_what_is_shown(self, widget, tmp_path,
                                                 monkeypatch):
        widget._handle_log_line("first line")
        widget._handle_log_line("second line")
        out = tmp_path / "exported.txt"
        monkeypatch.setattr(gui.QFileDialog, "getSaveFileName",
                            lambda *a, **k: (str(out), ""))
        widget.exportLog()
        assert out.read_text().splitlines() == ["first line", "second line"]

    def test_a_cancelled_export_writes_nothing_and_says_nothing(
            self, widget, tmp_path, monkeypatch):
        """Cancel is not an error: no file, and no dialog either. The
        second assertion also keeps a regression here from opening a real
        modal, which would hang the suite rather than fail it."""
        said = []
        monkeypatch.setattr(gui.QFileDialog, "getSaveFileName",
                            lambda *a, **k: ("", ""))
        monkeypatch.setattr(gui.QMessageBox, "critical",
                            lambda *args: said.append(args[1]))
        widget.exportLog()
        assert list(tmp_path.iterdir()) == []
        assert said == []

    def test_an_export_that_fails_says_so_and_raises_nothing(
            self, widget, tmp_path, monkeypatch):
        blocked = tmp_path / "blocked"
        blocked.mkdir()
        said = []
        monkeypatch.setattr(gui.QFileDialog, "getSaveFileName",
                            lambda *a, **k: (str(blocked), ""))
        monkeypatch.setattr(gui.QMessageBox, "critical",
                            lambda *args: said.append(args[1]))
        widget.exportLog()
        assert said == ["Export Failed"]

    def test_the_handler_detaches_with_the_widget(self, qapp,
                                                  flow_cell_config):
        """logging keeps handlers globally: one left attached would post
        to a destroyed widget, and take the process with it."""
        import logging
        from fluidics.subscribers import Subscribers
        system = SimpleNamespace(
            devices=SimpleNamespace(selector_valves=SimpleNamespace(
                get_port_names=lambda: ["Port 1"])),
            session=FakeSession(), warnings=Subscribers("test warnings"))
        logger = logging.getLogger("fluidics")
        before = len(logger.handlers)
        w = gui.SequencesWidget(flow_cell_config, system)
        assert len(logger.handlers) == before + 1
        w.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        assert len(logger.handlers) == before, "the log handler outlived the tab"


class TestExpansion:
    """A loaded file opens collapsed -- one line per sequence, not a wall
    of every field -- and what the operator opens stays open."""

    def _opened(self, widget):
        return [row for row in range(widget.tree.topLevelItemCount())
                if widget.tree.topLevelItem(row).isExpanded()]

    def test_a_loaded_file_opens_collapsed(self, widget):
        widget.setSequences([FLOW, TEMP, FLOW])
        assert self._opened(widget) == []
        assert widget.tree.topLevelItem(0).childCount() > 0, \
            "collapsed, not empty: the fields are there to open"

    def test_loading_again_collapses_what_the_last_file_had_open(self, widget):
        widget.setSequences([FLOW, TEMP])
        widget.tree.topLevelItem(0).setExpanded(True)
        widget.setSequences([TEMP, FLOW])
        assert self._opened(widget) == [], \
            "a new file's rows are not the old file's"

    def test_a_structural_change_leaves_the_open_rows_open(self, widget):
        """Adding, moving or removing re-renders every row; a row the
        operator opened to edit must not close under them."""
        widget.setSequences([FLOW, TEMP])
        widget.tree.topLevelItem(1).setExpanded(True)
        widget.tree.setCurrentItem(widget.tree.topLevelItem(1))
        widget.duplicateSequence()
        assert self._opened(widget) == [1]

    def test_an_open_row_follows_its_sequence_through_a_move(self, widget):
        """Open state belongs to the sequence, not the position: move the
        row you opened and it is still the one standing open."""
        widget.setSequences([FLOW, TEMP])
        widget.tree.topLevelItem(0).setExpanded(True)
        widget.tree.setCurrentItem(widget.tree.topLevelItem(0))
        widget.moveSequenceDown()
        assert [s["type"] for s in widget._model] == \
            ["set_temperature", "flow_reagent"]
        assert self._opened(widget) == [1], \
            "the open row stayed at the index instead of following the move"

    def test_a_removed_sequence_takes_its_open_state_with_it(self, widget):
        """Its identity must not linger: a later sequence allocated at the
        same address would otherwise render open for no reason."""
        widget.setSequences([FLOW, TEMP])
        widget.tree.topLevelItem(1).setExpanded(True)
        widget.tree.setCurrentItem(widget.tree.topLevelItem(1))
        widget.removeSequence()
        assert self._opened(widget) == []
        assert widget._opened == set(), "a dead sequence's id was kept"

    def test_a_row_the_operator_closed_stays_closed(self, widget):
        widget.setSequences([FLOW, TEMP])
        widget.tree.topLevelItem(0).setExpanded(True)
        widget.tree.topLevelItem(0).setExpanded(False)
        widget._refresh()
        assert self._opened(widget) == []


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
        assert widget._model[0]["volume"] == 500
        assert widget._model[1]["volume"] == "900"

    def test_move_down_swaps_and_the_edges_hold(self, widget):
        widget.setSequences([FLOW, TEMP])
        widget.tree.setCurrentItem(widget.tree.topLevelItem(0))
        widget.moveSequenceDown()
        assert [s["type"] for s in widget._model] == \
            ["set_temperature", "flow_reagent"]
        # The moved row stays selected, so a second press keeps moving it --
        # and at the edge, nothing happens.
        widget.moveSequenceDown()
        assert [s["type"] for s in widget._model] == \
            ["set_temperature", "flow_reagent"]
        widget.moveSequenceUp()
        assert [s["type"] for s in widget._model] == \
            ["flow_reagent", "set_temperature"]

    def test_remove_takes_the_selected_sequence_out(self, widget):
        widget.setSequences([FLOW, TEMP])
        # A selected child means its parent.
        widget.tree.setCurrentItem(widget.tree.topLevelItem(0).child(0))
        widget.removeSequence()
        assert [s["type"] for s in widget._model] == ["set_temperature"]

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
        assert "volume" in widget._model.problem(0)
        assert widget.tree.topLevelItem(0).toolTip(0) == widget._model.problem(0)
        assert not widget.runButton.isEnabled()
        assert "volume" in widget.runButton.toolTip()

    def test_a_port_the_rig_lacks_is_flagged(self, widget):
        widget.setSequences([FLOW])
        beyond = available_port_count(widget.config) + 1
        field_item(widget, 0, "fluidic_port").setText(1, str(beyond))
        assert f"fluidic_port={beyond}" in widget._model.problem(0)

    def test_fixing_the_field_clears_the_verdict_and_frees_the_run(self, widget):
        widget.setSequences([FLOW])
        volume = field_item(widget, 0, "volume")
        volume.setText(1, "abc")
        volume.setText(1, "750")
        assert widget._model.problem(0) is None
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
        widget.tree.topLevelItem(0).setCheckState(0, Qt.Unchecked)
        assert widget.runButton.isEnabled()


class TestApplicationTypes:
    def test_a_wrong_application_row_is_flagged_and_blocks_the_run(self, widget):
        """The live paint speaks the same verdict as the run gate: this rig
        is Flow Cell, and an Open Chamber type must not wait until run time
        to be refused."""
        widget.setSequences([FLOW, {"type": "add_reagent", "fluidic_port": 2,
                                    "flow_rate": 500, "volume": 100}])
        assert "not a Flow Cell sequence type" in widget._model.problem(1)
        assert not widget.runButton.isEnabled()
        widget.tree.setCurrentItem(widget.tree.topLevelItem(1))
        widget.removeSequence()
        assert widget.runButton.isEnabled()


class TestResumeRunsTheTail:
    def test_run_ended_starts_the_tail_through_the_real_widget(
            self, widget, monkeypatch):
        """End to end: a run over rows 0,2,3 that ends in flight on tree
        row 2 resumes as exactly that entry and the one after -- the
        checkboxes stay as they were, and the never-in-the-run row 1 is
        untouched."""
        monkeypatch.setattr(gui.QMessageBox, "question",
                            lambda *args: gui.QMessageBox.Yes)
        monkeypatch.setattr(gui.QMessageBox, "information", lambda *args: None)
        widget.setSequences([FLOW, dict(FLOW, include=False), FLOW, TEMP])
        from fluidics.time_estimate import plan_run
        plan = plan_run(widget.config, widget.getSequences(selected_only=True))
        rows = widget._includedRows()
        widget._plan = tuple(e._replace(row=rows[e.row]) for e in plan)
        handed = []
        widget.system.run = lambda seqs, plan=None: handed.append(plan)
        widget._handle_run_event(
            gui.RunEnded("run-1", "stopped", None, 0.0, position=1))
        assert [entry.row for entry in handed[0]] == [2, 3]
        assert widget._includedRows() == [0, 2, 3], "the checkboxes changed"


class TestRunStartRelabelsThePlan:
    def test_plan_rows_become_tree_rows_before_the_run_starts(
            self, widget, monkeypatch):
        """system.plan() numbers rows by the filtered selection; the widget
        must relabel them to its own tree rows before handing the plan to
        the run, or a sparse selection highlights (and resumes) the wrong
        row."""
        from tests.worker_helpers import plan_for
        monkeypatch.setattr(gui.QMessageBox, "question",
                            lambda *args: gui.QMessageBox.Yes)
        widget.setSequences([FLOW, dict(FLOW, include=False), FLOW])
        handed = []
        widget.system.plan = lambda seqs: plan_for(
            [{"type": "flow_reagent"}] * len(seqs))
        widget.system.run = lambda seqs, plan=None: handed.append(plan)
        widget.runSelectedSequences()
        assert [entry.row for entry in handed[0]] == [0, 2]
