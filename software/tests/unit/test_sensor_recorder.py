import threading

from fluidics.sensor_recorder import SensorRecorder, SensorSeries


def test_series_window_trims_by_seconds():
    series = SensorSeries()
    for i in range(10):
        series.append(float(i), t=100.0 + i)
    ts, vs = series.window()
    assert len(ts) == 10 and vs[0] == 0.0
    ts, vs = series.window(seconds=3)
    assert ts == [106.0, 107.0, 108.0, 109.0] and vs == [6.0, 7.0, 8.0, 9.0]


def test_recorder_writes_csv_only_while_recording(tmp_path):
    recorder = SensorRecorder()
    recorder.record("channel_1", 20.0, t=1.0)
    assert not recorder.recording

    path = tmp_path / "t.csv"
    assert recorder.start_recording(str(path))
    recorder.set_step_label("R01 hyb")
    recorder.record("channel_1", 21.0, t=2.0)
    recorder.stop_recording()
    recorder.record("channel_1", 22.0, t=3.0)

    lines = path.read_text().strip().splitlines()
    assert lines[0] == "time,channel,value,step"
    assert lines[1] == "2.000,channel_1,21.0,R01 hyb"
    assert len(lines) == 2
    assert len(recorder.channel("channel_1").window()[0]) == 3


def test_a_failed_write_stops_the_recording_and_says_so(tmp_path, caplog):
    """A recording that cannot be written must not take the run with it:
    it stops itself, loudly, and later samples still reach the buffers."""
    import logging
    recorder = SensorRecorder()
    path = tmp_path / "t.csv"
    assert recorder.start_recording(str(path))

    class Refuses:
        def writerow(self, row):
            raise OSError("disk full")

    recorder._writer = Refuses()
    with caplog.at_level(logging.WARNING, logger="fluidics"):
        recorder.record("channel_1", 21.0, t=2.0)
    assert not recorder.recording, "a failed write must stop the recording"
    assert "stopping the recording" in caplog.text
    recorder.record("channel_1", 22.0, t=3.0)          # must not raise
    assert len(recorder.channel("channel_1").window()[0]) == 2


def test_the_step_label_tags_the_rows_written_after_it(tmp_path):
    recorder = SensorRecorder()
    path = tmp_path / "t.csv"
    assert recorder.start_recording(str(path))
    recorder.record("flow", 1.0, t=1.0)
    recorder.set_step_label("R01 hyb")
    recorder.record("flow", 2.0, t=2.0)
    recorder.set_step_label("")
    recorder.record("flow", 3.0, t=3.0)
    recorder.stop_recording()
    steps = [line.split(",")[3] for line in
             path.read_text().strip().splitlines()[1:]]
    assert steps == ["", "R01 hyb", ""]


def test_the_tail_is_flushed_when_the_recording_stops(tmp_path):
    """Rows are flushed on an interval, not per sample, so stopping has to
    push out what the interval has not."""
    recorder = SensorRecorder()
    path = tmp_path / "t.csv"
    assert recorder.start_recording(str(path))
    recorder.record("flow", 1.0, t=1.0)
    recorder.record("flow", 2.0, t=2.0)
    recorder.stop_recording()
    assert len(path.read_text().strip().splitlines()) == 3


def test_the_flush_cadence_ignores_the_samples_own_timestamps(tmp_path,
                                                              monkeypatch):
    """Timestamps are the caller's and may be historic, replayed out of
    order, or stepped by NTP -- none of which should decide when the file
    is flushed, or the bound on what a crash costs is not a bound. The
    monotonic clock is held still here, so only a sample timestamp could
    move the deadline."""
    import fluidics.sensor_recorder as recorder_module
    monkeypatch.setattr(recorder_module.time, "monotonic", lambda: 1000.0)
    recorder = SensorRecorder()
    path = tmp_path / "t.csv"
    assert recorder.start_recording(str(path))
    recorder.record("flow", 1.0, t=0.0)
    recorder.record("flow", 2.0, t=100_000.0)     # a day's jump in sample time
    assert path.read_text().strip().splitlines() == ["time,channel,value,step"], \
        "a sample timestamp drove the flush"
    recorder.stop_recording()
    assert len(path.read_text().strip().splitlines()) == 3


def test_the_cadence_does_flush_once_the_interval_passes(tmp_path, monkeypatch):
    """The other half: the deadline is real, so a recording left running
    reaches the disk without waiting to be stopped."""
    import fluidics.sensor_recorder as recorder_module
    clock = iter([1000.0, 1000.5, 1002.0])
    monkeypatch.setattr(recorder_module.time, "monotonic", lambda: next(clock))
    recorder = SensorRecorder()
    path = tmp_path / "t.csv"
    assert recorder.start_recording(str(path))          # 1000.0: the header
    recorder.record("flow", 1.0, t=1.0)                 # 1000.5: too soon
    assert len(path.read_text().strip().splitlines()) == 1
    recorder.record("flow", 2.0, t=2.0)                 # 1002.0: due
    assert len(path.read_text().strip().splitlines()) == 3


def test_recorder_is_thread_safe():
    recorder = SensorRecorder()

    def pump(name):
        for i in range(1000):
            recorder.record(name, float(i))

    threads = [threading.Thread(target=pump, args=(f"c{k}",)) for k in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(recorder.channel("c0").window()[0]) == 1000
    assert len(recorder.channel("c1").window()[0]) == 1000
