"""The Reservoirs window, opened from the Run Experiments tab, over a
simulated system: one row for every tracked reservoir and every bottle
the valves can draw from (the sipper's port left out), at 0 until drawn
from and again after a reset, blank where no volume is declared and
`undeclared` where a volume is yet to be declared; Reset selected, Reset
<plate> and Reset all as manual jobs the Run tab owns, the verb's refusal
said before the question; Set selected…, Set <plate>… and a double click
on a Loaded cell setting loaded volumes the same way; and the repaints
that keep it current."""
import threading

import pytest
from qtpy.QtCore import QEvent, QItemSelectionModel
from qtpy.QtWidgets import QApplication, QHeaderView

from fluidics.control.config import gantry, load_config
from fluidics.qt.reservoirs_window import LOADED_COLUMN
from fluidics.qt.sequence_editor import RUN_TAB_JOB_SOURCE, SequencesWidget

from ..conftest import settle

QUESTION = "Accumulated goes to 0, and a declared volume reads full again."


@pytest.fixture
def reservoirs(qapp, simulated_system):
    """(ReservoirsWindow, SequencesWidget, FluidicsSystem) over an instant
    simulated rig built from `config`, the window opened from the tab's
    button as the operator opens it; torn down clean -- a factory, since
    this suite builds from two configs, one of them edited first."""
    built = []

    def _open(config):
        system = simulated_system(config)
        tab = SequencesWidget(config, system)
        built.append(tab)
        tab.reservoirsButton.click()
        return tab.reservoirsWindow, tab, system

    yield _open
    for tab in built:
        # Destroyed, not only closed (the window with it, as its child): the
        # tab's subscriptions, the log handler among them, come off on
        # `destroyed`, so the system's close posts into nothing.
        tab.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def _rows(window):
    """The table as painted: {reservoir: (reagent, accumulated, current, loaded)}."""
    table = window.table
    return {table.item(r, 0).text(): tuple(table.item(r, c).text() for c in range(1, 5))
            for r in range(table.rowCount())}


def _keys(window):
    return [window.table.item(r, 0).text() for r in range(window.table.rowCount())]


def _select(window, *keys):
    """Highlight the rows of `keys`, as a ctrl-click on each would."""
    table = window.table
    for row, key in enumerate(_keys(window)):
        if key in keys:
            table.selectionModel().select(table.model().index(row, 0),
                                          QItemSelectionModel.Select | QItemSelectionModel.Rows)


def _agrees(monkeypatch, answer=True):
    """The Yes/No question answers `answer`; returns the list that will
    record what it asked."""
    asked = []
    monkeypatch.setattr("fluidics.qt.reservoirs_window._ask_yes_no",
                        lambda parent, title, text, **k: asked.append(text) or answer)
    return asked


def _warnings(monkeypatch):
    warned = []
    monkeypatch.setattr("fluidics.qt.reservoirs_window.QMessageBox.warning",
                        lambda parent, title, text: warned.append(text))
    return warned


def _enters(monkeypatch, value, ok=True):
    """The loaded volume dialog answers `value` (or is cancelled, ok
    False); returns the list that will record (prompt, prefill, minimum)
    for each time it is asked."""
    asked = []

    def get_int(parent, title, label, prefill, minimum, maximum, *rest):
        asked.append((label, prefill, minimum))
        return value, ok

    monkeypatch.setattr("fluidics.qt.reservoirs_window.QInputDialog.getInt", get_int)
    return asked


def _resets(monkeypatch, system):
    """Record each key the verb resets and the session's label for the job
    it runs in, then reset it."""
    heard = []
    refill = system.manual.refill
    monkeypatch.setattr(system.manual, "refill",
                        lambda *keys: heard.extend((key, system.session.source) for key in keys)
                        or refill(*keys))
    return heard


