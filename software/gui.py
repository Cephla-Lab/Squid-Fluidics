import argparse
import logging
import os
import sys
# Through qtpy, like fluidics.qt: two Qt bindings in one process is a crash,
# and qtpy resolves the binding once for the whole application (QT_API).
from qtpy.QtWidgets import (QApplication, QMainWindow, QTabWidget,
                            QFileDialog, QMessageBox)
from qtpy.QtCore import QSettings

from serial import SerialException

from fluidics.control.config import (
    DEFAULT_CONFIG_PATHS, available_port_count, default_config_path,
    load_config, port_key, save_config,
)
from fluidics.errors import DeviceError
from fluidics.devices import (
    ISSUE_FLOW_SENSORS,
    ISSUE_TEMPERATURE_CONTROLLER,
)
from fluidics.system import FluidicsSystem
from fluidics.events import RunEnded  # noqa: F401  (re-exported; see below)
from fluidics.run_log import setup_uncaught_exception_logging, start_log_file

import warnings
warnings.filterwarnings('ignore')

_logger = logging.getLogger("fluidics.gui")

# The widgets live in the importable fluidics.qt subpackage; this module
# arranges them. Names this file does not use itself are re-exported on
# purpose -- `gui.SequencesWidget` and friends are what the tests and any
# older script reach for -- so a linter calling them unused is wrong, and
# removing them breaks the suite.
from fluidics.qt.support import (WorkerEvent, PostsToQtThread, GuiLogHandler,  # noqa: E402,F401
                                 _ask_yes_no, _hms)
from fluidics.qt.sequence_editor import AddSequenceDialog, SequencesWidget  # noqa: E402,F401
from fluidics.qt.manual_control import PortNamesDialog, ManualControlWidget  # noqa: E402,F401
from fluidics.qt.sensor_plots import (_safe_filename_part, TimeSeriesPlotWidget,  # noqa: E402,F401
                                      TemperatureControlWidget, FlowSensorWidget,
                                      FlowSensorControlWidget)

def pick_config(cli_path=None):
    """The rig config to run with, loaded: the --config path if given, else
    the rig's own local config (default_config_path), else the last file an
    operator picked; when none of those exists, or one fails to load, a
    dialog asks instead of a traceback. Returns the loaded config (whose
    source_path is where save_config writes renames back), or None if the
    operator cancelled. The conventional local file outranks the remembered
    one on purpose: a rig's ./config.yaml is the rig's.
    """
    settings = QSettings()
    remembered = settings.value("config_path")
    path = cli_path or default_config_path() or (
        remembered if remembered and os.path.exists(remembered) else None)
    while True:
        if path is None:
            path, _ = QFileDialog.getOpenFileName(
                None, "Select a rig config", "", "Config files (*.yaml *.json)")
            if not path:
                return None
        try:
            config = load_config(path)
        except Exception as e:
            QMessageBox.critical(None, "Config Error", f"Could not load {path}:\n\n{e}")
            path = None
        else:
            # Absolute, or the memory means "whatever directory I am run
            # from next time" -- inert exactly when it is needed.
            settings.setValue("config_path", os.path.abspath(path))
            return config


