"""The Run Experiments tab's reagent table and its Refilled… declaration,
over a simulated system: one table for every reservoir drawn from, with
the ledger's loaded and remaining volumes where one is declared; what the
picker offers; a plate reloaded as a plate and a bottle refilled through
it, the verb's refusal said before the question; and none of it on a rig
that tracks no volume."""
import threading

import pytest
from qtpy.QtCore import QEvent
from qtpy.QtWidgets import QApplication

from fluidics.qt.sequence_editor import SequencesWidget

from ..conftest import settle


@pytest.fixture
def run_tab(qapp, simulated_system):
    """(SequencesWidget, FluidicsSystem) over an instant simulated rig built
    from `config`, torn down clean -- a factory, since this suite builds
    from two configs, one of them edited first."""
    built = []

    def _build(config):
        system = simulated_system(config)
        w = SequencesWidget(config, system)
        built.append(w)
        return w, system

    yield _build
    for w in built:
        # Destroyed, not only closed: its subscriptions (the log handler
        # among them) come off on `destroyed`, so the system's close
        # (simulated_system's teardown, after this) posts into nothing.
        w.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def _rows(w):
    """The table as painted: {reservoir: (reagent, used, loaded, remaining)}."""
    table = w.reagentsTable
    return {table.item(r, 0).text(): tuple(table.item(r, c).text() for c in range(1, 5))
            for r in range(table.rowCount())}


def _picks(monkeypatch, label):
    """The picker answers `label` (None: Cancel); returns the list that
    will record what it offered."""
    offered = []

    def get_item(parent, title, question, items, *args):
        offered.append(list(items))
        return (label or "", label is not None)

    monkeypatch.setattr("fluidics.qt.sequence_editor.QInputDialog.getItem", get_item)
    return offered


def _agrees(monkeypatch, answer=True):
    """The Yes/No question answers `answer`; returns the list that will
    record what it asked."""
    asked = []
    monkeypatch.setattr("fluidics.qt.sequence_editor._ask_yes_no",
                        lambda parent, title, text, **k: asked.append(text) or answer)
    return asked


def _warnings(monkeypatch):
    warned = []
    monkeypatch.setattr("fluidics.qt.sequence_editor.QMessageBox.warning",
                        lambda parent, title, text: warned.append(text))
    return warned


def test_the_table_shows_every_reservoir_drawn_from_with_blanks_where_no_volume_is_declared(
        run_tab, plate_sipper_config):
    """A well draw through the sipper and a draw from an untracked bottle,
    by the manual verbs: the well's row carries its name and the ledger's
    volumes, the bottle's only what this run drew."""
    w, system = run_tab(plate_sipper_config)
    assert w.reagentsTable.isHidden(), "nothing drawn yet"
    manual = system.manual
    manual.go_to_well("probes/A1")
    manual.open_port(1)
    manual.extract(2, 50, 2000)
    manual.open_port(26)
    manual.extract(2, 100, 2000)
    w._renderReagents()
    assert not w.reagentsTable.isHidden()
    assert _rows(w) == {"port_26": ("DAPI", "100", "", ""),
                        "probes/A1": ("hyb R01", "50", "110", "55")}


def test_the_picker_offers_everything_each_plate_and_each_tracked_bottle(
        run_tab, plate_sipper_config, monkeypatch):
    w, system = run_tab(plate_sipper_config)
    offered = _picks(monkeypatch, None)                  # Cancel
    system.devices.reservoirs.restore({"reservoirs": {"probes/A1": {"dispatched_ul": 50}}})
    w.refilledButton.click()
    assert offered == [["all reservoirs", "every well of probes", "every well of ssct", "Port 25: 4x SSC"]]
    assert system.devices.reservoirs.remaining("probes/A1") == 55, "cancelled: nothing refilled"