def _draws(system, *draws):
    """(port, well or None, uL) draws by the manual verbs, as one manual
    job; settled, so the session's state change has repainted the window."""
    manual = system.manual

    def draw():
        for port, well, ul in draws:
            if well is not None:
                manual.go_to_well(well)
            manual.open_port(port)
            manual.extract(2, ul, 2000)

    system.run_manual(draw)
    settle(system)


def test_every_bottle_and_every_well_has_a_row_before_any_draw(reservoirs, plate_sipper_config):
    """Every bottle the valves can draw from (the plumbed ports but the
    sipper's) and every well of both plates before any draw, an untracked
    bottle with blank volumes; after a well draw and a bottle draw, their
    figures, repainted by the job's end."""
    window, _, system = reservoirs(plate_sipper_config)
    assert window.isVisible() and window.windowTitle() == "Reservoirs"
    rows = _rows(window)
    assert len(rows) == 5 + 2 * 96
    assert "port_1" not in rows, "the sipper's port: its draws are its wells'"
    assert rows["port_25"] == ("4x SSC", "0", "500000", "500000")
    assert rows["port_26"] == ("DAPI", "0", "", "")
    assert rows["probes/A1"] == ("hyb R01", "0", "110", "110")
    assert rows["ssct/B3"] == ("sipper", "0", "1800", "1800")
    _draws(system, (1, "probes/A1", 50), (26, None, 100))
    rows = _rows(window)
    assert rows["port_26"] == ("DAPI", "100", "", "")
    assert rows["probes/A1"] == ("hyb R01", "50", "60", "110")
    assert _keys(window)[:7] == ["port_24", "port_25", "port_26", "port_27", "port_28",
                                 "probes/A1", "probes/B1"], \
        "the bottles by port, then each plate's wells column-major"


def test_the_reagent_column_takes_the_width(reservoirs, plate_sipper_config):
    """"Accumulated (uL)" must not read "ccumulated (u": the numeric columns
    size to their contents."""
    window, _, _ = reservoirs(plate_sipper_config)
    header = window.table.horizontalHeader()
    assert header.sectionResizeMode(1) == QHeaderView.Stretch
    assert all(header.sectionResizeMode(c) == QHeaderView.ResizeToContents for c in (0, 2, 3, 4))


def test_an_undeclared_reservoir_reads_undeclared_and_the_line_names_it(reservoirs, plate_sipper_config):
    """Current says `undeclared`; the line under the table names what is
    (a plate wholly undeclared once) and that a run is refused until it is
    reset; it goes with the last of them."""
    window, _, system = reservoirs(plate_sipper_config)
    tracker = system.devices.reservoirs
    assert window.undeclaredLabel.isHidden()
    probes = [key for key in tracker.tracked if key.startswith("probes/")]
    tracker.restore({"undeclared": ["port_25", *probes]})
    window.refresh()
    rows = _rows(window)
    assert rows["port_25"] == ("4x SSC", "0", "undeclared", "500000")
    assert rows["probes/H12"][2] == "undeclared" and rows["ssct/A1"][2] == "1800"
    assert not window.undeclaredLabel.isHidden()
    assert window.undeclaredLabel.text() == \
        "Undeclared: port_25, every well of probes. A run is refused until they are reset."
    tracker.refill("all")
    window.refresh()
    assert window.undeclaredLabel.isHidden() and window.undeclaredLabel.text() == ""
    assert _rows(window)["port_25"][2] == "500000"


