"""Live sensor plot widgets (temperature channels, flow sensors): rolling window,
query-interval throttle, and a per-plot CSV recording lifecycle. Extracted from gui.py
so an embedding application (Squid) can import them without the standalone app."""

import csv
import logging
import os
import re
import time
from datetime import datetime

from qtpy.QtWidgets import (QWidget, QGroupBox, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                            QPushButton, QSpinBox, QComboBox, QFileDialog, QMessageBox)

# backend_qtagg, not backend_qt5agg: the qt5 spelling pins the Qt5
# binding (and forces it outright on matplotlib >= 3.6), which is the
# two-bindings-in-one-process crash qtpy is here to avoid.
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

from fluidics.qt.support import PostsToQtThread, subscribe_until_detached
from fluidics.sensor_recorder import FLUSH_INTERVAL_SECONDS, SensorSeries

_logger = logging.getLogger("fluidics.gui")


# The longest window the operator can ask for, and so exactly how much
# history a plot's series needs to hold: appends are throttled to at most
# one a second, so a sample per second covers it.
MAX_WINDOW_SECONDS = 3600


def _safe_filename_part(text):
    """Reduce free-form text to something safe to embed in a filename."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", text).strip("._")
    return cleaned or "sensor"


class MplCanvas(FigureCanvasQTAgg):
    def __init__(self, parent=None, width=5, height=4, dpi=100):
        fig = Figure(figsize=(width, height), dpi=dpi)
        self.axes = fig.add_subplot(111)
        super(MplCanvas, self).__init__(fig)


class TimeSeriesPlotWidget(PostsToQtThread, QWidget):
    """Shared scaffolding for the live sensor plots.

    Owns the rolling time window, the query-interval throttle, and the CSV
    recording lifecycle. Subclasses own their data series -- SensorSeries,
    the one sample buffer in this package -- and how they draw them, via
    the three hooks below. Readings arrive on device threads and cross to
    the Qt thread through _post_event, the idiom the other fluidics.qt
    widgets already use.
    """

    # Where the save dialog opens; every plot shares it, so consecutive
    # recordings land next to each other. Session-only, no persistence.
    _last_record_dir = os.getcwd()

    def __init__(self, min_interval, parent=None):
        super().__init__(parent)
        self.query_interval = min_interval
        self.window_size = 60
        self.last_update = 0
        self.file = None
        self.writer = None
        self._flushed_at = 0.0
        # A recording outlives nothing: an embedded widget can be destroyed
        # mid-recording, and close_recordings() only runs when the
        # standalone window closes -- the buffered tail would never land.
        self.destroyed.connect(lambda: self.close_recording())

    # --- subclass hooks ---

    def _record_filename(self):
        """Filename for a new CSV recording."""
        raise NotImplementedError

    def _record_header(self):
        """Header row for a new CSV recording."""
        raise NotImplementedError

    def _refresh_plot(self):
        """Redraw the canvas. Implementations end with _finalize_plot()."""
        raise NotImplementedError

    # --- shared UI ---

    def _build_plot_box(self, title):
        """Build the plot group: interval/window controls, canvas, record button.

        Sets self.interval_input, self.window_input, self.canvas, and
        self.record_btn, and wires them to the shared handlers.
        """
        plot_box = QGroupBox(title)
        plot_layout = QVBoxLayout()

        plot_controls = QWidget()
        pc_layout = QHBoxLayout(plot_controls)
        pc_layout.addWidget(QLabel("Query Interval:"))
        self.interval_input = QSpinBox()
        self.interval_input.setMinimum(self.query_interval)
        self.interval_input.setValue(self.query_interval)
        self.interval_input.setSuffix(" s")
        pc_layout.addWidget(self.interval_input)
        pc_layout.addWidget(QLabel("Window Size:"))
        self.window_input = QSpinBox()
        self.window_input.setMinimum(10)
        self.window_input.setMaximum(MAX_WINDOW_SECONDS)
        self.window_input.setValue(self.window_size)
        self.window_input.setSuffix(" s")
        pc_layout.addWidget(self.window_input)
        plot_layout.addWidget(plot_controls)

        self.canvas = MplCanvas(self, width=5, height=4, dpi=100)
        plot_layout.addWidget(self.canvas)

        self.record_btn = QPushButton("Start Recording")
        plot_layout.addWidget(self.record_btn)
        plot_box.setLayout(plot_layout)

        self.record_btn.clicked.connect(self._toggle_record)
        self.interval_input.valueChanged.connect(self._set_interval)
        self.window_input.valueChanged.connect(self._set_window)

        return plot_box

    def _set_interval(self, value):
        self.query_interval = value

    def _set_window(self, value):
        self.window_size = value
        self._refresh_plot()

    # --- shared plotting ---

    def _finalize_plot(self, ylabel, title, current_time):
        """Apply the shared axis treatment and draw, anchored at the newest
        sample. Series are windowed at read time (SensorSeries.window), not
        trimmed on arrival, so widening the window shows the history that is
        already held instead of only what arrives after the change."""
        ax = self.canvas.axes
        ax.set_xlim([current_time - self.window_size, current_time])
        ax.set_xlabel("Seconds Ago")
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        ax.grid(True)
        # The x axis holds absolute timestamps but reads as "seconds ago", so
        # relabel through a formatter rather than set_xticklabels(): the latter
        # pins fixed strings to whatever ticks existed at call time, which
        # desynchronizes on resize or zoom and warns about a FixedFormatter
        # without a matching FixedLocator.
        ax.xaxis.set_major_formatter(
            FuncFormatter(lambda x, _pos: f"{current_time - x:.0f}"))
        self.canvas.draw()

    @staticmethod
    def _padded_limits(values):
        """y-limits with 10% headroom, or None if there is nothing to scale to."""
        if not values:
            return None
        y_min, y_max = min(values), max(values)
        padding = (y_max - y_min) * 0.1 if y_max != y_min else 1.0
        return y_min - padding, y_max + padding

    # --- shared recording ---

    def _toggle_record(self):
        if self.record_btn.text() == "Start Recording":
            default = os.path.join(TimeSeriesPlotWidget._last_record_dir,
                                   self._record_filename())
            path, _ = QFileDialog.getSaveFileName(
                self, "Save Recording", default, "CSV Files (*.csv)")
            if not path:
                return
            try:
                # utf-8, not the locale's: a fault row carries the sensor's
                # name and the fault's own text (micro signs, dashes).
                self.file = open(path, "w", newline="", encoding="utf-8")
            except OSError as e:
                QMessageBox.critical(self, "Recording Not Started",
                                     f"Could not create {path}:\n{e}")
                return
            TimeSeriesPlotWidget._last_record_dir = os.path.dirname(path)
            self.writer = csv.writer(self.file)
            self._flushed_at = 0.0          # the header flushes immediately
            self._write_row(self._record_header())
            self.record_btn.setText("Stop Recording")
        else:
            self.record_btn.setText("Start Recording")
            saved_path = self.close_recording()
            if saved_path:
                QMessageBox.information(self, "Recording Saved",
                                        f"Recording saved to:\n{saved_path}")

    def _write_row(self, row):
        """One row of the recording, flushed on a cadence rather than per
        row -- a flow sensor writes ~17 a second, and what a crash costs
        should be bounded all the same. The same bound SensorRecorder
        keeps, from the same constant."""
        if self.writer is None:
            return
        self.writer.writerow(row)
        now = time.monotonic()
        if self.file is not None and now - self._flushed_at >= FLUSH_INTERVAL_SECONDS:
            self.file.flush()
            self._flushed_at = now

    def close_recording(self):
        """Close the recording if one is open; returns its path, else None."""
        if self.file is None:
            return None
        saved_path = self.file.name
        self.file.close()
        self.file = None
        self.writer = None
        return saved_path


class SensorTabWidget(QWidget):
    """Container laying out one plot widget per channel or sensor.

    Qt delivers closeEvent only to top-level windows, so a tab embedded in a
    QTabWidget never gets one. FluidicsControlGUI.closeEvent calls
    close_recordings() on each tab explicitly instead.
    """

    def __init__(self):
        super().__init__()
        self.plot_widgets = []

    def close_recordings(self):
        for widget in self.plot_widgets:
            widget.close_recording()


class TemperatureChannelWidget(TimeSeriesPlotWidget):
    """One channel's worth of temperature UI: target/actual readout, plot,
    record toggle, query interval, window size."""

    def __init__(self, controller, channel, parent=None):
        # Temperature moves slowly; polling faster than 2 s buys nothing.
        super().__init__(min_interval=2, parent=parent)
        self.controller = controller
        self.channel = channel  # 1-based

        # One series of (actual, target) pairs rather than two series kept
        # in step: they are written together and drawn together, so the
        # alignment is the data's shape and not a rule about appending.
        self.readings = SensorSeries(maxlen=MAX_WINDOW_SECONDS)

        self._build_ui()
        self.temp_input.setText(f"{self.controller.target_temperatures[channel - 1]:.2f}")

    def _record_filename(self):
        return f"temp_ch{self.channel}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    def _record_header(self):
        return ["Time", "Actual Temperature", "Target Temperature"]

    def _build_ui(self):
        layout = QVBoxLayout(self)

        control = QGroupBox(f"Channel {self.channel} Control")
        control_layout = QVBoxLayout()

        row = QHBoxLayout()
        self.temp_label = QLabel("0.0°C")
        self.temp_input = QLineEdit()
        self.set_btn = QPushButton("Set")
        self.save_btn = QPushButton("Save")
        self.output_btn = QPushButton("Output OFF")
        self.output_btn.setCheckable(True)
        row.addWidget(QLabel("Current:"))
        row.addWidget(self.temp_label)
        row.addWidget(QLabel("Target:"))
        row.addWidget(self.temp_input)
        row.addWidget(QLabel("°C"))
        row.addWidget(self.set_btn)
        row.addWidget(self.save_btn)
        row.addWidget(self.output_btn)
        control_layout.addLayout(row)
        control.setLayout(control_layout)

        plot_box = self._build_plot_box(f"Channel {self.channel} Plot")

        layout.addWidget(control)
        layout.addWidget(plot_box)

        self.set_btn.clicked.connect(self._set_clicked)
        self.save_btn.clicked.connect(self._save_clicked)
        self.output_btn.toggled.connect(self._on_output_toggled)

        self._sync_output_button()

    def _on_reading(self, temp, current_time):
        # The driver's output state can change under a run (make_safe
        # switches it off after an abort); follow it rather than assume it.
        self._sync_output_button()
        if current_time - self.last_update < self.query_interval:
            return
        self.temp_label.setText(f"{temp:.1f}°C")
        target = self.controller.target_temperatures[self.channel - 1]
        self.readings.append((temp, target), current_time)
        self._write_row([datetime.fromtimestamp(current_time), temp, target])
        self._refresh_plot()
        self.last_update = current_time

    def _refresh_plot(self):
        times, readings = self.readings.window(self.window_size)
        if not times:
            return
        temps = [actual for actual, _ in readings]
        targets = [target for _, target in readings]
        ax = self.canvas.axes
        ax.clear()
        ax.plot(times, temps, "b-", label="Actual")
        ax.plot(times, targets, "r--", label="Target")
        limits = self._padded_limits(temps + targets)
        if limits:
            ax.set_ylim(list(limits))
        ax.legend()
        self._finalize_plot("Temperature (°C)",
                            f"Channel {self.channel} Temperature", times[-1])

    def _set_clicked(self):
        try:
            t = float(self.temp_input.text())
            self.controller.set_target_temperature(self.channel, t)
        except ValueError:
            _logger.warning("Invalid temperature for channel %s", self.channel)

    def _save_clicked(self):
        self.controller.save_target_temperature(self.channel)

    def _sync_output_button(self):
        on = self.controller.output_enabled[self.channel - 1]
        self.output_btn.blockSignals(True)
        self.output_btn.setChecked(on)
        self.output_btn.blockSignals(False)
        self.output_btn.setText("Output ON" if on else "Output OFF")

    def _on_output_toggled(self, checked):
        try:
            self.controller.set_output_enabled(self.channel, checked)
        except Exception as e:
            _logger.error("Failed to %s output on channel %s: %s",
                          "enable" if checked else "disable", self.channel, e)
        self._sync_output_button()

class TemperatureControlWidget(PostsToQtThread, SensorTabWidget):
    """Container that lays out one TemperatureChannelWidget per channel."""

    def __init__(self, controller):
        super().__init__()
        self.controller = controller

        layout = QHBoxLayout(self)
        for c in range(1, controller.channels + 1):
            cw = TemperatureChannelWidget(controller, c)
            self.plot_widgets.append(cw)
            layout.addWidget(cw)

        # Just another subscriber; the GUI starts the publisher because its
        # plots are what consume it (see TCMController.start). Detached on
        # destroyed: an embedded widget must not outlive itself.
        detach = subscribe_until_detached((self.controller, self._on_callback))
        self.destroyed.connect(detach)
        self.controller.start()

    def _on_callback(self, temps):
        # Runs in the controller's polling thread; marshal to the GUI thread.
        self._post_event("_fanout", list(temps))

    def _fanout(self, temps):
        # Already on the Qt thread: the channels are handed their reading
        # directly rather than posted a second time.
        current_time = datetime.now().timestamp()
        for cw, t in zip(self.plot_widgets, temps):
            cw._on_reading(t, current_time)


class FlowSensorWidget(TimeSeriesPlotWidget):
    """One flow sensor's readout, plot, and CSV recording."""

    def __init__(self, sensor, draw_protection=True, parent=None):
        super().__init__(min_interval=1, parent=parent)
        self.sensor = sensor
        self.draw_protection = draw_protection

        self.flows = SensorSeries(maxlen=MAX_WINDOW_SECONDS)

        self._build_ui()
        # Detached on destroyed, like every other widget here: an embedded
        # one must not outlive itself on a channel the rig keeps publishing.
        detach = subscribe_until_detached((sensor, self._on_callback),
                                          (sensor.faults, self._on_fault_callback))
        self.destroyed.connect(detach)

    def _record_filename(self):
        # sensor.name is free-form config text; keep it out of the path itself
        # so a name containing a separator writes here rather than elsewhere.
        return f"flow_{_safe_filename_part(self.sensor.name)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    def _record_header(self):
        return ["Time", "Flow Rate (uL/min)", "Fault"]

    def _build_ui(self):
        layout = QVBoxLayout(self)

        readout = QGroupBox(f"{self.sensor.name} (I2C index {self.sensor.index})")
        readout_layout = QHBoxLayout()
        self.flow_label = QLabel("--")
        readout_layout.addWidget(QLabel("Flow rate:"))
        readout_layout.addWidget(self.flow_label)
        readout_layout.addStretch()

        # Draw protection, switchable mid-run. Each draw reads the mode once
        # when it arms, so changing this affects the next draw, not the one
        # already running. Being able to move a sensor from warn to stop
        # without restarting is the point: the tolerance and ramp-up that suit
        # a given setup are found by watching warnings, and a restart to try
        # the next value costs a whole run.
        self.monitor_combo = QComboBox()
        self.monitor_combo.addItems(["off", "warn", "stop"])
        self.monitor_combo.setCurrentText(self.sensor.monitor)
        self.monitor_combo.setToolTip(
            "off: plot only.\n"
            "warn: log a flow fault and carry on.\n"
            "stop: halt the draw and fail the sequence."
        )
        self.monitor_combo.currentTextChanged.connect(self._on_monitor_changed)
        if not self.draw_protection:
            # Only MERFISHOperations arms the sensors. Leaving the control live
            # on an application that consumes nothing would offer the operator
            # a safety switch wired to nothing, which is worse than not
            # offering it -- so show what it actually is: off, and unavailable.
            self.monitor_combo.setCurrentText("off")
            self.monitor_combo.setEnabled(False)
            self.monitor_combo.setToolTip(
                "Draw protection is only available for the Flow Cell "
                "application."
            )
        readout_layout.addWidget(QLabel("Draw protection:"))
        readout_layout.addWidget(self.monitor_combo)
        readout.setLayout(readout_layout)

        plot_box = self._build_plot_box("Plot")

        layout.addWidget(readout)
        layout.addWidget(plot_box)

    def _on_monitor_changed(self, mode):
        self.sensor.monitor = mode
        _logger.info("Flow sensor %r: draw protection set to %s.",
                     self.sensor.name, mode)

    def _on_callback(self, flow, timestamp):
        # Runs in the controller's reader thread; marshal to the GUI thread.
        self._post_event("_on_reading", flow, timestamp)

    def _on_fault_callback(self, mode, fault, timestamp):
        # Runs in the controller's reader thread; marshal to the GUI thread.
        self._post_event("_on_fault", mode, fault, timestamp)

    def _on_fault(self, mode, fault, timestamp):
        # A draw-protection trip, filed beside the readings it was judged
        # from. This is the only durable trace a `warn` fault leaves: the
        # progress-bar notice is cleared at the next run and stdout may not
        # exist. Its own row rather than a mark on the nearest reading, so the
        # verdict carries the tripping sample's timestamp even if the reading
        # row it belongs to was written a queue-slot earlier.
        self._write_row([datetime.fromtimestamp(timestamp), "",
                         f"{mode}: {fault}"])

    def _on_reading(self, flow, current_time):
        # Write every sample to CSV regardless of the plot's query interval:
        # interval_input bottoms out at 1 Hz, which is roughly one sample in
        # every 17 at the 60 ms packet cadence and would erase anything the
        # recording is actually meant to catch (e.g. a ~180 ms dropout).
        self._write_row([datetime.fromtimestamp(current_time),
                         "" if flow is None else f"{flow:.2f}", ""])

        if current_time - self.last_update < self.query_interval:
            return

        if flow is None:
            self.flow_label.setText("invalid")
        else:
            self.flow_label.setText(f"{flow:.1f} µL/min")

        # None is appended as-is: matplotlib renders it as a gap, which is
        # what an invalid reading should look like rather than a 3276.7 spike.
        self.flows.append(flow, current_time)

        self._refresh_plot()
        self.last_update = current_time

    def _refresh_plot(self):
        times, flows = self.flows.window(self.window_size)
        if not times:
            return
        ax = self.canvas.axes
        ax.clear()
        ax.plot(times, flows, "b-")

        # Scale to the real readings only; invalid samples carry no magnitude.
        limits = self._padded_limits([f for f in flows if f is not None])
        if limits:
            ax.set_ylim(list(limits))

        self._finalize_plot("Flow Rate (µL/min)", self.sensor.name, times[-1])


class FlowSensorControlWidget(SensorTabWidget):
    """Container laying out one FlowSensorWidget per configured sensor."""

    def __init__(self, sensors, draw_protection=True):
        super().__init__()
        layout = QHBoxLayout(self)
        for sensor in sensors:
            sw = FlowSensorWidget(sensor, draw_protection=draw_protection)
            self.plot_widgets.append(sw)
            layout.addWidget(sw)
