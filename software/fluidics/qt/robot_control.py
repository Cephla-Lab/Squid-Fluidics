"""The Robot tab: jog, home, teach, go to well, reset a fault, and the
reservoir table. Every verb is a manual job through the session (one job on
the rig); the controls freeze while ANY job runs (the session's state, not
only this tab's); position is painted from the stage's publish; STOP is the
session's abort -- the ManualControlWidget's contract, applied to the
sipper (spec §9). Teaching goes through the manual verbs, which change the
live robot and the config together, or neither; Save writes the config.
"""

import logging

from qtpy.QtWidgets import (QCheckBox, QComboBox, QGridLayout, QGroupBox, QHBoxLayout,
                            QHeaderView, QLabel, QMessageBox, QPushButton, QTableWidget,
                            QTableWidgetItem, QVBoxLayout, QWidget)

from fluidics.control.config import gantry, save_config
from fluidics.events import RunStarted, SequenceCompleted
from fluidics.vocabulary import sipper_vocabulary

from qtpy.QtCore import Qt

from .deck_map import DeckMapWidget
from .support import PostsToQtThread, _ask_yes_no, subscribe_until_detached

_logger = logging.getLogger(__name__)

JOG_STEPS_MM = (0.1, 1.0, 10.0)

# The session's `source` for a manual job this tab started: STOP is live
# for it alone, and the main window greys every other tab while it runs.
ROBOT_JOB_SOURCE = "robot"


class WellCombo(QComboBox):
    """An editable well picker whose value is what the operator sees: the
    typed text, or the picked item's well. QComboBox.currentData() keeps the
    last picked item while the text says something else, so it is never
    read here."""

    def __init__(self):
        super().__init__()
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)

    def fill(self, wells):
        """`wells`: [(well, label)] as SipperVocabulary.well_names gives them."""
        self.clear()
        for well, label in wells:
            self.addItem(label, well)

    def value(self):
        text = self.currentText().strip()
        index = self.findText(text)
        if index >= 0:
            return self.itemData(index)
        return text.split(":")[0].strip()


def connect_click(button, slot):
    """`clicked` carries a `checked` bool; a slot with defaulted arguments
    would take it as its first one. The wrapper drops it."""
    button.clicked.connect(lambda _checked=False, slot=slot: slot())


class _ToolWindow(QWidget):
    """A tool window that floats beside the main window, holding a layout
    built by the tab; closing hides it, and its controls live on."""

    def __init__(self, title, layout, parent=None):
        super().__init__(parent, Qt.Tool)
        self.setWindowTitle(title)
        self.setLayout(layout)


