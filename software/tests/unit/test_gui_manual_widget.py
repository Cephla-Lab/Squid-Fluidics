"""ManualControlWidget over a simulated system: the tracked bottles' remaining
volumes and the "Bottle refilled…" declaration (spec §7, §9), which every
ledger rig has, robot or not; a rig that tracks nothing shows neither."""
from qtpy.QtCore import QEvent
from qtpy.QtWidgets import QApplication


def _destroy(widget):
    """Destroyed, not only closed: its subscriptions come off the system's
    channels on `destroyed`."""
    widget.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_the_manual_tab_shows_remaining_and_declares_a_bottle(qapp, plate_sipper_config, tmp_path, monkeypatch):
    """Codex finding 15: tracked bottles are declared from the manual tab,
    which every ledger rig has, robot or not."""
    from fluidics.devices import build_devices
    from fluidics.qt.manual_control import ManualControlWidget
    from fluidics.system import FluidicsSystem
    devices = build_devices(plate_sipper_config, simulation=True, instant=True)
    for sensor in devices.flow_sensors:
        sensor.close()
    system = FluidicsSystem(plate_sipper_config, devices, report_dir=tmp_path)
    try:
        w = ManualControlWidget(plate_sipper_config, system)
        system.devices.reservoirs._dispatched["port_25"] = 1000
        w.refreshRemaining()
        assert w.remainingLabel("port_25") == "479000"
        monkeypatch.setattr("fluidics.qt.manual_control._pick_bottle", lambda parent, keys: "port_25")
        w.bottleRefilled()
        assert system.wait()      # untimed: a timed wait cannot block under the fake clock
        assert system.devices.reservoirs.remaining("port_25") == 480000
        _destroy(w)
    finally:
        assert system.close() == []


def test_the_routed_bottle_s_remaining_shows_beside_the_port(qapp, plate_sipper_config, tmp_path):
    """Beside the source-port combo: the routed port's bottle when it is
    tracked, blank otherwise; repainted when a move ends."""
    from fluidics.devices import build_devices
    from fluidics.qt.manual_control import ManualControlWidget
    from fluidics.system import FluidicsSystem
    devices = build_devices(plate_sipper_config, simulation=True, instant=True)
    for sensor in devices.flow_sensors:
        sensor.close()
    system = FluidicsSystem(plate_sipper_config, devices, report_dir=tmp_path)
    try:
        w = ManualControlWidget(plate_sipper_config, system)
        w.valveCombo.setCurrentIndex(w.valveCombo.findData(25))     # opens port 25: a manual job
        assert system.wait()
        QApplication.processEvents()
        assert "480000" in w.routedRemainingLabel.text()
        w.valveCombo.setCurrentIndex(w.valveCombo.findData(26))
        assert system.wait()
        QApplication.processEvents()
        assert w.routedRemainingLabel.text() == ""
        _destroy(w)
    finally:
        assert system.close() == []


def test_a_rig_with_bottles_and_no_robot_declares_them_here(qapp, flow_cell_config, tmp_path, monkeypatch):
    """The manual tab is where a bottle is declared on every ledger rig --
    one without a robot (and so without a Robot tab) included. The click
    asks which bottle; Cancel refills nothing."""
    from fluidics.devices import build_devices
    from fluidics.qt.manual_control import ManualControlWidget
    from fluidics.system import FluidicsSystem
    flow_cell_config.reagent_selection.selector_valves.loaded_volume_ul = {"port_2": 10000}
    devices = build_devices(flow_cell_config, simulation=True, instant=True)
    for sensor in devices.flow_sensors:
        sensor.close()
    system = FluidicsSystem(flow_cell_config, devices, report_dir=tmp_path)
    try:
        assert system.devices.robot is None and system.devices.reservoirs is not None
        w = ManualControlWidget(flow_cell_config, system)
        system.devices.reservoirs._dispatched["port_2"] = 400
        w.refreshRemaining()
        assert w.remainingLabel("port_2") == "9600"
        asked = []
        monkeypatch.setattr("fluidics.qt.manual_control._pick_bottle",
                            lambda parent, keys: asked.append(dict(keys)))
        w.bottleRefilledButton.click()
        assert system.wait()
        assert asked == [{"port_2": "Port 2: y"}]
        assert system.devices.reservoirs.remaining("port_2") == 9600, "cancelled: nothing refilled"
        _destroy(w)
    finally:
        assert system.close() == []


def test_a_rig_that_tracks_nothing_shows_no_bottles(qapp, flow_cell_config, tmp_path):
    """D8: no declared volumes, no ledger, the manual tab as it was."""
    from fluidics.devices import build_devices
    from fluidics.qt.manual_control import ManualControlWidget
    from fluidics.system import FluidicsSystem
    devices = build_devices(flow_cell_config, simulation=True, instant=True)
    for sensor in devices.flow_sensors:
        sensor.close()
    system = FluidicsSystem(flow_cell_config, devices, report_dir=tmp_path)
    try:
        assert system.devices.reservoirs is None
        w = ManualControlWidget(flow_cell_config, system)
        assert not hasattr(w, "bottleRefilledButton")
        w.refreshRemaining()                       # a no-op, not an error
        _destroy(w)
    finally:
        assert system.close() == []
