"""RobotControlWidget over a simulated system (spec §9): verbs as manual
jobs, the freeze on any job, teaching through the manual verbs (the
dedicated wells among them), clicks that carry no bool, the editable well
picker, the deck map."""
import logging
import shutil
import threading
import time

import pytest
from qtpy.QtCore import QEvent, Qt
from qtpy.QtTest import QTest
from qtpy.QtWidgets import QApplication

import gui
from fluidics.control.config import gantry, load_config
from fluidics.errors import OperationError
from fluidics.qt.deck_map import ROLE_COLORS
from fluidics.qt.robot_control import RobotControlWidget
from fluidics.robot.labware import WellRef, well_key

from ..conftest import FIXTURES_DIR, HYB, PRIME, settle
from .test_gui_deck_map import _at


@pytest.fixture
def widget(qapp, plate_sipper_config, simulated_system):
    system = simulated_system(plate_sipper_config)
    w = RobotControlWidget(plate_sipper_config, system)
    yield w, system
    # Destroyed, not only closed: its subscriptions come off the system's
    # channels on `destroyed`, so the system's close (simulated_system's
    # teardown, after this) posts into nothing.
    w.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)


@pytest.fixture
def main_window(qapp, simulated_system, monkeypatch):
    """The real window over a simulated system for `config`: (window,
    system), gui's build patched to hand that system back. Destroyed at
    teardown, before the system closes, as the widget above is."""
    windows = []

    def _open(config):
        system = simulated_system(config)
        monkeypatch.setattr(gui.FluidicsSystem, "build", lambda *args, **kwargs: system)
        window = gui.FluidicsControlGUI(config, True)
        windows.append(window)
        return window, system

    yield _open
    for window in windows:
        window.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def _go_to_well(w, system, reservoir, well):
    """The Wells row as the operator works it: pick the reservoir, type
    (or pick) the well, Go to well; the move settled."""
    w.reservoirCombo.setCurrentIndex(w.reservoirCombo.findData(reservoir))
    w.wellCombo.setCurrentText(well)
    w.goToWell()
    settle(system)


def _click_well(w, system, reservoir, well):
    """A click on the map at the well's centre; the move it starts settled."""
    w.deckMap.resize(640, 360)
    QTest.mouseClick(w.deckMap, Qt.LeftButton,
                     pos=_at(w.deckMap, system.devices.robot.placements[reservoir], well))
    settle(system)


def test_go_to_well_runs_as_a_manual_job_and_paints_the_position(widget):
    w, system = widget
    _go_to_well(w, system, "ssct", "A2")
    assert system.devices.robot.current_well.well == "A2"
    assert w.positionLabel.text() == "x 102.50  y 10.50  z 52.00"


def test_home_through_the_tab_lifts_the_tip_and_repaints_the_position(widget):
    """Home is a manual job like the rest: the tip comes up out of its well
    (no witness, retracted) and the label follows the stage's publish."""
    w, system = widget
    _go_to_well(w, system, "ssct", "A2")
    robot = system.devices.robot
    assert not robot.is_retracted
    heard = []
    system.session.state.subscribe(heard.append)
    w.homeButton.click()
    settle(system)
    assert heard == ["manual", None], "a manual job on the session, like every verb of this tab"
    assert robot.current_well is None and robot.is_retracted
    assert w.positionLabel.text() == "x 0.50  y 0.50  z 0.50"


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
    gate = threading.Event()
    system.run_manual(lambda: gate.wait(5))
    try:
        assert system.busy
        QApplication.processEvents()
        assert not w.goToWellButton.isEnabled() and not w.setA1Button.isEnabled()
        assert not w.stopButton.isEnabled(), "STOP belongs to the job's own tab"
    finally:
        gate.set()
    settle(system)
    assert w.goToWellButton.isEnabled()


def test_stop_is_live_for_this_tab_s_own_job_and_stops_it(widget, monkeypatch, real_clock):
    """The one STOP: live while this tab's own move runs, and pressing it
    is the session's abort -- the move, waiting on the run's control as
    every device does, ends as a stop, not an error, and promptly."""
    w, system = widget
    stopped, errors = [], []
    monkeypatch.setattr(w, "operationStopped", lambda: stopped.append(True))
    monkeypatch.setattr(w, "handleError", errors.append)
    w._run(lambda: system.session.control.sleep(5))
    QApplication.processEvents()
    assert w.stopButton.isEnabled() and not w.goToWellButton.isEnabled()
    pressed = time.monotonic()
    w.stopButton.click()
    settle(system)
    assert time.monotonic() - pressed < 2, "STOP did not stop the move"
    assert stopped == [True] and errors == []
    assert not w.stopButton.isEnabled() and w.goToWellButton.isEnabled()


