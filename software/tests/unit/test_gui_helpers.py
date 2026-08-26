# tests/unit/test_gui_helpers.py
"""Tests for pure helpers in gui.py.

Importing gui pulls in PyQt5, which is fine without a display as long as no
QApplication is constructed. The widgets themselves have no test harness; only
module-level pure functions are covered here.
"""

import os
from datetime import datetime
from types import SimpleNamespace

import pytest

import gui
from fluidics.flow_monitor import FlowFault


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


class TestRecordingSaveDialog:
    """Start Recording asks where to save, pre-filled with the generated
    filename; Stop Recording reports the full path it saved to. Called unbound
    against a stub, since constructing TimeSeriesPlotWidget needs a
    QApplication.
    """

    class Button:
        def __init__(self, text):
            self._text = text

        def text(self):
            return self._text

        def setText(self, text):
            self._text = text

    class Stub:
        def __init__(self):
            self.record_btn = TestRecordingSaveDialog.Button("Start Recording")
            self.file = None
            self.writer = None

        def _record_filename(self):
            return "flow_test_20260819_000000.csv"

        def _record_header(self):
            return ["Time", "Flow Rate (uL/min)"]

        def close_recording(self):
            return gui.TimeSeriesPlotWidget.close_recording(self)

    @pytest.fixture
    def dialogs(self, monkeypatch, tmp_path):
        """Route the file dialog and message boxes; pin the shared last-dir."""
        calls = SimpleNamespace(defaults=[], chosen="", info=[], error=[])

        def fake_get_save_filename(parent, caption, default, filter):
            calls.defaults.append(default)
            return calls.chosen, filter

        monkeypatch.setattr(gui.QFileDialog, "getSaveFileName",
                            fake_get_save_filename)
        monkeypatch.setattr(gui.QMessageBox, "information",
                            lambda parent, title, text: calls.info.append(text))
        monkeypatch.setattr(gui.QMessageBox, "critical",
                            lambda parent, title, text: calls.error.append(text))
        monkeypatch.setattr(gui.TimeSeriesPlotWidget, "_last_record_dir",
                            str(tmp_path))
        return calls

    def test_cancelling_the_dialog_leaves_recording_unstarted(self, dialogs):
        stub = self.Stub()
        gui.TimeSeriesPlotWidget._toggle_record(stub)
        assert stub.file is None
        assert stub.record_btn.text() == "Start Recording"

    def test_the_dialog_is_prefilled_with_the_generated_name(self, dialogs, tmp_path):
        stub = self.Stub()
        gui.TimeSeriesPlotWidget._toggle_record(stub)
        assert dialogs.defaults == [str(tmp_path / "flow_test_20260819_000000.csv")]

    def test_starting_writes_the_header_at_the_chosen_path(self, dialogs, tmp_path):
        path = tmp_path / "run1.csv"
        dialogs.chosen = str(path)
        stub = self.Stub()
        gui.TimeSeriesPlotWidget._toggle_record(stub)
        assert stub.record_btn.text() == "Stop Recording"
        stub.close_recording()
        # read_text() collapses csv's \r\n line ending via universal newlines.
        assert path.read_text() == "Time,Flow Rate (uL/min)\n"

    def test_stopping_reports_where_the_file_was_saved(self, dialogs, tmp_path):
        dialogs.chosen = str(tmp_path / "run1.csv")
        stub = self.Stub()
        gui.TimeSeriesPlotWidget._toggle_record(stub)
        gui.TimeSeriesPlotWidget._toggle_record(stub)
        assert stub.file is None
        assert stub.record_btn.text() == "Start Recording"
        assert len(dialogs.info) == 1
        assert str(tmp_path / "run1.csv") in dialogs.info[0]

    def test_the_chosen_directory_is_remembered_for_the_next_dialog(self, dialogs, tmp_path):
        (tmp_path / "data").mkdir()
        dialogs.chosen = str(tmp_path / "data" / "run1.csv")
        gui.TimeSeriesPlotWidget._toggle_record(self.Stub())
        dialogs.chosen = ""
        gui.TimeSeriesPlotWidget._toggle_record(self.Stub())
        assert dialogs.defaults[1] == str(
            tmp_path / "data" / "flow_test_20260819_000000.csv")

    def test_stopping_with_no_open_file_shows_no_notice(self, dialogs):
        # close_recordings() at app exit can close the file while the button
        # still reads "Stop Recording"; a click then must not crash or report
        # a phantom save.
        stub = self.Stub()
        stub.record_btn.setText("Stop Recording")
        gui.TimeSeriesPlotWidget._toggle_record(stub)
        assert dialogs.info == []
        assert stub.record_btn.text() == "Start Recording"

    def test_a_failed_open_reports_and_stays_unstarted(self, dialogs, tmp_path):
        dialogs.chosen = str(tmp_path / "no_such_dir" / "run1.csv")
        stub = self.Stub()
        gui.TimeSeriesPlotWidget._toggle_record(stub)
        assert len(dialogs.error) == 1
        assert stub.file is None
        assert stub.record_btn.text() == "Start Recording"


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