def test_reset_selected_resets_the_highlighted_reservoirs_as_a_run_tab_job(
        reservoirs, plate_sipper_config, monkeypatch):
    """Two rows highlighted, one left alone: the question names the two,
    each is reset through the verb in one manual job the session labels for
    the Run tab, and the job's end repaints the window."""
    window, _, system = reservoirs(plate_sipper_config)
    tracker = system.devices.reservoirs
    tracker.restore({"reservoirs": {"port_25": {"dispatched_ul": 2500},
                                    "probes/A1": {"dispatched_ul": 50},
                                   "ssct/A2": {"dispatched_ul": 100}}})
    window.refresh()
    asked, heard = _agrees(monkeypatch), _resets(monkeypatch, system)
    assert not window.resetSelectedButton.isEnabled(), "nothing highlighted"
    _select(window, "port_25", "probes/A1")
    assert window.selectedKeys() == ["port_25", "probes/A1"]
    window.resetSelectedButton.click()
    settle(system)
    assert asked == [f"Reset port_25, probes/A1? {QUESTION}"]
    assert heard == [("port_25", RUN_TAB_JOB_SOURCE), ("probes/A1", RUN_TAB_JOB_SOURCE)]
    assert tracker.remaining("port_25") == 500000 and tracker.remaining("probes/A1") == 110
    assert tracker.remaining("ssct/A2") == 1700, "a row not highlighted is not reset"
    rows = _rows(window)
    assert rows["port_25"][1] == "0" and rows["probes/A1"][1] == "0" and rows["ssct/A2"][1] == "100"


def test_reset_plate_resets_every_well_of_it_and_nothing_else(reservoirs, plate_sipper_config, monkeypatch):
    """A plate reloads as a plate: one button per plate."""
    window, _, system = reservoirs(plate_sipper_config)
    assert sorted(window.resetPlateButtons) == ["probes", "ssct"]
    tracker = system.devices.reservoirs
    tracker.restore({"reservoirs": {"ssct/A1": {"dispatched_ul": 1000}, "ssct/B3": {"dispatched_ul": 200},
                                    "probes/A1": {"dispatched_ul": 50}}})
    asked, heard = _agrees(monkeypatch), _resets(monkeypatch, system)
    window.resetPlateButtons["ssct"].click()
    settle(system)
    assert asked == [f"Reset every well of ssct? {QUESTION}"] and heard == [("ssct", RUN_TAB_JOB_SOURCE)]
    assert tracker.remaining("ssct/A1") == 1800 and tracker.remaining("ssct/B3") == 1800
    assert tracker.remaining("probes/A1") == 60
    assert _rows(window)["ssct/A1"] == ("sipper", "0", "1800", "1800")


def test_reset_all_resets_everything_the_untracked_bottles_included(
        reservoirs, plate_sipper_config, monkeypatch):
    window, _, system = reservoirs(plate_sipper_config)
    _draws(system, (1, "probes/A1", 50), (26, None, 100))
    system.manual.retract()                        # the tip leaves probes/A1 before its plate is lifted
    assert _rows(window)["port_26"] == ("DAPI", "100", "", "")
    asked = _agrees(monkeypatch)
    window.resetAllButton.click()
    settle(system)
    assert asked == [f"Reset every reservoir? {QUESTION}"]
    rows = _rows(window)
    assert rows["port_26"] == ("DAPI", "0", "", ""), "an untracked bottle's reset zeroes it and keeps its row"
    assert rows["probes/A1"] == ("hyb R01", "0", "110", "110")


def test_a_declined_reset_resets_nothing(reservoirs, plate_sipper_config, monkeypatch):
    """QPushButton.clicked carries a bool; a slot that took it as its first
    argument would skip the question."""
    window, _, system = reservoirs(plate_sipper_config)
    system.devices.reservoirs.restore({"reservoirs": {"probes/A1": {"dispatched_ul": 50}}})
    asked = _agrees(monkeypatch, answer=False)
    window.resetPlateButtons["probes"].click()
    QApplication.processEvents()
    assert asked == [f"Reset every well of probes? {QUESTION}"]
    assert not system.busy
    assert system.devices.reservoirs.remaining("probes/A1") == 60, "declined: nothing reset"


