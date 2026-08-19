# Global-Timestamp Cross-Camera Matching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `CrossCameraReconciler` matches master/slave frame pairs using RealSense's GLOBAL_TIME-domain timestamp instead of raw HW timestamps, and reports a new "Global TS Latency" (`global_ts_gap_us`) metric alongside the existing "HW TS Latency" (`pairing_gap_us`) so real-hardware drift in the HW-ts-based one-time offset calibration can be directly compared against the (expected-to-be-drift-free) global-ts number, pair-for-pair.

**Architecture:** `ContinuousCapture` gains an opt-in `capture_global_ts` flag that reads+validates each frame's RealSense global timestamp; this threads through `FramePairSample`/`AcquisitionLoop`/`TestSession`'s row exactly like the existing HW timestamp does, always present (`None` when not captured). `CrossCameraReconciler`'s join switches to this new field (a plain tight-window search, no more calibration-for-matching), while `pairing_gap_us`'s own one-time HW-offset calibration moves to being a post-match reporting step instead of a pre-match search concern. Only multi-camera runs (`MultiCameraLiveSessionPage`) turn `capture_global_ts` on; single-camera `LiveSessionPage` never does.

**Tech Stack:** Python 3.10+/3.13, PySide6, pyrealsense2, pytest (`QT_QPA_PLATFORM=offscreen`, shared `qapp` fixture).

## Global Constraints

- `ContinuousCapture.capture_global_ts` defaults to `False`; `SessionEngineThread`'s own `capture_global_ts` constructor parameter also defaults `False`. `gui/pages/live_session_page.py`'s `LiveSessionPage.start_session()` never sets it - single-camera runs never require RealSense's `global_time_enabled` support.
- When `capture_global_ts=True` and either frame's `get_frame_timestamp_domain()` isn't `rs.timestamp_domain.global_time`, raise a `RuntimeError` with a clear message - fail loudly, never silently compute a meaningless value.
- `CrossCameraReconciler`'s join (matching) key is `{ts_role}_global_ts_us`, using a plain, uniform `max_match_gap_us` window from the very first row for a given spec - no more unbounded-first-search branch.
- `pairing_gap_us` ("HW TS Latency") keeps its exact current meaning and data key: still computed from raw HW ts (`{ts_role}_ts_us`), still offset-corrected once per spec (first match defines the offset, reported as `0.0`).
- `global_ts_gap_us` ("Global TS Latency") is the plain, **never offset-corrected** difference between the two sides' global timestamps for the same matched pair - `master_global_ts - slave_global_ts`, nothing subtracted.
- Both metrics are computed from exactly ONE matching pass per pair - never two independent searches.
- `domain/csv_export.py` needs no code changes - its column list is already derived dynamically from whatever keys exist in the rows.

---

### Task 1: `ContinuousCapture` gains opt-in global-timestamp capture

**Files:**
- Modify: `engine/streams.py` (`ContinuousCapture.__init__`, `frames_with_diagnostics`, `frames`; new module-level `_read_global_ts_us` helper)
- Test: `tests/engine/test_streams.py`

**Interfaces:**
- Produces: `ContinuousCapture(..., capture_global_ts=False)` - new constructor kwarg. `frames_with_diagnostics()` now yields an 8-tuple `(image_a, image_b, ts_a, ts_b, num_a, num_b, global_ts_a, global_ts_b)` (previously a 6-tuple) - the last two are `None, None` when `capture_global_ts` is `False`. `_read_global_ts_us(frame_a, frame_b) -> (float, float)` - a new standalone module-level function, raises `RuntimeError` if either frame isn't in the `rs.timestamp_domain.global_time` domain, else returns `(frame_a.get_timestamp() * 1000.0, frame_b.get_timestamp() * 1000.0)` (ms -> us).
- Consumes: nothing from other tasks - fully self-contained.

- [ ] **Step 1: Write the failing tests**

Add this import to `tests/engine/test_streams.py`'s existing `from engine.streams import (...)` block (currently lines 7-14):

```python
from engine.streams import (
    list_devices, capture_synced_frame_pair, ContinuousCapture,
    enable_auto_exposure,
    list_video_stream_options_from_device, resolve_and_group, group_for_pick, exposure_for_group,
    set_emitter_enabled, set_manual_exposure, stream_slug,
    parse_camera_tests_config, resolve_camera_tests,
    set_inter_cam_sync_mode, INTER_CAM_SYNC_MASTER, INTER_CAM_SYNC_SLAVE,
    resolve_inter_cam_sync_value, resolve_max_slave_color_resolution,
    _read_global_ts_us,
)
```

Append this new section at the end of `tests/engine/test_streams.py`:

```python
# --- ContinuousCapture's opt-in capture_global_ts feature: _read_global_ts_us
# is a pure validation+conversion helper, testable with a tiny fake exposing
# only the two rs.frame methods it actually calls - no real pipeline/frameset
# needed, same "pull the meaningful logic into its own testable function"
# convention _depth_sync_stream/_build_config already use in this file. ---

class _FakeGlobalTsFrame:
    def __init__(self, timestamp_ms, domain):
        self._timestamp_ms = timestamp_ms
        self._domain = domain

    def get_timestamp(self):
        return self._timestamp_ms

    def get_frame_timestamp_domain(self):
        return self._domain


def test_read_global_ts_us_converts_ms_to_us_for_both_frames():
    frame_a = _FakeGlobalTsFrame(1000.5, rs.timestamp_domain.global_time)
    frame_b = _FakeGlobalTsFrame(2000.25, rs.timestamp_domain.global_time)

    global_ts_a, global_ts_b = _read_global_ts_us(frame_a, frame_b)

    assert global_ts_a == 1_000_500.0
    assert global_ts_b == 2_000_250.0


def test_read_global_ts_us_raises_when_frame_a_is_the_wrong_domain():
    frame_a = _FakeGlobalTsFrame(1000.0, rs.timestamp_domain.system_time)
    frame_b = _FakeGlobalTsFrame(2000.0, rs.timestamp_domain.global_time)

    with pytest.raises(RuntimeError, match="GLOBAL_TIME"):
        _read_global_ts_us(frame_a, frame_b)


def test_read_global_ts_us_raises_when_frame_b_is_the_wrong_domain():
    frame_a = _FakeGlobalTsFrame(1000.0, rs.timestamp_domain.global_time)
    frame_b = _FakeGlobalTsFrame(2000.0, rs.timestamp_domain.hardware_clock)

    with pytest.raises(RuntimeError, match="GLOBAL_TIME"):
        _read_global_ts_us(frame_a, frame_b)


def test_continuous_capture_capture_global_ts_defaults_to_false():
    capture = ContinuousCapture("SN1", _ir_pick(), _color_pick())
    assert capture.capture_global_ts is False


def test_continuous_capture_capture_global_ts_can_be_enabled():
    capture = ContinuousCapture("SN1", _ir_pick(), _color_pick(), capture_global_ts=True)
    assert capture.capture_global_ts is True
```

