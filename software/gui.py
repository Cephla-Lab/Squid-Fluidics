import argparse
import csv
import logging
import os
import re
import sys
import time
from datetime import datetime
from PyQt5.QtWidgets import (QApplication, QMainWindow, QTabWidget, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QTreeWidget, QTreeWidgetItem,
                             QHeaderView, QCheckBox, QFileDialog, QMessageBox, QComboBox,
                             QSpinBox, QLabel, QProgressBar, QLineEdit,
                             QGroupBox, QGridLayout, QSizePolicy, QDialog, QFormLayout,
                             QDoubleSpinBox, QDialogButtonBox)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QEvent, QCoreApplication, QSettings
from PyQt5.QtGui import QColor, QBrush

from serial import SerialException

from fluidics.control.config import load_config
from fluidics.control.discovery import DeviceNotFoundError
from fluidics.errors import DeviceError
from fluidics.devices import (
    ISSUE_FLOW_SENSORS,
    ISSUE_TEMPERATURE_CONTROLLER,
)
from fluidics.system import FluidicsSystem
from fluidics.run_log import setup_uncaught_exception_logging, start_log_file
from fluidics.sequences import (
    load_sequences, save_sequences_yaml, get_included_sequences,
    get_fields_for_type, check_ports_against_config,
    SEQUENCE_TYPES, SEQUENCE_TYPE_LABELS, APPLICATION_SEQUENCES,
    SequenceListAdapter,
)

import matplotlib.pyplot as plt
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter
import warnings
warnings.filterwarnings('ignore')

_logger = logging.getLogger("fluidics.gui")

def pick_config(cli_path=None):
    """The rig config to run with, loaded: the --config path if given, else
    the rig's own ./config.yaml (or .json), else the last file an operator
    picked; when none of those exists, or one fails to load, a dialog asks
    instead of a traceback. Returns the loaded config, or None if the
    operator cancelled. The conventional local file outranks the remembered
    one on purpose: a rig's ./config.yaml is the rig's.
    """
    settings = QSettings("Cephla", "FluidicsControl")
    path = cli_path or next(
        (p for p in ["./config.yaml", "./config.json", settings.value("config_path")]
         if p and os.path.exists(p)), None)
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
            continue
        settings.setValue("config_path", path)
        return config


class WorkerEvent(QEvent):
    EVENT_TYPE = QEvent.Type(QEvent.registerEventType())

    def __init__(self, callback_name, *args):
        super().__init__(WorkerEvent.EVENT_TYPE)
        self.callback_name = callback_name
        self.args = args


class PostsToQtThread:
    """For a QObject fed by worker threads: _post_event(name, *args) queues a
    call to one of its own methods on the Qt thread. Mix in before the Qt
    base class, so event() here sees the WorkerEvent first."""

    def _post_event(self, method_name, *args):
        QCoreApplication.postEvent(self, WorkerEvent(method_name, *args))

    def event(self, event):
        if event.type() == WorkerEvent.EVENT_TYPE:
            getattr(self, event.callback_name)(*event.args)
            return True
        return super().event(event)


