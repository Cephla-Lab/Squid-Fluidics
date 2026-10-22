"""ManualControlWidget over a simulated system: the tab's wiring on a real
widget -- a port picked on the valve combo turns the valve as a manual job
on the session, and the controls come back when it ends. The volumes it
used to show live in the Run Experiments tab's Reservoirs window now."""
import pytest
from qtpy.QtCore import QEvent
from qtpy.QtWidgets import QApplication

from fluidics.qt.manual_control import ManualControlWidget

from ..conftest import settle


@pytest.fixture
def manual_tab(qapp, simulated_system, flow_cell_config):
    system = simulated_system(flow_cell_config)
    w = ManualControlWidget(flow_cell_config, system)
    yield w, system
    w.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_picking_a_port_turns_the_valve_as_a_manual_job_and_frees_the_controls(manual_tab):
    w, system = manual_tab
    heard = []
    system.session.state.subscribe(heard.append)
    port = w.valveCombo.itemData(1)
    w.valveCombo.setCurrentIndex(1)
    settle(system)
    assert heard == ["manual", None]
    assert system.devices.selector_valves.get_current_port() == port
    assert w.valveCombo.isEnabled() and not w.stopButton.isEnabled()