def _frozen(w):
    return not any(control.isEnabled() for control in
                   (w.goToWellButton, w.jogButtons[("x", 1.0)], w.setA1Button))


def test_an_embedders_freeze_outlasts_its_fluidics_run(widget):
    """An embedding protocol freezes the tab for all of it, imaging too:
    the end of a fluidics run inside it repaints the tab, and the freeze
    stays (Codex finding 4)."""
    w, system = widget
    w.setControlsEnabled(False)
    system.run([HYB])
    settle(system)
    assert w.deckMap.currentWell == WellRef("probes", "A1"), "the run's end repaints the tab"
    assert _frozen(w) and not w.stopButton.isEnabled()


def test_the_embedder_lifts_its_freeze_once_the_rig_is_idle(widget):
    w, system = widget
    w.setControlsEnabled(False)
    system.run([HYB])
    settle(system)
    assert _frozen(w)                  # the freeze held through the run's end
    w.setControlsEnabled(True)
    assert w.goToWellButton.isEnabled() and w.setA1Button.isEnabled()
    assert not w.stopButton.isEnabled()


@pytest.mark.parametrize("own", [False, True], ids=["another_tab_s_job", "this_tab_s_job"])
def test_the_embedder_cannot_unfreeze_a_running_job_and_stop_follows_the_session(widget, real_clock, own):
    """The embedder's switch gates the controls, never the session: while
    a job runs they stay frozen whatever it says, and STOP is live for
    this tab's own job whatever it says."""
    w, system = widget
    gate = threading.Event()
    if own:
        w._run(lambda: gate.wait(5))
        # The start's own repaint, before the session's posted one lands.
        assert _frozen(w) and w.stopButton.isEnabled()
    else:
        system.run_manual(lambda: gate.wait(5))
    try:
        assert system.busy
        QApplication.processEvents()
        w.setControlsEnabled(False)
        assert _frozen(w) and w.stopButton.isEnabled() == own
        w.setControlsEnabled(True)
        assert _frozen(w) and w.stopButton.isEnabled() == own
    finally:
        gate.set()
    settle(system)
    assert w.goToWellButton.isEnabled() and not w.stopButton.isEnabled()


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
    _go_to_well(w, system, reservoir, typed)
    assert said == [] and asked == [reference]
    assert well_key(system.devices.robot.current_well) == reference


def test_a_jog_click_moves_the_axis_it_says(widget):
    w, system = widget
    w.jogButtons[("z", 10.0)].click()
    settle(system)
    assert system.devices.robot.position()[2] == 10.5      # from the stroke's start, 0.5


def test_teach_goes_through_the_manual_verbs_then_saves(widget, monkeypatch):
    w, system = widget
    saved = []
    monkeypatch.setattr("fluidics.qt.robot_control.save_config", lambda config: saved.append(config))
    w.teachPlateCombo.setCurrentIndex(w.teachPlateCombo.findData("probes"))
    w.jogButtons[("x", 1.0)].click()
    settle(system)
    w.setA1Here()
    settle(system)
    assert tuple(gantry(w.config).reservoirs[1].a1_xy_mm) == (1.5, 0.5)     # homing clears to 0.5, then +1
    assert system.devices.robot.placements["probes"].a1_xy_mm == (1.5, 0.5)
    w.saveTeaching()
    assert saved == [w.config]


def test_a_refused_teach_is_a_dialog_and_changes_nothing(widget, monkeypatch):
    """Set travel height at Z 0.5 (where homing leaves it) is refused by the
    verb, on the job's thread; the refusal comes back as this tab's
    dialog, and the config keeps its travel height."""
    w, system = widget
    said = []
    monkeypatch.setattr("fluidics.qt.robot_control.QMessageBox.critical",
                        lambda parent, title, text: said.append(text))
    before = gantry(w.config).travel_z_mm
    w.setTravelButton.click()
    settle(system)
    assert said and "travel height" in said[0]
    assert gantry(w.config).travel_z_mm == before


def _set_dedicated(w, system, role, reservoir, well):
    """A dedicated well's row as the operator works it: pick the
    reservoir, type (or pick) the well, Set; the job settled."""
    reservoirs, wells, button = w.dedicatedWells[role]
    reservoirs.setCurrentIndex(reservoirs.findData(reservoir))
    wells.setCurrentText(well)
    button.click()
    settle(system)


