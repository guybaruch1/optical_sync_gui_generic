import csv
from domain.csv_export import export_clean_and_other_csvs, export_session_csvs, export_series_csv


def _row(pair_index, exclude_reason=None, stream_a_frame_drop=False, stream_b_frame_drop=False,
         pairing_gap_us_excluded=False, pairing_gap_us_exclude_reason=None):
    return {
        "pair_index": pair_index,
        "ir_ts_us": 1000.0 + pair_index,
        "rgb_ts_us": 1000.5 + pair_index,
        "stream_a_frame_drop": stream_a_frame_drop,
        "stream_b_frame_drop": stream_b_frame_drop,
        "pairing_gap_us": -0.5,
        "pairing_gap_us_excluded": pairing_gap_us_excluded,
        "pairing_gap_us_exclude_reason": pairing_gap_us_exclude_reason,
        "position_gap_ms_excluded": exclude_reason is not None,
        "position_gap_ms_exclude_reason": exclude_reason,
    }


def test_export_session_csvs_splits_by_frame_drop(tmp_path):
    rows = [
        _row(0),
        _row(1, exclude_reason="frame_drop", stream_a_frame_drop=True),
        _row(2, exclude_reason="warmup"),
    ]
    kept_path = tmp_path / "kept.csv"
    dropped_path = tmp_path / "dropped.csv"

    n_kept, n_dropped = export_session_csvs(rows, str(kept_path), str(dropped_path))

    assert n_kept == 2  # pair 0 (clean) and pair 2 (warmup - still kept, just flagged)
    assert n_dropped == 1  # pair 1 (frame_drop) goes to the dropped file

    with open(kept_path, newline="") as f:
        kept_rows = list(csv.DictReader(f))
    with open(dropped_path, newline="") as f:
        dropped_rows = list(csv.DictReader(f))

    assert [r["pair_index"] for r in kept_rows] == ["0", "2"]
    assert [r["pair_index"] for r in dropped_rows] == ["1"]


def test_export_session_csvs_routes_by_boolean_flag_even_when_exclude_reason_is_no_led_data(tmp_path):
    # Regression test for sub-finding 2b: a pair that's BOTH a frame drop and
    # missing LED data gets labeled "no_led_data" by PositionGapMetric (its
    # no_led_data > miss > frame_drop > warmup priority order), not
    # "frame_drop" - so the old string-match against "*_exclude_reason" ==
    # "frame_drop" would have misrouted it into the kept file. The new
    # boolean-flag check must still route it to dropped.
    rows = [
        _row(0, exclude_reason="no_led_data", stream_b_frame_drop=True),
    ]
    kept_path = tmp_path / "kept.csv"
    dropped_path = tmp_path / "dropped.csv"

    n_kept, n_dropped = export_session_csvs(rows, str(kept_path), str(dropped_path))

    assert (n_kept, n_dropped) == (0, 1)


def test_export_session_csvs_empty_rows(tmp_path):
    kept_path = tmp_path / "kept.csv"
    dropped_path = tmp_path / "dropped.csv"
    n_kept, n_dropped = export_session_csvs([], str(kept_path), str(dropped_path))
    assert (n_kept, n_dropped) == (0, 0)
    assert kept_path.exists()
    assert dropped_path.exists()


def test_export_clean_and_other_csvs_splits_by_any_excluded_flag(tmp_path):
    # Different split from export_session_csvs' kept/dropped: this one
    # routes on whether ANY "*_excluded" column is True, not just the
    # frame-drop boolean - miss/warmup/outlier rows go to "other" too, not
    # just frame_drop ones.
    rows = [
        _row(0),  # nothing excluded on either metric - clean
        _row(1, exclude_reason="frame_drop", stream_a_frame_drop=True,
             pairing_gap_us_excluded=True, pairing_gap_us_exclude_reason="frame_drop"),
        _row(2, exclude_reason="warmup"),
        _row(3, exclude_reason="miss"),
        _row(4, pairing_gap_us_excluded=True, pairing_gap_us_exclude_reason="syncer_outlier"),
    ]
    clean_path = tmp_path / "clean.csv"
    other_path = tmp_path / "other.csv"

    n_clean, n_other = export_clean_and_other_csvs(rows, str(clean_path), str(other_path))

    assert n_clean == 1
    assert n_other == 4

    with open(clean_path, newline="") as f:
        clean_rows = list(csv.DictReader(f))
    with open(other_path, newline="") as f:
        other_rows = list(csv.DictReader(f))

    assert [r["pair_index"] for r in clean_rows] == ["0"]
    assert [r["pair_index"] for r in other_rows] == ["1", "2", "3", "4"]


def test_export_clean_and_other_csvs_is_generic_over_any_excluded_column():
    # Not hardcoded to pairing_gap_us/position_gap_ms specifically - any
    # future metric's own "<name>_excluded" column must route a row to
    # "other" too, since TestSession.process_pair writes one such column
    # per registered Metric, not just the two built-in ones.
    from domain.csv_export import _is_clean_row

    clean_row = {"pair_index": 0, "some_metric_excluded": False}
    dirty_row = {"pair_index": 1, "some_metric_excluded": True}
    assert _is_clean_row(clean_row) is True
    assert _is_clean_row(dirty_row) is False


def test_export_clean_and_other_csvs_empty_rows(tmp_path):
    clean_path = tmp_path / "clean.csv"
    other_path = tmp_path / "other.csv"
    n_clean, n_other = export_clean_and_other_csvs([], str(clean_path), str(other_path))
    assert (n_clean, n_other) == (0, 0)
    assert clean_path.exists()
    assert other_path.exists()


def test_export_series_csv_writes_one_column_per_series(tmp_path):
    path = tmp_path / "chart.csv"
    export_series_csv(
        str(path),
        series_x=[0, 1, 2],
        series_y_by_name={"stream_a_frame_drops": [0, 1, 0], "stream_b_frame_drops": [0, 0, -1]},
    )

    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))

    assert [r["pair_index"] for r in rows] == ["0", "1", "2"]
    assert [r["stream_a_frame_drops"] for r in rows] == ["0", "1", "0"]
    assert [r["stream_b_frame_drops"] for r in rows] == ["0", "0", "-1"]