def test_a_click_asks_before_reloading_a_plate(run_tab, plate_sipper_config, monkeypatch):
    """QPushButton.clicked carries a bool; a slot that took it as its first
    argument would skip the question."""
    w, system = run_tab(plate_sipper_config)
    _picks(monkeypatch, "every well of probes")
    asked = _agrees(monkeypatch, answer=False)
    system.devices.reservoirs.restore({"reservoirs": {"probes/A1": {"dispatched_ul": 50}}})
    w.refilledButton.click()
    QApplication.processEvents()
    assert asked == ["Mark every well of probes as full?"]
    assert system.devices.reservoirs.remaining("probes/A1") == 55, "declined: nothing refilled"


def test_a_reload_is_refused_while_the_tip_is_in_that_plate(run_tab, plate_sipper_config, monkeypatch):
    """Lifting the plate the tip rests in bends the tip: the tab says so
    before it asks, and starts no job; the other plate reloads as usual."""
    w, system = run_tab(plate_sipper_config)
    asked, warned = _agrees(monkeypatch), _warnings(monkeypatch)
    system.manual.go_to_well("ssct/A2")
    system.devices.reservoirs.restore({"reservoirs": {"ssct/A1": {"dispatched_ul": 100}}})
    _picks(monkeypatch, "every well of ssct")
    w.refilled()
    QApplication.processEvents()
    assert asked == [] and len(warned) == 1 and "ssct/A2" in warned[0] and "Retract" in warned[0]
    assert system.devices.reservoirs.remaining("ssct/A1") == 1650, "nothing refilled"
    _picks(monkeypatch, "every well of probes")
    w.refilled()
    settle(system)
    assert asked == ["Mark every well of probes as full?"]


def test_a_reload_is_refused_while_the_tip_is_down_without_a_witness(
        run_tab, plate_sipper_config, monkeypatch):
    """A jog clears the witness without lifting the tip: the tab says so
    before it asks, for any plate, until Retract."""
    w, system = run_tab(plate_sipper_config)
    asked, warned = _agrees(monkeypatch), _warnings(monkeypatch)
    system.manual.jog("z", 10.0)
    robot = system.devices.robot
    assert robot.current_well is None and not robot.is_retracted
    _picks(monkeypatch, "every well of probes")
    w.refilled()
    QApplication.processEvents()
    assert asked == [] and len(warned) == 1 and "not known to be retracted" in warned[0]
    system.manual.retract()
    w.refilled()
    settle(system)
    assert len(asked) == 1


def test_a_plate_reloads_as_a_plate_and_the_table_repaints(run_tab, plate_sipper_config, monkeypatch):
    """Every well of the plate is full again -- the wells drawn from this
    run or since their refill included -- and the reload's finish repaints
    the table, which has nothing left to show."""
    w, system = run_tab(plate_sipper_config)
    _agrees(monkeypatch)
    ledger = system.devices.reservoirs
    ledger.restore({"reservoirs": {"ssct/A1": {"dispatched_ul": 1000}, "ssct/B3": {"dispatched_ul": 200}}})
    w._renderReagents()
    assert _rows(w) == {"ssct/A1": ("sipper", "0", "1800", "750"),
                        "ssct/B3": ("sipper", "0", "1800", "1550")}
    _picks(monkeypatch, "every well of ssct")
    w.refilled()
    settle(system)
    assert ledger.remaining("ssct/A1") == 1750 and ledger.remaining("ssct/B3") == 1750
    assert w.reagentsTable.rowCount() == 0 and w.reagentsTable.isHidden(), \
        "the reload's finish repaints the table"


def test_a_bottle_is_declared_here_on_a_rig_without_a_robot(run_tab, flow_cell_config, monkeypatch):
    """A tracked bottle on a plain rig: the picker offers everything and
    that bottle, under the rig's own label for its port; the pick refills it."""
    flow_cell_config.reagent_selection.selector_valves.loaded_volume_ul = {"port_2": 10000}
    w, system = run_tab(flow_cell_config)
    assert system.devices.robot is None and system.devices.reservoirs.tracked == ("port_2",)
    system.devices.reservoirs.restore({"reservoirs": {"port_2": {"dispatched_ul": 400}}})
    w._renderReagents()
    assert _rows(w) == {"port_2": ("y", "0", "10000", "9600")}
    offered = _picks(monkeypatch, "Port 2: y")
    _agrees(monkeypatch)
    w.refilledButton.click()
    settle(system)
    assert offered == [["all reservoirs", "Port 2: y"]]
    assert system.devices.reservoirs.remaining("port_2") == 10000


