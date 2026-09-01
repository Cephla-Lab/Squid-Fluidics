"""Live sensor plot widgets (temperature channels, flow sensors): rolling window,
query-interval throttle, and a per-plot CSV recording lifecycle. Extracted from gui.py
so an embedding application (Squid) can import them without the standalone app."""

import csv
import logging
import os
import re
from datetime import datetime

from qtpy.QtCore import Signal
from qtpy.QtWidgets import (QWidget, QGroupBox, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                            QPushButton, QSpinBox, QComboBox, QFileDialog, QMessageBox)

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

from fluidics.qt.support import subscribe_until_detached

_logger = logging.getLogger("fluidics.gui")


def _safe_filename_part(text):
    """Reduce free-form text to something safe to embed in a filename."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", text).strip("._")
    return cleaned or "sensor"


class MplCanvas(FigureCanvasQTAgg):
    def __init__(self, parent=None, width=5, height=4, dpi=100):
        fig = Figure(figsize=(width, height), dpi=dpi)
        self.axes = fig.add_subplot(111)
        super(MplCanvas, self).__init__(fig)


class TimeSeriesPlotWidget(QWidget):
    """Shared scaffolding for the live sensor plots.

    Owns the rolling time window, the query-interval throttle, and the CSV
    recording lifecycle. Subclasses own their data series and how they draw
    them, via the four hooks below.
    """

    # Where the save dialog opens; every plot shares it, so consecutive
    # recordings land next to each other. Session-only, no persistence.
    _last_record_dir = os.getcwd()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.times = []
        self.query_interval = 1
        self.window_size = 60
        self.last_update = 0
        self.file = None
        self.writer = None

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

    def _build_plot_box(self, title, min_interval):
        """Build the plot group: interval/window controls, canvas, record button.

        Sets self.interval_input, self.window_input, self.canvas, and
        self.record_btn, and wires them to the shared handlers.
        """
        self.query_interval = min_interval

        plot_box = QGroupBox(title)
        plot_layout = QVBoxLayout()

        plot_controls = QWidget()
        pc_layout = QHBoxLayout(plot_controls)
        pc_layout.addWidget(QLabel("Query Interval:"))
        self.interval_input = QSpinBox()
        self.interval_input.setMinimum(min_interval)
        self.interval_input.setValue(min_interval)
        self.interval_input.setSuffix(" s")
        pc_layout.addWidget(self.interval_input)
        pc_layout.addWidget(QLabel("Window Size:"))
        self.window_input = QSpinBox()
        self.window_input.setMinimum(10)
        self.window_input.setMaximum(3600)
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

    def _trim_window(self, *series):
        """Drop samples now outside the window, keeping series aligned to times.

        The caller appends to self.times and its own lists, then names those
        lists here. Keeping the append and the trim at one call site is why
        there is no _series() hook: a hook would split the correspondence
        across two methods, where a wrong order silently misfiles values.
        """
        while self.times and self.times[-1] - self.times[0] > self.window_size:
            self.times.pop(0)
            for data in series:
                data.pop(0)

    def _finalize_plot(self, ax, ylabel, title):
        """Apply the shared axis treatment and draw."""
        current_time = self.times[-1]
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
                self.file = open(path, "w", newline="")
            except OSError as e:
                QMessageBox.critical(self, "Recording Not Started",
                                     f"Could not create {path}:\n{e}")
                return
            TimeSeriesPlotWidget._last_record_dir = os.path.dirname(path)
            self.writer = csv.writer(self.file)
            self.writer.writerow(self._record_header())
            self.record_btn.setText("Stop Recording")
        else:
            self.record_btn.setText("Start Recording")
            saved_path = self.close_recording()
            if saved_path:
                QMessageBox.information(self, "Recording Saved",
                                        f"Recording saved to:\n{saved_path}")

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

    reading_signal = Signal(float, float)  # (temp, current_time)

    def __init__(self, controller, channel, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.channel = channel  # 1-based

        self.temps = []
        self.targets = []

        self.reading_signal.connect(self._on_reading)

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

        # Temperature moves slowly; polling faster than 2 s buys nothing.
        plot_box = self._build_plot_box(f"Channel {self.channel} Plot", min_interval=2)

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
        self.times.append(current_time)
        self.temps.append(temp)
        self.targets.append(target)
        self._trim_window(self.temps, self.targets)
        if self.writer is not None:
            self.writer.writerow([datetime.fromtimestamp(current_time), temp, target])
        self._refresh_plot()
        self.last_update = current_time

    def _refresh_plot(self):
        if not self.temps or not self.times:
            return
        ax = self.canvas.axes
        ax.clear()
        ax.plot(self.times, self.temps, "b-", label="Actual")
        ax.plot(self.times, self.targets, "r--", label="Target")
        limits = self._padded_limits(self.temps + self.targets)
        if limits:
            ax.set_ylim(list(limits))
        ax.legend()
        self._finalize_plot(ax, "Temperature (°C)", f"Channel {self.channel} Temperature")

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

class TemperatureControlWidget(SensorTabWidget):
    """Container that lays out one TemperatureChannelWidget per channel."""

    readings_signal = Signal(list)  # list[float] of length controller.channels

    def __init__(self, controller):
        super().__init__()
        self.controller = controller

        layout = QHBoxLayout(self)
        for c in range(1, controller.channels + 1):
            cw = TemperatureChannelWidget(controller, c)
            self.plot_widgets.append(cw)
            layout.addWidget(cw)

        self.readings_signal.connect(self._fanout)
        # Just another subscriber; the GUI starts the publisher because its
        # plots are what consume it (see TCMController.start). Detached on
        # destroyed: an embedded widget must not outlive itself.
        detach = subscribe_until_detached((self.controller, self._on_callback))
        self.destroyed.connect(detach)
        self.controller.start()

    def _on_callback(self, temps):
        # Runs in the controller's polling thread; marshal to the GUI thread.
        self.readings_signal.emit(list(temps))

    def _fanout(self, temps):
        current_time = datetime.now().timestamp()
        for cw, t in zip(self.plot_widgets, temps):
            cw.reading_signal.emit(t, current_time)


class FlowSensorWidget(TimeSeriesPlotWidget):
    """One flow sensor's readout, plot, and CSV recording."""

    reading_signal = Signal(object, float)  # (flow_ul_min or None, timestamp)
    fault_signal = Signal(str, object, float)  # (mode, FlowFault, timestamp)

    def __init__(self, sensor, draw_protection=True, parent=None):
        super().__init__(parent)
        self.sensor = sensor
        self.draw_protection = draw_protection

        self.flows = []

        self.reading_signal.connect(self._on_reading)
        self.fault_signal.connect(self._on_fault)
        self._build_ui()
        # Detached on destroyed: unsubscribe removes by identity, so the exact
        # bound callbacks are retained (an embedded widget must not outlive itself).
        reading_cb, fault_cb = self._on_callback, self._on_fault_callback
        sensor.subscribe(reading_cb)
        sensor.subscribe_faults(fault_cb)

        def detach():
            sensor.unsubscribe(reading_cb)
            sensor.unsubscribe_faults(fault_cb)

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

        plot_box = self._build_plot_box("Plot", min_interval=1)

        layout.addWidget(readout)
        layout.addWidget(plot_box)

    def _on_monitor_changed(self, mode):
        self.sensor.monitor = mode
        _logger.info("Flow sensor %r: draw protection set to %s.",
                     self.sensor.name, mode)

    def _on_callback(self, flow, timestamp):
        # Runs in the controller's reader thread; marshal to the GUI thread.
        self.reading_signal.emit(flow, timestamp)

    def _on_fault_callback(self, mode, fault, timestamp):
        # Runs in the controller's reader thread; marshal to the GUI thread.
        self.fault_signal.emit(mode, fault, timestamp)

    def _on_fault(self, mode, fault, timestamp):
        # A draw-protection trip, filed beside the readings it was judged
        # from. This is the only durable trace a `warn` fault leaves: the
        # progress-bar notice is cleared at the next run and stdout may not
        # exist. Its own row rather than a mark on the nearest reading, so the
        # verdict carries the tripping sample's timestamp even if the reading
        # row it belongs to was written a queue-slot earlier.
        if self.writer is not None:
            self.writer.writerow([datetime.fromtimestamp(timestamp), "",
                                  f"{mode}: {fault}"])

    def _on_reading(self, flow, current_time):
        # Write every sample to CSV regardless of the plot's query interval:
        # interval_input bottoms out at 1 Hz, which is roughly one sample in
        # every 17 at the 60 ms packet cadence and would erase anything the
        # recording is actually meant to catch (e.g. a ~180 ms dropout).
        if self.writer is not None:
            self.writer.writerow([datetime.fromtimestamp(current_time),
                                  "" if flow is None else f"{flow:.2f}", ""])

        if current_time - self.last_update < self.query_interval:
            return

        if flow is None:
            self.flow_label.setText("invalid")
        else:
            self.flow_label.setText(f"{flow:.1f} µL/min")

        # None is appended as-is: matplotlib renders it as a gap, which is
        # what an invalid reading should look like rather than a 3276.7 spike.
        self.times.append(current_time)
        self.flows.append(flow)
        self._trim_window(self.flows)

        self._refresh_plot()
        self.last_update = current_time

    def _refresh_plot(self):
        if not self.times:
            return
        ax = self.canvas.axes
        ax.clear()
        ax.plot(self.times, self.flows, "b-")

        # Scale to the real readings only; invalid samples carry no magnitude.
        limits = self._padded_limits([f for f in self.flows if f is not None])
        if limits:
            ax.set_ylim(list(limits))

        self._finalize_plot(ax, "Flow Rate (µL/min)", self.sensor.name)


class FlowSensorControlWidget(SensorTabWidget):
    """Container laying out one FlowSensorWidget per configured sensor."""

    def __init__(self, sensors, draw_protection=True):
        super().__init__()
        layout = QHBoxLayout(self)
        for sensor in sensors:
            sw = FlowSensorWidget(sensor, draw_protection=draw_protection)
            self.plot_widgets.append(sw)
            layout.addWidget(sw)
