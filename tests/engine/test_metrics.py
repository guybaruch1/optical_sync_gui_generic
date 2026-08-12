import numpy as np
from engine.metrics import (
    FramePairSample,
    find_last_on_led,
    compute_position_gap,
    PairingGapMetric,
    PositionGapMetric,
    _is_frame_drop,
    is_position_gap_debug_outlier,
)


def test_is_frame_drop_false_when_fps_is_zero():
    # 1_000_000.0 / fps would otherwise raise ZeroDivisionError.
    assert _is_frame_drop(prev_ts=0.0, curr_ts=100_000.0, fps=0, threshold_factor=1.5) is False


def test_is_frame_drop_false_when_fps_is_negative():
    assert _is_frame_drop(prev_ts=0.0, curr_ts=100_000.0, fps=-30, threshold_factor=1.5) is False


def test_is_frame_drop_still_detects_a_real_drop_with_valid_fps():
    # Expected delta at 30fps is ~33333us; a 500_000us jump should still trip it.
    assert _is_frame_drop(prev_ts=0.0, curr_ts=500_000.0, fps=30, threshold_factor=1.5) is True


def test_is_frame_drop_false_for_a_normal_on_time_delta():
    # Expected delta at 30fps is ~33333us; landing right on schedule is not a drop.
    assert _is_frame_drop(prev_ts=0.0, curr_ts=33_333.0, fps=30, threshold_factor=1.5) is False


def test_is_frame_drop_true_when_timestamp_exactly_repeats():
    # delta == 0 means the pipeline handed back the SAME frame again instead of a
    # new one (a stale/duplicate frame) - real hardware never produces two
    # distinct captures with a byte-identical HW timestamp. This used to slip
    # through uncaught: 0 is neither negative nor greater than the threshold, so
    # a repeated frame looked indistinguishable from "right on schedule".
    assert _is_frame_drop(prev_ts=100_000.0, curr_ts=100_000.0, fps=30, threshold_factor=1.5) is True


def test_find_last_on_led_plain_block():
    on = np.zeros(10, dtype=bool)
    on[3:6] = True  # LEDs 3,4,5 on -> last is 5
    last, length = find_last_on_led(on)
    assert last == 5
    assert length == 3


def test_find_last_on_led_wrap_around():
    on = np.zeros(10, dtype=bool)
    on[[8, 9, 0, 1]] = True  # wraps 9->0, post-wrap highest is 1
    last, length = find_last_on_led(on)
    assert last == 1
    assert length == 4


def test_find_last_on_led_nothing_on():
    on = np.zeros(10, dtype=bool)
    last, length = find_last_on_led(on)
    assert last is None
    assert length == 0


def test_compute_position_gap_wraps_to_shortest_path():
    diff = compute_position_gap(stream_a_last=2, stream_b_last=98, n=100)
    assert diff == 4


def test_pairing_gap_metric_flags_outlier():
    metric = PairingGapMetric(outlier_threshold_us=100_000)
    sample = FramePairSample(pair_index=0, stream_a_ts_us=1_000_000.0, stream_b_ts_us=1_500_000.0)
    result = metric.update(sample)
    assert result.name == "pairing_gap_us"
    assert result.value == -500_000.0
    assert result.excluded is True
    assert result.exclude_reason == "syncer_outlier"


def test_pairing_gap_metric_accepts_close_pair():
    metric = PairingGapMetric(outlier_threshold_us=100_000)
    sample = FramePairSample(pair_index=0, stream_a_ts_us=1_000_000.0, stream_b_ts_us=1_000_050.0)
    result = metric.update(sample)
    assert result.excluded is False
    assert result.exclude_reason is None


