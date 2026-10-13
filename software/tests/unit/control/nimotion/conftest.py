"""The vendored driver's tests run as upstream runs them: on the real clock.

They model drives in wall time (the sim:// bus advances with time.monotonic,
and several assert how long a wait took), so this suite's autouse fake clock
is undone for every test under this directory.
"""
import pytest


@pytest.fixture(autouse=True)
def _upstream_clock(real_clock):
    yield
