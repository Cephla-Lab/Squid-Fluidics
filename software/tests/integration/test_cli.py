# tests/integration/test_cli.py
"""run_sequences.main() end to end in simulation -- the entry point's own
lifecycle (build, run, wait, tear down, exit code), which the worker and
DeviceSet tests cannot see. The autouse fake clock makes the simulated
bring-up and moves instant; the run log and crash hook are stubbed out."""

import shutil
import sys
from types import SimpleNamespace

import pytest

import run_sequences

from .test_sample_files import QUICKSTART_PAIRS, SOFTWARE


@pytest.fixture
def cli(monkeypatch, tmp_path):
    """main()'s exit code for the given argv: a clean run returns, everything
    else exits."""
    monkeypatch.setattr(run_sequences, "start_log_file", lambda directory=None: None)
    monkeypatch.setattr(run_sequences, "stop_log_file", lambda: None)
    monkeypatch.setattr(run_sequences, "setup_uncaught_exception_logging", lambda: None)
    monkeypatch.setattr("fluidics.reports.default_report_directory",
                        lambda: tmp_path / "reports")

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["run_sequences.py", *argv])
        try:
            run_sequences.main()
        except SystemExit as exit_info:
            return exit_info.code
        return 0

    return run


@pytest.fixture
def cli_pair(cli):
    """The documented pairing: a sequence file and a config from the sample
    directories, run in simulation."""
    def run(sequences, config):
        return cli("--path", str(SOFTWARE / "sample_sequences" / sequences),
                   "--config", str(SOFTWARE / "sample_config" / config),
                   "--simulation")

    return run


@pytest.mark.parametrize("sequences, config", QUICKSTART_PAIRS)
def test_a_clean_simulated_run_exits_zero(cli_pair, sequences, config):
    assert cli_pair(sequences, config) == 0


def test_a_thread_that_fails_to_start_exits_promptly(cli_pair, thread_cannot_start):
    """A start() that raises lands in main()'s except Exception with the
    session left free -- nothing to wait on: teardown and exit 1, promptly."""
    assert cli_pair(*QUICKSTART_PAIRS[0]) == 1


def test_without_a_config_flag_the_rigs_own_legacy_json_serves(cli, monkeypatch, tmp_path):
    """The GUI and the CLI share one convention (default_config_path): a
    legacy JSON-only rig must not launch under one and traceback in the
    other."""
    shutil.copy(SOFTWARE / "tests" / "fixtures" / "legacy_flow_cell_config.json",
                tmp_path / "config.json")
    monkeypatch.chdir(tmp_path)
    assert cli("--path", str(SOFTWARE / "sample_sequences" / "merfish-experiment.yaml"),
               "--simulation") == 0


def test_a_wrong_application_sequence_file_fails_at_time_zero(cli_pair, monkeypatch):
    """An Open Chamber sequence file against a Flow Cell rig must exit
    before anything moves. The exit code alone cannot pin that -- the run
    would fail mid-experiment too -- so the pin is that the rig is never
    even built."""
    built = []
    monkeypatch.setattr(run_sequences, "FluidicsSystem",
                        SimpleNamespace(build=lambda *a, **k: built.append(True)))
    assert cli_pair("open-chamber-experiment.yaml", "flow_cell_config.yaml") == 1
    assert built == [], "the rig was built for a file the check should have refused"
