# Cross-Camera Optical Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add cross-camera Optical Sync (`position_gap_ms`) as a second cross-camera metric alongside the existing cross-camera HW TS Latency (`pairing_gap_us`), move both into their own new tab (placed first) in the multi-camera live session page, and fix a real pre-existing GUI-thread performance anti-pattern in the cross-camera plotting code along the way.

**Architecture:** `engine/metrics.py`'s `PositionGapMetric.update()` starts exposing the detected on-LED index per stream via `MetricResult.extra` (already-existing mechanism, rides straight into `row_ready` unmodified). `engine/cross_camera_reconciler.py`'s `_build_cross_row` reuses the SAME already-matched (master_row, slave_row) pair the HW-timestamp reconciler finds and computes a second value from it via the existing free functions `compute_position_gap`/nothing new stateful - no second matching pass, no new metric instance. `CrossCameraPairSpec` gains the master camera's own `num_leds`/`switch_time_ms` (master's config is authoritative, mirroring the existing "master wins" precedent). The GUI page moves the cross-camera section into a new "Cross-Camera Sync" tab (first position) with two graph+stats rows, and fixes `_on_cross_pair_ready` to stop calling `add_point` on every unthrottled row (an existing anti-pattern CLAUDE.md documents as having already caused a real GUI freeze for intra-camera plotting) - all `add_point` calls move to the already-throttled `_on_cross_stats_ready`.

