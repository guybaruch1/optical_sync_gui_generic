"""Pure Python, no Qt/hardware - matches independent per-camera row_ready
streams by nearest RealSense GLOBAL_TIME-domain timestamp (not raw HW ts -
see engine/cross_camera_reconciler.py's own module docstring for why) and
reuses PairingGapMetric unmodified to produce two cross-camera
(master-vs-slave) metrics: the original HW TS Latency (still offset-
corrected against each device's own arbitrary epoch) and the new Global TS
Latency (never offset-corrected - a drift-free check on the former). See
docs/superpowers's multi-camera design doc's "Design detail" section 1 for
the full rationale."""

import numpy as np
import pytest

from engine.cross_camera_reconciler import (
    CrossCameraPairSpec, CrossCameraReconciler, build_cross_camera_pair_specs,
)
from engine.metrics import FramePairSample, PairingGapMetric, PositionGapMetric
from engine.test_session import TestSession, TestSessionConfig


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
        global_ts_gap_metric=PairingGapMetric(outlier_threshold_us=outlier_threshold_us),
        num_leds=num_leds,
        switch_time_ms=switch_time_ms,
    )


def _row(pair_index, ts_us, role="stream_a", frame_drop=False, last_led=None,
         position_gap_ms_excluded=False, position_gap_ms_exclude_reason=None,
         global_ts_us=None):
    row = {
        "pair_index": pair_index,
        f"{role}_ts_us": ts_us,
        # Defaults to the SAME value as the raw HW ts when not given
        # explicitly - most tests below don't care about the two clocks
        # diverging; only the calibration-specific tests set global_ts_us
        # to something genuinely different from ts_us.
        f"{role}_global_ts_us": ts_us if global_ts_us is None else global_ts_us,
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
# pipeline.start() call, so two genuinely-genlocked devices' raw HW
# timestamps still differ by an arbitrary, but perfectly STABLE, constant
# offset (measured on real hardware: anywhere from ~2.6s to ~13.3s across
# different runs).
#
# Further real-hardware finding: even that "stable" HW-ts offset turned out
# to drift slowly over long runs (measured: ~40us over 50s) - small, but
# real, and baked silently into the reported HW TS Latency number as if it
# were genuine physical latency. RealSense's GLOBAL_TIME-domain timestamp
# (periodically re-corrected against the HOST's own clock, not each
# device's free-running local counter) is directly comparable across
# devices with no per-device epoch to bridge - so CrossCameraReconciler's
# JOIN (matching) now uses global ts, with a plain, uniform tight window
# from the very first row (no more unbounded-first-search calibration
# dance). "HW TS Latency" (pairing_gap_us) keeps its EXACT prior meaning -
# still computed from raw HW ts, still offset-corrected once per spec, now
# as a small reporting step in _build_cross_row rather than a pre-match
# concern. The new "Global TS Latency" (global_ts_gap_us) is the plain,
# NEVER offset-corrected difference between the two sides' global
# timestamps for the same matched pair - directly comparable against HW TS
# Latency pair-for-pair, which is the whole point: if global time behaves
# as expected, this number stays near zero with no drift, unlike its HW-ts
# counterpart. ---

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
    assert row["pairing_gap_us"] == 0.0  # first-ever pair - defines the HW-ts offset baseline
    assert row["pairing_gap_us_excluded"] is False
    # global_ts_us defaults to ts_us in _row(), so global_ts_gap_us here is
    # the plain (uncorrected) -50.0, NOT 0.0 - it never gets a baseline.
    assert row["global_ts_gap_us"] == -50.0
    assert row["global_ts_gap_us_excluded"] is False


def test_cross_row_carries_the_raw_global_timestamps_too():
    spec = _spec()
    reconciler = CrossCameraReconciler([spec])

    reconciler.ingest_row("cam1", _row(10, 1_000_000.0, global_ts_us=2_000_000.0))
    cross_rows = reconciler.ingest_row("cam2", _row(20, 1_000_050.0, global_ts_us=2_000_060.0))

    assert cross_rows[0]["master_global_ts_us"] == 2_000_000.0
    assert cross_rows[0]["slave_global_ts_us"] == 2_000_060.0


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
    assert cross_rows[0]["global_ts_gap_us"] == -50.0


def test_matching_depends_on_global_ts_not_raw_hw_ts():
    # There's no more "first match is unbounded" calibration exemption for
    # matching itself - global ts needs no calibration, so a plain tight
    # window applies from the very first row. Raw HW ts still carries its
    # own arbitrary per-device epoch (a ~49-second gap here, matching the
    # scale real hardware showed) - proving a match still succeeds anyway
    # confirms matching is now driven ENTIRELY by global ts, indifferent to
    # how far apart the raw HW ts values are.
    spec = _spec()
    reconciler = CrossCameraReconciler([spec])

    reconciler.ingest_row("cam1", _row(10, 1_000_000.0, global_ts_us=5_000_000.0))
    cross_rows = reconciler.ingest_row("cam2", _row(20, 50_000_000.0, global_ts_us=5_000_010.0))

    assert len(cross_rows) == 1
    assert cross_rows[0]["pairing_gap_us"] == 0.0  # HW-ts offset still calibrates on this first match
    assert cross_rows[0]["global_ts_gap_us"] == -10.0  # plain diff, no calibration


def test_first_match_for_a_spec_also_respects_the_tight_window():
    # Unlike the old design, there is no special "first match is unbounded"
    # exemption anymore - a candidate outside max_match_gap_us in GLOBAL-TS
    # space is rejected even on a spec's very first interaction.
    spec = _spec()
    reconciler = CrossCameraReconciler([spec], max_match_gap_us=50_000)
    reconciler.ingest_row("cam1", _row(10, 1_000_000.0, global_ts_us=2_000_000.0))
    cross_rows = reconciler.ingest_row("cam2", _row(20, 1_000_010.0, global_ts_us=2_500_010.0))  # 500ms away

    assert cross_rows == []


def test_second_pair_reports_the_hw_ts_residual_relative_to_the_learned_offset():
    # HW TS Latency still needs its own one-time-learned offset (raw HW ts
    # still carries an arbitrary per-device epoch) - now computed in
    # _build_cross_row, decoupled from matching (which uses global ts,
    # kept close together throughout so every row still matches).
    spec = _spec()
    reconciler = CrossCameraReconciler([spec])
    reconciler.ingest_row("cam1", _row(10, 1_000_000.0, global_ts_us=2_000_000.0))
    reconciler.ingest_row("cam2", _row(20, 50_000_000.0, global_ts_us=2_000_010.0))  # HW-ts offset learned: 49_000_000

    reconciler.ingest_row("cam1", _row(11, 1_033_000.0, global_ts_us=2_033_000.0))
    cross_rows = reconciler.ingest_row("cam2", _row(21, 50_033_010.0, global_ts_us=2_033_012.0))

    assert len(cross_rows) == 1
    assert cross_rows[0]["pairing_gap_us"] == -10.0  # 10us genuine HW-clock residual, not ~49_000_000
    # global_ts_gap_us is the plain diff on BOTH pairs, never offset-corrected:
    # pair 1: 2_000_000 - 2_000_010 = -10.0; pair 2: 2_033_000 - 2_033_012 = -12.0.
    assert cross_rows[0]["global_ts_gap_us"] == -12.0


def test_global_ts_gap_never_gets_offset_corrected_even_across_many_pairs():
    # Explicit, dedicated proof that global_ts_gap_us is ALWAYS the plain,
    # uncorrected difference - correcting it would defeat its whole purpose
    # as an independent check on whether global time genuinely stays
    # comparable with no drift.
    spec = _spec()
    reconciler = CrossCameraReconciler([spec])
    reconciler.ingest_row("cam1", _row(10, 1_000_000.0, global_ts_us=2_000_000.0))
    reconciler.ingest_row("cam2", _row(20, 1_000_010.0, global_ts_us=2_000_007.0))

    reconciler.ingest_row("cam1", _row(11, 1_033_000.0, global_ts_us=2_033_000.0))
    cross_rows = reconciler.ingest_row("cam2", _row(21, 1_033_010.0, global_ts_us=2_033_009.0))

    assert cross_rows[0]["global_ts_gap_us"] == -9.0  # 2_033_000 - 2_033_009
    assert cross_rows[0]["global_ts_gap_us_excluded"] is False


def test_no_cross_row_when_no_counterpart_within_max_match_gap():
    # Explicit exclusion, not a forced/misleading match - matches this
    # project's existing convention (outlier thresholds, frame-drop flags,
    # warmup exclusion) of never silently connecting unrelated frames.
    spec = _spec()
    reconciler = CrossCameraReconciler([spec], max_match_gap_us=50_000)
    reconciler.ingest_row("cam1", _row(10, 1_000_000.0, global_ts_us=2_000_000.0))
    reconciler.ingest_row("cam2", _row(20, 1_000_010.0, global_ts_us=2_000_010.0))  # HW-ts offset learned: 10

    reconciler.ingest_row("cam1", _row(11, 2_000_000.0, global_ts_us=3_000_000.0))
    cross_rows = reconciler.ingest_row("cam2", _row(21, 2_500_010.0, global_ts_us=3_500_010.0))  # 500ms away

    assert cross_rows == []


def test_match_diagnostics_reports_matched_and_unmatched_counts():
    # Each ingest_row call for a registered camera increments exactly one of
    # matched_count/unmatched_count for that spec, based on whether THAT
    # call produced a cross-row - not one increment per logical pair. So a
    # normal matched pair contributes ONE unmatched increment (the first
    # side's row, buffered with nothing yet to match) and ONE matched
    # increment (the second side's row, which finds it) - see
    # match_diagnostics's own docstring for why this exists: a real-hardware
    # run whose matching silently never succeeds should still leave behind
    # data explaining why.
    spec = _spec()
    reconciler = CrossCameraReconciler([spec], max_match_gap_us=50_000)

    reconciler.ingest_row("cam1", _row(10, 1_000_000.0))  # buffered, no counterpart yet -> unmatched
    matched = reconciler.ingest_row("cam2", _row(20, 1_000_010.0))  # finds cam1's row -> matched
    assert len(matched) == 1

    # A further master row with no counterpart ever arriving -> unmatched again.
    unmatched = reconciler.ingest_row("cam1", _row(11, 5_000_000.0))
    assert unmatched == []

    diagnostics = reconciler.match_diagnostics()
    assert diagnostics == [{
        "slave_camera_id": "cam2", "stream_identity": "infrared1",
        "matched_count": 1, "unmatched_count": 2,
    }]


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
    # slave streams - including each spec learning its OWN HW-ts offset
    # independently, not sharing one, and each spec's own global_ts_gap_us
    # computed independently too (global_ts_us defaults to ts_us here, so
    # global_ts_gap_us differs from pairing_gap_us precisely because only
    # the latter gets offset-corrected).
    spec_vs_slave1 = _spec(slave_camera_id="cam2")
    spec_vs_slave2 = _spec(slave_camera_id="cam3")
    reconciler = CrossCameraReconciler([spec_vs_slave1, spec_vs_slave2])

    # Round 1: both specs calibrate/match off cam1's single first row.
    reconciler.ingest_row("cam2", _row(1, 1_000_010.0))
    reconciler.ingest_row("cam3", _row(1, 1_000_020.0))
    first_cross_rows = reconciler.ingest_row("cam1", _row(5, 1_000_000.0))

    assert len(first_cross_rows) == 2
    assert all(row["pairing_gap_us"] == 0.0 for row in first_cross_rows)  # both calibrating, not measuring yet
    by_slave_first = {row["slave_camera_id"]: row for row in first_cross_rows}
    assert by_slave_first["cam2"]["global_ts_gap_us"] == -10.0  # 1_000_000 - 1_000_010
    assert by_slave_first["cam3"]["global_ts_gap_us"] == -20.0  # 1_000_000 - 1_000_020

    # Round 2: cam1 advances once (feeds both specs identically); each
    # slave advances by a DIFFERENT amount, proving each spec's own learned
    # HW-ts offset (10 for cam2, 20 for cam3) is applied independently.
    reconciler.ingest_row("cam1", _row(6, 1_100_000.0))
    second_cross_rows = []
    second_cross_rows += reconciler.ingest_row("cam2", _row(2, 1_100_015.0))  # HW-ts residual: -5
    second_cross_rows += reconciler.ingest_row("cam3", _row(2, 1_100_028.0))  # HW-ts residual: -8

    by_slave = {row["slave_camera_id"]: row for row in second_cross_rows}
    assert by_slave["cam2"]["pairing_gap_us"] == -5.0
    assert by_slave["cam3"]["pairing_gap_us"] == -8.0
    assert by_slave["cam2"]["global_ts_gap_us"] == -15.0  # 1_100_000 - 1_100_015, no offset correction
    assert by_slave["cam3"]["global_ts_gap_us"] == -28.0  # 1_100_000 - 1_100_028, no offset correction


def test_matched_cross_row_excluded_when_either_side_dropped_a_frame():
    # Reuses PairingGapMetric's own existing frame-drop-takes-priority
    # exclusion logic completely unmodified, for BOTH the HW-ts and the
    # global-ts metric instances.
    spec = _spec()
    reconciler = CrossCameraReconciler([spec])

    reconciler.ingest_row("cam1", _row(10, 1_000_000.0, frame_drop=True))
    cross_rows = reconciler.ingest_row("cam2", _row(20, 1_000_010.0))

    assert cross_rows[0]["pairing_gap_us_excluded"] is True
    assert cross_rows[0]["pairing_gap_us_exclude_reason"] == "frame_drop"
    assert cross_rows[0]["global_ts_gap_us_excluded"] is True
    assert cross_rows[0]["global_ts_gap_us_exclude_reason"] == "frame_drop"


def test_matches_using_each_camera_own_row_role_when_master_is_stream_b():
    # A camera's own row uses "stream_a"/"stream_b" keys depending on which
    # of ITS two picks this stream identity happens to be - the master's
    # role and the slave's role are independent and don't have to match.
    spec = _spec(master_row_role="stream_b", slave_row_role="stream_a")
    reconciler = CrossCameraReconciler([spec])

    reconciler.ingest_row("cam1", _row(10, 1_000_000.0, role="stream_b"))
    cross_rows = reconciler.ingest_row("cam2", _row(20, 1_000_005.0, role="stream_a"))

    assert len(cross_rows) == 1
    assert cross_rows[0]["pairing_gap_us"] == 0.0  # first-ever pair for this spec - HW-ts calibration


# --- Cross-camera Optical Sync: reuses the SAME matched (master_row,
# slave_row) pair the reconciler already finds - no second match, no new
# stateful metric. Mirrors PairingGapMetric's own exclusion priority (frame
# drop first), then reuses each camera's own already-computed
# position_gap_ms_excluded/exclude_reason for detection failures.
# Unaffected by the matching-key change - all timestamps here stay close
# together via _row()'s own defaults. ---

def test_matched_pair_computes_cross_camera_position_gap():
    spec = _spec(num_leds=4, switch_time_ms=2.0)
    reconciler = CrossCameraReconciler([spec])

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
# up to 2 slaves on the hub page. ---

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


def test_build_specs_gives_each_pair_its_own_metric_instances():
    master = _CamSpec("cam1", is_master=True,
                       stream_identities={"stream_a": "infrared1", "stream_b": "color"})
    slave = _CamSpec("cam2", is_master=False,
                      stream_identities={"stream_a": "infrared1", "stream_b": "color"})

    specs = build_cross_camera_pair_specs([master, slave], outlier_threshold_us=100_000)

    assert specs[0].pairing_gap_metric is not specs[1].pairing_gap_metric
    assert specs[0].global_ts_gap_metric is not specs[1].global_ts_gap_metric
    # Also distinct from this SAME spec's own pairing_gap_metric - two
    # independent metric instances per spec, not one reused for both.
    assert specs[0].global_ts_gap_metric is not specs[0].pairing_gap_metric


def test_build_cross_camera_pair_specs_uses_masters_num_leds_and_switch_time_ms():
    master = _CamSpec("cam1", True, {"stream_a": "infrared1"}, num_leds=20, switch_time_ms=2.5)
    slave = _CamSpec("cam2", False, {"stream_a": "infrared1"}, num_leds=999, switch_time_ms=999.0)

    specs = build_cross_camera_pair_specs([master, slave], outlier_threshold_us=100_000)

    assert len(specs) == 1
    assert specs[0].num_leds == 20
    assert specs[0].switch_time_ms == 2.5


# --- Key-name binding: every test above hand-builds rows via _row(...,
# last_led=...), duplicating the "{role}_last_led" key-name literal rather
# than obtaining it from real production code. This test instead drives the
# REAL engine.metrics.PositionGapMetric through a REAL engine.test_session.
# TestSession (whose process_pair folds MetricResult.extra into the row) so
# a future rename of PositionGapMetric's extra keys - or of what TestSession
# folds into the row - would break this test loudly instead of leaving
# _compute_cross_position_gap silently reporting "miss" forever. ---

def test_real_position_gap_metric_key_names_connect_end_to_end_through_test_session():
    threshold = np.full(4, 150.0)
    metric = PositionGapMetric(
        stream_a_threshold=threshold, stream_b_threshold=threshold, num_leds=4,
        switch_time_ms=1.0, warmup_pairs_to_skip=0,
    )
    session = TestSession(TestSessionConfig(metrics=[metric]))
    session.start()

    row1 = session.process_pair(FramePairSample(
        pair_index=0, stream_a_ts_us=1_000_000.0, stream_b_ts_us=1_000_000.0,
        stream_a_global_ts_us=2_000_000.0, stream_b_global_ts_us=2_000_000.0,
        stream_a_bright=np.array([50.0, 50.0, 200.0, 50.0]),
        stream_b_bright=np.array([50.0, 200.0, 50.0, 50.0]),
    ))
    row2 = session.process_pair(FramePairSample(
        pair_index=1, stream_a_ts_us=1_000_050.0, stream_b_ts_us=1_000_050.0,
        stream_a_global_ts_us=2_000_050.0, stream_b_global_ts_us=2_000_050.0,
        stream_a_bright=np.array([50.0, 50.0, 50.0, 200.0]),
        stream_b_bright=np.array([200.0, 50.0, 50.0, 50.0]),
    ))

    spec = _spec(master_row_role="stream_a", slave_row_role="stream_a", num_leds=4, switch_time_ms=1.0)
    reconciler = CrossCameraReconciler([spec])
    assert reconciler.ingest_row("cam1", row1) == []  # buffered, awaiting the slave's row
    cross_rows = reconciler.ingest_row("cam2", row2)

    assert len(cross_rows) == 1
    assert cross_rows[0]["position_gap_ms"] is not None
