import os
import sys
import csv
import time
import threading
import argparse
from datetime import datetime
from PyQt5.QtWidgets import (QApplication, QMainWindow, QTabWidget, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QTreeWidget, QTreeWidgetItem,
                             QHeaderView, QCheckBox, QFileDialog, QMessageBox, QComboBox,
                             QSpinBox, QLabel, QProgressBar, QLineEdit,
                             QGroupBox, QGridLayout, QSizePolicy, QDialog, QFormLayout,
                             QDoubleSpinBox, QDialogButtonBox)
from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal, pyqtSlot, Q_ARG, QMetaObject, QEvent, QCoreApplication
from PyQt5.QtGui import QColor, QBrush

from fluidics.control.config import load_config
from fluidics.control.controller import FluidController, FluidControllerSimulation
from fluidics.control.syringe_pump import SyringePump, SyringePumpSimulation
from fluidics.control.selector_valve import SelectorValveSystem
from fluidics.control.disc_pump import DiscPump
from fluidics.control.temperature_controller import TCMController, TCMControllerSimulation

from fluidics.control._def import CMD_SET
from fluidics.control.tecancavro.tecanapi import TecanAPITimeout
from fluidics.merfish_operations import MERFISHOperations
from fluidics.open_chamber_operations import OpenChamberOperations
from fluidics.experiment_worker import ExperimentWorker
from fluidics.sequences import (
    load_sequences, save_sequences_yaml, get_included_sequences,
    get_fields_for_type, SEQUENCE_TYPES, SEQUENCE_TYPE_LABELS, APPLICATION_SEQUENCES,
    SequenceListAdapter,
)

import matplotlib.pyplot as plt
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
from matplotlib.figure import Figure
import warnings
warnings.filterwarnings('ignore')

def load_config_file(config_path=None):
    if config_path is None:
        # Try YAML first, fall back to JSON (which auto-converts)
        if os.path.exists('./config.yaml'):
            config_path = './config.yaml'
        else:
            config_path = './config.json'
    return load_config(config_path)


class WorkerEvent(QEvent):
    EVENT_TYPE = QEvent.Type(QEvent.registerEventType())

    def __init__(self, callback_name, *args):
        super().__init__(WorkerEvent.EVENT_TYPE)
        self.callback_name = callback_name
        self.args = args


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