def test_a_reset_is_refused_before_the_question_while_the_tip_is_in_that_plate(
        reservoirs, plate_sipper_config, monkeypatch):
    """Lifting the plate the tip rests in bends the tip: the window says so
    before it asks, for the plate's button, Reset all and a highlighted
    well of it alike, and starts no job; the other plate resets as usual."""
    window, _, system = reservoirs(plate_sipper_config)
    asked, warned = _agrees(monkeypatch), _warnings(monkeypatch)
    system.manual.go_to_well("ssct/A2")
    system.devices.reservoirs.restore({"reservoirs": {"ssct/A1": {"dispatched_ul": 100}}})
    window.refresh()
    _select(window, "ssct/A1")
    for button in (window.resetPlateButtons["ssct"], window.resetAllButton, window.resetSelectedButton):
        button.click()
        QApplication.processEvents()
    assert asked == [] and len(warned) == 3
    assert all("ssct/A2" in text and "Retract" in text for text in warned)
    assert not system.busy and system.devices.reservoirs.remaining("ssct/A1") == 1700
    window.resetPlateButtons["probes"].click()
    settle(system)
    assert asked == [f"Reset every well of probes? {QUESTION}"]


def test_a_reset_is_refused_while_the_tip_is_down_without_a_witness(
        reservoirs, plate_sipper_config, monkeypatch):
    """A jog clears the witness without lifting the tip: any plate's reset
    waits for Retract; a bottle is never under the tip."""
    window, _, system = reservoirs(plate_sipper_config)
    asked, warned = _agrees(monkeypatch), _warnings(monkeypatch)
    system.manual.jog("z", 10.0)
    robot = system.devices.robot
    assert robot.current_well is None and not robot.is_retracted
    window.resetPlateButtons["probes"].click()
    QApplication.processEvents()
    assert asked == [] and len(warned) == 1 and "not known to be retracted" in warned[0]
    _select(window, "port_25")
    window.resetSelectedButton.click()
    settle(system)
    assert asked == [f"Reset port_25? {QUESTION}"]
    system.manual.retract()
    window.resetPlateButtons["probes"].click()
    settle(system)
    assert len(asked) == 2


def test_a_reset_that_fails_is_a_dialog(reservoirs, plate_sipper_config, monkeypatch):
    """The verb runs on the job's thread; what it raises comes back as the
    window's dialog."""
    window, _, system = reservoirs(plate_sipper_config)
    said = []
    monkeypatch.setattr("fluidics.qt.reservoirs_window.QMessageBox.critical",
                        lambda parent, title, text: said.append(text))
    _agrees(monkeypatch)

    def refuse(key):
        raise RuntimeError("the volume tracking file could not be written")

    monkeypatch.setattr(system.manual, "refill", refuse)
    window.resetAllButton.click()
    settle(system)
    assert said == ["The reset was not recorded: the volume tracking file could not be written"]


def test_the_resets_and_the_settings_wait_for_an_idle_rig(
        reservoirs, plate_sipper_config, real_clock, monkeypatch):
    """A reset or a setting is a manual job: every one of their buttons is
    dead while any job runs and live again after, from its own repaint on
    the session's state, and a double click on a Loaded cell asks nothing.
    On the real clock: the fake one's Event.wait(5) returns at once."""
    window, tab, system = reservoirs(plate_sipper_config)
    asked = _enters(monkeypatch, 1000, ok=False)
    _select(window, "port_25")
    buttons = (window.resetSelectedButton, window.resetAllButton, *window.resetPlateButtons.values(),
               window.setSelectedButton, *window.setPlateButtons.values())
    assert sorted(window.setPlateButtons) == ["probes", "ssct"]
    assert all(button.isEnabled() for button in buttons)
    gate = threading.Event()
    system.run_manual(lambda: gate.wait(5))
    try:
        QApplication.processEvents()
        assert not any(button.isEnabled() for button in buttons)
        window.table.cellDoubleClicked.emit(_keys(window).index("port_25"), LOADED_COLUMN)
        assert asked == [], "a double click under a job asked for a volume"
        assert tab.reservoirsButton.isEnabled(), "opening the window is not a job"
    finally:
        gate.set()
    settle(system)
    assert all(button.isEnabled() for button in buttons)


