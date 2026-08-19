"""Cross-camera (master-vs-slave) HW TS Latency AND Optical Sync
reconciliation for the multi-camera sync test, plus a Global TS Latency
metric using RealSense's GLOBAL_TIME-domain timestamp.

Deliberately does NOT touch engine.session_engine/engine.test_session/
engine.acquisition_loop - each configured camera keeps running its own
existing, unmodified SessionEngineThread/TestSession/AcquisitionLoop,
exactly as a single-camera run does today. This module only consumes the
already-existing row_ready dict shape (engine.test_session.TestSession.
process_pair's own row) from however many cameras are running
concurrently: the "{role}_ts_us"/"{role}_global_ts_us"/"{role}_frame_drop"
keys drive the HW TS Latency and Global TS Latency metrics (both reusing
engine.metrics.PairingGapMetric completely unmodified, on two independent
instances per pair-spec), and the "{role}_last_led"/"position_gap_ms_excluded"/
"position_gap_ms_exclude_reason" keys - folded into each row by
engine.metrics.PositionGapMetric's own MetricResult.extra - drive the
third, Optical Sync metric (engine.metrics.compute_position_gap, reused on
the SAME already-matched pair, no second matching pass) - see
docs/superpowers's multi-camera design doc's "Design detail" section 1.

No Qt, no pyrealsense2 - pure Python, fully unit-testable with fake row
dicts, same layering convention as engine.test_session/engine.metrics.
"""

from dataclasses import dataclass

from engine.metrics import FramePairSample, PairingGapMetric, compute_position_gap


@dataclass
class CrossCameraPairSpec:
    """One master-vs-slave, one-stream-identity comparison to reconcile.
    A rig with N slaves and/or multiple shared stream identities has one
    of these per (slave, identity) combination - see engine.streams.
    stream_slug for how `stream_identity` is derived upstream; this module
    just takes it as an opaque matching key, decoupled from pyrealsense2."""
    master_camera_id: str
    slave_camera_id: str
    stream_identity: str
    master_row_role: str  # "stream_a" or "stream_b" - which field on the MASTER's own row
    slave_row_role: str   # "stream_a" or "stream_b" - which field on the SLAVE's own row
    pairing_gap_metric: object  # engine.metrics.PairingGapMetric, HW-ts-based, offset-corrected
    global_ts_gap_metric: object  # engine.metrics.PairingGapMetric, global-ts-based, NEVER offset-corrected
    # Master's own num_leds/switch_time_ms - authoritative for the cross-camera
    # Optical Sync circular wraparound math and unit conversion (same "master's
    # config wins" reasoning already used elsewhere in this project). The
    # slave's own configured values are never read here.
    num_leds: int
    switch_time_ms: float


def build_cross_camera_pair_specs(camera_specs, outlier_threshold_us):
    """Builds one CrossCameraPairSpec per (slave, shared stream identity)
    pair against the single designated master, from a rig's camera specs -
    duck-typed on camera_id/is_master/stream_identities
    ({"stream_a": "infrared1", "stream_b": "color"}, one entry per stream
    that camera's own wizard flow configured), so this works with either
    engine.multi_camera_session's real CameraSessionSpec or a lightweight
    test fake. A slave missing an identity the master has just produces no
    pair for that identity - no error, same "heterogeneous sensor setups
    are fine" requirement CrossCameraReconciler itself follows. Every
    returned spec gets its OWN PairingGapMetric instance (never shared
    across pairs, matching how each intra-camera test already gets its own
    metric instances today). Raises ValueError if exactly one master isn't
    designated - loud failure instead of silently building nothing or
    picking an arbitrary one, matching this project's "abort with a clear
    error, not a silent partial run" convention (see e.g. main_window.py's
    settings.yaml validation).

    Pairs ANY shared stream identity, infrared or color - a genlock slave's
    color/RGB stream genuinely hardware-synchronizes fine as long as its
    resolution stays within the confirmed-safe ceiling for that camera
    model (a real USB-bandwidth limit, not a hardware/firmware block - see
    tools/genlock_diag/diag_genlock_quality_test.py). Enforcing that
    resolution ceiling is gui/main_window.py's job
    (_slave_genlock_color_resolution_conflicts), not this function's -
    this module only reconciles whatever streams the operator configured,
    it doesn't second-guess whether they're safe."""
    masters = [spec for spec in camera_specs if spec.is_master]
    if len(masters) != 1:
        raise ValueError(
            "Exactly one camera must be designated master, found {}".format(len(masters))
        )
    master = masters[0]

    pair_specs = []
    for slave in camera_specs:
        if slave is master:
            continue
        shared_identities = set(master.stream_identities.values()) & set(slave.stream_identities.values())
        for identity in sorted(shared_identities):
            master_row_role = next(role for role, ident in master.stream_identities.items() if ident == identity)
            slave_row_role = next(role for role, ident in slave.stream_identities.items() if ident == identity)
            pair_specs.append(CrossCameraPairSpec(
                master_camera_id=master.camera_id,
                slave_camera_id=slave.camera_id,
                stream_identity=identity,
                master_row_role=master_row_role,
                slave_row_role=slave_row_role,
                pairing_gap_metric=PairingGapMetric(outlier_threshold_us=outlier_threshold_us),
                global_ts_gap_metric=PairingGapMetric(outlier_threshold_us=outlier_threshold_us),
                num_leds=master.num_leds,
                switch_time_ms=master.switch_time_ms,
            ))
    return pair_specs