def _fill_at(w, reservoir, well):
    """The colour the deck map paints at a well's centre."""
    w.deckMap.resize(640, 360)
    placement = w.system.devices.robot.placements[reservoir]
    return w.deckMap.grab().toImage().pixelColor(_at(w.deckMap, placement, well))


def test_the_dedicated_wells_are_offered_in_the_teach_window_prefilled_from_the_config(widget):
    w, _ = widget
    for role, well in (("priming", "H11"), ("clean-up", "H12")):
        reservoirs, wells, button = w.dedicatedWells[role]
        assert (reservoirs.currentData(), wells.value()) == ("ssct", well)
        assert {control.window() for control in (reservoirs, wells, button)} == {w.teachWindow}
    w.setControlsEnabled(False)
    assert not any(control.isEnabled() for control in w.dedicatedWells["priming"])


def test_set_names_a_dedicated_well_in_the_config_and_the_map_marks_it(widget, caplog):
    """The verb writes the config's value as reservoir/well -- a bare well
    on the reservoir picked beside it, a qualified one as typed -- and the
    job's end repaints the deck map's marks."""
    w, system = widget
    assert _fill_at(w, "ssct", "C3") != ROLE_COLORS["priming"]
    with caplog.at_level(logging.INFO, logger="fluidics.manual_operations"):
        _set_dedicated(w, system, "priming", "ssct", "C3")
    assert gantry(w.config).priming_well == "ssct/C3"
    assert "Taught: priming well ssct/C3." in caplog.text
    assert _fill_at(w, "ssct", "C3") == ROLE_COLORS["priming"]
    assert _fill_at(w, "ssct", "H11") != ROLE_COLORS["priming"], "the old mark goes"
    _set_dedicated(w, system, "clean-up", "probes", "ssct/C3")
    assert gantry(w.config).clean_up_well == "ssct/C3"
    assert w.deckMap.legend() == ["priming + clean-up well"]


@pytest.mark.parametrize("reservoir, typed, refusal", [
    ("ssct", "Z9", "not a well"),
    ("ssct", "nope/A1", "no reservoir named 'nope'"),
    # 110 uL loaded, against the sipper's 250 uL line.
    ("probes", "C3", "probes/C3 holds 110 uL when loaded, less than the 250 uL"),
])
def test_a_refused_dedicated_well_is_a_dialog_and_changes_nothing(widget, monkeypatch, reservoir, typed, refusal):
    w, system = widget
    said = []
    monkeypatch.setattr("fluidics.qt.robot_control.QMessageBox.critical",
                        lambda parent, title, text: said.append(text))
    _set_dedicated(w, system, "priming", reservoir, typed)
    assert len(said) == 1 and refusal in said[0]
    assert gantry(w.config).priming_well == "ssct/H11"
    assert w.deckMap.legend() == ["clean-up well", "priming well"]


def test_the_next_priming_row_primes_the_sipper_from_the_well_set(widget):
    """The operations read the gantry block at the row, so a well set
    between runs is the one drawn from."""
    w, system = widget
    _set_dedicated(w, system, "priming", "ssct", "H10")
    system.run([PRIME])
    settle(system)
    tracker = system.devices.reservoirs
    assert tracker.remaining("ssct/H10") < 1800, "the sipper's port primed from the well set"
    assert tracker.remaining("ssct/H11") == 1800


def test_save_writes_the_dedicated_wells_to_the_config_file(qapp, simulated_system, tmp_path):
    """Save, the teach window's one write, carries the dedicated wells
    with the rest of the teaching: read back from the file."""
    path = tmp_path / "plate_sipper_config.yaml"
    shutil.copy(FIXTURES_DIR / "plate_sipper_config.yaml", path)
    config = load_config(str(path))
    system = simulated_system(config)
    w = RobotControlWidget(config, system)
    try:
        _set_dedicated(w, system, "priming", "ssct", "H10")
        _set_dedicated(w, system, "clean-up", "ssct", "G12")
        w.saveTeaching()
    finally:
        w.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    saved = gantry(load_config(str(path)))
    assert (saved.priming_well, saved.clean_up_well) == ("ssct/H10", "ssct/G12")


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
    w, system = widget
    _go_to_well(w, system, "ssct", "A2")
    assert w.deckMap.currentWell == WellRef("ssct", "A2")
    assert w.deckMap.stateText() == "sipper: lowered in ssct/A2   x 102.5  y 10.5  z 52.0 mm"
    w._run(w.manual.retract)
    settle(system)
    assert w.deckMap.currentWell is None
    assert w.deckMap.stateText() == "sipper: retracted over ssct/A2   x 102.5  y 10.5  z 1.0 mm"


