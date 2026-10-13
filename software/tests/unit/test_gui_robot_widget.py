"""RobotControlWidget over a simulated system (spec §9): verbs as manual
jobs, the freeze on any job, teaching through the manual verbs, clicks that
carry no bool, the editable well picker, the reservoir table."""
import pytest
from qtpy.QtCore import QEvent
from qtpy.QtWidgets import QApplication

from fluidics.system import FluidicsSystem
from tests.conftest import wait_until


@pytest.fixture
def widget(qapp, plate_sipper_config, tmp_path):
    from fluidics.devices import build_devices
    from fluidics.qt.robot_control import RobotControlWidget
    devices = build_devices(plate_sipper_config, simulation=True, instant=True)
    for sensor in devices.flow_sensors:
        sensor.close()
    system = FluidicsSystem(plate_sipper_config, devices, report_dir=tmp_path)
    w = RobotControlWidget(plate_sipper_config, system)
    yield w, system
    # Destroyed, not only closed: its subscriptions come off the system's
    # channels on `destroyed`, so the close below posts into nothing.
    w.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert system.close() == []


def _settle(system):
    """The job over, its thread gone, every report it posted delivered.
    Untimed, as the suite waits on a session: under the fake clock a timed
    wait cannot block, and wait_until's budget is fake seconds, which other
    threads spend and a job's error path (a logged traceback) can outlast.
    The join matters too: the job's on_finished is posted after `busy`
    already reads False."""
    assert system.wait()
    QApplication.processEvents()


def test_go_to_well_runs_as_a_manual_job_and_paints_the_position(widget):
    w, system = widget
    w.reservoirCombo.setCurrentIndex(w.reservoirCombo.findData("ssct"))
    w.wellCombo.setCurrentText("A2")
    w.goToWell()
    _settle(system)
    assert system.devices.robot.current_well.well == "A2"
    assert w.positionLabel.text() == "x 102.50  y 10.50  z 52.00"


def test_the_well_picker_takes_the_typed_well_over_the_last_pick(widget):
    w, _ = widget
    w.wellCombo.setCurrentIndex(w.wellCombo.findData("A1"))
    w.wellCombo.setCurrentText("B3")
    assert w.wellCombo.value() == "B3"
    w.wellCombo.setCurrentIndex(w.wellCombo.findData("A1"))
    assert w.wellCombo.value() == "A1"          # the label "A1: hyb R01" resolves to the well


def test_controls_freeze_while_any_job_runs(widget, real_clock):
    """A job started elsewhere (the manual tab, a run) freezes this tab
    too: the session's state, not this tab's own verbs, is what disables.
    On the real clock: the fake one's Event.wait(5) returns at once."""
    w, system = widget
    import threading
    gate = threading.Event()
    system.run_manual(lambda: gate.wait(5))
    try:
        assert wait_until(lambda: system.busy)
        QApplication.processEvents()
        assert not w.goToWellButton.isEnabled() and not w.setA1Button.isEnabled()
        assert not w.stopButton.isEnabled(), "STOP belongs to the job's own tab"
    finally:
        gate.set()
    _settle(system)
    assert w.goToWellButton.isEnabled()


def test_stop_is_live_for_this_tab_s_own_job_and_stops_it(widget, monkeypatch, real_clock):
    """The one STOP: live while this tab's own move runs, and pressing it
    is the session's abort -- the move, waiting on the run's control as
    every device does, ends as a stop, not an error, and promptly."""
    import time
    w, system = widget
    stopped, errors = [], []
    monkeypatch.setattr(w, "operationStopped", lambda: stopped.append(True))
    monkeypatch.setattr(w, "handleError", errors.append)
    w._run(lambda: system.session.control.sleep(5))
    QApplication.processEvents()
    assert w.stopButton.isEnabled() and not w.goToWellButton.isEnabled()
    pressed = time.monotonic()
    w.stopButton.click()
    _settle(system)
    assert time.monotonic() - pressed < 2, "STOP did not stop the move"
    assert stopped == [True] and errors == []
    assert not w.stopButton.isEnabled() and w.goToWellButton.isEnabled()