def test_pairing_gap_metric_excludes_on_frame_drop_even_within_outlier_threshold():
    # A frame drop must exclude the pairing-gap measurement too, even when the
    # raw timestamp gap is well within outlier_threshold_us - this is the bug
    # fix: previously PairingGapMetric had zero frame-drop awareness.
    metric = PairingGapMetric(outlier_threshold_us=100_000)
    sample = FramePairSample(
        pair_index=0, stream_a_ts_us=1_000_000.0, stream_b_ts_us=1_000_050.0,
        stream_a_frame_drop=True, stream_b_frame_drop=False,
    )
    result = metric.update(sample)
    assert result.excluded is True
    assert result.exclude_reason == "frame_drop"


def test_pairing_gap_metric_excludes_on_stream_b_frame_drop():
    metric = PairingGapMetric(outlier_threshold_us=100_000)
    sample = FramePairSample(
        pair_index=0, stream_a_ts_us=1_000_000.0, stream_b_ts_us=1_000_050.0,
        stream_a_frame_drop=False, stream_b_frame_drop=True,
    )
    result = metric.update(sample)
    assert result.excluded is True
    assert result.exclude_reason == "frame_drop"


def test_pairing_gap_metric_frame_drop_reason_wins_over_outlier():
    # Both a drop and an outlier gap present at once - reason should still
    # read "frame_drop" per the decided priority (drop OR outlier; reason is
    # "frame_drop" if the drop is why).
    metric = PairingGapMetric(outlier_threshold_us=100_000)
    sample = FramePairSample(
        pair_index=0, stream_a_ts_us=1_000_000.0, stream_b_ts_us=1_500_000.0,
        stream_a_frame_drop=True, stream_b_frame_drop=False,
    )
    result = metric.update(sample)
    assert result.excluded is True
    assert result.exclude_reason == "frame_drop"


def test_position_gap_metric_reports_miss_when_nothing_on():
    threshold = np.full(10, 150.0)
    metric = PositionGapMetric(
        stream_a_threshold=threshold, stream_b_threshold=threshold, num_leds=10,
        switch_time_ms=1.0, warmup_pairs_to_skip=0,
    )
    sample = FramePairSample(
        pair_index=0, stream_a_ts_us=0.0, stream_b_ts_us=0.0,
        stream_a_bright=np.full(10, 50.0), stream_b_bright=np.full(10, 50.0),
    )
    result = metric.update(sample)
    assert result.excluded is True
    assert result.exclude_reason == "miss"


def test_position_gap_metric_computes_gap_ms():
    threshold = np.full(10, 150.0)
    metric = PositionGapMetric(
        stream_a_threshold=threshold, stream_b_threshold=threshold, num_leds=10,
        switch_time_ms=2.0, warmup_pairs_to_skip=0,
    )
    stream_a_bright = np.full(10, 50.0); stream_a_bright[5] = 200.0
    stream_b_bright = np.full(10, 50.0); stream_b_bright[3] = 200.0
    sample = FramePairSample(pair_index=0, stream_a_ts_us=0.0, stream_b_ts_us=0.0, stream_a_bright=stream_a_bright, stream_b_bright=stream_b_bright)
    result = metric.update(sample)
    assert result.excluded is False
    assert result.value == 4.0  # (5 - 3) LED steps * 2.0 ms


def test_position_gap_metric_flags_warmup_pairs():
    threshold = np.full(10, 150.0)
    metric = PositionGapMetric(
        stream_a_threshold=threshold, stream_b_threshold=threshold, num_leds=10,
        switch_time_ms=1.0, warmup_pairs_to_skip=2,
    )
    bright = np.full(10, 200.0)
    first = metric.update(FramePairSample(0, 0.0, 0.0, bright, bright))
    second = metric.update(FramePairSample(1, 33333.0, 33333.0, bright, bright))
    third = metric.update(FramePairSample(2, 66666.0, 66666.0, bright, bright))
    assert first.exclude_reason == "warmup"
    assert second.exclude_reason == "warmup"
    assert third.exclude_reason is None


