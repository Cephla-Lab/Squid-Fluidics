# tests/unit/test_gui_helpers.py
"""Tests for pure helpers in gui.py.

Importing gui pulls in Qt (through qtpy), which is fine without a display as
long as no QApplication is constructed. The widgets themselves have no test
harness; only
module-level pure functions are covered here.
"""

import os
from datetime import datetime
from types import SimpleNamespace

import pytest

import shutil

import gui
import fluidics.qt.manual_control as manual_control
from qtpy.QtWidgets import QDialog


def _bind(name, stub, cls=None):
    """An unbound widget method, bound to a stub."""
    cls = cls or gui.SequencesWidget
    return lambda *args: getattr(cls, name)(stub, *args)
from fluidics.flow_monitor import FlowFault
from fluidics.events import RunEnded, SequenceCompleted, SequenceStarted
from fluidics.run_session import SessionSnapshot
from fluidics.subscribers import Subscribers


class Button:
    """A QPushButton's text, enabled state and tooltip, as the widgets use
    them."""

    def __init__(self, text="Pause"):
        self._text = text
        self.enabled = None
        self.tooltip = None

    def text(self):
        return self._text

    def setText(self, text):
        self._text = text

    def setEnabled(self, enabled):
        self.enabled = enabled

    def setToolTip(self, text):
        self.tooltip = text


class Quiet:
    """Any attribute is another Quiet; calling one does nothing -- for the
    tidying a widget method does that a test does not care about."""

    def __getattr__(self, name):
        return Quiet()

    def __call__(self, *args):
        return None


class FakeSession:
    """The RunSession as the widgets see it: a kind, the state that follows
    from it, and the controls recorded."""

    def __init__(self, kind=None, paused=False, at_rest=False, cancelled=False,
                 elapsed_seconds=0.0):
        self.kind = kind
        self.paused = paused
        self.at_rest = at_rest
        self.cancelled = cancelled
        self.elapsed_seconds = elapsed_seconds
        self.calls = []
        self.state = Subscribers("fake session state")
        self.events = Subscribers("fake run events")

    def snapshot(self):
        # The real namedtuple, keyword-constructed: a renamed or added
        # field fails here instead of silently drifting from the widget.
        return SessionSnapshot(kind=self.kind, cancelled=self.cancelled,
                               paused=self.paused, at_rest=self.at_rest,
                               elapsed_seconds=self.elapsed_seconds)

    @property
    def busy(self):
        return self.kind is not None

    def abort(self):
        self.calls.append("abort")
        self.cancelled = True
        return True

    def pause(self):
        self.calls.append("pause")
        self.paused = True
        return True

    def resume(self):
        self.calls.append("resume")
        self.paused = False
        return True

    def wait(self, timeout=None):
        self.calls.append(("wait", timeout))
        return True


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


from ..worker_helpers import plan_for


def plan_of(durations, rows=None):
    """A plan stub for display tests: one empty-sequence entry per figure;
    rows are tree rows (the widget relabels plans at run start)."""
    return plan_for([{"type": "flow_reagent"}] * len(durations),
                    seconds=list(durations), rows=rows)


class TestHighlightFollowsThePlan:
    """The plan's rows are relabeled to tree rows at run start, so a sparse
    selection highlights the row the operator sees -- not the position in
    the filtered list. Called unbound against a stub."""

    def test_a_sparse_selection_lights_each_checked_row_in_turn(self):
        stub = SimpleNamespace(
            _plan=plan_of([1.0, 1.0, 1.0], rows=[0, 2, 4]),
            sequenceLabel=SimpleNamespace(setText=lambda t: None),
            highlighted=[],
        )
        stub.highlightRow = stub.highlighted.append
        for position in range(3):
            gui.SequencesWidget._handle_run_event(
                stub, SequenceStarted("run-1", position))
        assert stub.highlighted == [0, 2, 4]


class TestRecordingSaveDialog:
    """Start Recording asks where to save, pre-filled with the generated
    filename; Stop Recording reports the full path it saved to. Called unbound
    against a stub, since constructing TimeSeriesPlotWidget needs a
    QApplication.
    """

    class Stub:
        def __init__(self):
            self.record_btn = Button("Start Recording")
            self.file = None
            self.writer = None
            self._flushed_at = 0.0

        def _record_filename(self):
            return "flow_test_20260819_000000.csv"

        def _record_header(self):
            return ["Time", "Flow Rate (uL/min)"]

        def _write_row(self, row):
            return gui.TimeSeriesPlotWidget._write_row(self, row)

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


