# tests/integration/test_cli.py
"""run_sequences.main() end to end in simulation -- the entry point's own
lifecycle (build, run, wait, tear down, exit code), which the worker and
DeviceSet tests cannot see. The autouse fake clock makes the simulated
bring-up and moves instant; the run log and crash hook are stubbed out."""

import sys
from types import SimpleNamespace

import pytest

import run_sequences

from .test_sample_files import QUICKSTART_PAIRS, SOFTWARE


@pytest.fixture
def cli(monkeypatch):
    monkeypatch.setattr(run_sequences, "start_log_file", lambda directory=None: None)
    monkeypatch.setattr(run_sequences, "stop_log_file", lambda: None)
    monkeypatch.setattr(run_sequences, "setup_uncaught_exception_logging", lambda: None)

    def run(sequences, config):
        """main()'s exit code for the documented pairing: a clean run
        returns, everything else exits."""
        monkeypatch.setattr(sys, "argv", [
            "run_sequences.py",
            "--path", str(SOFTWARE / "sample_sequences" / sequences),
            "--config", str(SOFTWARE / "sample_config" / config),
            "--simulation"])
        try:
            run_sequences.main()
        except SystemExit as exit_info:
            return exit_info.code
        return 0

    return run


@pytest.mark.parametrize("sequences, config", QUICKSTART_PAIRS)
def test_a_clean_simulated_run_exits_zero(cli, sequences, config):
    assert cli(sequences, config) == 0


def test_a_thread_that_fails_to_start_exits_promptly(cli, monkeypatch):
    """A start() that raises lands in main()'s except Exception, which has
    no run to wait on: teardown and exit 1, promptly. Only the CLI's own
    Thread is stubbed -- the simulated drivers keep theirs."""
    class CannotStart:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(run_sequences, "threading", SimpleNamespace(Thread=CannotStart))
    assert cli(*QUICKSTART_PAIRS[0]) == 1