class FluidicsControlGUI(PostsToQtThread, QMainWindow):
    def __init__(self, config, is_simulation):
        super().__init__()
        self.config = config
        self.simulation = is_simulation
        # Tabs that own CSV recordings. closeEvent flushes them on exit, since
        # Qt never delivers a close event to a tab-embedded child widget.
        self.sensorTabs = []

        try:
            self.system = FluidicsSystem.build(self.config, self.simulation,
                                               on_issue=self._report_bringup_issue)
        except (DeviceError, SerialException) as e:
            # Nothing works without the controller or the pump, so there is
            # no window worth showing -- just the message. DeviceError covers
            # "not plugged in / wrong serial" and "present but stuck";
            # SerialException is the
            # sibling "plugged in but the port won't open" (permissions, a
            # stale process holding it) -- same operator problem, same dialog.
            QMessageBox.critical(self, "Device Unavailable", str(e))
            raise SystemExit(1)
        # The one job on the rig -- a run or a manual move -- for both tabs.
        self.session = self.system.session
        self.session.state.subscribe(lambda kind: self._post_event("_renderTabs", kind))
        self.temperatureController = self.system.devices.temperature_controller
        self.flowSensors = self.system.devices.flow_sensors

        self.initUI()

    def initUI(self):
        self.setWindowTitle("Fluidics Control System")
        self.setGeometry(100, 100, 950, 600)

        # Create tab widget
        self.tabWidget = QTabWidget()

        # "Settings and Manual Control" tab
        runExperimentsTab = SequencesWidget(self.config, self.system)
        manualControlTab = ManualControlWidget(self.config, self.system)
        # TODO: integrate temperature controller ui

        self.tabWidget.addTab(runExperimentsTab, "Run Experiments")
        self.tabWidget.addTab(manualControlTab, "Settings and Manual Control")
        if self.temperatureController is not None:
            temperatureControlTab = TemperatureControlWidget(self.temperatureController)
            self.tabWidget.addTab(temperatureControlTab, "Temperature Control")
            self.sensorTabs.append(temperatureControlTab)

        if self.flowSensors:
            draw_protection = self.config.application == "Flow Cell"
            self._warn_if_draw_protection_unavailable(draw_protection)
            flowSensorTab = FlowSensorControlWidget(self.flowSensors,
                                                    draw_protection=draw_protection)
            self.tabWidget.addTab(flowSensorTab, "Flow Sensors")
            self.sensorTabs.append(flowSensorTab)

        self.setCentralWidget(self.tabWidget)

    # What build_devices reports through on_issue is entry-point-neutral; the
    # dialog title and the "which tab you will be missing" guidance are the
    # GUI's to add.
    _BRINGUP_HINTS = {
        ISSUE_TEMPERATURE_CONTROLLER: (
            "Temperature Controller",
            "\n\nCheck that the serial number in config.yaml matches a "
            "connected device. The Temperature Control tab will not be "
            "available."),
        ISSUE_FLOW_SENSORS: (
            "Flow Sensor",
            "\n\nCheck that the sensor is connected to the matching I2C "
            "index. The Flow Sensor tab will not be available."),
    }

    def _report_bringup_issue(self, kind, message):
        _logger.warning(message)
        title, hint = self._BRINGUP_HINTS.get(kind, ("Hardware", ""))
        QMessageBox.warning(self, title, message + hint)

    def _warn_if_draw_protection_unavailable(self, draw_protection):
        """Say so loudly when a configured mode will not be acted on.

        Only MERFISHOperations arms the sensors, so on any other application a
        config asking for warn or stop is inert. Silence there would leave the
        operator believing a draw is protected when nothing is watching it.
        """
        if draw_protection:
            return
        configured = [s.name for s in self.flowSensors if s.monitor != "off"]
        if not configured:
            return
        for sensor in self.flowSensors:
            sensor.monitor = "off"
        msg = (f"Draw protection is configured for {', '.join(configured)} but "
               f"is only available for the Flow Cell application. The sensors "
               f"will read and plot; they will not stop a draw.")
        _logger.warning(msg)
        QMessageBox.warning(self, "Flow Sensor", msg)

    RUN_TAB, MANUAL_TAB = 0, 1

    def _renderTabs(self, kind):
        """The tab that did not start the job goes dead while it runs: a run
        must not start under a manual move (the operations' valve turn would
        pass its gate and change the reagent under a live draw), nor a manual
        move under a run."""
        self.tabWidget.setTabEnabled(self.RUN_TAB, kind != "manual")
        self.tabWidget.setTabEnabled(self.MANUAL_TAB, kind != "run")

    def _quiesce(self):
        """A live job must end before the devices close (system.close does
        that); the operator gets to say. True if the window may close."""
        if not self.session.busy:
            return True
        what = "run" if self.session.kind == "run" else "manual move"
        return _ask_yes_no(self, "Still running",
                           f"A {what} is in progress. Abort it and exit?")

    def closeEvent(self, event):
        if not self._quiesce():
            event.ignore()
            return

        # Qt only sends QCloseEvent to top-level windows, so a tab embedded in
        # self.tabWidget never gets one. Flush their CSV recordings here so
        # quitting mid-recording doesn't leave a file handle dangling.
        for tab in self.sensorTabs:
            tab.close_recordings()

        self.system.close(timeout=10)
        super().closeEvent(event)


if __name__ == '__main__':
    setup_uncaught_exception_logging()
    # One file per GUI session: bring-up, manual moves, and every run land in
    # it. Closed by logging's own shutdown at exit.
    start_log_file()
    parser = argparse.ArgumentParser()
    parser.add_argument("--simulation", help="Run the GUI with simulated hardware.", action='store_true')
    parser.add_argument("--config", help="Rig config, YAML or legacy JSON (default: "
                        f"{' then '.join(DEFAULT_CONFIG_PATHS)}, then the last "
                        "file picked; asks otherwise).")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    # The identity every QSettings() in the process stores under.
    QCoreApplication.setOrganizationName("Cephla")
    QCoreApplication.setApplicationName("FluidicsControl")
    config = pick_config(args.config)
    if config is None:
        sys.exit(1)
    gui = FluidicsControlGUI(config, args.simulation)
    gui.show()
    sys.exit(app.exec_())
