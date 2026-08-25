# tests/unit/test_gui_temperature_widget.py
"""TemperatureControlWidget's subscription wiring, end to end -- the same
argument as test_gui_flow_widget's docstring, for this widget's three wiring
lines. The delta here: the constructor also starts the driver's polling
thread, which the test stops again before asserting, so the only publisher
left is the synchronous one it drives itself.
"""

import gui
from fluidics.control.temperature_controller import TCMControllerSimulation

from .test_gui_helpers import RecordingWriter


def test_a_publish_on_the_controller_lands_in_the_recording(qapp):
    controller = TCMControllerSimulation(channels=1)
    widget = gui.TemperatureControlWidget(controller)
    try:
        # The constructor must have started the publisher...
        assert controller._polling_started
        # ...then stop its loop. No join here: under the patched Event.wait,
        # Thread.start() can return before the bootstrap marks the thread
        # started, and join() would raise in that window -- close() in the
        # finally joins once it is joinable. Determinism does not need the
        # join anyway: the thread's cross-thread emissions sit in the
        # never-spun event queue; only the synchronous publish below lands.
        controller._terminate_polling = True

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


def test_the_output_button_follows_the_driver_on_each_reading(qapp):
    """make_safe switches the TEC off from the worker thread after an abort;
    the tab must show what the driver knows, not what was last clicked."""
    controller = TCMControllerSimulation(channels=1)
    widget = gui.TemperatureControlWidget(controller)
    try:
        controller._terminate_polling = True
        channel = widget.plot_widgets[0]
        controller.set_output_enabled(1, True)
        controller._publish()
        assert channel.output_btn.isChecked()
        controller.set_output_enabled(1, False)
        controller._publish()
        assert not channel.output_btn.isChecked()
    finally:
        widget.deleteLater()
        controller.close()