(`_ir_pick`/`_color_pick` already exist in this file, defined above the `ContinuousCapture._depth_sync_stream` test section.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_streams.py -k "global_ts" -v`
Expected: FAIL - `ImportError: cannot import name '_read_global_ts_us'` (doesn't exist yet), and `TypeError: __init__() got an unexpected keyword argument 'capture_global_ts'`.

- [ ] **Step 3: Implement**

In `engine/streams.py`, add this new function right before `class ContinuousCapture:` (currently line 648):

```python
def _read_global_ts_us(frame_a, frame_b):
    """Reads and validates both frames' RealSense global timestamp
    (frame.get_timestamp(), converted from its native ms to this project's
    _ts_us microsecond convention) - the join key
    engine.cross_camera_reconciler.CrossCameraReconciler's matching uses.
    Raises if either frame isn't actually reporting the GLOBAL_TIME domain:
    global_time_enabled may be disabled/unsupported on this device/driver,
    in which case frame.get_timestamp() silently falls back to a different
    domain (system_time/hardware_clock) that isn't comparable across two
    independent devices the way GLOBAL_TIME is meant to be - a silently-
    wrong value here would be worse than an obvious failure (same "fail
    loudly" convention as the frame_timestamp metadata check in
    ContinuousCapture.frames_with_diagnostics, the only caller of this
    function)."""
    domain = rs.timestamp_domain.global_time
    if frame_a.get_frame_timestamp_domain() != domain or frame_b.get_frame_timestamp_domain() != domain:
        raise RuntimeError(
            "This camera is not reporting frames in the RealSense GLOBAL_TIME "
            "timestamp domain (global_time_enabled may be disabled or unsupported "
            "on this device/driver), which the cross-camera Global TS Latency "
            "metric requires. Reconnect the camera or disable "
            "camera_sync.capture_global_ts and retry."
        )
    return frame_a.get_timestamp() * 1000.0, frame_b.get_timestamp() * 1000.0
```

Modify `ContinuousCapture.__init__` (currently lines 649-660):

```python
class ContinuousCapture:
    def __init__(self, device_serial, pick_a, pick_b, enable_depth_for_ir_sync=True, capture_global_ts=False):
        self.device_serial = device_serial
        self.pick_a = pick_a
        self.pick_b = pick_b
        # See _depth_sync_stream/_build_config - whether to co-enable the
        # stereo module's depth stream to fix IR/RGB sync.
        self.enable_depth_for_ir_sync = enable_depth_for_ir_sync
        # Opt-in: reads+validates each frame's RealSense GLOBAL_TIME-domain
        # timestamp too (see _read_global_ts_us) - a cross-camera-only
        # concept (engine.cross_camera_reconciler's matching key and its
        # Global TS Latency metric), so single-camera runs never need or
        # request it.
        self.capture_global_ts = capture_global_ts
        # Set on start() to whether a depth stream was actually requested
        # (self._depth_sync_stream() is not None) - not a resolve/success
        # check, just what start() attempted, for callers that want to report.
        self.depth_sync_active = False
        self._pipeline = None
```

Modify `frames_with_diagnostics` (currently lines 751-777):

```python
    def frames_with_diagnostics(self):
        while True:
            frameset = self._pipeline.wait_for_frames()
            frame_a = self._get_frame(frameset, self.pick_a)
            frame_b = self._get_frame(frameset, self.pick_b)
            if not frame_a or not frame_b:
                continue

            metadata = rs.frame_metadata_value.frame_timestamp
            if not (frame_a.supports_frame_metadata(metadata) and frame_b.supports_frame_metadata(metadata)):
                raise RuntimeError(
                    "This camera/driver does not expose per-frame HW timestamp metadata "
                    "(frame_metadata_value.frame_timestamp), which the sync metrics require. "
                    "On Windows, RealSense per-frame metadata is often disabled by default at "
                    "the OS/driver level and needs a one-time enablement step (see Intel's "
                    "librealsense documentation on Windows metadata support) - reconnect the "
                    "camera after enabling it and retry."
                )

            image_a = decode_frame(bytes(frame_a.get_data()), self.pick_a["format"], self.pick_a["width"], self.pick_a["height"])
            image_b = decode_frame(bytes(frame_b.get_data()), self.pick_b["format"], self.pick_b["width"], self.pick_b["height"])
            ts_a = frame_a.get_frame_metadata(metadata)
            ts_b = frame_b.get_frame_metadata(metadata)
            num_a = frame_a.get_frame_number()
            num_b = frame_b.get_frame_number()

            if self.capture_global_ts:
                global_ts_a, global_ts_b = _read_global_ts_us(frame_a, frame_b)
            else:
                global_ts_a, global_ts_b = None, None

            yield image_a, image_b, ts_a, ts_b, num_a, num_b, global_ts_a, global_ts_b
```

Modify `frames` (currently lines 747-749):

```python
    def frames(self):
        for stream_a_image, stream_b_image, stream_a_ts_us, stream_b_ts_us, _, _, _, _ in self.frames_with_diagnostics():
            yield stream_a_image, stream_b_image, stream_a_ts_us, stream_b_ts_us
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_streams.py -v`
Expected: PASS (every test in this file).

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python.exe -m pytest -v`
Expected: PASS (every test in the project).

- [ ] **Step 6: Commit**

```bash
git add engine/streams.py tests/engine/test_streams.py
git commit -m "feat: ContinuousCapture gains opt-in RealSense global-timestamp capture"
```

---

### Task 2: Plumb global timestamps through `FramePairSample`, `AcquisitionLoop`, `TestSession`, `SessionEngineThread`

**Files:**
- Modify: `engine/metrics.py` (`FramePairSample`), `engine/acquisition_loop.py` (`AcquisitionLoop.run_until_stopped`), `engine/test_session.py` (`TestSession.process_pair`), `engine/session_engine.py` (`SessionEngineThread.__init__`/`run`/`_frame_pairs_with_brightness`)
- Test: `tests/engine/test_acquisition_loop.py`, `tests/engine/test_test_session.py`

**Interfaces:**
- Consumes: `ContinuousCapture(..., capture_global_ts=...)`/`frames_with_diagnostics()`'s 8-tuple shape from Task 1 (exact tuple order: `image_a, image_b, ts_a, ts_b, num_a, num_b, global_ts_a, global_ts_b`).
- Produces: `FramePairSample.stream_a_global_ts_us`/`stream_b_global_ts_us` (both `"float | None" = None`, appended as the LAST two fields so existing positional-argument call sites like `FramePairSample(0, 0.0, 0.0)` stay valid). `TestSession.process_pair()`'s row dict always carries `"stream_a_global_ts_us"`/`"stream_b_global_ts_us"` keys (`None` when not captured). `SessionEngineThread(..., capture_global_ts=False)` - new constructor kwarg, passed straight through to its own `ContinuousCapture`.

- [ ] **Step 1: Write the failing tests**

In `tests/engine/test_acquisition_loop.py`, replace `fake_frame_source` (currently lines 14-18):

```python
def fake_frame_source(n_pairs):
    for i in range(n_pairs):
        stream_a_image = np.full((4, 4, 3), i, dtype=np.uint8)
        stream_b_image = np.full((4, 4, 3), i, dtype=np.uint8)
        yield stream_a_image, stream_b_image, float(i), float(i), None, None, None, None
```

Append this new section at the end of `tests/engine/test_acquisition_loop.py`:

```python
# --- Global timestamps: AcquisitionLoop's frame_source now yields an
# 8-tuple (2 more entries than before) - fake_frame_source above always
# supplies None, None for them; this section proves real values thread
# through into the row unchanged. ---

def fake_frame_source_with_global_ts(n_pairs):
    for i in range(n_pairs):
        stream_a_image = np.full((4, 4, 3), i, dtype=np.uint8)
        stream_b_image = np.full((4, 4, 3), i, dtype=np.uint8)
        yield stream_a_image, stream_b_image, float(i), float(i), None, None, float(i) * 10, float(i) * 10 + 1


def test_run_until_stopped_threads_global_ts_into_the_row():
    session = TestSession(TestSessionConfig(metrics=[CountingMetric()]))
    session.start()
    callbacks = AcquisitionCallbacks(on_frames=lambda *a: None, on_row=lambda r: None, on_stats=lambda s: None)
    loop = AcquisitionLoop(fake_frame_source_with_global_ts(2), session, callbacks, display_stride=10)

    rows = loop.run_until_stopped(is_stop_requested=lambda: False, elapsed_s_fn=lambda: 0.0)

    assert rows[0]["stream_a_global_ts_us"] == 0.0
    assert rows[0]["stream_b_global_ts_us"] == 1.0
    assert rows[1]["stream_a_global_ts_us"] == 10.0
    assert rows[1]["stream_b_global_ts_us"] == 11.0
```

Append this new section at the end of `tests/engine/test_test_session.py`:

```python
def test_process_pair_carries_global_ts_into_the_row():
    session = TestSession(TestSessionConfig(metrics=[FakeMetric()]))
    session.start()
    row = session.process_pair(FramePairSample(
        pair_index=0, stream_a_ts_us=100.0, stream_b_ts_us=100.0,
        stream_a_global_ts_us=5_000.0, stream_b_global_ts_us=5_001.0,
    ))
    assert row["stream_a_global_ts_us"] == 5_000.0
    assert row["stream_b_global_ts_us"] == 5_001.0


def test_process_pair_defaults_global_ts_to_none_when_not_captured():
    # Every existing single-camera FramePairSample call (this file's own
    # other tests included) never sets these two fields - process_pair
    # must not require them.
    session = TestSession(TestSessionConfig(metrics=[FakeMetric()]))
    session.start()
    row = session.process_pair(FramePairSample(pair_index=0, stream_a_ts_us=100.0, stream_b_ts_us=100.0))
    assert row["stream_a_global_ts_us"] is None
    assert row["stream_b_global_ts_us"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_acquisition_loop.py tests/engine/test_test_session.py -v`
Expected: the pre-existing tests in `test_acquisition_loop.py` FAIL first with `ValueError: not enough values to unpack` (since `fake_frame_source` now yields 8-tuples but `AcquisitionLoop` still unpacks 6) - this is expected; the two NEW tests fail with the same unpack error plus `KeyError`/`AttributeError` for the not-yet-existing global-ts fields. `test_test_session.py`'s two new tests FAIL with `TypeError: __init__() got an unexpected keyword argument 'stream_a_global_ts_us'` and `KeyError: 'stream_a_global_ts_us'`.

- [ ] **Step 3: Implement**

In `engine/metrics.py`, modify `FramePairSample` (currently lines 19-27):

```python
@dataclass
class FramePairSample:
    pair_index: int
    stream_a_ts_us: float
    stream_b_ts_us: float
    stream_a_bright: "np.ndarray | None" = None
    stream_b_bright: "np.ndarray | None" = None
    stream_a_frame_drop: bool = False
    stream_b_frame_drop: bool = False
    stream_a_global_ts_us: "float | None" = None
    stream_b_global_ts_us: "float | None" = None
```

In `engine/acquisition_loop.py`, modify `AcquisitionLoop.run_until_stopped` (currently lines 30-56):

```python
    def run_until_stopped(self, is_stop_requested, elapsed_s_fn) -> "list[dict]":
        pair_index = 0
        for (stream_a_image, stream_b_image, stream_a_ts_us, stream_b_ts_us,
             stream_a_bright, stream_b_bright,
             stream_a_global_ts_us, stream_b_global_ts_us) in self.frame_source:
            if is_stop_requested():
                break
            if self.test_session.should_auto_stop(elapsed_s_fn()):
                break

            sample = FramePairSample(
                pair_index=pair_index,
                stream_a_ts_us=stream_a_ts_us,
                stream_b_ts_us=stream_b_ts_us,
                stream_a_bright=stream_a_bright,
                stream_b_bright=stream_b_bright,
                stream_a_global_ts_us=stream_a_global_ts_us,
                stream_b_global_ts_us=stream_b_global_ts_us,
            )
            row = self.test_session.process_pair(sample)
            self.callbacks.on_row(row)
            if self.callbacks.on_frame_pair is not None:
                self.callbacks.on_frame_pair(stream_a_image, stream_b_image, row)

            if pair_index % self.display_stride == 0:
                self.callbacks.on_frames(stream_a_image, stream_b_image, pair_index)
                self.callbacks.on_stats(row)

            pair_index += 1

        return self.test_session.stop()
```

In `engine/test_session.py`, modify `TestSession.process_pair`'s row dict (currently lines 52-58):

```python
        row = {
            "pair_index": sample.pair_index,
            "stream_a_ts_us": sample.stream_a_ts_us,
            "stream_b_ts_us": sample.stream_b_ts_us,
            "stream_a_global_ts_us": sample.stream_a_global_ts_us,
            "stream_b_global_ts_us": sample.stream_b_global_ts_us,
            "stream_a_frame_drop": stream_a_drop,
            "stream_b_frame_drop": stream_b_drop,
        }
```

In `engine/session_engine.py`, modify `SessionEngineThread.__init__`'s signature (currently lines 74-81) - add `capture_global_ts=False` right after `enable_depth_for_ir_sync=True`:

```python
    def __init__(self, ctx, device_serial, pick_a, pick_b, camera_controls,
                 test_session, stream_a_xy=None, stream_b_xy=None, neighborhood_size=5,
                 scan_direction=None, switch_time_ms=None,
                 display_stride=10, position_gap_metric=None, dual_panel_config=None,
                 enable_depth_for_ir_sync=True, capture_global_ts=False,
                 hardware_reset_before_start=False,
                 hardware_reset_settle_s=8.0, output_dir=None,
                 position_gap_outlier_threshold_ms=None, position_gap_outlier_max_snapshots=200,
                 parent=None):
```

Add the assignment right after `self.enable_depth_for_ir_sync = enable_depth_for_ir_sync` (currently line 93):

```python
        self.enable_depth_for_ir_sync = enable_depth_for_ir_sync
        # Cross-camera-only concept (engine.cross_camera_reconciler's
        # matching key and its Global TS Latency metric) - see
        # ContinuousCapture.__init__'s own capture_global_ts docstring for
        # why single-camera runs never set this.
        self.capture_global_ts = capture_global_ts
```

Modify `_frame_pairs_with_brightness` (currently lines 132-149) to read from `frames_with_diagnostics()` directly instead of the 4-tuple `frames()` wrapper, so the global-ts values are available to pass through:

```python
    def _frame_pairs_with_brightness(self):
        """Adapts ContinuousCapture.frames_with_diagnostics()'s 8-tuple into
        the 8-tuple AcquisitionLoop/FramePairSample need, by sampling
        brightness at each calibrated LED position and discarding the two
        frame-number entries this method has no use for. Reads
        frames_with_diagnostics() directly (not the plain 4-tuple frames()
        wrapper) specifically so the global-ts values it also carries reach
        AcquisitionLoop - frames() itself stays unchanged for its own
        callers (gui/pages/calibration_page.py, gui/pages/roi_select_page.py),
        which have no notion of metrics/global-ts at all."""
        for (stream_a_image, stream_b_image, stream_a_ts_us, stream_b_ts_us, _, _,
             stream_a_global_ts_us, stream_b_global_ts_us) in self._capture.frames_with_diagnostics():
            stream_a_bright = (
                sample_all_neighborhood_brightness(stream_a_image, self.stream_a_xy, self._stream_a_safe_size)
                if self.stream_a_xy is not None else None
            )
            stream_b_bright = (
                sample_all_neighborhood_brightness(stream_b_image, self.stream_b_xy, self._stream_b_safe_size)
                if self.stream_b_xy is not None else None
            )
            yield (stream_a_image, stream_b_image, stream_a_ts_us, stream_b_ts_us,
                   stream_a_bright, stream_b_bright, stream_a_global_ts_us, stream_b_global_ts_us)
```

Modify the `ContinuousCapture(...)` construction inside `run()` (currently lines 253-256):

```python
            self._capture = ContinuousCapture(
                self.device_serial, self.pick_a, self.pick_b,
                enable_depth_for_ir_sync=self.enable_depth_for_ir_sync,
                capture_global_ts=self.capture_global_ts,
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_acquisition_loop.py tests/engine/test_test_session.py -v`
Expected: PASS (every test in both files).

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python.exe -m pytest -v`
Expected: PASS (every test in the project).

- [ ] **Step 6: Commit**

```bash
git add engine/metrics.py engine/acquisition_loop.py engine/test_session.py engine/session_engine.py \
        tests/engine/test_acquisition_loop.py tests/engine/test_test_session.py
git commit -m "feat: thread global timestamps through FramePairSample/AcquisitionLoop/TestSession/SessionEngineThread"
```

---

### Task 3: `CrossCameraReconciler` matches on global TS; adds Global TS Latency; multi-camera page wiring

**Files:**
- Modify: `engine/cross_camera_reconciler.py` (`CrossCameraPairSpec`, `build_cross_camera_pair_specs`, `CrossCameraReconciler.__init__`/`ingest_row`/`_ingest_side`/`_build_cross_row`)
- Modify: `gui/pages/multi_camera_live_session_page.py` (`_build_slave_section`, `_reset_cross_run_state`, `start_all_sessions`'s `thread_kwargs`, `_on_cross_pair_ready`, `_on_cross_stats_ready`)
- Test: `tests/engine/test_cross_camera_reconciler.py`, `tests/gui/pages/test_multi_camera_live_session_page.py`

**Interfaces:**
- Consumes: `TestSession.process_pair()`'s row dict now always carrying `stream_a_global_ts_us`/`stream_b_global_ts_us` (Task 2); `SessionEngineThread(..., capture_global_ts=...)` (Task 2).
- Produces: `CrossCameraPairSpec.global_ts_gap_metric: object` (a second, independent `PairingGapMetric` instance). Every cross-row dict `CrossCameraReconciler.ingest_row()`/`_build_cross_row` returns now also carries `global_ts_gap_us`/`global_ts_gap_us_excluded`/`global_ts_gap_us_exclude_reason`, alongside the unchanged `pairing_gap_us`/`_excluded`/`_exclude_reason`. `gui/pages/multi_camera_live_session_page.py`'s per-slave section dict (`self._slave_sections[slave_camera_id]`) gains a `"global_ts_plot"` key. `self._cross_running_stats` gains `(slave_camera_id, identity, "global_ts_gap_us")` entries alongside the existing `"pairing_gap_us"`/`"position_gap_ms"` ones.

**Why this is one task, not two:** `tests/gui/pages/test_multi_camera_live_session_page.py`'s cross-camera tests exercise the REAL `CrossCameraReconciler` end-to-end (only `SessionEngineThread` itself is faked, via `thread_factory` - `MultiCameraSessionController`/`CrossCameraReconciler` are the genuine production classes). Landing the reconciler's matching-key change without also updating this file's `row_ready.emit({...})` fake dicts (which currently never carry `stream_a_global_ts_us`/`stream_b_global_ts_us` at all) would leave every cross-camera match silently failing - the full suite would only turn green again once both files land together.

- [ ] **Step 1: Write the failing tests for the reconciler**

In `tests/engine/test_cross_camera_reconciler.py`, replace `_row` (currently lines 46-57):

```python
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
```

Replace the block comment right above the matching tests (currently lines 60-75) with:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_cross_camera_reconciler.py -v`
Expected: FAIL - most matching tests fail because `_ingest_side` still reads `{ts_role}_ts_us` (no `global_ts_gap_us` key produced at all, so `KeyError`/`AssertionError` on the new assertions); `test_build_specs_gives_each_pair_its_own_metric_instances` fails with `AttributeError: 'CrossCameraPairSpec' object has no attribute 'global_ts_gap_metric'`.

- [ ] **Step 3: Implement the reconciler changes**

In `engine/cross_camera_reconciler.py`, modify `CrossCameraPairSpec` (currently lines 28-46) - add `global_ts_gap_metric` right after `pairing_gap_metric`:

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
    pairing_gap_metric: object  # engine.metrics.PairingGapMetric, HW-ts-based, offset-corrected
    global_ts_gap_metric: object  # engine.metrics.PairingGapMetric, global-ts-based, NEVER offset-corrected
    # Master's own num_leds/switch_time_ms - authoritative for the cross-camera
    # Optical Sync circular wraparound math and unit conversion (same "master's
    # config wins" reasoning already used elsewhere in this project). The
    # slave's own configured values are never read here.
    num_leds: int
    switch_time_ms: float
```

Modify `build_cross_camera_pair_specs`'s `CrossCameraPairSpec(...)` construction (currently lines 91-100):

```python
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
```

Replace `CrossCameraReconciler.__init__` (currently lines 180-201):

```python
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
```

Replace `ingest_row` (currently lines 203-224):

```python
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
```

Replace `_ingest_side` (currently lines 226-256):

```python
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
```

Replace `_build_cross_row` (currently lines 258-294):

```python
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
```

Replace the module docstring at the top of the file (currently lines 1-21):

```python
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
```

Replace the `CrossCameraReconciler` class docstring (currently lines 140-178):

```python
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
    genlocked devices' raw HW timestamps still differ by an arbitrary, but
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_cross_camera_reconciler.py -v`
Expected: PASS (every test in this file).

- [ ] **Step 5: Write the failing tests for the multi-camera page**

In `tests/gui/pages/test_multi_camera_live_session_page.py`, every `row_ready.emit({...})` call needs `stream_a_global_ts_us`/`stream_b_global_ts_us` keys added (set equal to that same dict's `stream_a_ts_us`/`stream_b_ts_us` values, so `global_ts_gap_us` works out to a plain, easy-to-hand-compute difference in each test). Replace the following functions with these exact bodies:

```python
def test_cross_pair_ready_does_not_plot_directly(qapp, tmp_path):
    # Efficiency fix: row_ready-cadence callbacks must stay O(1) (CLAUDE.md's
    # documented row_ready/stats_ready split) - add_point only happens on the
    # throttled stats_ready cadence, in _on_cross_stats_ready.
    page, fake_threads = _page_with_fake_threads()
    page.set_cameras(object(), _two_cameras(tmp_path))
    page.start_all_sessions()
    pairing_plot = page._slave_sections["cam2"]["pairing_plot"]

    fake_threads["SN1"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_000.0, "stream_b_ts_us": 1_000_000.0,
        "stream_a_global_ts_us": 1_000_000.0, "stream_b_global_ts_us": 1_000_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_010.0, "stream_b_ts_us": 1_000_010.0,
        "stream_a_global_ts_us": 1_000_010.0, "stream_b_global_ts_us": 1_000_010.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })

    assert pairing_plot.get_series_data("infrared1")[1] == []
    # 2, not 1: _camera_config's two cameras share BOTH "infrared1" and
    # "color" identities, so one row_ready from each camera legitimately
    # produces one cross_pair_ready per shared identity.
    assert len(page._cross_rows) == 2


