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
    five attributes, so tests don't need the full per-camera session config
    (device_serial, session_engine_kwargs, etc.)."""

    def __init__(self, camera_id, is_master, stream_identities, num_leds=10, switch_time_ms=1.0):
        self.camera_id = camera_id
        self.is_master = is_master
        self.stream_identities = stream_identities  # e.g. {"stream_a": "infrared1", "stream_b": "color"}
        self.num_leds = num_leds
        self.switch_time_ms = switch_time_ms


def _spec(master_camera_id="cam1", slave_camera_id="cam2", stream_identity="infrared1",
          master_row_role="stream_a", slave_row_role="stream_a", outlier_threshold_us=100_000,
          num_leds=10, switch_time_ms=1.0):
    return CrossCameraPairSpec(
        master_camera_id=master_camera_id,
        slave_camera_id=slave_camera_id,
        stream_identity=stream_identity,
        master_row_role=master_row_role,
        slave_row_role=slave_row_role,
        pairing_gap_metric=PairingGapMetric(outlier_threshold_us=outlier_threshold_us),
        num_leds=num_leds,
        switch_time_ms=switch_time_ms,
    )


def _row(pair_index, ts_us, role="stream_a", frame_drop=False, last_led=None,
         position_gap_ms_excluded=False, position_gap_ms_exclude_reason=None):
    row = {
        "pair_index": pair_index,
        f"{role}_ts_us": ts_us,
        f"{role}_frame_drop": frame_drop,
        "position_gap_ms_excluded": position_gap_ms_excluded,
        "position_gap_ms_exclude_reason": position_gap_ms_exclude_reason,
    }
    if last_led is not None:
        row[f"{role}_last_led"] = last_led
    return row


# --- Real-hardware finding (this project's own multi-camera genlock
# investigation - see tools/genlock_diag/diag_genlock_quality_test.py):
# genlock stabilizes the PHASE/RATE between two devices' independent HW
# clocks (~10us jitter) but does NOT align their absolute starting epochs -
# each device's own frame_timestamp counter resets near zero at its own
# pipeline.start() call, so two genuinely-genlocked devices' raw timestamps
# still differ by an arbitrary, but perfectly STABLE, constant offset
# (measured on real hardware: anywhere from ~2.6s to ~13.3s across
# different runs). CrossCameraReconciler therefore CALIBRATES this offset
# once per pair, from whichever correspondence it can establish first (an
# unbounded nearest-match, since no window size could safely assume the
# constant's scale ahead of time), then matches/reports every later row
# relative to that learned baseline with the normal tight window. The
# FIRST matched pair for any given identity IS the calibration - it always
# reports pairing_gap_us == 0.0 by construction, since it defines the
# baseline rather than measuring anything yet. ---

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
    assert row["pairing_gap_us"] == 0.0  # first-ever pair - defines the baseline, see comment above
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
    assert cross_rows[0]["pairing_gap_us"] == 0.0


def test_calibration_handles_a_large_arbitrary_constant_offset():
    # Proves calibration isn't limited to small/already-close values - a
    # ~49-SECOND raw gap (matching the scale real hardware actually showed)
    # still produces a match, since calibration deliberately ignores
    # max_match_gap_us for the first-ever pair.
    spec = _spec()
    reconciler = CrossCameraReconciler([spec])

    reconciler.ingest_row("cam1", _row(10, 1_000_000.0))
    cross_rows = reconciler.ingest_row("cam2", _row(20, 50_000_000.0))

    assert len(cross_rows) == 1
    assert cross_rows[0]["pairing_gap_us"] == 0.0


def test_second_pair_reports_the_residual_relative_to_the_learned_offset():
    # Calibrate once with a large constant offset, then confirm a SECOND
    # pair reports only the small genuine residual (how much the two
    # devices' clocks diverged since calibration) - not the raw multi-
    # second difference, and not zero either (that was only true for the
    # calibration pair itself).
    spec = _spec()
    reconciler = CrossCameraReconciler([spec])
    reconciler.ingest_row("cam1", _row(10, 1_000_000.0))
    reconciler.ingest_row("cam2", _row(20, 50_000_000.0))  # offset learned: 49_000_000

    reconciler.ingest_row("cam1", _row(11, 1_033_000.0))  # master advances by 33_000
    cross_rows = reconciler.ingest_row("cam2", _row(21, 50_033_010.0))  # slave advances by 33_010

    assert len(cross_rows) == 1
    assert cross_rows[0]["pairing_gap_us"] == -10.0  # 10us genuine residual, not ~49_000_000


def test_no_cross_row_when_no_counterpart_within_max_match_gap():
    # Explicit exclusion, not a forced/misleading match - matches this
    # project's existing convention (outlier thresholds, frame-drop flags,
    # warmup exclusion) of never silently connecting unrelated frames.
    # Calibration's own first-pair match is deliberately unbounded (see
    # comment above), so this must be checked AFTER a pair is already
    # calibrated, not as the very first interaction.
    spec = _spec()
    reconciler = CrossCameraReconciler([spec], max_match_gap_us=50_000)
    reconciler.ingest_row("cam1", _row(10, 1_000_000.0))
    reconciler.ingest_row("cam2", _row(20, 1_000_010.0))  # calibrates: offset = 10

    reconciler.ingest_row("cam1", _row(11, 2_000_000.0))
    cross_rows = reconciler.ingest_row("cam2", _row(21, 2_500_010.0))  # 500ms away, once offset-adjusted

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
    # slave streams - including each spec learning its OWN calibration
    # offset independently, not sharing one.
    spec_vs_slave1 = _spec(slave_camera_id="cam2")
    spec_vs_slave2 = _spec(slave_camera_id="cam3")
    reconciler = CrossCameraReconciler([spec_vs_slave1, spec_vs_slave2])

    # Round 1: both specs calibrate off cam1's single first row.
    reconciler.ingest_row("cam2", _row(1, 1_000_010.0))
    reconciler.ingest_row("cam3", _row(1, 1_000_020.0))
    first_cross_rows = reconciler.ingest_row("cam1", _row(5, 1_000_000.0))

    assert len(first_cross_rows) == 2
    assert all(row["pairing_gap_us"] == 0.0 for row in first_cross_rows)  # both calibrating, not measuring yet

    # Round 2: cam1 advances once (feeds both specs identically); each
    # slave advances by a DIFFERENT amount, proving each spec's own learned
    # offset (10 for cam2, 20 for cam3) is applied independently.
    reconciler.ingest_row("cam1", _row(6, 1_100_000.0))
    second_cross_rows = []
    second_cross_rows += reconciler.ingest_row("cam2", _row(2, 1_100_015.0))  # residual: -5
    second_cross_rows += reconciler.ingest_row("cam3", _row(2, 1_100_028.0))  # residual: -8

    by_slave = {row["slave_camera_id"]: row for row in second_cross_rows}
    assert by_slave["cam2"]["pairing_gap_us"] == -5.0
    assert by_slave["cam3"]["pairing_gap_us"] == -8.0


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
    assert cross_rows[0]["pairing_gap_us"] == 0.0  # first-ever pair for this spec - calibration


# --- Cross-camera Optical Sync: reuses the SAME matched (master_row,
# slave_row) pair the HW-timestamp reconciler already finds - no second
# match, no new stateful metric. Mirrors PairingGapMetric's own exclusion
# priority (frame drop first), then reuses each camera's own already-
# computed position_gap_ms_excluded/exclude_reason for detection failures. ---

def test_matched_pair_computes_cross_camera_position_gap():
    spec = _spec(num_leds=4, switch_time_ms=2.0)
    reconciler = CrossCameraReconciler([spec])

    # Calibration pair (see class docstring) - HW TS offset learned here,
    # not asserted on in this test.
    reconciler.ingest_row("cam1", _row(1, 1_000_000.0, last_led=0))
    cross_rows = reconciler.ingest_row("cam2", _row(1, 1_000_010.0, last_led=0))

    assert cross_rows[0]["position_gap_ms"] == 0.0
    assert cross_rows[0]["position_gap_ms_excluded"] is False
    assert cross_rows[0]["position_gap_ms_exclude_reason"] is None


def test_matched_pair_uses_masters_own_num_leds_and_switch_time_ms_for_wraparound():
    spec = _spec(num_leds=4, switch_time_ms=2.0)
    reconciler = CrossCameraReconciler([spec])
    reconciler.ingest_row("cam1", _row(1, 1_000_000.0, last_led=0))
    reconciler.ingest_row("cam2", _row(1, 1_000_010.0, last_led=0))

    reconciler.ingest_row("cam1", _row(2, 1_100_000.0, last_led=3))
    cross_rows = reconciler.ingest_row("cam2", _row(2, 1_100_010.0, last_led=0))

    # compute_position_gap(3, 0, 4): diff=3 > half(2.0) -> diff -= 4 -> -1;
    # -1 * switch_time_ms(2.0) == -2.0.
    assert cross_rows[0]["position_gap_ms"] == -2.0


def test_cross_position_gap_excluded_on_frame_drop():
    spec = _spec()
    reconciler = CrossCameraReconciler([spec])
    reconciler.ingest_row("cam1", _row(1, 1_000_000.0, last_led=0))
    reconciler.ingest_row("cam2", _row(1, 1_000_010.0, last_led=0))

    reconciler.ingest_row("cam1", _row(2, 1_100_000.0, last_led=1, frame_drop=True))
    cross_rows = reconciler.ingest_row("cam2", _row(2, 1_100_010.0, last_led=0))

    # frame_drop keeps the real computed value (mirrors PositionGapMetric's
    # own frame_drop/warmup exclusions) - _spec()'s defaults are
    # num_leds=10, switch_time_ms=1.0, so compute_position_gap(1, 0, 10)
    # == 1 (no wraparound, 1 <= half of 10), * 1.0 == 1.0ms.
    assert cross_rows[0]["position_gap_ms"] == 1.0
    assert cross_rows[0]["position_gap_ms_excluded"] is True
    assert cross_rows[0]["position_gap_ms_exclude_reason"] == "frame_drop"


def test_cross_position_gap_reuses_a_cameras_own_miss_exclusion():
    spec = _spec()
    reconciler = CrossCameraReconciler([spec])
    reconciler.ingest_row("cam1", _row(1, 1_000_000.0, last_led=0))
    reconciler.ingest_row("cam2", _row(1, 1_000_010.0, last_led=0))

    # Master's own intra-camera PositionGapMetric already excluded this row
    # as a "miss" (e.g. no clear on-LED detected that frame) - reused
    # verbatim, no new detection logic invented here.
    reconciler.ingest_row("cam1", _row(
        2, 1_100_000.0, last_led=None, position_gap_ms_excluded=True, position_gap_ms_exclude_reason="miss",
    ))
    cross_rows = reconciler.ingest_row("cam2", _row(2, 1_100_010.0, last_led=0))

    assert cross_rows[0]["position_gap_ms"] is None
    assert cross_rows[0]["position_gap_ms_excluded"] is True
    assert cross_rows[0]["position_gap_ms_exclude_reason"] == "miss"


def test_cross_position_gap_reuses_a_cameras_own_warmup_exclusion_even_though_computable():
    # Unlike frame_drop (which now keeps its computed value), warmup is
    # reused from each camera's own intra-camera exclusion and still
    # discards the value - an accepted, unchanged trade-off (LED indices
    # are always resolved before PositionGapMetric's own warmup check
    # fires, so this branch is reachable even when both LEDs ARE detected).
    spec = _spec()
    reconciler = CrossCameraReconciler([spec])
    reconciler.ingest_row("cam1", _row(1, 1_000_000.0, last_led=0))
    reconciler.ingest_row("cam2", _row(1, 1_000_010.0, last_led=0))

    reconciler.ingest_row("cam1", _row(
        2, 1_100_000.0, last_led=1, position_gap_ms_excluded=True, position_gap_ms_exclude_reason="warmup",
    ))
    cross_rows = reconciler.ingest_row("cam2", _row(2, 1_100_010.0, last_led=0))

    assert cross_rows[0]["position_gap_ms"] is None
    assert cross_rows[0]["position_gap_ms_excluded"] is True
    assert cross_rows[0]["position_gap_ms_exclude_reason"] == "warmup"


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

    assert len(specs) == 4  # 2 slaves x 2 shared identities
    pairs = {(s.slave_camera_id, s.stream_identity) for s in specs}
    assert pairs == {("cam2", "infrared1"), ("cam2", "color"), ("cam3", "infrared1"), ("cam3", "color")}
    for s in specs:
        assert s.master_camera_id == "cam1"
        assert s.master_row_role == "stream_a" if s.stream_identity == "infrared1" else s.master_row_role == "stream_b"


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
    master = _CamSpec("cam1", is_master=True,
                       stream_identities={"stream_a": "infrared1", "stream_b": "color"})
    slave = _CamSpec("cam2", is_master=False,
                      stream_identities={"stream_a": "infrared1", "stream_b": "color"})

    specs = build_cross_camera_pair_specs([master, slave], outlier_threshold_us=100_000)

    assert specs[0].pairing_gap_metric is not specs[1].pairing_gap_metric


def test_build_cross_camera_pair_specs_uses_masters_num_leds_and_switch_time_ms():
    master = _CamSpec("cam1", True, {"stream_a": "infrared1"}, num_leds=20, switch_time_ms=2.5)
    slave = _CamSpec("cam2", False, {"stream_a": "infrared1"}, num_leds=999, switch_time_ms=999.0)

    specs = build_cross_camera_pair_specs([master, slave], outlier_threshold_us=100_000)

    assert len(specs) == 1
    assert specs[0].num_leds == 20
    assert specs[0].switch_time_ms == 2.5