def deliver_posted_events():
    """Run what _post_event queued. The sensor widgets marshal readings and
    faults to the Qt thread the same way the rest of fluidics.qt does, so a
    test publishing on the caller's thread must let the queue drain before
    asserting -- which is also how the reading arrives in production."""
    gui.QApplication.sendPostedEvents(None, gui.WorkerEvent.EVENT_TYPE)


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
        stub = SimpleNamespace(
            writer=RecordingWriter() if writer == "unset" else writer,
            # Park the throttle so _on_reading returns right after the CSV
            # write instead of running the plot path, which needs widgets.
            last_update=float("inf"),
            query_interval=1,
            file=None,              # nothing to flush; the writer records
            _flushed_at=0.0,
        )
        stub._write_row = lambda row: gui.TimeSeriesPlotWidget._write_row(stub, row)
        return stub

    def test_every_row_matches_the_header_width(self):
        stub = self.stub()
        gui.FlowSensorWidget._on_reading(stub, 500.0, 100.0)
        gui.FlowSensorWidget._on_fault(stub, "warn", make_flow_fault(), 100.06)
        header = gui.FlowSensorWidget._record_header(stub)
        assert header == ["Time", "Flow Rate (uL/min)", "Fault"]
        assert all(len(row) == len(header) for row in stub.writer.rows)

    def test_rows_are_flushed_on_a_cadence_not_per_row(self, tmp_path,
                                                        monkeypatch):
        """A flow sensor writes ~17 rows a second; flushing each one is a
        syscall per sample on the reader's thread. What a crash costs is
        bounded by the interval instead -- the same bound the long-format
        recorder keeps."""
        import csv
        import fluidics.qt.sensor_plots as sensor_plots
        clock = iter([1000.0, 1000.5, 1002.0])
        monkeypatch.setattr(sensor_plots.time, "monotonic", lambda: next(clock))
        path = tmp_path / "rec.csv"
        stub = self.stub(writer=None)
        stub.file = open(path, "w", newline="", encoding="utf-8")
        stub.writer = csv.writer(stub.file)
        try:
            stub._write_row(["a"])                  # 1000.0: due, flushes
            assert path.read_text().splitlines() == ["a"]
            stub._write_row(["b"])                  # 1000.5: too soon
            assert path.read_text().splitlines() == ["a"], "flushed per row"
            stub._write_row(["c"])                  # 1002.0: due again
            assert path.read_text().splitlines() == ["a", "b", "c"]
        finally:
            stub.file.close()

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
    """One signal: the button cancels through the session, which every waiting
    device and the worker share. The cancel also releases a run already held,
    so Abort needs no resume first. Called unbound against a stub."""

    def test_abort_goes_through_the_session_and_kills_the_controls(self):
        stub = run_widget()
        gui.SequencesWidget.abortSequences(stub)
        assert stub.session.calls == ["abort"]
        # The run is over; there is nothing left to hold or to abort.
        assert stub.pauseButton.enabled is False
        assert stub.abortButton.enabled is False

    def test_abort_does_nothing_when_no_run_is_the_job(self):
        stub = SimpleNamespace(session=FakeSession(kind="manual"))
        gui.SequencesWidget.abortSequences(stub)
        assert stub.session.calls == []


class TestRunEnded:
    """RunEnded reports; the session's state change resets the display.
    Driven here in the order the events are posted: RunEnded lands inside
    the end's transition, before state(None) -- the dialog shows over the
    run display as it stood, then the reset follows. Called unbound
    against stubs."""

    def _ending(self, monkeypatch):
        order = []
        monkeypatch.setattr(gui.QMessageBox, "information",
                            lambda *args: order.append(("info", args[1])))
        monkeypatch.setattr(gui.QMessageBox, "critical",
                            lambda *args: order.append(("error", args[1])))
        stub = Quiet()
        stub.session = FakeSession()     # idle: the reset is current, not stale
        stub._renderRunControls = lambda: order.append("render")
        return stub, order

    def test_a_completed_run_says_finished_then_the_reset_redraws(self, monkeypatch):
        stub, order = self._ending(monkeypatch)
        gui.SequencesWidget._reportRunEnded(
            stub, RunEnded("run-1", "finished", None, 5.0, None))
        gui.SequencesWidget._handle_state(stub)
        assert order == [("info", "Finished"), "render"]

    def test_a_stopped_run_says_stopped_not_finished(self, monkeypatch):
        """The operator pressed Abort: one dialog saying so, not an Error
        followed by a Finished."""
        stub, order = self._ending(monkeypatch)
        offered = []
        stub._offerResume = offered.append
        stub._plan = plan_of([1.0])
        gui.SequencesWidget._reportRunEnded(
            stub, RunEnded("run-1", "stopped", None, 5.0, position=0))
        gui.SequencesWidget._handle_state(stub)
        assert order == [("info", "Stopped"), "render"]
        assert offered == [0], "the resume offer must follow the dialog"

    def test_a_failed_run_says_why_once(self, monkeypatch):
        stub, order = self._ending(monkeypatch)
        stub._offerResume = lambda position: None
        stub._plan = plan_of([1.0])
        gui.SequencesWidget._reportRunEnded(
            stub, RunEnded("run-1", "failed", "pump fault", 5.0, position=0))
        gui.SequencesWidget._handle_state(stub)
        assert order == [("error", "Error"), "render"]

    def test_a_jobs_start_resets_nothing(self):
        """Only the end of a job clears the display: a state change with
        the rig busy must not stop the clock the button handler just
        started."""
        stopped = []
        stub = Quiet()
        stub.timer = SimpleNamespace(stop=lambda: stopped.append(True))
        for kind in ("run", "manual"):
            stub.session = FakeSession(kind=kind)
            gui.SequencesWidget._handle_state(stub)
        assert stopped == []

    def test_a_manual_jobs_start_deadens_this_tabs_controls(self):
        """Every state transition redraws the buttons: a manual move must
        deaden Run here without the main window's tab guard helping."""
        stub = run_widget()
        stub.session.kind = "manual"
        gui.SequencesWidget._handle_state(stub)
        assert stub.runButton.enabled is False
        assert stub.moveUpButton.enabled is False


