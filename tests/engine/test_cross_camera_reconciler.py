"""Pure Python, no Qt/hardware - matches independent per-camera row_ready
streams by nearest timestamp and reuses PairingGapMetric unmodified to
produce the new cross-camera (master-vs-slave) HW TS Latency metric for a
multi-camera sync test. See docs/superpowers's multi-camera design doc's
"Design detail" section 1 for the full rationale."""

import pytest

from engine.cross_camera_reconciler import (
    CrossCameraPairSpec, CrossCameraReconciler, build_cross_camera_pair_specs,
)
from engine.metrics import PairingGapMetric


class _CamSpec:
    """Minimal duck-typed stand-in for engine.multi_camera_session's real
    CameraSessionSpec - build_cross_camera_pair_specs only ever reads these
    three attributes, so tests don't need the full per-camera session
    config (device_serial, session_engine_kwargs, etc.)."""

    def __init__(self, camera_id, is_master, stream_identities):
        self.camera_id = camera_id
        self.is_master = is_master
        self.stream_identities = stream_identities  # e.g. {"stream_a": "infrared1", "stream_b": "color"}


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


# --- build_cross_camera_pair_specs: pure spec-building from a rig's camera
# configs, no Qt/hardware. Consumed by engine.multi_camera_session to wire
# up a CrossCameraReconciler once the operator has designated a master and
# up to 2 slaves on the new hub page. ---

def test_build_specs_one_master_two_slaves_shared_identities():
    master = _CamSpec("cam1", is_master=True,
                       stream_identities={"stream_a": "infrared1", "stream_b": "color"})
    slave1 = _CamSpec("cam2", is_master=False,
                       stream_identities={"stream_a": "infrared1", "stream_b": "color"})
    slave2 = _CamSpec("cam3", is_master=False,
                       stream_identities={"stream_a": "infrared1", "stream_b": "color"})

    specs = build_cross_camera_pair_specs([master, slave1, slave2], outlier_threshold_us=100_000)

    # 2 slaves x 1 shared identity each - "color" is shared too but never
    # produces a pair (see test_build_specs_never_builds_a_pair_for_a_color_
    # identity_even_if_shared below for why: confirmed via real-hardware
    # testing that a genlock slave's color sensor cannot produce frames at
    # all, so a cross-camera color pair would never receive real slave data).
    assert len(specs) == 2
    pairs = {(s.slave_camera_id, s.stream_identity) for s in specs}
    assert pairs == {("cam2", "infrared1"), ("cam3", "infrared1")}
    for s in specs:
        assert s.master_camera_id == "cam1"
        assert s.master_row_role == "stream_a"


def test_build_specs_never_builds_a_pair_for_a_color_identity_even_if_shared():
    master = _CamSpec("cam1", is_master=True,
                       stream_identities={"stream_a": "infrared1", "stream_b": "color"})
    slave = _CamSpec("cam2", is_master=False,
                      stream_identities={"stream_a": "infrared1", "stream_b": "color"})

    specs = build_cross_camera_pair_specs([master, slave], outlier_threshold_us=100_000)

    assert all(s.stream_identity != "color" for s in specs)
    assert all(s.stream_identity.startswith("infrared") for s in specs)


def test_build_specs_pairs_every_shared_infrared_identity_not_just_one():
    # Proves the fix filters by prefix, not by hardcoding a single literal -
    # a camera with two IR sensors sharing both with the master still gets
    # both paired.
    master = _CamSpec("cam1", is_master=True,
                       stream_identities={"stream_a": "infrared1", "stream_b": "infrared2"})
    slave = _CamSpec("cam2", is_master=False,
                      stream_identities={"stream_a": "infrared1", "stream_b": "infrared2"})

    specs = build_cross_camera_pair_specs([master, slave], outlier_threshold_us=100_000)

    assert {s.stream_identity for s in specs} == {"infrared1", "infrared2"}


def test_build_specs_skips_identity_the_slave_does_not_have():
    # Heterogeneous per-camera sensor setups must be supported - a camera
    # missing a given identity just means no pair for it, no error.
    master = _CamSpec("cam1", is_master=True,
                       stream_identities={"stream_a": "infrared1", "stream_b": "color"})
    slave = _CamSpec("cam2", is_master=False, stream_identities={"stream_a": "infrared1"})  # no color

    specs = build_cross_camera_pair_specs([master, slave], outlier_threshold_us=100_000)

    assert len(specs) == 1
    assert specs[0].stream_identity == "infrared1"


def test_build_specs_uses_each_camera_own_row_role_independently():
    # The master's "infrared1" might live under a different stream_a/b slot
    # than the slave's own "infrared1" - each camera's role mapping is its
    # own, matched only by the shared identity string.
    master = _CamSpec("cam1", is_master=True, stream_identities={"stream_b": "infrared1"})
    slave = _CamSpec("cam2", is_master=False, stream_identities={"stream_a": "infrared1"})

    specs = build_cross_camera_pair_specs([master, slave], outlier_threshold_us=100_000)

    assert len(specs) == 1
    assert specs[0].master_row_role == "stream_b"
    assert specs[0].slave_row_role == "stream_a"


def test_build_specs_returns_empty_list_with_no_slaves():
    # Single-camera case (N=1) degrades gracefully - no cross-camera pairs,
    # no error.
    master = _CamSpec("cam1", is_master=True, stream_identities={"stream_a": "infrared1"})

    assert build_cross_camera_pair_specs([master], outlier_threshold_us=100_000) == []


def test_build_specs_raises_when_no_master_designated():
    slave = _CamSpec("cam2", is_master=False, stream_identities={"stream_a": "infrared1"})

    with pytest.raises(ValueError):
        build_cross_camera_pair_specs([slave], outlier_threshold_us=100_000)


def test_build_specs_raises_when_more_than_one_master_designated():
    master1 = _CamSpec("cam1", is_master=True, stream_identities={"stream_a": "infrared1"})
    master2 = _CamSpec("cam2", is_master=True, stream_identities={"stream_a": "infrared1"})

    with pytest.raises(ValueError):
        build_cross_camera_pair_specs([master1, master2], outlier_threshold_us=100_000)


def test_build_specs_gives_each_pair_its_own_pairing_gap_metric_instance():
    # Two shared INFRARED identities (not infrared+color - color never
    # produces a pair at all, see the tests above) so this still exercises
    # 2 real specs.
    master = _CamSpec("cam1", is_master=True,
                       stream_identities={"stream_a": "infrared1", "stream_b": "infrared2"})
    slave = _CamSpec("cam2", is_master=False,
                      stream_identities={"stream_a": "infrared1", "stream_b": "infrared2"})

    specs = build_cross_camera_pair_specs([master, slave], outlier_threshold_us=100_000)

    assert specs[0].pairing_gap_metric is not specs[1].pairing_gap_metric