def test_matching_rows_plot_a_cross_camera_hw_ts_point_on_stats_ready(qapp, tmp_path):
    page, fake_threads = _page_with_fake_threads()
    page.set_cameras(object(), _two_cameras(tmp_path))
    page.start_all_sessions()

    # First pair is the reconciler's own HW-ts calibration pair - always 0.0.
    fake_threads["SN1"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_000.0, "stream_b_ts_us": 1_000_000.0,
        "stream_a_global_ts_us": 1_000_000.0, "stream_b_global_ts_us": 1_000_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_010.0, "stream_b_ts_us": 1_000_010.0,
        "stream_a_global_ts_us": 1_000_010.0, "stream_b_global_ts_us": 1_000_010.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    # Second pair, after calibration (HW-ts offset learned: 10) - reports
    # the genuine residual (-5), not the raw absolute difference.
    fake_threads["SN1"].row_ready.emit({
        "pair_index": 2, "stream_a_ts_us": 1_100_000.0, "stream_b_ts_us": 1_100_000.0,
        "stream_a_global_ts_us": 1_100_000.0, "stream_b_global_ts_us": 1_100_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 2, "stream_a_ts_us": 1_100_015.0, "stream_b_ts_us": 1_100_015.0,
        "stream_a_global_ts_us": 1_100_015.0, "stream_b_global_ts_us": 1_100_015.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })

    fake_threads["SN1"].stats_ready.emit({"pair_index": 2})

    pairing_plot = page._slave_sections["cam2"]["pairing_plot"]
    _, ys = pairing_plot.get_series_data("infrared1")
    assert ys == [-5.0]


def test_matching_rows_plot_a_cross_camera_global_ts_point_on_stats_ready(qapp, tmp_path):
    # Global TS Latency never gets offset-corrected - both pairs here use
    # IDENTICAL global-ts and hw-ts values (see _row payloads below), so
    # the plotted global-ts point (-15.0, the plain diff on the LATEST
    # pair) differs from the HW TS Latency point (-5.0, offset-corrected)
    # for the exact same underlying data - proving the two metrics are
    # genuinely independent, not aliases of each other.
    page, fake_threads = _page_with_fake_threads()
    page.set_cameras(object(), _two_cameras(tmp_path))
    page.start_all_sessions()

    fake_threads["SN1"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_000.0, "stream_b_ts_us": 1_000_000.0,
        "stream_a_global_ts_us": 1_000_000.0, "stream_b_global_ts_us": 1_000_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_010.0, "stream_b_ts_us": 1_000_010.0,
        "stream_a_global_ts_us": 1_000_010.0, "stream_b_global_ts_us": 1_000_010.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN1"].row_ready.emit({
        "pair_index": 2, "stream_a_ts_us": 1_100_000.0, "stream_b_ts_us": 1_100_000.0,
        "stream_a_global_ts_us": 1_100_000.0, "stream_b_global_ts_us": 1_100_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 2, "stream_a_ts_us": 1_100_015.0, "stream_b_ts_us": 1_100_015.0,
        "stream_a_global_ts_us": 1_100_015.0, "stream_b_global_ts_us": 1_100_015.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })

    fake_threads["SN1"].stats_ready.emit({"pair_index": 2})

    global_ts_plot = page._slave_sections["cam2"]["global_ts_plot"]
    _, ys = global_ts_plot.get_series_data("infrared1")
    assert ys == [-15.0]


