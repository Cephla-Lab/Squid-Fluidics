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
# Imported from the package, not through gui: these widgets are meant to be
# usable without the standalone application, and a test that goes through
# gui.py would not notice the day they stop being.
from qtpy.QtCore import QEvent
from qtpy.QtWidgets import QApplication

from fluidics.control.flow_sensor import FlowSensorSimulation
from fluidics.qt.sensor_plots import FlowSensorWidget

from ..conftest import in_a_fresh_interpreter
from .test_gui_helpers import (RecordingWriter, deliver_posted_events,
                               make_flow_fault)


def test_a_fault_published_on_the_sensor_lands_in_the_recording(qapp):
    sensor = FlowSensorSimulation(index=1, name="syringe_draw")
    widget = FlowSensorWidget(sensor, draw_protection=True)
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
    widget = FlowSensorWidget(sensor, draw_protection=True)
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
    widget = FlowSensorWidget(sensor, draw_protection=True)
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


def test_a_destroyed_widget_closes_its_recording(qapp, tmp_path):
    """An embedded widget can be destroyed mid-recording -- the standalone
    window's close_recordings() never runs for it -- and the buffered tail
    would go with it."""
    import csv
    sensor = FlowSensorSimulation(index=1, name="syringe_draw")
    widget = FlowSensorWidget(sensor, draw_protection=True)
    path = tmp_path / "rec.csv"
    handle = open(path, "w", newline="", encoding="utf-8")
    widget.file = handle
    widget.writer = csv.writer(handle)
    widget._write_row(["Time", "Flow Rate (uL/min)", "Fault"])
    widget._write_row(["t", "500.00", ""])
    widget.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    sensor.close()
    assert handle.closed, "the recording was left open"
    assert len(path.read_text().strip().splitlines()) == 2, \
        "the buffered tail never reached disk"


def test_a_destroyed_widget_leaves_the_sensor_no_subscribers(qapp):
    """The detach is what lets an embedded tab close while the rig runs
    on: both channels must come back empty, not just the reading one."""
    sensor = FlowSensorSimulation(index=1, name="syringe_draw")
    try:
        widget = FlowSensorWidget(sensor, draw_protection=True)
        assert sensor._subscribers._callbacks and sensor.faults._callbacks
        widget.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        assert sensor._subscribers._callbacks == [], "readings still subscribed"
        assert sensor.faults._callbacks == [], "faults still subscribed"
    finally:
        sensor.close()


def test_the_widgets_import_without_the_standalone_app():
    """Squid imports these modules directly. gui.py must not be on the
    path to them."""
    dragged_in = in_a_fresh_interpreter(
        "import sys, fluidics.qt.sensor_plots, fluidics.qt.manual_control, "
        "fluidics.qt.sequence_editor; print('gui' in sys.modules)")
    assert dragged_in == "False", "the widgets dragged gui.py in"


def test_without_a_recording_a_fault_is_dropped_quietly(qapp):
    sensor = FlowSensorSimulation(index=1, name="syringe_draw")
    widget = FlowSensorWidget(sensor, draw_protection=True)
    try:
        sensor.notify_fault("stop", make_flow_fault(), 100.06)  # must not raise
        deliver_posted_events()
        assert widget.writer is None
    finally:
        widget.deleteLater()
        sensor.close()