class _PendingBuffer:
    """A small buffer of one camera's not-yet-matched rows for one side of
    one CrossCameraPairSpec. Bounded (oldest dropped first) since an
    unmatched row eventually stops being a plausible match for anything -
    same "don't grow forever" reasoning as gui.widgets.live_plot.LivePlot's
    bounded deques."""

    def __init__(self, max_len):
        self._items = []  # [(ts_us, row), ...]
        self._max_len = max_len

    def push(self, ts_us, row):
        self._items.append((ts_us, row))
        if len(self._items) > self._max_len:
            self._items.pop(0)

    def pop_nearest(self, ts_us, max_gap_us):
        """Removes and returns (matched_ts_us, row) for the buffered entry
        nearest ts_us, if within max_gap_us - or None, leaving the buffer
        untouched, if the nearest candidate is still too far away (or the
        buffer is empty). Explicit exclusion rather than a forced/misleading
        match, matching this project's established convention (outlier
        thresholds, frame-drop flags, warmup exclusion) of never silently
        connecting unrelated data. Returns the matched ts_us too (not just
        the row) so CrossCameraReconciler can learn a pair's constant
        calibration offset from it - see that class's own docstring."""
        if not self._items:
            return None
        best_index = min(range(len(self._items)), key=lambda i: abs(self._items[i][0] - ts_us))
        best_ts, best_row = self._items[best_index]
        if abs(best_ts - ts_us) > max_gap_us:
            return None
        del self._items[best_index]
        return best_ts, best_row