def test_start_all_sessions_resets_running_stats_and_plots_on_a_second_run(qapp, tmp_path):
    page, fake_threads = _page_with_fake_threads()
    page.set_cameras(object(), _two_cameras(tmp_path))

    page.start_all_sessions()

    # Simulate "a previous run happened": pollute both a HW-ts and a
    # global-ts RunningStats instance and add points directly to both plots.
    pairing_key = ("cam2", "infrared1", "pairing_gap_us")
    global_ts_key = ("cam2", "infrared1", "global_ts_gap_us")
    page._cross_running_stats[pairing_key].update(123.0)
    page._cross_running_stats[global_ts_key].update(456.0)
    assert page._cross_running_stats[pairing_key].count != 0
    assert page._cross_running_stats[global_ts_key].count != 0
    pairing_plot = page._slave_sections["cam2"]["pairing_plot"]
    global_ts_plot = page._slave_sections["cam2"]["global_ts_plot"]
    pairing_plot.add_point("infrared1", 1, 5.0)
    global_ts_plot.add_point("infrared1", 1, 7.0)
    assert pairing_plot.get_series_data("infrared1")[1] != []
    assert global_ts_plot.get_series_data("infrared1")[1] != []

    fake_threads["SN1"].session_finished.emit([])
    fake_threads["SN1"].finished.emit()
    fake_threads["SN2"].session_finished.emit([])
    fake_threads["SN2"].finished.emit()

    page.start_all_sessions()

    assert page._cross_running_stats[pairing_key].count == 0
    assert page._cross_running_stats[global_ts_key].count == 0
    assert page._slave_sections["cam2"]["pairing_plot"].get_series_data("infrared1")[1] == []
    assert page._slave_sections["cam2"]["global_ts_plot"].get_series_data("infrared1")[1] == []


