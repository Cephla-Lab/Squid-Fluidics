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

Same-thread emit means Qt delivers the signal synchronously (direct
connection), so no event loop needs to run; the cross-thread queued delivery
is Qt's contract, not ours to test.
"""

from datetime import datetime

import pytest

# Qt's offscreen platform and the shared qapp fixture live in tests/conftest.
import gui
from fluidics.control.flow_sensor import FlowSensorSimulation

from .test_gui_helpers import RecordingWriter, make_flow_fault


def test_a_fault_published_on_the_sensor_lands_in_the_recording(qapp):
    sensor = FlowSensorSimulation(index=1, name="syringe_draw")
    widget = gui.FlowSensorWidget(sensor, draw_protection=True)
    try:
        widget.writer = RecordingWriter()
        sensor.notify_fault("warn", make_flow_fault(), 100.06)
        rows = widget.writer.rows
        assert len(rows) == 1
        assert rows[0][0] == datetime.fromtimestamp(100.06)
        assert rows[0][2].startswith("warn: ")
    finally:
        widget.deleteLater()
        sensor.close()


def test_without_a_recording_a_fault_is_dropped_quietly(qapp):
    sensor = FlowSensorSimulation(index=1, name="syringe_draw")
    widget = gui.FlowSensorWidget(sensor, draw_protection=True)
    try:
        sensor.notify_fault("stop", make_flow_fault(), 100.06)  # must not raise
        assert widget.writer is None
    finally:
        widget.deleteLater()
        sensor.close()
