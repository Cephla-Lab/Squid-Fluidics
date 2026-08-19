# tests/unit/test_gui_helpers.py
"""Tests for pure helpers in gui.py.

Importing gui pulls in PyQt5, which is fine without a display as long as no
QApplication is constructed. The widgets themselves have no test harness; only
module-level pure functions are covered here.
"""

import os

import pytest

import gui


class TestSafeFilenamePart:
    """Recording filenames interpolate FlowSensorConfig.name, which is
    free-form config text. It must not be able to escape the working directory
    or break open().
    """

    def test_ordinary_name_passes_through(self):
        assert gui._safe_filename_part("syringe_draw") == "syringe_draw"

    def test_keeps_safe_punctuation(self):
        assert gui._safe_filename_part("flow-1.2_a") == "flow-1.2_a"

    @pytest.mark.parametrize("name", [
        "../../etc/passwd",
        "a/b",
        "a\\b",
        "/absolute",
    ])
    def test_strips_path_separators(self, name):
        cleaned = gui._safe_filename_part(name)
        assert "/" not in cleaned
        assert "\\" not in cleaned
        assert os.sep not in cleaned
        assert os.path.basename(cleaned) == cleaned

    @pytest.mark.parametrize("name", ["", "...", "///", "___"])
    def test_degenerate_names_fall_back(self, name):
        # Must still be a usable filename component, never empty.
        assert gui._safe_filename_part(name) == "sensor"

    def test_spaces_become_underscores(self):
        assert gui._safe_filename_part("waste line") == "waste_line"


class TestDrawProtectionUnavailable:
    """Only MERFISHOperations arms the sensors, so on any other application a
    configured warn/stop mode is inert. Silence there would leave the operator
    believing a draw is protected when nothing is watching it.

    Called unbound against a stub, since the method touches only self.flowSensors
    and a message box -- constructing a QMainWindow needs a QApplication.
    """

    class Stub:
        def __init__(self, sensors):
            self.flowSensors = sensors

    class Sensor:
        def __init__(self, name, monitor):
            self.name = name
            self.monitor = monitor

    @pytest.fixture
    def shown(self, monkeypatch):
        messages = []
        monkeypatch.setattr(gui.QMessageBox, "warning",
                            lambda parent, title, text: messages.append(text))
        return messages

    def _run(self, sensors, draw_protection):
        stub = self.Stub(sensors)
        gui.FluidicsControlGUI._warn_if_draw_protection_unavailable(
            stub, draw_protection)
        return stub

    def test_a_configured_mode_is_reported(self, shown):
        self._run([self.Sensor("syringe_draw", "stop")], draw_protection=False)
        assert len(shown) == 1
        assert "syringe_draw" in shown[0]

    def test_the_mode_is_forced_off_so_the_gui_cannot_show_it_as_active(self, shown):
        stub = self._run([self.Sensor("s", "stop")], draw_protection=False)
        assert stub.flowSensors[0].monitor == "off"

    def test_sensors_already_off_are_not_reported(self, shown):
        self._run([self.Sensor("s", "off")], draw_protection=False)
        assert shown == []

    def test_nothing_is_reported_when_protection_is_available(self, shown):
        stub = self._run([self.Sensor("s", "stop")], draw_protection=True)
        assert shown == []
        assert stub.flowSensors[0].monitor == "stop"