def test_cross_stats_ready_routes_only_to_the_exercised_slave_with_three_cameras(qapp, tmp_path):
    # Every other test in this file that drives real row_ready/stats_ready
    # data uses a 2-camera (1 master + 1 slave) setup, where mis-routing to
    # the wrong slave is undetectable by construction. This one uses 3
    # cameras (1 master + 2 slaves) and only ever exercises ONE of the two
    # slaves, proving _on_cross_stats_ready routes to the CORRECT slave's
    # widgets, not just some slave's.
    page, fake_threads = _page_with_fake_threads()
    cameras = _two_cameras(tmp_path)
    cameras.append({"camera_id": "cam3", "label": "D455 C", "is_master": False,
                     "config": _camera_config(tmp_path, device_serial="SN3")})
    page.set_cameras(object(), cameras)
    page.start_all_sessions()

    # First pair is the reconciler's own HW-ts calibration pair.
    fake_threads["SN1"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_000.0, "stream_b_ts_us": 1_000_000.0,
        "stream_a_global_ts_us": 1_000_000.0, "stream_b_global_ts_us": 1_000_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_010.0, "stream_b_ts_us": 1_000_010.0,
        "stream_a_global_ts_us": 1_000_010.0, "stream_b_global_ts_us": 1_000_010.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    # Second pair - a real match for cam2 only. cam3/SN3 never emits anything.
    fake_threads["SN1"].row_ready.emit({
        "pair_index": 2, "stream_a_ts_us": 1_100_000.0, "stream_b_ts_us": 1_100_000.0,
        "stream_a_global_ts_us": 1_100_000.0, "stream_b_global_ts_us": 1_100_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 2, "stream_a_ts_us": 1_100_015.0, "stream_b_ts_us": 1_100_015.0,
        "stream_a_global_ts_us": 1_100_015.0, "stream_b_global_ts_us": 1_100_015.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })

    fake_threads["SN1"].stats_ready.emit({"pair_index": 2})

    exercised_ys = page._slave_sections["cam2"]["pairing_plot"].get_series_data("infrared1")[1]
    unexercised_ys = page._slave_sections["cam3"]["pairing_plot"].get_series_data("infrared1")[1]
    assert exercised_ys != []
    assert unexercised_ys == []
    exercised_global_ys = page._slave_sections["cam2"]["global_ts_plot"].get_series_data("infrared1")[1]
    unexercised_global_ys = page._slave_sections["cam3"]["global_ts_plot"].get_series_data("infrared1")[1]
    assert exercised_global_ys != []
    assert unexercised_global_ys == []


def test_cross_stats_panel_shows_latest_pair_index_and_running_stats(qapp, tmp_path):
    page, fake_threads = _page_with_fake_threads()
    page.set_cameras(object(), _two_cameras(tmp_path))
    page.start_all_sessions()

    fake_threads["SN1"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_000.0, "stream_b_ts_us": 1_000_000.0,
        "stream_a_global_ts_us": 1_000_000.0, "stream_b_global_ts_us": 1_000_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_010.0, "stream_b_ts_us": 1_000_010.0,
        "stream_a_global_ts_us": 1_000_010.0, "stream_b_global_ts_us": 1_000_010.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN1"].row_ready.emit({
        "pair_index": 2, "stream_a_ts_us": 1_100_000.0, "stream_b_ts_us": 1_100_000.0,
        "stream_a_global_ts_us": 1_100_000.0, "stream_b_global_ts_us": 1_100_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 2, "stream_a_ts_us": 1_100_015.0, "stream_b_ts_us": 1_100_015.0,
        "stream_a_global_ts_us": 1_100_015.0, "stream_b_global_ts_us": 1_100_015.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })

    fake_threads["SN1"].stats_ready.emit({"pair_index": 2})

    stats_panel = page._slave_sections["cam2"]["stats_panel"]
    # "pair_index" is the reconciler's own synthetic counter - by the second
    # stats_ready tick, both "infrared1" and "color" identities have each
    # produced 2 cross-rows (4 total across both identities), so the max
    # pair_index seen is 4 (the reconciler's _pair_counter increments once
    # per cross-row it builds, across every pair-spec it owns).
    assert stats_panel._value_labels["pair_index"].text() == "4"
    assert stats_panel._value_labels["infrared1_hw_ts_latency_min"].text() != "-"
    assert stats_panel._value_labels["infrared1_hw_ts_latency_avg"].text() != "-"
    assert stats_panel._value_labels["infrared1_global_ts_latency_min"].text() != "-"
    assert stats_panel._value_labels["infrared1_global_ts_latency_avg"].text() != "-"