class RecordingWriter:
    """A csv.writer stand-in. Shared with test_gui_flow_widget."""

    def __init__(self):
        self.rows = []

    def writerow(self, row):
        self.rows.append(list(row))


def make_flow_fault():
    """The canonical fault the recording tests format. Shared with
    test_gui_flow_widget so the two suites cannot drift onto different
    'canonical' faults."""
    return FlowFault(sensor_name="syringe_draw", expected_ul_min=500.0,
                     tolerance_fraction=0.3, measured_ul_min=12.0,
                     out_of_band_seconds=0.18, consecutive_samples=3)


class TestFlowRecordingRows:
    """What lands in the flow CSV. Called unbound against a stub, since
    constructing FlowSensorWidget needs a QApplication.

    The Fault column is the durable trace of a draw-protection trip -- the
    progress-bar notice is cleared at the next run -- so the fault row must
    line up with the header and carry the trip's own timestamp.
    """

    def stub(self, writer="unset"):
        return SimpleNamespace(
            writer=RecordingWriter() if writer == "unset" else writer,
            # Park the throttle so _on_reading returns right after the CSV
            # write instead of running the plot path, which needs widgets.
            last_update=float("inf"),
            query_interval=1,
        )

    def test_every_row_matches_the_header_width(self):
        stub = self.stub()
        gui.FlowSensorWidget._on_reading(stub, 500.0, 100.0)
        gui.FlowSensorWidget._on_fault(stub, "warn", make_flow_fault(), 100.06)
        header = gui.FlowSensorWidget._record_header(stub)
        assert header == ["Time", "Flow Rate (uL/min)", "Fault"]
        assert all(len(row) == len(header) for row in stub.writer.rows)

    def test_a_reading_row_leaves_the_fault_field_empty(self):
        stub = self.stub()
        gui.FlowSensorWidget._on_reading(stub, 500.0, 100.0)
        assert stub.writer.rows[0][1] == "500.00"
        assert stub.writer.rows[0][2] == ""

    def test_a_fault_row_names_the_mode_and_the_fault(self):
        stub = self.stub()
        gui.FlowSensorWidget._on_fault(stub, "warn", make_flow_fault(), 100.06)
        row = stub.writer.rows[0]
        assert row[0] == datetime.fromtimestamp(100.06)
        assert row[1] == ""
        assert row[2].startswith("warn: ")
        assert "syringe_draw" in row[2]
        assert "12" in row[2]          # the measured value survives into the CSV

    def test_no_recording_no_rows_no_crash(self):
        stub = self.stub(writer=None)
        gui.FlowSensorWidget._on_reading(stub, 500.0, 100.0)
        gui.FlowSensorWidget._on_fault(stub, "stop", make_flow_fault(), 100.06)