def test_a_click_on_a_well_of_the_map_goes_there_as_a_manual_job(widget):
    w, system = widget
    _click_well(w, system, "ssct", "B3")
    assert system.devices.robot.current_well == WellRef("ssct", "B3")
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
    _go_to_well(w, system, "ssct", "A2")
    assert robot.current_well is None and robot.position() == (102.5, 10.5, 1.0)
    assert w.deckMap.stateText() == "sipper: retracted over ssct/A2   x 102.5  y 10.5  z 1.0 mm"
    with pytest.raises(OperationError, match="Do not lower tip"):
        system.run([HYB])
    assert not system.busy
    w.lowerTipBox.setChecked(False)
    assert robot.lower_tip is True
    w.setControlsEnabled(False)
    assert not w.lowerTipBox.isEnabled()


def test_teaching_and_jog_live_in_their_own_window_and_the_map_on_the_tab(widget):
    """The tab is the run-time view (map, go to well); jog and
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


def test_the_teach_plates_picker_follows_the_wells_picker_and_a_map_click(widget, monkeypatch):
    """Visit a well on ssct (a map click), open Teach…, jog, Set A1 here: it
    is ssct's A1 that is taught, not the first plate in the list."""
    w, system = widget
    said = []
    monkeypatch.setattr(w, "handleError", said.append)     # a refusal is a dialog: record it, never block
    assert w.teachPlateCombo.currentData() == w.reservoirCombo.currentData() == "probes"
    _click_well(w, system, "ssct", "A1")
    assert w.teachPlateCombo.currentData() == "ssct"
    w._run(w.manual.retract)
    settle(system)
    w._run(lambda: w.manual.jog("x", 1.0))
    settle(system)
    probes_before = system.devices.robot.placements["probes"].a1_xy_mm
    w.setA1Here()
    settle(system)
    assert said == []
    assert system.devices.robot.placements["ssct"].a1_xy_mm == system.devices.robot.position()[:2]
    assert system.devices.robot.placements["probes"].a1_xy_mm == probes_before


def test_the_do_not_lower_tip_box_shows_the_robots_state_when_built(widget):
    w, system = widget
    system.devices.robot.lower_tip = False
    second = RobotControlWidget(w.config, system)
    try:
        assert second.lowerTipBox.isChecked()
    finally:
        second.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    system.devices.robot.lower_tip = True


def test_teaching_moves_the_drawn_plate(widget):
    """The map re-reads the live placements when the teach job ends."""
    w, system = widget
    before = w.deckMap.pixel(*system.devices.robot.placements["probes"].xy_mm("A1"))
    w.teachPlateCombo.setCurrentIndex(w.teachPlateCombo.findData("probes"))
    w._run(lambda: w.manual.jog("x", 10.0))
    settle(system)
    w.setA1Here()
    settle(system)
    after = w.deckMap.pixel(*system.devices.robot.placements["probes"].xy_mm("A1"))
    # Taught 1.5 mm nearer the stage origin, which the map's half turn
    # puts to the right.
    assert after[0] > before[0], "A1 taught nearer the stage origin moves the drawn plate"


def test_the_window_starts_with_do_not_lower_tip_on(main_window, plate_sipper_config, simulated_system):
    """A Go to well before the taught positions are checked never dips: the
    standalone window turns the switch on at start and the Robot tab shows
    it, so a run from the window is refused until it is off; a system built
    without the window lowers the tip, so a command-line run is not."""
    window, system = main_window(plate_sipper_config)
    assert system.devices.robot.lower_tip is False
    assert window.tabWidget.widget(window.ROBOT_TAB).lowerTipBox.isChecked()
    with pytest.raises(OperationError, match="Do not lower tip"):
        system.run([HYB])
    assert simulated_system(plate_sipper_config).devices.robot.lower_tip is True