```

Also modify `test_cross_running_stats_registered_per_slave_identity_and_metric` (currently lines 465-473) to add the new metric's keys:

```python
def test_cross_running_stats_registered_per_slave_identity_and_metric(qapp, tmp_path):
    page, _ = _page_with_fake_threads()

    page.set_cameras(object(), _two_cameras(tmp_path))

    assert ("cam2", "infrared1", "pairing_gap_us") in page._cross_running_stats
    assert ("cam2", "infrared1", "global_ts_gap_us") in page._cross_running_stats
    assert ("cam2", "infrared1", "position_gap_ms") in page._cross_running_stats
    assert ("cam2", "color", "pairing_gap_us") in page._cross_running_stats
    assert ("cam2", "color", "global_ts_gap_us") in page._cross_running_stats
    assert ("cam2", "color", "position_gap_ms") in page._cross_running_stats
```

Add these two new tests anywhere in the file (e.g. right after `test_set_cameras_with_two_cameras_builds_one_cross_series_per_shared_identity`):

```python
def test_slave_section_has_a_global_ts_latency_plot(qapp, tmp_path):
    page, _ = _page_with_fake_threads()

    page.set_cameras(object(), _two_cameras(tmp_path))

    assert "global_ts_plot" in page._slave_sections["cam2"]


def test_start_all_sessions_requests_global_ts_capture_for_every_camera(qapp, tmp_path):
    page, fake_threads = _page_with_fake_threads()
    page.set_cameras(object(), _two_cameras(tmp_path))

    page.start_all_sessions()

    assert fake_threads["SN1"].kwargs["capture_global_ts"] is True
    assert fake_threads["SN2"].kwargs["capture_global_ts"] is True
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_multi_camera_live_session_page.py -v`
Expected: FAIL - `KeyError: 'global_ts_plot'` from the new/updated tests (the page doesn't build this plot yet), `KeyError: True` / `assert None is True` for the `capture_global_ts` assertion (`thread_kwargs` doesn't set it yet), and the reconciler-dependent tests fail because `_build_slave_section`/`_on_cross_stats_ready` don't yet read/plot `global_ts_gap_us` at all.

- [ ] **Step 7: Implement the multi-camera page changes**

In `gui/pages/multi_camera_live_session_page.py`, replace `_build_slave_section` (currently lines 345-412):

```python
    def _build_slave_section(self, slave, roles, master_display, specs):
        """One slave's worth of cross-camera UI: a header line, three
        stacked graphs (HW TS Latency, Global TS Latency, Optical Sync),
        and one combined stats panel - mirrors CameraLiveSessionPanel's own
        graphs_column + single stats_panel layout, scoped to this one
        slave's shared identities. Registers this slave's series keys and
        RunningStats instances into self._cross_pair_series_keys/
        self._cross_running_stats as a side effect - _on_cross_pair_ready/
        _on_cross_stats_ready read those to route incoming cross-rows here."""
        slave_camera_id = slave["camera_id"]
        slave_role = roles[slave_camera_id]

        section_widget = QWidget()
        section_layout = QVBoxLayout(section_widget)

        header_text = _slave_vs_master_title(slave_role, master_display)
        section_layout.addWidget(QLabel(header_text))

        pairing_plot = LivePlot()
        pairing_plot.setLabel("left", "HW TS Latency (us)")
        pairing_plot.setLabel("bottom", "Pair Index")

        global_ts_plot = LivePlot()
        global_ts_plot.setLabel("left", "Global TS Latency (us)")
        global_ts_plot.setLabel("bottom", "Pair Index")

        position_plot = LivePlot()
        position_plot.setLabel("left", "Optical Sync (ms)")
        position_plot.setLabel("bottom", "Pair Index")

        stats_panel = StatsPanel()
        stats_panel.setFixedWidth(220)
        stats_panel.add_section_header("Live Data")
        stats_panel.add_field("pair_index", "Pair Index")
        for spec in specs:
            identity = spec.stream_identity
            stats_panel.add_field("{}_pairing_gap_us".format(identity), "{} HW TS Latency (us)".format(identity))
            stats_panel.add_field("{}_global_ts_gap_us".format(identity), "{} Global TS Latency (us)".format(identity))
            stats_panel.add_field("{}_position_gap_ms".format(identity), "{} Optical Sync (ms)".format(identity))
        stats_panel.add_field("switch_time_ms", "LED Switch Time (ms)")
        stats_panel.add_section_header("Stats")
        stats_rows = []
        for spec in specs:
            identity = spec.stream_identity
            stats_rows.append(("{}_hw_ts_latency".format(identity), "{} HW TS Latency".format(identity)))
            stats_rows.append(("{}_global_ts_latency".format(identity), "{} Global TS Latency".format(identity)))
            stats_rows.append(("{}_optical_sync".format(identity), "{} Optical Sync".format(identity)))
        stats_panel.add_stats_table(stats_rows)
        if specs:
            stats_panel.set_value("switch_time_ms", specs[0].switch_time_ms)

        for index, spec in enumerate(specs):
            identity = spec.stream_identity
            color = CROSS_CAMERA_COLORS[index % len(CROSS_CAMERA_COLORS)]
            pairing_plot.add_series(identity, color=color, display_name=identity)
            global_ts_plot.add_series(identity, color=color, display_name=identity)
            position_plot.add_series(identity, color=color, display_name=identity)
            self._cross_pair_series_keys[(slave_camera_id, identity)] = identity
            self._cross_running_stats[(slave_camera_id, identity, "pairing_gap_us")] = RunningStats()
            self._cross_running_stats[(slave_camera_id, identity, "global_ts_gap_us")] = RunningStats()
            self._cross_running_stats[(slave_camera_id, identity, "position_gap_ms")] = RunningStats()

        self._slave_sections[slave_camera_id] = {
            "pairing_plot": pairing_plot, "global_ts_plot": global_ts_plot, "position_plot": position_plot,
            "stats_panel": stats_panel,
        }

        graphs_column = QVBoxLayout()
        graphs_column.addWidget(pairing_plot, stretch=1)
        graphs_column.addWidget(global_ts_plot, stretch=1)
        graphs_column.addWidget(position_plot, stretch=1)

        middle_row = QHBoxLayout()
        middle_row.addLayout(graphs_column, stretch=1)
        middle_row.addWidget(stats_panel)
        section_layout.addLayout(middle_row)

        return section_widget
```

Replace `_reset_cross_run_state` (currently lines 414-432):

```python
    def _reset_cross_run_state(self):
        """Mirrors CameraLiveSessionPanel.prepare_for_run's own per-run reset
        of ITS plots/stats, for the cross-camera widgets: without this,
        repeated Start-All clicks in the same page visit leave
        self._cross_running_stats' min/avg/std/max permanently polluted by
        every previous run's samples (min/max in particular never recover),
        and the plots keep drawing the new run's points (pair_index
        restarting from 1) on top of the previous run's leftover data. Also
        refreshes each slave's own "LED Switch Time (ms)" display field to
        the value THIS run will actually use - it was built once, at
        set_cameras() time, from each camera's own original per-camera
        config, which no longer matches once the operator confirms a
        per-test override that differs from it."""
        for section in self._slave_sections.values():
            section["pairing_plot"].clear_data()
            section["global_ts_plot"].clear_data()
            section["position_plot"].clear_data()
            section["stats_panel"].set_value("switch_time_ms", self._last_confirmed_switch_time_ms)
        for key in self._cross_running_stats:
            self._cross_running_stats[key] = RunningStats()