class TestAbortSequences:
    """One signal: the button cancels through the DeviceSet, which the worker
    waits on. Called unbound against a stub; worker=SimpleNamespace() only
    satisfies the `if self.worker` guard."""

    def test_abort_goes_through_the_device_set_only(self):
        aborted = []
        stub = SimpleNamespace(
            worker=SimpleNamespace(),
            experiment_ops=object(),
            devices=SimpleNamespace(abort=lambda: aborted.append(True)),
            abortButton=SimpleNamespace(setEnabled=lambda enabled: None),
            pauseButton=SimpleNamespace(setEnabled=lambda enabled: None),
        )
        gui.SequencesWidget.abortSequences(stub)
        assert aborted == [True]

    def test_abort_takes_the_pause_button_away_too(self):
        """The run is ending; there is nothing left to hold. The cancel also
        releases a run already held, so Abort needs no resume first."""
        enabled = []
        stub = SimpleNamespace(
            worker=SimpleNamespace(),
            experiment_ops=object(),
            devices=SimpleNamespace(abort=lambda: None),
            abortButton=SimpleNamespace(setEnabled=lambda on: None),
            pauseButton=SimpleNamespace(setEnabled=enabled.append),
        )
        gui.SequencesWidget.abortSequences(stub)
        assert enabled == [False]


class TestRunFinished:
    """The cancellation belongs to the run: _handle_finished clears it after
    the worker is reaped and before the manual tab is re-enabled, or the
    tab's moves would raise on a stale abort. Called unbound against a stub."""

    class Quiet:
        """Any attribute is another Quiet; calling one does nothing -- the
        widgets _handle_finished tidies up that this test does not care about."""

        def __getattr__(self, name):
            return TestRunFinished.Quiet()

        def __call__(self, *args):
            return None

    def test_the_signal_is_reset_before_the_manual_tab_returns(self, monkeypatch):
        monkeypatch.setattr(gui.QMessageBox, "information", lambda *args: None)
        order = []
        stub = self.Quiet()
        stub.worker = object()
        stub.worker_thread = SimpleNamespace(join=lambda: order.append("join"))
        stub.devices = SimpleNamespace(reset=lambda: order.append("reset"))
        stub.sequence_running = SimpleNamespace(
            emit=lambda running: order.append(("running", running)))
        gui.SequencesWidget._handle_finished(stub)
        assert order == ["join", "reset", ("running", False)]
        assert stub.worker is None


