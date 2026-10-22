"""ManualControlWidget over a simulated system: the tracked bottles' remaining
volumes and the "Bottle refilled…" declaration (spec §7, §9), which every
rig with a declared bottle has, robot or not; a rig that tracks nothing
shows neither."""
import pytest
from qtpy.QtCore import QEvent
from qtpy.QtWidgets import QApplication

from fluidics.qt.manual_control import ManualControlWidget

from ..conftest import settle


@pytest.fixture
def manual_widget(qapp, simulated_system):
    """(ManualControlWidget, FluidicsSystem) over an instant simulated rig
    built from `config`, torn down clean -- test_gui_robot_widget's `widget`
    fixture as a factory, since this suite builds from two configs, one of
    them edited first."""
    built = []

    def _build(config):
        system = simulated_system(config)
        w = ManualControlWidget(config, system)
        built.append(w)
        return w, system

    yield _build
    for w in built:
        # Destroyed, not only closed: its subscriptions come off the system's
        # channels on `destroyed`, so the system's close (simulated_system's
        # teardown, after this) posts into nothing.
        w.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_the_manual_tab_shows_remaining_and_declares_a_bottle(manual_widget, plate_sipper_config, monkeypatch):
    """Codex finding 15: tracked bottles are declared from the manual tab,
    which every rig has, robot or not."""
    w, system = manual_widget(plate_sipper_config)
    system.devices.reservoirs._dispatched["port_25"] = 1000
    w.refreshRemaining()
    assert w.remainingLabel("port_25") == "479000"
    monkeypatch.setattr("fluidics.qt.manual_control._pick_bottle", lambda parent, keys: "port_25")
    w.bottleRefilled()
    assert system.wait()      # untimed: a timed wait cannot block under the fake clock
    assert system.devices.reservoirs.remaining("port_25") == 480000


def test_the_routed_bottle_s_remaining_shows_beside_the_port(manual_widget, plate_sipper_config):
    """Beside the source-port combo: the routed port's bottle when it is
    tracked, blank otherwise; repainted when a move ends."""
    w, system = manual_widget(plate_sipper_config)
    w.valveCombo.setCurrentIndex(w.valveCombo.findData(25))     # opens port 25: a manual job
    settle(system)
    assert "480000" in w.routedRemainingLabel.text()
    w.valveCombo.setCurrentIndex(w.valveCombo.findData(26))
    settle(system)
    assert w.routedRemainingLabel.text() == ""


def test_a_rig_with_bottles_and_no_robot_declares_them_here(manual_widget, flow_cell_config, monkeypatch):
    """The manual tab is where a bottle is declared on every rig that tracks
    one -- a rig without a robot (and so without a Robot tab) included. The
    click asks which bottle; Cancel refills nothing."""
    flow_cell_config.reagent_selection.selector_valves.loaded_volume_ul = {"port_2": 10000}
    w, system = manual_widget(flow_cell_config)
    assert system.devices.robot is None and system.devices.reservoirs.tracked == ("port_2",)
    system.devices.reservoirs._dispatched["port_2"] = 400
    w.refreshRemaining()
    assert w.remainingLabel("port_2") == "9600"
    asked = []
    monkeypatch.setattr("fluidics.qt.manual_control._pick_bottle",
                        lambda parent, keys: asked.append(dict(keys)))
    w.bottleRefilledButton.click()
    assert system.wait()
    assert asked == [{"port_2": "Port 2: y"}]
    assert system.devices.reservoirs.remaining("port_2") == 9600, "cancelled: nothing refilled"


def test_a_rig_that_tracks_nothing_shows_no_bottles(manual_widget, flow_cell_config):
    """D8: no declared volume, a ledger that tracks nothing, the manual tab
    as it was."""
    w, system = manual_widget(flow_cell_config)
    assert system.devices.reservoirs.tracked == ()
    assert not hasattr(w, "bottleRefilledButton")
    w.refreshRemaining()                       # a no-op, not an error
