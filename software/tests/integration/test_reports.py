# tests/integration/test_reports.py
"""The per-run report through the whole rig: a real run on the simulated
devices ends, and its record lands where the fixture pointed (tmp_path --
the same directory object the test reads). What goes in the record is
pinned per field in tests/unit/test_reports.py; this is the wiring."""

import json

from fluidics.events import RunEnded

from ..conftest import hears, wait_until
from .conftest import FLOW_CELL_STEP


class TestRunReports:
    def test_a_runs_record_lands_on_disk(self, system, real_clock, tmp_path):
        ended = hears(system.session.events, RunEnded,
                      key=lambda event: event.run_id)
        system.run([FLOW_CELL_STEP])
        assert system.wait(5)
        assert wait_until(lambda: ended)
        assert system.reports.wait(5)
        report = json.loads((tmp_path / f"{ended[0]}.json").read_text())
        assert report["run_id"] == ended[0]
        assert report["outcome"] == "finished"
        assert report["plan"], "the plan belongs in the record"
        assert report["reagent_used_ul"], "the ledger's totals belong in it"


def test_the_report_carries_the_reservoirs(plate_sipper_config, instant_devices, tmp_path, real_clock):
    from fluidics.system import FluidicsSystem
    system = FluidicsSystem(plate_sipper_config, instant_devices(plate_sipper_config), report_dir=tmp_path)
    try:
        system.run([{"type": "flow_reagent", "fluidic_port": 1, "well": "A1", "flow_rate": 2000, "volume": 100}])
        assert system.wait(5) and system.reports.wait(5)
        [path] = list(tmp_path.glob("run-*.json"))
        report = json.loads(path.read_text())
        assert {"key": "probes/A1", "dispatched_ul": 100, "remaining_ul": 5} in report["reservoirs"]
    finally:
        assert system.close() == []