def run_widget(paused=False, at_rest=False, total_time=100, elapsed=0):
    """A SequencesWidget stub mid-run, for the clock-and-buttons methods
    called unbound -- constructing the real widget needs a QApplication.
    Module-level beside the fakes it is built from: more than one test class
    paints against it."""
    session = FakeSession(kind="run", paused=paused,
                          at_rest=at_rest, elapsed_seconds=elapsed)
    stub = SimpleNamespace(
        session=session,
        runButton=Button(), pauseButton=Button(), abortButton=Button(),
        loadButton=Button(), addButton=Button(), removeButton=Button(),
        duplicateButton=Button(), moveUpButton=Button(), moveDownButton=Button(),
        timeLabel=SimpleNamespace(setText=lambda text: None),
        progressBar=SimpleNamespace(setValue=lambda value: None),
        timer=SimpleNamespace(stop=lambda: None),
        total_time=total_time,
        calls=session.calls,
    )
    # The widget's methods call back into self, so an unbound call needs
    # them on the stub too.
    for name in ("_showTimeRemaining", "_renderRunControls"):
        stub.__dict__[name] = _bind(name, stub)
    # A static method needs no stub of its own; a valid list blocks nothing;
    # the usage table has its own tests.
    stub.__dict__["_pauseSuffix"] = gui.SequencesWidget._pauseSuffix
    stub.__dict__["_blockingError"] = lambda: None
    stub.__dict__["_renderUsage"] = lambda: None
    return stub


class TestPauseControls:
    """The button and the label, called unbound against stubs -- constructing
    SequencesWidget needs a QApplication.

    The two pause moments are the point: `paused` is "someone asked",
    `at_rest` is "the run has actually stopped". Between them a move is still
    finishing, and the operator must be able to tell -- "pausing" means liquid
    may still be moving.
    """

    @pytest.mark.parametrize("paused, at_rest, suffix", [
        (False, False, ""),
        (True, False, " (pausing\u2026)"),
        (True, True, " (paused)"),
        # (False, True) cannot arise: RunControl.at_rest is paused *and*
        # something parked, pinned in test_errors.py.
    ])
    def test_the_two_moments_and_their_label(self, paused, at_rest, suffix):
        """What each costs is the clock's business now (TestRunningClock in
        test_errors); the tick paints the session's figure with the suffix."""
        shown = []
        stub = run_widget(paused=paused, at_rest=at_rest, elapsed=7)
        stub.timeLabel = SimpleNamespace(setText=shown.append)
        assert gui.SequencesWidget._pauseSuffix(paused, at_rest) == suffix
        gui.SequencesWidget.updateTimeRemaining(stub)
        # 7 of the 100 elapsed: the label proves the tick read the session's
        # clock rather than counting one of its own.
        assert shown == [f"00:01:33 remaining{suffix}"]

    def test_the_label_and_its_clock_come_from_one_snapshot(self):
        """The clock a line prints and the pause it names must come from the
        same instant: another thread owns them, and a paint that read twice
        could pair a pre-pause clock with a post-pause label."""
        reads = []
        session = FakeSession(kind="run", paused=True, at_rest=True,
                              elapsed_seconds=7)
        original = session.snapshot

        def counting():
            reads.append(True)
            return original()

        session.snapshot = counting
        shown = []
        stub = run_widget()
        stub.session = session
        stub.timeLabel = SimpleNamespace(setText=shown.append)
        gui.SequencesWidget._showTimeRemaining(stub)
        assert len(reads) == 1, "the paint read the session more than once"
        assert shown == ["00:01:33 remaining (paused)"]

    def test_the_first_press_pauses_and_offers_a_resume(self):
        stub = run_widget()
        gui.SequencesWidget.pauseSequences(stub)
        assert stub.calls == ["pause"]
        assert stub.pauseButton.text() == "Resume"

    def test_the_next_press_resumes(self):
        stub = run_widget(paused=True)
        gui.SequencesWidget.pauseSequences(stub)
        assert stub.calls == ["resume"]
        assert stub.pauseButton.text() == "Pause"

    def test_a_press_repaints_the_label_without_touching_the_clock(self):
        """The press redraws the last-painted state with the new suffix; the
        clock is the session's and only the tick reads it. (Before the clock
        moved, a press routed through the tick charged a second per click.)"""
        shown = []
        stub = run_widget(elapsed=10)
        stub.timeLabel = SimpleNamespace(setText=shown.append)
        gui.SequencesWidget.pauseSequences(stub)
        assert shown == ["00:01:30 remaining (pausing\u2026)"]

    def test_a_press_before_the_estimate_arrives_does_not_raise(self):
        """The worker posts the estimate to the event queue, so an operator
        who presses Pause within the first second finds total_time unset. A
        slot that raises there takes the whole GUI down with it."""
        stub = run_widget(total_time=None)
        gui.SequencesWidget.pauseSequences(stub)
        assert stub.calls == ["pause"]
        assert stub.pauseButton.text() == "Resume"

    def test_the_tick_keeps_coming_while_held_even_at_zero_remaining(self):
        """Otherwise the label freezes on "pausing" and never says the run has
        actually stopped."""
        stopped = []
        shown = []
        stub = run_widget(paused=True, at_rest=True, total_time=1, elapsed=99)
        stub.timer = SimpleNamespace(stop=lambda: stopped.append(True))
        stub.timeLabel = SimpleNamespace(setText=shown.append)
        gui.SequencesWidget.updateTimeRemaining(stub)
        assert stopped == []
        assert shown == ["00:00:00 remaining (paused)"]

    def test_a_run_that_ends_on_its_own_takes_the_controls_with_it(self):
        """A flow fault cancels from the MCU reader thread, so no button press
        and no callback runs: the tick is what notices."""
        stub = run_widget()
        stub.session.cancelled = True
        gui.SequencesWidget.updateTimeRemaining(stub)
        assert stub.pauseButton.enabled is False
        assert stub.abortButton.enabled is False

    def test_a_finished_run_puts_the_button_back(self):
        """The end of the job -- the session's state change, whatever ended
        it -- restores the controls."""
        stub = Quiet()
        stub.session = FakeSession()
        stub.pauseButton = Button("Resume")
        stub.runButton = Button()
        stub.abortButton = Button()
        stub._renderRunControls = _bind("_renderRunControls", stub)
        stub._blockingError = lambda: None
        gui.SequencesWidget._handle_state(stub)
        assert stub.pauseButton.text() == "Pause"
        assert stub.pauseButton.enabled is False
        assert stub.runButton.enabled is True


