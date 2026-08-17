"""Cross-camera (master-vs-slave) HW TS Latency reconciliation for the
multi-camera sync test.

Deliberately does NOT touch engine.session_engine/engine.test_session/
engine.acquisition_loop - each configured camera keeps running its own
existing, unmodified SessionEngineThread/TestSession/AcquisitionLoop,
exactly as a single-camera run does today. This module only consumes the
already-existing row_ready dict shape (engine.test_session.TestSession.
process_pair's own row: "{role}_ts_us"/"{role}_frame_drop" keys) from
however many cameras are running concurrently, and reuses
engine.metrics.PairingGapMetric completely unmodified to compute the new
cross-camera metric - see docs/superpowers's multi-camera design doc's
"Design detail" section 1.

No Qt, no pyrealsense2 - pure Python, fully unit-testable with fake row
dicts, same layering convention as engine.test_session/engine.metrics.
"""

from dataclasses import dataclass

from engine.metrics import FramePairSample, PairingGapMetric


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
    pairing_gap_metric: object  # engine.metrics.PairingGapMetric, one instance per pair
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
    from ~2.6s to ~13.3s across different runs, varying with how long each
    pipeline actually took to start relative to the other). A fixed
    max_match_gap_us (default 50ms) could never bridge that on its own -
    the true corresponding pair is always orders of magnitude further away
    than any window small enough to also reject genuinely wrong pairs.

    So each pair CALIBRATES this constant offset once, from whichever
    correspondence it can establish first (an unbounded nearest-match - no
    window size could safely assume the offset's scale ahead of time), then
    matches and reports every later row relative to that learned baseline
    with the normal tight window. The first matched pair for a given
    identity IS the calibration - it always reports pairing_gap_us == 0.0
    by construction (it defines the baseline, it doesn't measure anything
    yet); every pair after that reports the genuine residual (how much the
    two devices' clocks have diverged since calibration), which is what
    this metric actually means. Without this correction, PairingGapMetric's
    own unmodified outlier-exclusion (built for INTRA-camera sub-millisecond
    gaps) would flag every cross-camera row as an outlier anyway, given the
    raw multi-second difference - so this isn't just a matching fix, it's
    required for the metric to report anything meaningful at all.

    Calibrated once per reconciler lifetime (i.e. once per Live Session
    run), not periodically re-verified - real data showed the offset stays
    effectively perfectly stable for a full 20s recording once established;
    add recalibration later only if real hardware ever shows drift."""

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

        # Per-spec learned calibration offset (slave_ts - master_ts at the
        # moment of calibration) - None until that spec's first match.
        self._offset_us = [None] * len(pair_specs)

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
                    index=index, is_master_side=True,
                    build=lambda match, offset_us: self._build_cross_row(spec, row, match, offset_us),
                )
            else:
                cross_row = self._ingest_side(
                    row, ts_role=spec.slave_row_role,
                    own_buffer=self._slave_buffers[index],
                    other_buffer=self._master_buffers[index],
                    index=index, is_master_side=False,
                    build=lambda match, offset_us: self._build_cross_row(spec, match, row, offset_us),
                )
            if cross_row is not None:
                cross_rows.append(cross_row)
        return cross_rows

    def _ingest_side(self, row, ts_role, own_buffer, other_buffer, index, is_master_side, build):
        ts_us = row.get(f"{ts_role}_ts_us")
        if ts_us is None:
            return None

        offset_us = self._offset_us[index]
        if offset_us is None:
            # Not yet calibrated for this pair - accept the CLOSEST
            # available candidate regardless of distance (see class
            # docstring: the real constant offset can be arbitrarily large,
            # so no window size is safe to assume ahead of time). This
            # match establishes the pair's baseline; every later match uses
            # the normal tight window instead.
            match = other_buffer.pop_nearest(ts_us, float("inf"))
            if match is None:
                own_buffer.push(ts_us, row)
                return None
            matched_ts, matched_row = match
            offset_us = (ts_us - matched_ts) if not is_master_side else (matched_ts - ts_us)
            self._offset_us[index] = offset_us
            return build(matched_row, offset_us)

        # Already calibrated - shift the query into the OTHER side's own
        # raw timestamp space before searching, using the real tight window.
        query_ts = (ts_us + offset_us) if is_master_side else (ts_us - offset_us)
        match = other_buffer.pop_nearest(query_ts, self._max_match_gap_us)
        if match is None:
            own_buffer.push(ts_us, row)
            return None
        _, matched_row = match
        return build(matched_row, offset_us)

    def _build_cross_row(self, spec, master_row, slave_row, offset_us):
        self._pair_counter += 1
        master_ts_us = master_row[f"{spec.master_row_role}_ts_us"]
        slave_ts_us = slave_row[f"{spec.slave_row_role}_ts_us"]
        sample = FramePairSample(
            pair_index=self._pair_counter,
            # Offset-corrected: removes the arbitrary, per-pipeline-session
            # constant learned at calibration, so PairingGapMetric's own
            # unmodified gap = stream_a_ts_us - stream_b_ts_us math reports
            # the genuine residual latency - see class docstring.
            stream_a_ts_us=master_ts_us,
            stream_b_ts_us=slave_ts_us - offset_us,
            stream_a_frame_drop=master_row.get(f"{spec.master_row_role}_frame_drop", False),
            stream_b_frame_drop=slave_row.get(f"{spec.slave_row_role}_frame_drop", False),
        )
        result = spec.pairing_gap_metric.update(sample)
        return {
            "pair_index": sample.pair_index,
            "master_camera_id": spec.master_camera_id,
            "slave_camera_id": spec.slave_camera_id,
            "stream_identity": spec.stream_identity,
            "master_pair_index": master_row.get("pair_index"),
            "slave_pair_index": slave_row.get("pair_index"),
            "master_ts_us": master_ts_us,  # RAW, unadjusted - for CSV/debugging transparency
            "slave_ts_us": slave_ts_us,    # RAW, unadjusted
            result.name: result.value,
            f"{result.name}_excluded": result.excluded,
            f"{result.name}_exclude_reason": result.exclude_reason,
        }
