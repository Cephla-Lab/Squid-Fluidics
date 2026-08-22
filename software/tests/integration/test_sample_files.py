# tests/integration/test_sample_files.py
"""The shipped sample files, loaded and run instead of trusted.

The README tells new users to base their rig config on sample_config/, and
both CLAUDE.md files give `run_sequences.py --path sample_sequences/... `
as the canonical first command -- yet nothing loaded these files in tests, so
a model change (`extra='forbid'` makes stranding easy) could break the
documented quickstart and nobody would notice until the next fresh setup.

Three layers: every sample config loads, every sample sequence file loads and
fits at least one application, and the two documented quickstart pairings run
to completion through the same simulation stack the CLI drives -- the closest
thing to an end-to-end test of the documented CLI path, minus the argument
parsing and thread plumbing run_sequences.py adds around it.
"""

from pathlib import Path

import pytest

from fluidics.control.config import load_config
from fluidics.devices import build_operations
from fluidics.experiment_worker import ExperimentWorker
from fluidics.sequences import (
    APPLICATION_SEQUENCES, get_included_sequences, load_sequences,
)

SOFTWARE = Path(__file__).resolve().parents[2]
SAMPLE_CONFIGS = sorted((SOFTWARE / "sample_config").glob("*.yaml"))
SAMPLE_SEQUENCES = sorted((SOFTWARE / "sample_sequences").glob("*.yaml"))

# The pairings the docs actually tell people to run. Distinct from the globs
# above: a missing file here must fail (the quickstart names it), while the
# globs validate whatever ships -- including any extra local file, which is a
# feature, not an accident.
QUICKSTART_PAIRS = [
    ("merfish-experiment.yaml", "flow_cell_config.yaml"),
    ("open-chamber-experiment.yaml", "open_chamber_config.yaml"),
]


def test_the_sample_directories_are_not_empty():
    """Guards the globs themselves: a renamed directory would otherwise turn
    every parametrized test below into a silent zero-case pass."""
    assert SAMPLE_CONFIGS
    assert SAMPLE_SEQUENCES


@pytest.mark.parametrize("path", SAMPLE_CONFIGS, ids=lambda p: p.name)
def test_sample_config_loads(path):
    config = load_config(str(path))
    assert config.application in APPLICATION_SEQUENCES


@pytest.mark.parametrize("path", SAMPLE_SEQUENCES, ids=lambda p: p.name)
def test_sample_sequences_load_and_fit_an_application(path):
    sequences = load_sequences(str(path))
    assert sequences, f"{path.name} loaded empty"
    used = {seq["type"] for seq in sequences}
    fits = [app for app, allowed in APPLICATION_SEQUENCES.items()
            if used <= set(allowed)]
    assert fits, (f"{path.name} uses types {sorted(used)} that no single "
                  f"application supports")


class TestQuickstartRunsEndToEnd:
    """`run_sequences.py --path <sequences> --config <config> --simulation`,
    minus the argument parsing: the same build_devices/build_operations/
    ExperimentWorker path the CLI takes, run to completion. The suite's fake
    clock makes the simulated moves and incubations instant.
    """

    @pytest.mark.parametrize("sequence_name,config_name", QUICKSTART_PAIRS,
                             ids=[p[0] for p in QUICKSTART_PAIRS])
    def test_documented_pairing_completes_without_errors(
            self, sequence_name, config_name, built):
        sequences = load_sequences(str(SOFTWARE / "sample_sequences" / sequence_name))
        included = get_included_sequences(sequences)
        config = load_config(str(SOFTWARE / "sample_config" / config_name))

        devices = built(config, simulation=True)
        # build_devices degrades a failed sensor bring-up to none-at-all, so
        # without this a broken flow_sensors block in the sample config would
        # still "complete without errors" -- with nothing watching the draws.
        assert len(devices.flow_sensors) == len(config.flow_sensors or [])

        errors, statuses, finished = [], [], []
        ops = build_operations(config, devices)
        worker = ExperimentWorker(ops, included, config, callbacks={
            "on_error": errors.append,
            "update_progress":
                lambda index, num, status: statuses.append(status),
            "on_finished": lambda: finished.append(True),
        })
        # The CLI runs this on a thread only to keep its console alive;
        # the loop itself is synchronous.
        worker.run()

        assert errors == []
        assert finished == [True]
        expected = sum(s.get("repeat", 1) for s in included)
        assert statuses.count("Completed") == expected