def test_one_job_on_the_rig_greys_the_other_tabs(main_window, plate_sipper_config, real_clock):
    """The real window: a Robot-tab move greys the run and manual tabs; a
    manual-tab move greys the run and Robot tabs; all come back after."""
    window, system = main_window(plate_sipper_config)
    tabs = window.tabWidget

    def enabled():
        return {name: tabs.isTabEnabled(index) for name, index in
                (("run", window.RUN_TAB), ("manual", window.MANUAL_TAB), ("robot", window.ROBOT_TAB))}

    for tab, expected in ((window.ROBOT_TAB, {"run": False, "manual": False, "robot": True}),
                          (window.MANUAL_TAB, {"run": False, "manual": True, "robot": False})):
        gate = threading.Event()
        tabs.widget(tab)._run(lambda: gate.wait(5))
        try:
            QApplication.processEvents()
            assert enabled() == expected
        finally:
            gate.set()
        settle(system)
        assert enabled() == {"run": True, "manual": True, "robot": True}


@pytest.mark.parametrize("in_front", ["run", "manual", "robot"])
def test_a_reset_from_the_reservoirs_window_keeps_the_run_tab_live(main_window, plate_sipper_config,
                                                                    monkeypatch, real_clock, in_front):
    """A reset is a manual job the Run tab's Reservoirs window starts: the
    main window must not grey that tab -- nor so the window, its child --
    under its own job; the other two go dead until it ends. The window
    floats, so the reset may start with another tab in front: that tab
    goes dead, and the Run tab comes forward rather than Qt's next tab."""
    window, system = main_window(plate_sipper_config)
    tabs = window.tabWidget
    tabs.setCurrentIndex({"run": window.RUN_TAB, "manual": window.MANUAL_TAB,
                          "robot": window.ROBOT_TAB}[in_front])
    gate = threading.Event()
    monkeypatch.setattr(system.manual, "refill", lambda *keys: gate.wait(5))
    monkeypatch.setattr("fluidics.qt.reservoirs_window._ask_yes_no", lambda *a, **k: True)

    def enabled():
        return {name: tabs.isTabEnabled(index) for name, index in
                (("run", window.RUN_TAB), ("manual", window.MANUAL_TAB), ("robot", window.ROBOT_TAB))}

    run_tab = tabs.widget(window.RUN_TAB)
    run_tab.reservoirsButton.click()
    reservoirs = run_tab.reservoirsWindow
    reservoirs.resetPlateButtons["probes"].click()
    try:
        QApplication.processEvents()
        assert tabs.currentIndex() == window.RUN_TAB
        assert enabled() == {"run": True, "manual": False, "robot": False}
        assert reservoirs.isEnabled() and not reservoirs.resetAllButton.isEnabled()
    finally:
        gate.set()
    settle(system)
    assert tabs.currentIndex() == window.RUN_TAB
    assert enabled() == {"run": True, "manual": True, "robot": True}
    assert reservoirs.resetAllButton.isEnabled()


def test_a_teach_job_started_with_the_manual_tab_in_front_brings_the_robot_tab_forward(
        main_window, plate_sipper_config, real_clock):
    """The Teach window floats too: its job greys the Manual tab, and the
    Robot tab comes forward, not Qt's next tab."""
    window, system = main_window(plate_sipper_config)
    tabs = window.tabWidget
    tabs.setCurrentIndex(window.MANUAL_TAB)
    gate = threading.Event()
    tabs.widget(window.ROBOT_TAB)._run(lambda: gate.wait(5))
    try:
        QApplication.processEvents()
        assert tabs.currentIndex() == window.ROBOT_TAB
    finally:
        gate.set()
    settle(system)
    assert tabs.currentIndex() == window.ROBOT_TAB


@pytest.mark.parametrize("config_name, has_robot", [("plate_sipper_config", True),
                                                    ("flow_cell_config", False)])
def test_the_main_window_adds_a_robot_tab_only_with_a_gantry(main_window, request, config_name, has_robot):
    """The tab is built when the gantry block is configured (spec §9), the
    way the temperature and flow tabs are; D8: none without it."""
    window, _ = main_window(request.getfixturevalue(config_name))
    tabs = [window.tabWidget.tabText(i) for i in range(window.tabWidget.count())]
    assert ("Robot" in tabs) == has_robot
    assert window.ROBOT_TAB == (tabs.index("Robot") if has_robot else None)


def test_a_dedicated_well_set_elsewhere_shows_on_its_row(widget):
    """A Set made through the verb by a script or an embedder: the row
    shows the config's well once the job ends, and when Teach opens."""
    w, system = widget
    system.run_manual(lambda: system.manual.set_dedicated_well("priming", "ssct/B4"))
    settle(system)
    reservoirs, wells, _ = w.dedicatedWells["priming"]
    assert (reservoirs.currentData(), wells.value()) == ("ssct", "B4")
