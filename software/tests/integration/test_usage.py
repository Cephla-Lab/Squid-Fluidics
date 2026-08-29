# tests/integration/test_usage.py
"""The usage ledger against the real simulated rig: draws the operations
actually queue land on the right port, from a run's beginning."""

import pytest

from .conftest import FLOW_CELL_STEP


class TestUsageThroughARun:
    def test_a_run_charges_its_port_and_only_its_port(self, system):
        system.run([FLOW_CELL_STEP])
        assert system.wait()
        totals = system.usage.snapshot()
        assert set(totals) == {1}, f"drawn from unexpected ports: {totals}"
        # The step's own 500 uL plus whatever tubing turnover the
        # operations add: bounded, so the tariff is not copied back out.
        assert 500 <= totals[1] <= 3500

    def test_a_fill_tubing_draw_is_charged_to_its_own_port(self, system):
        step = dict(FLOW_CELL_STEP, fill_tubing_with=24)
        system.run([step])
        assert system.wait()
        totals = system.usage.snapshot()
        assert set(totals) == {1, 24}
        assert totals[24] > 0

    def test_the_next_run_starts_clean(self, system):
        system.run([FLOW_CELL_STEP])
        assert system.wait()
        first = system.usage.snapshot()[1]
        system.run([FLOW_CELL_STEP])
        assert system.wait()
        assert system.usage.snapshot()[1] == pytest.approx(first), \
            "the previous experiment's draws leaked into this one"
