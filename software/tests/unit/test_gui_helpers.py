# tests/unit/test_gui_helpers.py
"""Tests for pure helpers in gui.py.

Importing gui pulls in PyQt5, which is fine without a display as long as no
QApplication is constructed. The widgets themselves have no test harness; only
module-level pure functions are covered here.
"""

import os

import pytest

import gui


class TestSafeFilenamePart:
    """Recording filenames interpolate FlowSensorConfig.name, which is
    free-form config text. It must not be able to escape the working directory
    or break open().
    """

    def test_ordinary_name_passes_through(self):
        assert gui._safe_filename_part("syringe_draw") == "syringe_draw"

    def test_keeps_safe_punctuation(self):
        assert gui._safe_filename_part("flow-1.2_a") == "flow-1.2_a"

    @pytest.mark.parametrize("name", [
        "../../etc/passwd",
        "a/b",
        "a\\b",
        "/absolute",
    ])
    def test_strips_path_separators(self, name):
        cleaned = gui._safe_filename_part(name)
        assert "/" not in cleaned
        assert "\\" not in cleaned
        assert os.sep not in cleaned
        assert os.path.basename(cleaned) == cleaned

    @pytest.mark.parametrize("name", ["", "...", "///", "___"])
    def test_degenerate_names_fall_back(self, name):
        # Must still be a usable filename component, never empty.
        assert gui._safe_filename_part(name) == "sensor"

    def test_spaces_become_underscores(self):
        assert gui._safe_filename_part("waste line") == "waste_line"


class TestHighlightFollowsCheckedRows:
    """The worker runs only the checked sequences, so the index it reports is a
    position in that filtered list -- not a tree row. With a non-contiguous
    selection, highlighting the raw index lights up the wrong sequence.

    _handle_progress must translate through the rows snapshotted at run start.
    Called unbound against a stub, since constructing SequencesWidget needs a
    QApplication.
    """

    class Label:
        def setText(self, text):
            pass

    class Stub:
        def __init__(self, running_rows):
            self._running_rows = running_rows
            self.total_sequences = len(running_rows)
            self.sequenceLabel = TestHighlightFollowsCheckedRows.Label()
            self.highlighted = []

        def highlightRow(self, row):
            self.highlighted.append(row)

    def test_a_sparse_selection_lights_each_checked_row_in_turn(self):
        stub = self.Stub(running_rows=[0, 2, 4])
        for index in range(3):
            gui.SequencesWidget._handle_progress(stub, index, index + 1, "Started")
        assert stub.highlighted == [0, 2, 4]

    def test_an_out_of_range_index_clears_the_highlight(self):
        # Defensive: an index past the snapshot can only come from a worker
        # bug; better no highlight than a wrong one. (A tree that shrank
        # mid-run is caught separately, by highlightRow's own bounds check.)
        stub = self.Stub(running_rows=[0])
        gui.SequencesWidget._handle_progress(stub, 5, 6, "Started")
        assert stub.highlighted == [None]

    def test_a_negative_index_clears_the_highlight_too(self):
        # Python indexing would wrap -1 to the last checked row -- the guard
        # must treat it like any other impossible index.
        stub = self.Stub(running_rows=[0, 2])
        gui.SequencesWidget._handle_progress(stub, -1, 0, "Started")
        assert stub.highlighted == [None]


class TestCheckedRows:
    """_checkedRows is both the row filter getSequences(selected_only=True)
    iterates and the snapshot _handle_progress translates through, so it must
    list the checked rows in tree order.
    """

    class FakeItem:
        def __init__(self, checked):
            self._checked = checked

        def checkState(self, column):
            return gui.Qt.Checked if self._checked else gui.Qt.Unchecked

    class FakeTree:
        def __init__(self, states):
            self._items = [TestCheckedRows.FakeItem(s) for s in states]

        def topLevelItemCount(self):
            return len(self._items)

        def topLevelItem(self, i):
            return self._items[i]

    class Stub:
        def __init__(self, tree):
            self.tree = tree

    def test_returns_checked_rows_in_tree_order(self):
        stub = self.Stub(self.FakeTree([True, False, True, False, True]))
        assert gui.SequencesWidget._checkedRows(stub) == [0, 2, 4]

    def test_nothing_checked_gives_no_rows(self):
        stub = self.Stub(self.FakeTree([False, False]))
        assert gui.SequencesWidget._checkedRows(stub) == []


class TestDrawProtectionUnavailable:
    """Only MERFISHOperations arms the sensors, so on any other application a
    configured warn/stop mode is inert. Silence there would leave the operator
    believing a draw is protected when nothing is watching it.

    Called unbound against a stub, since the method touches only self.flowSensors
    and a message box -- constructing a QMainWindow needs a QApplication.
    """

    class Stub:
        def __init__(self, sensors):
            self.flowSensors = sensors

    class Sensor:
        def __init__(self, name, monitor):
            self.name = name
            self.monitor = monitor

    @pytest.fixture
    def shown(self, monkeypatch):
        messages = []
        monkeypatch.setattr(gui.QMessageBox, "warning",
                            lambda parent, title, text: messages.append(text))
        return messages

    def _run(self, sensors, draw_protection):
        stub = self.Stub(sensors)
        gui.FluidicsControlGUI._warn_if_draw_protection_unavailable(
            stub, draw_protection)
        return stub

    def test_a_configured_mode_is_reported(self, shown):
        self._run([self.Sensor("syringe_draw", "stop")], draw_protection=False)
        assert len(shown) == 1
        assert "syringe_draw" in shown[0]

    def test_the_mode_is_forced_off_so_the_gui_cannot_show_it_as_active(self, shown):
        stub = self._run([self.Sensor("s", "stop")], draw_protection=False)
        assert stub.flowSensors[0].monitor == "off"

    def test_sensors_already_off_are_not_reported(self, shown):
        self._run([self.Sensor("s", "off")], draw_protection=False)
        assert shown == []

    def test_nothing_is_reported_when_protection_is_available(self, shown):
        stub = self._run([self.Sensor("s", "stop")], draw_protection=True)
        assert shown == []
        assert stub.flowSensors[0].monitor == "stop"