class RobotControlWidget(PostsToQtThread, QWidget):
    def __init__(self, config, system):
        super().__init__()
        self.config = config
        self.system = system
        self.session = system.session
        self.manual = system.manual
        self._vocabulary = sipper_vocabulary(config)
        self._controls = []
        # The embedder's switch (setControlsEnabled), kept apart from the
        # session's state so a job's end never lifts it.
        self._externallyEnabled = True
        self._plan = ()
        detach = subscribe_until_detached(
            (system.devices.robot.moves, self._onMove),
            (system.session.state, self._onSessionState),
            (system.session.events, self._onRunEvent))
        self.destroyed.connect(detach)
        self.initUI()
        self._paintPosition(system.devices.robot.position())
        self.refreshReservoirs()
        # Built while a job runs (an embedder's tab), it starts frozen.
        self._paintControls()

    # --- layout ---

    def initUI(self):
        layout = QVBoxLayout()

        status = QGroupBox("Stage")
        row = QHBoxLayout()
        self.positionLabel = QLabel("")
        row.addWidget(self.positionLabel)
        self.primedLabel = QLabel("primed this session: no")
        row.addWidget(self.primedLabel)
        self.homeButton = self._button(row, "Home", lambda: self._run(self.manual.home_robot))
        self.retractButton = self._button(row, "Retract", lambda: self._run(self.manual.retract))
        self.resetFaultButton = self._button(row, "Reset fault", lambda: self._run(self.manual.reset_robot_faults))
        self.stopButton = QPushButton("STOP")
        connect_click(self.stopButton, self.stopMove)
        self.stopButton.setEnabled(False)
        row.addWidget(self.stopButton)
        # Teaching (with the jog it needs) lives in its own window, opened
        # from here, so the tab is the run-time view; the button stays live
        # under a job, since opening the window is not a move.
        self.teachButton = QPushButton("Teach…")
        connect_click(self.teachButton, self.showTeach)
        row.addWidget(self.teachButton)
        # Checked: Go to well (button or map) retracts and travels, never
        # dips -- taught positions checked over empty plates; no witness,
        # so no draw, and a run is refused while it is on. Per session.
        self.lowerTipBox = QCheckBox("Do not lower tip")
        self.lowerTipBox.setToolTip("Go to well travels over the well and stops retracted: check "
                                    "taught positions over empty plates. No draw can pass and a run "
                                    "is refused while this is on.")
        self.lowerTipBox.toggled.connect(lambda checked: self.manual.set_lower_tip(not checked))
        row.addWidget(self.lowerTipBox)
        self._controls.append(self.lowerTipBox)
        column = QVBoxLayout()
        column.addLayout(row)
        # The deck map: the plates where they are taught, the tip where the
        # stage last said it was. A click on a well is Go to well for that
        # well, and nothing between wells; it freezes with the controls.
        robot = self.system.devices.robot
        g = gantry(self.config)
        self.deckMap = DeckMapWidget(robot.strokes())
        self.deckMap.setPlacements(robot.placements)
        self.deckMap.setNamedWells({
            "priming": self._vocabulary.resolve(g.priming_well),
            "clean-up": self._vocabulary.resolve(g.clean_up_well)})
        self.deckMap.wellClicked.connect(self._goToClickedWell)
        column.addWidget(self.deckMap, 1)
        self._controls.append(self.deckMap)
        status.setLayout(column)
        layout.addWidget(status)

        # --- the teach window: jog, then the teach row ---
        teaching = QVBoxLayout()
        jog = QGroupBox("Jog")
        grid = QGridLayout()
        self.jogButtons = {}
        for r, axis in enumerate("xyz"):
            grid.addWidget(QLabel(axis.upper()), r, 0)
            for c, step in enumerate(JOG_STEPS_MM):
                # Outward from the middle: -10 -1 -0.1 | +0.1 +1 +10.
                for sign, column in ((-1, 3 - c), (1, 4 + c)):
                    mm = sign * step
                    button = self._button(grid, f"{mm:+g}",
                                          lambda a=axis, m=mm: self._run(lambda: self.manual.jog(a, m)),
                                          r, column)
                    self.jogButtons[(axis, mm)] = button
        jog.setLayout(grid)
        teaching.addWidget(jog)

        wells = QGroupBox("Wells")
        row = QHBoxLayout()
        self.reservoirCombo = QComboBox()
        for name in sorted(self._vocabulary.reservoirs):
            self.reservoirCombo.addItem(name, name)
        self.reservoirCombo.currentIndexChanged.connect(self._fillWells)
        row.addWidget(self.reservoirCombo)
        self._controls.append(self.reservoirCombo)
        self.wellCombo = WellCombo()
        row.addWidget(self.wellCombo)
        self._controls.append(self.wellCombo)
        self._fillWells()
        self.goToWellButton = self._button(row, "Go to well", self.goToWell)
        wells.setLayout(row)
        layout.addWidget(wells)

        teach = QGroupBox("Teach (then Save)")
        row = QHBoxLayout()
        row.addWidget(QLabel("Plate"))
        self.teachPlateCombo = QComboBox()
        for name in sorted(self._vocabulary.reservoirs):
            self.teachPlateCombo.addItem(name, name)
        row.addWidget(self.teachPlateCombo)
        self._controls.append(self.teachPlateCombo)
        self.setA1Button = self._button(row, "Set A1 here", self.setA1Here)
        self.setDipButton = self._button(row, "Set dip depth here", self.setDipHere)
        self.setTravelButton = self._button(row, "Set travel height here", self.setTravelHere)
        self.saveButton = self._button(row, "Save", self.saveTeaching)
        teach.setLayout(row)
        teaching.addWidget(teach)
        self.teachWindow = _ToolWindow("Teach", teaching, parent=self)

        ledger = QGroupBox("Reservoirs")
        column = QVBoxLayout()
        self.reservoirTable = QTableWidget(0, 3)
        self.reservoirTable.setHorizontalHeaderLabels(["Reservoir", "Dispatched (µL)", "Remaining (µL)"])
        # The numeric columns size to their contents (the header's label
        # counts), the name takes the rest: at the default width the labels
        # read "ispatched (µ".
        header = self.reservoirTable.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.reservoirTable.verticalHeader().setVisible(False)
        self.reservoirTable.setEditTriggers(QTableWidget.NoEditTriggers)
        column.addWidget(self.reservoirTable)
        row = QHBoxLayout()
        self.undeclaredLabel = QLabel("")
        self.undeclaredLabel.setWordWrap(True)
        row.addWidget(self.undeclaredLabel, 1)
        self.plateReloadedButton = self._button(row, "Plate reloaded…", self.plateReloaded)
        column.addLayout(row)
        ledger.setLayout(column)
        layout.addWidget(ledger)

        self.setLayout(layout)

    def _button(self, container, text, slot, *grid_pos):
        button = QPushButton(text)
        connect_click(button, slot)
        if grid_pos:
            container.addWidget(button, *grid_pos)
        else:
            container.addWidget(button)
        self._controls.append(button)
        return button

    def _fillWells(self):
        self.wellCombo.fill(self._vocabulary.well_names(self.reservoirCombo.currentData()))

    # --- verbs: every one a manual job ---

    def goToWell(self):
        """The well on the reservoir picked beside it -- or, typed as
        reservoir/well, as typed."""
        reservoir = self.reservoirCombo.currentData()
        well = self.wellCombo.value()
        reference = well if "/" in well else f"{reservoir}/{well}"
        self._run(lambda: self.manual.go_to_well(reference))

    def showTeach(self):
        """Open the teach window, or bring it back to the front."""
        self.teachWindow.show()
        self.teachWindow.raise_()
        self.teachWindow.activateWindow()

    def _goToClickedWell(self, reservoir, well):
        """A click on the map: the pickers show the well, and the move is
        the same manual job the button starts."""
        self.reservoirCombo.setCurrentIndex(self.reservoirCombo.findData(reservoir))
        self.wellCombo.setCurrentText(well)
        self._run(lambda: self.manual.go_to_well(f"{reservoir}/{well}"))

    def stopMove(self):
        """The session's abort, for this tab's own move: a job from another
        tab is stopped from that tab."""
        if self._ownsJob():
            self.session.abort()

    def _ownsJob(self):
        """Whether the job in flight is a move this tab started: read from
        the session, which sets and clears its source with its kind."""
        return self.session.kind == "manual" and self.session.source == ROBOT_JOB_SOURCE

    def _run(self, verb):
        """Hand one verb to the session, off the GUI thread, as the manual
        tab's _run does; a press while the rig is busy is refused, not
        queued."""
        try:
            self.system.run_manual(verb, callbacks={
                "on_stopped": lambda: self._post_event("operationStopped"),
                "on_error": lambda message: self._post_event("handleError", message),
                "on_finished": lambda: self._post_event("operationFinished"),
            }, source=ROBOT_JOB_SOURCE)
        except RuntimeError as e:
            _logger.info("%s; the robot move was not started.", e)
            return
        # Posted reports cannot be handled before this returns, so paint
        # now: the session took the job before run_manual returned.
        self._paintControls()

    def operationStopped(self):
        pass

    def handleError(self, message):
        QMessageBox.critical(self, "Robot", message)

    def operationFinished(self):
        self._renderSession()

    # --- the session and the embedder: either freezes this tab ---

    def _onSessionState(self, kind):
        # On the session's thread; the paint crosses to Qt, and reads the
        # session when it lands rather than trusting `kind` (RunSession.state).
        self._post_event("_renderSession")

    def _renderSession(self):
        self._paintControls()
        if not self.session.busy:
            # Whatever ran -- this tab's move, a manual draw, a run -- may
            # have drawn from a well or declared a refill, taught a plate,
            # or left the tip in a well (the witness is final once the job
            # is over; a move's own publish can land before it is set).
            self.refreshReservoirs()
            robot = self.system.devices.robot
            self.deckMap.setPlacements(robot.placements)
            self._paintPosition(robot.position())

    def setControlsEnabled(self, enabled):
        """The embedder's switch (spec §9): a protocol that runs fluidics
        between imaging freezes the tab for all of it. Only this lifts it;
        the controls come back once it allows them and the rig is idle."""
        self._externallyEnabled = enabled
        self._paintControls()

    def _paintControls(self):
        """The controls are live while the embedder allows them and no job
        runs. STOP follows the session alone, never the embedder: live
        while this tab's own job runs."""
        busy = self.session.busy
        enabled = self._externallyEnabled and not busy
        for control in self._controls:
            control.setEnabled(enabled)
        self.stopButton.setEnabled(busy and self._ownsJob())

    def _onRunEvent(self, event):
        # Completed, not started: a priming the operator aborted mid-way
        # primed nothing.
        if isinstance(event, RunStarted):
            self._plan = event.plan
        elif isinstance(event, SequenceCompleted) and self._plan:
            if self._plan[event.position].sequence.get("type") == "priming":
                self._post_event("_markPrimed")

    def _markPrimed(self):
        self.primedLabel.setText("primed this session: yes")

    # --- position ---

    def _onMove(self, position):
        self._post_event("_paintPosition", position)

    def _paintPosition(self, position):
        robot = self.system.devices.robot
        self.deckMap.setPosition(position, robot.is_retracted)
        self.deckMap.setCurrentWell(robot.current_well)
        if position is None:
            self.positionLabel.setText("x --  y --  z --  (position unknown: home or retract)")
            return
        x, y, z = position
        self.positionLabel.setText(f"x {x:.2f}  y {y:.2f}  z {z:.2f}")

    # --- teach: through the manual verbs; then Save ---

    def _knownPosition(self):
        """Where the stage is, from its last publish (no I/O), or None with
        the operator told to home."""
        position = self.system.devices.robot.position()
        if position is None:
            QMessageBox.warning(self, "Robot", "The stage position is unknown: home first.")
        return position

    def setA1Here(self):
        position = self._knownPosition()
        if position is None:
            return
        name, (x, y, _) = self.teachPlateCombo.currentData(), position
        self._run(lambda: self.manual.teach_a1(name, (x, y)))

    def setDipHere(self):
        position = self._knownPosition()
        if position is None:
            return
        name, z = self.teachPlateCombo.currentData(), position[2]
        self._run(lambda: self.manual.teach_dip(name, z))

    def setTravelHere(self):
        position = self._knownPosition()
        if position is None:
            return
        z = position[2]
        self._run(lambda: self.manual.teach_travel(z))

    def saveTeaching(self):
        if self.session.busy:
            QMessageBox.warning(self, "Rig busy", "Teaching can be saved when the rig is idle.")
            return
        try:
            save_config(self.config)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"The teaching applies for this session, but saving failed: {e}")

    # --- the ledger ---

    def refreshReservoirs(self):
        ledger = self.system.devices.reservoirs
        rows = ledger.rows() if ledger is not None else []
        self.reservoirTable.setRowCount(len(rows))
        for r, (key, dispatched, remaining) in enumerate(rows):
            for c, text in enumerate((key, f"{dispatched:.0f}", f"{remaining:.0f}")):
                self.reservoirTable.setItem(r, c, QTableWidgetItem(text))
        undeclared = ledger.undeclared if ledger is not None else []
        self.undeclaredLabel.setText(
            "" if not undeclared else
            f"Ledger undeclared for {len(undeclared)} reservoirs ({', '.join(undeclared[:4])}"
            f"{', …' if len(undeclared) > 4 else ''}): declare refills before running")

    def plateReloaded(self):
        name = self.reservoirCombo.currentData()
        if not _ask_yes_no(self, "Plate reloaded?", f"Mark every well of {name} as full?"):
            return
        self._run(lambda: self.manual.refill(name))