HYB_FROM_A1 = {"type": "flow_reagent", "fluidic_port": 1, "well": "A1", "flow_rate": 2000, "volume": 100}


def _frozen(w):
    return not any(control.isEnabled() for control in
                   (w.goToWellButton, w.jogButtons[("x", 1.0)], w.setA1Button, w.plateReloadedButton))


def test_an_embedders_freeze_outlasts_its_fluidics_run(widget):
    """An embedding protocol freezes the tab for all of it, imaging too:
    the end of a fluidics run inside it repaints the tab, and the freeze
    stays (Codex finding 4)."""
    w, system = widget
    w.setControlsEnabled(False)
    system.run([HYB_FROM_A1])
    _settle(system)
    drawn = [w.reservoirTable.item(r, 0).text() for r in range(w.reservoirTable.rowCount())]
    assert drawn == ["probes/A1"], "the run's end repaints the tab"
    assert _frozen(w) and not w.stopButton.isEnabled()


def test_the_embedder_lifts_its_freeze_once_the_rig_is_idle(widget):
    w, system = widget
    w.setControlsEnabled(False)
    system.run([HYB_FROM_A1])
    _settle(system)
    assert _frozen(w)                  # the freeze held through the run's end
    w.setControlsEnabled(True)
    assert w.goToWellButton.isEnabled() and w.setA1Button.isEnabled() and w.plateReloadedButton.isEnabled()
    assert not w.stopButton.isEnabled()


@pytest.mark.parametrize("own", [False, True], ids=["another_tab_s_job", "this_tab_s_job"])
def test_the_embedder_cannot_unfreeze_a_running_job_and_stop_follows_the_session(widget, real_clock, own):
    """The embedder's switch gates the controls, never the session: while
    a job runs they stay frozen whatever it says, and STOP is live for
    this tab's own job whatever it says."""
    import threading
    w, system = widget
    gate = threading.Event()
    if own:
        w._run(lambda: gate.wait(5))
        # The start's own repaint, before the session's posted one lands.
        assert _frozen(w) and w.stopButton.isEnabled()
    else:
        system.run_manual(lambda: gate.wait(5))
    try:
        assert wait_until(lambda: system.busy)
        QApplication.processEvents()
        w.setControlsEnabled(False)
        assert _frozen(w) and w.stopButton.isEnabled() == own
        w.setControlsEnabled(True)
        assert _frozen(w) and w.stopButton.isEnabled() == own
    finally:
        gate.set()
    _settle(system)
    assert w.goToWellButton.isEnabled() and not w.stopButton.isEnabled()


PRIMING = {"type": "priming", "fluidic_port": 25, "flow_rate": 5000, "volume": 800}


def test_a_completed_priming_row_reads_primed(widget):
    w, system = widget
    system.run([PRIMING])
    _settle(system)
    assert w.primedLabel.text() == "primed this session: yes"


def test_an_aborted_priming_row_primes_nothing(widget, monkeypatch, real_clock):
    """Started is not done: a priming the operator aborted mid-way must not
    read as primed."""
    import threading
    w, system = widget
    started = threading.Event()

    def held_priming(sequence):
        started.set()
        system.session.control.sleep(5)          # aborted here

    monkeypatch.setattr(system.operations, "process_sequence", held_priming)
    system.run([PRIMING])
    assert started.wait(5)
    system.abort()
    _settle(system)
    assert w.primedLabel.text() == "primed this session: no"


@pytest.mark.parametrize("reservoir, typed, reference", [
    ("ssct", "A3", "ssct/A3"),          # bare: on the reservoir picked beside it
    ("probes", "ssct/A2", "ssct/A2"),   # qualified: as typed, whatever is picked
])
def test_go_to_well_takes_a_bare_or_a_qualified_well(widget, monkeypatch, reservoir, typed, reference):
    w, system = widget
    asked, said = [], []
    monkeypatch.setattr(w, "handleError", said.append)     # a refusal is a dialog: record it, never block
    go_to_well = system.manual.go_to_well
    monkeypatch.setattr(system.manual, "go_to_well", lambda text: asked.append(text) or go_to_well(text))
    w.reservoirCombo.setCurrentIndex(w.reservoirCombo.findData(reservoir))
    w.wellCombo.setCurrentText(typed)
    w.goToWell()
    _settle(system)
    assert said == [] and asked == [reference]
    well = system.devices.robot.current_well
    assert f"{well.reservoir}/{well.well}" == reference