class CrossCameraReconciler:
    """Called with every camera's row_ready row, from every configured
    camera (master and slaves alike) - buffering is symmetric, since the
    two AcquisitionLoops run on independent threads at independent
    cadences, either side's row can legitimately arrive first.

    Real-hardware finding (this project's own multi-camera genlock
    investigation - see tools/genlock_diag/diag_genlock_quality_test.py):
    genlock stabilizes the PHASE/RATE between two devices' independent HW
    clocks (~10us jitter once genuinely locked) but does NOT align their
    absolute starting epochs - each device's own frame_timestamp counter
    resets near zero at its own pipeline.start() call, so two genuinely-
    genlocked devices' raw timestamps still differ by an arbitrary, but
    perfectly STABLE, constant offset (measured on real hardware: anywhere
    from ~2.6s to ~13.3s across different runs). Further real-hardware
    finding: even that "stable" offset turned out to drift slowly over long
    runs (measured: ~40us over 50s) - small, but real, and silently baked
    into the reported HW TS Latency number as if it were genuine physical
    latency.

    RealSense's GLOBAL_TIME-domain timestamp (frame.get_timestamp(),
    periodically re-corrected against the HOST's own clock rather than each
    device's free-running local counter - see engine.streams.
    _read_global_ts_us) is directly comparable across two independent
    devices with no per-device epoch to bridge. So MATCHING (the join) now
    uses global timestamps, with a plain, uniform max_match_gap_us window
    from the very first row for a given spec - no more unbounded-first-
    search calibration branch, since global ts needs no calibration at all.

    "HW TS Latency" (pairing_gap_us) keeps its EXACT prior meaning: raw HW
    ts still carries its own arbitrary per-device epoch, so it still needs
    a one-time-learned offset (the first match for a spec defines it,
    reported as 0.0) subtracted before diffing - this is now a small
    reporting step in _build_cross_row rather than a pre-match concern.
    "Global TS Latency" (global_ts_gap_us) is the plain, NEVER offset-
    corrected difference between the two sides' global timestamps for the
    same matched pair - correcting it would defeat its whole purpose as an
    independent, drift-free check on HW TS Latency: if global time behaves
    as expected, this number stays near zero with no drift, directly
    comparable pair-for-pair against its HW-ts counterpart, which may not."""

    def __init__(self, pair_specs, buffer_seconds=1.0, max_match_gap_us=50_000.0, fps_hint=30.0):
        self._pair_specs = pair_specs
        self._max_match_gap_us = max_match_gap_us
        buffer_len = max(1, int(fps_hint * buffer_seconds))
        self._pair_counter = 0

        # Every spec gets its OWN pair of buffers (master-side, slave-side),
        # indexed by position in pair_specs - NOT shared across specs, even
        # when two specs share the same master or the same stream identity,
        # so one master's row can independently match against every slave
        # it's compared against without any cross-spec interference.
        self._master_buffers = [_PendingBuffer(buffer_len) for _ in pair_specs]
        self._slave_buffers = [_PendingBuffer(buffer_len) for _ in pair_specs]

        # Per-spec learned HW-ts offset (slave_hw_ts - master_hw_ts at the
        # moment of that spec's first match) - None until then. Matching
        # itself no longer needs this (it uses global ts, which needs no
        # calibration) - this is purely a reporting concern for
        # pairing_gap_us ("HW TS Latency"), computed lazily in
        # _build_cross_row.
        self._hw_offset_us = [None] * len(pair_specs)

        self._specs_by_camera = {}
        for index, spec in enumerate(pair_specs):
            self._specs_by_camera.setdefault(spec.master_camera_id, []).append((index, spec, "master"))
            self._specs_by_camera.setdefault(spec.slave_camera_id, []).append((index, spec, "slave"))

    def ingest_row(self, camera_id, row):
        cross_rows = []
        for index, spec, side in self._specs_by_camera.get(camera_id, []):
            if side == "master":
                cross_row = self._ingest_side(
                    row, ts_role=spec.master_row_role,
                    own_buffer=self._master_buffers[index],
                    other_buffer=self._slave_buffers[index],
                    build=lambda match: self._build_cross_row(index, spec, row, match),
                )
            else:
                cross_row = self._ingest_side(
                    row, ts_role=spec.slave_row_role,
                    own_buffer=self._slave_buffers[index],
                    other_buffer=self._master_buffers[index],
                    build=lambda match: self._build_cross_row(index, spec, match, row),
                )
            if cross_row is not None:
                cross_rows.append(cross_row)
        return cross_rows

    def _ingest_side(self, row, ts_role, own_buffer, other_buffer, build):
        ts_us = row.get(f"{ts_role}_global_ts_us")
        if ts_us is None:
            return None

        # A plain, uniform tight-window search from the very first row for
        # this spec - global timestamps from two genlocked, global-time-
        # enabled devices are directly comparable with no per-device epoch
        # to bridge, so unlike the old HW-ts design, there is no separate
        # "unbounded first match" branch needed here at all.
        match = other_buffer.pop_nearest(ts_us, self._max_match_gap_us)
        if match is None:
            own_buffer.push(ts_us, row)
            return None
        _, matched_row = match
        return build(matched_row)

    def _build_cross_row(self, index, spec, master_row, slave_row):
        self._pair_counter += 1
        master_hw_ts = master_row[f"{spec.master_row_role}_ts_us"]
        slave_hw_ts = slave_row[f"{spec.slave_row_role}_ts_us"]
        master_global_ts = master_row[f"{spec.master_row_role}_global_ts_us"]
        slave_global_ts = slave_row[f"{spec.slave_row_role}_global_ts_us"]
        master_frame_drop = master_row.get(f"{spec.master_row_role}_frame_drop", False)
        slave_frame_drop = slave_row.get(f"{spec.slave_row_role}_frame_drop", False)

        # HW TS Latency keeps its exact prior meaning: raw HW ts still
        # carries an arbitrary per-device epoch, so it still needs a
        # one-time-learned offset, subtracted before diffing - this is now
        # a small reporting step here rather than a pre-match concern (see
        # class docstring).
        hw_offset_us = self._hw_offset_us[index]
        if hw_offset_us is None:
            hw_offset_us = slave_hw_ts - master_hw_ts
            self._hw_offset_us[index] = hw_offset_us

        hw_sample = FramePairSample(
            pair_index=self._pair_counter,
            stream_a_ts_us=master_hw_ts,
            stream_b_ts_us=slave_hw_ts - hw_offset_us,
            stream_a_frame_drop=master_frame_drop,
            stream_b_frame_drop=slave_frame_drop,
        )
        hw_result = spec.pairing_gap_metric.update(hw_sample)

        # Global TS Latency: the plain, NEVER offset-corrected difference -
        # global timestamps are directly comparable already; correcting
        # this one would defeat its whole purpose as an independent,
        # drift-free check on HW TS Latency.
        global_sample = FramePairSample(
            pair_index=self._pair_counter,
            stream_a_ts_us=master_global_ts,
            stream_b_ts_us=slave_global_ts,
            stream_a_frame_drop=master_frame_drop,
            stream_b_frame_drop=slave_frame_drop,
        )
        global_result = spec.global_ts_gap_metric.update(global_sample)

        position_gap_ms, position_gap_excluded, position_gap_exclude_reason = _compute_cross_position_gap(
            spec, master_row, slave_row, master_frame_drop, slave_frame_drop,
        )
        # Explicit key names, NOT hw_result.name/global_result.name - both
        # results come from PairingGapMetric instances, whose .name is
        # always the class-level "pairing_gap_us" regardless of which
        # instance produced it; using .name for both would silently make
        # the second write clobber the first under the same dict key.
        return {
            "pair_index": hw_sample.pair_index,
            "master_camera_id": spec.master_camera_id,
            "slave_camera_id": spec.slave_camera_id,
            "stream_identity": spec.stream_identity,
            "master_pair_index": master_row.get("pair_index"),
            "slave_pair_index": slave_row.get("pair_index"),
            "master_ts_us": master_hw_ts,  # RAW, unadjusted - for CSV/debugging transparency
            "slave_ts_us": slave_hw_ts,    # RAW, unadjusted
            "pairing_gap_us": hw_result.value,
            "pairing_gap_us_excluded": hw_result.excluded,
            "pairing_gap_us_exclude_reason": hw_result.exclude_reason,
            "global_ts_gap_us": global_result.value,
            "global_ts_gap_us_excluded": global_result.excluded,
            "global_ts_gap_us_exclude_reason": global_result.exclude_reason,
            "position_gap_ms": position_gap_ms,
            "position_gap_ms_excluded": position_gap_excluded,
            "position_gap_ms_exclude_reason": position_gap_exclude_reason,
        }