def test_position_gap_metric_flags_frame_drop():
    # PositionGapMetric no longer computes frame-drop status itself - it just
    # reads sample.stream_a_frame_drop/stream_b_frame_drop, which TestSession
    # is now responsible for setting before calling update().
    threshold = np.full(10, 150.0)
    metric = PositionGapMetric(
        stream_a_threshold=threshold, stream_b_threshold=threshold, num_leds=10,
        switch_time_ms=1.0, warmup_pairs_to_skip=0,
    )
    bright = np.full(10, 200.0)
    result = metric.update(FramePairSample(
        1, 500_000.0, 33333.0, bright, bright, stream_a_frame_drop=True, stream_b_frame_drop=False,
    ))
    assert result.exclude_reason == "frame_drop"


def test_position_gap_metric_no_frame_drop_reason_when_sample_flags_clean():
    threshold = np.full(10, 150.0)
    metric = PositionGapMetric(
        stream_a_threshold=threshold, stream_b_threshold=threshold, num_leds=10,
        switch_time_ms=1.0, warmup_pairs_to_skip=0,
    )
    stream_a_bright = np.full(10, 50.0); stream_a_bright[5] = 200.0
    stream_b_bright = np.full(10, 50.0); stream_b_bright[3] = 200.0
    sample = FramePairSample(
        pair_index=0, stream_a_ts_us=0.0, stream_b_ts_us=0.0,
        stream_a_bright=stream_a_bright, stream_b_bright=stream_b_bright,
        stream_a_frame_drop=False, stream_b_frame_drop=False,
    )
    result = metric.update(sample)
    assert result.excluded is False
    assert result.exclude_reason is None


def test_position_gap_metric_tracks_last_on_masks_for_debug_snapshots():
    threshold = np.full(4, 150.0)
    metric = PositionGapMetric(
        stream_a_threshold=threshold, stream_b_threshold=threshold, num_leds=4,
        switch_time_ms=1.0, warmup_pairs_to_skip=0,
    )
    assert metric.last_stream_a_on_mask is None
    assert metric.last_stream_b_on_mask is None

    stream_a_bright = np.array([50.0, 200.0, 50.0, 50.0])
    stream_b_bright = np.array([200.0, 50.0, 50.0, 50.0])
    metric.update(FramePairSample(0, 0.0, 0.0, stream_a_bright, stream_b_bright))

    assert metric.last_stream_a_on_mask.tolist() == [False, True, False, False]
    assert metric.last_stream_b_on_mask.tolist() == [True, False, False, False]


def test_is_position_gap_debug_outlier_true_at_exact_positive_threshold():
    # >=, not >, matching "delta above or equal to 5".
    row = {"position_gap_ms": 5.0, "position_gap_ms_excluded": False}
    assert is_position_gap_debug_outlier(row, threshold_ms=5.0) is True


def test_is_position_gap_debug_outlier_true_at_exact_negative_threshold():
    # Magnitude-based - a -5ms gap is just as much an outlier as +5ms.
    row = {"position_gap_ms": -5.0, "position_gap_ms_excluded": False}
    assert is_position_gap_debug_outlier(row, threshold_ms=5.0) is True


def test_is_position_gap_debug_outlier_false_below_threshold():
    row = {"position_gap_ms": 4.9, "position_gap_ms_excluded": False}
    assert is_position_gap_debug_outlier(row, threshold_ms=5.0) is False


def test_is_position_gap_debug_outlier_false_when_already_excluded():
    # A frame_drop/warmup-excluded row already has a known cause - don't
    # also flag it as an unexplained optical-sync outlier.
    row = {"position_gap_ms": 50.0, "position_gap_ms_excluded": True}
    assert is_position_gap_debug_outlier(row, threshold_ms=5.0) is False


def test_is_position_gap_debug_outlier_false_when_value_is_none():
    # no_led_data/miss rows carry value=None - nothing to threshold against.
    row = {"position_gap_ms": None, "position_gap_ms_excluded": True}
    assert is_position_gap_debug_outlier(row, threshold_ms=5.0) is False
