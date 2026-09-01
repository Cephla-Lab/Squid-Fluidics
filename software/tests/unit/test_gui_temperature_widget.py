# tests/unit/test_gui_temperature_widget.py
"""TemperatureControlWidget's subscription wiring, end to end -- the same
argument as test_gui_flow_widget's docstring, for this widget's three wiring
lines -- and the output button following the driver."""

import pytest

from qtpy.QtCore import QEvent
from qtpy.QtWidgets import QApplication

from fluidics.control.temperature_controller import TCMControllerSimulation
from fluidics.qt.sensor_plots import TemperatureControlWidget

from .test_gui_helpers import RecordingWriter, deliver_posted_events


@pytest.fixture
def channel_widget(qapp):
    """A one-channel widget over the simulation, with the driver's poll loop
    stopped so the only publisher left is the synchronous one the test
    drives; the driver is closed after the widget.

    The poll thread is joined and its queued readings delivered before the
    test starts, so nothing it published on the way out can be mistaken for
    what the test publishes.
    """
    controller = TCMControllerSimulation(channels=1)
    widget = TemperatureControlWidget(controller)
    assert controller._polling_started   # the constructor starts the publisher
    controller._terminate_polling = True
    controller._polling_thread.join(5)
    assert not controller._polling_thread.is_alive(), "the poll loop is still publishing"
    deliver_posted_events()
    try:
        yield controller, widget.plot_widgets[0]
    finally:
        widget.deleteLater()
        controller.close()


def test_a_publish_on_the_controller_lands_in_the_recording(channel_widget):
    controller, channel = channel_widget
    channel.writer = RecordingWriter()
    # The plot throttles by query_interval on the clock; make this reading
    # due rather than depend on the poll thread having advanced the fake
    # clock first.
    channel.last_update = 0.0
    controller.actual_temperatures = [25.0]
    controller._publish()
    deliver_posted_events()
    rows = channel.writer.rows
    assert len(rows) == 1
    assert rows[0][1] == 25.0


def test_the_output_button_follows_the_driver_on_each_reading(channel_widget):
    """make_safe switches the TEC off from the worker thread after an abort;
    the tab must show what the driver knows, not what was last clicked."""
    controller, channel = channel_widget
    controller.set_output_enabled(1, True)
    controller._publish()
    deliver_posted_events()
    assert channel.output_btn.isChecked()
    controller.set_output_enabled(1, False)
    controller._publish()
    deliver_posted_events()
    assert not channel.output_btn.isChecked()


class TestSetControlsEnabled:
    def test_channel_controls_follow_in_lockstep(self):
        from types import SimpleNamespace

        from fluidics.qt.sensor_plots import TemperatureChannelWidget, TemperatureControlWidget

        calls = []
        control = lambda name: SimpleNamespace(setEnabled=lambda on, n=name: calls.append((n, on)))  # noqa: E731
        stub = SimpleNamespace(
            temp_input=control("input"), set_btn=control("set"), save_btn=control("save"), output_btn=control("output")
        )
        TemperatureChannelWidget.set_controls_enabled(stub, False)
        assert calls == [("input", False), ("set", False), ("save", False), ("output", False)]

        class Channel(SimpleNamespace):
            set_controls_enabled = TemperatureChannelWidget.set_controls_enabled

        channel = Channel(**vars(stub))
        fanout = SimpleNamespace(plot_widgets=[channel, channel])
        calls.clear()
        TemperatureControlWidget.set_controls_enabled(fanout, True)
        assert [on for _n, on in calls] == [True] * 8