def _compute_cross_position_gap(spec, master_row, slave_row, master_frame_drop, slave_frame_drop):
    """Cross-camera Optical Sync value for one already-matched pair - reuses
    the SAME matched (master_row, slave_row) the HW-timestamp reconciler
    already found, no second matching pass. LED-index availability is
    checked first (a "miss" - no clear on-LED detected by one or both
    cameras that frame - is the only case with no computable value at
    all); once a value IS computable, frame drop is reported as the
    exclusion reason with the value still attached (mirroring
    engine.metrics.PositionGapMetric's own frame_drop/warmup exclusions,
    which likewise keep a real value), then each camera's OWN
    already-computed intra-camera position_gap_ms_excluded/exclude_reason
    (no_led_data/miss/warmup) is reused as a final catch-all - no new
    detection logic invented. Master's own num_leds/switch_time_ms (see
    CrossCameraPairSpec) are authoritative for the circular wraparound math
    and unit conversion - the slave's own configured values are never read
    or validated here."""
    master_led = master_row.get(f"{spec.master_row_role}_last_led")
    slave_led = slave_row.get(f"{spec.slave_row_role}_last_led")
    if master_led is None or slave_led is None:
        return None, True, "miss"

    gap_ms = compute_position_gap(master_led, slave_led, spec.num_leds) * spec.switch_time_ms

    if master_frame_drop or slave_frame_drop:
        return gap_ms, True, "frame_drop"
    if master_row.get("position_gap_ms_excluded"):
        return None, True, master_row.get("position_gap_ms_exclude_reason")
    if slave_row.get("position_gap_ms_excluded"):
        return None, True, slave_row.get("position_gap_ms_exclude_reason")
    return gap_ms, False, None
