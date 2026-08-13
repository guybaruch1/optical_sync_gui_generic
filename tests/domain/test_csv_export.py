import csv
from domain.csv_export import export_session_csvs, export_series_csv, export_cross_camera_csv


def _row(pair_index, exclude_reason=None, stream_a_frame_drop=False, stream_b_frame_drop=False):
    return {
        "pair_index": pair_index,
        "ir_ts_us": 1000.0 + pair_index,
        "rgb_ts_us": 1000.5 + pair_index,
        "stream_a_frame_drop": stream_a_frame_drop,
        "stream_b_frame_drop": stream_b_frame_drop,
        "pairing_gap_us": -0.5,
        "pairing_gap_us_excluded": False,
        "pairing_gap_us_exclude_reason": None,
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


# --- export_cross_camera_csv: one combined file for every cross-camera
# pair, filterable/pivotable by slave_camera_id/stream_identity - NOT split
# kept/dropped like export_session_csvs (that split is specifically about
# per-stream frame drops within one camera's own pipeline; cross rows carry
# no stream_a_frame_drop/stream_b_frame_drop columns at all). ---

def _cross_row(pair_index, slave_camera_id="cam2", stream_identity="infrared1",
                pairing_gap_us=-10.0, excluded=False, exclude_reason=None):
    return {
        "pair_index": pair_index, "master_camera_id": "cam1", "slave_camera_id": slave_camera_id,
        "stream_identity": stream_identity, "master_pair_index": pair_index, "slave_pair_index": pair_index,
        "master_ts_us": 1000.0, "slave_ts_us": 1010.0,
        "pairing_gap_us": pairing_gap_us, "pairing_gap_us_excluded": excluded,
        "pairing_gap_us_exclude_reason": exclude_reason,
    }


def test_export_cross_camera_csv_writes_every_row_to_one_file(tmp_path):
    rows = [_cross_row(1), _cross_row(2, slave_camera_id="cam3", stream_identity="color")]
    path = tmp_path / "cross_camera_sync.csv"

    n_rows = export_cross_camera_csv(rows, str(path))

    assert n_rows == 2
    with open(path, newline="") as f:
        written = list(csv.DictReader(f))
    assert [r["slave_camera_id"] for r in written] == ["cam2", "cam3"]
    assert [r["stream_identity"] for r in written] == ["infrared1", "color"]


def test_export_cross_camera_csv_empty_rows_still_writes_a_valid_file(tmp_path):
    path = tmp_path / "cross_camera_sync.csv"

    n_rows = export_cross_camera_csv([], str(path))

    assert n_rows == 0
    assert path.exists()


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