def test_the_window_keeps_itself_current_while_open_and_a_closed_one_waits_to_be_shown(
        reservoirs, plate_sipper_config):
    """Open, it repaints each second: a draw and a rename on the Manual
    tab, with no job to repaint, show within one. Closed, it is not
    painted every second for nobody, and opening it again paints it."""
    window, tab, system = reservoirs(plate_sipper_config)
    manual = system.manual
    assert window._tick.isActive()
    manual.open_port(26)
    manual.extract(2, 100, 2000)
    names = plate_sipper_config.reagent_selection.selector_valves.name_mapping
    plate_sipper_config.reagent_selection.selector_valves.name_mapping = dict(names, port_26="Hoechst")
    window._tick.timeout.emit()
    assert _rows(window)["port_26"] == ("Hoechst", "100", "", "")
    window.close()
    assert not window._tick.isActive()
    manual.extract(2, 50, 2000)
    window.refresh()
    assert _rows(window)["port_26"] == ("Hoechst", "100", "", ""), "hidden: not painted"
    tab.reservoirsButton.click()
    assert window.isVisible() and window._tick.isActive()
    assert _rows(window)["port_26"] == ("Hoechst", "150", "", "")


def test_the_selection_follows_the_reservoirs_when_a_bottle_joins_the_rows(
        reservoirs, plate_sipper_config, monkeypatch):
    """A volume tracker built without its bottles (by hand, say) lists a
    bottle at its first draw, its row inserted among the bottles above
    every well: the highlighted well stays highlighted, so a reset of the
    selection resets what the operator picked."""
    monkeypatch.setattr("fluidics.devices.bottle_keys", lambda config: [])
    window, _, system = reservoirs(plate_sipper_config)
    _select(window, "probes/A1")
    _draws(system, (26, None, 100))
    assert _keys(window)[1] == "port_26"
    assert window.selectedKeys() == ["probes/A1"]


def test_a_rig_that_tracks_nothing_lists_and_resets_its_bottles(
        reservoirs, flow_cell_config, monkeypatch):
    """No declared volume and no plate: a row per plumbed bottle from the
    start, every volume blank and nothing undeclared; a draw counts on its
    row, and Reset all zeroes it and keeps the row."""
    window, _, system = reservoirs(flow_cell_config)
    assert system.devices.reservoirs.tracked == () and window.resetPlateButtons == {}
    rows = _rows(window)
    assert len(rows) == 28 and rows["port_3"] == ("z", "0", "", "")
    assert window.resetAllButton.isEnabled()
    _draws(system, (3, None, 100))
    assert _rows(window)["port_3"] == ("z", "100", "", "")
    assert window.undeclaredLabel.isHidden()
    _agrees(monkeypatch)
    window.resetAllButton.click()
    settle(system)
    assert _rows(window)["port_3"] == ("z", "0", "", "")
    assert all(accumulated == 0 for _, _, accumulated, *_ in system.devices.reservoirs.balances())


def test_a_tracked_bottle_on_a_rig_without_a_robot_resets_from_its_row(
        reservoirs, flow_cell_config, monkeypatch):
    flow_cell_config.reagent_selection.selector_valves.loaded_volume_ul = {"port_2": 10000}
    window, _, system = reservoirs(flow_cell_config)
    assert system.devices.robot is None and system.devices.reservoirs.tracked == ("port_2",)
    system.devices.reservoirs.restore({"reservoirs": {"port_2": {"dispatched_ul": 400}}})
    window.refresh()
    assert _rows(window)["port_2"] == ("y", "400", "9600", "10000")
    assert _rows(window)["port_3"] == ("z", "0", "", ""), "the untracked bottles beside it"
    _agrees(monkeypatch)
    _select(window, "port_2")
    window.resetSelectedButton.click()
    settle(system)
    assert system.devices.reservoirs.remaining("port_2") == 10000
    assert _rows(window)["port_2"] == ("y", "0", "10000", "10000")


