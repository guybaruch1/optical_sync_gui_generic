from engine.metrics import FramePairSample, MetricResult, Metric, PairingGapMetric
from engine.test_session import TestSession, TestSessionConfig


class FakeMetric(Metric):
    name = "fake_metric"

    def update(self, sample):
        return MetricResult(name=self.name, value=float(sample.pair_index), excluded=False, exclude_reason=None)


class DropAwareFakeMetric(Metric):
    # Mirrors how a real Metric consults sample.stream_a_frame_drop/
    # stream_b_frame_drop once TestSession has set them, to prove the row/
    # excluded flag stays consistent across metrics, not just for
    # PairingGapMetric specifically.
    name = "drop_aware_metric"

    def update(self, sample):
        is_drop = sample.stream_a_frame_drop or sample.stream_b_frame_drop
        return MetricResult(
            name=self.name, value=0.0, excluded=is_drop,
            exclude_reason="frame_drop" if is_drop else None,
        )


class FakeMetricWithExtra(Metric):
    name = "fake_with_extra"

    def update(self, sample):
        return MetricResult(
            name=self.name, value=1.0, excluded=False, exclude_reason=None,
            extra={"custom_flag": True},
        )


def test_start_sets_running_true():
    session = TestSession(TestSessionConfig(metrics=[FakeMetric()]))
    assert session.is_running is False
    session.start()
    assert session.is_running is True


def test_process_pair_returns_flat_row_and_buffers_it():
    session = TestSession(TestSessionConfig(metrics=[FakeMetric()]))
    session.start()
    row = session.process_pair(FramePairSample(pair_index=0, stream_a_ts_us=100.0, stream_b_ts_us=100.0))
    assert row["pair_index"] == 0
    assert row["stream_a_ts_us"] == 100.0
    assert row["fake_metric"] == 0.0
    assert row["fake_metric_excluded"] is False
    assert row["fake_metric_exclude_reason"] is None
    # No fps configured -> _is_frame_drop can't tell, so both default to False.
    assert row["stream_a_frame_drop"] is False
    assert row["stream_b_frame_drop"] is False


def test_process_pair_detects_frame_drop_end_to_end_and_excludes_every_metric():
    # Proves the "a dropped frame invalidates both measurements for that
    # pair" behavior end to end: TestSession computes the drop once from
    # configured fps/threshold_factor, writes it into the row AND mutates the
    # sample so every metric's own update(sample) call sees the same flag.
    session = TestSession(TestSessionConfig(
        metrics=[PairingGapMetric(outlier_threshold_us=100_000), DropAwareFakeMetric()],
        stream_a_fps=30, stream_b_fps=30, frame_drop_threshold_factor=1.5,
    ))
    session.start()
    session.process_pair(FramePairSample(pair_index=0, stream_a_ts_us=0.0, stream_b_ts_us=0.0))
    # Expected delta at 30fps is ~33333us; a 500_000us jump on stream_a only
    # should trip the drop check for stream_a but not stream_b.
    row = session.process_pair(FramePairSample(pair_index=1, stream_a_ts_us=500_000.0, stream_b_ts_us=33_333.0))

    assert row["stream_a_frame_drop"] is True
    assert row["stream_b_frame_drop"] is False
    assert row["pairing_gap_us_excluded"] is True
    assert row["pairing_gap_us_exclude_reason"] == "frame_drop"
    assert row["drop_aware_metric_excluded"] is True
    assert row["drop_aware_metric_exclude_reason"] == "frame_drop"


def test_process_pair_folds_extra_dict_into_row():
    session = TestSession(TestSessionConfig(metrics=[FakeMetricWithExtra()]))
    session.start()
    row = session.process_pair(FramePairSample(pair_index=0, stream_a_ts_us=0.0, stream_b_ts_us=0.0))
    assert row["custom_flag"] is True


def test_stop_returns_all_buffered_rows_and_sets_running_false():
    session = TestSession(TestSessionConfig(metrics=[FakeMetric()]))
    session.start()
    session.process_pair(FramePairSample(0, 0.0, 0.0))
    session.process_pair(FramePairSample(1, 1.0, 1.0))
    rows = session.stop()
    assert len(rows) == 2
    assert session.is_running is False


def test_should_auto_stop_respects_configured_duration():
    session = TestSession(TestSessionConfig(metrics=[], duration_s=5.0))
    assert session.should_auto_stop(elapsed_s=4.9) is False
    assert session.should_auto_stop(elapsed_s=5.0) is True


def test_should_auto_stop_never_true_when_duration_is_none():
    session = TestSession(TestSessionConfig(metrics=[], duration_s=None))
    assert session.should_auto_stop(elapsed_s=1_000_000.0) is False


def test_process_pair_carries_global_ts_into_the_row():
    session = TestSession(TestSessionConfig(metrics=[FakeMetric()]))
    session.start()
    row = session.process_pair(FramePairSample(
        pair_index=0, stream_a_ts_us=100.0, stream_b_ts_us=100.0,
        stream_a_global_ts_us=5_000.0, stream_b_global_ts_us=5_001.0,
    ))
    assert row["stream_a_global_ts_us"] == 5_000.0
    assert row["stream_b_global_ts_us"] == 5_001.0


def test_process_pair_defaults_global_ts_to_none_when_not_captured():
    # Every existing single-camera FramePairSample call (this file's own
    # other tests included) never sets these two fields - process_pair
    # must not require them.
    session = TestSession(TestSessionConfig(metrics=[FakeMetric()]))
    session.start()
    row = session.process_pair(FramePairSample(pair_index=0, stream_a_ts_us=100.0, stream_b_ts_us=100.0))
    assert row["stream_a_global_ts_us"] is None
    assert row["stream_b_global_ts_us"] is None