class AddSequenceDialog(QDialog):
    """Dialog for adding a new sequence to the tree."""

    def __init__(self, parent, application, port_names):
        super().__init__(parent)
        self.setWindowTitle("Add Sequence")
        self.application = application
        self.port_names = port_names
        self.result_dict = None
        self._field_widgets = {}

        self.initUI()

    def initUI(self):
        layout = QVBoxLayout(self)

        # Sequence type selector
        type_layout = QHBoxLayout()
        type_layout.addWidget(QLabel("Type:"))
        self.typeCombo = QComboBox()
        available_types = APPLICATION_SEQUENCES.get(self.application, list(SEQUENCE_TYPES.keys()))
        for seq_type in available_types:
            self.typeCombo.addItem(SEQUENCE_TYPE_LABELS.get(seq_type, seq_type), seq_type)
        type_layout.addWidget(self.typeCombo)
        layout.addLayout(type_layout)

        # Form for fields
        self.formLayout = QFormLayout()
        layout.addLayout(self.formLayout)

        # OK/Cancel
        self.buttonBox = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttonBox.accepted.connect(self.accept)
        self.buttonBox.rejected.connect(self.reject)
        layout.addWidget(self.buttonBox)

        # Connect type change
        self.typeCombo.currentIndexChanged.connect(self._rebuild_fields)
        self._rebuild_fields()

    def _rebuild_fields(self):
        # Clear existing form rows
        while self.formLayout.rowCount() > 0:
            self.formLayout.removeRow(0)
        self._field_widgets.clear()

        seq_type = self.typeCombo.currentData()
        fields = get_fields_for_type(seq_type)

        for field_name, field_info in fields.items():
            if field_name == 'include':
                continue  # handled by tree checkbox

            default = field_info.default if field_info.default is not None else None

            if field_name == 'name':
                widget = QLineEdit()
                if default is not None:
                    widget.setText(str(default))
            elif field_name == 'fluidic_port':
                widget = QComboBox()
                for i, pname in enumerate(self.port_names):
                    widget.addItem(pname, i + 1)
                if default is not None:
                    widget.setCurrentIndex(max(0, int(default) - 1))
            elif field_name in ('temperature', 'incubation_time'):
                widget = QDoubleSpinBox()
                widget.setDecimals(2)
                widget.setRange(0, 100000)
                if default is not None:
                    widget.setValue(float(default))
            else:
                # int fields: flow_rate, volume, repeat, fill_tubing_with
                widget = QSpinBox()
                widget.setRange(0, 100000)
                if default is not None:
                    widget.setValue(int(default))

            self._field_widgets[field_name] = widget
            self.formLayout.addRow(field_name, widget)

    def accept(self):
        seq_type = self.typeCombo.currentData()
        d = {'type': seq_type}
        for field_name, widget in self._field_widgets.items():
            if isinstance(widget, QLineEdit):
                val = widget.text().strip()
                if val:
                    d[field_name] = val
            elif isinstance(widget, QComboBox):
                d[field_name] = widget.currentData()
            elif isinstance(widget, QDoubleSpinBox):
                d[field_name] = widget.value()
            elif isinstance(widget, QSpinBox):
                d[field_name] = widget.value()
        self.result_dict = d
        super().accept()


