import numpy as np
from engine.acquisition_loop import AcquisitionLoop, AcquisitionCallbacks
from engine.metrics import Metric, MetricResult
from engine.test_session import TestSession, TestSessionConfig


class CountingMetric(Metric):
    name = "count"

    def update(self, sample):
        return MetricResult(name=self.name, value=float(sample.pair_index), excluded=False, exclude_reason=None)


def fake_frame_source(n_pairs):
    for i in range(n_pairs):
        stream_a_image = np.full((4, 4, 3), i, dtype=np.uint8)
        stream_b_image = np.full((4, 4, 3), i, dtype=np.uint8)
        yield stream_a_image, stream_b_image, float(i), float(i), None, None


def test_run_until_stopped_processes_every_frame_and_calls_on_row():
    session = TestSession(TestSessionConfig(metrics=[CountingMetric()]))
    session.start()
    rows_seen = []
    frames_seen = []
    stats_seen = []
    callbacks = AcquisitionCallbacks(
        on_frames=lambda stream_a, stream_b, idx: frames_seen.append(idx),
        on_row=lambda row: rows_seen.append(row),
        on_stats=lambda stats: stats_seen.append(stats),
    )
    loop = AcquisitionLoop(fake_frame_source(5), session, callbacks, display_stride=2)

    stop_after = {"count": 0}

    def is_stop_requested():
        stop_after["count"] += 1
        return stop_after["count"] > 5  # never true before the generator is exhausted

    rows = loop.run_until_stopped(is_stop_requested, elapsed_s_fn=lambda: 0.0)

    assert len(rows) == 5
    assert [row["pair_index"] for row in rows] == [0, 1, 2, 3, 4]
    assert len(rows_seen) == 5


def test_run_until_stopped_throttles_frame_display_by_stride():
    session = TestSession(TestSessionConfig(metrics=[CountingMetric()]))
    session.start()
    frames_seen = []
    callbacks = AcquisitionCallbacks(
        on_frames=lambda stream_a, stream_b, idx: frames_seen.append(idx),
        on_row=lambda row: None,
        on_stats=lambda stats: None,
    )
    loop = AcquisitionLoop(fake_frame_source(10), session, callbacks, display_stride=3)
    loop.run_until_stopped(is_stop_requested=lambda: False, elapsed_s_fn=lambda: 0.0)

    # Every metric row is still processed for all 10 pairs, but the video
    # callback should only fire every 3rd pair (0, 3, 6, 9).
    assert frames_seen == [0, 3, 6, 9]


def test_run_until_stopped_honors_stop_request_mid_stream():
    session = TestSession(TestSessionConfig(metrics=[CountingMetric()]))
    session.start()
    callbacks = AcquisitionCallbacks(on_frames=lambda *a: None, on_row=lambda r: None, on_stats=lambda s: None)
    loop = AcquisitionLoop(fake_frame_source(100), session, callbacks, display_stride=10)

    seen = {"n": 0}

    def is_stop_requested():
        seen["n"] += 1
        return seen["n"] > 3  # first true on the 4th check, i.e. before processing the 4th frame

    rows = loop.run_until_stopped(is_stop_requested, elapsed_s_fn=lambda: 0.0)
    # is_stop_requested is checked before each frame is processed, so the 4th
    # check (which returns True) stops the loop having processed exactly 3 frames.
    assert len(rows) == 3


def test_run_until_stopped_honors_session_auto_stop_duration():
    session = TestSession(TestSessionConfig(metrics=[CountingMetric()], duration_s=2.0))
    session.start()
    callbacks = AcquisitionCallbacks(on_frames=lambda *a: None, on_row=lambda r: None, on_stats=lambda s: None)
    loop = AcquisitionLoop(fake_frame_source(100), session, callbacks, display_stride=10)

    elapsed = {"t": 0.0}

    def elapsed_s_fn():
        elapsed["t"] += 1.0
        return elapsed["t"]

    rows = loop.run_until_stopped(is_stop_requested=lambda: False, elapsed_s_fn=elapsed_s_fn)
    # elapsed_s_fn is checked before each frame is processed: call 1 returns
    # 1.0 (< duration_s, so frame 0 is processed), call 2 returns 2.0
    # (>= duration_s, so the loop stops before processing a second frame).
    assert len(rows) == 1


def test_run_until_stopped_calls_on_frame_pair_every_pair_unthrottled():
    session = TestSession(TestSessionConfig(metrics=[CountingMetric()]))
    session.start()
    frame_pairs_seen = []
    callbacks = AcquisitionCallbacks(
        on_frames=lambda stream_a, stream_b, idx: None,
        on_row=lambda row: None,
        on_stats=lambda stats: None,
        on_frame_pair=lambda stream_a, stream_b, row: frame_pairs_seen.append(row["pair_index"]),
    )
    loop = AcquisitionLoop(fake_frame_source(5), session, callbacks, display_stride=10)
    loop.run_until_stopped(is_stop_requested=lambda: False, elapsed_s_fn=lambda: 0.0)

    # Unlike on_frames (throttled to every display_stride pairs), on_frame_pair
    # fires for every single pair - it must be able to see pairs the GUI's
    # own throttled callbacks never do.
    assert frame_pairs_seen == [0, 1, 2, 3, 4]


def test_run_until_stopped_works_without_on_frame_pair():
    # on_frame_pair is optional (defaults to None) - every existing caller
    # that doesn't pass it (SessionEngineThread before this change,
    # tools/panel_drift/panel_drift_measure.py, the other tests in this
    # file) must keep working unchanged.
    session = TestSession(TestSessionConfig(metrics=[CountingMetric()]))
    session.start()
    callbacks = AcquisitionCallbacks(on_frames=lambda *a: None, on_row=lambda r: None, on_stats=lambda s: None)
    loop = AcquisitionLoop(fake_frame_source(3), session, callbacks, display_stride=10)
    rows = loop.run_until_stopped(is_stop_requested=lambda: False, elapsed_s_fn=lambda: 0.0)
    assert len(rows) == 3
