# tests/unit/test_sensor_series.py
"""SensorSeries: the one sample buffer."""

from fluidics.sensor_series import SensorSeries


def test_series_window_trims_by_seconds():
    series = SensorSeries()
    for i in range(10):
        series.append(float(i), t=100.0 + i)
    ts, vs = series.window()
    assert len(ts) == 10 and vs[0] == 0.0
    ts, vs = series.window(seconds=3)
    assert ts == [106.0, 107.0, 108.0, 109.0] and vs == [6.0, 7.0, 8.0, 9.0]
