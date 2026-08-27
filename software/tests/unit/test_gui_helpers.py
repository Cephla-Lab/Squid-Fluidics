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


def _bind(name, stub, cls=None):
    """An unbound widget method, bound to a stub."""
    cls = cls or gui.SequencesWidget
    return lambda *args: getattr(cls, name)(stub, *args)
from fluidics.flow_monitor import FlowFault


class Button:
    """A QPushButton's text and enabled state, as the widgets use them."""

    def __init__(self, text="Pause"):
        self._text = text
        self.enabled = None

    def text(self):
        return self._text

    def setText(self, text):
        self._text = text

    def setEnabled(self, enabled):
        self.enabled = enabled


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

    def __init__(self, kind=None, paused=False, at_rest=False, cancelled=False):
        self.kind = kind
        self.paused = paused
        self.at_rest = at_rest
        self.cancelled = cancelled
        self.calls = []

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

    class Stub:
        def __init__(self):
            self.record_btn = Button("Start Recording")
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
    """One signal: the button cancels through the session, which every waiting
    device and the worker share. The cancel also releases a run already held,
    so Abort needs no resume first. Called unbound against a stub."""

    def test_abort_goes_through_the_session_and_kills_the_controls(self):
        stub = SimpleNamespace(session=FakeSession(kind="run"),
                               runButton=Button(), pauseButton=Button(), abortButton=Button())
        stub._renderRunControls = _bind("_renderRunControls", stub)
        gui.SequencesWidget.abortSequences(stub)
        assert stub.session.calls == ["abort"]
        # The run is over; there is nothing left to hold or to abort.
        assert stub.pauseButton.enabled is False
        assert stub.abortButton.enabled is False

    def test_abort_does_nothing_when_no_run_is_the_job(self):
        stub = SimpleNamespace(session=FakeSession(kind="manual"))
        gui.SequencesWidget.abortSequences(stub)
        assert stub.session.calls == []


class TestRunFinished:
    """Called unbound against a stub."""

    def _finishing(self, monkeypatch):
        order = []
        monkeypatch.setattr(gui.QMessageBox, "information",
                            lambda *args: order.append(("info", args[1])))
        monkeypatch.setattr(gui.QMessageBox, "critical",
                            lambda *args: order.append(("error", args[1])))
        stub = Quiet()
        stub._ended_early = False
        stub._renderRunControls = lambda: order.append("render")
        return stub, order

    def test_a_completed_run_redraws_the_controls_then_says_finished(self, monkeypatch):
        stub, order = self._finishing(monkeypatch)
        gui.SequencesWidget._handle_finished(stub)
        assert order == ["render", ("info", "Finished")]

    def test_a_stopped_run_says_stopped_once_not_finished(self, monkeypatch):
        """The operator pressed Abort: one dialog saying so, not an Error
        followed by a Finished."""
        stub, order = self._finishing(monkeypatch)
        gui.SequencesWidget._handle_stopped(stub)
        gui.SequencesWidget._handle_finished(stub)
        assert order == [("info", "Stopped"), "render"]

    def test_a_failed_run_says_why_once(self, monkeypatch):
        stub, order = self._finishing(monkeypatch)
        gui.SequencesWidget._handle_error(stub, "pump fault")
        gui.SequencesWidget._handle_finished(stub)
        assert order == [("error", "Error"), "render"]