class TestManualControl:
    """The manual tab picks a ManualOperations verb and hands it to _run,
    which hands it to the session. Called unbound against stubs; the verbs
    are recorded rather than run."""

    class Manual:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            def verb(*args, **kwargs):
                self.calls.append((name, args, sorted(kwargs)))
            return verb

    def stub(self):
        stub = SimpleNamespace(
            manual=self.Manual(),
            session=FakeSession(),
            valveCombo=SimpleNamespace(currentData=lambda: 3),
            syringePortCombo=SimpleNamespace(currentText=lambda: "2"),
            speedCombo=SimpleNamespace(currentData=lambda: 500.0),
            volumeSpinBox=SimpleNamespace(value=lambda: 300),
            pumpInput=SimpleNamespace(value=lambda: 2.5),
            _started=lambda seconds: None,
        )
        stub._run = lambda verb: verb()
        stub._syringeArgs = _bind("_syringeArgs", stub, gui.ManualControlWidget)
        return stub

    def test_the_valve_combo_opens_the_port_through_run(self):
        stub = self.stub()
        gui.ManualControlWidget.openValve(stub)
        assert stub.manual.calls == [("open_port", (3,), [])]

    @pytest.mark.parametrize("button, verb, args", [
        ("extractSyringe", "extract", (2, 300, 500.0)),
        ("dispenseSyringe", "dispense", (2, 300, 500.0)),
        ("emptySyringe", "empty_to_waste", ()),
    ])
    def test_each_syringe_button_is_one_verb_with_a_progress_hook(self, button, verb, args):
        stub = self.stub()
        getattr(gui.ManualControlWidget, button)(stub)
        assert stub.manual.calls == [(verb, args, ["on_started"])]

    def test_the_disc_pump_runs_for_the_spin_box_seconds_through_run(self):
        stub = self.stub()
        gui.ManualControlWidget.startDiscPump(stub)
        assert stub.manual.calls == [("aspirate", (2.5,), ["on_started"])]

    def _runnable(self, refuse=False):
        """A stub _run can drive: the session records what it is handed."""
        handed, posted, enabled = [], [], []

        def run_manual(verb, callbacks):
            if refuse:
                raise RuntimeError("the rig is busy: a run is in progress")
            handed.append((verb, callbacks))

        stub = SimpleNamespace(
            system=SimpleNamespace(run_manual=run_manual),
            setControlsEnabled=enabled.append,
            _post_event=lambda name, *args: posted.append((name, args)),
        )
        return stub, handed, posted, enabled

    def test_the_verb_and_its_three_reports_go_to_the_session(self):
        stub, handed, posted, enabled = self._runnable()
        verb = lambda: None
        gui.ManualControlWidget._run(stub, verb)
        assert enabled == [False]
        (given, callbacks), = handed
        assert given is verb
        callbacks["on_stopped"]()
        callbacks["on_error"]("no reply from pump")
        callbacks["on_finished"]()
        assert posted == [("operationStopped", ()), ("handleError", ("no reply from pump",)),
                          ("operationFinished", ())]

    def test_a_refused_press_leaves_the_controls_alone(self):
        stub, handed, posted, enabled = self._runnable(refuse=True)
        gui.ManualControlWidget._run(stub, lambda: None)
        assert handed == [] and enabled == []

    def test_stop_aborts_only_a_manual_move(self):
        stub = SimpleNamespace(session=FakeSession(kind="manual"))
        gui.ManualControlWidget.stopMove(stub)
        stub.session.kind = "run"
        gui.ManualControlWidget.stopMove(stub)
        assert stub.session.calls == ["abort"]

    def _finishing(self, timed):
        stub = Quiet()
        stub.enabled, stub.bar = [], []
        stub.setControlsEnabled = stub.enabled.append
        stub.syringeProgressBar = SimpleNamespace(setValue=stub.bar.append)
        timer = SimpleNamespace(active=timed)
        timer.isActive = lambda: timer.active
        timer.stop = lambda: setattr(timer, "active", False)
        stub.progress_timer = timer
        stub._settleBar = _bind("_settleBar", stub, gui.ManualControlWidget)
        return stub

    def test_finished_frees_the_tab_and_completes_a_bar_still_counting(self):
        stub = self._finishing(timed=False)
        gui.ManualControlWidget.operationFinished(stub)      # a valve move: no bar
        assert stub.bar == [] and stub.enabled == [True]
        stub = self._finishing(timed=True)
        gui.ManualControlWidget.operationFinished(stub)      # a timed move that ran out
        assert stub.bar == [100] and stub.enabled == [True]

    def test_a_stopped_move_zeroes_the_bar_with_no_dialog_and_finished_frees_the_tab(
            self, monkeypatch):
        monkeypatch.setattr(gui.QMessageBox, "critical",
                            lambda *args: pytest.fail("a stop is not an error"))
        stub = self._finishing(timed=True)
        gui.ManualControlWidget.operationStopped(stub)
        gui.ManualControlWidget.operationFinished(stub)
        assert stub.bar == [0] and stub.enabled == [True]

    def test_an_error_zeroes_the_bar_and_says_why(self, monkeypatch):
        dialogs = []
        monkeypatch.setattr(gui.QMessageBox, "critical", lambda *args: dialogs.append(args[2]))
        stub = self._finishing(timed=True)
        gui.ManualControlWidget.handleError(stub, "boom")
        gui.ManualControlWidget.operationFinished(stub)
        assert stub.bar == [0] and stub.enabled == [True]
        assert dialogs == ["Manual operation failed: boom"]

    def test_the_stop_button_is_live_exactly_while_the_others_are_not(self):
        states = []
        control = SimpleNamespace(setEnabled=lambda on: states.append(("control", on)))
        stub = SimpleNamespace(_controls=[control],
                               stopButton=SimpleNamespace(setEnabled=lambda on: states.append(("stop", on))))
        gui.ManualControlWidget.setControlsEnabled(stub, False)
        gui.ManualControlWidget.setControlsEnabled(stub, True)
        assert states == [("control", False), ("stop", True), ("control", True), ("stop", False)]