def test_a_click_asks_before_reloading_a_plate(widget, monkeypatch):
    """QPushButton.clicked carries a bool; a slot that took it as its first
    argument would skip the confirmation (Codex finding 14)."""
    w, system = widget
    asked = []
    monkeypatch.setattr("fluidics.qt.robot_control._ask_yes_no", lambda *a, **k: asked.append(a) or False)
    system.devices.reservoirs._dispatched["probes/A1"] = 50
    w.plateReloadedButton.click()
    QApplication.processEvents()
    assert len(asked) == 1
    assert system.devices.reservoirs.remaining("probes/A1") == 55, "declined: nothing refilled"


def test_a_jog_click_moves_the_axis_it_says(widget):
    w, system = widget
    w.jogButtons[("z", 10.0)].click()
    _settle(system)
    assert system.devices.robot.position()[2] == 10.5      # from the stroke's start, 0.5


def test_teach_goes_through_the_manual_verbs_then_saves(widget, monkeypatch):
    w, system = widget
    saved = []
    monkeypatch.setattr("fluidics.qt.robot_control.save_config", lambda config: saved.append(config))
    w.teachPlateCombo.setCurrentIndex(w.teachPlateCombo.findData("probes"))
    w.jogButtons[("x", 1.0)].click()
    _settle(system)
    w.setA1Here()
    _settle(system)
    from fluidics.control.config import gantry
    assert tuple(gantry(w.config).reservoirs[1].a1_xy_mm) == (1.5, 0.5)     # homing clears to 0.5, then +1
    assert system.devices.robot.placements["probes"].a1_xy_mm == (1.5, 0.5)
    w.saveTeaching()
    assert saved == [w.config]


def test_a_refused_teach_is_a_dialog_and_changes_nothing(widget, monkeypatch):
    """Set travel height at Z 0 (where homing leaves it) is refused by the
    verb, on the job's thread; the refusal comes back as this tab's
    dialog, and the config keeps its travel height."""
    w, system = widget
    said = []
    monkeypatch.setattr("fluidics.qt.robot_control.QMessageBox.critical",
                        lambda parent, title, text: said.append(text))
    from fluidics.control.config import gantry
    before = gantry(w.config).travel_z_mm
    w.setTravelButton.click()
    _settle(system)
    assert said and "travel height" in said[0]
    assert gantry(w.config).travel_z_mm == before


def test_the_wells_table_shows_what_was_drawn_and_a_reload_clears_it(widget, monkeypatch):
    w, system = widget
    monkeypatch.setattr("fluidics.qt.robot_control._ask_yes_no", lambda *a, **k: True)
    system.devices.reservoirs._dispatched["ssct/A1"] = 1000
    w.refreshReservoirs()
    rows = {w.reservoirTable.item(r, 0).text(): w.reservoirTable.item(r, 2).text()
            for r in range(w.reservoirTable.rowCount())}
    assert rows["ssct/A1"] == "750"
    w.reservoirCombo.setCurrentIndex(w.reservoirCombo.findData("ssct"))
    w.plateReloaded()
    _settle(system)
    assert system.devices.reservoirs.remaining("ssct/A1") == 1750
    assert w.reservoirTable.rowCount() == 0, "the reload's finish repaints the table"


def test_the_reservoir_table_shows_its_whole_headers(widget):
    """"Dispatched (µL)" and "Remaining (µL)" read "ispatched (µ" at the
    default column width: the numeric columns size to their contents and
    the name column takes the rest, as the editor's usage table does."""
    from qtpy.QtWidgets import QHeaderView
    w, _ = widget
    header = w.reservoirTable.horizontalHeader()
    assert header.sectionResizeMode(0) == QHeaderView.Stretch
    assert header.sectionResizeMode(1) == header.sectionResizeMode(2) == QHeaderView.ResizeToContents


