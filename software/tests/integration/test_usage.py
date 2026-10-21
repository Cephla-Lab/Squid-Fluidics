# tests/integration/test_usage.py
"""The ledger's per-run view against the real simulated rig: draws the
operations actually queue land on the right port, from a run's beginning
-- on a rig that declares no volume, where the ledger is the tally alone."""

import pytest

from .conftest import FLOW_CELL_STEP


def used(system):
    """{port: uL drawn since the run started}, from the ledger's rows."""
    return {port: used_ul for port, _, used_ul in system.devices.reservoirs.used_rows()}


class TestUsageThroughARun:
    def test_a_run_charges_its_port_and_only_its_port(self, system):
        system.run([FLOW_CELL_STEP])
        assert system.wait()
        totals = used(system)
        assert set(totals) == {1}, f"drawn from unexpected ports: {totals}"
        # The step's own 500 uL plus whatever tubing turnover the
        # operations add: bounded, so the tariff is not copied back out.
        assert 500 <= totals[1] <= 3500

    def test_a_fill_tubing_draw_is_charged_to_its_own_port(self, system):
        step = dict(FLOW_CELL_STEP, fill_tubing_with=24)
        system.run([step])
        assert system.wait()
        totals = used(system)
        assert set(totals) == {1, 24}
        assert totals[24] > 0

    def test_the_next_run_starts_clean(self, system):
        system.run([FLOW_CELL_STEP])
        assert system.wait()
        first = used(system)[1]
        system.run([FLOW_CELL_STEP])
        assert system.wait()
        assert used(system)[1] == pytest.approx(first), \
            "the previous experiment's draws leaked into this one"
