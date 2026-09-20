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
def temperature_widget(qapp):
    """Build a widget over the simulation with `channels` channels, the
    driver's poll loop stopped so the only publisher left is the
    synchronous one the test drives; the driver is closed after the
    widget.

    The poll thread is joined and its queued readings delivered before the
    test starts, so nothing it published on the way out can be mistaken for
    what the test publishes.
    """
    built = []

    def build(channels=1):
        controller = TCMControllerSimulation(channels=channels)
        widget = TemperatureControlWidget(controller)
        assert controller._polling_started   # the constructor starts it
        controller._terminate_polling = True
        controller._polling_thread.join(5)
        assert not controller._polling_thread.is_alive(), \
            "the poll loop is still publishing"
        deliver_posted_events()
        built.append((controller, widget))
        return controller, widget

    yield build
    for controller, widget in built:
        widget.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        controller.close()


@pytest.fixture
def channel_widget(temperature_widget):
    """The one-channel case, as (controller, its single channel widget)."""
    controller, widget = temperature_widget()
    return controller, widget.plot_widgets[0]


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


class TestOutputReadout:
    """What the TEC is actually driving, beside what it was told to."""

    def test_it_shows_what_the_driver_last_read(self, channel_widget):
        controller, channel = channel_widget
        controller.output_voltages = [3.214]
        controller.output_currents = [-1.05]
        controller._publish()
        deliver_posted_events()
        assert channel.voltage_label.text() == "3.21 V"
        assert channel.current_label.text() == "-1.05 A"

    def test_a_read_the_unit_did_not_answer_is_a_dash(self, channel_widget):
        controller, channel = channel_widget
        controller.output_voltages = [None]
        controller.output_currents = [None]
        controller._publish()
        deliver_posted_events()
        assert channel.voltage_label.text() == "--"
        assert channel.current_label.text() == "--"

    def test_it_is_not_held_to_the_plot_s_query_interval(self, channel_widget):
        """Live status, like the output button: a reading the plot skips
        still moves it."""
        controller, channel = channel_widget
        channel.last_update = float("inf")   # no reading is ever due
        controller.output_voltages = [1.5]
        controller._publish()
        deliver_posted_events()
        assert channel.voltage_label.text() == "1.50 V"

    def test_each_channel_shows_its_own(self, temperature_widget):
        controller, widget = temperature_widget(channels=2)
        controller.output_voltages = [1.0, 2.0]
        controller._publish()
        deliver_posted_events()
        assert [c.voltage_label.text() for c in widget.plot_widgets] == \
            ["1.00 V", "2.00 V"]


class TestSetControlsEnabled:
    """An embedder's run owns the TEC: the setpoint controls freeze while
    the plot and its recording carry on."""

    def _controls(self, channel):
        return [channel.temp_input, channel.set_btn, channel.save_btn,
                channel.output_btn]

    def test_the_setpoint_controls_freeze_and_come_back(self, channel_widget):
        _controller, channel = channel_widget
        channel.setControlsEnabled(False)
        assert not any(c.isEnabled() for c in self._controls(channel))
        channel.setControlsEnabled(True)
        assert all(c.isEnabled() for c in self._controls(channel))

    def test_the_plot_and_its_recording_are_left_alone(self, channel_widget):
        _controller, channel = channel_widget
        channel.setControlsEnabled(False)
        assert channel.record_btn.isEnabled(), "the recording froze with them"
        assert channel.interval_input.isEnabled()
        assert channel.window_input.isEnabled()
        assert channel.voltage_label.isEnabled(), "the readout froze too"
        assert channel.current_label.isEnabled()

    def test_the_container_reaches_every_channel(self, temperature_widget):
        """Two channels, so freezing one twice cannot pass for freezing
        both."""
        _controller, widget = temperature_widget(channels=2)
        widget.setControlsEnabled(False)
        assert [c.temp_input.isEnabled() for c in widget.plot_widgets] == \
            [False, False]