**Tech Stack:** Python 3.10+/3.13, PySide6, pyqtgraph (via `gui/widgets/live_plot.py`'s `LivePlot`), matplotlib `Agg` backend (`domain/plot_export.py`), pytest (`QT_QPA_PLATFORM=offscreen` for widget tests, shared `qapp` fixture).

## Global Constraints

- Reuse the same matched (master_row, slave_row) pair the HW-timestamp reconciler already finds - no second buffering/matching pass, no new stateful metric class for the cross-camera value.
- No `num_leds` mismatch validation between master and slave - master's own `num_leds`/`switch_time_ms` are authoritative, the slave's own configured values are never read or validated against them (same "master's config wins" precedent `switch_time_ms` already sets elsewhere in this codebase).
- Exclusion mirrors the existing cross-camera HW TS Latency structure: frame drop first (either camera's own `stream_a_frame_drop`/`stream_b_frame_drop`, whichever row role applies), then reuse each camera's own already-computed `position_gap_ms_excluded`/`position_gap_ms_exclude_reason` for detection failures (`no_led_data`/`miss`/`warmup`) - no new detection-failure logic invented.
- Applies identically regardless of single-panel or dual-panel topology - no panel-topology special-casing anywhere in this feature.
- `row_ready`/unthrottled callbacks must stay O(1) (CLAUDE.md convention) - actual `LivePlot.add_point()` calls happen only on the throttled `stats_ready` cadence, never on `row_ready`/`cross_pair_ready`.
- New cross-camera tab is placed FIRST in the tab widget (before per-camera tabs) - cross-camera Optical Sync is the operator's primary test.
- `domain/csv_export.py`'s `export_cross_camera_csv` needs NO code changes - it already discovers `fieldnames` dynamically from whatever keys exist across all `cross_rows`, so the new `position_gap_ms`/`position_gap_ms_excluded`/`position_gap_ms_exclude_reason` keys appear in the CSV automatically once `_build_cross_row` starts producing them.

---

### Task 1: `PositionGapMetric` exposes the detected LED index via `extra`

**Files:**
- Modify: `engine/metrics.py:174-198` (`PositionGapMetric.update`)
- Test: `tests/engine/test_metrics.py`

**Interfaces:**
- Consumes: nothing new - `find_last_on_led`/`compute_position_gap` (`engine/metrics.py`, unchanged), `MetricResult.extra` (`engine/metrics.py:36`, already exists, already folded into rows by `engine/test_session.py:64-65`'s `if result.extra: row.update(result.extra)`).
- Produces: `MetricResult.extra == {"stream_a_last_led": int | None, "stream_b_last_led": int | None}` on every `PositionGapMetric.update()` call whose sample carried brightness data (i.e. every branch except the early `no_led_data` return). Task 3 reads these off `row_ready` dicts as `f"{role}_last_led"`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/engine/test_metrics.py` (near the existing `PositionGapMetric` tests, e.g. after `test_position_gap_metric_tracks_last_on_masks_for_debug_snapshots`):

```python
def test_position_gap_metric_exposes_detected_led_index_via_extra():
    threshold = np.full(4, 150.0)
    metric = PositionGapMetric(
        stream_a_threshold=threshold, stream_b_threshold=threshold, num_leds=4,
        switch_time_ms=1.0, warmup_pairs_to_skip=0,
    )
    stream_a_bright = np.array([50.0, 200.0, 50.0, 50.0])
    stream_b_bright = np.array([50.0, 50.0, 200.0, 50.0])

    result = metric.update(FramePairSample(0, 0.0, 0.0, stream_a_bright, stream_b_bright))

    assert result.extra == {"stream_a_last_led": 1, "stream_b_last_led": 2}


def test_position_gap_metric_extra_reflects_a_miss_on_one_side():
    threshold = np.full(4, 150.0)
    metric = PositionGapMetric(
        stream_a_threshold=threshold, stream_b_threshold=threshold, num_leds=4,
        switch_time_ms=1.0, warmup_pairs_to_skip=0,
    )
    stream_a_bright = np.array([50.0, 200.0, 50.0, 50.0])
    stream_b_bright = np.full(4, 50.0)  # nothing on

    result = metric.update(FramePairSample(0, 0.0, 0.0, stream_a_bright, stream_b_bright))

    assert result.exclude_reason == "miss"
    assert result.extra == {"stream_a_last_led": 1, "stream_b_last_led": None}


def test_position_gap_metric_extra_is_none_with_no_led_data_at_all():
    threshold = np.full(4, 150.0)
    metric = PositionGapMetric(
        stream_a_threshold=threshold, stream_b_threshold=threshold, num_leds=4,
        switch_time_ms=1.0, warmup_pairs_to_skip=0,
    )

    result = metric.update(FramePairSample(0, 0.0, 0.0, None, None))

    assert result.exclude_reason == "no_led_data"
    assert result.extra is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_metrics.py -k "extra" -v`
Expected: FAIL - `AssertionError: None == {...}` (current `update()` never sets `extra` on any branch).

- [ ] **Step 3: Implement**

Replace `engine/metrics.py:174-198`'s `PositionGapMetric.update` body:

```python
    def update(self, sample: FramePairSample) -> MetricResult:
        self._pair_count += 1
        is_warmup = self._pair_count <= self.warmup_pairs_to_skip

        if sample.stream_a_bright is None or sample.stream_b_bright is None:
            return MetricResult(name=self.name, value=None, excluded=True, exclude_reason="no_led_data")

        stream_a_on = sample.stream_a_bright > self.stream_a_threshold
        stream_b_on = sample.stream_b_bright > self.stream_b_threshold
        self.last_stream_a_on_mask = stream_a_on
        self.last_stream_b_on_mask = stream_b_on
        stream_a_last, _ = find_last_on_led(stream_a_on)
        stream_b_last, _ = find_last_on_led(stream_b_on)
        # Detected on-LED index per stream (or None), threaded into row_ready
        # via MetricResult.extra - an immutable per-row snapshot, safe to
        # buffer/match later (unlike last_stream_a_on_mask above, a LIVE,
        # mutable attribute already overwritten by later frames by the time a
        # cross-camera reconciler match is found for a buffered row).
        # Consumed by engine.cross_camera_reconciler's cross-camera Optical
        # Sync computation.
        extra = {"stream_a_last_led": stream_a_last, "stream_b_last_led": stream_b_last}

        if stream_a_last is None or stream_b_last is None:
            return MetricResult(name=self.name, value=None, excluded=True, exclude_reason="miss", extra=extra)

        diff = compute_position_gap(stream_a_last, stream_b_last, self.num_leds)
        gap_ms = diff * self.switch_time_ms

        if sample.stream_a_frame_drop or sample.stream_b_frame_drop:
            return MetricResult(name=self.name, value=gap_ms, excluded=True, exclude_reason="frame_drop", extra=extra)
        if is_warmup:
            return MetricResult(name=self.name, value=gap_ms, excluded=True, exclude_reason="warmup", extra=extra)
        return MetricResult(name=self.name, value=gap_ms, excluded=False, exclude_reason=None, extra=extra)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_metrics.py -v`
Expected: PASS (all tests in the file, including the 3 new ones and every pre-existing `PositionGapMetric` test).

- [ ] **Step 5: Commit**

```bash
git add engine/metrics.py tests/engine/test_metrics.py
git commit -m "feat: PositionGapMetric exposes detected LED index via MetricResult.extra"
```

---

### Task 2: `CrossCameraPairSpec` carries master's `num_leds`/`switch_time_ms`

**Files:**
- Modify: `engine/cross_camera_reconciler.py:24-89` (`CrossCameraPairSpec`, `build_cross_camera_pair_specs`)
- Test: `tests/engine/test_cross_camera_reconciler.py:15-36` (`_CamSpec`, `_spec` helpers)

**Interfaces:**
- Consumes: nothing new.
- Produces: `CrossCameraPairSpec.num_leds: int`, `CrossCameraPairSpec.switch_time_ms: float` - populated from the MASTER's own camera spec (`master.num_leds`/`master.switch_time_ms`), never the slave's. The duck-typed `camera_specs` contract (`CameraSessionSpec`, `_IdentitySpec`, test fakes) must all carry `num_leds`/`switch_time_ms` attributes from this task onward - Task 4 and Task 5 supply these on the real production types. Task 3 reads `spec.num_leds`/`spec.switch_time_ms` off this dataclass.

- [ ] **Step 1: Write the failing test**

Extend `tests/engine/test_cross_camera_reconciler.py:15-36`'s helpers:

```python
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
```

Add a new test, e.g. right after `build_cross_camera_pair_specs`'s existing tests in that file:

```python
def test_build_cross_camera_pair_specs_uses_masters_num_leds_and_switch_time_ms():
    master = _CamSpec("cam1", True, {"stream_a": "infrared1"}, num_leds=20, switch_time_ms=2.5)
    slave = _CamSpec("cam2", False, {"stream_a": "infrared1"}, num_leds=999, switch_time_ms=999.0)

    specs = build_cross_camera_pair_specs([master, slave], outlier_threshold_us=100_000)

    assert len(specs) == 1
    assert specs[0].num_leds == 20
    assert specs[0].switch_time_ms == 2.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_cross_camera_reconciler.py -k masters_num_leds -v`
Expected: FAIL with `TypeError: CrossCameraPairSpec.__init__() got an unexpected keyword argument 'num_leds'`.

- [ ] **Step 3: Implement**

In `engine/cross_camera_reconciler.py`, extend the dataclass (line 24-36):

```python
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
```

Update `build_cross_camera_pair_specs` (line 81-88) to populate the new fields:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_cross_camera_reconciler.py -v`
Expected: PASS (all tests in the file - the extended `_CamSpec`/`_spec` helpers keep every pre-existing call site working via their new defaulted params).

- [ ] **Step 5: Commit**

```bash
git add engine/cross_camera_reconciler.py tests/engine/test_cross_camera_reconciler.py
git commit -m "feat: CrossCameraPairSpec carries master's num_leds/switch_time_ms"
```

---

### Task 3: `_build_cross_row` computes the cross-camera Optical Sync value

**Files:**
- Modify: `engine/cross_camera_reconciler.py` (imports; new `_compute_cross_position_gap` function; `_build_cross_row`)
- Test: `tests/engine/test_cross_camera_reconciler.py` (`_row` helper; new tests)

**Interfaces:**
- Consumes: `CrossCameraPairSpec.num_leds`/`switch_time_ms` (Task 2); row dict keys `f"{role}_last_led"` (Task 1's `extra`, folded into `row_ready` by `engine/test_session.py`) and `f"{role}_frame_drop"`/`"position_gap_ms_excluded"`/`"position_gap_ms_exclude_reason"` (already-existing intra-camera row keys, from that camera's own `PositionGapMetric`).
- Produces: every cross-row dict returned by `CrossCameraReconciler.ingest_row` gains `"position_gap_ms"` (float or `None`), `"position_gap_ms_excluded"` (bool), `"position_gap_ms_exclude_reason"` (str or `None`) alongside the existing `pairing_gap_us`/`pairing_gap_us_excluded`/`pairing_gap_us_exclude_reason` fields. Task 7's GUI code and `domain/csv_export.py`/`domain/plot_export.py` (Task 6) read these bare-name keys.

- [ ] **Step 1: Write the failing tests**

Extend `tests/engine/test_cross_camera_reconciler.py:39-45`'s `_row` helper:

```python
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
```

Add new tests, e.g. in a new section near the end of the file:

```python
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

    assert cross_rows[0]["position_gap_ms"] is None
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_cross_camera_reconciler.py -k "position_gap" -v`
Expected: FAIL with `KeyError: 'position_gap_ms'` (current `_build_cross_row` never sets it).

- [ ] **Step 3: Implement**

In `engine/cross_camera_reconciler.py`, update the import (line 21):

```python
from engine.metrics import FramePairSample, PairingGapMetric, compute_position_gap
```

Add a new module-level function (place it right before `_build_cross_row`, i.e. just before line 246):

```python
def _compute_cross_position_gap(spec, master_row, slave_row, master_frame_drop, slave_frame_drop):
    """Cross-camera Optical Sync value for one already-matched pair - reuses
    the SAME matched (master_row, slave_row) the HW-timestamp reconciler
    already found, no second matching pass. Mirrors PairingGapMetric's own
    exclusion priority (frame drop first), then reuses each camera's OWN
    already-computed intra-camera position_gap_ms_excluded/exclude_reason
    for detection failures (no_led_data/miss/warmup) - no new detection
    logic invented. Master's own num_leds/switch_time_ms (see
    CrossCameraPairSpec) are authoritative for the circular wraparound math
    and unit conversion - the slave's own configured values are never read
    or validated here.

    Falls back to excluding as "miss" if either side's row doesn't carry a
    detected LED index at all (e.g. a hand-built row that predates this
    field) - real production rows always carry position_gap_ms_excluded
    consistently with their own f"{role}_last_led" key (both come from the
    same engine.metrics.PositionGapMetric.update() call), so this path is
    defensive, not a case real hardware rows are expected to hit."""
    if master_frame_drop or slave_frame_drop:
        return None, True, "frame_drop"
    if master_row.get("position_gap_ms_excluded"):
        return None, True, master_row.get("position_gap_ms_exclude_reason")
    if slave_row.get("position_gap_ms_excluded"):
        return None, True, slave_row.get("position_gap_ms_exclude_reason")

    master_led = master_row.get(f"{spec.master_row_role}_last_led")
    slave_led = slave_row.get(f"{spec.slave_row_role}_last_led")
    if master_led is None or slave_led is None:
        return None, True, "miss"

    diff = compute_position_gap(master_led, slave_led, spec.num_leds)
    return diff * spec.switch_time_ms, False, None
```

Replace `_build_cross_row` (lines 246-274):

```python
    def _build_cross_row(self, spec, master_row, slave_row, offset_us):
        self._pair_counter += 1
        master_ts_us = master_row[f"{spec.master_row_role}_ts_us"]
        slave_ts_us = slave_row[f"{spec.slave_row_role}_ts_us"]
        master_frame_drop = master_row.get(f"{spec.master_row_role}_frame_drop", False)
        slave_frame_drop = slave_row.get(f"{spec.slave_row_role}_frame_drop", False)
        sample = FramePairSample(
            pair_index=self._pair_counter,
            # Offset-corrected: removes the arbitrary, per-pipeline-session
            # constant learned at calibration, so PairingGapMetric's own
            # unmodified gap = stream_a_ts_us - stream_b_ts_us math reports
            # the genuine residual latency - see class docstring.
            stream_a_ts_us=master_ts_us,
            stream_b_ts_us=slave_ts_us - offset_us,
            stream_a_frame_drop=master_frame_drop,
            stream_b_frame_drop=slave_frame_drop,
        )
        result = spec.pairing_gap_metric.update(sample)
        position_gap_ms, position_gap_excluded, position_gap_exclude_reason = _compute_cross_position_gap(
            spec, master_row, slave_row, master_frame_drop, slave_frame_drop,
        )
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
            "position_gap_ms": position_gap_ms,
            "position_gap_ms_excluded": position_gap_excluded,
            "position_gap_ms_exclude_reason": position_gap_exclude_reason,
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_cross_camera_reconciler.py -v`
Expected: PASS (all tests in the file).

- [ ] **Step 5: Commit**

```bash
git add engine/cross_camera_reconciler.py tests/engine/test_cross_camera_reconciler.py
git commit -m "feat: cross-camera reconciler computes Optical Sync from the same matched pair"
```

---

### Task 4: `CameraSessionSpec` carries `num_leds`/`switch_time_ms`

**Files:**
- Modify: `engine/multi_camera_session.py:35-59` (`CameraSessionSpec`)
- Test: `tests/engine/test_multi_camera_session.py:45-56` (`_spec` helper); new test

**Interfaces:**
- Consumes: nothing new.
- Produces: `CameraSessionSpec.num_leds: int`, `CameraSessionSpec.switch_time_ms: float` - required (no default) fields, satisfying `build_cross_camera_pair_specs`'s duck-typed contract (Task 2) so `MultiCameraSessionController.__init__`'s existing `build_cross_camera_pair_specs(camera_specs, ...)` call (`engine/multi_camera_session.py:115`) works end-to-end with real specs. Task 5 populates these at construction time from the GUI page's `config` dict.

- [ ] **Step 1: Write the failing test**

Extend `tests/engine/test_multi_camera_session.py:45-56`'s `_spec` helper:

```python
def _spec(camera_id, is_master, inter_cam_sync_value=1, stream_identities=None,
          hardware_reset_before_start=False, device_serial=None, dual_panel_config=None,
          num_leds=10, switch_time_ms=1.0):
    return CameraSessionSpec(
        camera_id=camera_id,
        is_master=is_master,
        inter_cam_sync_value=inter_cam_sync_value,
        stream_identities=stream_identities or {"stream_a": "infrared1"},
        device_serial=device_serial or "{}_serial".format(camera_id),
        num_leds=num_leds,
        switch_time_ms=switch_time_ms,
        hardware_reset_before_start=hardware_reset_before_start,
        hardware_reset_settle_s=0.0,
        thread_kwargs={"dual_panel_config": dual_panel_config} if dual_panel_config is not None else {},
    )
```

Add a new test near the other `build_cross_camera_pair_specs`-adjacent controller tests in that file:

```python
def test_controller_builds_cross_camera_pair_specs_using_real_camera_session_spec_num_leds(qapp):
    specs = [
        _spec("cam1", True, num_leds=20, switch_time_ms=2.5),
        _spec("cam2", False, num_leds=999, switch_time_ms=999.0),
    ]

    controller = _controller(specs)

    assert controller._reconciler is not None
    pair_spec = controller._reconciler._pair_specs[0]
    assert pair_spec.num_leds == 20
    assert pair_spec.switch_time_ms == 2.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_multi_camera_session.py -k real_camera_session_spec_num_leds -v`
Expected: FAIL with `TypeError: CameraSessionSpec.__init__() got an unexpected keyword argument 'num_leds'`.

- [ ] **Step 3: Implement**

In `engine/multi_camera_session.py`, update `CameraSessionSpec` (lines 35-59):

```python
@dataclass
class CameraSessionSpec:
    """Everything MultiCameraSessionController needs for one configured
    camera. `thread_kwargs` is passed straight through to the thread
    factory (normally SessionEngineThread's constructor) - ctx and
    device_serial are supplied by the controller itself, and
    hardware_reset_before_start is always forced False there (see
    start_all's docstring for why), so thread_kwargs should carry
    everything else SessionEngineThread's real constructor takes
    (pick_a, pick_b, camera_controls, test_session, stream_a_xy, ...)."""
    camera_id: str
    is_master: bool
    # Raw rs.option.inter_cam_sync_mode value for THIS camera's generation
    # (D400 vs D500-series use different value schemes - see
    # engine.streams.set_inter_cam_sync_mode's own docstring). None skips
    # genlock entirely for this camera (e.g. a lone camera with nothing to
    # sync against).
    inter_cam_sync_value: "int | None"
    # {"stream_a": "infrared1", "stream_b": "color"} - this camera's own
    # engine.streams.stream_slug mapping, for cross-camera identity matching.
    stream_identities: dict
    device_serial: str
    # This camera's own num_leds/switch_time_ms - read by
    # engine.cross_camera_reconciler.build_cross_camera_pair_specs off
    # whichever spec is the designated master, for the cross-camera Optical
    # Sync computation (CrossCameraPairSpec's own docstring: master's config
    # wins, the slave's own values are never read).
    num_leds: int
    switch_time_ms: float
    hardware_reset_before_start: bool = False
    hardware_reset_settle_s: float = 8.0
    thread_kwargs: dict = field(default_factory=dict)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_multi_camera_session.py -v`
Expected: PASS (all tests in the file - every pre-existing `_spec(...)` call site keeps working via the new defaulted params).

- [ ] **Step 5: Commit**

```bash
git add engine/multi_camera_session.py tests/engine/test_multi_camera_session.py
git commit -m "feat: CameraSessionSpec carries num_leds/switch_time_ms"
```

---

### Task 5: Wire `num_leds`/`switch_time_ms` through the multi-camera live session page

**Files:**
- Modify: `gui/pages/multi_camera_live_session_page.py:68-78` (`_IdentitySpec`), `:189-192` (`_rebuild_cross_camera_section`'s `identity_specs` construction), `:283-291` (`start_all_sessions`'s `CameraSessionSpec` construction)
- Test: `tests/gui/pages/test_multi_camera_live_session_page.py`

**Interfaces:**
- Consumes: `CameraSessionSpec.num_leds`/`switch_time_ms` (Task 4); `config["num_leds"]`/`config["switch_time_ms"]` (already-existing per-camera config dict keys, already used at `multi_camera_live_session_page.py:246` to construct that camera's own `PositionGapMetric`).
- Produces: `_IdentitySpec.num_leds`/`switch_time_ms` attributes (satisfies `build_cross_camera_pair_specs`'s duck-typed contract for the page's own series-naming preview call at `_rebuild_cross_camera_section`); every `CameraSessionSpec` built in `start_all_sessions` carries its camera's real `num_leds`/`switch_time_ms`, so `MultiCameraSessionController`'s real cross-camera reconciler (Task 3/4) computes genuine Optical Sync values in a real run.

- [ ] **Step 1: Write the failing test**

Add to `tests/gui/pages/test_multi_camera_live_session_page.py`, near the other `start_all_sessions`-carries-config tests (e.g. after `test_start_all_sessions_defaults_inter_cam_sync_value_to_none_when_config_omits_it`):

```python
def test_start_all_sessions_carries_each_cameras_own_num_leds_and_switch_time_ms_into_its_spec(qapp, tmp_path):
    page, _ = _page_with_fake_threads()
    cameras = _two_cameras(tmp_path)
    cameras[0]["config"]["num_leds"] = 20
    cameras[0]["config"]["switch_time_ms"] = 3.0
    page.set_cameras(object(), cameras)

    page.start_all_sessions()

    spec = next(s for s in page._controller._camera_specs if s.camera_id == "cam1")
    assert spec.num_leds == 20
    assert spec.switch_time_ms == 3.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_multi_camera_live_session_page.py -k num_leds_and_switch_time_ms -v`
Expected: FAIL - either `AttributeError: 'CameraSessionSpec' object has no attribute 'num_leds'` (if Task 4 landed but this task hasn't) or, run standalone before Task 4, `TypeError: CameraSessionSpec.__init__() missing 2 required positional arguments`.

- [ ] **Step 3: Implement**

In `gui/pages/multi_camera_live_session_page.py`, update `_IdentitySpec` (lines 68-78):

```python
class _IdentitySpec:
    """Duck-typed stand-in for engine.multi_camera_session.CameraSessionSpec,
    carrying only the 5 attributes build_cross_camera_pair_specs actually
    reads (camera_id/is_master/stream_identities/num_leds/switch_time_ms) -
    used here purely to decide which cross-camera series to show; the real
    CameraSessionSpec list built in start_all_sessions carries everything
    else."""

    def __init__(self, camera_id, is_master, stream_identities, num_leds, switch_time_ms):
        self.camera_id = camera_id
        self.is_master = is_master
        self.stream_identities = stream_identities
        self.num_leds = num_leds
        self.switch_time_ms = switch_time_ms
```

Update `_rebuild_cross_camera_section`'s `identity_specs` construction (lines 189-192):

```python
        identity_specs = [
            _IdentitySpec(
                camera["camera_id"], camera["is_master"], _stream_identities(camera["config"]),
                num_leds=camera["config"]["num_leds"], switch_time_ms=camera["config"]["switch_time_ms"],
            )
            for camera in cameras
        ]
```

Update `start_all_sessions`'s `CameraSessionSpec` construction (lines 283-291):

```python
            camera_specs.append(CameraSessionSpec(
                camera_id=camera_id, is_master=camera["is_master"],
                inter_cam_sync_value=config.get("inter_cam_sync_value"),
                stream_identities=_stream_identities(config),
                device_serial=config["device_serial"],
                num_leds=config["num_leds"], switch_time_ms=config["switch_time_ms"],
                hardware_reset_before_start=config["hardware_reset_before_start"],
                hardware_reset_settle_s=config["hardware_reset_settle_s"],
                thread_kwargs=thread_kwargs,
            ))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_multi_camera_live_session_page.py -v`
Expected: PASS (all tests in the file).

- [ ] **Step 5: Commit**

```bash
git add gui/pages/multi_camera_live_session_page.py tests/gui/pages/test_multi_camera_live_session_page.py
git commit -m "feat: thread num_leds/switch_time_ms into multi-camera CameraSessionSpec"
```

---

### Task 6: Static cross-camera plot export gains an Optical Sync subplot

**Files:**
- Modify: `domain/plot_export.py:137-172` (`_build_cross_camera_figure`)
- Test: `tests/domain/test_plot_export.py:82-134`

**Interfaces:**
- Consumes: `position_gap_ms`/`position_gap_ms_excluded` keys on cross-row dicts (Task 3); `_to_plot_value`/`_style_axis`/`_figure_width`/`CROSS_CAMERA_COLORS`/`SURFACE`/`_FIGURE_HEIGHT` (all pre-existing, unchanged).
- Produces: `_build_cross_camera_figure(cross_rows)` returns a 2-axis figure - `fig.axes[0]` is the existing HW TS Latency subplot (unchanged behavior/tests), `fig.axes[1]` is the new Optical Sync subplot. `export_cross_camera_plot`'s signature/behavior is otherwise unchanged.

- [ ] **Step 1: Write the failing tests**

Extend `tests/domain/test_plot_export.py:82-88`'s `_cross_row` helper:

```python
def _cross_row(pair_index, slave_camera_id="cam2", stream_identity="infrared1",
                pairing_gap_us=-10.0, excluded=False,
                position_gap_ms=1.0, position_gap_ms_excluded=False):
    return {
        "pair_index": pair_index, "master_camera_id": "cam1", "slave_camera_id": slave_camera_id,
        "stream_identity": stream_identity,
        "pairing_gap_us": pairing_gap_us, "pairing_gap_us_excluded": excluded,
        "position_gap_ms": position_gap_ms, "position_gap_ms_excluded": position_gap_ms_excluded,
    }
```

Add new tests after `test_export_cross_camera_plot_nans_out_excluded_values` (line 125-134):

```python
def test_export_cross_camera_plot_draws_position_gap_on_second_axis():
    rows = [
        _cross_row(0, slave_camera_id="cam2", stream_identity="infrared1"),
        _cross_row(1, slave_camera_id="cam2", stream_identity="infrared1"),
        _cross_row(0, slave_camera_id="cam3", stream_identity="color"),
    ]

    import matplotlib.pyplot as plt
    from domain.plot_export import _build_cross_camera_figure
    fig = _build_cross_camera_figure(rows)
    lines = fig.axes[1].get_lines()

    assert len(lines) == 2
    plt.close(fig)


def test_export_cross_camera_plot_nans_out_excluded_position_gap_values():
    rows = [_cross_row(0, position_gap_ms=99.0, position_gap_ms_excluded=True)]

    import matplotlib.pyplot as plt
    from domain.plot_export import _build_cross_camera_figure
    fig = _build_cross_camera_figure(rows)
    line = fig.axes[1].get_lines()[0]

    assert math.isnan(line.get_ydata()[0])
    plt.close(fig)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/domain/test_plot_export.py -k position_gap_on_second_axis -v`
Expected: FAIL with `IndexError: list index out of range` (current figure has only one axis).

- [ ] **Step 3: Implement**

Replace `_build_cross_camera_figure` in `domain/plot_export.py` (lines 137-166):

```python
def _build_cross_camera_figure(cross_rows):
    """Two stacked subplots (sharing one x-axis, "Pair index") - HW TS
    Latency and Optical Sync each get their own y-axis, same "wildly
    different scales" reasoning _build_figure's own 3-axis split already
    uses for the intra-camera plot. Same NaN-for-excluded convention, one
    line per (slave_camera_id, stream_identity) pair rather than a fixed
    axis-per-metric layout - engine.cross_camera_reconciler's own
    pair_index is a synthetic, shared-across-all-pairs counter (not
    comparable to any one camera's own pair_index), so it's used here only
    as this plot's own x-axis, not cross-referenced against per-camera
    CSVs. Split out from export_cross_camera_plot so tests can inspect the
    plotted line data directly, same reason _build_figure is split from
    export_session_plot."""
    groups = {}
    for row in cross_rows:
        key = (row["slave_camera_id"], row["stream_identity"])
        groups.setdefault(key, []).append(row)

    fig, (pairing_ax, position_ax) = plt.subplots(
        2, 1, figsize=(_figure_width(len(cross_rows)), _FIGURE_HEIGHT), sharex=True,
    )
    fig.patch.set_facecolor(SURFACE)

    for index, key in enumerate(sorted(groups.keys())):
        pair_rows = groups[key]
        pair_indices = [row["pair_index"] for row in pair_rows]
        color = CROSS_CAMERA_COLORS[index % len(CROSS_CAMERA_COLORS)]

        pairing_values = [_to_plot_value(row.get("pairing_gap_us"), row.get("pairing_gap_us_excluded"))
                           for row in pair_rows]
        pairing_ax.plot(pair_indices, pairing_values, label="{} {}".format(*key), color=color)

        position_values = [_to_plot_value(row.get("position_gap_ms"), row.get("position_gap_ms_excluded"))
                            for row in pair_rows]
        position_ax.plot(pair_indices, position_values, label="{} {}".format(*key), color=color)

    pairing_ax.set_ylabel("Cross-camera HW TS latency (us)")
    _style_axis(pairing_ax)

    position_ax.set_ylabel("Cross-camera Optical Sync (ms)")
    position_ax.set_xlabel("Pair index")
    _style_axis(position_ax)

    fig.tight_layout()
    return fig
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/domain/test_plot_export.py -v`
Expected: PASS (all tests in the file, including the two pre-existing `fig.axes[0]`-based tests, which stay valid since `pairing_ax` is still `fig.axes[0]`).

- [ ] **Step 5: Commit**

```bash
git add domain/plot_export.py tests/domain/test_plot_export.py
git commit -m "feat: cross-camera plot export gains an Optical Sync subplot"
```

---

### Task 7: New "Cross-Camera Sync" tab (first position), two graphs, efficiency fix

**Files:**
- Modify: `gui/pages/multi_camera_live_session_page.py` (module docstring; `__init__`; `set_cameras`; `_rebuild_cross_camera_section`; `_on_cross_pair_ready`; `_on_cross_stats_ready`)
- Test: `tests/gui/pages/test_multi_camera_live_session_page.py`

**Interfaces:**
- Consumes: `latest_by_pair` dicts from `MultiCameraSessionController.cross_stats_ready` (`engine/multi_camera_session.py:73-89,241-244`, unchanged - already carries full cross-row dicts, including the new `position_gap_ms*` keys from Task 3, keyed by `(slave_camera_id, stream_identity)`); `LivePlot.add_series(name, color, display_name=None)`/`add_point(name, x, y)` (`gui/widgets/live_plot.py:67,79`); `StatsPanel.add_section_header(text)`/`add_field(key, label)`/`set_value(key, value)` (`gui/widgets/stats_panel.py:38,43,92`).
- Produces: `self.tabs` has the "Cross-Camera Sync" tab at index 0 (before any per-camera tab); `self.cross_plot`/`self.cross_stats_panel` (HW TS Latency, unchanged names) and new `self.cross_position_plot`/`self.cross_position_stats_panel` (Optical Sync); `_on_cross_pair_ready` is pure O(1) bookkeeping (no `add_point` call); `_on_cross_stats_ready` is the only place either graph's `add_point` is called.

- [ ] **Step 1: Write the failing tests**

Replace the existing `test_matching_rows_produce_a_cross_camera_plot_point` test in `tests/gui/pages/test_multi_camera_live_session_page.py` (lines 234-269) with:

```python
def test_cross_pair_ready_does_not_plot_directly(qapp, tmp_path):
    # Efficiency fix: row_ready-cadence callbacks must stay O(1) (CLAUDE.md's
    # documented row_ready/stats_ready split) - add_point only happens on the
    # throttled stats_ready cadence, in _on_cross_stats_ready.
    page, fake_threads = _page_with_fake_threads()
    page.set_cameras(object(), _two_cameras(tmp_path))
    page.start_all_sessions()
    series_key = page._cross_pair_series_keys[("cam2", "infrared1")]

    fake_threads["SN1"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_000.0, "stream_b_ts_us": 1_000_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_010.0, "stream_b_ts_us": 1_000_010.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })

    assert page.cross_plot.get_series_data(series_key)[1] == []
    assert len(page._cross_rows) == 1


def test_matching_rows_plot_a_cross_camera_hw_ts_point_on_stats_ready(qapp, tmp_path):
    page, fake_threads = _page_with_fake_threads()
    page.set_cameras(object(), _two_cameras(tmp_path))
    page.start_all_sessions()

    # First pair is the reconciler's own calibration pair (see
    # engine.cross_camera_reconciler.CrossCameraReconciler's docstring -
    # genlock stabilizes phase/rate between two devices, not their absolute
    # HW-timestamp epoch, so the first-ever match learns that constant
    # offset rather than measuring anything yet) - always reports 0.0.
    fake_threads["SN1"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_000.0, "stream_b_ts_us": 1_000_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_010.0, "stream_b_ts_us": 1_000_010.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    # Second pair, after calibration (offset learned: 10) - reports the
    # genuine residual (-5), not the raw absolute difference.
    fake_threads["SN1"].row_ready.emit({
        "pair_index": 2, "stream_a_ts_us": 1_100_000.0, "stream_b_ts_us": 1_100_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 2, "stream_a_ts_us": 1_100_015.0, "stream_b_ts_us": 1_100_015.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })

    # The plot point only appears once the throttled stats_ready cadence
    # fires - mirrors piggybacking on any camera's own stats_ready
    # (engine.multi_camera_session.MultiCameraSessionController._on_stats_ready).
    fake_threads["SN1"].stats_ready.emit({"pair_index": 2})

    series_key = page._cross_pair_series_keys[("cam2", "infrared1")]
    _, ys = page.cross_plot.get_series_data(series_key)
    assert ys == [-5.0]


def test_matching_rows_plot_a_cross_camera_optical_sync_point_on_stats_ready(qapp, tmp_path):
    page, fake_threads = _page_with_fake_threads()
    page.set_cameras(object(), _two_cameras(tmp_path))
    page.start_all_sessions()

    # Calibration pair - HW TS offset learned; LED indices equal (no
    # assertion needed on this one).
    fake_threads["SN1"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_000.0, "stream_b_ts_us": 1_000_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
        "stream_a_last_led": 0, "position_gap_ms_excluded": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_010.0, "stream_b_ts_us": 1_000_010.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
        "stream_a_last_led": 0, "position_gap_ms_excluded": False,
    })
    # Second pair: master detects LED 1, slave detects LED 0. _camera_config's
    # default num_leds=2, switch_time_ms=1.0 ->
    # compute_position_gap(1, 0, 2) == 1, * 1.0 == 1.0ms.
    fake_threads["SN1"].row_ready.emit({
        "pair_index": 2, "stream_a_ts_us": 1_100_000.0, "stream_b_ts_us": 1_100_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
        "stream_a_last_led": 1, "position_gap_ms_excluded": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 2, "stream_a_ts_us": 1_100_015.0, "stream_b_ts_us": 1_100_015.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
        "stream_a_last_led": 0, "position_gap_ms_excluded": False,
    })

    fake_threads["SN1"].stats_ready.emit({"pair_index": 2})

    series_key = page._cross_pair_series_keys[("cam2", "infrared1")]
    _, ys = page.cross_position_plot.get_series_data(series_key)
    assert ys == [1.0]


def test_cross_camera_tab_is_first(qapp, tmp_path):
    page, _ = _page_with_fake_threads()

    page.set_cameras(object(), _two_cameras(tmp_path))

    assert page.tabs.tabText(0) == "Cross-Camera Sync"
    assert page.tabs.count() == 3  # cross-camera tab + 2 per-camera tabs
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_multi_camera_live_session_page.py -k "cross_pair_ready_does_not_plot_directly or plot_a_cross_camera or cross_camera_tab_is_first" -v`
Expected: FAIL - `test_cross_pair_ready_does_not_plot_directly` fails because current code DOES call `add_point` from `_on_cross_pair_ready` (so `get_series_data` already returns a non-empty list); `test_matching_rows_plot_a_cross_camera_optical_sync_point_on_stats_ready` fails with `AttributeError: 'MultiCameraLiveSessionPage' object has no attribute 'cross_position_plot'`; `test_cross_camera_tab_is_first` fails on `tabText(0) == "Cross-Camera Sync"` (currently a per-camera panel tab).

- [ ] **Step 3: Implement**

In `gui/pages/multi_camera_live_session_page.py`, update the module docstring's stale genlock/cross-camera line (lines 35-37) - drop the now-outdated "stays infrared-only" claim (cross-camera RGB pairing was re-enabled with a resolution guard earlier in this project - see `gui/main_window.py`'s `_slave_genlock_color_resolution_conflicts`):

```python
Cross-camera comparison pairs any shared stream identity the operator
configured (infrared or color) - see engine/cross_camera_reconciler.py
and gui/main_window.py's own resolution-ceiling guard for a genlock
slave's color stream.
```

Update `__init__` (replace lines 134-141):

```python
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        self._cross_tab_widget = QWidget()
        self._cross_tab_layout = QVBoxLayout(self._cross_tab_widget)
        self.cross_plot = None
        self.cross_stats_panel = None
        self.cross_position_plot = None
        self.cross_position_stats_panel = None
```

Update `set_cameras` (replace lines 146-164):

```python
    def set_cameras(self, ctx, cameras):
        """cameras: list of {"camera_id", "label", "is_master", "config"} -
        exactly what MainWindow's self._cameras/self._master_camera_id
        already hold, built fresh by MainWindow's own _refresh_camera_hub-
        style helper right before switching to this page."""
        self._ctx = ctx
        self._cameras = cameras

        self.tabs.clear()
        self._panels = {}

        # Cross-camera tab first - it's the operator's primary test.
        self._rebuild_cross_camera_section(cameras)
        self.tabs.addTab(self._cross_tab_widget, "Cross-Camera Sync")

        for camera in cameras:
            panel = CameraLiveSessionPanel(camera["camera_id"])
            config = camera["config"]
            panel.set_camera_labels(camera["label"], config["stream_a_label"], config["stream_b_label"])
            tab_label = camera["label"] + (" [MASTER]" if camera["is_master"] else "")
            self.tabs.addTab(panel, tab_label)
            self._panels[camera["camera_id"]] = panel
```

Replace `_rebuild_cross_camera_section` (lines 166-218):

```python
    def _rebuild_cross_camera_section(self, cameras):
        while self._cross_tab_layout.count():
            item = self._cross_tab_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self._cross_pair_series_keys = {}
        self.cross_plot = LivePlot()
        self.cross_plot.setLabel("left", "Cross-Camera HW TS Latency (us)")
        self.cross_plot.setLabel("bottom", "Pair Index")
        self.cross_stats_panel = StatsPanel()
        self.cross_stats_panel.setFixedWidth(220)
        self.cross_stats_panel.add_section_header("Cross-Camera HW TS Latency")

        self.cross_position_plot = LivePlot()
        self.cross_position_plot.setLabel("left", "Cross-Camera Optical Sync (ms)")
        self.cross_position_plot.setLabel("bottom", "Pair Index")
        self.cross_position_stats_panel = StatsPanel()
        self.cross_position_stats_panel.setFixedWidth(220)
        self.cross_position_stats_panel.add_section_header("Cross-Camera Optical Sync")

        self._cross_tab_layout.addWidget(QLabel("Cross-Camera Sync (master vs. each slave)"))

        if len(cameras) < 2:
            self._cross_tab_layout.addWidget(
                QLabel("Add a second camera to see cross-camera sync.")
            )
            return

        identity_specs = [
            _IdentitySpec(
                camera["camera_id"], camera["is_master"], _stream_identities(camera["config"]),
                num_leds=camera["config"]["num_leds"], switch_time_ms=camera["config"]["switch_time_ms"],
            )
            for camera in cameras
        ]
        try:
            # outlier_threshold_us here only shapes the PairingGapMetric
            # instances this call constructs for its own throwaway use
            # (deciding which series to show) - start_all_sessions builds
            # the REAL ones the controller actually uses, with each run's
            # own configured threshold.
            pair_specs = build_cross_camera_pair_specs(identity_specs, outlier_threshold_us=100_000)
        except ValueError:
            # No master designated - shouldn't be reachable once Start is
            # actually clickable (CameraHubPage._can_start already requires
            # exactly one), but guard defensively rather than crash the page.
            pair_specs = []

        labels_by_id = {camera["camera_id"]: camera["label"] for camera in cameras}
        for index, spec in enumerate(pair_specs):
            series_key = "{}::{}".format(spec.slave_camera_id, spec.stream_identity)
            display_name = "{} {}".format(labels_by_id[spec.slave_camera_id], spec.stream_identity)
            color = CROSS_CAMERA_COLORS[index % len(CROSS_CAMERA_COLORS)]
            self.cross_plot.add_series(series_key, color=color, display_name=display_name)
            self.cross_stats_panel.add_field(series_key, display_name)
            self.cross_position_plot.add_series(series_key, color=color, display_name=display_name)
            self.cross_position_stats_panel.add_field(series_key, display_name)
            self._cross_pair_series_keys[(spec.slave_camera_id, spec.stream_identity)] = series_key

        pairing_row = QHBoxLayout()
        pairing_row.addWidget(self.cross_plot, stretch=1)
        pairing_row.addWidget(self.cross_stats_panel)
        self._cross_tab_layout.addLayout(pairing_row)

        position_row = QHBoxLayout()
        position_row.addWidget(self.cross_position_plot, stretch=1)
        position_row.addWidget(self.cross_position_stats_panel)
        self._cross_tab_layout.addLayout(position_row)
```

Replace `_on_cross_pair_ready` and `_on_cross_stats_ready` (lines 352-368):

```python
    def _on_cross_pair_ready(self, cross_row):
        # O(1) bookkeeping only - no add_point here. Fires unthrottled, once
        # per cross-camera match; plotting on this cadence caused a real GUI
        # freeze for the analogous intra-camera case (see CLAUDE.md's
        # row_ready/stats_ready cadence split). Both graphs' add_point calls
        # happen only in _on_cross_stats_ready, below.
        self._cross_rows.append(cross_row)

    def _on_cross_stats_ready(self, latest_by_pair):
        for key, series_key in self._cross_pair_series_keys.items():
            row = latest_by_pair.get(key)
            if row is None:
                continue

            self.cross_stats_panel.set_value(series_key, row["pairing_gap_us"])
            pairing_value = row["pairing_gap_us"]
            if row.get("pairing_gap_us_excluded"):
                pairing_value = float("nan")
            self.cross_plot.add_point(series_key, row["pair_index"], pairing_value)

            if row.get("position_gap_ms") is not None:
                self.cross_position_stats_panel.set_value(series_key, row["position_gap_ms"])
                position_value = row["position_gap_ms"]
                if row.get("position_gap_ms_excluded"):
                    position_value = float("nan")
                self.cross_position_plot.add_point(series_key, row["pair_index"], position_value)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_multi_camera_live_session_page.py -v`
Expected: PASS (all tests in the file).

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python.exe -m pytest -v`
Expected: PASS (every test in the project, including all tasks in this plan).

- [ ] **Step 6: Commit**

```bash
git add gui/pages/multi_camera_live_session_page.py tests/gui/pages/test_multi_camera_live_session_page.py
git commit -m "feat: cross-camera sync moves into its own tab with HW TS + Optical Sync graphs"
```

---

## Self-Review

**Spec coverage:**
- Section 1 (data flow via `MetricResult.extra`, reuse the same matched pair) - Task 1 (extra fields), Task 3 (`_build_cross_row` reuses the pair, no second match, no new metric instance).
- Section 2 (exclusion: frame drop first, reused miss/no_led_data, master's num_leds/switch_time_ms authoritative, no panel-topology special-casing) - Task 3's `_compute_cross_position_gap` implements the exact priority order; no panel-related branching anywhere in this plan, satisfying "no special-casing needed."
- Section 3 (new tab first, two graph+stats rows, efficiency fix) - Task 7.
- Section 4 (3-camera generalization) - no dedicated task needed, confirmed free by construction: `build_cross_camera_pair_specs` already loops over every non-master camera (Task 2 only adds two fields to its output), and `_rebuild_cross_camera_section`/`_on_cross_stats_ready` already iterate `pair_specs`/`_cross_pair_series_keys` generically (no camera-count-specific logic anywhere in Task 7's rewritten methods) - a 3rd camera just produces a 2nd `CrossCameraPairSpec` and a 2nd pair of series on both graphs automatically.
- `domain/csv_export.py` - confirmed via direct code read to need no changes (Global Constraints); explicitly called out so no task was wrongly added for it.

**Placeholder scan:** No TBD/TODO/"add appropriate"/"similar to Task N" phrases - every step has real, complete code.

**Type consistency:** `position_gap_ms`/`position_gap_ms_excluded`/`position_gap_ms_exclude_reason` (Task 3's output keys) match exactly what Task 6 (`_build_cross_camera_figure`) and Task 7 (`_on_cross_stats_ready`) read. `num_leds`/`switch_time_ms` attribute names match across `CrossCameraPairSpec` (Task 2), `CameraSessionSpec` (Task 4), and `_IdentitySpec` (Task 5/7). `cross_position_plot`/`cross_position_stats_panel` names introduced in Task 7 are used consistently in that task's own tests and implementation - no other task references them.
