import math
import os

import pytest

from domain.panel_drift_analysis import (
    parse_gap_series, linear_fit_rate, bin_series, find_transitions, compute_local_rates,
    summarize_drift, export_drift_over_time_plot, export_local_rate_plot,
)


def _row(ts_us, gap_ms, excluded=False):
    return {
        "stream_a_ts_us": ts_us,
        "position_gap_ms": gap_ms,
        "position_gap_ms_excluded": excluded,
    }


def test_parse_gap_series_skips_excluded_and_missing():
    rows = [
        _row(0, 0.0),
        _row(200_000, 1.0, excluded=True),
        _row(400_000, None),
        _row(600_000, 2.0),
    ]
    elapsed_s, values = parse_gap_series(rows)
    assert elapsed_s == [0.0, 0.6]
    assert values == [0.0, 2.0]


def test_parse_gap_series_returns_empty_for_no_usable_rows():
    assert parse_gap_series([]) == ([], [])
    assert parse_gap_series([{"stream_a_ts_us": None, "position_gap_ms": 1.0}]) == ([], [])


def test_linear_fit_rate_recovers_a_known_slope():
    # y = 2*t + 1 exactly - polyfit must recover slope=2, intercept=1.
    elapsed_s = [0.0, 1.0, 2.0, 3.0]
    values = [1.0, 3.0, 5.0, 7.0]
    slope, intercept = linear_fit_rate(elapsed_s, values)
    assert slope == pytest.approx(2.0)
    assert intercept == pytest.approx(1.0)


def test_linear_fit_rate_none_with_fewer_than_2_points():
    assert linear_fit_rate([0.0], [1.0]) is None
    assert linear_fit_rate([], []) is None


def test_linear_fit_rate_none_when_all_timestamps_identical():
    # No time spread at all - a slope isn't meaningfully definable.
    assert linear_fit_rate([5.0, 5.0, 5.0], [1.0, 2.0, 3.0]) is None


def test_bin_series_takes_median_per_bin():
    elapsed_s = [0.5, 1.0, 1.5, 9.9, 10.5, 11.0]
    values = [1.0, 1.0, 3.0, 1.0, 5.0, 5.0]
    binned = bin_series(elapsed_s, values, bin_seconds=10.0)
    # bin 0 (t in [0, 10)): values [1, 1, 3] -> median 1.0
    # bin 1 (t in [10, 20)): values [5, 5] -> median 5.0
    assert [v for _, v in binned] == [1.0, 5.0]


def test_bin_series_is_robust_to_within_bin_oscillation():
    # Mirrors real-hardware behavior seen after a panel-drift transition: a
    # several-second oscillation between two adjacent values before settling -
    # entirely within one 10s bin here, so the median must absorb it rather
    # than reporting it as a real change.
    elapsed_s = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]
    values = [0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    binned = bin_series(elapsed_s, values, bin_seconds=10.0)
    assert len(binned) == 1
    assert binned[0][1] == 1.0  # majority value in the bin


def test_find_transitions_ignores_flat_series():
    binned = [(5.0, 1.0), (15.0, 1.0), (25.0, 1.0)]
    assert find_transitions(binned) == []


def test_find_transitions_detects_real_changes_only():
    binned = [(5.0, 0.0), (15.0, 0.0), (25.0, 1.0), (35.0, 1.0), (45.0, 2.0)]
    transitions = find_transitions(binned)
    assert transitions == [(25.0, 0.0, 1.0), (45.0, 1.0, 2.0)]


def test_compute_local_rates_between_consecutive_transitions():
    # Transition to 1 at t=20 (from t=0's implicit start), then to 2 at t=60 -
    # local rate for the 2nd transition is (2-1)/(60-20) = 0.025 ms/s.
    transitions = [(20.0, 0.0, 1.0), (60.0, 1.0, 2.0)]
    rates = compute_local_rates(transitions)
    assert rates == [(60.0, 0.025)]


def test_compute_local_rates_empty_with_fewer_than_2_transitions():
    assert compute_local_rates([]) == []
    assert compute_local_rates([(20.0, 0.0, 1.0)]) == []


def test_summarize_drift_end_to_end():
    rows = [_row(int(t * 1_000_000), v) for t, v in [
        (0.0, 0.0), (5.0, 0.0), (12.0, 1.0), (18.0, 1.0), (25.0, 2.0),
    ]]
    summary = summarize_drift(rows, bin_seconds=10.0)
    assert summary["elapsed_s_range"] == (0.0, 25.0)
    assert summary["n_samples"] == 5
    assert summary["overall_slope_per_s"] is not None
    assert len(summary["transitions"]) >= 1


def test_summarize_drift_handles_no_usable_rows():
    summary = summarize_drift([])
    assert summary["elapsed_s_range"] is None
    assert summary["overall_slope_per_s"] is None
    assert summary["transitions"] == []


def test_export_drift_over_time_plot_writes_a_file(tmp_path):
    rows = [_row(int(t * 1_000_000), v) for t, v in [
        (0.0, 0.0), (5.0, 0.0), (12.0, 1.0), (18.0, 1.0), (25.0, 2.0),
    ]]
    path = str(tmp_path / "over_time.png")

    summary = export_drift_over_time_plot(rows, path, bin_seconds=10.0)

    assert os.path.exists(path)
    assert os.path.getsize(path) > 0
    assert summary["elapsed_s_range"] == (0.0, 25.0)


def test_export_drift_over_time_plot_returns_none_for_no_usable_rows(tmp_path):
    path = str(tmp_path / "over_time.png")
    assert export_drift_over_time_plot([], path) is None
    assert not os.path.exists(path)


def test_export_local_rate_plot_writes_a_file_when_enough_transitions(tmp_path):
    summary = {
        "local_rates": [(60.0, 0.025), (120.0, 0.05)],
        "overall_slope_per_s": 0.03,
    }
    path = str(tmp_path / "local_rate.png")

    result = export_local_rate_plot(summary, path)

    assert result is True
    assert os.path.exists(path)
    assert os.path.getsize(path) > 0


def test_export_local_rate_plot_false_with_no_transitions(tmp_path):
    summary = {"local_rates": [], "overall_slope_per_s": None}
    path = str(tmp_path / "local_rate.png")

    result = export_local_rate_plot(summary, path)

    assert result is False
    assert not os.path.exists(path)
