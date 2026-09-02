"""The standalone manual-control widget and the port-names dialog.
Moved verbatim from gui.py; imports go through qtpy."""

import logging
import time

from qtpy.QtCore import Qt, QSignalBlocker, QTimer
from qtpy.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from fluidics.control.config import available_ports, port_key, save_config
from fluidics.qt.support import PostsToQtThread, subscribe_until_detached

_logger = logging.getLogger("fluidics.gui")


class PortNamesDialog(QDialog):
    """Rename the reagent ports: one row per port, blank means unnamed.
    After OK, result_mapping holds the config's name_mapping shape --
    {'port_<n>': name} for the named ports only -- and None before."""

    def __init__(self, parent, ports, name_mapping):
        super().__init__(parent)
        self.setWindowTitle("Port Names")
        self.result_mapping = None
        self._edits = []

        names = name_mapping or {}
        form_host = QWidget()
        form = QFormLayout(form_host)
        # One row per port the rig offers -- naming a position with no
        # line on it would name nothing.
        for port in ports:
            edit = QLineEdit()
            edit.setText(names.get(port_key(port), ""))
            self._edits.append((port, edit))
            form.addRow(f"Port {port}", edit)

        # A cascade offers a couple dozen ports; the dialog scrolls rather
        # than outgrowing the screen.
        scroll = QScrollArea()
        scroll.setWidget(form_host)
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(min(400, 28 * len(ports) + 20))

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(scroll)
        layout.addWidget(buttons)

    def accept(self):
        self.result_mapping = {
            port_key(port): name
            for port, edit in self._edits
            if (name := edit.text().strip())}
        super().accept()



