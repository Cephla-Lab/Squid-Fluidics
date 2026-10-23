"""The Reservoirs window: every reservoir volume tracking knows, what each
has given since its last reset and, where the config declares a volume,
what is left of it and what was loaded -- with the resets and the loaded
volumes' settings. A tool window the Run Experiments tab opens from
Reservoirs…, on every rig, declared volumes or not. It reads the volume
tracker's balances() and nothing else; each reset is the manual refill
verb and each setting the manual set_loaded_volume verb, which writes the
config file's volume fields and nothing else of it, both run as a job on
the session under the opener's `job_source`, so they wait for an idle rig
like any move, a run's gate reads what they wrote, and the main window
keeps the opener's tab live under them."""

from qtpy.QtCore import QItemSelection, QItemSelectionModel, Qt, QTimer
from qtpy.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                            QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
                            QWidget)

from fluidics.reservoirs import describe_keys
from fluidics.robot.labware import plate_of
from fluidics.vocabulary import sipper_vocabulary

from .support import PostsToQtThread, _ask_yes_no, connect_click, subscribe_until_detached

COLUMNS = ("Reservoir", "Reagent", "Accumulated (µL)", "Current (µL)", "Loaded (µL)")
LOADED_COLUMN = COLUMNS.index("Loaded (µL)")


def _volume(ul):
    """A volume cell: blank where the reservoir declares none."""
    return "" if ul is None else f"{ul:.0f}"


class ReservoirsWindow(PostsToQtThread, QWidget):
    """One table, the volume tracker's balances, and under it two rows of
    buttons. The resets: Reset selected (the highlighted rows), Reset
    <plate> per plate (a plate reloads as a plate) and Reset all. The
    loaded volumes: Set selected… and Set <plate>…, each asking for the
    volume in one dialog, and a double click on a row's Loaded cell, which
    is Set selected… for that row. Never modal, and owned by the tab that
    opens it, so it goes with that tab.

    While it is open it repaints itself each second -- a run's draws, a
    manual draw, a port renamed on the Manual tab all show within one --
    and on every session state change, which it hears itself, so a job's
    start and end (its own among them) show at once whoever opened it;
    closed, it paints nothing, and opening it paints it. The buttons are
    live only while the rig is idle, read from the session at each
    paint."""

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
        self.table.itemSelectionChanged.connect(self._paintButtons)
        self.table.cellDoubleClicked.connect(self._onCellDoubleClicked)
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
        plates = sorted(sipper.reservoirs) if sipper is not None else ()
        for name in plates:
            button = QPushButton(f"Reset {name}")
            connect_click(button, lambda name=name: self.resetPlate(name))
            resets.addWidget(button)
            self.resetPlateButtons[name] = button
        self.resetAllButton = QPushButton("Reset all")
        connect_click(self.resetAllButton, self.resetAll)
        resets.addWidget(self.resetAllButton)
        layout.addLayout(resets)

        loaded = QHBoxLayout()
        loaded.addWidget(QLabel("Loaded:"))
        self.setSelectedButton = QPushButton("Set selected…")
        connect_click(self.setSelectedButton, self.setSelected)
        loaded.addWidget(self.setSelectedButton)
        loaded.addStretch()
        self.setPlateButtons = {}
        for name in plates:
            button = QPushButton(f"Set {name}…")
            connect_click(button, lambda name=name: self.setPlate(name))
            loaded.addWidget(button)
            self.setPlateButtons[name] = button
        layout.addLayout(loaded)
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
        self._paintButtons()

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

    def _paintButtons(self):
        """A reset or a setting is a manual job: it waits for an idle rig
        like any other."""
        idle = not self.session.busy
        selected = bool(self.selectedKeys())
        self.resetSelectedButton.setEnabled(idle and selected)
        self.setSelectedButton.setEnabled(idle and selected)
        for button in (*self.resetPlateButtons.values(), *self.setPlateButtons.values()):
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
        self._start(lambda: manual.refill(*keys), "_resetFailed")

    def _resetFailed(self, message):
        QMessageBox.critical(self, "Reset", f"The reset was not recorded: {message}")

    # --- the loaded volumes ---

    def setSelected(self):
        keys = self.selectedKeys()
        if keys:
            self._setLoaded(keys, describe_keys(keys, self.tracker.tracked))

    def setPlate(self, name):
        self._setLoaded([name], f"every well of {name}")

    def _onCellDoubleClicked(self, row, column):
        """A Loaded cell is Set selected… for its row, selected first if
        it was not, so the dialog names what it sets."""
        if column != LOADED_COLUMN or row >= len(self._keys) or self.session.busy:
            return
        key = self._keys[row]
        if key not in self.selectedKeys():
            self._select({key})
        self.setSelected()

    def _setLoaded(self, keys, what):
        """Ask for what `keys` hold when loaded and set it as one manual job
        on the session. The verb's refusal is said before the dialog where
        the keys alone decide it, and again, once the value is in, before
        the job starts, so a refusal is never a failure after the fact."""
        manual = self.system.manual
        refusal = manual.loaded_volume_refusal(keys)
        if refusal:
            QMessageBox.warning(self, "Loaded volume", refusal)
            return
        ul, ok = QInputDialog.getInt(self, "Loaded volume", f"Loaded volume of {what} (µL):",
                                     self._firstLoaded(keys) or 1, 1, 2 ** 31 - 1)
        if not ok:
            return
        refusal = manual.loaded_volume_refusal(keys, ul)
        if refusal:
            QMessageBox.warning(self, "Loaded volume", refusal)
            return
        self._start(lambda: manual.set_loaded_volume(keys, ul), "_setFailed")

    def _firstLoaded(self, keys):
        """The loaded volume of the first reservoir `keys` names, in table
        order (a plate's name names every well of it), or None where it
        declares none."""
        tracked = set(self.tracker.tracked)
        for key in self._keys:
            if key in keys or plate_of(key) in keys:
                return self.tracker.loaded(key) if key in tracked else None
        return None

    def _setFailed(self, message):
        QMessageBox.critical(self, "Loaded volume", f"The loaded volume was not set: {message}")

    def _start(self, verb, failed):
        """Run `verb` as one manual job under the opener's label; what it
        raises comes back as the dialog the method named `failed` shows."""
        try:
            self.system.run_manual(verb, callbacks={
                "on_error": lambda message: self._post_event(failed, message),
            }, source=self._jobSource)
        except RuntimeError as e:
            QMessageBox.warning(self, "Rig busy", str(e))
            return
        # The session took the job before run_manual returned, and its
        # posted state change cannot land before this does: paint now.
        self.refresh()