class SequencesWidget(QWidget):

    sequence_running = pyqtSignal(bool)

    def __init__(self, config, syringe, selector_valves, disc_pump, temperature_controller):
        super().__init__()
        self.config = config
        self.syringePump = syringe
        self.selectorValveSystem = selector_valves
        self.discPump = disc_pump
        self.temperatureController = temperature_controller

        self.experiment_ops = None  # Will be set based on the selected application
        self.worker = None

        if self.config.application == 'Flow Cell':
            self.experiment_ops = MERFISHOperations(self.config, self.syringePump, self.selectorValveSystem, self.temperatureController)
        elif self.config.application == "Open Chamber":
            self.experiment_ops = OpenChamberOperations(self.config, self.syringePump, self.selectorValveSystem, self.discPump, self.temperatureController)
        else:
            raise ValueError(f"Unsupported application: {self.config.application!r}")

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
        buttonLayout.addWidget(self.abortButton)

        layout.addLayout(buttonLayout)

        # Progress bar
        self.progressBar = QProgressBar()
        self.sequenceLabel = QLabel("0/0 sequences")
        self.timeLabel = QLabel("00:00:00 remaining")

        progressSection = QVBoxLayout()
        progressLabelLayout = QHBoxLayout()
        progressLabelLayout.addWidget(self.sequenceLabel)
        progressLabelLayout.addStretch()
        progressLabelLayout.addWidget(self.timeLabel)
        progressSection.addLayout(progressLabelLayout)
        progressSection.addWidget(self.progressBar)

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
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            seq_type = item.data(0, Qt.UserRole)
            include = item.checkState(0) == Qt.Checked

            if selected_only and not include:
                continue

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
        except Exception as e:
            QMessageBox.critical(self, "Invalid Sequence", f"Failed to validate sequences: {str(e)}")
            return
        self.total_sequences = sum(s.get('repeat', 1) for s in selected)

        if not selected:
            QMessageBox.warning(self, "No Sequences Selected", "Please select at least one sequence to run.")
            return

        callbacks = {
            'update_progress': self.updateProgress,
            'on_error': self.handleError,
            'on_finished': self.onWorkerFinished,
            'on_estimate': self.setTimeEstimate
        }

        self.runButton.setEnabled(False)
        self.abortButton.setEnabled(True)
        self.sequence_running.emit(True)

        self.worker = ExperimentWorker(self.experiment_ops, selected, self.config, callbacks)
        self.worker_thread = threading.Thread(target=self.worker.run, daemon=True)

        self.sequenceLabel.setText(f"0/{self.total_sequences} sequences")
        self.timer.start(1000)

        self.worker_thread.start()

    def updateTimeRemaining(self):
        """Update the time remaining display and progress bar"""
        self.elapsed_time += 1  # Add one second
        remaining = max(0, self.total_time - self.elapsed_time)

        # Update time label
        hours = int(remaining // 3600)
        minutes = int((remaining % 3600) // 60)
        seconds = int(remaining % 60)
        self.timeLabel.setText(f"{hours:02d}:{minutes:02d}:{seconds:02d} remaining")
            
        # Update progress bar percentage
        progress = min(100, int((self.elapsed_time / max(self.total_time, 1)) * 100))
        self.progressBar.setValue(progress)
            
        if remaining <= 0:
            self.timer.stop()

    def event(self, event):
        if event.type() == WorkerEvent.EVENT_TYPE:
            if event.callback_name == 'update_progress':
                self._handle_progress(*event.args)
            elif event.callback_name == 'show_error':
                self._handle_error(*event.args)
            elif event.callback_name == 'on_finished':
                self._handle_finished()
            elif event.callback_name == 'set_time_estimate':
                self._handle_time_estimate(*event.args)
            return True
        return super().event(event)

    def _post_event(self, callback_name, *args):
        QCoreApplication.postEvent(self, WorkerEvent(callback_name, *args))

    def _handle_progress(self, index, sequence_num, status):
        self.sequenceLabel.setText(f"{sequence_num}/{self.total_sequences} sequences")
        self.highlightRow(index)

    def _handle_error(self, error_message):
        QMessageBox.critical(self, "Error", error_message)

    def _handle_finished(self):
        self.runButton.setEnabled(True)
        self.abortButton.setEnabled(False)
        self.progressBar.setValue(0)
        self.timeLabel.setText("00:00:00 remaining")
        self.sequenceLabel.setText("0/0 sequences")
        self.timer.stop()
        self.highlightRow(None)
        self.sequence_running.emit(False)
        
        if self.worker:
            self.worker_thread.join()
            self.worker = None

        self.syringePump.reset_abort()
        if self.temperatureController is not None:
            self.temperatureController.reset_abort()
        if self.discPump is not None:
            self.discPump.reset_abort()
        QMessageBox.information(self, "Finished", "Sequence execution finished.")

    def _handle_time_estimate(self, time_to_finish, n_sequences):
        self.total_time = time_to_finish
        self.progressBar.setMaximum(100)  # For percentage
        self.progressBar.setValue(0)

    def setTimeEstimate(self, time_to_finish, n_sequences):
        self._post_event('set_time_estimate', time_to_finish, n_sequences)

    def updateProgress(self, index, sequence_num, status):
        self._post_event('update_progress', index, sequence_num, status)

    def handleError(self, error_message):
        self._post_event('show_error', error_message)

    def onWorkerFinished(self):
        self._post_event('on_finished')

    def abortSequences(self):
        if self.worker and self.experiment_ops:
            self.syringePump.abort()
            if self.discPump is not None:
                self.discPump.abort()
            if self.temperatureController is not None:
                self.temperatureController.abort()
            self.worker.abort()
            self.abortButton.setEnabled(False)


class ManualControlWidget(QWidget):
    def __init__(self, config, syringe, selector_valves, disc_pump):
        super().__init__()
        self.config = config
        self.syringePump = syringe
        self.selectorValveSystem = selector_valves
        self.disc_pump = disc_pump

        # Initialize timers
        self.progress_timer = QTimer(self)
        self.progress_timer.timeout.connect(self.updateProgress)
        
        self.plunger_timer = QTimer(self)
        self.plunger_timer.timeout.connect(self.updatePlungerPosition)
        
        self.operation_start_time = None
        self.operation_duration = None

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
        self.valveCombo.addItems(self.selectorValveSystem.get_port_names())
        self.valveCombo.currentIndexChanged.connect(self.openValve)
        valveLayout.addWidget(self.valveCombo)
        valveGroupBox.setLayout(valveLayout)
        mainLayout.addWidget(valveGroupBox)

        if self.config.application == "Open Chamber":
            pumpGroupBox = QGroupBox("Disc Pump Control")
            pumpLayout = QHBoxLayout()
            pumpLayout.setContentsMargins(5, 5, 5, 5)
            pumpLayout.addWidget(QLabel("Operation time:"))
            self.pumpInput = QLineEdit()
            pumpLayout.addWidget(self.pumpInput)
            pumpLayout.addWidget(QLabel("s"))
            self.pumpButton = QPushButton("Start")
            pumpLayout.addWidget(self.pumpButton)
            self.pumpButton.clicked.connect(self.startDiscPump)
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
        speed_code_limit = self.config.syringe_pump.speed_code_limit
        for code in range(speed_code_limit, len(self.syringePump.SPEED_SEC_MAPPING)):
            rate = self.syringePump.get_flow_rate(code)
            self.speedCombo.addItem(f"{rate} mL/min", code)
        self.speedCombo.setCurrentIndex(40 - self.config.syringe_pump.speed_code_limit)  # Set default to code 40
        leftLayout.addWidget(QLabel("Speed:"), 1, 0)
        leftLayout.addWidget(self.speedCombo, 1, 1)

        self.volumeSpinBox = QSpinBox()
        self.volumeSpinBox.setRange(1, self.config.syringe_pump.volume_ul)
        self.volumeSpinBox.setSuffix(" μL")
        leftLayout.addWidget(QLabel("Volume:"), 2, 0)
        leftLayout.addWidget(self.volumeSpinBox, 2, 1)

        actionLayout = QHBoxLayout()
        self.pushButton = QPushButton("Extract")
        self.pushButton.clicked.connect(lambda: self.operateSyringe("extract"))
        self.pullButton = QPushButton("Dispense")
        self.pullButton.clicked.connect(lambda: self.operateSyringe("dispense"))
        self.emptyButton = QPushButton("Empty to Waste")
        self.emptyButton.clicked.connect(lambda: self.operateSyringe("empty"))
        actionLayout.addWidget(self.pushButton)
        actionLayout.addWidget(self.pullButton)
        leftLayout.addLayout(actionLayout, 3, 0, 1, 2)
        leftLayout.addWidget(self.emptyButton)

        topLayout.addWidget(leftWidget, 3)

        # Right side - Plunger position
        # TODO: stop updating position when not on this tab
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

        self.syringeProgressBar = QProgressBar()
        self.syringeProgressBar.setRange(0, 100)
        syringeLayout.addWidget(QLabel("Execution Progress:"))
        syringeLayout.addWidget(self.syringeProgressBar)

        syringeGroupBox.setLayout(syringeLayout)
        mainLayout.addWidget(syringeGroupBox)

        self.setLayout(mainLayout)

        # Initialize plunger position
        self.updatePlungerPosition()

    def openValve(self):
        port = self.valveCombo.currentIndex() + 1
        self.selectorValveSystem.open_port(port)

    def operateSyringe(self, action):
        if self.syringePump.is_busy:
            print("Syringe pump is busy.")
            return

        syringe_port = int(self.syringePortCombo.currentText())
        speed_code = self.speedCombo.currentData()
        volume = self.volumeSpinBox.value()
        
        try:
            # Disable control buttons during operation
            self.setControlsEnabled(False)

            # Start operation
            self.syringePump.reset_chain()
            if action == "dispense":
                exec_time = self.syringePump.dispense(syringe_port, volume, speed_code)
            elif action == "extract":
                exec_time = self.syringePump.extract(syringe_port, volume, speed_code)
            elif action == "empty":
                exec_time = self.syringePump.dispense_to_waste()

            # Set up progress tracking
            self.operation_duration = exec_time

            # Start syringe operation in a separate thread
            operation_thread = threading.Thread(target=self._executeSyringeOperation, 
                                             args=(action, syringe_port, volume, speed_code))
            operation_thread.daemon = True
            operation_thread.start()
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error operating syringe pump: {str(e)}")
            self.setControlsEnabled(True)

    def _executeSyringeOperation(self, action, syringe_port, volume, speed_code):
        try:
            self.operation_start_time = time.time()

            # Start progress updates
            QMetaObject.invokeMethod(self, "startProgressTimer", Qt.QueuedConnection)

            self.syringePump.execute()

            # Clean up
            QMetaObject.invokeMethod(self, "operationComplete", Qt.QueuedConnection)

        except TecanAPITimeout:
            QMetaObject.invokeMethod(self, "operationComplete", Qt.QueuedConnection)

        except Exception as e:
            QMetaObject.invokeMethod(self, "handleError", 
                                   Qt.QueuedConnection,
                                   Q_ARG(str, str(e)))

    def startDiscPump(self):
        if self.disc_pump is not None:
            self.pumpButton.setEnabled(False)
            self.disc_pump.aspirate(float(self.pumpInput.text()))
            self.pumpButton.setEnabled(True)

    @pyqtSlot()
    def startProgressTimer(self):
        self.syringeProgressBar.setValue(0)
        self.progress_timer.start(100)  # Update progress every 100ms

    @pyqtSlot()
    def operationComplete(self):
        self.progress_timer.stop()
        self.syringeProgressBar.setValue(100)
        self.setControlsEnabled(True)
        self.operation_start_time = None
        self.operation_duration = None
        self.syringePump.is_busy = False

    @pyqtSlot(str)
    def handleError(self, error_message):
        #if error_message[:48] != "Tecan serial communication exceeded max attempts":
        QMessageBox.critical(self, "Error", f"Syringe pump error: {error_message}")
        self.syringePump.wait_for_stop()
        self.setControlsEnabled(True)
        self.progress_timer.stop()
        self.syringeProgressBar.setValue(0)

    def setControlsEnabled(self, enabled):
        self.pushButton.setEnabled(enabled)
        self.pullButton.setEnabled(enabled)
        self.emptyButton.setEnabled(enabled)
        self.syringePortCombo.setEnabled(enabled)
        self.speedCombo.setEnabled(enabled)
        self.volumeSpinBox.setEnabled(enabled)

    def updateProgress(self):
        if self.operation_start_time is None or self.operation_duration is None:
            return
            
        elapsed = time.time() - self.operation_start_time
        progress = min(100, int((elapsed / self.operation_duration) * 100))
        self.syringeProgressBar.setValue(progress)

    def updatePlungerPosition(self):
        try:
            position = self.syringePump.get_plunger_position() * self.config.syringe_pump.volume_ul
            self.plungerPositionBar.setValue(int(position))
        except Exception:
            pass

    def showEvent(self, event):
        # Start timer when widget becomes visible
        super().showEvent(event)
        self.plunger_timer.start(500)
        self.valveCombo.setCurrentIndex(self.selectorValveSystem.get_current_port() - 1)

    def hideEvent(self, event):
        # Stop timer when widget becomes hidden
        super().hideEvent(event)
        self.plunger_timer.stop()

    def closeEvent(self, event):
        self.progress_timer.stop()
        self.plunger_timer.stop()
        super().closeEvent(event)


class MplCanvas(FigureCanvasQTAgg):
    def __init__(self, parent=None, width=5, height=4, dpi=100):
        fig = Figure(figsize=(width, height), dpi=dpi)
        self.axes = fig.add_subplot(111)
        super(MplCanvas, self).__init__(fig)


class TemperatureChannelWidget(QWidget):
    """One channel's worth of temperature UI: target/actual readout, plot,
    record toggle, query interval, window size."""

    reading_signal = pyqtSignal(float, float)  # (temp, current_time)

    def __init__(self, controller, channel, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.channel = channel  # 1-based

        self.temps = []
        self.times = []
        self.targets = []
        self.query_interval = 2
        self.window_size = 60
        self.last_update = 0
        self.file = None
        self.writer = None

        self.reading_signal.connect(self._on_reading)

        self._build_ui()
        self.temp_input.setText(f"{self.controller.target_temperatures[channel - 1]:.2f}")

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

        plot_box = QGroupBox(f"Channel {self.channel} Plot")
        plot_layout = QVBoxLayout()

        plot_controls = QWidget()
        pc_layout = QHBoxLayout(plot_controls)
        pc_layout.addWidget(QLabel("Query Interval:"))
        self.interval_input = QSpinBox()
        self.interval_input.setMinimum(2)
        self.interval_input.setValue(2)
        self.interval_input.setSuffix(" s")
        pc_layout.addWidget(self.interval_input)
        pc_layout.addWidget(QLabel("Window Size:"))
        self.window_input = QSpinBox()
        self.window_input.setMinimum(10)
        self.window_input.setMaximum(3600)
        self.window_input.setValue(60)
        self.window_input.setSuffix(" s")
        pc_layout.addWidget(self.window_input)
        plot_layout.addWidget(plot_controls)

        self.canvas = MplCanvas(self, width=5, height=4, dpi=100)
        plot_layout.addWidget(self.canvas)

        self.record_btn = QPushButton("Start Recording")
        plot_layout.addWidget(self.record_btn)
        plot_box.setLayout(plot_layout)

        layout.addWidget(control)
        layout.addWidget(plot_box)

        self.set_btn.clicked.connect(self._set_clicked)
        self.save_btn.clicked.connect(self._save_clicked)
        self.output_btn.toggled.connect(self._on_output_toggled)
        self.record_btn.clicked.connect(self._toggle_record)
        self.interval_input.valueChanged.connect(self._set_interval)
        self.window_input.valueChanged.connect(self._set_window)

        self._sync_output_button()

    def _set_interval(self, value):
        self.query_interval = value

    def _set_window(self, value):
        self.window_size = value
        self._refresh_plot()

    def _on_reading(self, temp, current_time):
        if current_time - self.last_update < self.query_interval:
            return
        self.temp_label.setText(f"{temp:.1f}°C")
        target = self.controller.target_temperatures[self.channel - 1]
        self.temps.append(temp)
        self.targets.append(target)
        self.times.append(current_time)
        if self.writer is not None:
            self.writer.writerow([datetime.fromtimestamp(current_time), temp, target])
        while self.times and current_time - self.times[0] > self.window_size:
            self.times.pop(0)
            self.temps.pop(0)
            self.targets.pop(0)
        self._refresh_plot()
        self.last_update = current_time

    def _refresh_plot(self):
        if not self.temps or not self.times:
            return
        ax = self.canvas.axes
        ax.clear()
        ax.plot(self.times, self.temps, "b-", label="Actual")
        ax.plot(self.times, self.targets, "r--", label="Target")
        y_min = min(min(self.temps), min(self.targets))
        y_max = max(max(self.temps), max(self.targets))
        padding = (y_max - y_min) * 0.1 if y_max != y_min else 1.0
        ax.set_ylim([y_min - padding, y_max + padding])
        current_time = self.times[-1]
        ax.set_xlim([current_time - self.window_size, current_time])
        ax.set_xlabel("Seconds Ago")
        ax.set_ylabel("Temperature (°C)")
        ax.set_title(f"Channel {self.channel} Temperature")
        ax.grid(True)
        ax.legend()
        ax.set_xticklabels([f"{x:.0f}" for x in current_time - ax.get_xticks()])
        self.canvas.draw()

    def _set_clicked(self):
        try:
            t = float(self.temp_input.text())
            self.controller.set_target_temperature(self.channel, t)
        except ValueError:
            print(f"Invalid temperature for channel {self.channel}")

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
            print(f"Failed to {'enable' if checked else 'disable'} output on "
                  f"channel {self.channel}: {e}")
        self._sync_output_button()

    def _toggle_record(self):
        if self.record_btn.text() == "Start Recording":
            self.record_btn.setText("Stop Recording")
            filename = f"temp_ch{self.channel}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            self.file = open(filename, "w", newline="")
            self.writer = csv.writer(self.file)
            self.writer.writerow(["Time", "Actual Temperature", "Target Temperature"])
        else:
            self.record_btn.setText("Start Recording")
            if self.file is not None:
                self.file.close()
                self.file = None
                self.writer = None

    def close_recording(self):
        if self.file is not None:
            self.file.close()
            self.file = None
            self.writer = None


class TemperatureControlWidget(QWidget):
    """Container that lays out one TemperatureChannelWidget per channel."""

    readings_signal = pyqtSignal(list)  # list[float] of length controller.channels

    def __init__(self, controller):
        super().__init__()
        self.controller = controller

        layout = QHBoxLayout(self)
        self.channel_widgets = []
        for c in range(1, controller.channels + 1):
            cw = TemperatureChannelWidget(controller, c)
            self.channel_widgets.append(cw)
            layout.addWidget(cw)

        self.readings_signal.connect(self._fanout)
        self.controller.temperature_updating_callback = self._on_callback
        self.controller.actual_temp_updating_thread.start()

    def _on_callback(self, temps):
        # Runs in the controller's polling thread; marshal to the GUI thread.
        self.readings_signal.emit(list(temps))

    def _fanout(self, temps):
        current_time = datetime.now().timestamp()
        for cw, t in zip(self.channel_widgets, temps):
            cw.reading_signal.emit(t, current_time)

    def closeEvent(self, event):
        for cw in self.channel_widgets:
            cw.close_recording()
        event.accept()


class FluidicsControlGUI(QMainWindow):
    def __init__(self, is_simulation):
        super().__init__()
        self.config = load_config_file()
        self.simulation = is_simulation
        self.temperatureController = None

        self.initialize_hardware(self.simulation, self.config)
        self.selectorValveSystem = SelectorValveSystem(self.controller, self.config)

        if self.config.application == "Open Chamber":
            self.discPump = DiscPump(self.controller)
        else:
            self.discPump = None

        self.initUI()

    def initUI(self):
        self.setWindowTitle("Fluidics Control System")
        self.setGeometry(100, 100, 950, 600)

        # Create tab widget
        self.tabWidget = QTabWidget()

        # "Settings and Manual Control" tab
        runExperimentsTab = SequencesWidget(self.config, self.syringePump, self.selectorValveSystem, self.discPump, self.temperatureController)
        manualControlTab = ManualControlWidget(self.config, self.syringePump, self.selectorValveSystem, self.discPump)
        # TODO: integrate temperature controller ui

        self.tabWidget.addTab(runExperimentsTab, "Run Experiments")
        self.tabWidget.addTab(manualControlTab, "Settings and Manual Control")
        if self.temperatureController is not None:
            temperatureControlTab = TemperatureControlWidget(self.temperatureController)
            self.tabWidget.addTab(temperatureControlTab, "Temperature Control")

        self.setCentralWidget(self.tabWidget)
        runExperimentsTab.sequence_running.connect(self.set_manual_control_tab_state)

    def initialize_hardware(self, simulation, config):
        if simulation:
            self.controller = FluidControllerSimulation(config.microcontroller.serial_number)
            self.syringePump = SyringePumpSimulation(
                                sn=config.syringe_pump.serial_number,
                                syringe_ul=config.syringe_pump.volume_ul,
                                speed_code_limit=config.syringe_pump.speed_code_limit,
                                waste_port=config.syringe_pump.waste_port)
            if config.temperature_controller is not None:
                tc_cfg = config.temperature_controller
                self.temperatureController = TCMControllerSimulation(
                    sn=tc_cfg.serial_number,
                    channels=tc_cfg.channels,
                    tolerance_celsius=tc_cfg.tolerance_celsius,
                    stabilization_timeout_seconds=tc_cfg.stabilization_timeout_seconds,
                )
        else:
            self.controller = FluidController(config.microcontroller.serial_number)
            self.syringePump = SyringePump(
                                sn=config.syringe_pump.serial_number,
                                syringe_ul=config.syringe_pump.volume_ul,
                                speed_code_limit=config.syringe_pump.speed_code_limit,
                                waste_port=config.syringe_pump.waste_port)
            if config.temperature_controller is not None:
                try:
                    tc_cfg = config.temperature_controller
                    self.temperatureController = TCMController(
                        sn=tc_cfg.serial_number,
                        channels=tc_cfg.channels,
                        tolerance_celsius=tc_cfg.tolerance_celsius,
                        stabilization_timeout_seconds=tc_cfg.stabilization_timeout_seconds,
                    )
                except Exception as e:
                    msg = f"Failed to initialize temperature controller: {e}"
                    print(msg)
                    self.temperatureController = None
                    QMessageBox.warning(
                        self,
                        "Temperature Controller",
                        f"{msg}\n\nCheck that the serial number in config.yaml "
                        f"matches a connected device. The Temperature Control "
                        f"tab will not be available."
                    )

        self.controller.begin()
        self.controller.send_command(CMD_SET.CLEAR)

    def set_manual_control_tab_state(self, is_running):
        manual_control_tab_index = 1
        self.tabWidget.setTabEnabled(manual_control_tab_index, not is_running)

    def closeEvent(self, event):
        if self.temperatureController is not None:
            self.temperatureController.close()

        if self.config.application == "Open Chamber":
            self.syringePump.close()
        elif self.config.application == "Flow Cell":
            self.syringePump.close(True)
        super().closeEvent(event)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--simulation", help="Run the GUI with simulated hardware.", action='store_true')
    args = parser.parse_args()

    app = QApplication(sys.argv)
    gui = FluidicsControlGUI(args.simulation)
    gui.show()
    sys.exit(app.exec_())