class TestMainWindowJobs:
    """The main window keeps the tab that did not start the job dead while it
    runs, and asks before closing under a live job (the system then stops it
    before the devices close)."""

    def _tabs(self, kind):
        enabled = {}
        stub = SimpleNamespace(RUN_TAB=0, MANUAL_TAB=1, session=FakeSession(kind=kind),
                               tabWidget=SimpleNamespace(setTabEnabled=enabled.__setitem__))
        gui.FluidicsControlGUI._renderTabs(stub)
        return enabled

    def test_the_other_tab_goes_dead_for_the_length_of_the_job(self):
        """The tabs follow the session, not the announcement they were
        posted with: _renderTabs takes no kind, so a notification held up
        behind a modal cannot deaden a tab for a job that has ended."""
        assert self._tabs("run") == {0: True, 1: False}
        assert self._tabs("manual") == {0: False, 1: True}
        assert self._tabs(None) == {0: True, 1: True}

    def _closing(self, busy):
        session = FakeSession(kind="run" if busy else None)
        return SimpleNamespace(session=session), session.calls

    def test_an_idle_rig_closes_without_a_question(self, monkeypatch):
        monkeypatch.setattr(gui.QMessageBox, "question",
                            lambda *args: pytest.fail("nothing to ask about"))
        stub, order = self._closing(busy=False)
        assert gui.FluidicsControlGUI._quiesce(stub) is True
        assert order == []

    def test_declining_keeps_the_window_and_the_job(self, monkeypatch):
        monkeypatch.setattr(gui.QMessageBox, "question", lambda *args: gui.QMessageBox.No)
        stub, order = self._closing(busy=True)
        assert gui.FluidicsControlGUI._quiesce(stub) is False
        assert order == []

    def test_accepting_lets_the_window_close(self, monkeypatch):
        monkeypatch.setattr(gui.QMessageBox, "question", lambda *args: gui.QMessageBox.Yes)
        stub, order = self._closing(busy=True)
        assert gui.FluidicsControlGUI._quiesce(stub) is True
        assert order == [], "the stopping is the system's, in close()"


class TestRunDisplayClocks:
    """3.2: every run starts its clocks fresh, and the tick outlives the
    estimate -- an estimate is an estimate. Called unbound against stubs."""

    def test_a_new_run_starts_its_clocks_fresh(self):
        started = []
        labeled = []
        stub = SimpleNamespace(
            total_time=300,
            sequenceLabel=SimpleNamespace(setText=labeled.append),
            timer=SimpleNamespace(start=started.append),
            _renderRunControls=lambda: None,
        )
        gui.SequencesWidget._beginRunDisplay(stub, 7)
        assert stub.total_time is None, "the old estimate would price the new run"
        assert labeled == ["0/7 sequences"]
        assert started == [1000]

    def test_the_tick_outlives_the_estimate(self):
        """A run longer than its estimate still needs the label at 00:00:00
        and -- since the tick is what watches for a flow-fault self-cancel --
        the buttons kept honest."""
        stopped, shown = [], []
        stub = run_widget(total_time=100, elapsed=200)
        stub.timer = SimpleNamespace(stop=lambda: stopped.append(True))
        stub.timeLabel = SimpleNamespace(setText=shown.append)
        gui.SequencesWidget.updateTimeRemaining(stub)
        assert stopped == [], "the tick stopped on the estimate, not the run"
        assert shown == ["00:00:00 remaining"]