```

Modify `start_all_sessions`'s `thread_kwargs` dict (currently lines 485-496):

```python
            thread_kwargs = dict(
                pick_a=config["pick_a"], pick_b=config["pick_b"], camera_controls=config["camera_controls"],
                test_session=test_session,
                stream_a_xy=config["stream_a_xy"], stream_b_xy=config["stream_b_xy"],
                neighborhood_size=config["neighborhood_size"], scan_direction=config["scan_direction"],
                switch_time_ms=self._last_confirmed_switch_time_ms, display_stride=display_stride,
                position_gap_metric=position_gap_metric, dual_panel_config=config["dual_panel_config"],
                enable_depth_for_ir_sync=config["enable_depth_for_ir_sync"],
                output_dir=output_dir,
                position_gap_outlier_threshold_ms=config["position_gap_outlier_threshold_ms"],
                position_gap_outlier_max_snapshots=config["position_gap_outlier_max_snapshots"],
                # This page's own cameras always number >= 2 (a solo camera
                # routes to LiveSessionPage instead - see gui/main_window.py's
                # _on_start_multi_camera_session_requested) - global
                # timestamps are only ever needed for CrossCameraReconciler's
                # matching/Global TS Latency metric, so every camera in a
                # multi-camera run captures them; LiveSessionPage's own
                # start_session() never sets this.
                capture_global_ts=True,
            )
```

Replace `_on_cross_pair_ready` (currently lines 586-604):

```python
    def _on_cross_pair_ready(self, cross_row):
        # O(1) bookkeeping only - no add_point here. Fires unthrottled, once
        # per cross-camera match; plotting on this cadence caused a real GUI
        # freeze for the analogous intra-camera case (see CLAUDE.md's
        # row_ready/stats_ready cadence split). Both graphs' add_point calls,
        # and the actual stats-panel pushes, happen only in
        # _on_cross_stats_ready, below - RunningStats.update() here is the
        # one exception, matching CameraLiveSessionPanel.on_row_ready's own
        # unthrottled accumulation (cheap, no plotting).
        self._cross_rows.append(cross_row)

        key = (cross_row["slave_camera_id"], cross_row["stream_identity"])
        pairing_stats = self._cross_running_stats.get(key + ("pairing_gap_us",))
        if pairing_stats is not None and not cross_row.get("pairing_gap_us_excluded"):
            pairing_stats.update(cross_row["pairing_gap_us"])
        global_ts_stats = self._cross_running_stats.get(key + ("global_ts_gap_us",))
        if global_ts_stats is not None and not cross_row.get("global_ts_gap_us_excluded"):
            global_ts_stats.update(cross_row["global_ts_gap_us"])
        position_stats = self._cross_running_stats.get(key + ("position_gap_ms",))
        if (position_stats is not None and cross_row.get("position_gap_ms") is not None
                and not cross_row.get("position_gap_ms_excluded")):
            position_stats.update(cross_row["position_gap_ms"])
```

Replace `_on_cross_stats_ready` (currently lines 606-649):

```python
    def _on_cross_stats_ready(self, latest_by_pair):
        rows_by_slave = {}
        for (slave_camera_id, identity), row in latest_by_pair.items():
            rows_by_slave.setdefault(slave_camera_id, []).append((identity, row))

        for slave_camera_id, identity_rows in rows_by_slave.items():
            section = self._slave_sections.get(slave_camera_id)
            if section is None:
                continue
            stats_panel = section["stats_panel"]
            pairing_plot = section["pairing_plot"]
            global_ts_plot = section["global_ts_plot"]
            position_plot = section["position_plot"]

            # A slave sharing multiple identities can have each identity's
            # own match complete independently, landing different
            # pair_index values in the same tick - show the most recently
            # completed match across all of this slave's identities as the
            # single "is this still updating" heartbeat.
            stats_panel.set_value("pair_index", max(row["pair_index"] for _, row in identity_rows))

            for identity, row in identity_rows:
                series_key = self._cross_pair_series_keys.get((slave_camera_id, identity))
                if series_key is None:
                    continue

                stats_panel.set_value("{}_pairing_gap_us".format(identity), row["pairing_gap_us"])
                pairing_value = row["pairing_gap_us"]
                if row.get("pairing_gap_us_excluded"):
                    pairing_value = float("nan")
                pairing_plot.add_point(series_key, row["pair_index"], pairing_value)

                stats_panel.set_value("{}_global_ts_gap_us".format(identity), row["global_ts_gap_us"])
                global_ts_value = row["global_ts_gap_us"]
                if row.get("global_ts_gap_us_excluded"):
                    global_ts_value = float("nan")
                global_ts_plot.add_point(series_key, row["pair_index"], global_ts_value)

                if row.get("position_gap_ms") is not None:
                    stats_panel.set_value("{}_position_gap_ms".format(identity), row["position_gap_ms"])
                    position_value = row["position_gap_ms"]
                    if row.get("position_gap_ms_excluded"):
                        position_value = float("nan")
                    position_plot.add_point(series_key, row["pair_index"], position_value)

                pairing_stats = self._cross_running_stats.get((slave_camera_id, identity, "pairing_gap_us"))
                if pairing_stats is not None:
                    self._push_running_stats(stats_panel, "{}_hw_ts_latency".format(identity), pairing_stats)
                global_ts_stats = self._cross_running_stats.get((slave_camera_id, identity, "global_ts_gap_us"))
                if global_ts_stats is not None:
                    self._push_running_stats(stats_panel, "{}_global_ts_latency".format(identity), global_ts_stats)
                position_stats = self._cross_running_stats.get((slave_camera_id, identity, "position_gap_ms"))
                if position_stats is not None:
                    self._push_running_stats(stats_panel, "{}_optical_sync".format(identity), position_stats)
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_cross_camera_reconciler.py tests/gui/pages/test_multi_camera_live_session_page.py -v`
Expected: PASS (every test in both files).

- [ ] **Step 9: Run the full suite**

Run: `.venv\Scripts\python.exe -m pytest -v`
Expected: PASS (every test in the project).

- [ ] **Step 10: Commit**

```bash
git add engine/cross_camera_reconciler.py gui/pages/multi_camera_live_session_page.py \
        tests/engine/test_cross_camera_reconciler.py tests/gui/pages/test_multi_camera_live_session_page.py
git commit -m "feat: match cross-camera pairs on RealSense global timestamp; add Global TS Latency metric"
```

---

### Task 4: Static plot export gains the Global TS Latency line

**Files:**
- Modify: `domain/plot_export.py` (`_build_cross_camera_figure`)
- Test: `tests/domain/test_plot_export.py`

**Interfaces:**
- Consumes: cross-row dicts now carrying `global_ts_gap_us`/`global_ts_gap_us_excluded` (Task 3) - this task's own tests build these dicts directly via `_cross_row()`, no live coupling to Task 3's other files.
- Produces: no new public function - `_build_cross_camera_figure`'s pairing axis (`fig.axes[0]`) now carries 2 lines per stream identity instead of 1.

- [ ] **Step 1: Write the failing tests**

In `tests/domain/test_plot_export.py`, replace `_cross_row` (currently lines 82-90):

```python
def _cross_row(pair_index, slave_camera_id="cam2", stream_identity="infrared1",
                pairing_gap_us=-10.0, excluded=False,
                global_ts_gap_us=-10.0, global_ts_gap_us_excluded=False,
                position_gap_ms=1.0, position_gap_ms_excluded=False):
    return {
        "pair_index": pair_index, "master_camera_id": "cam1", "slave_camera_id": slave_camera_id,
        "stream_identity": stream_identity,
        "pairing_gap_us": pairing_gap_us, "pairing_gap_us_excluded": excluded,
        "global_ts_gap_us": global_ts_gap_us, "global_ts_gap_us_excluded": global_ts_gap_us_excluded,
        "position_gap_ms": position_gap_ms, "position_gap_ms_excluded": position_gap_ms_excluded,
    }
