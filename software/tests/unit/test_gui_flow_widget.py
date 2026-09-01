# tests/unit/test_gui_flow_widget.py
"""The one widget test: FlowSensorWidget's fault wiring, end to end.

test_gui_helpers.py deliberately never constructs a widget, and for row
formatting that is enough -- but the fault column's delivery path is three
lines of wiring (subscribe_faults in __init__, the signal connect, the
marshal) with the CSV as its only consumer, and a mutation run showed all of
it can be deleted with the whole suite staying green. This file constructs
the real widget under Qt's offscreen platform to pin that path: a fault
published on the sensor must come out as a CSV row. The row's exact format is
TestFlowRecordingRows' contract, not ours -- assertions here stop at "the
right fault arrived at the right time".

The readings and faults cross to the Qt thread through _post_event, so a
test publishing on its own thread drains the queue before asserting
(deliver_posted_events) -- the same delivery the sensor's reader thread
gets, without an event loop.
"""

from datetime import datetime

import pytest

# Qt's offscreen platform and the shared qapp fixture live in tests/conftest.
import gui
from fluidics.control.flow_sensor import FlowSensorSimulation

from .test_gui_helpers import (RecordingWriter, deliver_posted_events,
                               make_flow_fault)


def test_a_fault_published_on_the_sensor_lands_in_the_recording(qapp):
    sensor = FlowSensorSimulation(index=1, name="syringe_draw")
    widget = gui.FlowSensorWidget(sensor, draw_protection=True)
    try:
        widget.writer = RecordingWriter()
        sensor.notify_fault("warn", make_flow_fault(), 100.06)
        deliver_posted_events()
        rows = widget.writer.rows
        assert len(rows) == 1
        assert rows[0][0] == datetime.fromtimestamp(100.06)
        assert rows[0][2].startswith("warn: ")
    finally:
        widget.deleteLater()
        sensor.close()


def test_a_fault_waits_for_the_qt_thread_rather_than_landing_inline(qapp):
    """The sensor publishes from its reader thread. The widget must take
    the fault on the Qt thread, so nothing touches the recording (or the
    canvas) from the thread that read the sample."""
    sensor = FlowSensorSimulation(index=1, name="syringe_draw")
    widget = gui.FlowSensorWidget(sensor, draw_protection=True)
    try:
        widget.writer = RecordingWriter()
        sensor.notify_fault("warn", make_flow_fault(), 100.06)
        assert widget.writer.rows == [], "the fault was written inline"
        deliver_posted_events()
        assert len(widget.writer.rows) == 1
    finally:
        widget.deleteLater()
        sensor.close()


def test_the_plot_draws_the_window_not_the_whole_buffer(qapp):
    """The series holds far more than the plot shows; what is drawn is the
    window the operator asked for, cut at read time."""
    sensor = FlowSensorSimulation(index=1, name="syringe_draw")
    widget = gui.FlowSensorWidget(sensor, draw_protection=True)
    try:
        widget.window_size = 10
        for n in range(100):
            widget.flows.append(float(n), t=1000.0 + n)
        widget._refresh_plot()
        drawn = list(widget.canvas.axes.lines[0].get_xdata())
        assert len(widget.flows.window()[0]) == 100, "the buffer keeps it all"
        assert drawn == [1000.0 + n for n in range(89, 100)], \
            "the plot drew outside its window"
    finally:
        widget.deleteLater()
        sensor.close()


def test_without_a_recording_a_fault_is_dropped_quietly(qapp):
    sensor = FlowSensorSimulation(index=1, name="syringe_draw")
    widget = gui.FlowSensorWidget(sensor, draw_protection=True)
    try:
        sensor.notify_fault("stop", make_flow_fault(), 100.06)  # must not raise
        deliver_posted_events()
        assert widget.writer is None
    finally:
        widget.deleteLater()
        sensor.close()