class TestPickConfig:
    """3.3: --config, then the rig's own local config, then the last file
    picked; a dialog instead of a traceback when none exists or one fails."""

    class Settings(dict):
        def value(self, key):
            return self.get(key)

        def setValue(self, key, value):
            self[key] = value

    @pytest.fixture
    def picking(self, monkeypatch, tmp_path, fixtures_dir):
        ns = SimpleNamespace(settings=self.Settings(), asked=[], errors=[], tmp=tmp_path)
        monkeypatch.setattr(gui, "QSettings", lambda: ns.settings)
        monkeypatch.setattr(gui.QFileDialog, "getOpenFileName",
                            lambda *a, **k: (ns.asked.pop(0) if ns.asked else "", ""))
        monkeypatch.setattr(gui.QMessageBox, "critical",
                            lambda parent, title, text: ns.errors.append(text))
        monkeypatch.chdir(tmp_path)
        ns.write = lambda name: str(shutil.copy(fixtures_dir / "flow_cell_config.yaml",
                                                tmp_path / name))
        return ns

    def test_the_rigs_own_config_wins_and_is_remembered_absolutely(self, picking):
        picking.write("config.yaml")
        picking.settings["config_path"] = picking.write("elsewhere.yaml")
        config = gui.pick_config()
        assert config is not None
        assert config.source_path == picking.settings["config_path"], \
            "the config's source is the file save_config must write back to"
        remembered = picking.settings["config_path"]
        assert remembered.endswith("config.yaml"), "the remembered file outranked the rig's own"
        assert os.path.isabs(remembered), \
            "a relative memory means whatever directory comes next -- inert when needed"

    def test_the_cli_path_outranks_everything(self, picking):
        picking.write("config.yaml")
        given = picking.write("given.yaml")
        config = gui.pick_config(given)
        assert config is not None and config.source_path == os.path.abspath(given)
        assert picking.settings["config_path"] == os.path.abspath(given)

    def test_the_last_picked_file_serves_when_the_rig_has_none(self, picking):
        picking.settings["config_path"] = picking.write("elsewhere.yaml")
        assert gui.pick_config() is not None
        assert picking.errors == []
        assert picking.settings["config_path"].endswith("elsewhere.yaml")

    def test_nothing_found_asks_and_cancel_means_none(self, picking):
        assert gui.pick_config() is None
        assert picking.errors == []

    def test_a_file_that_fails_to_load_gets_a_dialog_then_asks_again(self, picking):
        (picking.tmp / "config.yaml").write_text("application: 'No Such Application'\n")
        picking.asked.append(picking.write("good.yaml"))
        assert gui.pick_config() is not None
        assert len(picking.errors) == 1 and "config.yaml" in picking.errors[0]
        assert picking.settings["config_path"].endswith("good.yaml")


class TestBringupDialogs:
    def test_a_stuck_valve_gets_the_same_dialog_as_an_unplugged_pump(self, qapp, monkeypatch):
        """One DeviceError family, one bring-up dialog: fail fast, report well."""
        from fluidics.errors import DeviceError
        dialogs = []
        monkeypatch.setattr(gui.QMessageBox, "critical",
                            lambda parent, title, text: dialogs.append((title, text)))

        def stuck(config, simulation, on_issue=None):
            raise DeviceError("Selector valve 0: at position 1, expected 2 -- check the valve is free to rotate")

        monkeypatch.setattr(gui.FluidicsSystem, "build", stuck)
        with pytest.raises(SystemExit):
            gui.FluidicsControlGUI(None, is_simulation=True)
        assert dialogs and "free to rotate" in dialogs[0][1]


class TestReAnchoring:
    """When a sequence completes, what remains is the estimate of the plan
    entries not yet run -- whatever the finished ones actually took. Called
    unbound against stubs."""

    def stub(self, durations, elapsed=100.0):
        return SimpleNamespace(
            session=FakeSession(kind="run", elapsed_seconds=elapsed),
            _plan=plan_of(durations), total_time=999.0,
        )

    def test_a_completed_sequence_reanchors_the_countdown(self):
        stub = self.stub([60.0, 40.0, 20.0], elapsed=100.0)
        gui.SequencesWidget._handle_run_event(
            stub, SequenceCompleted("run-1", position=0))
        assert stub.total_time == pytest.approx(100.0 + 60.0), \
            "the remainder is the not-yet-run estimates from the clock's now"

    def test_the_last_completion_anchors_to_the_clock_alone(self):
        stub = self.stub([60.0, 40.0], elapsed=95.0)
        gui.SequencesWidget._handle_run_event(
            stub, SequenceCompleted("run-1", position=1))
        assert stub.total_time == pytest.approx(95.0)


class TestConfirmStart:
    def test_the_dialog_carries_the_figures_and_no_means_no(self, monkeypatch):
        asked = []

        def question(parent, title, text, buttons, default):
            asked.append(text)
            return gui.QMessageBox.No

        monkeypatch.setattr(gui.QMessageBox, "question", question)
        stub = SimpleNamespace()
        assert gui.SequencesWidget._confirmStart(stub, 3725, 7) is False
        assert "7 sequence(s)" in asked[0] and "01:02:05" in asked[0]

    def test_yes_starts(self, monkeypatch):
        monkeypatch.setattr(gui.QMessageBox, "question",
                            lambda *args: gui.QMessageBox.Yes)
        assert gui.SequencesWidget._confirmStart(SimpleNamespace(), 60, 1) is True