```

Replace `test_export_cross_camera_plot_draws_one_line_per_identity` (currently lines 111-128):

```python
def test_export_cross_camera_plot_draws_one_line_per_identity():
    # Rows are pre-filtered to ONE slave by the caller (gui/pages/
    # multi_camera_live_session_page.py) - a single figure can still have
    # multiple lines if that one slave shares multiple stream identities
    # with master. 4, not 2: each identity now draws BOTH an HW TS Latency
    # line and a Global TS Latency line on this same axis (see
    # test_export_cross_camera_plot_draws_global_ts_gap_as_dashed_line_same_color_as_hw_ts_latency
    # for how they're told apart).
    rows = [
        _cross_row(0, stream_identity="infrared1", pairing_gap_us=-10.0),
        _cross_row(1, stream_identity="infrared1", pairing_gap_us=-11.0),
        _cross_row(0, stream_identity="color", pairing_gap_us=5.0),
    ]

    import matplotlib.pyplot as plt
    from domain.plot_export import _build_cross_camera_figure
    fig = _build_cross_camera_figure(rows, title="Slave 1")
    lines = fig.axes[0].get_lines()

    assert len(lines) == 4
    plt.close(fig)
```

Add these two new tests right after it:

```python
def test_export_cross_camera_plot_draws_global_ts_gap_as_dashed_line_same_color_as_hw_ts_latency():
    rows = [_cross_row(0, stream_identity="infrared1", pairing_gap_us=-10.0, global_ts_gap_us=-2.0)]

    import matplotlib.pyplot as plt
    from domain.plot_export import _build_cross_camera_figure
    fig = _build_cross_camera_figure(rows, title="Slave 1")
    hw_line, global_line = fig.axes[0].get_lines()

    assert hw_line.get_linestyle() == "-"
    assert global_line.get_linestyle() == "--"
    assert hw_line.get_color() == global_line.get_color()
    assert global_line.get_ydata()[0] == -2.0
    plt.close(fig)


def test_export_cross_camera_plot_nans_out_excluded_global_ts_gap_values():
    rows = [_cross_row(0, global_ts_gap_us=99999.0, global_ts_gap_us_excluded=True)]

    import matplotlib.pyplot as plt
    from domain.plot_export import _build_cross_camera_figure
    fig = _build_cross_camera_figure(rows, title="Slave 1")
    _, global_line = fig.axes[0].get_lines()

    assert math.isnan(global_line.get_ydata()[0])
    plt.close(fig)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/domain/test_plot_export.py -v`
Expected: FAIL - `test_export_cross_camera_plot_draws_one_line_per_identity` fails with `assert 2 == 4`; the two new tests fail with `ValueError: not enough values to unpack (expected 2, got 1)` (only 1 line currently drawn per identity).

- [ ] **Step 3: Implement**

In `domain/plot_export.py`, replace the per-identity loop and `pairing_ax.set_ylabel` line inside `_build_cross_camera_figure` (currently lines 164-178):

```python
    for index, identity in enumerate(sorted(groups.keys())):
        pair_rows = groups[identity]
        pair_indices = [row["pair_index"] for row in pair_rows]
        color = CROSS_CAMERA_COLORS[index % len(CROSS_CAMERA_COLORS)]

        pairing_values = [_to_plot_value(row.get("pairing_gap_us"), row.get("pairing_gap_us_excluded"))
                           for row in pair_rows]
        pairing_ax.plot(pair_indices, pairing_values, label=identity, color=color)

        # Same color as this identity's own HW TS Latency line, dashed
        # instead of solid - lets the operator directly compare the two
        # latency measures for the same pairs on one chart (the whole
        # point of this metric: Global TS Latency should stay near zero
        # with no drift, unlike its HW-ts counterpart).
        global_ts_values = [_to_plot_value(row.get("global_ts_gap_us"), row.get("global_ts_gap_us_excluded"))
                             for row in pair_rows]
        pairing_ax.plot(pair_indices, global_ts_values, label="{} (global)".format(identity),
                         color=color, linestyle="--")

        position_values = [_to_plot_value(row.get("position_gap_ms"), row.get("position_gap_ms_excluded"))
                            for row in pair_rows]
        position_ax.plot(pair_indices, position_values, label=identity, color=color)

    pairing_ax.set_ylabel("HW TS / Global TS Latency (us)")
    _style_axis(pairing_ax)
```

(The `position_ax.set_ylabel`/`_style_axis(position_ax)` lines right after stay unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/domain/test_plot_export.py -v`
Expected: PASS (every test in this file).

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python.exe -m pytest -v`
Expected: PASS (every test in the project).

- [ ] **Step 6: Commit**

```bash
git add domain/plot_export.py tests/domain/test_plot_export.py
git commit -m "feat: static cross-camera plot export draws Global TS Latency alongside HW TS Latency"
```

---

## Self-Review

**Spec coverage:**
- Section 1 (`ContinuousCapture` opt-in `capture_global_ts` + `_read_global_ts_us`, fail-loudly domain check) - Task 1.
- Section 1's plumbing through `FramePairSample`/`AcquisitionLoop`/`TestSession`'s row - Task 2.
- Section 2 (only multi-camera runs set `capture_global_ts=True`; `SessionEngineThread`'s own constructor param) - Task 2 (`SessionEngineThread`) + Task 3 (`MultiCameraLiveSessionPage`'s `thread_kwargs`).
- Section 3 (`CrossCameraReconciler`'s join switches to global ts; `pairing_gap_us`'s HW-offset calibration preserved as a reporting step; new `global_ts_gap_metric`/`global_ts_gap_us`) - Task 3.
- Section 4 (live GUI: third plot + stats fields per slave section) - Task 3.
- Section 5 (static plot export: second line, same axis, dashed) - Task 4.
- Section 6 (CSV: no changes needed) - confirmed by scope: no task touches `domain/csv_export.py`.
- "What doesn't change" (single-camera path, `PositionGapMetric`, `pairing_gap_us` semantics, `MultiCameraSessionController`) - confirmed: no task modifies `gui/pages/live_session_page.py`, `engine/metrics.py`'s `PositionGapMetric`, or `engine/multi_camera_session.py`.

**Placeholder scan:** No TBD/TODO/"add appropriate"/"similar to Task N" phrases - every step has complete, real code, including every existing test function this plan modifies (given in full, not referenced by name only).

**Type consistency:** `capture_global_ts` is the exact same parameter name on `ContinuousCapture.__init__` (Task 1), `SessionEngineThread.__init__` (Task 2), and `thread_kwargs`/`fake_threads[...].kwargs["capture_global_ts"]` (Task 3). `global_ts_gap_us`/`global_ts_gap_us_excluded`/`global_ts_gap_us_exclude_reason` are the exact same three key names used in `CrossCameraReconciler._build_cross_row` (Task 3), the GUI page's `_on_cross_pair_ready`/`_on_cross_stats_ready` (Task 3), and `domain/plot_export.py`'s `_build_cross_camera_figure` (Task 4). `stream_a_global_ts_us`/`stream_b_global_ts_us` are consistent across `FramePairSample`, `TestSession.process_pair`'s row, `_frame_pairs_with_brightness`'s yielded tuple, and every test file that constructs rows/samples by hand (Tasks 1-3). `global_ts_gap_metric` is the same field name in `CrossCameraPairSpec`'s dataclass definition and `build_cross_camera_pair_specs`'s construction (Task 3).
