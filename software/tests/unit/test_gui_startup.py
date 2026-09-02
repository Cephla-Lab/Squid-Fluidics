# tests/unit/test_gui_startup.py
"""gui.main(): the standalone application's startup path.

Everything under `if __name__ == '__main__'` used to be unreachable from
the suite, so a name dropped from an import raised NameError only when
someone ran the app -- after the change had merged. This drives the same
path with the window stubbed: argparse, the QApplication, the
organisation identity QSettings stores under, and the config choice.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

import gui

SOFTWARE_ROOT = Path(__file__).resolve().parents[2]
CONFIG = SOFTWARE_ROOT / "sample_config" / "flow_cell_config.yaml"


@pytest.fixture
def started(qapp, monkeypatch):
    """Run main() with the window stubbed; returns what it was handed."""
    built = []
    monkeypatch.setattr(gui, "setup_uncaught_exception_logging", lambda: None)
    monkeypatch.setattr(gui, "start_log_file", lambda directory=None: None)
    monkeypatch.setattr(gui.QApplication, "exec_", lambda self: 0)
    monkeypatch.setattr(gui, "FluidicsControlGUI", lambda config, simulation:
                        SimpleNamespace(show=lambda: built.append(
                            (config.application, simulation))))
    return built


def test_the_application_comes_up_in_simulation(started):
    assert gui.main(["--simulation", "--config", str(CONFIG)]) == 0
    assert started == [("Flow Cell", True)], "the window was never shown"


def test_the_identity_qsettings_stores_under_is_set(started):
    gui.main(["--config", str(CONFIG)])
    assert gui.QCoreApplication.organizationName() == "Cephla"
    assert gui.QCoreApplication.applicationName() == "FluidicsControl"


def test_no_config_no_window(started, monkeypatch):
    """pick_config returns None when the operator cancels the dialog."""
    monkeypatch.setattr(gui, "pick_config", lambda path: None)
    assert gui.main([]) == 1
    assert started == []