class TestPortNames:
    """Renaming ports: the dialog collects the mapping, editPortNames writes
    it into the config and the rig's own file, the combo repaints in place."""

    def test_the_dialog_prefills_and_returns_only_named_ports(self, qapp):
        dialog = gui.PortNamesDialog(None, [1, 2, 3, 4], {"port_2": "DAPI"})
        assert [edit.text() for _port, edit in dialog._edits] == \
            ["", "DAPI", "", ""]
        dialog._edits[0][1].setText("  wash  ")
        dialog._edits[1][1].setText("")         # cleared by the operator
        dialog.accept()
        assert dialog.result_mapping == {"port_1": "wash"}

    def test_the_dialog_names_the_ports_the_rig_offers(self, qapp):
        """A rig with gaps -- an unplumbed position between two lines --
        gets a row per port it has, keyed by that port and not by row."""
        dialog = gui.PortNamesDialog(None, [1, 5, 9], None)
        assert [port for port, _edit in dialog._edits] == [1, 5, 9]
        dialog._edits[1][1].setText("bleach")
        dialog.accept()
        assert dialog.result_mapping == {"port_5": "bleach"}

    def test_a_rename_lands_in_config_file_and_combo(self, qapp, monkeypatch,
                                                     tmp_path, fixtures_dir):
        config_path = str(tmp_path / "config.yaml")
        shutil.copy(fixtures_dir / "flow_cell_config.yaml", config_path)
        config = gui.load_config(config_path)

        class FakeDialog:
            def __init__(self, parent, port_count, mapping):
                self.result_mapping = {"port_1": "DAPI"}

            def exec_(self):
                return QDialog.Accepted

        monkeypatch.setattr(manual_control, "PortNamesDialog", FakeDialog)
        refreshed = []
        stub = SimpleNamespace(session=FakeSession(), config=config,
                               _refreshPortNames=lambda: refreshed.append(True))
        gui.ManualControlWidget.editPortNames(stub)
        assert config.reagent_selection.selector_valves.name_mapping == \
            {"port_1": "DAPI"}
        assert refreshed == [True]
        reloaded = gui.load_config(config_path)
        assert reloaded.reagent_selection.selector_valves.name_mapping == \
            {"port_1": "DAPI"}

    def test_busy_refuses_before_the_dialog_opens(self, monkeypatch):
        warned = []
        monkeypatch.setattr(gui.QMessageBox, "warning",
                            lambda parent, title, text: warned.append(title))
        constructed = []
        monkeypatch.setattr(manual_control, "PortNamesDialog",
                            lambda *args: constructed.append(True))
        stub = SimpleNamespace(session=FakeSession(kind="manual"))
        gui.ManualControlWidget.editPortNames(stub)
        assert warned == ["Rig busy"] and constructed == []

    def _combo(self, qapp, ports, selected):
        from qtpy.QtWidgets import QComboBox
        combo = QComboBox()
        for port, label in ports:
            combo.addItem(label, port)
        combo.setCurrentIndex(combo.findData(selected))
        return combo

    def test_the_repaint_keeps_the_selection_and_moves_no_valve(self, qapp):
        combo = self._combo(qapp, [(1, "Port 1: "), (2, "Port 2: "),
                                   (3, "Port 3: ")], selected=3)
        moved = []
        combo.currentIndexChanged.connect(moved.append)
        stub = SimpleNamespace(
            valveCombo=combo,
            manual=SimpleNamespace(port_names=lambda: [
                (1, "Port 1: DAPI"), (2, "Port 2: "), (3, "Port 3: ")]))
        stub._fillPorts = _bind("_fillPorts", stub, gui.ManualControlWidget)
        gui.ManualControlWidget._refreshPortNames(stub)
        assert combo.currentData() == 3, "the rename moved the selection"
        assert combo.itemText(0) == "Port 1: DAPI"
        assert moved == [], "a rename must not move a valve"

    def test_the_selection_survives_a_list_with_gaps(self, qapp):
        """Ports the rig does not offer are absent, so the item at index
        n is not port n+1 -- the selection follows its port."""
        combo = self._combo(qapp, [(1, "Port 1: "), (5, "Port 5: "),
                                   (9, "Port 9: ")], selected=9)
        stub = SimpleNamespace(
            valveCombo=combo,
            manual=SimpleNamespace(port_names=lambda: [
                (1, "Port 1: "), (5, "Port 5: x"), (9, "Port 9: ")]))
        stub._fillPorts = _bind("_fillPorts", stub, gui.ManualControlWidget)
        gui.ManualControlWidget._refreshPortNames(stub)
        assert combo.currentData() == 9


class TestUsageTable:
    """The run tab's per-port table: painted from the ledger's snapshot,
    names read fresh from the config at each paint, hidden when empty."""

    def _stub(self, rows):
        from qtpy.QtWidgets import QTableWidget
        table = QTableWidget(0, 3)
        stub = SimpleNamespace(
            usageTable=table,
            system=SimpleNamespace(
                usage=SimpleNamespace(rows=lambda: list(rows))),
        )
        return stub, table

    def test_totals_paint_with_fresh_names(self, qapp):
        stub, table = self._stub([(1, None, 500.0), (3, "DAPI", 1500.0)])
        gui.SequencesWidget._renderUsage(stub)
        assert not table.isHidden()
        assert table.rowCount() == 2
        rows = [(table.item(r, 0).text(), table.item(r, 1).text(),
                 table.item(r, 2).text()) for r in range(2)]
        assert rows == [("1", "", "500"), ("3", "DAPI", "1500")]

    def test_nothing_drawn_hides_the_table(self, qapp):
        stub, table = self._stub([])
        table.setVisible(True)
        gui.SequencesWidget._renderUsage(stub)
        assert table.isHidden()


class TestHeldVolumePaint:
    def test_the_bar_paints_the_published_reading(self, qapp):
        from qtpy.QtWidgets import QProgressBar
        bar = QProgressBar()
        bar.setRange(0, 5000)
        stub = SimpleNamespace(plungerPositionBar=bar)
        gui.ManualControlWidget._handle_held_volume(stub, 2600.0)
        assert bar.value() == 2600