def test_a_rig_that_tracks_nothing_has_no_declaration(run_tab, flow_cell_config):
    """No declared volume: the table still counts, with every volume blank,
    and there is nothing to declare -- no button, no notice."""
    w, system = run_tab(flow_cell_config)
    assert system.devices.reservoirs.tracked == ()
    assert w.refilledButton is None and w.undeclaredLabel is None
    system.manual.open_port(3)
    system.manual.extract(2, 100, 2000)
    w._renderReagents()
    assert _rows(w) == {"port_3": ("z", "100", "", "")}


def test_the_undeclared_notice_names_what_the_ledger_has_not_declared(
        run_tab, plate_sipper_config, monkeypatch):
    """Said at build (the launch may have found no file) and after every
    declaration, which clears its share."""
    w, system = run_tab(plate_sipper_config)
    ledger = system.devices.reservoirs
    assert w.undeclaredLabel.text() == ""
    probes = [key for key in ledger.tracked if key.startswith("probes/")]
    ledger.restore({"undeclared": ["port_25", "ssct/A1", *probes]})
    second = SequencesWidget(plate_sipper_config, system)
    try:
        assert second.undeclaredLabel.text() == (
            "Ledger undeclared for port_25, every well of probes, ssct/A1: "
            "declare refills before running")
    finally:
        second.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    w._renderReagents()
    assert w.undeclaredLabel.text().startswith("Ledger undeclared for port_25, every well of probes")
    _agrees(monkeypatch)
    _picks(monkeypatch, "every well of probes")
    w.refilled()
    settle(system)
    assert w.undeclaredLabel.text() == \
        "Ledger undeclared for port_25, ssct/A1: declare refills before running"
    _picks(monkeypatch, "all reservoirs")
    w.refilled()
    settle(system)
    assert w.undeclaredLabel.text() == ""


def test_a_refill_that_fails_is_a_dialog(run_tab, plate_sipper_config, monkeypatch):
    """The verb runs on the job's thread; what it raises comes back as this
    tab's dialog, as a failed move does on the manual tab."""
    w, system = run_tab(plate_sipper_config)
    said = []
    monkeypatch.setattr("fluidics.qt.sequence_editor.QMessageBox.critical",
                        lambda parent, title, text: said.append(text))
    _agrees(monkeypatch)
    _picks(monkeypatch, "all reservoirs")

    def refuse(key):
        raise RuntimeError("the ledger could not be written")

    monkeypatch.setattr(system.manual, "refill", refuse)
    w.refilled()
    settle(system)
    assert said == ["The refill was not recorded: the ledger could not be written"]


def test_refilled_waits_for_an_idle_rig(run_tab, plate_sipper_config, real_clock):
    """A declaration is a manual job: the button is dead while any job
    runs, like the run controls, and live again after. On the real clock:
    the fake one's Event.wait(5) returns at once."""
    w, system = run_tab(plate_sipper_config)
    assert w.refilledButton.isEnabled()
    gate = threading.Event()
    system.run_manual(lambda: gate.wait(5))
    try:
        QApplication.processEvents()
        assert not w.refilledButton.isEnabled()
    finally:
        gate.set()
    settle(system)
    assert w.refilledButton.isEnabled()


def test_the_table_sizes_its_numeric_columns_to_their_headers(run_tab, plate_sipper_config):
    """"Used this run (uL)" must not read "sed this run (u": the reagent
    column takes the width, the numeric ones size to their contents."""
    from qtpy.QtWidgets import QHeaderView
    w, _ = run_tab(plate_sipper_config)
    header = w.reagentsTable.horizontalHeader()
    assert header.sectionResizeMode(1) == QHeaderView.Stretch
    assert all(header.sectionResizeMode(c) == QHeaderView.ResizeToContents for c in (0, 2, 3, 4))