# --- the loaded volumes ---

def _sets(monkeypatch, system):
    """Record each (keys, uL) the verb sets and the session's label for the
    job it runs in, then set it."""
    heard = []
    set_loaded_volume = system.manual.set_loaded_volume
    monkeypatch.setattr(system.manual, "set_loaded_volume",
                        lambda keys, ul: heard.append((list(keys), ul, system.session.source))
                        or set_loaded_volume(keys, ul))
    return heard


def test_set_selected_sets_the_highlighted_rows_as_a_run_tab_job(
        reservoirs, plate_sipper_config_on_file, monkeypatch):
    """The dialog names the rows and starts from the first one's loaded
    volume; accumulated stands and current follows, an untracked bottle
    tracked from then on; the config file carries both."""
    config = plate_sipper_config_on_file
    window, _, system = reservoirs(config)
    system.devices.reservoirs.restore({"reservoirs": {"port_25": {"dispatched_ul": 2500}}})
    window.refresh()
    asked, heard = _enters(monkeypatch, 600000), _sets(monkeypatch, system)
    assert not window.setSelectedButton.isEnabled(), "nothing highlighted"
    _select(window, "port_25", "port_26")
    window.setSelectedButton.click()
    settle(system)
    assert asked == [("Loaded volume of port_25, port_26 (µL):", 500000, 1)]
    assert heard == [(["port_25", "port_26"], 600000, RUN_TAB_JOB_SOURCE)]
    rows = _rows(window)
    assert rows["port_25"] == ("4x SSC", "2500", "597500", "600000")
    assert rows["port_26"] == ("DAPI", "0", "undeclared", "600000"), "newly tracked: a reset declares it"
    assert "port_26" in window.undeclaredLabel.text()
    assert load_config(config.source_path).reagent_selection.selector_valves.loaded_volume_ul == {
        "port_25": 600000, "port_26": 600000}


def test_set_plate_sets_every_well_of_it_and_nothing_else(
        reservoirs, plate_sipper_config_on_file, monkeypatch):
    config = plate_sipper_config_on_file
    window, _, system = reservoirs(config)
    system.devices.reservoirs.restore({"reservoirs": {"probes/A1": {"dispatched_ul": 50}}})
    asked = _enters(monkeypatch, 350)
    window.setPlateButtons["probes"].click()
    settle(system)
    assert asked == [("Loaded volume of every well of probes (µL):", 110, 1)]
    rows = _rows(window)
    assert rows["probes/A1"] == ("hyb R01", "50", "300", "350")
    assert rows["probes/H12"] == ("sipper", "0", "350", "350")
    assert rows["ssct/A1"] == ("sipper", "0", "1800", "1800")
    assert gantry(load_config(config.source_path)).reservoirs[1].loaded_ul == 350


def test_a_double_click_on_a_loaded_cell_sets_that_row(
        reservoirs, plate_sipper_config_on_file, monkeypatch):
    """The row is selected first, alone, so the dialog names what it sets;
    a double click on any other column is not a setting."""
    window, _, system = reservoirs(plate_sipper_config_on_file)
    asked = _enters(monkeypatch, 1000)
    _select(window, "probes/A1")
    row = _keys(window).index("ssct/B1")
    window.table.cellDoubleClicked.emit(row, LOADED_COLUMN - 1)
    assert asked == [] and window.selectedKeys() == ["probes/A1"]
    window.table.cellDoubleClicked.emit(row, LOADED_COLUMN)
    settle(system)
    assert asked == [("Loaded volume of ssct/B1 (µL):", 1800, 1)]
    assert window.selectedKeys() == ["ssct/B1"]
    assert _rows(window)["ssct/B1"] == ("sipper", "0", "1000", "1000")
    assert _rows(window)["probes/A1"][3] == "110"