def test_the_map_judges_the_published_position_not_the_live_stage(widget):
    """The paint lands on the Qt thread after the publish, when the next leg
    may already be under way and the stage's own position is unknown: with
    "Do not lower tip" on, a retract followed by the travel flashed red. The
    published Z decides."""
    w, system = widget
    stage = system.devices.robot._stage
    stage._position = None                     # a leg in flight
    w._paintPosition((102.5, 10.5, 1.0))
    assert w.deckMap.stateText().startswith("sipper: retracted over ssct/A2")
    w._paintPosition((102.5, 10.5, 52.0))
    assert w.deckMap.stateText().startswith("sipper: lowered")


def test_an_unknown_position_is_painted_as_such(widget):
    w, _ = widget
    w._paintPosition(None)
    assert "unknown" in w.positionLabel.text()
    assert w.deckMap.stateText().startswith("sipper: position unknown")


def test_the_deck_map_follows_the_sipper(widget):
    """Lowered in the well after a go-to-well, with the well ringed; retracted
    with no well after a retract -- from the stage's publish, not a poll."""
    from fluidics.robot.labware import WellRef
    w, system = widget
    w.reservoirCombo.setCurrentIndex(w.reservoirCombo.findData("ssct"))
    w.wellCombo.setCurrentText("A2")
    w.goToWell()
    _settle(system)
    assert w.deckMap.currentWell == WellRef("ssct", "A2")
    assert w.deckMap.stateText() == "sipper: lowered in ssct/A2   x 102.5  y 10.5  z 52.0 mm"
    w._run(w.manual.retract)
    _settle(system)
    assert w.deckMap.currentWell is None
    assert w.deckMap.stateText() == "sipper: retracted over ssct/A2   x 102.5  y 10.5  z 1.0 mm"


def test_a_click_on_a_well_of_the_map_goes_there_as_a_manual_job(widget):
    from qtpy.QtCore import QPoint, Qt
    from qtpy.QtTest import QTest
    w, system = widget
    w.deckMap.resize(640, 360)
    placement = system.devices.robot.placements["ssct"]
    px, py = w.deckMap.pixel(*placement.xy_mm("B3"))
    QTest.mouseClick(w.deckMap, Qt.LeftButton, pos=QPoint(int(round(px)), int(round(py))))
    _settle(system)
    assert system.devices.robot.current_well.well == "B3"
    assert system.devices.robot.current_well.reservoir == "ssct"
    assert w.reservoirCombo.currentData() == "ssct" and w.wellCombo.value() == "B3"


def test_the_map_shows_the_named_wells_and_freezes_with_the_controls(widget):
    w, _ = widget
    assert w.deckMap.legend() == ["clean-up well", "priming well"]
    w.setControlsEnabled(False)
    assert not w.deckMap.isEnabled()
    w.setControlsEnabled(True)
    assert w.deckMap.isEnabled()


def test_do_not_lower_tip_stops_go_to_well_over_the_well_and_refuses_a_run(widget):
    """Checked, Go to well (button or map) retracts and travels, never dips:
    no witness, the map says "retracted over", and a run is refused at
    time zero. The box is one of the controls, so it freezes under a job."""
    w, system = widget
    robot = system.devices.robot
    w.lowerTipBox.setChecked(True)
    assert robot.lower_tip is False
    w.reservoirCombo.setCurrentIndex(w.reservoirCombo.findData("ssct"))
    w.wellCombo.setCurrentText("A2")
    w.goToWell()
    _settle(system)
    assert robot.current_well is None and robot.position() == (102.5, 10.5, 1.0)
    assert w.deckMap.stateText() == "sipper: retracted over ssct/A2   x 102.5  y 10.5  z 1.0 mm"
    from fluidics.errors import OperationError
    with pytest.raises(OperationError, match="Do not lower tip"):
        system.run([HYB_FROM_A1])
    assert not system.busy
    w.lowerTipBox.setChecked(False)
    assert robot.lower_tip is True
    w.setControlsEnabled(False)
    assert not w.lowerTipBox.isEnabled()


