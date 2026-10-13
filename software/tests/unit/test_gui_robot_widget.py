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
    # channels on `destroyed`, so the close's park below posts into nothing.
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


def test_stop_is_live_for_this_tab_s_own_job(widget, real_clock):
    """The one STOP: live while this tab's own move runs, the session's
    abort when pressed."""
    w, system = widget
    import threading
    gate = threading.Event()
    w._run(lambda: gate.wait(5))
    try:
        QApplication.processEvents()
        assert w.stopButton.isEnabled() and not w.goToWellButton.isEnabled()
    finally:
        gate.set()
    _settle(system)
    assert not w.stopButton.isEnabled() and w.goToWellButton.isEnabled()


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
    assert system.devices.robot.position()[2] == 10.0


def test_teach_goes_through_the_manual_verbs_then_saves(widget, monkeypatch):
    w, system = widget
    saved = []
    monkeypatch.setattr("fluidics.qt.robot_control.save_config", lambda config: saved.append(config))
    w.reservoirCombo.setCurrentIndex(w.reservoirCombo.findData("probes"))
    w.jogButtons[("x", 1.0)].click()
    _settle(system)
    w.setA1Here()
    _settle(system)
    from fluidics.control.config import gantry
    assert tuple(gantry(w.config).reservoirs[1].a1_xy_mm) == (1.0, 0.0)
    assert system.devices.robot.placements["probes"].a1_xy_mm == (1.0, 0.0)
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


def test_an_unknown_position_is_painted_as_such(widget):
    w, _ = widget
    w._paintPosition(None)
    assert "unknown" in w.positionLabel.text()


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