class TestResumeOffer:
    """After an early end, RunEnded names the plan entry in flight; the
    offer prices the plan's tail and, on Yes, runs exactly it -- the
    checkboxes are never touched. Called unbound against stubs."""

    def _stub(self, monkeypatch, answer=gui.QMessageBox.No, plan=(),
              titled=None):
        asked = []

        def question(*args):
            asked.append(args[2])
            return answer

        monkeypatch.setattr(gui.QMessageBox, "question", question)
        started = []
        stub = SimpleNamespace(
            _plan=plan,
            # What the tree would say now, so a regression that asks the
            # model instead of the plan prints this and fails.
            _model=SimpleNamespace(title=lambda row: titled),
            system=SimpleNamespace(
                run=lambda seqs, plan=None: started.append(plan)),
            _beginRunDisplay=lambda count: started.append(("display", count)),
            asked=asked, started=started,
        )
        stub._startRun = lambda seqs, plan: gui.SequencesWidget._startRun(
            stub, seqs, plan)
        return stub

    def test_yes_runs_exactly_the_tail(self, monkeypatch):
        plan = plan_of([10.0, 20.0, 30.0], rows=[0, 2, 3])
        stub = self._stub(monkeypatch, answer=gui.QMessageBox.Yes, plan=plan)
        gui.SequencesWidget._offerResume(stub, 1)
        assert stub.started == [plan[1:], ("display", 2)], \
            "the run must be the interrupted plan's tail, nothing else"

    def test_no_runs_nothing(self, monkeypatch):
        stub = self._stub(monkeypatch, plan=plan_of([10.0, 20.0]))
        gui.SequencesWidget._offerResume(stub, 0)
        assert stub.started == []

    def test_the_offer_names_the_point_and_carries_the_bill(self, monkeypatch):
        """One dialog is both the resume point and the confirm: the entry's
        repeat k/n and the tail's own estimate are in the text."""
        from ..worker_helpers import plan_for
        plan = plan_for([{"type": "priming", "name": "prime", "repeat": 2}],
                        seconds=[45.0, 45.0])
        # The row was renamed mid-run: the offer must name what the plan
        # captured -- what will actually execute.
        stub = self._stub(monkeypatch, plan=plan, titled="renamed later")
        gui.SequencesWidget._offerResume(stub, 1)
        text = stub.asked[0]
        assert "prime" in text and "renamed later" not in text
        assert "repeat 2/2" in text
        assert "1 sequence(s) remain" in text
        assert "00:00:45" in text

    def test_a_finished_run_offers_nothing(self, monkeypatch):
        """RunEnded carries position=None when the run finished; there is
        nowhere to resume from and no question to ask."""
        monkeypatch.setattr(gui.QMessageBox, "information", lambda *args: None)
        offered = []
        stub = SimpleNamespace(_offerResume=lambda position: offered.append(position))
        gui.SequencesWidget._reportRunEnded(
            stub, RunEnded("run-1", "finished", None, 5.0, position=None))
        assert offered == []


class TestStaleStateNone:
    def test_a_stale_none_does_not_clear_a_run_already_started(self):
        """The resume offer starts the tail from inside the old run's
        RunEnded dialog chain; the old state(None) can land after it. The
        reset must apply only if the rig is still idle at delivery."""
        stopped = []
        stub = Quiet()
        stub.session = FakeSession(kind="run")     # the tail is already going
        stub.timer = SimpleNamespace(stop=lambda: stopped.append(True))
        stub._renderRunControls = lambda: None
        gui.SequencesWidget._handle_state(stub)
        assert stopped == [], "a stale state(None) stopped the new run's clock"

    def test_a_current_none_still_resets(self):
        stopped = []
        stub = Quiet()
        stub.session = FakeSession()               # idle: the reset is real
        stub.timer = SimpleNamespace(stop=lambda: stopped.append(True))
        stub._renderRunControls = lambda: None
        gui.SequencesWidget._handle_state(stub)
        assert stopped == [True]


class TestPostsToQtThread:
    def test_a_missing_handler_fails_where_it_was_posted(self, qapp):
        """The target is resolved on the producer's thread. A renamed
        handler must fail at the call site, not as an AttributeError
        inside event() on the Qt thread -- where, per run_log's note on
        sys.excepthook, PyQt can take the process with it."""
        from qtpy.QtCore import QObject
        from fluidics.qt.support import PostsToQtThread

        class Poster(PostsToQtThread, QObject):
            pass

        poster = Poster()
        with pytest.raises(AttributeError):
            poster._post_event("_a_handler_that_was_renamed")


class TestDetachOnDestroy:
    """The embedded widgets must remove exactly the callbacks they registered:
    Subscribers.unsubscribe deregisters by identity, and a fresh bound-method
    access is a different object every time."""

    def test_subscribe_until_detached_removes_exactly_what_it_registered(self):
        from fluidics.qt.support import subscribe_until_detached

        feeds = [Subscribers("warnings"), Subscribers("state"), Subscribers("events")]
        callbacks = [(lambda *a: None), (lambda *a: None), (lambda *a: None)]
        detach = subscribe_until_detached(*zip(feeds, callbacks))
        for feed, callback in zip(feeds, callbacks):
            assert feed._callbacks == [callback]
        detach()
        for feed in feeds:
            assert feed._callbacks == []
        detach()  # idempotent, like Subscribers.unsubscribe itself

    def test_widgets_connect_the_detach_to_destroyed(self):
        """Every widget that subscribes must hand it back. Source-read
        rather than driven, for the three whose feeds need a whole system
        to build; the sensor widgets pin the same invariant by behaviour
        in test_gui_flow_widget/test_gui_temperature_widget."""
        import inspect
        from fluidics.qt import manual_control, sensor_plots, sequence_editor

        for cls in (sequence_editor.SequencesWidget,
                    manual_control.ManualControlWidget,
                    sensor_plots.FlowSensorWidget,
                    sensor_plots.TemperatureControlWidget):
            src = inspect.getsource(cls.__init__)
            assert "subscribe_until_detached(" in src, cls.__name__
            assert "self.destroyed.connect(detach)" in src, cls.__name__
