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
                             QTableWidget, QTableWidgetItem,
                             QGroupBox, QGridLayout, QSizePolicy, QDialog, QFormLayout,
                             QDoubleSpinBox, QDialogButtonBox, QScrollArea,
                             QPlainTextEdit, QSplitter)
from PyQt5.QtCore import (Qt, QTimer, pyqtSignal, QEvent, QCoreApplication,
                          QSettings, QSignalBlocker)
from PyQt5.QtGui import QColor, QBrush

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
from fluidics.events import (RunEnded, RunStarted, SequenceCompleted,
                             SequenceStarted, plan_seconds, repeat_suffix)
from fluidics.files import atomic_write
from fluidics.run_log import (LOGGER_NAME, setup_uncaught_exception_logging,
                             start_log_file)
from fluidics.sequences import (
    load_sequences, save_sequences_yaml,
    get_fields_for_type, validate_sequences, sequence_port_problems,
    sequence_type_problem, types_for_application,
    SEQUENCE_TYPES, SEQUENCE_TYPE_LABELS,
    SequenceListAdapter,
)
from pydantic import ValidationError

import matplotlib.pyplot as plt
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter
import warnings
warnings.filterwarnings('ignore')

_logger = logging.getLogger("fluidics.gui")

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
        available_types = types_for_application(self.application)
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


class PortNamesDialog(QDialog):
    """Rename the reagent ports: one row per port, blank means unnamed.
    After OK, result_mapping holds the config's name_mapping shape --
    {'port_<n>': name} for the named ports only -- and None before."""

    def __init__(self, parent, port_count, name_mapping):
        super().__init__(parent)
        self.setWindowTitle("Port Names")
        self.result_mapping = None
        self._edits = []

        names = name_mapping or {}
        form_host = QWidget()
        form = QFormLayout(form_host)
        for port in range(1, port_count + 1):
            edit = QLineEdit()
            edit.setText(names.get(port_key(port), ""))
            self._edits.append(edit)
            form.addRow(f"Port {port}", edit)

        # A cascade offers a couple dozen ports; the dialog scrolls rather
        # than outgrowing the screen.
        scroll = QScrollArea()
        scroll.setWidget(form_host)
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(min(400, 28 * port_count + 20))

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(scroll)
        layout.addWidget(buttons)

    def accept(self):
        self.result_mapping = {
            port_key(port): name
            for port, edit in enumerate(self._edits, start=1)
            if (name := edit.text().strip())}
        super().accept()


def _ask_yes_no(parent, title, text, default=QMessageBox.No):
    """One spelling of the Yes/No question the GUI asks three ways --
    start a run, resume one, abort on exit."""
    answer = QMessageBox.question(parent, title, text,
                                  QMessageBox.Yes | QMessageBox.No, default)
    return answer == QMessageBox.Yes


def _hms(seconds):
    """Seconds as hh:mm:ss, for the estimate dialog and the countdown."""
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


class GuiLogHandler(logging.Handler):
    """Feeds the run tab's log pane from the fluidics logger.

    Records arrive on whatever thread logged them -- the run's, the MCU
    reader's, a report writer's -- so the text crosses to the Qt thread
    the way every other cross-thread paint does. `detach()` takes it off
    the logger: handlers are held globally, so one left attached would
    outlive its tab and post into a destroyed widget.
    """

    def __init__(self, widget):
        super().__init__(level=logging.INFO)
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="%m/%d %H:%M:%S"))
        self._widget = widget

    def emit(self, record):
        widget = self._widget
        if widget is None:
            return
        try:
            widget._post_event("_handle_log_line", self.format(record))
        except RuntimeError:
            # The C++ widget went out from under us: its tab is gone but
            # this handler is still on the logger, so take it off here --
            # nothing else will, and posting again would be worse.
            self.detach()

    def detach(self):
        """Take this handler off the logger and stop feeding the pane.
        Idempotent: the widget's destruction and a failed post both land
        here, in either order."""
        self._widget = None
        logging.getLogger(LOGGER_NAME).removeHandler(self)

    def close(self):
        self._widget = None
        super().close()


