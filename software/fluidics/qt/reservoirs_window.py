"""The Reservoirs window: every reservoir volume tracking knows, what each
has given since its last reset and, where the config declares a volume,
what is left of it and what was loaded -- with the resets. A tool window
the Run Experiments tab opens from Reservoirs…, on every rig, declared
volumes or not. It reads the volume tracker's balances() and nothing
else; each reset is the manual refill verb, run as a job on the session under the
opener's `job_source`, so a reset waits for an idle rig like any move, a
run's gate reads the file it rewrote, and the main window keeps the
opener's tab live under it."""

from qtpy.QtCore import QItemSelection, QItemSelectionModel, Qt, QTimer
from qtpy.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
                            QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from fluidics.reservoirs import describe_keys
from fluidics.vocabulary import sipper_vocabulary

from .support import PostsToQtThread, _ask_yes_no, connect_click, subscribe_until_detached

COLUMNS = ("Reservoir", "Reagent", "Accumulated (µL)", "Current (µL)", "Loaded (µL)")


def _volume(ul):
    """A volume cell: blank where the reservoir declares none."""
    return "" if ul is None else f"{ul:.0f}"


class ReservoirsWindow(PostsToQtThread, QWidget):
    """One table, the volume tracker's balances, and the resets: Reset
    selected (the highlighted rows), Reset <plate> per plate (a plate
    reloads as a plate) and Reset all. Never modal, and owned by the tab that opens it,
    so it goes with that tab.

    While it is open it repaints itself each second -- a run's draws, a
    manual draw, a port renamed on the Manual tab all show within one --
    and on every session state change, which it hears itself, so a job's
    start and end (its own resets' among them) show at once whoever
    opened it; closed, it paints nothing, and opening it paints it. The
    resets are live only while the rig is idle, read from the session at
    each paint."""

    def __init__(self, config, system, job_source, parent=None):
        super().__init__(parent, Qt.Tool)
        self.setWindowTitle("Reservoirs")
        self.system = system
        self.session = system.session
        self.tracker = system.devices.reservoirs
        self._jobSource = job_source
        self._keys = []             # the reservoir each row shows, as last painted
        # Detached on destroyed: the window goes with its opener, the rig does not.
        self.destroyed.connect(subscribe_until_detached(
            (system.session.state, self._onSessionState)))

        layout = QVBoxLayout(self)
        # The reagent column takes the width; the numeric ones size to
        # their contents, so their headers show whole.
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        header = self.table.horizontalHeader()
        for column in range(len(COLUMNS)):
            header.setSectionResizeMode(
                column, QHeaderView.Stretch if column == 1 else QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.itemSelectionChanged.connect(self._paintResets)
        layout.addWidget(self.table)

        self.undeclaredLabel = QLabel("")
        self.undeclaredLabel.setWordWrap(True)
        self.undeclaredLabel.setStyleSheet("color: #b36b00;")
        self.undeclaredLabel.setVisible(False)
        layout.addWidget(self.undeclaredLabel)

        resets = QHBoxLayout()
        self.resetSelectedButton = QPushButton("Reset selected")
        connect_click(self.resetSelectedButton, self.resetSelected)
        resets.addWidget(self.resetSelectedButton)
        resets.addStretch()
        self.resetPlateButtons = {}
        sipper = sipper_vocabulary(config)
        for name in sorted(sipper.reservoirs) if sipper is not None else ():
            button = QPushButton(f"Reset {name}")
            connect_click(button, lambda name=name: self.resetPlate(name))
            resets.addWidget(button)
            self.resetPlateButtons[name] = button
        self.resetAllButton = QPushButton("Reset all")
        connect_click(self.resetAllButton, self.resetAll)
        resets.addWidget(self.resetAllButton)
        layout.addLayout(resets)
        self.resize(640, 480)

        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self.refresh)

    # --- the paint ---

    def showEvent(self, event):
        """Opened, or brought back: the volumes as they stand now, the names
        read fresh (a port renamed on the Manual tab while no job ran)."""
        super().showEvent(event)
        self._tick.start()
        self.refresh()

    def hideEvent(self, event):
        """Closed: nothing to keep current."""
        self._tick.stop()
        super().hideEvent(event)

    def _onSessionState(self, kind):
        # On the session's thread; the paint crosses to Qt, and reads the
        # session when it lands rather than trusting `kind` (RunSession.state).
        self._post_event("refresh")

    def refresh(self):
        """Paint the volumes and the resets as they stand. A row's cells are
        rewritten in place, so a tick does not disturb the selection; when
        the rows themselves change (a volume tracker built without its
        bottles lists one at its first draw), the selection follows the
        reservoirs rather than the row numbers, so a reset of the selection
        resets what the operator picked."""
        if not self.isVisible():
            return
        rows = self.tracker.balances()
        selected = set(self.selectedKeys())
        self.table.setRowCount(len(rows))
        for index, (key, reagent, accumulated, current, loaded, undeclared) in enumerate(rows):
            cells = (key, reagent or "", f"{accumulated:.0f}",
                     "undeclared" if undeclared else _volume(current), _volume(loaded))
            for column, text in enumerate(cells):
                self._paintCell(index, column, text)
        keys = [key for key, *_ in rows]
        if keys != self._keys:
            self._keys = keys
            self._select(selected)
        # The rows' own flags: the line and the table read one instant.
        undeclared = [key for key, *_, flag in rows if flag]
        self.undeclaredLabel.setText(
            f"Undeclared: {describe_keys(undeclared, self.tracker.tracked)}. "
            "A run is refused until they are reset." if undeclared else "")
        self.undeclaredLabel.setVisible(bool(undeclared))
        self._paintResets()

    def _paintCell(self, row, column, text):
        item = self.table.item(row, column)
        if item is None:
            self.table.setItem(row, column, QTableWidgetItem(text))
        elif item.text() != text:
            item.setText(text)

    def selectedKeys(self):
        """The highlighted reservoirs, in table order."""
        rows = sorted(index.row() for index in self.table.selectionModel().selectedRows())
        return [self._keys[row] for row in rows if row < len(self._keys)]

    def _select(self, keys):
        model = self.table.model()
        selection = QItemSelection()
        for row, key in enumerate(self._keys):
            if key in keys:
                selection.select(model.index(row, 0), model.index(row, model.columnCount() - 1))
        self.table.selectionModel().select(selection, QItemSelectionModel.ClearAndSelect)

    def _paintResets(self):
        """A reset is a manual job: it waits for an idle rig like any
        other."""
        idle = not self.session.busy
        self.resetSelectedButton.setEnabled(idle and bool(self.selectedKeys()))
        for button in self.resetPlateButtons.values():
            button.setEnabled(idle)
        self.resetAllButton.setEnabled(idle and bool(self._keys))

    # --- the resets ---

    def resetSelected(self):
        keys = self.selectedKeys()
        if keys:
            self._reset(keys, describe_keys(keys, self.tracker.tracked))

    def resetPlate(self, name):
        self._reset([name], f"every well of {name}")

    def resetAll(self):
        self._reset(["all"], "every reservoir")

    def _reset(self, keys, what):
        """Hear the verb's refusal if there is one, confirm, and run the
        resets as one manual job on the session, one write: all of them,
        or none, so "not recorded" is true when it is said. The refusal
        (the tip in that plate, or not known to be retracted) is said
        before the question: a dialog after "yes" would read as a failure,
        not a rule."""
        manual = self.system.manual
        for key in keys:
            refusal = manual.refill_refusal(key)
            if refusal:
                QMessageBox.warning(self, "Reset", refusal)
                return
        if not _ask_yes_no(self, "Reset?", f"Reset {what}? Accumulated goes to 0, and a "
                                           "declared volume reads full again."):
            return

        try:
            self.system.run_manual(lambda: manual.refill(*keys), callbacks={
                "on_error": lambda message: self._post_event("_resetFailed", message),
            }, source=self._jobSource)
        except RuntimeError as e:
            QMessageBox.warning(self, "Rig busy", str(e))
            return
        # The session took the job before run_manual returned, and its
        # posted state change cannot land before this does: paint now.
        self.refresh()

    def _resetFailed(self, message):
        QMessageBox.critical(self, "Reset", f"The reset was not recorded: {message}")