class TestPauseControls:
    """The button and the label, called unbound against stubs -- constructing
    SequencesWidget needs a QApplication.

    The two pause moments are the point: `paused` is "someone asked",
    `holding` is "the run has stopped at a gate". Between them a move is
    still finishing, and the operator must be able to tell -- "pausing" means
    liquid may still be moving.
    """

    class Button:
        def __init__(self, text="Pause"):
            self._text = text

        def text(self):
            return self._text

        def setText(self, text):
            self._text = text

        def setEnabled(self, enabled):
            self.enabled = enabled

    def _fields(self, paused=False, holding=0, total_time=100, elapsed=0):
        control = SimpleNamespace(paused=paused, holding=holding)
        calls = []
        return SimpleNamespace(
            devices=SimpleNamespace(
                run_control=control,
                pause=lambda: (calls.append("pause"),
                               setattr(control, "paused", True))[0],
                resume=lambda: (calls.append("resume"),
                                setattr(control, "paused", False))[0]),
            pauseButton=self.Button(),
            timeLabel=SimpleNamespace(setText=lambda text: None),
            progressBar=SimpleNamespace(setValue=lambda value: None),
            timer=SimpleNamespace(stop=lambda: None),
            total_time=total_time,
            elapsed_time=elapsed,
            calls=calls,
        )

    def stub(self, **kwargs):
        """The widget's own methods call back into self, so an unbound call
        needs them on the stub too."""
        stub = self._fields(**kwargs)
        stub._pauseState = lambda: gui.SequencesWidget._pauseState(stub)
        stub.updateTimeRemaining = lambda: gui.SequencesWidget.updateTimeRemaining(stub)
        return stub

    def test_the_first_press_pauses_and_offers_a_resume(self):
        stub = self.stub()
        gui.SequencesWidget.pauseSequences(stub)
        assert stub.calls == ["pause"]
        assert stub.pauseButton.text() == "Resume"

    def test_the_next_press_resumes(self):
        stub = self.stub(paused=True)
        stub.pauseButton.setText("Resume")
        gui.SequencesWidget.pauseSequences(stub)
        assert stub.calls == ["resume"]
        assert stub.pauseButton.text() == "Pause"

    def test_a_requested_pause_reads_as_pausing(self):
        """The move in flight is still going, and still counts against the
        estimate."""
        stub = self.stub(paused=True, holding=0)
        assert gui.SequencesWidget._pauseState(stub) == " (pausing\u2026)"
        gui.SequencesWidget.updateTimeRemaining(stub)
        assert stub.elapsed_time == 1, "a run still moving must spend time"

    def test_a_held_run_reads_as_paused_and_spends_no_time(self):
        """Ten minutes of coffee must not come off the estimate."""
        stub = self.stub(paused=True, holding=1)
        assert gui.SequencesWidget._pauseState(stub) == " (paused)"
        gui.SequencesWidget.updateTimeRemaining(stub)
        assert stub.elapsed_time == 0

    def test_a_running_run_says_nothing_and_spends_time(self):
        stub = self.stub()
        assert gui.SequencesWidget._pauseState(stub) == ""
        gui.SequencesWidget.updateTimeRemaining(stub)
        assert stub.elapsed_time == 1

    def test_the_label_carries_the_state(self):
        shown = []
        stub = self.stub(paused=True, holding=1)
        stub.timeLabel = SimpleNamespace(setText=shown.append)
        gui.SequencesWidget.updateTimeRemaining(stub)
        assert shown == ["00:01:40 remaining (paused)"]

    def test_the_tick_keeps_coming_while_held_even_at_zero_remaining(self):
        """Otherwise the label freezes on "pausing" and never says the run
        has actually stopped."""
        stopped = []
        stub = self.stub(paused=True, holding=1, total_time=1, elapsed=99)
        stub.timer = SimpleNamespace(stop=lambda: stopped.append(True))
        gui.SequencesWidget.updateTimeRemaining(stub)
        assert stopped == []

    def test_a_press_before_the_estimate_arrives_does_not_raise(self):
        """The worker posts the estimate to the event queue, so an operator
        who presses Pause within the first second finds total_time unset. A
        slot that raises there takes the whole GUI down with it."""
        stub = self.stub(total_time=None)
        gui.SequencesWidget.pauseSequences(stub)
        assert stub.calls == ["pause"]
        assert stub.pauseButton.text() == "Resume"

    def test_a_finished_run_puts_the_button_back(self):
        noop = lambda *args: None
        stub = SimpleNamespace(
            runButton=SimpleNamespace(setEnabled=noop),
            pauseButton=self.Button("Resume"),
            abortButton=SimpleNamespace(setEnabled=noop),
            progressBar=SimpleNamespace(setValue=noop),
            timeLabel=SimpleNamespace(setText=noop),
            sequenceLabel=SimpleNamespace(setText=noop),
            timer=SimpleNamespace(stop=noop),
            highlightRow=noop,
            worker=None,
            devices=SimpleNamespace(reset=noop),
            sequence_running=SimpleNamespace(emit=noop),
        )
        import pytest
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(gui.QMessageBox, "information", lambda *args: None)
            gui.SequencesWidget._handle_finished(stub)
        assert stub.pauseButton.text() == "Pause"
        assert stub.pauseButton.enabled is False