class SequencesWidget(PostsToQtThread, QWidget):
    """Edits the sequence list and runs it through the system's session, which
    owns the run's thread and its end; this widget renders the callbacks.

    The sequence list itself is `_sequences`, a list of dicts -- the tree
    only renders it, and every edit routes back through it. The model holds
    what the operator typed; getSequences() validates and coerces on the
    way out, and live validation asks the same question per row as it goes.
    """

    def __init__(self, config, system):
        super().__init__()
        self.config = config
        self.system = system
        self.session = system.session
        self.selectorValveSystem = system.devices.selector_valves

        self._sequences = []     # THE sequence list; the tree renders it
        self._invalid = {}       # model row -> live-validation message
        self._port_limit = available_port_count(config)   # config-fixed
        self._plan = ()          # the running run's plan, rows = model rows
        # Which sequences the operator has open, by identity: a move swaps
        # the dicts themselves, so an open row follows its sequence rather
        # than staying behind at the index it was rendered at.
        self._opened = set()
        system.warnings.subscribe(self.reportWarning)
        # The run display ends when the session's job does -- whichever way
        # it ends -- rather than riding any one worker callback; the run's
        # boundary facts arrive on the one events channel.
        system.session.state.subscribe(self._onSessionState)
        system.session.events.subscribe(self._onRunEvent)

        self.initUI()

        # The log pane reads the same records the console and the run log
        # file get, at INFO. Detached when the widget goes: logging keeps
        # its handlers globally, so a live handler would outlive the tab.
        self._logHandler = GuiLogHandler(self)
        logging.getLogger(LOGGER_NAME).addHandler(self._logHandler)
        handler = self._logHandler          # not self: the lambda outlives it
        self.destroyed.connect(lambda: handler.detach())

    def initUI(self):
        layout = QVBoxLayout()

        # Tree for displaying sequences
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Property", "Value"])
        self.tree.setColumnCount(2)
        self.tree.setEditTriggers(QTreeWidget.NoEditTriggers)
        self.tree.itemDoubleClicked.connect(self._onItemDoubleClicked)
        self.tree.itemChanged.connect(self._onItemChanged)
        self.tree.itemExpanded.connect(self._onItemOpened)
        self.tree.itemCollapsed.connect(self._onItemClosed)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)

        # What the run log is saying, where the run is being watched. The
        # operator sizes it against the sequences: a splitter rather than a
        # fixed height, since a bench session wants one or the other.
        self.logView = QPlainTextEdit()
        self.logView.setReadOnly(True)
        self.logView.setMaximumBlockCount(self.LOG_LINES)
        self.logView.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.exportLogButton = QPushButton("Export Log...")
        self.exportLogButton.clicked.connect(self.exportLog)
        logHeader = QHBoxLayout()
        logHeader.addWidget(QLabel("Log"))
        logHeader.addStretch()
        logHeader.addWidget(self.exportLogButton)
        logPane = QWidget()
        logLayout = QVBoxLayout(logPane)
        logLayout.setContentsMargins(0, 0, 0, 0)
        logLayout.addLayout(logHeader)
        logLayout.addWidget(self.logView)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.tree)
        splitter.addWidget(logPane)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter)

        # The list's editing verbs on one row, the run's controls on the next.
        self.loadButton = QPushButton("Load")
        self.loadButton.clicked.connect(self.loadSequences)
        self.saveButton = QPushButton("Save")
        self.saveButton.clicked.connect(self.saveSequences)
        self.addButton = QPushButton("Add Sequence")
        self.addButton.clicked.connect(self.addSequence)
        self.removeButton = QPushButton("Remove")
        self.removeButton.clicked.connect(self.removeSequence)
        self.duplicateButton = QPushButton("Duplicate")
        self.duplicateButton.clicked.connect(self.duplicateSequence)
        self.moveUpButton = QPushButton("Move Up")
        self.moveUpButton.clicked.connect(self.moveSequenceUp)
        self.moveDownButton = QPushButton("Move Down")
        self.moveDownButton.clicked.connect(self.moveSequenceDown)
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

        editLayout = QHBoxLayout()
        for button in (self.loadButton, self.saveButton, self.addButton,
                       self.removeButton, self.duplicateButton,
                       self.moveUpButton, self.moveDownButton,
                       self.selectAllButton, self.selectNoneButton):
            editLayout.addWidget(button)
        layout.addLayout(editLayout)

        runLayout = QHBoxLayout()
        for button in (self.runButton, self.pauseButton, self.abortButton):
            runLayout.addWidget(button)
        layout.addLayout(runLayout)

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
        # Count, bar and countdown share one line: three short readings
        # that belong together, and the tab's vertical room belongs to the
        # sequences and the log.
        statusRow = QHBoxLayout()
        statusRow.addWidget(self.sequenceLabel)
        statusRow.addWidget(self.progressBar, 1)
        statusRow.addWidget(self.timeLabel)
        progressSection.addLayout(statusRow)
        progressSection.addWidget(self.warningLabel)

        # Reagent drawn per port since the run began (manual draws show
        # between runs). Hidden until something has been drawn.
        self.usageTable = QTableWidget(0, 3)
        self.usageTable.setHorizontalHeaderLabels(["Port", "Reagent", "Used (\u00b5L)"])
        self.usageTable.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.usageTable.verticalHeader().setVisible(False)
        self.usageTable.setEditTriggers(QTableWidget.NoEditTriggers)
        self.usageTable.setMaximumHeight(120)
        self.usageTable.setVisible(False)
        progressSection.addWidget(self.usageTable)

        layout.addLayout(progressSection)

        self.setLayout(layout)

        # Timer for updating time remaining
        self.timer = QTimer()
        self.timer.timeout.connect(self.updateTimeRemaining)
        self.total_time = None

    # What the log pane keeps. Enough to cover a long run's narration
    # without holding the whole session's text in the widget -- the run
    # log file on disk is the complete record.
    LOG_LINES = 2000

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

    # --- the model, and the tree that renders it ---
    #
    # Rendering writes to the tree under QSignalBlocker (reentrant: it
    # restores the previous state), so a paint never reads back as an edit.

    def setSequences(self, sequences):
        """Replace the model and render it: the dicts are the truth, the
        tree their view."""
        self._sequences = [dict(seq) for seq in sequences]
        # A new file's rows are not the old file's: it opens collapsed,
        # one line per sequence, whatever was open before. Not merely the
        # prune's job -- the old dicts are freed here, and a new one
        # allocated at a remembered address would render open by accident.
        self._opened.clear()
        self._refresh()

    def _refresh(self, select=None):
        """Re-validate and re-render the whole model -- the close of every
        structural change (load, add, remove, move, duplicate). A field
        edit updates in place instead, so it cannot steal the cursor."""
        self._validateAll()
        self._renderTree()
        if select is not None and 0 <= select < self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(select))
        self._renderRunControls()

    def _renderTree(self):
        # Sequences that have left the model take their open state with
        # them -- and a later dict could otherwise be allocated at a
        # remembered address and render open for no reason.
        self._opened &= {id(seq) for seq in self._sequences}
        with QSignalBlocker(self.tree):
            self.tree.clear()
            for seq in self._sequences:
                self._renderSequenceRow(seq)
        self._renderValidation()

    def _renderSequenceRow(self, seq):
        """One top-level item per sequence; one child per field of its type
        -- every field, defaults included, so a value at its default can
        still be edited."""
        seq_type = seq.get('type', '')
        type_label = SEQUENCE_TYPE_LABELS.get(seq_type, seq_type)
        item = QTreeWidgetItem([seq.get('name') or type_label, f"Type: {type_label}"])
        item.setFlags(item.flags() | Qt.ItemIsEditable)
        item.setCheckState(0, Qt.Checked if self._isIncluded(seq) else Qt.Unchecked)

        try:
            type_fields = get_fields_for_type(seq_type)
        except ValueError:
            type_fields = {}
        for fname, finfo in type_fields.items():
            if fname in ('include', 'name'):
                continue
            value = seq.get(fname, finfo.default)
            child = QTreeWidgetItem([self.FIELD_LABELS.get(fname, fname),
                                     '' if value is None else str(value)])
            child.setData(0, Qt.UserRole, fname)
            child.setFlags(child.flags() | Qt.ItemIsEditable)
            item.addChild(child)

        self.tree.addTopLevelItem(item)
        item.setExpanded(id(seq) in self._opened)

    def _handle_log_line(self, line):
        """On the Qt thread: the pane keeps the last LOG_LINES lines, and
        follows the tail only when the operator is already at it -- a
        scroll back to read something must not be yanked forward."""
        bar = self.logView.verticalScrollBar()
        at_tail = bar.value() >= bar.maximum() - 2
        self.logView.appendPlainText(line)
        if at_tail:
            bar.setValue(bar.maximum())

    def exportLog(self):
        """Write what the pane is showing to a file the operator picks --
        the window's text, not the whole rolling log file on disk."""
        default = time.strftime("fluidics-log-%Y%m%d-%H%M%S.txt")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Log", default, "Text Files (*.txt)")
        if not path:
            return
        try:
            with atomic_write(path) as f:
                f.write(self.logView.toPlainText() + "\n")
        except OSError as e:
            QMessageBox.critical(self, "Export Failed",
                                 f"Could not write {path}:\n{e}")
            return
        _logger.info("Log exported to %s", path)

    def _onItemOpened(self, item):
        self._noteOpen(item, True)

    def _onItemClosed(self, item):
        self._noteOpen(item, False)

    def _noteOpen(self, item, is_open):
        """Remember what the operator opened or closed. Rendering runs
        under QSignalBlocker, so only their own clicks reach here."""
        row = self.tree.indexOfTopLevelItem(item)
        if not 0 <= row < len(self._sequences):
            return                       # a field row, or mid-rebuild
        seq_id = id(self._sequences[row])
        self._opened.add(seq_id) if is_open else self._opened.discard(seq_id)

    def _onItemChanged(self, item, column):
        """An edit or a checkbox toggle: write it into the model, then
        revalidate that row. The tree holds no state of its own."""
        parent = item.parent()
        top = parent if parent is not None else item
        row = self.tree.indexOfTopLevelItem(top)
        if not 0 <= row < len(self._sequences):
            return
        seq = self._sequences[row]
        if parent is None:
            seq['include'] = item.checkState(0) == Qt.Checked
            name = item.text(0).strip()
            type_label = SEQUENCE_TYPE_LABELS.get(seq.get('type'), '')
            seq['name'] = name if name and name != type_label else None
            # The title renders from the model, always: an emptied name --
            # or the type's label typed out -- reads as the type again.
            with QSignalBlocker(self.tree):
                item.setText(0, seq['name'] or type_label)
        else:
            fname = item.data(0, Qt.UserRole)
            raw = item.text(1).strip()
            seq[fname] = raw if raw else None
        self._validateRow(row)
        self._renderValidation()
        self._renderRunControls()

    # --- live validation ---

    def _validateAll(self):
        self._invalid = {}
        for row in range(len(self._sequences)):
            self._validateRow(row)

    def _validateRow(self, row):
        problem = self._rowProblem(self._sequences[row])
        if problem is None:
            self._invalid.pop(row, None)
        else:
            self._invalid[row] = problem

    def _rowProblem(self, seq):
        """The verdict on one row, as a message or None. A pure question:
        the model is never rewritten -- it holds what the operator typed;
        the coercion happens on a copy here, and for real in getSequences."""
        # The type first: a wrong-application row is also union-valid, and
        # for an unknown type this message beats the union's tag complaint.
        type_problem = sequence_type_problem(seq, self.config.application)
        if type_problem is not None:
            return type_problem
        try:
            validated = SequenceListAdapter.validate_python([seq])
        except ValidationError as e:
            first = e.errors()[0]
            field = ".".join(str(part) for part in first["loc"][2:]) or "sequence"
            return f"{field}: {first['msg']}"
        problems = sequence_port_problems(validated[0].model_dump(),
                                          self._port_limit)
        if problems:
            return ("; ".join(problems)
                    + f": this configuration has ports 1..{self._port_limit}")
        return None

    def _renderValidation(self):
        """Paint the verdicts: an invalid row is red, with the error as its
        tooltip."""
        red = QBrush(QColor('red'))
        clear = QBrush()
        with QSignalBlocker(self.tree):
            for row in range(self.tree.topLevelItemCount()):
                item = self.tree.topLevelItem(row)
                message = self._invalid.get(row)
                for column in (0, 1):
                    item.setForeground(column, red if message else clear)
                item.setToolTip(0, message or '')

    def _blockingError(self):
        """The first error among the rows a run would actually take --
        an invalid row that is not checked blocks nothing."""
        for row in self._includedRows():
            if row in self._invalid:
                return f"Sequence {row + 1}: {self._invalid[row]}"
        return None

    # --- reading the model out ---

    @staticmethod
    def _isIncluded(seq):
        """The include field, defaulting on -- the one spelling of what the
        checkbox means."""
        return seq.get('include', True)

    def _includedRows(self):
        """Model rows a run takes, in order."""
        return [row for row, seq in enumerate(self._sequences)
                if self._isIncluded(seq)]

    def getSequences(self, selected_only=False):
        """The model, validated and coerced -- the dicts a run or a save
        takes. selected_only reads exactly _includedRows(), so a snapshot
        of that list stays index-aligned with the sequences handed to the
        worker."""
        rows = self._includedRows() if selected_only else range(len(self._sequences))
        validated = SequenceListAdapter.validate_python(
            [self._sequences[row] for row in rows])
        return [s.model_dump() for s in validated]

    def loadSequences(self):
        fileName, _ = QFileDialog.getOpenFileName(
            self, "Open Sequences", "",
            "Sequence Files (*.yaml *.yml *.csv)")
        if fileName:
            try:
                self.setSequences(load_sequences(fileName))
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
            self._sequences.append(dict(dialog.result_dict))
            self._refresh(select=len(self._sequences) - 1)

    def _currentRow(self):
        """The model row of the selected sequence -- a selected child means
        its parent; None with nothing selected."""
        item = self.tree.currentItem()
        if item is None:
            return None
        if item.parent() is not None:
            item = item.parent()
        row = self.tree.indexOfTopLevelItem(item)
        return row if row >= 0 else None

    def removeSequence(self):
        row = self._currentRow()
        if row is None:
            return
        self._sequences.pop(row)
        self._refresh(select=min(row, len(self._sequences) - 1))

    def duplicateSequence(self):
        row = self._currentRow()
        if row is None:
            return
        self._sequences.insert(row + 1, dict(self._sequences[row]))
        self._refresh(select=row + 1)

    def moveSequenceUp(self):
        self._moveSequence(-1)

    def moveSequenceDown(self):
        self._moveSequence(+1)

    def _moveSequence(self, delta):
        row = self._currentRow()
        if row is None:
            return
        target = row + delta
        if not 0 <= target < len(self._sequences):
            return
        seqs = self._sequences
        seqs[row], seqs[target] = seqs[target], seqs[row]
        self._refresh(select=target)

    def selectAll(self):
        self._setAllIncluded(True)

    def selectNone(self):
        self._setAllIncluded(False)

    def _setAllIncluded(self, included):
        for seq in self._sequences:
            seq['include'] = included
        self._refresh()

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
        if not self._sequences:
            return
        try:
            selected = self.getSequences(selected_only=True)
            # A port this rig does not have, or a wrong-application type,
            # must fail here, at the button, not hours in -- live
            # validation paints the same verdicts, but this is the gate.
            validate_sequences(selected, self.config)
        except Exception as e:
            QMessageBox.critical(self, "Invalid Sequence", f"Failed to validate sequences: {str(e)}")
            return
        if not selected:
            QMessageBox.warning(self, "No Sequences Selected", "Please select at least one sequence to run.")
            return

        # Planned here, before anything starts, so the operator confirms
        # with the figure in hand; the same plan rides into the run below,
        # so the dialog and the run cannot disagree. RunStarted brings it
        # back with the run's id, and the display paints from that.
        plan = self.system.plan(selected)
        if not self._confirmStart(plan_seconds(plan), len(plan)):
            return
        # The plan's rows index `selected`; this widget's rows include the
        # unchecked ones. Relabel once, so highlight and resume land on
        # tree rows without a translation table riding alongside.
        rows = self._includedRows()
        plan = tuple(entry._replace(row=rows[entry.row]) for entry in plan)

        self._warnings.clear()
        self.warningLabel.setVisible(False)
        self._startRun(selected, plan)

    def _startRun(self, sequences, plan):
        """Hand the job to the rig and open this tab's display -- the one
        ending shared by the Run button and the resume offer, so the two
        start sites cannot drift. The tabs (and, for the offer, the modal
        chain) make a busy rig unreachable in practice; the dialog beats a
        traceback if it ever is not."""
        try:
            self.system.run(sequences, plan=plan)
        except RuntimeError as e:
            QMessageBox.warning(self, "Rig busy", str(e))
            return
        self._beginRunDisplay(len(plan))

    def _confirmStart(self, seconds, n_sequences):
        """The operator sees the bill before the run starts: how many
        sequences, how long they should take. True to go ahead."""
        return _ask_yes_no(
            self, "Start run?",
            f"{n_sequences} sequence(s), estimated {_hms(seconds)}.\n\nStart the run?",
            default=QMessageBox.Yes)

    def _beginRunDisplay(self, count):
        """A fresh run's display. The previous run's estimate must not price
        this one while RunStarted is still in the post."""
        self.total_time = None
        self._renderRunControls()
        self.sequenceLabel.setText(f"0/{count} sequences")
        self.timer.start(1000)

    def updateTimeRemaining(self):
        """The one-second repaint: read the session's clock, paint, and keep
        the buttons honest.

        Runs until the session's state change stops it (_handle_state), not
        until the estimate hits zero: a run can outlive its estimate, and a
        flow fault cancels from the reader thread with only this tick
        watching.
        """
        self._showTimeRemaining()
        self._renderRunControls()
        self._renderUsage()

    def _renderUsage(self):
        """The per-port totals as they stand; the ledger's rows carry the
        names, read fresh per call, so a rename shows on the next tick."""
        rows = self.system.usage.rows()
        self.usageTable.setVisible(bool(rows))
        self.usageTable.setRowCount(len(rows))
        for index, (port, name, used) in enumerate(rows):
            for column, text in enumerate((str(port), name or "",
                                           f"{used:.0f}")):
                self.usageTable.setItem(index, column, QTableWidgetItem(text))

    def _showTimeRemaining(self):
        """Draw the label and the bar from one session snapshot: the clock a
        line prints and the pause it names come from the same instant."""
        if self.total_time is None:
            # The estimate is posted to this thread's queue, so it can still
            # be pending when the operator presses Pause. The button already
            # says what happened; the next tick writes the label.
            return
        snap = self.session.snapshot()
        suffix = self._pauseSuffix(snap.paused, snap.at_rest)
        remaining = max(0, self.total_time - snap.elapsed_seconds)
        self.timeLabel.setText(f"{_hms(remaining)} remaining{suffix}")

        progress = min(100, int((snap.elapsed_seconds / max(self.total_time, 1)) * 100))
        self.progressBar.setValue(progress)

    @staticmethod
    def _pauseSuffix(paused, at_rest):
        """What the time label says about a pause, if anything."""
        if at_rest:
            return " (paused)"
        if paused:
            return " (pausing\u2026)"
        return ""

    def _renderRunControls(self):
        """Draw the run buttons from the state of the run -- and of the list:
        a selection with an invalid row cannot start.

        One place, because four methods used to poke text and enabled
        directly and could drift apart -- and because a run can end without
        any of them being called: a flow fault cancels from the MCU reader
        thread, and the buttons must go dead then too.
        """
        snap = self.session.snapshot()
        live = snap.kind == "run" and not snap.cancelled
        idle = snap.kind is None
        error = self._blockingError()
        self.runButton.setEnabled(idle and error is None)
        self.runButton.setToolTip(error or "")
        self.pauseButton.setEnabled(live)
        self.abortButton.setEnabled(live)
        self.pauseButton.setText("Resume" if snap.paused else "Pause")
        # The list is frozen while a job rides it: the plan's rows are a
        # snapshot of the model, and a mid-run move would walk the highlight
        # (and the run's meaning) to the wrong row.
        for button in (self.loadButton, self.addButton, self.removeButton,
                       self.duplicateButton, self.moveUpButton,
                       self.moveDownButton):
            button.setEnabled(idle)

    def pauseSequences(self):
        """Hold the run after the move in flight, or let it go on."""
        if self.session.paused:
            self.session.resume()
        else:
            self.session.pause()
        self._renderRunControls()
        # The label would otherwise wait for the next tick to say anything.
        self._showTimeRemaining()

    def _handle_run_event(self, event):
        """One handler for the run's boundary facts; continuous state stays
        with the tick's snapshot reads."""
        if isinstance(event, RunStarted):
            self._plan = event.plan
            self.total_time = plan_seconds(event.plan)
            self.progressBar.setMaximum(100)  # For percentage
            self.progressBar.setValue(0)
        elif isinstance(event, SequenceStarted):
            self.sequenceLabel.setText(
                f"{event.position + 1}/{len(self._plan)} sequences")
            self.highlightRow(self._plan[event.position].row)
        elif isinstance(event, SequenceCompleted):
            # Re-anchor: whatever the finished sequences actually took, what
            # remains is the estimate of the ones not yet run, from now.
            self.total_time = (self.session.elapsed_seconds
                               + plan_seconds(self._plan[event.position + 1:]))
        elif isinstance(event, RunEnded):
            self._reportRunEnded(event)

    def _reportRunEnded(self, event):
        """The run's last word, delivered with the rig already free (the
        session clears kind and signal before publishing): one dialog for
        how it ended -- shown over the run display as it stood, the state
        reset follows -- then the resume offer when there is somewhere to
        resume from."""
        if event.outcome == "stopped":
            QMessageBox.information(self, "Stopped", "The run was stopped.")
        elif event.outcome == "failed":
            QMessageBox.critical(self, "Error", event.message)
        else:
            QMessageBox.information(self, "Finished",
                                    "Sequence execution finished.")
        if event.position is not None:
            self._offerResume(event.position)

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

    def _offerResume(self, position):
        """After an early end: offer to run the plan's tail -- the entry
        that was in flight (its repeat re-run whole, never skipped) and
        everything after it -- as a new run. The offer names the resume
        point and carries the tail's own bill, so it is the confirm dialog
        too; Yes starts the run. The checkboxes are not touched: the tail
        is the interrupted experiment's remainder, whatever the tree says
        by then.

        Structural edits are disabled during the run, so `entry.row`
        still names the tree row the operator sees; and from the RunEnded
        dialog through this question the Qt thread never leaves the modal
        chain, so the rig stays free until Yes claims it.
        """
        tail = self._plan[position:]
        entry = tail[0]
        # The plan's own captured label: field edits stay live during a
        # run, and the offer must name what will actually execute, not
        # what the tree says by now.
        label = entry.label
        which = repeat_suffix(entry)
        if not _ask_yes_no(
                self, "Resume from here?",
                f"The run ended at sequence {entry.row + 1} ({label}{which}).\n\n"
                f"Resume from there? {len(tail)} sequence(s) remain, "
                f"estimated {_hms(plan_seconds(tail))}."):
            return
        _logger.info("Resume accepted: %d sequence(s) from row %d (%s%s).",
                     len(tail), entry.row + 1, label, which)
        self._startRun(None, tail)

    def _onSessionState(self, kind):
        # On the session's thread; the display change crosses to Qt. The
        # payload stays behind: the repaint reads the session as it stands
        # at delivery, so no queued notification can be stale.
        self._post_event('_handle_state')

    def _handle_state(self):
        """The run display follows the session, not any one callback: the
        job ending -- finished, aborted, or a fault's self-cancel -- is what
        stops the clock and clears the run's furniture. RunEnded's dialog
        lands first (over the still-painted run, highlight included); this
        reset follows it in the posted order. Every transition redraws the
        buttons, so a manual job deadens this tab's controls without
        leaning on the main window's tab guard.

        Repaints from the session's current state, never from what was
        announced: the resume offer starts the tail from inside the old
        run's RunEnded dialog chain, and the old run's state(None) can
        still be queued behind it -- a reset keyed to the delivery would
        stop the new run's clock and clear its display."""
        if self.session.kind is None:
            self.timer.stop()
            self.progressBar.setValue(0)
            self.timeLabel.setText("00:00:00 remaining")
            self.sequenceLabel.setText("0/0 sequences")
            self.highlightRow(None)
        self._renderRunControls()
        self._renderUsage()

    def _onRunEvent(self, event):
        # On the worker's or session's thread; the paint crosses to Qt.
        self._post_event('_handle_run_event', event)

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
        # The plunger bar paints what the pump publishes after each of its
        # own readings; the GUI thread never touches the serial line.
        system.devices.syringe_pump.held_volume.subscribe(self._onHeldVolume)

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
        port = self.valveCombo.currentIndex() + 1
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
        dialog = PortNamesDialog(self, available_port_count(self.config),
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
            current = self.valveCombo.currentIndex()
            self.valveCombo.clear()
            self.valveCombo.addItems(self.manual.port_names())
            self.valveCombo.setCurrentIndex(current)

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

    def _onHeldVolume(self, volume_ul):
        # On the pump's thread; the paint crosses to Qt.
        self._post_event('_handle_held_volume', volume_ul)

    def _handle_held_volume(self, volume_ul):
        self.plungerPositionBar.setValue(int(volume_ul))

    def showEvent(self, event):
        super().showEvent(event)
        # Show where the valves are; do not move them there.
        self.valveCombo.blockSignals(True)
        self.valveCombo.setCurrentIndex(self.manual.current_port() - 1)
        self.valveCombo.blockSignals(False)

    def closeEvent(self, event):
        self.progress_timer.stop()
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