class SequencesWidget(PostsToQtThread, QWidget):
    """Edits the sequence list and runs it through the system's session, which
    owns the run's thread and its end; this widget renders the callbacks."""

    def __init__(self, config, system):
        super().__init__()
        self.config = config
        self.system = system
        self.session = system.session
        self.selectorValveSystem = system.devices.selector_valves

        self._running_rows = []  # Tree rows of the sequences handed to the worker
        self._ended_early = False   # a stop or an error has already had its dialog
        system.warnings.subscribe(self.reportWarning)

        self.initUI()

    def initUI(self):
        layout = QVBoxLayout()

        # Tree for displaying sequences
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Property", "Value"])
        self.tree.setColumnCount(2)
        self.tree.setEditTriggers(QTreeWidget.NoEditTriggers)
        self.tree.itemDoubleClicked.connect(self._onItemDoubleClicked)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        layout.addWidget(self.tree)

        # Buttons
        buttonLayout = QHBoxLayout()
        self.loadButton = QPushButton("Load")
        self.loadButton.clicked.connect(self.loadSequences)
        self.saveButton = QPushButton("Save")
        self.saveButton.clicked.connect(self.saveSequences)
        self.addButton = QPushButton("Add Sequence")
        self.addButton.clicked.connect(self.addSequence)
        self.removeButton = QPushButton("Remove Sequence")
        self.removeButton.clicked.connect(self.removeSequence)
        self.selectAllButton = QPushButton("Select All")
        self.selectAllButton.clicked.connect(self.selectAll)
        self.selectNoneButton = QPushButton("Select None")
        self.selectNoneButton.clicked.connect(self.selectNone)
        self.runButton = QPushButton("Run Selected Sequences")
        self.runButton.clicked.connect(self.runSelectedSequences)
        self.pauseButton = QPushButton("Pause")
        self.pauseButton.clicked.connect(self.pauseSequences)
        self.pauseButton.setEnabled(False)  # Initially disabled
        self.abortButton = QPushButton("Abort")
        self.abortButton.clicked.connect(self.abortSequences)
        self.abortButton.setEnabled(False)  # Initially disabled

        buttonLayout.addWidget(self.loadButton)
        buttonLayout.addWidget(self.saveButton)
        buttonLayout.addWidget(self.addButton)
        buttonLayout.addWidget(self.removeButton)
        buttonLayout.addWidget(self.selectAllButton)
        buttonLayout.addWidget(self.selectNoneButton)
        buttonLayout.addWidget(self.runButton)
        buttonLayout.addWidget(self.pauseButton)
        buttonLayout.addWidget(self.abortButton)

        layout.addLayout(buttonLayout)

        # Progress bar
        self.progressBar = QProgressBar()
        self.sequenceLabel = QLabel("0/0 sequences")
        self.timeLabel = QLabel("00:00:00 remaining")
        # Draw-protection notices land here rather than in a QMessageBox: a
        # `warn`-mode fault can fire once per draw, and a modal dialog per draw
        # would be unusable during the very run it is meant to inform.
        self.warningLabel = QLabel()
        self.warningLabel.setStyleSheet("color: #b36b00;")
        self.warningLabel.setWordWrap(True)
        self.warningLabel.setVisible(False)
        self._warnings = []

        progressSection = QVBoxLayout()
        progressLabelLayout = QHBoxLayout()
        progressLabelLayout.addWidget(self.sequenceLabel)
        progressLabelLayout.addStretch()
        progressLabelLayout.addWidget(self.timeLabel)
        progressSection.addLayout(progressLabelLayout)
        progressSection.addWidget(self.progressBar)
        progressSection.addWidget(self.warningLabel)

        layout.addLayout(progressSection)

        self.setLayout(layout)

        # Timer for updating time remaining
        self.timer = QTimer()
        self.timer.timeout.connect(self.updateTimeRemaining)
        self.elapsed_time = 0
        self.total_time = None

    FIELD_LABELS = {
        'fluidic_port': 'Fluidic Port',
        'flow_rate': 'Flow Rate (\u00b5L/min)',
        'volume': 'Volume (\u00b5L)',
        'fill_tubing_with': 'Fill Tubing With',
        'incubation_time': 'Incubation Time (min)',
        'repeat': 'Repeat',
        'temperature': 'Temperature (\u00b0C)',
    }

    def _onItemDoubleClicked(self, item, column):
        """Allow editing: name (col 0) for top-level items, value (col 1) for children."""
        is_top_level = item.parent() is None
        if is_top_level and column == 0:
            self.tree.editItem(item, 0)
        elif not is_top_level and column == 1:
            self.tree.editItem(item, 1)

    def populateTree(self, sequences):
        """Populate the tree widget from a list of sequence dicts."""
        self.tree.clear()
        for seq in sequences:
            self._addSequenceItem(seq)

    def _addSequenceItem(self, seq):
        """Add a single sequence dict as a top-level tree item."""
        seq_type = seq.get('type', '')
        type_label = SEQUENCE_TYPE_LABELS.get(seq_type, seq_type)
        name = seq.get('name') or ''

        display_name = name if name else type_label
        item = QTreeWidgetItem([display_name, f"Type: {type_label}"])
        item.setData(0, Qt.UserRole, seq_type)
        item.setFlags(item.flags() | Qt.ItemIsEditable)

        # Include checkbox
        include = seq.get('include', True)
        item.setCheckState(0, Qt.Checked if include else Qt.Unchecked)

        # Determine which fields to show
        try:
            type_fields = get_fields_for_type(seq_type)
        except ValueError:
            type_fields = {}

        # Required fields (no default or default is a required sentinel)
        required_field_names = set()
        for fname, finfo in type_fields.items():
            if fname in ('type', 'include', 'name'):
                continue
            if finfo.is_required():
                required_field_names.add(fname)

        # Add child items for fields (excluding 'type', 'include', 'name')
        for fname, finfo in type_fields.items():
            if fname in ('type', 'include', 'name'):
                continue
            value = seq.get(fname)
            default = finfo.default

            # Show field if it has a non-None, non-default value, or is required
            if fname in required_field_names or (value is not None and value != default):
                display_value = str(value) if value is not None else ''
                display_label = self.FIELD_LABELS.get(fname, fname)
                child = QTreeWidgetItem([display_label, display_value])
                child.setData(0, Qt.UserRole, fname)
                child.setFlags(child.flags() | Qt.ItemIsEditable)
                item.addChild(child)

        self.tree.addTopLevelItem(item)
        item.setExpanded(True)

    def getSequences(self, selected_only=False):
        """Read the tree back into a list of sequence dicts."""
        sequences = []
        rows = self._checkedRows() if selected_only else range(self.tree.topLevelItemCount())
        for i in rows:
            item = self.tree.topLevelItem(i)
            seq_type = item.data(0, Qt.UserRole)
            include = item.checkState(0) == Qt.Checked

            seq = {'type': seq_type, 'include': include}

            # Read name from top-level item text (skip if it matches default type label)
            name = item.text(0).strip()
            type_label = SEQUENCE_TYPE_LABELS.get(seq_type, '')
            if name and name != type_label:
                seq['name'] = name

            for j in range(item.childCount()):
                child = item.child(j)
                fname = child.data(0, Qt.UserRole) or child.text(0)
                raw_value = child.text(1).strip()

                if not raw_value:
                    continue

                seq[fname] = raw_value

            sequences.append(seq)
        # Validate via pydantic to coerce the QLineEdit strings into ints/floats.
        validated = SequenceListAdapter.validate_python(sequences)
        return [s.model_dump() for s in validated]

    def loadSequences(self):
        fileName, _ = QFileDialog.getOpenFileName(
            self, "Open Sequences", "",
            "Sequence Files (*.yaml *.yml *.csv)")
        if fileName:
            try:
                sequences = load_sequences(fileName)
                self.populateTree(sequences)
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to load sequences: {str(e)}")

    def saveSequences(self):
        fileName, _ = QFileDialog.getSaveFileName(
            self, "Save Sequences", "",
            "YAML Files (*.yaml)")
        if fileName:
            if not fileName.lower().endswith(('.yaml', '.yml')):
                fileName += '.yaml'
            try:
                save_sequences_yaml(self.getSequences(), fileName)
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to save sequences: {str(e)}")

    def addSequence(self):
        port_names = self.selectorValveSystem.get_port_names()
        dialog = AddSequenceDialog(self, self.config.application, port_names)
        if dialog.exec_() == QDialog.Accepted and dialog.result_dict:
            self._addSequenceItem(dialog.result_dict)

    def removeSequence(self):
        current = self.tree.currentItem()
        if current is None:
            return
        # If a child is selected, remove its parent (the top-level sequence)
        if current.parent() is not None:
            current = current.parent()
        index = self.tree.indexOfTopLevelItem(current)
        if index >= 0:
            self.tree.takeTopLevelItem(index)

    def selectAll(self):
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setCheckState(0, Qt.Checked)

    def selectNone(self):
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setCheckState(0, Qt.Unchecked)

    def _checkedRows(self):
        """Tree rows whose checkbox is checked, in order. getSequences reads
        exactly these rows when selected_only=True, so a snapshot of this list
        stays index-aligned with the sequences handed to the worker."""
        return [i for i in range(self.tree.topLevelItemCount())
                if self.tree.topLevelItem(i).checkState(0) == Qt.Checked]

    def highlightRow(self, row_index):
        """Highlight the currently running sequence in the tree."""
        white_brush = QBrush(QColor('white'))
        blue_brush = QBrush(QColor('lightblue'))

        # Reset all highlights
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            item.setBackground(0, white_brush)
            item.setBackground(1, white_brush)

        # Set new highlighting
        if row_index is not None and row_index < self.tree.topLevelItemCount():
            item = self.tree.topLevelItem(row_index)
            item.setBackground(0, blue_brush)
            item.setBackground(1, blue_brush)

    def runSelectedSequences(self):
        if self.tree.topLevelItemCount() == 0:
            return
        try:
            selected = self.getSequences(selected_only=True)
            # A port this rig does not have must fail here, at the button,
            # not hours in when the valve is asked to open it.
            check_ports_against_config(selected, self.config)
        except Exception as e:
            QMessageBox.critical(self, "Invalid Sequence", f"Failed to validate sequences: {str(e)}")
            return
        # The worker reports positions in this filtered list; remember which
        # tree row each one came from so the highlight lands on it.
        self._running_rows = self._checkedRows()
        self.total_sequences = sum(s.get('repeat', 1) for s in selected)

        if not selected:
            QMessageBox.warning(self, "No Sequences Selected", "Please select at least one sequence to run.")
            return

        callbacks = {
            'update_progress': self.updateProgress,
            'on_stopped': self.onStopped,
            'on_error': self.handleError,
            'on_finished': self.onWorkerFinished,
            'on_estimate': self.setTimeEstimate,
        }

        self._warnings.clear()
        self.warningLabel.setVisible(False)
        self._ended_early = False

        try:
            self.system.run(selected, callbacks)
        except RuntimeError as e:
            # A manual move got in first; the tabs normally prevent this.
            QMessageBox.warning(self, "Rig busy", str(e))
            return
        self._beginRunDisplay()

    def _beginRunDisplay(self):
        """A fresh run's clocks. The previous run's seconds must not leak in,
        and its estimate must not price this one while the new estimate is
        still in the post."""
        self.elapsed_time = 0
        self.total_time = None
        self._renderRunControls()
        self.sequenceLabel.setText(f"0/{self.total_sequences} sequences")
        self.timer.start(1000)

    def updateTimeRemaining(self):
        """The one-second tick: spend a second unless the run is stopped.

        A run at rest spends no time, the way the incubation clock does. A
        pause that has been asked for but not yet reached still counts -- the
        move in flight really is running.
        """
        paused, at_rest = self._runState()
        if not at_rest:
            self.elapsed_time += 1  # Add one second
        self._showTimeRemaining(self._pauseSuffix(paused, at_rest))
        # A run can also end while the tick is what is watching: a flow fault
        # cancels from the reader thread. So the tick runs until the run
        # ends (_handle_finished stops it), not until the estimate does --
        # an estimate is an estimate, and a run outliving it still needs
        # its label and its buttons kept honest.
        self._renderRunControls()

    def _showTimeRemaining(self, suffix=None):
        """Draw the label and the bar from the elapsed time as it stands.

        Separate from the tick, which advances that clock: a Pause click
        refreshes the label and must not spend a second of the run.
        """
        if self.total_time is None:
            # The estimate is posted to this thread's queue, so it can still
            # be pending when the operator presses Pause. The button already
            # says what happened; the next tick writes the label.
            return
        if suffix is None:
            suffix = self._pauseSuffix(*self._runState())
        remaining = max(0, self.total_time - self.elapsed_time)

        hours = int(remaining // 3600)
        minutes = int((remaining % 3600) // 60)
        seconds = int(remaining % 60)
        self.timeLabel.setText(
            f"{hours:02d}:{minutes:02d}:{seconds:02d} remaining{suffix}")

        progress = min(100, int((self.elapsed_time / max(self.total_time, 1)) * 100))
        self.progressBar.setValue(progress)

    def _runState(self):
        """(asked to pause, actually stopped) -- read once per repaint.

        Another thread owns these, so two reads can disagree: taking them
        together is what keeps the clock decision and the label it prints
        from coming from different instants.
        """
        session = self.session
        return session.paused, session.at_rest

    @staticmethod
    def _pauseSuffix(paused, at_rest):
        """What the time label says about a pause, if anything."""
        if at_rest:
            return " (paused)"
        if paused:
            return " (pausing\u2026)"
        return ""

    def _renderRunControls(self):
        """Draw the run buttons from the state of the run.

        One place, because four methods used to poke text and enabled
        directly and could drift apart -- and because a run can end without
        any of them being called: a flow fault cancels from the MCU reader
        thread, and the buttons must go dead then too.
        """
        live = self.session.kind == "run" and not self.session.cancelled
        self.runButton.setEnabled(not self.session.busy)
        self.pauseButton.setEnabled(live)
        self.abortButton.setEnabled(live)
        self.pauseButton.setText("Resume" if self.session.paused else "Pause")

    def pauseSequences(self):
        """Hold the run after the move in flight, or let it go on."""
        if self.session.paused:
            self.session.resume()
        else:
            self.session.pause()
        self._renderRunControls()
        # The label would otherwise wait for the next tick to say anything.
        self._showTimeRemaining()

    def _handle_progress(self, index, sequence_num, status):
        self.sequenceLabel.setText(f"{sequence_num}/{self.total_sequences} sequences")
        row = self._running_rows[index] if 0 <= index < len(self._running_rows) else None
        self.highlightRow(row)

    def _handle_warning(self, message):
        self._warnings.append(message)
        count = len(self._warnings)
        prefix = "\u26a0 " if count == 1 else f"\u26a0 {count} notices, latest: "
        self.warningLabel.setText(prefix + message)
        self.warningLabel.setVisible(True)

    def reportWarning(self, message):
        """Called from the MCU reader thread, so it must not touch widgets.
        The system's channel logs it; this only shows it."""
        self._post_event('_handle_warning', message)

    def _handle_stopped(self):
        self._ended_early = True
        QMessageBox.information(self, "Stopped", "The run was stopped.")

    def _handle_error(self, error_message):
        self._ended_early = True
        QMessageBox.critical(self, "Error", error_message)

    def _handle_finished(self):
        self.progressBar.setValue(0)
        self.timeLabel.setText("00:00:00 remaining")
        self.sequenceLabel.setText("0/0 sequences")
        self.timer.stop()
        self.highlightRow(None)
        self._renderRunControls()
        # A run that ended early has already said so; one dialog, not two.
        if not self._ended_early:
            QMessageBox.information(self, "Finished", "Sequence execution finished.")

    def _handle_time_estimate(self, time_to_finish, n_sequences):
        self.total_time = time_to_finish
        self.progressBar.setMaximum(100)  # For percentage
        self.progressBar.setValue(0)

    # The worker's callbacks arrive on its thread; each crosses to the Qt
    # thread as a call to its _handle_ method.
    def setTimeEstimate(self, time_to_finish, n_sequences):
        self._post_event('_handle_time_estimate', time_to_finish, n_sequences)

    def updateProgress(self, index, sequence_num, status):
        self._post_event('_handle_progress', index, sequence_num, status)

    def onStopped(self):
        self._post_event('_handle_stopped')

    def handleError(self, error_message):
        self._post_event('_handle_error', error_message)

    def onWorkerFinished(self):
        self._post_event('_handle_finished')

    def abortSequences(self):
        if self.session.kind == "run":
            # One signal, shared by the worker and every waiting device. It
            # also releases a held run, so Abort works while paused without
            # the operator having to resume first.
            self.session.abort()
            self._renderRunControls()


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
        self.plunger_timer = QTimer(self)
        self.plunger_timer.timeout.connect(self.updatePlungerPosition)

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
        self.valveCombo.addItems(self.manual.port_names())
        self.valveCombo.currentIndexChanged.connect(self.openValve)
        valveLayout.addWidget(self.valveCombo)
        self._controls.append(self.valveCombo)
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

        self.updatePlungerPosition()

    # --- the controls: each picks a verb and hands it to _run ---

    def openValve(self):
        port = self.valveCombo.currentIndex() + 1
        self._run(lambda: self.manual.open_port(port))

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

    def updateProgress(self):
        elapsed = time.time() - self.operation_start_time
        progress = min(100, int((elapsed / max(self.operation_duration, 1e-9)) * 100))
        self.syringeProgressBar.setValue(progress)

    def updatePlungerPosition(self):
        # The wire is read only while a manual move is running -- that is
        # when the plunger is going somewhere the pump has not yet recorded.
        # Idle, the pump's own reading from the end of its last move is the
        # truth, and costs the GUI thread nothing.
        try:
            volume = self.manual.held_volume_ul(refresh=self.session.kind == "manual")
            self.plungerPositionBar.setValue(int(volume))
        except Exception as e:
            _logger.debug("Plunger position poll failed: %s", e)

    def showEvent(self, event):
        super().showEvent(event)
        self.plunger_timer.start(500)
        # Show where the valves are; do not move them there.
        self.valveCombo.blockSignals(True)
        self.valveCombo.setCurrentIndex(self.manual.current_port() - 1)
        self.valveCombo.blockSignals(False)

    def hideEvent(self, event):
        super().hideEvent(event)
        self.plunger_timer.stop()

    def closeEvent(self, event):
        self.progress_timer.stop()
        self.plunger_timer.stop()
        super().closeEvent(event)


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

    reading_signal = pyqtSignal(float, float)  # (temp, current_time)

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

    readings_signal = pyqtSignal(list)  # list[float] of length controller.channels

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
        # plots are what consume it (see TCMController.start).
        self.controller.subscribe(self._on_callback)
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

    reading_signal = pyqtSignal(object, float)  # (flow_ul_min or None, timestamp)
    fault_signal = pyqtSignal(str, object, float)  # (mode, FlowFault, timestamp)

    def __init__(self, sensor, draw_protection=True, parent=None):
        super().__init__(parent)
        self.sensor = sensor
        self.draw_protection = draw_protection

        self.flows = []

        self.reading_signal.connect(self._on_reading)
        self.fault_signal.connect(self._on_fault)
        self._build_ui()
        self.sensor.subscribe(self._on_callback)
        self.sensor.subscribe_faults(self._on_fault_callback)

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
        except (DeviceNotFoundError, DeviceError, SerialException) as e:
            # Nothing works without the controller or the pump, so there is
            # no window worth showing -- just the message. DeviceNotFoundError
            # is "not plugged in / wrong serial"; SerialException is the
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
        answer = QMessageBox.question(
            self, "Still running", f"A {what} is in progress. Abort it and exit?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return answer == QMessageBox.Yes

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
                        "./config.yaml then ./config.json, then the last file "
                        "picked; asks otherwise).")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    config = pick_config(args.config)
    if config is None:
        sys.exit(1)
    gui = FluidicsControlGUI(config, args.simulation)
    gui.show()
    sys.exit(app.exec_())
