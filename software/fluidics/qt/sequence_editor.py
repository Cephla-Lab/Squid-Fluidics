"""The standalone sequence editor and run display (SequencesWidget) and its Add-sequence dialog.
Moved verbatim from gui.py; imports go through qtpy."""

import logging
import time

from pydantic import ValidationError
from qtpy.QtCore import Qt, QSignalBlocker, QTimer
from qtpy.QtGui import QBrush, QColor
from qtpy.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from fluidics.control.config import available_port_count
from fluidics.events import RunEnded, RunStarted, SequenceCompleted, SequenceStarted, plan_seconds, repeat_suffix
from fluidics.files import atomic_write
from fluidics.qt.support import GuiLogHandler, PostsToQtThread, _ask_yes_no, _hms, subscribe_until_detached
from fluidics.run_log import LOGGER_NAME
from fluidics.sequence_list import SequenceList, type_label
from fluidics.sequences import (
    SEQUENCE_TYPE_LABELS,
    get_fields_for_type,
    load_sequences,
    save_sequences_yaml,
    types_for_application,
    validate_sequences,
)

_logger = logging.getLogger("fluidics.gui")


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

            if field_name in ('name', 'round'):
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
    owns the run's thread and its end; this widget renders the callbacks.

    The sequence list itself is `_model`, a Qt-free SequenceList -- the
    tree only renders it, and every edit routes back through it. The model
    holds what the operator typed, judges each row as it goes, and coerces
    on the way out (getSequences); this widget paints those verdicts and
    turns clicks into its verbs.
    """

    def __init__(self, config, system):
        super().__init__()
        self.config = config
        self.system = system
        self.session = system.session
        self.selectorValveSystem = system.devices.selector_valves

        # THE sequence list (fluidics.sequence_list, Qt-free): it holds the
        # dicts, validates them and performs the structural verbs; this
        # widget renders it and turns clicks into its calls.
        self._model = SequenceList(config.application,
                                   available_port_count(config))
        self._plan = ()          # the running run's plan, rows = model rows
        # Which sequences the operator has open, by identity: a move swaps
        # the dicts themselves, so an open row follows its sequence rather
        # than staying behind at the index it was rendered at.
        self._opened = set()
        # The run display ends when the session's job does -- whichever way
        # it ends -- rather than riding any one worker callback; the run's
        # boundary facts arrive on the one events channel. Detached on
        # destroyed: an embedded tab must not outlive itself.
        detach = subscribe_until_detached(
            (system.warnings, self.reportWarning),
            (system.session.state, self._onSessionState),
            (system.session.events, self._onRunEvent),
        )
        self.destroyed.connect(detach)

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
        'round': 'Round',
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
        self._model.replace(sequences)
        # A new file's rows are not the old file's: it opens collapsed,
        # one line per sequence, whatever was open before. Not merely the
        # prune's job -- the old dicts are freed here, and a new one
        # allocated at a remembered address would render open by accident.
        self._opened.clear()
        self._refresh()

    def _refresh(self, select=None):
        """Re-render the whole model -- the close of every structural
        change (load, add, remove, move, duplicate); the model revalidates
        itself as it changes. A field edit updates in place instead, so it
        cannot steal the cursor."""
        self._renderTree()
        if select is not None and 0 <= select < self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(select))
        self._renderRunControls()

    def _renderTree(self):
        # Sequences that have left the model take their open state with
        # them -- and a later dict could otherwise be allocated at a
        # remembered address and render open for no reason.
        self._opened &= {id(seq) for seq in self._model}
        with QSignalBlocker(self.tree):
            self.tree.clear()
            for seq in self._model:
                self._renderSequenceRow(seq)
        self._renderValidation()

    def _renderSequenceRow(self, seq):
        """One top-level item per sequence; one child per field of its type
        -- every field, defaults included, so a value at its default can
        still be edited."""
        seq_type = seq.get('type', '')
        label = type_label(seq)
        item = QTreeWidgetItem([seq.get('name') or label, f"Type: {label}"])
        item.setFlags(item.flags() | Qt.ItemIsEditable)
        item.setCheckState(
            0, Qt.Checked if SequenceList.is_included(seq) else Qt.Unchecked)

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
        if not 0 <= row < len(self._model):
            return                       # a field row, or mid-rebuild
        seq_id = id(self._model[row])
        self._opened.add(seq_id) if is_open else self._opened.discard(seq_id)

    def _onItemChanged(self, item, column):
        """An edit or a checkbox toggle: write it into the model, then
        revalidate that row. The tree holds no state of its own."""
        parent = item.parent()
        top = parent if parent is not None else item
        row = self.tree.indexOfTopLevelItem(top)
        if not 0 <= row < len(self._model):
            return
        if parent is None:
            self._model.set_included(row, item.checkState(0) == Qt.Checked)
            # The title renders from the model, always: an emptied name --
            # or the type's label typed out -- reads as the type again.
            with QSignalBlocker(self.tree):
                item.setText(0, self._model.set_name(row, item.text(0)))
        else:
            self._model.set_field(row, item.data(0, Qt.UserRole),
                                  item.text(1).strip())
        self._renderValidation()
        self._renderRunControls()

    # --- live validation (judged by the model, painted here) ---

    def _renderValidation(self):
        """Paint the verdicts: an invalid row is red, with the error as its
        tooltip."""
        red = QBrush(QColor('red'))
        clear = QBrush()
        with QSignalBlocker(self.tree):
            for row in range(self.tree.topLevelItemCount()):
                item = self.tree.topLevelItem(row)
                message = self._model.problem(row)
                for column in (0, 1):
                    item.setForeground(column, red if message else clear)
                item.setToolTip(0, message or '')

    def _blockingError(self):
        """What stops a run, as the operator should hear it."""
        return self._model.blocking_error()

    # --- reading the model out ---

    def _includedRows(self):
        """Model rows a run takes, in order."""
        return self._model.included_rows()

    def getSequences(self, selected_only=False):
        """The model, validated and coerced -- the dicts a run or a save
        takes."""
        return self._model.validated(included_only=selected_only)

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
            self._refresh(select=self._model.add(dialog.result_dict))

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
        self._refresh(select=self._model.remove(row))

    def duplicateSequence(self):
        row = self._currentRow()
        if row is None:
            return
        self._refresh(select=self._model.duplicate(row))

    def moveSequenceUp(self):
        self._moveSequence(-1)

    def moveSequenceDown(self):
        self._moveSequence(+1)

    def _moveSequence(self, delta):
        row = self._currentRow()
        if row is None:
            return
        target = self._model.move(row, delta)
        if target is not None:
            self._refresh(select=target)

    def selectAll(self):
        self._setAllIncluded(True)

    def selectNone(self):
        self._setAllIncluded(False)

    def _setAllIncluded(self, included):
        self._model.set_all_included(included)
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
        if not len(self._model):
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
