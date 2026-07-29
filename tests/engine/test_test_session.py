from engine.metrics import FramePairSample, MetricResult, Metric
from engine.test_session import TestSession, TestSessionConfig


class FakeMetric(Metric):
    name = "fake_metric"

    def update(self, sample):
        return MetricResult(name=self.name, value=float(sample.pair_index), excluded=False, exclude_reason=None)


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
    row = session.process_pair(FramePairSample(pair_index=0, ir_ts_us=100.0, rgb_ts_us=100.0))
    assert row["pair_index"] == 0
    assert row["ir_ts_us"] == 100.0
    assert row["fake_metric"] == 0.0
    assert row["fake_metric_excluded"] is False
    assert row["fake_metric_exclude_reason"] is None


def test_process_pair_folds_extra_dict_into_row():
    session = TestSession(TestSessionConfig(metrics=[FakeMetricWithExtra()]))
    session.start()
    row = session.process_pair(FramePairSample(pair_index=0, ir_ts_us=0.0, rgb_ts_us=0.0))
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