class TestPauseControls:
    """The button and the label, called unbound against stubs -- constructing
    SequencesWidget needs a QApplication.

    The two pause moments are the point: `paused` is "someone asked",
    `at_rest` is "the run has actually stopped". Between them a move is still
    finishing, and the operator must be able to tell -- "pausing" means liquid
    may still be moving.
    """

    def widget(self, paused=False, at_rest=False, total_time=100, elapsed=0,
               worker=True):
        session = FakeSession(kind="run" if worker else None, paused=paused, at_rest=at_rest)
        stub = SimpleNamespace(
            session=session,
            runButton=Button(), pauseButton=Button(), abortButton=Button(),
            timeLabel=SimpleNamespace(setText=lambda text: None),
            progressBar=SimpleNamespace(setValue=lambda value: None),
            timer=SimpleNamespace(stop=lambda: None),
            total_time=total_time,
            elapsed_time=elapsed,
            calls=session.calls,
        )
        # The widget's methods call back into self, so an unbound call needs
        # them on the stub too.
        for name in ("_runState", "_showTimeRemaining", "_renderRunControls"):
            stub.__dict__[name] = _bind(name, stub)
        # A static method needs no stub of its own.
        stub.__dict__["_pauseSuffix"] = gui.SequencesWidget._pauseSuffix
        return stub

    @pytest.mark.parametrize("paused, at_rest, suffix, spends_time", [
        (False, False, "", True),
        (True, False, " (pausing\u2026)", True),    # the move in flight still counts
        (True, True, " (paused)", False),           # stopped: no time passes
        # (False, True) cannot arise: RunControl.at_rest is paused *and*
        # something parked, pinned in test_errors.py.
    ])
    def test_the_two_moments_and_what_each_costs(self, paused, at_rest, suffix,
                                                 spends_time):
        shown = []
        stub = self.widget(paused=paused, at_rest=at_rest)
        stub.timeLabel = SimpleNamespace(setText=shown.append)
        assert gui.SequencesWidget._pauseSuffix(paused, at_rest) == suffix
        gui.SequencesWidget.updateTimeRemaining(stub)
        assert stub.elapsed_time == (1 if spends_time else 0)
        assert shown == [f"00:01:{40 - int(spends_time):02d} remaining{suffix}"]

    def test_the_state_is_read_once_a_tick(self):
        """The clock decision and the label it prints must come from the same
        instant: another thread owns these, and a tick that read twice could
        decline to charge a second and then print "(pausing...)"."""
        reads = []

        class Control:
            kind, busy, cancelled = "run", True, False

            @property
            def paused(self):
                reads.append("paused")
                return True

            @property
            def at_rest(self):
                reads.append("at_rest")
                return True

        stub = self.widget()
        stub.session = Control()
        gui.SequencesWidget.updateTimeRemaining(stub)
        assert reads.count("at_rest") == 1, reads

    def test_the_first_press_pauses_and_offers_a_resume(self):
        stub = self.widget()
        gui.SequencesWidget.pauseSequences(stub)
        assert stub.calls == ["pause"]
        assert stub.pauseButton.text() == "Resume"

    def test_the_next_press_resumes(self):
        stub = self.widget(paused=True)
        gui.SequencesWidget.pauseSequences(stub)
        assert stub.calls == ["resume"]
        assert stub.pauseButton.text() == "Pause"

    def test_a_press_costs_the_estimate_nothing(self):
        """The press refreshes the label; going through the one-second tick to
        do that would charge a second of the run for every click."""
        shown = []
        stub = self.widget(elapsed=10)
        stub.timeLabel = SimpleNamespace(setText=shown.append)
        gui.SequencesWidget.pauseSequences(stub)
        assert stub.elapsed_time == 10
        assert shown == ["00:01:30 remaining (pausing\u2026)"]

    def test_a_press_before_the_estimate_arrives_does_not_raise(self):
        """The worker posts the estimate to the event queue, so an operator
        who presses Pause within the first second finds total_time unset. A
        slot that raises there takes the whole GUI down with it."""
        stub = self.widget(total_time=None)
        gui.SequencesWidget.pauseSequences(stub)
        assert stub.calls == ["pause"]
        assert stub.pauseButton.text() == "Resume"

    def test_the_tick_keeps_coming_while_held_even_at_zero_remaining(self):
        """Otherwise the label freezes on "pausing" and never says the run has
        actually stopped."""
        stopped = []
        shown = []
        stub = self.widget(paused=True, at_rest=True, total_time=1, elapsed=99)
        stub.timer = SimpleNamespace(stop=lambda: stopped.append(True))
        stub.timeLabel = SimpleNamespace(setText=shown.append)
        gui.SequencesWidget.updateTimeRemaining(stub)
        assert stopped == []
        assert shown == ["00:00:00 remaining (paused)"]

    def test_a_run_that_ends_on_its_own_takes_the_controls_with_it(self):
        """A flow fault cancels from the MCU reader thread, so no button press
        and no callback runs: the tick is what notices."""
        stub = self.widget()
        stub.session.cancelled = True
        gui.SequencesWidget.updateTimeRemaining(stub)
        assert stub.pauseButton.enabled is False
        assert stub.abortButton.enabled is False

    def test_a_finished_run_puts_the_button_back(self, monkeypatch):
        monkeypatch.setattr(gui.QMessageBox, "information", lambda *args: None)
        stub = Quiet()
        stub.session = FakeSession()
        stub.pauseButton = Button("Resume")
        stub.runButton = Button()
        stub.abortButton = Button()
        stub._renderRunControls = _bind("_renderRunControls", stub)
        gui.SequencesWidget._handle_finished(stub)
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
            valveCombo=SimpleNamespace(currentIndex=lambda: 2),
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

    def test_the_other_tab_goes_dead_for_the_length_of_the_job(self):
        enabled = {}
        stub = SimpleNamespace(RUN_TAB=0, MANUAL_TAB=1,
                               tabWidget=SimpleNamespace(setTabEnabled=enabled.__setitem__))
        gui.FluidicsControlGUI._renderTabs(stub, "run")
        assert enabled == {0: True, 1: False}
        gui.FluidicsControlGUI._renderTabs(stub, "manual")
        assert enabled == {0: False, 1: True}
        gui.FluidicsControlGUI._renderTabs(stub, None)
        assert enabled == {0: True, 1: True}

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
