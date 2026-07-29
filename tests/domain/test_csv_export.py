import csv
from domain.csv_export import export_session_csvs, export_series_csv


def _row(pair_index, exclude_reason=None):
    return {
        "pair_index": pair_index,
        "ir_ts_us": 1000.0 + pair_index,
        "rgb_ts_us": 1000.5 + pair_index,
        "pairing_gap_us": -0.5,
        "pairing_gap_us_excluded": False,
        "pairing_gap_us_exclude_reason": None,
        "position_gap_ms_excluded": exclude_reason is not None,
        "position_gap_ms_exclude_reason": exclude_reason,
    }


def test_export_session_csvs_splits_by_frame_drop(tmp_path):
    rows = [_row(0), _row(1, exclude_reason="frame_drop"), _row(2, exclude_reason="warmup")]
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


def test_export_session_csvs_empty_rows(tmp_path):
    kept_path = tmp_path / "kept.csv"
    dropped_path = tmp_path / "dropped.csv"
    n_kept, n_dropped = export_session_csvs([], str(kept_path), str(dropped_path))
    assert (n_kept, n_dropped) == (0, 0)
    assert kept_path.exists()
    assert dropped_path.exists()


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
