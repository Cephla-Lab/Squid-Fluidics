# tests/integration/test_cli.py
"""run_sequences.main() end to end in simulation -- the entry point's own
lifecycle (build, run, wait, tear down, exit code), which the worker and
DeviceSet tests cannot see. The autouse fake clock makes the simulated
bring-up and moves instant; the run log and crash hook are stubbed out."""

import sys
import threading
from pathlib import Path

import pytest

import run_sequences

SOFTWARE = Path(run_sequences.__file__).parent
FLOW_CELL = (SOFTWARE / "sample_sequences" / "merfish-experiment.yaml",
             SOFTWARE / "sample_config" / "flow_cell_config.yaml")


@pytest.fixture
def cli(monkeypatch):
    monkeypatch.setattr(run_sequences, "start_log_file", lambda directory=None: None)
    monkeypatch.setattr(run_sequences, "stop_log_file", lambda: None)
    monkeypatch.setattr(run_sequences, "setup_uncaught_exception_logging", lambda: None)

    def run(sequences, config):
        """main()'s exit code: a clean run returns, everything else exits."""
        monkeypatch.setattr(sys, "argv", ["run_sequences.py", "--path", str(sequences),
                                          "--config", str(config), "--simulation"])
        try:
            run_sequences.main()
        except SystemExit as exit_info:
            return exit_info.code
        return 0

    return run


def test_a_clean_simulated_run_exits_zero(cli):
    assert cli(*FLOW_CELL) == 0


def test_a_thread_that_fails_to_start_does_not_hang_the_exit(cli, monkeypatch):
    """`thread` used to be assigned before start(); a start() that raised
    left the error handler waiting on a finished event no thread would set."""
    def cannot_start(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", cannot_start)
    assert cli(*FLOW_CELL) == 1