class ManualControlWidget(PostsToQtThread, QWidget):
    """The operator's hand on the rig, one move at a time -- see _run."""

    def __init__(self, config, system):
        super().__init__()
        self.config = config
        self.system = system
        self.session = system.session
        self.manual = system.manual
        self._controls = []         # everything _run disables while a move runs

        self.progress_timer = QTimer(self)
        self.progress_timer.timeout.connect(self.updateProgress)
        # The plunger bar paints what the pump publishes after each of its
        # own readings; the GUI thread never touches the serial line. Detached
        # on destroyed: an embedded widget must not outlive itself.
        detach = subscribe_until_detached((system.devices.syringe_pump.held_volume, self._onHeldVolume))
        self.destroyed.connect(detach)

        self.initUI()

    def initUI(self):
        mainLayout = QVBoxLayout()
        mainLayout.setSpacing(10)

        # Selector Valve Control
        valveGroupBox = QGroupBox("Selector Valve Control")
        valveLayout = QHBoxLayout()
        valveLayout.setContentsMargins(5, 5, 5, 5)
        valveLayout.addWidget(QLabel("Source port:"))
        self.valveCombo = QComboBox()
        self._fillPorts()
        self.valveCombo.currentIndexChanged.connect(self.openValve)
        valveLayout.addWidget(self.valveCombo)
        self._controls.append(self.valveCombo)
        self.portNamesButton = QPushButton("Port Names…")
        self.portNamesButton.clicked.connect(self.editPortNames)
        valveLayout.addWidget(self.portNamesButton)
        self._controls.append(self.portNamesButton)
        valveGroupBox.setLayout(valveLayout)
        mainLayout.addWidget(valveGroupBox)

        if self.config.application == "Open Chamber":
            pumpGroupBox = QGroupBox("Disc Pump Control")
            pumpLayout = QHBoxLayout()
            pumpLayout.setContentsMargins(5, 5, 5, 5)
            pumpLayout.addWidget(QLabel("Operation time:"))
            self.pumpInput = QDoubleSpinBox()      # a spin box, not free text
            self.pumpInput.setRange(0.1, 600)
            self.pumpInput.setDecimals(1)
            self.pumpInput.setValue(10)
            self.pumpInput.setSuffix(" s")
            pumpLayout.addWidget(self.pumpInput)
            self.pumpButton = QPushButton("Start")
            pumpLayout.addWidget(self.pumpButton)
            self.pumpButton.clicked.connect(self.startDiscPump)
            self._controls += [self.pumpInput, self.pumpButton]
            pumpGroupBox.setLayout(pumpLayout)
            mainLayout.addWidget(pumpGroupBox)

        # Syringe Pump Control
        syringeGroupBox = QGroupBox("Syringe Pump Control")
        syringeLayout = QVBoxLayout()
        syringeLayout.setContentsMargins(5, 5, 5, 5)
        syringeLayout.setSpacing(5)

        topLayout = QHBoxLayout()

        # Left side controls
        leftWidget = QWidget()
        leftLayout = QGridLayout(leftWidget)
        self.syringePortCombo = QComboBox()
        self.syringePortCombo.addItems(map(str, self.config.syringe_pump.ports_allowed))
        leftLayout.addWidget(QLabel("Port:"), 0, 0)
        leftLayout.addWidget(self.syringePortCombo, 0, 1)

        self.speedCombo = QComboBox()
        # uL/min, matching what sequences are written in -- picking a speed
        # here and typing a flow_rate into a sequence use one scale. Slowest
        # last, and the default.
        for rate in self.manual.flow_rates():
            self.speedCombo.addItem(f"{rate:,.0f} µL/min", rate)
        self.speedCombo.setCurrentIndex(self.speedCombo.count() - 1)
        leftLayout.addWidget(QLabel("Speed:"), 1, 0)
        leftLayout.addWidget(self.speedCombo, 1, 1)

        self.volumeSpinBox = QSpinBox()
        self.volumeSpinBox.setRange(1, self.config.syringe_pump.volume_ul)
        self.volumeSpinBox.setSuffix(" μL")
        leftLayout.addWidget(QLabel("Volume:"), 2, 0)
        leftLayout.addWidget(self.volumeSpinBox, 2, 1)

        actionLayout = QHBoxLayout()
        self.pushButton = QPushButton("Extract")
        self.pushButton.clicked.connect(self.extractSyringe)
        self.pullButton = QPushButton("Dispense")
        self.pullButton.clicked.connect(self.dispenseSyringe)
        self.emptyButton = QPushButton("Empty to Waste")
        self.emptyButton.clicked.connect(self.emptySyringe)
        actionLayout.addWidget(self.pushButton)
        actionLayout.addWidget(self.pullButton)
        leftLayout.addLayout(actionLayout, 3, 0, 1, 2)
        leftLayout.addWidget(self.emptyButton)
        self._controls += [self.pushButton, self.pullButton, self.emptyButton,
                           self.syringePortCombo, self.speedCombo, self.volumeSpinBox]

        topLayout.addWidget(leftWidget, 3)

        # Right side - Plunger position
        rightWidget = QWidget()
        rightLayout = QVBoxLayout(rightWidget)
        self.plungerPositionLabel = QLabel("Plunger Position (μL)")
        rightLayout.addWidget(self.plungerPositionLabel, alignment=Qt.AlignHCenter)
        self.plungerPositionBar = QProgressBar()
        self.plungerPositionBar.setRange(0, self.config.syringe_pump.volume_ul)
        self.plungerPositionBar.setOrientation(Qt.Vertical)
        self.plungerPositionBar.setTextVisible(False)
        # The pump's last reading, no serial traffic (refresh=False).
        self.plungerPositionBar.setValue(int(self.manual.held_volume_ul(refresh=False)))
        rightLayout.addWidget(self.plungerPositionBar, alignment=Qt.AlignHCenter)

        topLayout.addWidget(rightWidget, 1)

        syringeLayout.addLayout(topLayout)

        progressLayout = QHBoxLayout()
        self.syringeProgressBar = QProgressBar()
        self.syringeProgressBar.setRange(0, 100)
        progressLayout.addWidget(self.syringeProgressBar, 1)
        # The one control live while a move runs: stops whatever it is.
        self.stopButton = QPushButton("Stop")
        self.stopButton.setEnabled(False)
        self.stopButton.clicked.connect(self.stopMove)
        progressLayout.addWidget(self.stopButton)
        syringeLayout.addWidget(QLabel("Execution Progress:"))
        syringeLayout.addLayout(progressLayout)

        syringeGroupBox.setLayout(syringeLayout)
        mainLayout.addWidget(syringeGroupBox)

        self.setLayout(mainLayout)

    # --- the controls: each picks a verb and hands it to _run ---

    def openValve(self):
        port = self.valveCombo.currentData()
        self._run(lambda: self.manual.open_port(port))

    def editPortNames(self):
        """Rename the reagent ports and write the rig's config back. Idle
        only: the names feed port lists a running job may be reading.

        Names live on the config object; consumers read them fresh at each
        paint or dialog-open. Only a widget that holds items (this tab's
        combo) needs an explicit refresh on rename."""
        if self.session.busy:
            QMessageBox.warning(self, "Rig busy",
                                "Port names can be edited when the rig is idle.")
            return
        sv = self.config.reagent_selection.selector_valves
        dialog = PortNamesDialog(self, available_ports(self.config),
                                 sv.name_mapping)
        if dialog.exec_() != QDialog.Accepted:
            return
        sv.name_mapping = dialog.result_mapping or None
        self._refreshPortNames()
        try:
            save_config(self.config)    # to the file the config came from
        except Exception as e:
            QMessageBox.critical(self, "Error",
                                 "The names apply for this session, but "
                                 f"saving the config failed: {e}")

    def _refreshPortNames(self):
        """Repaint the port list from the config, keeping the selection --
        a rename must not move a valve."""
        with QSignalBlocker(self.valveCombo):
            current = self.valveCombo.currentData()
            self._fillPorts()
            self.valveCombo.setCurrentIndex(self.valveCombo.findData(current))

    def _syringeArgs(self):
        return (int(self.syringePortCombo.currentText()), self.volumeSpinBox.value(),
                self.speedCombo.currentData())

    def extractSyringe(self):
        port, volume, rate = self._syringeArgs()
        self._run(lambda: self.manual.extract(port, volume, rate, on_started=self._started))

    def dispenseSyringe(self):
        port, volume, rate = self._syringeArgs()
        self._run(lambda: self.manual.dispense(port, volume, rate, on_started=self._started))

    def emptySyringe(self):
        self._run(lambda: self.manual.empty_to_waste(on_started=self._started))

    def startDiscPump(self):
        seconds = self.pumpInput.value()
        self._run(lambda: self.manual.aspirate(seconds, on_started=self._started))

    def stopMove(self):
        """Stop the move in flight: the same signal that aborts a run. The
        session resets it once the move has unwound, so the next move is
        not refused on a stale cancel."""
        if self.session.kind == "manual":
            self.session.abort()

    def _run(self, verb):
        """Hand one manual verb to the session, which runs it off the GUI
        thread. The controls go dead until the move's report has been
        handled -- a completion, a stop, or an error as a dialog with the
        controls restored first. One at a time: a press while the rig is
        busy is refused, not queued."""
        try:
            self.system.run_manual(verb, callbacks={
                "on_stopped": lambda: self._post_event("operationStopped"),
                "on_error": lambda message: self._post_event("handleError", message),
                "on_finished": lambda: self._post_event("operationFinished"),
            })
        except RuntimeError as e:
            _logger.info("%s; the manual move was not started.", e)
            return
        # Posted reports cannot be handled before this returns.
        self.setControlsEnabled(False)

    def _started(self, seconds):
        """From the worker thread: the move is under way and expected to
        take `seconds` -- what the progress bar counts against."""
        self._post_event("startProgress", float(seconds))

    # --- the move's report, on the Qt thread ---

    def startProgress(self, seconds):
        self.operation_start_time = time.time()
        self.operation_duration = seconds
        self.syringeProgressBar.setValue(0)
        self.progress_timer.start(100)

    def operationStopped(self):
        self._settleBar(0)

    def handleError(self, error_message):
        self._settleBar(0)
        QMessageBox.critical(self, "Error", f"Manual operation failed: {error_message}")

    def operationFinished(self):
        # Last, always. A bar still counting means the move ran to its end;
        # a stop or an error has already zeroed it, a valve move never had one.
        if self.progress_timer.isActive():
            self._settleBar(100)
        self.setControlsEnabled(True)

    def _settleBar(self, value):
        self.progress_timer.stop()
        self.syringeProgressBar.setValue(value)

    def setControlsEnabled(self, enabled):
        for control in self._controls:
            control.setEnabled(enabled)
        self.stopButton.setEnabled(not enabled)

    def _fillPorts(self):
        """Each item carries its port number: the list holds only the
        ports the rig offers, so its indices are not port numbers."""
        self.valveCombo.clear()
        for port, label in self.manual.port_names():
            self.valveCombo.addItem(label, port)

    def updateProgress(self):
        elapsed = time.time() - self.operation_start_time
        progress = min(100, int((elapsed / max(self.operation_duration, 1e-9)) * 100))
        self.syringeProgressBar.setValue(progress)

    def _onHeldVolume(self, volume_ul):
        # On the pump's thread; the paint crosses to Qt.
        self._post_event('_handle_held_volume', volume_ul)

    def _handle_held_volume(self, volume_ul):
        self.plungerPositionBar.setValue(int(volume_ul))

    def showEvent(self, event):
        super().showEvent(event)
        self._showCurrentPort()

    def _showCurrentPort(self):
        """Show where the valves are; do not move them there.

        A valve sitting on a port this rig does not offer -- port 1 at
        power-on, on a rig whose port 1 has no tubing volume -- has no item
        to select, and the box is left blank. The operator reads it to know
        where the fluid path goes, so naming a port the valve is not on is
        worse than naming none.
        """
        with QSignalBlocker(self.valveCombo):
            self.valveCombo.setCurrentIndex(
                self.valveCombo.findData(self.manual.current_port()))

    def closeEvent(self, event):
        self.progress_timer.stop()
        super().closeEvent(event)