def test_a_refusal_the_keys_decide_is_said_before_the_dialog(
        reservoirs, plate_sipper_config_on_file, monkeypatch):
    """The verb is asked first with the keys alone: a refusal there is a
    warning, the dialog never opens and no job starts."""
    window, _, system = reservoirs(plate_sipper_config_on_file)
    asked, warned = _enters(monkeypatch, 1000), _warnings(monkeypatch)
    refuse = system.manual.loaded_volume_refusal
    monkeypatch.setattr(system.manual, "loaded_volume_refusal",
                        lambda keys, ul=None: "port_25 is not set here" if ul is None else refuse(keys, ul))
    _select(window, "port_25")
    window.setSelectedButton.click()
    QApplication.processEvents()
    assert warned == ["port_25 is not set here"] and asked == []
    assert not system.busy and system.devices.reservoirs.loaded("port_25") == 500000


def test_a_refused_volume_is_said_before_the_job_and_sets_nothing(
        reservoirs, plate_sipper_config_on_file, monkeypatch):
    """The priming well below the sipper's line: refused once the value is
    in, before any job, so nothing is written anywhere."""
    config = plate_sipper_config_on_file
    window, _, system = reservoirs(config)
    before = open(config.source_path, encoding="utf-8").read()
    asked, warned = _enters(monkeypatch, 100), _warnings(monkeypatch)
    window.setPlateButtons["ssct"].click()
    QApplication.processEvents()
    assert len(asked) == 1
    assert warned == ["ssct/H11 is the priming well: it must hold at least the 250 uL the "
                      "sipper's line takes to prime"]
    assert not system.busy and system.devices.reservoirs.loaded("ssct/H11") == 1800
    assert open(config.source_path, encoding="utf-8").read() == before


def test_a_cancelled_dialog_sets_nothing(reservoirs, plate_sipper_config_on_file, monkeypatch):
    window, _, system = reservoirs(plate_sipper_config_on_file)
    asked = _enters(monkeypatch, 5000, ok=False)
    window.setPlateButtons["probes"].click()
    QApplication.processEvents()
    assert len(asked) == 1 and not system.busy
    assert system.devices.reservoirs.loaded("probes/A1") == 110


def test_a_setting_that_fails_is_a_dialog(reservoirs, plate_sipper_config_on_file, monkeypatch):
    """The verb runs on the job's thread; what it raises comes back as the
    window's dialog."""
    window, _, system = reservoirs(plate_sipper_config_on_file)
    said = []
    monkeypatch.setattr("fluidics.qt.reservoirs_window.QMessageBox.critical",
                        lambda parent, title, text: said.append(text))
    _enters(monkeypatch, 350)

    def fail(keys, ul):
        raise RuntimeError("the volume tracking file could not be written")

    monkeypatch.setattr(system.manual, "set_loaded_volume", fail)
    window.setPlateButtons["probes"].click()
    settle(system)
    assert said == ["The loaded volume was not set: the volume tracking file could not be written"]


def test_a_rig_without_a_robot_sets_a_bottle_from_its_row(
        reservoirs, config_on_file, monkeypatch):
    """No plates, so no plate buttons; an untracked bottle given a volume
    is tracked from then on, in the file the config came from."""
    config = config_on_file("flow_cell_config.yaml")
    window, _, system = reservoirs(config)
    assert window.setPlateButtons == {}
    _enters(monkeypatch, 10000)
    _select(window, "port_3")
    window.setSelectedButton.click()
    settle(system)
    assert system.devices.reservoirs.tracked == ("port_3",)
    assert _rows(window)["port_3"] == ("z", "0", "undeclared", "10000"), "newly tracked: a reset declares it"
    assert load_config(config.source_path).reagent_selection.selector_valves.loaded_volume_ul == {"port_3": 10000}