def test_teaching_and_jog_live_in_their_own_window_and_the_map_on_the_tab(widget):
    """The tab is the run-time view (map, go to well, reservoirs); jog and
    teaching are a window the operator opens from the Stage row and closes.
    Opening it is allowed while a job runs (not a move); its controls
    freeze like every other."""
    w, _ = widget
    assert w.deckMap.window() is w.window()
    assert w.teachWindow.isVisible() is False
    for control in (w.jogButtons[("x", 1.0)], w.setA1Button, w.saveButton, w.teachPlateCombo):
        assert control.window() is w.teachWindow
    w.teachButton.click()
    assert w.teachWindow.isVisible() and w.teachWindow.windowTitle() == "Teach"
    w.teachWindow.close()
    assert w.teachWindow.isVisible() is False
    w.setControlsEnabled(False)
    assert w.teachButton.isEnabled() and _frozen(w) and not w.teachPlateCombo.isEnabled()


def test_teaching_moves_the_drawn_plate(widget, monkeypatch):
    """The map re-reads the live placements when the teach job ends."""
    w, system = widget
    monkeypatch.setattr("fluidics.qt.robot_control._ask_yes_no", lambda *a, **k: True)
    before = w.deckMap.pixel(*system.devices.robot.placements["probes"].xy_mm("A1"))
    w.teachPlateCombo.setCurrentIndex(w.teachPlateCombo.findData("probes"))
    w._run(w.manual.home_robot)
    _settle(system)
    w._run(lambda: w.manual.jog("x", 10.0))
    _settle(system)
    w.setA1Here()
    _settle(system)
    after = w.deckMap.pixel(*system.devices.robot.placements["probes"].xy_mm("A1"))
    assert after[0] < before[0], "A1 taught 2 mm left of where it was moves the drawn plate"


def test_one_job_on_the_rig_greys_the_other_tabs(qapp, plate_sipper_config, tmp_path, monkeypatch,
                                                  real_clock):
    """The real window: a Robot-tab move greys the run and manual tabs; a
    manual-tab move greys the run and Robot tabs; all come back after."""
    import threading
    import gui
    from fluidics.devices import build_devices
    devices = build_devices(plate_sipper_config, simulation=True, instant=True)
    for sensor in devices.flow_sensors:
        sensor.close()
    system = FluidicsSystem(plate_sipper_config, devices, report_dir=tmp_path)
    monkeypatch.setattr(gui.FluidicsSystem, "build", lambda *args, **kwargs: system)
    window = gui.FluidicsControlGUI(plate_sipper_config, True)
    tabs = window.tabWidget

    def enabled():
        return {name: tabs.isTabEnabled(index) for name, index in
                (("run", window.RUN_TAB), ("manual", window.MANUAL_TAB), ("robot", window.ROBOT_TAB))}

    try:
        for tab, expected in ((window.ROBOT_TAB, {"run": False, "manual": False, "robot": True}),
                              (window.MANUAL_TAB, {"run": False, "manual": True, "robot": False})):
            gate = threading.Event()
            tabs.widget(tab)._run(lambda: gate.wait(5))
            try:
                QApplication.processEvents()
                assert enabled() == expected
            finally:
                gate.set()
            _settle(system)
            assert enabled() == {"run": True, "manual": True, "robot": True}
    finally:
        window.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        assert system.close() == []


@pytest.mark.parametrize("config_name, has_robot", [("plate_sipper_config", True),
                                                    ("flow_cell_config", False)])
def test_the_main_window_adds_a_robot_tab_only_with_a_gantry(qapp, request, tmp_path, monkeypatch,
                                                              config_name, has_robot):
    """The tab is built when the gantry block is configured (spec §9), the
    way the temperature and flow tabs are; D8: none without it."""
    import gui
    from fluidics.devices import build_devices
    config = request.getfixturevalue(config_name)
    devices = build_devices(config, simulation=True, instant=True)
    for sensor in devices.flow_sensors:
        sensor.close()
    system = FluidicsSystem(config, devices, report_dir=tmp_path)
    monkeypatch.setattr(gui.FluidicsSystem, "build", lambda *args, **kwargs: system)
    window = gui.FluidicsControlGUI(config, True)
    try:
        tabs = [window.tabWidget.tabText(i) for i in range(window.tabWidget.count())]
        assert ("Robot" in tabs) == has_robot
        assert window.ROBOT_TAB == (tabs.index("Robot") if has_robot else None)
    finally:
        window.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        assert system.close() == []
