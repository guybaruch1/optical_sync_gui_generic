"""Pure Python, no Qt/hardware - matches independent per-camera row_ready
streams by nearest timestamp and reuses PairingGapMetric unmodified to
produce the new cross-camera (master-vs-slave) HW TS Latency metric for a
multi-camera sync test. See docs/superpowers's multi-camera design doc's
"Design detail" section 1 for the full rationale."""

from engine.cross_camera_reconciler import CrossCameraPairSpec, CrossCameraReconciler
from engine.metrics import PairingGapMetric


def _spec(master_camera_id="cam1", slave_camera_id="cam2", stream_identity="infrared1",
          master_row_role="stream_a", slave_row_role="stream_a", outlier_threshold_us=100_000):
    return CrossCameraPairSpec(
        master_camera_id=master_camera_id,
        slave_camera_id=slave_camera_id,
        stream_identity=stream_identity,
        master_row_role=master_row_role,
        slave_row_role=slave_row_role,
        pairing_gap_metric=PairingGapMetric(outlier_threshold_us=outlier_threshold_us),
    )


def _row(pair_index, ts_us, role="stream_a", frame_drop=False):
    return {
        "pair_index": pair_index,
        f"{role}_ts_us": ts_us,
        f"{role}_frame_drop": frame_drop,
    }


def test_master_row_then_slave_row_produces_a_matched_cross_row():
    spec = _spec()
    reconciler = CrossCameraReconciler([spec])

    assert reconciler.ingest_row("cam1", _row(10, 1_000_000.0)) == []
    cross_rows = reconciler.ingest_row("cam2", _row(20, 1_000_050.0))

    assert len(cross_rows) == 1
    row = cross_rows[0]
    assert row["master_camera_id"] == "cam1"
    assert row["slave_camera_id"] == "cam2"
    assert row["stream_identity"] == "infrared1"
    assert row["master_pair_index"] == 10
    assert row["slave_pair_index"] == 20
    assert row["pairing_gap_us"] == -50.0  # master_ts(1_000_000) - slave_ts(1_000_050)
    assert row["pairing_gap_us_excluded"] is False


def test_slave_row_then_master_row_produces_the_same_matched_cross_row():
    # Order must not matter - the two cameras' AcquisitionLoops run on
    # independent threads at independent cadences, either side's row can
    # legitimately arrive first.
    spec = _spec()
    reconciler = CrossCameraReconciler([spec])

    assert reconciler.ingest_row("cam2", _row(20, 1_000_050.0)) == []
    cross_rows = reconciler.ingest_row("cam1", _row(10, 1_000_000.0))

    assert len(cross_rows) == 1
    assert cross_rows[0]["pairing_gap_us"] == -50.0


def test_no_cross_row_when_no_counterpart_within_max_match_gap():
    # Explicit exclusion, not a forced/misleading match - matches this
    # project's existing convention (outlier thresholds, frame-drop flags,
    # warmup exclusion) of never silently connecting unrelated frames.
    spec = _spec()
    reconciler = CrossCameraReconciler([spec], max_match_gap_us=50_000)

    reconciler.ingest_row("cam1", _row(10, 1_000_000.0))
    cross_rows = reconciler.ingest_row("cam2", _row(20, 1_500_000.0))  # 500ms away

    assert cross_rows == []


def test_a_matched_master_row_is_not_reused_for_a_second_slave_row():
    spec = _spec()
    reconciler = CrossCameraReconciler([spec])

    reconciler.ingest_row("cam1", _row(10, 1_000_000.0))
    first_match = reconciler.ingest_row("cam2", _row(20, 1_000_010.0))
    assert len(first_match) == 1

    # cam1's pair_index=10 row was already consumed by the match above - a
    # second slave row landing near the SAME timestamp must not match it
    # again (it's gone from the buffer), even though it's numerically close.
    second_match = reconciler.ingest_row("cam2", _row(21, 1_000_015.0))
    assert second_match == []


def test_ignores_rows_from_a_camera_not_registered_in_any_pair_spec():
    spec = _spec()
    reconciler = CrossCameraReconciler([spec])

    assert reconciler.ingest_row("some_unrelated_camera", _row(1, 1_000_000.0)) == []


def test_two_specs_sharing_one_master_are_matched_independently():
    # 1 master vs 2 slaves, same stream identity - a heterogeneous-sensor
    # rig where the master's own row must independently pair against each
    # slave's buffered row, with no cross-interference between the two
    # slave streams.
    spec_vs_slave1 = _spec(slave_camera_id="cam2")
    spec_vs_slave2 = _spec(slave_camera_id="cam3")
    reconciler = CrossCameraReconciler([spec_vs_slave1, spec_vs_slave2])

    reconciler.ingest_row("cam2", _row(1, 1_000_010.0))
    reconciler.ingest_row("cam3", _row(1, 1_000_020.0))
    cross_rows = reconciler.ingest_row("cam1", _row(5, 1_000_000.0))

    assert len(cross_rows) == 2
    by_slave = {row["slave_camera_id"]: row for row in cross_rows}
    assert by_slave["cam2"]["pairing_gap_us"] == -10.0
    assert by_slave["cam3"]["pairing_gap_us"] == -20.0


def test_matched_cross_row_excluded_when_either_side_dropped_a_frame():
    # Reuses PairingGapMetric's own existing frame-drop-takes-priority
    # exclusion logic completely unmodified.
    spec = _spec()
    reconciler = CrossCameraReconciler([spec])

    reconciler.ingest_row("cam1", _row(10, 1_000_000.0, frame_drop=True))
    cross_rows = reconciler.ingest_row("cam2", _row(20, 1_000_010.0))

    assert cross_rows[0]["pairing_gap_us_excluded"] is True
    assert cross_rows[0]["pairing_gap_us_exclude_reason"] == "frame_drop"


def test_matches_using_each_camera_own_row_role_when_master_is_stream_b():
    # A camera's own row uses "stream_a"/"stream_b" keys depending on which
    # of ITS two picks this stream identity happens to be - the master's
    # role and the slave's role are independent and don't have to match.
    spec = _spec(master_row_role="stream_b", slave_row_role="stream_a")
    reconciler = CrossCameraReconciler([spec])

    reconciler.ingest_row("cam1", _row(10, 1_000_000.0, role="stream_b"))
    cross_rows = reconciler.ingest_row("cam2", _row(20, 1_000_005.0, role="stream_a"))

    assert len(cross_rows) == 1
    assert cross_rows[0]["pairing_gap_us"] == -5.0
