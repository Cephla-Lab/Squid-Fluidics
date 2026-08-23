# tests/unit/test_gui_temperature_widget.py
"""TemperatureControlWidget's subscription wiring, end to end.

Same rationale as test_gui_flow_widget: the widget's three wiring lines
(subscribe in __init__, the signal connect, the fan-out) are the whole
delivery path from the driver to the plots and CSV, and only a constructed
widget can prove them. A publish on the controller must come out as a
recorded reading. Same-thread emit is delivered synchronously; the polling
thread the constructor starts also publishes, but its cross-thread emissions
sit in the never-spun event queue, so the assertions below are
deterministic.
"""

from datetime import datetime

import pytest

# Qt's offscreen platform is forced in tests/conftest.py.
from PyQt5.QtWidgets import QApplication

import gui
from fluidics.control.temperature_controller import TCMControllerSimulation

from .test_gui_helpers import RecordingWriter


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def test_a_publish_on_the_controller_lands_in_the_recording(qapp):
    controller = TCMControllerSimulation(channels=1)
    widget = gui.TemperatureControlWidget(controller)
    try:
        channel = widget.plot_widgets[0]
        channel.writer = RecordingWriter()
        controller.actual_temperatures = [25.0]
        controller._publish()
        rows = channel.writer.rows
        assert len(rows) == 1
        assert rows[0][1] == 25.0
    finally:
        widget.deleteLater()
        controller.close()
