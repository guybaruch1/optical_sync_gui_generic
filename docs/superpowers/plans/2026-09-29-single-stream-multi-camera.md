# Single-Stream Cameras in the Multi-Camera Test - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a camera in a multi-camera run use ONE stream (`pick_b = None`), measured only against the other cameras on the Cross-Camera Sync tab, with a slim per-camera tab.

**Architecture:** `pick_b is None` is the single "single-stream" signal, threaded through the existing A/B code (settings parsing -> capture -> wizard pages -> main window -> session metrics -> multi-camera page). A new `LedDetectionMetric` replaces the intra-camera metrics for single-stream cameras and feeds the cross-camera reconciler the same `stream_a_last_led` key; the reconciler falls back to its `led_detection_*` exclusion keys when a row has no `position_gap_ms_*` keys. Two-stream behavior is unchanged.

**Tech Stack:** Python 3.13, PySide6, pyrealsense2, numpy, pyqtgraph, matplotlib, pytest.

**Spec:** `docs/superpowers/specs/2026-09-29-single-stream-multi-camera-design.md`

## Global Constraints

- Run tests with `.venv\Scripts\python.exe -m pytest ...` from the repo root; widget tests use the session `qapp` fixture from `tests/conftest.py`.
- Single source of truth for single-stream: `pick_b is None`. No new flag fields in config dicts, except `CameraSummary.single_stream` (display/gating only) and `CameraLiveSessionPanel(single_stream=...)` (layout).
- Two-stream behavior, CSV columns and existing tests must stay unchanged. The full suite must pass at the end of every task.
- Single-stream tests are defined in settings.yaml with no `stream_b_identity` and `sensor_options` entries with only `stream_a`.
- Single-stream cameras never use dual-panel mode (checkbox disabled + unticked).
- A run of exactly one camera that is single-stream cannot start. Hub tooltip text, verbatim: `"A single-stream camera needs at least one other camera to compare against."`
- `camera_sync.enable_depth_for_ir_sync` still applies to an infrared single pick.
- Match surrounding code style: long explanatory comments where the code does something non-obvious, `"...".format()` strings, no f-strings in production code except where the file already uses them.
- Commit after each task on branch `feature/single-stream-multi-camera`, ending messages with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Stale stream-B state from a previous camera's sub-flow** (e.g. `gui_state.stream_b_roi`) leaking into a single-stream camera's committed config -> must be `None`. Test in Task 10.
2. **Switching the Test combo between a single-stream and a two-stream test** on Stream Config -> dual-panel checkbox and Exposure B re-enable/re-show for the two-stream test. Test in Task 6.
3. **Single-stream master vs a two-stream slave whose matching stream is its stream_b** (e.g. master "Color only", slave IR-vs-RGB) -> cross Optical Sync computed with per-camera roles. Test in Task 4.
4. **Removing the other camera from a two-camera run** that leaves one single-stream camera -> Start disabled with the tooltip. Test in Task 10.
5. **GMSL TSC fps check with single-stream cameras** -> must not crash on `pick_b = None`. Test in Task 10.

---

## File Structure

| File | Change |
|---|---|
| `engine/streams.py` | optional stream B in test parsing/resolution, `resolve_and_group`, `exposure_for_group`, `ContinuousCapture`, `_read_global_ts_us` |
| `settings.yaml` | example "IR1 only" / "Color only" tests |
| `domain/calibration.py` | single-slug `update_config_leds` / `load_led_positions` |
| `engine/metrics.py` | new `LedDetectionMetric` |
| `engine/cross_camera_reconciler.py` | exclusion fallback to `led_detection_*` |
| `engine/dual_panel_control.py` | `single_panel_stream_for_picks(pick_a, None)` |
| `domain/realsense_utils.py` | new `draw_single_stream_overlay` |
| `engine/stream_preview_thread.py` | single-stream preview |
| `gui/pages/stream_config_page.py` | single-stream option label, hidden Exposure B, disabled dual panel, emits `None` |
| `gui/pages/roi_select_page.py` | one ROI |
| `gui/pages/calibration_page.py` | one-stream detection |
| `gui/pages/threshold_tuning_page.py`, `engine/threshold_preview_thread.py` | stream B hidden / skipped |
| `gui/pages/camera_hub_page.py` | `CameraSummary.single_stream`, Start gating |
| `gui/main_window.py` | sub-flow, gating, conflicts, GMSL, single-panel target |
| `engine/session_engine.py` | `_emit_frames` skips B |
| `gui/widgets/camera_live_session_panel.py`, `domain/plot_export.py` | single-stream layout/snapshots/plot |
| `gui/pages/multi_camera_live_session_page.py` | identities, metric choice, panel mode |
| `CLAUDE.md`, `README.md` | docs |

---

### Task 1: settings.yaml single-stream tests (parse + resolve)

**Files:**
- Modify: `engine/streams.py` (`parse_camera_tests_config`, `resolve_camera_tests`)
- Modify: `settings.yaml` (`camera.stream_options`)
- Test: `tests/engine/test_streams.py`

**Interfaces:**
- Produces: parsed test dict `stream_b_identity` is `None` for a single-stream test, each `sensor_options` entry has `"stream_b": None`. `resolve_camera_tests` options are `{"pick_a": <option>, "pick_b": None}` for single-stream tests.

- [ ] **Step 1: Write the failing tests** (append near the other `parse_camera_tests_config` / `resolve_camera_tests` tests)

```python
def test_parse_camera_tests_config_accepts_a_single_stream_test():
    raw = [{
        "test_name": "IR1 only",
        "stream_a_identity": {"stream_type": "infrared", "stream_index": 1},
        "sensor_options": [{"stream_a": {"width": 1280, "height": 720, "fps": 30, "format": "y8"}}],
    }]

    parsed = parse_camera_tests_config(raw)

    assert parsed == [{
        "test_name": "IR1 only",
        "stream_a_identity": {"stream_type": rs.stream.infrared, "stream_index": 1},
        "stream_b_identity": None,
        "sensor_options": [{
            "stream_a": {"width": 1280, "height": 720, "fps": 30, "format": rs.format.y8},
            "stream_b": None,
        }],
    }]


def test_parse_camera_tests_config_rejects_stream_b_side_in_a_single_stream_test():
    raw = [{
        "test_name": "IR1 only",
        "stream_a_identity": {"stream_type": "infrared", "stream_index": 1},
        "sensor_options": [{"stream_a": {"width": 1280, "height": 720, "fps": 30, "format": "y8"},
                            "stream_b": {"width": 1280, "height": 720, "fps": 30, "format": "y8"}}],
    }]
    with pytest.raises(ValueError, match="IR1 only"):
        parse_camera_tests_config(raw)


def test_parse_camera_tests_config_rejects_missing_stream_b_side_in_a_two_stream_test():
    raw = [_raw_test(
        "IR vs RGB", {"stream_type": "infrared", "stream_index": 1}, {"stream_type": "color", "stream_index": 0},
        [{"stream_a": {"width": 1280, "height": 720, "fps": 30, "format": "y8"}}],
    )]
    with pytest.raises(ValueError, match="IR vs RGB"):
        parse_camera_tests_config(raw)


def test_resolve_camera_tests_single_stream_test_yields_pick_b_none():
    device_options = [_device_option(rs.stream.infrared, 1, 1280, 720, 30, rs.format.y8)]
    parsed_tests = [{
        "test_name": "IR1 only",
        "stream_a_identity": {"stream_type": rs.stream.infrared, "stream_index": 1},
        "stream_b_identity": None,
        "sensor_options": [
            {"stream_a": {"width": 1280, "height": 720, "fps": 30, "format": rs.format.y8}, "stream_b": None},
            {"stream_a": {"width": 640, "height": 480, "fps": 30, "format": rs.format.y8}, "stream_b": None},
        ],
    }]

    resolved = resolve_camera_tests(device_options, parsed_tests)

    assert resolved == [{"test_name": "IR1 only", "options": [{"pick_a": device_options[0], "pick_b": None}]}]
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_streams.py -k "single_stream or missing_stream_b_side" -v`
Expected: FAIL (KeyError on `stream_b_identity` / no ValueError raised).

- [ ] **Step 3: Implement** - replace the parsing loop at the end of `parse_camera_tests_config` and extend its docstring with one sentence ("A test with no stream_b_identity is a single-stream test: stream_b_identity is None and every sensor_options entry must have only a stream_a side (its "stream_b" is None) - used by multi-camera runs that compare one stream per camera across cameras.")

```python
    parsed = []
    for test in raw_tests:
        single_stream = test.get("stream_b_identity") is None
        sensor_options = []
        for entry in test["sensor_options"]:
            has_b = entry.get("stream_b") is not None
            if single_stream and has_b:
                raise ValueError(
                    "settings.yaml camera.stream_options: test {!r} has no stream_b_identity "
                    "(a single-stream test) but one of its sensor_options entries has a "
                    "stream_b side.".format(test["test_name"])
                )
            if not single_stream and not has_b:
                raise ValueError(
                    "settings.yaml camera.stream_options: test {!r} has a stream_b_identity but "
                    "one of its sensor_options entries has no stream_b side.".format(test["test_name"])
                )
            sensor_options.append({
                "stream_a": parse_side(entry["stream_a"]),
                "stream_b": parse_side(entry["stream_b"]) if has_b else None,
            })
        parsed.append({
            "test_name": test["test_name"],
            "stream_a_identity": parse_identity(test["stream_a_identity"]),
            "stream_b_identity": None if single_stream else parse_identity(test["stream_b_identity"]),
            "sensor_options": sensor_options,
        })
    return parsed
```

In `resolve_camera_tests`, replace the inner loop body:

```python
        for entry in test["sensor_options"]:
            pick_a = _find_matching_option(device_options, {**test["stream_a_identity"], **entry["stream_a"]})
            if test.get("stream_b_identity") is None:
                # Single-stream test - only stream A has to exist on this device.
                if pick_a is not None:
                    resolved_options.append({"pick_a": pick_a, "pick_b": None})
                continue
            pick_b = _find_matching_option(device_options, {**test["stream_b_identity"], **entry["stream_b"]})
            if pick_a is not None and pick_b is not None:
                resolved_options.append({"pick_a": pick_a, "pick_b": pick_b})
```

Add to the docstring: `"pick_b" is None for a single-stream test (see parse_camera_tests_config).`

- [ ] **Step 4: Add example tests to `settings.yaml`** - append to BOTH the `"RealSense D455"` and `"RealSense D585 Prototype"` lists (use each file block's own brace-spacing style), and add a comment above `stream_options:`'s first camera explaining the single-stream shape:

```yaml
    # A test with no stream_b_identity (and only a stream_a side in each
    # sensor_options entry) is SINGLE-STREAM: the camera contributes one
    # stream to a multi-camera run, compared only against the other cameras
    # (Cross-Camera Sync). Needs at least one other camera to start.
      - test_name: "IR1 only"
        stream_a_identity: {stream_type: infrared, stream_index: 1}
        sensor_options:
          - stream_a: {width: 1280, height: 720, fps: 30, format: y8}
      - test_name: "Color only"
        stream_a_identity: {stream_type: color, stream_index: 0}
        sensor_options:
          - stream_a: {width: 1280, height: 720, fps: 30, format: bgr8}
          - stream_a: {width: 640, height: 480, fps: 30, format: bgr8}
```

(640x480 is offered because a genlock slave's color stream is capped - see `camera.inter_cam_sync.max_slave_color_resolution`.)

- [ ] **Step 5: Run the stream tests and a settings smoke check**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_streams.py -v`
Expected: PASS.
Run: `.venv\Scripts\python.exe -c "import yaml; from engine.streams import parse_camera_tests_config; s=yaml.safe_load(open('settings.yaml')); [parse_camera_tests_config(v) for v in s['camera']['stream_options'].values()]; print('ok')"`
Expected: `ok`.

- [ ] **Step 6: Commit**

```bash
git add engine/streams.py settings.yaml tests/engine/test_streams.py
git commit -m "feat: single-stream tests in settings.yaml camera.stream_options"
```

---

### Task 2: Capture layer accepts `pick_b = None`

**Files:**
- Modify: `engine/streams.py` (`resolve_and_group`, `exposure_for_group`, `_read_global_ts_us`, `ContinuousCapture`)
- Test: `tests/engine/test_streams.py`

**Interfaces:**
- Produces: `resolve_and_group(device, pick_a, pick_b=None)` -> `[(sensor_a, [profile_a])]` when `pick_b is None`. `exposure_for_group(profiles, pick_a, None, ea, eb)` -> `ea`. `_read_global_ts_us(frame_a, frame_b=None)` -> `(ts_a_us, None)` when `frame_b is None`. `ContinuousCapture(serial, pick_a, None, ...)` enables only stream A (+ depth when applicable) and `frames_with_diagnostics()` yields `(image_a, None, ts_a, None, num_a, None, global_ts_a, None)`.

- [ ] **Step 1: Write the failing tests** (append; reuse `FakeProfile2`, `FakeSensor2`, `FakeDevice`, `_ir_pick`, `_color_pick`, `_FakeGlobalTsFrame`)

```python
def test_resolve_and_group_single_pick_returns_one_group_with_one_profile():
    ir_profile = FakeProfile2(rs.stream.infrared, 1, rs.format.y8, 1280, 720, 30)
    ir_sensor = FakeSensor2(profiles=[ir_profile])
    device = FakeDevice([ir_sensor])
    pick_a = {"sensor_index": 0, "stream_type": rs.stream.infrared, "stream_index": 1,
              "format": rs.format.y8, "width": 1280, "height": 720, "fps": 30}

    groups = resolve_and_group(device, pick_a, None)

    assert groups == [(ir_sensor, [ir_profile])]


def test_exposure_for_group_with_no_pick_b_returns_exposure_a():
    ir_profile = FakeProfile2(rs.stream.infrared, 1, rs.format.y8, 1280, 720, 30)
    pick_a = {"stream_type": rs.stream.infrared, "stream_index": 1,
              "format": rs.format.y8, "width": 1280, "height": 720, "fps": 30}
    assert exposure_for_group([ir_profile], pick_a, None, exposure_a=1111, exposure_b=None) == 1111


def test_read_global_ts_us_with_only_frame_a():
    frame_a = _FakeGlobalTsFrame(1000.5, rs.timestamp_domain.global_time)
    assert _read_global_ts_us(frame_a, None) == (1_000_500.0, None)


def test_depth_sync_stream_for_a_single_infrared_pick():
    capture = ContinuousCapture("SN1", _ir_pick(width=848, height=480, fps=60), None, enable_depth_for_ir_sync=True)
    assert capture._depth_sync_stream() == (848, 480, 60)


def test_depth_sync_stream_is_none_for_a_single_color_pick():
    capture = ContinuousCapture("SN1", _color_pick(), None, enable_depth_for_ir_sync=True)
    assert capture._depth_sync_stream() is None


class _RecordingConfig:
    def __init__(self):
        self.enabled = []
    def enable_device(self, serial):
        pass
    def enable_stream(self, *args):
        self.enabled.append(args)


def test_build_config_single_pick_enables_only_stream_a_and_depth():
    capture = ContinuousCapture("SN1", _ir_pick(), None, enable_depth_for_ir_sync=True)
    with patch("engine.streams.rs.config", _RecordingConfig):
        config = capture._build_config()
    stream_types = [args[0] for args in config.enabled]
    assert stream_types == [rs.stream.infrared, rs.stream.depth]


class _FakeStreamFrame:
    def __init__(self, width, height, ts_us, frame_number, global_ts_ms):
        self._data = bytes(width * height)
        self._ts_us = ts_us
        self._frame_number = frame_number
        self._global_ts_ms = global_ts_ms
    def __bool__(self):
        return True
    def get_data(self):
        return self._data
    def supports_frame_metadata(self, metadata):
        return True
    def get_frame_metadata(self, metadata):
        return self._ts_us
    def get_frame_number(self):
        return self._frame_number
    def get_timestamp(self):
        return self._global_ts_ms
    def get_frame_timestamp_domain(self):
        return rs.timestamp_domain.global_time


class _FakeFrameset:
    def __init__(self, ir_frame):
        self._ir_frame = ir_frame
    def get_infrared_frame(self, index):
        return self._ir_frame
    def get_color_frame(self, index):
        raise AssertionError("single-stream capture must never ask for a second stream")


class _FakeRunningPipeline:
    def __init__(self, frameset):
        self._frameset = frameset
    def wait_for_frames(self):
        return self._frameset


def test_frames_with_diagnostics_single_pick_yields_none_for_stream_b():
    pick_a = _ir_pick(width=4, height=2)
    capture = ContinuousCapture("SN1", pick_a, None, capture_global_ts=True)
    capture._pipeline = _FakeRunningPipeline(_FakeFrameset(_FakeStreamFrame(4, 2, 1234.0, 7, 5.0)))

    image_a, image_b, ts_a, ts_b, num_a, num_b, global_a, global_b = next(capture.frames_with_diagnostics())

    assert image_a.shape == (2, 4)
    assert (image_b, ts_b, num_b, global_b) == (None, None, None, None)
    assert (ts_a, num_a, global_a) == (1234.0, 7, 5000.0)
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_streams.py -k "single or no_pick_b or only_frame_a" -v`
Expected: FAIL (TypeError / AttributeError on `None`).

- [ ] **Step 3: Implement** in `engine/streams.py`:

`resolve_and_group` - signature `def resolve_and_group(device, pick_a, pick_b=None):`; guard the same-stream check with `if pick_b is not None and ...`; after `sensor_a, profile_a = sensor_and_profile_for(pick_a)` add:

```python
    if pick_b is None:
        # Single-stream camera (a single-stream settings.yaml test) - one
        # sensor, one profile.
        return [(sensor_a, [profile_a])]
```

and add to its docstring: `pick_b may be None (single-stream camera) - then only pick_a's own sensor/profile is returned.`

`exposure_for_group` - change `has_b` to `has_b = pick_b is not None and any(_pick_matches(p, pick_b) for p in profiles)`.

`_read_global_ts_us`:

```python
def _read_global_ts_us(frame_a, frame_b=None):
    ...  # existing docstring, plus: "frame_b is None for a single-stream capture - its value is then None too."
    domain = rs.timestamp_domain.global_time
    frames = [frame for frame in (frame_a, frame_b) if frame is not None]
    if any(frame.get_frame_timestamp_domain() != domain for frame in frames):
        raise RuntimeError(...)  # existing message unchanged
    global_ts_b = frame_b.get_timestamp() * 1000.0 if frame_b is not None else None
    return frame_a.get_timestamp() * 1000.0, global_ts_b
```

`ContinuousCapture` - add a helper and use it in `_depth_sync_stream` (`for pick in self._active_picks():`) and `_build_config` (`for pick in self._active_picks():`):

```python
    def _active_picks(self):
        # pick_b is None for a single-stream camera (a single-stream
        # settings.yaml test) - only stream A is enabled then.
        return [pick for pick in (self.pick_a, self.pick_b) if pick is not None]
```

Replace `frames_with_diagnostics`:

```python
    def frames_with_diagnostics(self):
        single_stream = self.pick_b is None
        while True:
            frameset = self._pipeline.wait_for_frames()
            frame_a = self._get_frame(frameset, self.pick_a)
            frame_b = None if single_stream else self._get_frame(frameset, self.pick_b)
            if not frame_a or (not single_stream and not frame_b):
                continue

            metadata = rs.frame_metadata_value.frame_timestamp
            frames = [frame for frame in (frame_a, frame_b) if frame is not None]
            if not all(frame.supports_frame_metadata(metadata) for frame in frames):
                raise RuntimeError(...)  # existing message unchanged

            image_a = decode_frame(bytes(frame_a.get_data()), self.pick_a["format"], self.pick_a["width"], self.pick_a["height"])
            ts_a = frame_a.get_frame_metadata(metadata)
            num_a = frame_a.get_frame_number()
            if single_stream:
                image_b = ts_b = num_b = None
            else:
                image_b = decode_frame(bytes(frame_b.get_data()), self.pick_b["format"], self.pick_b["width"], self.pick_b["height"])
                ts_b = frame_b.get_frame_metadata(metadata)
                num_b = frame_b.get_frame_number()

            if self.capture_global_ts:
                global_ts_a, global_ts_b = _read_global_ts_us(frame_a, frame_b)
            else:
                global_ts_a, global_ts_b = None, None

            yield image_a, image_b, ts_a, ts_b, num_a, num_b, global_ts_a, global_ts_b
```

- [ ] **Step 4: Run tests**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_streams.py tests/gui/pages/test_roi_select_page.py -v`
Expected: PASS (ROI Select's `_apply_camera_controls` now works with `pick_b=None` via `exposure_for_group`).

- [ ] **Step 5: Commit**

```bash
git add engine/streams.py tests/engine/test_streams.py
git commit -m "feat: capture layer supports a single stream (pick_b=None)"
```

---

### Task 3: `LedDetectionMetric` and single-slug calibration storage

**Files:**
- Modify: `engine/metrics.py` (new class after `PositionGapMetric`)
- Modify: `domain/calibration.py` (`update_config_leds`, `load_led_positions`)
- Test: `tests/engine/test_metrics.py`, `tests/engine/test_test_session.py`, `tests/domain/test_calibration.py`

**Interfaces:**
- Produces: `LedDetectionMetric(stream_a_threshold, warmup_pairs_to_skip)`, `name = "led_detection"`; `update()` returns `MetricResult` with `value` = detected LED index or None, `extra={"stream_a_last_led": idx}` (absent on `no_led_data`), exclusion reasons in order `no_led_data`, `miss`, `frame_drop`, `warmup`. Attributes `last_stream_a_on_mask` (updated) and `last_stream_b_on_mask` (always None).
- Produces: `update_config_leds(config_path, camera_name, stream_a_slug, stream_a_positions, stream_a_res, stream_b_slug=None, stream_b_positions=None, stream_b_res=None)`; `load_led_positions(config_path, camera_name, stream_a_slug, stream_a_res, stream_b_slug=None, stream_b_res=None)` -> `(positions_a, None)` when `stream_b_slug is None`.

- [ ] **Step 1: Write the failing tests**

`tests/engine/test_metrics.py` (add `LedDetectionMetric` to the import list):

```python
def _single_sample(pair_index=0, bright=None, frame_drop=False):
    sample = FramePairSample(pair_index=pair_index, stream_a_ts_us=0.0, stream_b_ts_us=None,
                             stream_a_bright=bright, stream_b_bright=None)
    sample.stream_a_frame_drop = frame_drop
    return sample


def test_led_detection_metric_reports_detected_led_index():
    metric = LedDetectionMetric(stream_a_threshold=np.full(4, 100.0), warmup_pairs_to_skip=0)
    result = metric.update(_single_sample(bright=np.array([0.0, 200.0, 200.0, 0.0])))
    assert result.name == "led_detection"
    assert result.value == 2
    assert result.excluded is False
    assert result.extra == {"stream_a_last_led": 2}
    assert list(metric.last_stream_a_on_mask) == [False, True, True, False]
    assert metric.last_stream_b_on_mask is None


def test_led_detection_metric_no_led_data_has_no_extra():
    metric = LedDetectionMetric(stream_a_threshold=np.full(4, 100.0), warmup_pairs_to_skip=0)
    result = metric.update(_single_sample(bright=None))
    assert (result.value, result.excluded, result.exclude_reason, result.extra) == (None, True, "no_led_data", None)


def test_led_detection_metric_miss_when_nothing_on():
    metric = LedDetectionMetric(stream_a_threshold=np.full(3, 100.0), warmup_pairs_to_skip=0)
    result = metric.update(_single_sample(bright=np.zeros(3)))
    assert (result.value, result.exclude_reason, result.extra) == (None, "miss", {"stream_a_last_led": None})


def test_led_detection_metric_frame_drop_keeps_value_and_wins_over_warmup():
    metric = LedDetectionMetric(stream_a_threshold=np.full(3, 100.0), warmup_pairs_to_skip=5)
    result = metric.update(_single_sample(bright=np.array([200.0, 0.0, 0.0]), frame_drop=True))
    assert (result.value, result.excluded, result.exclude_reason) == (0, True, "frame_drop")


def test_led_detection_metric_flags_warmup_pairs():
    metric = LedDetectionMetric(stream_a_threshold=np.full(3, 100.0), warmup_pairs_to_skip=1)
    bright = np.array([200.0, 0.0, 0.0])
    assert metric.update(_single_sample(0, bright)).exclude_reason == "warmup"
    assert metric.update(_single_sample(1, bright)).excluded is False
```

`tests/engine/test_test_session.py`:

```python
def test_process_pair_single_stream_never_flags_a_stream_b_drop():
    session = TestSession(TestSessionConfig(metrics=[FakeMetric()], stream_a_fps=30, stream_b_fps=None,
                                            frame_drop_threshold_factor=1.5))
    session.start()
    rows = [session.process_pair(FramePairSample(pair_index=i, stream_a_ts_us=i * 33_333.0, stream_b_ts_us=None))
            for i in range(3)]
    assert all(row["stream_b_frame_drop"] is False for row in rows)
    assert all(row["stream_b_ts_us"] is None for row in rows)
    assert all(row["stream_a_frame_drop"] is False for row in rows)
```

`tests/domain/test_calibration.py`:

```python
def test_update_config_leds_single_stream_writes_only_stream_a(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"leds": {"Test Camera": {"color": {"frame_width": 1, "frame_height": 1, "positions": {}}}}}))

    update_config_leds(str(config_path), "Test Camera", "infrared1", {"0": [1.0, 2.0, 255.0, 100.0, 177.5]}, (1280, 720))

    camera_entry = yaml.safe_load(config_path.read_text())["leds"]["Test Camera"]
    assert set(camera_entry) == {"color", "infrared1"}  # existing slug untouched, no "None" slug written


def test_load_led_positions_single_stream_returns_none_for_stream_b(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"leds": {"Test Camera": {
        "infrared1": {"frame_width": 1280, "frame_height": 720, "positions": {"0": [1.0, 2.0, 255.0, 100.0, 177.5]}},
    }}}))

    positions_a, positions_b = load_led_positions(str(config_path), "Test Camera", "infrared1", (1280, 720))

    assert positions_a["0"] == [1.0, 2.0, 255.0, 100.0, 177.5]
    assert positions_b is None


def test_load_led_positions_single_stream_raises_when_uncalibrated(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"leds": {"Test Camera": {}}}))
    with pytest.raises(KeyError):
        load_led_positions(str(config_path), "Test Camera", "infrared1", (1280, 720))
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_metrics.py tests/engine/test_test_session.py tests/domain/test_calibration.py -v`
Expected: `LedDetectionMetric` ImportError; calibration tests fail on missing positional args. (The TestSession test may already pass - that's fine, it locks in the behavior.)

- [ ] **Step 3: Implement `LedDetectionMetric`** in `engine/metrics.py`, directly after `PositionGapMetric`:

```python
class LedDetectionMetric(Metric):
    """Single-stream counterpart of PositionGapMetric, for a camera that
    contributes ONE stream to a multi-camera run (pick_b is None - a
    single-stream settings.yaml test). There is no second stream to compare
    against inside the camera, so there is no intra-camera value to report;
    what the cross-camera reconciler needs is each frame's detected on-LED
    index, emitted under the SAME "stream_a_last_led" extra key
    PositionGapMetric uses, so engine.cross_camera_reconciler's
    _compute_cross_position_gap works unchanged. value is that index (handy
    in the CSV); exclusions mirror PositionGapMetric's own order
    (no_led_data, miss, frame_drop, warmup), and the reconciler reads them
    from this metric's led_detection_excluded/_exclude_reason keys when a
    row has no position_gap_ms_* keys."""

    name = "led_detection"

    def __init__(self, stream_a_threshold, warmup_pairs_to_skip):
        self.stream_a_threshold = stream_a_threshold
        self.warmup_pairs_to_skip = warmup_pairs_to_skip
        self._pair_count = 0
        # Same side-channel attributes as PositionGapMetric, so
        # engine/session_engine.py's overlay-mask copy works with either
        # metric. Stream B's is always None - there is no stream B.
        self.last_stream_a_on_mask = None
        self.last_stream_b_on_mask = None

    def update(self, sample: FramePairSample) -> MetricResult:
        self._pair_count += 1
        is_warmup = self._pair_count <= self.warmup_pairs_to_skip

        if sample.stream_a_bright is None:
            return MetricResult(name=self.name, value=None, excluded=True, exclude_reason="no_led_data")

        stream_a_on = sample.stream_a_bright > self.stream_a_threshold
        self.last_stream_a_on_mask = stream_a_on
        stream_a_last, _ = find_last_on_led(stream_a_on)
        extra = {"stream_a_last_led": stream_a_last}

        if stream_a_last is None:
            return MetricResult(name=self.name, value=None, excluded=True, exclude_reason="miss", extra=extra)
        if sample.stream_a_frame_drop:
            return MetricResult(name=self.name, value=stream_a_last, excluded=True, exclude_reason="frame_drop",
                                extra=extra)
        if is_warmup:
            return MetricResult(name=self.name, value=stream_a_last, excluded=True, exclude_reason="warmup",
                                extra=extra)
        return MetricResult(name=self.name, value=stream_a_last, excluded=False, exclude_reason=None, extra=extra)
```

- [ ] **Step 4: Implement single-slug calibration storage** in `domain/calibration.py`:

```python
def update_config_leds(config_path, camera_name, stream_a_slug, stream_a_positions, stream_a_res,
                        stream_b_slug=None, stream_b_positions=None, stream_b_res=None):
    """stream_b_* are None for a single-stream camera - only stream A's own
    slug block is written then."""
    with open(config_path, "r") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("leds", {})
    cfg["leds"].setdefault(camera_name, {})
    cfg["leds"][camera_name][stream_a_slug] = {
        "frame_width": stream_a_res[0], "frame_height": stream_a_res[1], "positions": stream_a_positions,
    }
    if stream_b_slug is not None:
        cfg["leds"][camera_name][stream_b_slug] = {
            "frame_width": stream_b_res[0], "frame_height": stream_b_res[1], "positions": stream_b_positions,
        }
    with open(config_path, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)


def load_led_positions(config_path, camera_name, stream_a_slug, stream_a_res, stream_b_slug=None, stream_b_res=None):
    """...existing docstring... stream_b_slug/stream_b_res are None for a
    single-stream camera - the returned stream B positions are then None."""
    with open(config_path, "r") as f:
        cfg = yaml.safe_load(f)
    leds_by_camera = cfg.get("leds", {})
    camera_entry = leds_by_camera.get(camera_name, {})
    wanted = [(stream_a_slug, stream_a_res)]
    if stream_b_slug is not None:
        wanted.append((stream_b_slug, stream_b_res))
    if any(slug not in camera_entry for slug, _ in wanted):
        raise KeyError(
            "No LED calibration yet for camera {!r} stream(s) {} - run calibration with "
            "this exact stream selection first.".format(camera_name, "/".join(repr(slug) for slug, _ in wanted))
        )

    for slug, current_res in wanted:
        ...  # existing resolution check loop body unchanged

    stream_b_positions = camera_entry[stream_b_slug]["positions"] if stream_b_slug is not None else None
    return camera_entry[stream_a_slug]["positions"], stream_b_positions
```

- [ ] **Step 5: Run tests**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_metrics.py tests/engine/test_test_session.py tests/domain/test_calibration.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add engine/metrics.py domain/calibration.py tests/engine/test_metrics.py tests/engine/test_test_session.py tests/domain/test_calibration.py
git commit -m "feat: LedDetectionMetric and single-slug LED calibration storage"
```

---

### Task 4: Cross-camera reconciler reads single-stream exclusions

**Files:**
- Modify: `engine/cross_camera_reconciler.py` (`_compute_cross_position_gap`, module docstring lines ~14-15)
- Test: `tests/engine/test_cross_camera_reconciler.py`

**Interfaces:**
- Consumes: rows from Task 3's `LedDetectionMetric` (`stream_a_last_led`, `led_detection_excluded`, `led_detection_exclude_reason`, no `position_gap_ms_*` keys).
- Produces: `_own_led_exclusion(row) -> (excluded, reason)`.

- [ ] **Step 1: Write the failing tests** (append; reuses `_spec`, `_row`, `_CamSpec`)

```python
def _single_stream_row(pair_index, ts_us, last_led, excluded=False, exclude_reason=None, frame_drop=False):
    """A single-stream camera's row: LedDetectionMetric output, no
    position_gap_ms_* keys at all (no intra-camera metric)."""
    return {
        "pair_index": pair_index,
        "stream_a_ts_us": ts_us, "stream_a_global_ts_us": ts_us, "stream_a_frame_drop": frame_drop,
        "stream_a_last_led": last_led,
        "led_detection": last_led, "led_detection_excluded": excluded, "led_detection_exclude_reason": exclude_reason,
    }


def test_cross_position_gap_between_two_single_stream_cameras():
    reconciler = CrossCameraReconciler([_spec(num_leds=10, switch_time_ms=1.0)])
    reconciler.ingest_row("cam1", _single_stream_row(1, 1_000_000.0, last_led=5))
    cross_rows = reconciler.ingest_row("cam2", _single_stream_row(1, 1_000_010.0, last_led=3))

    assert cross_rows[0]["position_gap_ms"] == 2.0
    assert cross_rows[0]["position_gap_ms_excluded"] is False


def test_cross_position_gap_reuses_a_single_stream_cameras_warmup_exclusion():
    reconciler = CrossCameraReconciler([_spec()])
    reconciler.ingest_row("cam1", _single_stream_row(1, 1_000_000.0, last_led=5, excluded=True, exclude_reason="warmup"))
    cross_rows = reconciler.ingest_row("cam2", _single_stream_row(1, 1_000_010.0, last_led=3))

    assert cross_rows[0]["position_gap_ms"] is None
    assert cross_rows[0]["position_gap_ms_exclude_reason"] == "warmup"


def test_cross_position_gap_single_stream_master_vs_two_stream_slave_on_its_stream_b():
    # Master runs "Color only" (its color is stream_a); the slave runs IR vs
    # RGB (its color is stream_b) - roles resolve per camera.
    specs = build_cross_camera_pair_specs(
        [_CamSpec("cam1", True, {"stream_a": "color"}),
         _CamSpec("cam2", False, {"stream_a": "infrared1", "stream_b": "color"})],
        outlier_threshold_us=100_000,
    )
    assert [(s.stream_identity, s.master_row_role, s.slave_row_role) for s in specs] == [("color", "stream_a", "stream_b")]
    reconciler = CrossCameraReconciler(specs)
    reconciler.ingest_row("cam1", _single_stream_row(1, 1_000_000.0, last_led=4))
    cross_rows = reconciler.ingest_row("cam2", _row(1, 1_000_010.0, role="stream_b", last_led=4))

    assert cross_rows[0]["position_gap_ms"] == 0.0
    assert cross_rows[0]["position_gap_ms_excluded"] is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_cross_camera_reconciler.py -k single_stream -v`
Expected: `test_..._warmup_exclusion` FAILS (reason not reused); the others may pass already - they lock in the behavior.

- [ ] **Step 3: Implement** - add above `_compute_cross_position_gap`:

```python
def _own_led_exclusion(row):
    """A camera's OWN already-computed LED exclusion for this row - its
    intra-camera PositionGapMetric's position_gap_ms_excluded/_exclude_reason
    for a two-stream camera, or LedDetectionMetric's led_detection_*
    equivalents for a single-stream camera (which has no position_gap_ms
    keys at all, since it has no second stream)."""
    if "position_gap_ms_excluded" in row:
        return row.get("position_gap_ms_excluded"), row.get("position_gap_ms_exclude_reason")
    return row.get("led_detection_excluded"), row.get("led_detection_exclude_reason")
```

and replace the last four `if` lines of `_compute_cross_position_gap` with:

```python
    for row in (master_row, slave_row):
        excluded, reason = _own_led_exclusion(row)
        if excluded:
            return None, True, reason
    return gap_ms, False, None
```

Add one sentence to its docstring: "For a single-stream camera the fallback reads LedDetectionMetric's led_detection_* keys instead (see _own_led_exclusion)." Update the module docstring's key list (around lines 14-15) to mention `led_detection_excluded`/`_exclude_reason`.

- [ ] **Step 4: Run tests**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_cross_camera_reconciler.py -v`
Expected: PASS (all 28 existing + 3 new).

- [ ] **Step 5: Commit**

```bash
git add engine/cross_camera_reconciler.py tests/engine/test_cross_camera_reconciler.py
git commit -m "feat: cross-camera Optical Sync reads single-stream LED exclusions"
```

---

### Task 5: Single-panel target and session engine frame emission

**Files:**
- Modify: `engine/dual_panel_control.py` (`single_panel_stream_for_picks`)
- Modify: `engine/session_engine.py` (extract `_emit_frames`)
- Test: `tests/engine/test_dual_panel_control.py`, `tests/engine/test_session_engine.py`

**Interfaces:**
- Produces: `single_panel_stream_for_picks(pick_a, pick_b=None)` -> `"stream_a"` for a lone infrared pick, `"stream_b"` for a lone color pick.
- Produces: `SessionEngineThread._emit_frames(stream_a_image, stream_b_image, pair_index)` - emits `frame_ready("stream_a", ...)` always and `frame_ready("stream_b", ...)` only when `self.pick_b is not None`.

- [ ] **Step 1: Write the failing tests**

`tests/engine/test_dual_panel_control.py` (next to `test_single_panel_stream_for_picks`):

```python
def test_single_panel_stream_for_a_single_pick():
    ir, color = rs.stream.infrared, rs.stream.color
    assert dual_panel_control.single_panel_stream_for_picks(_pick(ir), None) == "stream_a"
    assert dual_panel_control.single_panel_stream_for_picks(_pick(color), None) == "stream_b"
```

`tests/engine/test_session_engine.py`:

```python
import numpy as np

from engine.metrics import LedDetectionMetric


def test_emit_frames_single_stream_emits_only_stream_a_with_its_mask(qapp):
    metric = LedDetectionMetric(stream_a_threshold=np.full(2, 100.0), warmup_pairs_to_skip=0)
    metric.last_stream_a_on_mask = np.array([True, False])
    thread = SessionEngineThread(
        ctx=None, device_serial="SN1", pick_a={}, pick_b=None, camera_controls={}, test_session=None,
        position_gap_metric=metric,
    )
    emitted = []
    thread.frame_ready.connect(lambda name, image, pair_index, mask: emitted.append((name, pair_index, mask)))

    thread._emit_frames("image_a", None, 7)

    assert [(name, pair_index) for name, pair_index, _ in emitted] == [("stream_a", 7)]
    assert list(emitted[0][2]) == [True, False]
    assert emitted[0][2] is not metric.last_stream_a_on_mask  # a copy, not the live array


def test_emit_frames_two_stream_emits_both(qapp):
    thread = _make_thread(qapp)
    emitted = []
    thread.frame_ready.connect(lambda name, image, pair_index, mask: emitted.append(name))

    thread._emit_frames("image_a", "image_b", 0)

    assert emitted == ["stream_a", "stream_b"]
```

Update this test file's module docstring: it now also covers `_emit_frames` (pure signal-emission logic, no hardware).

- [ ] **Step 2: Run to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_dual_panel_control.py tests/engine/test_session_engine.py -v`
Expected: FAIL (`TypeError` on None / no `_emit_frames`).

- [ ] **Step 3: Implement**

`single_panel_stream_for_picks`:

```python
def single_panel_stream_for_picks(pick_a, pick_b=None):
    """... existing text ... pick_b is None for a single-stream camera -
    its one stream alone decides the panel."""
    import pyrealsense2 as rs
    types = {pick["stream_type"] for pick in (pick_a, pick_b) if pick is not None}
    ...  # rest unchanged
```

`engine/session_engine.py`: move the nested `on_frames` body out into a method, and make `run()`'s callbacks use `on_frames=self._emit_frames`:

```python
    def _emit_frames(self, stream_a_image, stream_b_image, pair_index):
        # (move the existing on_frames comment here verbatim)
        if self.position_gap_metric is not None:
            stream_a_mask = self.position_gap_metric.last_stream_a_on_mask
            stream_b_mask = self.position_gap_metric.last_stream_b_on_mask
            stream_a_mask = stream_a_mask.copy() if stream_a_mask is not None else None
            stream_b_mask = stream_b_mask.copy() if stream_b_mask is not None else None
        else:
            stream_a_mask = stream_b_mask = None
        self.frame_ready.emit("stream_a", stream_a_image, pair_index, stream_a_mask)
        # A single-stream camera (pick_b is None) has no stream B frame at all.
        if self.pick_b is not None:
            self.frame_ready.emit("stream_b", stream_b_image, pair_index, stream_b_mask)
```

Add to the `position_gap_metric` parameter's area a comment: it may be a `LedDetectionMetric` for a single-stream camera (same `last_stream_*_on_mask` attributes). `_maybe_save_position_gap_outlier` needs no change: a single-stream row has no `position_gap_ms`, so `is_position_gap_debug_outlier` returns False.

- [ ] **Step 4: Run tests**

Run: `.venv\Scripts\python.exe -m pytest tests/engine -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add engine/dual_panel_control.py engine/session_engine.py tests/engine/test_dual_panel_control.py tests/engine/test_session_engine.py
git commit -m "feat: single-panel target and frame emission for single-stream cameras"
```

---

### Task 6: Stream Config page and its preview

**Files:**
- Modify: `gui/pages/stream_config_page.py`
- Modify: `engine/stream_preview_thread.py`
- Modify: `domain/realsense_utils.py` (new `draw_single_stream_overlay` after `draw_bundle_overlay`)
- Test: `tests/gui/pages/test_stream_config_page.py`, `tests/domain/test_realsense_utils.py`

**Interfaces:**
- Consumes: Task 1's option shape `{"pick_a": ..., "pick_b": None}`.
- Produces: `StreamConfigPage.is_single_stream` (bool property); `config_chosen` emits `(pick_a, None, camera_controls)` with `camera_controls["exposure_b"] is None`; `draw_single_stream_overlay(image, bundle_index, frame_number, ts_us) -> image`.

- [ ] **Step 1: Write the failing tests** (`tests/gui/pages/test_stream_config_page.py`)

```python
def _single_tests(name="IR1 only", pick=IR1):
    return [{"test_name": name, "options": [{"pick_a": pick, "pick_b": None}]}]


def test_sensor_option_label_single_stream():
    assert _sensor_option_label({"pick_a": IR1, "pick_b": None}) == "1280x720 @ 30fps (y8)"


def test_single_stream_test_hides_exposure_b_and_disables_dual_panel(qapp):
    page = StreamConfigPage()
    page.populate(ctx=None, device_serial="123", tests=_single_tests(), preferred_dual_panel=True)

    assert page.is_single_stream is True
    assert page._camera_controls["exposure_b_spin"].isHidden()
    assert page._camera_controls["exposure_b_label"].isHidden()
    assert not page.dual_panel_checkbox.isEnabled()
    assert not page.dual_panel_checkbox.isChecked()


def test_switching_from_single_stream_to_two_stream_test_restores_controls(qapp):
    page = StreamConfigPage()
    page.populate(ctx=None, device_serial="123",
                  tests=_single_tests() + _tests(("IR vs RGB sync", [(IR1, COLOR0)])))

    page.combo_test.setCurrentIndex(1)

    assert page.is_single_stream is False
    assert not page._camera_controls["exposure_b_spin"].isHidden()
    assert page.dual_panel_checkbox.isEnabled()


def test_next_emits_none_pick_b_for_single_stream_test(qapp):
    page = StreamConfigPage()
    page.populate(ctx=None, device_serial="123", tests=_single_tests())
    page._camera_controls["manual_radio"].setChecked(True)
    received = []
    page.config_chosen.connect(received.append)

    page._on_next_clicked()

    pick_a, pick_b, camera_controls = received[0]
    assert (pick_a, pick_b) == (IR1, None)
    assert camera_controls["exposure_a"] == 8500
    assert camera_controls["exposure_b"] is None


def test_start_preview_passes_none_pick_b_for_single_stream_test(qapp, monkeypatch):
    import gui.pages.stream_config_page as stream_config_page_module
    constructed = []

    class _FakePreview:
        def __init__(self, ctx, serial, pick_a, pick_b, **kwargs):
            constructed.append((pick_a, pick_b))
            self.frame_ready = MagicMock()
            self.error = MagicMock()
        def start(self):
            pass

    monkeypatch.setattr(stream_config_page_module, "StreamPreviewThread", _FakePreview)
    page = StreamConfigPage()
    page.populate(ctx=None, device_serial="123", tests=_single_tests())

    page._on_start_preview_clicked()

    assert constructed == [(IR1, None)]
```

Overlay test:

```python
import numpy as np
from domain.realsense_utils import draw_single_stream_overlay


def test_draw_single_stream_overlay_returns_bgr_copy():
    image = np.zeros((40, 200), dtype=np.uint8)
    out = draw_single_stream_overlay(image, bundle_index=3, frame_number=10, ts_us=1234.0)
    assert out.shape == (40, 200, 3)
    assert image.max() == 0  # input untouched
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_stream_config_page.py tests/domain -k "single" -v`
Expected: FAIL.

- [ ] **Step 3: Implement `draw_single_stream_overlay`** in `domain/realsense_utils.py`:

```python
def draw_single_stream_overlay(image, bundle_index, frame_number, ts_us):
    """Single-stream counterpart of draw_bundle_overlay for the Stream
    Config preview of a single-stream test (pick_b is None) - there is no
    second stream and so no A/B delta to show, only this stream's own
    frame number and HW timestamp."""
    debug_img = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR) if len(image.shape) == 2 else image.copy()
    font = cv2.FONT_HERSHEY_SIMPLEX
    lines = [
        ("Bundle: {}".format(bundle_index), (0, 255, 0)),
        ("Frame: {}  |  Timestamp: {:.0f}".format(frame_number, ts_us), (0, 255, 255)),
    ]
    y = 25
    for text, color in lines:
        cv2.putText(debug_img, text, (10, y), font, 0.6, color, 2)
        y += 25
    return debug_img
```

- [ ] **Step 4: Implement the preview thread branch** in `engine/stream_preview_thread.py` (import `draw_single_stream_overlay`), inside the `if bundle_index % self.display_stride == 0:` block:

```python
                if bundle_index % self.display_stride == 0:
                    if self.pick_b is None:
                        # Single-stream test - no second stream, no delta.
                        print("Bundle {:>6} | Frame {:>6} | Timestamp {:>14.0f}".format(bundle_index, num_a, ts_a))
                        overlay_image = draw_single_stream_overlay(image_a, bundle_index, num_a, ts_a)
                    else:
                        ...  # existing delta/print/draw_bundle_overlay lines unchanged
                    self.frame_ready.emit(overlay_image)
```

Update the module docstring: "(or, for a single-stream test, just Stream A's own frame number/timestamp)".

- [ ] **Step 5: Implement Stream Config changes** in `gui/pages/stream_config_page.py`:

`_sensor_option_label` - first lines:

```python
    pick_a, pick_b = option["pick_a"], option["pick_b"]
    if pick_b is None:
        # Single-stream test - just the one stream's own geometry/format.
        return "{}x{} @ {}fps ({})".format(pick_a["width"], pick_a["height"], pick_a["fps"], pick_a["format"].name)
```

New property after `pick_b`:

```python
    @property
    def is_single_stream(self):
        """True when the selected sensor option has no Stream B (a
        single-stream settings.yaml test) - pick_b alone can't tell that
        apart from "nothing selected yet", which is also None."""
        option = self.combo_sensor_options.currentData()
        return option is not None and option["pick_b"] is None
```

`_populate_sensor_options` - add `self._update_single_stream_controls()` after `self._update_exposure_labels()`, and define:

```python
    def _update_single_stream_controls(self):
        """Single-stream tests (no Stream B) hide Exposure B and never use
        dual-panel mode (one stream only ever looks at one panel - the
        single-panel hub target in main_window picks which). Re-run on
        every test change, so switching back to a two-stream test restores
        both."""
        single_stream = self.is_single_stream
        w = self._camera_controls
        w["exposure_b_label"].setHidden(single_stream)
        w["exposure_b_spin"].setHidden(single_stream)
        if single_stream:
            self.dual_panel_checkbox.setChecked(False)
        self.dual_panel_checkbox.setEnabled(not single_stream)
        self.dual_panel_checkbox.setToolTip(
            "Dual LED panel mode needs two streams - not available for a single-stream test."
            if single_stream else ""
        )
```

`read_camera_controls` - `"exposure_b": None if (auto_exposure or self.is_single_stream) else w["exposure_b_spin"].value(),`

`_on_start_preview_clicked` and `_on_next_clicked` - replace `if pick_a is None or pick_b is None: return` with:

```python
        if self.combo_sensor_options.currentData() is None:
            return
        if pick_b is not None and self._streams_are_identical(pick_a, pick_b):
```

(keep each method's existing identical-streams message and early return under that `if`). Add a sentence to the module docstring: a single-stream test (no stream_b_identity) emits `pick_b=None`, hides Exposure B and disables the dual-panel checkbox.

- [ ] **Step 6: Run tests**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_stream_config_page.py tests/domain -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add gui/pages/stream_config_page.py engine/stream_preview_thread.py domain/realsense_utils.py tests/
git commit -m "feat: Stream Config supports single-stream tests"
```

---

### Task 7: ROI Select with one stream

**Files:**
- Modify: `gui/pages/roi_select_page.py` (`_capture_and_select`)
- Test: `tests/gui/pages/test_roi_select_page.py`

**Interfaces:**
- Produces: `roi_chosen` emits `(roi_a, None)` when `pick_b is None`.

- [ ] **Step 1: Write the failing test**

```python
def test_single_stream_capture_selects_one_roi_and_emits_none_for_b(qapp, monkeypatch):
    pick_a = {"stream_type": rs.stream.infrared, "stream_index": 1, "sensor_index": 0,
              "width": 4, "height": 2, "fps": 30, "format": rs.format.y8}
    monkeypatch.setattr(roi_select_page_module, "find_device_by_serial", lambda ctx, serial: object())
    monkeypatch.setattr(roi_select_page_module, "resolve_and_group", lambda device, a, b: [])
    monkeypatch.setattr(roi_select_page_module, "_apply_camera_controls", lambda *args: [])
    monkeypatch.setattr(roi_select_page_module, "turn_all_leds_on", lambda config: None)
    monkeypatch.setattr(roi_select_page_module, "turn_all_leds_off", lambda config: None)
    monkeypatch.setattr(roi_select_page_module.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        roi_select_page_module, "capture_synced_frame_pair",
        lambda groups, on_both_streaming=None, settle_frames=15: (on_both_streaming(), {(rs.stream.infrared, 1): bytes(8)})[1],
    )
    windows = []
    monkeypatch.setattr(roi_select_page_module, "_select_roi", lambda image, title: windows.append(title) or (0, 0, 4, 2))
    page = RoiSelectPage()
    page.set_context(ctx=None, device_serial="123", pick_a=pick_a, pick_b=None, camera_controls={})
    received = []
    page.roi_chosen.connect(received.append)

    page._on_capture_clicked()

    assert received == [((0, 0, 4, 2), None)]
    assert len(windows) == 1
```

(Add `import pyrealsense2 as rs` to the file's imports if it isn't there.)

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_roi_select_page.py -k single_stream -v`
Expected: FAIL (`TypeError: 'NoneType' object is not subscriptable`).

- [ ] **Step 3: Implement** in `_capture_and_select`:
- dual-panel branch: `image_b = self._capture_one_stream_on_frame(groups, pick_b, "stream_b", ...) if pick_b is not None else None`
- single-panel branch: `image_b = decode_frame(...) if pick_b is not None else None` (wrap the existing `image_b = decode_frame(...)` in that condition).
- After the `roi_a` cancel check, before `label_b`:

```python
        if pick_b is None:
            # Single-stream camera - one stream, one ROI.
            self.status_label.setText("ROI selected: {}={}".format(label_a, roi_a))
            self.roi_chosen.emit((roi_a, None))
            return
```

Move `label_b = stream_label(pick_b)` below that block. Add to the module docstring: a single-stream camera (`pick_b` None) gets one popup and emits `(roi_a, None)`.

- [ ] **Step 4: Run tests**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_roi_select_page.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gui/pages/roi_select_page.py tests/gui/pages/test_roi_select_page.py
git commit -m "feat: ROI Select handles a single-stream camera"
```

---

### Task 8: Calibration with one stream

**Files:**
- Modify: `gui/pages/calibration_page.py` (`_run_calibration`, new `_detect_stream`)
- Test: `tests/gui/pages/test_calibration_page.py`

**Interfaces:**
- Consumes: Task 3's `update_config_leds(..., stream_b_slug=None)`.
- Produces: `last_calibration_result` with `image_b_on`, `image_b_off`, `stream_b_otsu_threshold` all `None` for a single-stream run; config.yaml gets only stream A's slug.

- [ ] **Step 1: Write the failing test**

```python
def test_single_stream_calibration_saves_only_stream_a(qapp, tmp_path):
    import yaml
    config_path = str(tmp_path / "config.yaml")
    with open(config_path, "w") as f:
        f.write("leds: {}\n")
    ctx = _real_hardware_context(str(tmp_path), config_path)
    ctx["pick_b"] = None
    ctx["stream_b_roi"] = None
    pick_a = ctx["pick_a"]
    key_a = (pick_a["stream_type"], pick_a["stream_index"])
    on_frame = _make_2x2_grid_frame(60, 60, blob_value=220, background_value=20)
    off_frame = np.full((60, 60), 20, dtype=np.uint8)
    frames_on, frames_off = {key_a: on_frame.tobytes()}, {key_a: off_frame.tobytes()}

    page = CalibrationPage()
    page.set_context(**ctx)
    with patch.multiple(
        "gui.pages.calibration_page",
        find_device_by_serial=lambda ctx, serial: object(),
        resolve_and_group=lambda device, a, b: [],
        _apply_camera_controls=lambda groups, camera_controls, a, b: [],
        turn_all_leds_on=lambda config: None,
        turn_all_leds_off=lambda config: None,
        capture_synced_frame_pair=lambda groups, on_both_streaming=None, settle_frames=15: (
            on_both_streaming() if on_both_streaming else None, frames_on if on_both_streaming else frames_off
        )[1],
    ), patch("time.sleep"):
        page._on_run_clicked()

    result = page.last_calibration_result
    assert result is not None
    assert result["image_b_on"] is None and result["image_b_off"] is None
    assert result["stream_b_otsu_threshold"] is None
    with open(config_path) as f:
        assert set(yaml.safe_load(f)["leds"]["Intel RealSense D455"]) == {"infrared1"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_calibration_page.py -k single_stream -v`
Expected: FAIL.

- [ ] **Step 3: Implement** - extract the per-stream detection block into a method (the stream-A block and the stream-B block are identical apart from their names):

```python
    def _detect_stream(self, label, slug, image_on, image_off, roi, output_dir,
                       min_blob_area, row_gap_px, neighborhood_size):
        """One stream's LED detection + grid assignment + debug image -
        shared by stream A and (when present) stream B. Returns
        (positions, row_layout, otsu_threshold)."""
        # (move the existing "Cropped, not just masked" comment here)
        cropped = crop_to_roi(image_on, roi)
        self._log("Detecting LEDs in {} frame...".format(label))
        centroids, otsu = detect_led_centroids(cropped, None, min_blob_area)
        centroids = merge_close_centroids(centroids)
        self._log("Detected {} LED(s) in {} (Otsu threshold {}).".format(len(centroids), label, otsu))
        debug_path = os.path.join(output_dir, "debug_{}_detection.png".format(slug))
        try:
            # (move the existing build_grid_positions comment here)
            positions, row_layout, debug_centroids = build_grid_positions(
                centroids, roi, image_on, image_off, row_gap_px, neighborhood_size,
            )
        except RuntimeError:
            # (move the existing "No LEDs detected at all" comment here)
            save_debug_detection_image(cropped, centroids, debug_path)
            raise
        save_debug_detection_image(cropped, debug_centroids, debug_path)
        self._log("Saved debug image (cropped ROI + detected LEDs circled, numbered by grid ID): {}".format(debug_path))
        return positions, row_layout, otsu
```

In `_run_calibration`:
- dual-panel branch: `image_b_on, image_b_off = self._capture_on_off_for_stream(groups, pick_b, "stream_b", ...) if pick_b is not None else (None, None)`
- single-panel branch: `image_b_on = decode(frames_on, pick_b) if pick_b is not None else None`, same for `image_b_off`.
- Replace everything from `label_a, label_b = ...` through the `update_config_leds` log with:

```python
        single_stream = pick_b is None
        label_a, slug_a, res_a = stream_label(pick_a), stream_slug(pick_a), (pick_a["width"], pick_a["height"])
        positions_a, row_layout_a, otsu_a = self._detect_stream(
            label_a, slug_a, image_a_on, image_a_off, stream_a_roi, output_dir,
            min_blob_area, row_gap_px, neighborhood_size,
        )
        streams = [(label_a, positions_a)]
        otsu_b = None
        if not single_stream:
            label_b, slug_b, res_b = stream_label(pick_b), stream_slug(pick_b), (pick_b["width"], pick_b["height"])
            positions_b, row_layout_b, otsu_b = self._detect_stream(
                label_b, slug_b, image_b_on, image_b_off, stream_b_roi, output_dir,
                min_blob_area, row_gap_px, neighborhood_size,
            )
            streams.append((label_b, positions_b))
            if row_layout_a != row_layout_b:
                self._log(...)  # existing WARNING text unchanged

        for label, positions in streams:
            ...  # existing weakest-contrast loop body unchanged

        if single_stream:
            update_config_leds(config_path, camera_name, slug_a, positions_a, res_a)
            self._log("Saved {} LED positions ({}={}) to {}".format(len(positions_a), label_a, slug_a, config_path))
        else:
            update_config_leds(config_path, camera_name, slug_a, positions_a, res_a, slug_b, positions_b, res_b)
            self._log(...)  # existing two-stream log line unchanged
```

and in `last_calibration_result` use `stream_b_otsu_threshold=int(round(otsu_b)) if otsu_b is not None else None`.

- [ ] **Step 4: Run tests**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_calibration_page.py -v`
Expected: PASS (existing two-stream tests prove the refactor kept behavior).

- [ ] **Step 5: Commit**

```bash
git add gui/pages/calibration_page.py tests/gui/pages/test_calibration_page.py
git commit -m "feat: Calibration handles a single-stream camera"
```

---

### Task 9: Threshold Tuning with one stream

**Files:**
- Modify: `gui/pages/threshold_tuning_page.py`
- Modify: `engine/threshold_preview_thread.py`
- Test: `tests/gui/pages/test_threshold_tuning_page.py`

**Interfaces:**
- Consumes: `set_context(...)` called with `pick_b=None` and every `stream_b_*` / `image_b_*` value `None` (`stream_b_threshold_fraction_default` is still a number from settings).
- Produces: `stream_b_threshold` and `stream_b_xy` properties return `None` in single-stream mode; `self.stream_b_column_widget` (hidden in single-stream mode); `update_config_leds` called with stream A only.

- [ ] **Step 1: Write the failing tests**

```python
def _single_stream_context(**overrides):
    ctx = _minimal_context(
        pick_b=None, stream_b_xy=None, stream_b_on=None, stream_b_off=None, stream_b_roi=None,
        stream_b_label=None, image_b_on=None, image_b_off=None, stream_b_otsu_threshold=None,
        stream_b_positions=None,
    )
    ctx.update(overrides)
    return ctx


def _single_stream_page():
    page = ThresholdTuningPage()
    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        page.set_context(**_single_stream_context())
    return page


def test_single_stream_set_context_hides_stream_b_column(qapp):
    page = _single_stream_page()
    assert page.stream_b_column_widget.isHidden()
    assert page.stream_b_threshold is None
    assert page.stream_b_xy is None
    assert page.stream_a_threshold is not None


def test_two_stream_set_context_shows_stream_b_column_again(qapp):
    page = _single_stream_page()
    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        page.set_context(**_minimal_context())
    assert not page.stream_b_column_widget.isHidden()


def test_single_stream_start_passes_none_for_stream_b(qapp):
    page = _single_stream_page()
    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        page._on_start_clicked()
    assert _FakePreviewThread.last_args[3] is None  # pick_b
    assert _FakePreviewThread.last_kwargs["stream_b_xy"] is None


def test_single_stream_continue_persists_only_stream_a(qapp):
    page = _single_stream_page()
    with patch("gui.pages.threshold_tuning_page.update_config_leds") as mock_update, \
         patch("gui.pages.threshold_tuning_page.QMessageBox.warning") as mock_warning:
        page._on_continue_clicked()
    args = mock_update.call_args[0]
    assert len(args) == 5  # config_path, camera_name, slug_a, positions_a, res_a
    mock_warning.assert_not_called()  # 2 LEDs detected == num_leds 2


def test_single_stream_continue_warns_when_stream_a_count_differs_from_num_leds(qapp):
    page = _single_stream_page()
    page._context["num_leds"] = 5
    with patch("gui.pages.threshold_tuning_page.update_config_leds"), \
         patch("gui.pages.threshold_tuning_page.QMessageBox.warning") as mock_warning:
        page._on_continue_clicked()
    mock_warning.assert_called_once()
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_threshold_tuning_page.py -k single_stream -v`
Expected: FAIL.

- [ ] **Step 3: Implement the page**:

1. In `__init__`, wrap stream B's column in a widget so it can be hidden as a unit - replace `video_row.addLayout(stream_b_column)` with:

```python
        # A QWidget (not a bare layout) so a single-stream camera's context
        # can hide the whole Stream B column at once - see set_context.
        self.stream_b_column_widget = QWidget()
        self.stream_b_column_widget.setLayout(stream_b_column)
        ...
        video_row.addWidget(self.stream_b_column_widget)
```

2. In `set_context`, add near the top: `single_stream = pick_b is None`, store `single_stream=single_stream` in `self._context`, and change `stream_b_cropped_on=crop_to_roi(image_b_on, stream_b_roi) if not single_stream else None`. After `self._context = dict(...)`:

```python
        # A single-stream camera (pick_b None) has no Stream B to preview,
        # tune or persist - hide its whole column.
        self.stream_b_column_widget.setHidden(single_stream)
```

Set the stream B title only when not single-stream. Guard the B detection-slider lines:

```python
        self.stream_a_detection_slider.setValue(stream_a_otsu_threshold)
        self._on_detection_threshold_changed("stream_a", self.stream_a_detection_slider.value())
        if not single_stream:
            self.stream_b_detection_slider.setValue(stream_b_otsu_threshold)
            self._on_detection_threshold_changed("stream_b", self.stream_b_detection_slider.value())
```

3. `_on_detection_threshold_changed` and `_commit_detection_threshold`: after `if ctx is None: return`, add `if ctx["{}_cropped_on".format(stream_name)] is None: return` (a debounced B commit can't fire in single-stream mode, but this keeps both safe).

4. Properties:

```python
    @property
    def stream_b_threshold(self):
        if self._context["stream_b_on"] is None:
            return None  # single-stream camera
        return compute_threshold(...)  # existing body

    @property
    def stream_b_xy(self):
        return self._context["stream_b_xy"]  # already None for a single-stream camera
```

5. `_on_continue_clicked`:

```python
    def _on_continue_clicked(self):
        ctx = self._context
        slug_a = stream_slug(ctx["pick_a"])
        res_a = (ctx["pick_a"]["width"], ctx["pick_a"]["height"])
        stream_a_ids = list(ctx["stream_a_positions"].keys())
        if ctx["single_stream"]:
            if len(stream_a_ids) != ctx["num_leds"]:
                QMessageBox.warning(
                    self, "LED count mismatch",
                    "Detection tuning found {} LED(s), but settings.yaml's test.num_leds is {}. The "
                    "cross-camera Optical Sync math assumes they match - proceeding anyway, but treat "
                    "its results with caution until this is resolved (retune detection, or fix "
                    "test.num_leds).".format(len(stream_a_ids), ctx["num_leds"]),
                )
            update_config_leds(ctx["config_path"], ctx["camera_name"], slug_a, ctx["stream_a_positions"], res_a)
        else:
            ...  # existing two-stream count check + update_config_leds call, unchanged
        self._stop_preview_blocking()
        self.tuning_done.emit()
```

- [ ] **Step 4: Implement the preview thread** (`engine/threshold_preview_thread.py`):

```python
        self._stream_b_safe_size = (
            safe_neighborhood_size(stream_b_xy, neighborhood_size) if stream_b_xy is not None else neighborhood_size
        )
```

and in the frame loop, only sample/emit stream B when `self.pick_b is not None`:

```python
                    self.frame_ready.emit("stream_a", stream_a_image, frame_index, stream_a_bright)
                    # A single-stream camera (pick_b None) has no stream B.
                    if self.pick_b is not None:
                        stream_b_bright = sample_all_neighborhood_brightness(
                            stream_b_image, self.stream_b_xy, self._stream_b_safe_size
                        )
                        self.frame_ready.emit("stream_b", stream_b_image, frame_index, stream_b_bright)
```

(`resolve_and_group`, `exposure_for_group` and `ContinuousCapture` already accept `None` from Task 2.)

- [ ] **Step 5: Run tests**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_threshold_tuning_page.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add gui/pages/threshold_tuning_page.py engine/threshold_preview_thread.py tests/gui/pages/test_threshold_tuning_page.py
git commit -m "feat: Threshold Tuning handles a single-stream camera"
```

---

### Task 10: Main window sub-flow, Hub gating, start checks

**Files:**
- Modify: `gui/main_window.py`
- Modify: `gui/pages/camera_hub_page.py` (`CameraSummary`, `_can_start`, `set_cameras`)
- Test: `tests/gui/test_main_window.py`, `tests/gui/pages/test_camera_hub_page.py`

**Interfaces:**
- Consumes: everything from Tasks 1-9.
- Produces: committed per-camera `config` with `pick_b`, `stream_b_threshold`, `stream_b_xy`, `stream_b_roi`, `stream_b_label` all `None` for single-stream cameras. `CameraSummary(camera_id, label, is_master, configured, single_stream=False)`. `CameraHubPage.SOLO_SINGLE_STREAM_MESSAGE`.

- [ ] **Step 1: Write the failing tests**

Hub page:

```python
def test_start_disabled_for_a_solo_single_stream_camera(qapp):
    page = CameraHubPage()
    page.set_cameras([CameraSummary("cam1", "D455", is_master=True, configured=True, single_stream=True)])
    assert not page.start_button.isEnabled()
    assert page.start_button.toolTip() == CameraHubPage.SOLO_SINGLE_STREAM_MESSAGE
    assert CameraHubPage.SOLO_SINGLE_STREAM_MESSAGE == (
        "A single-stream camera needs at least one other camera to compare against."
    )


def test_start_enabled_for_two_single_stream_cameras(qapp):
    page = CameraHubPage()
    page.set_cameras([
        CameraSummary("cam1", "D455", is_master=True, configured=True, single_stream=True),
        CameraSummary("cam2", "D455", is_master=False, configured=True, single_stream=True),
    ])
    assert page.start_button.isEnabled()
    assert page.start_button.toolTip() == ""
```

Main window (add near `_configure_one_camera`; reuse `_full_settings`, `_make_window`, `IR1`, `COLOR0`, `_FakePreviewThread`, `_capture_critical`):

```python
def _ir1_only_test():
    return {
        "test_name": "IR1 only",
        "stream_a_identity": {"stream_type": "infrared", "stream_index": 1},
        "sensor_options": [{"stream_a": {"width": 1280, "height": 720, "fps": 30, "format": "y8"}}],
    }


def _single_stream_window(qapp, monkeypatch, tmp_path):
    settings = _full_settings({"Intel RealSense D455": [_ir_vs_rgb_test(), _ir1_only_test()]})
    window = _make_window(qapp, settings)
    monkeypatch.setattr(main_window_module, "list_video_stream_options", lambda ctx, serial: [IR1, COLOR0])
    monkeypatch.setattr(main_window_module, "save_gui_state", lambda state: None)
    monkeypatch.setattr(window.roi_page, "set_context", lambda *a, **k: None)
    monkeypatch.setattr(main_window_module, "ensure_output_dir", lambda settings: str(tmp_path))

    def _fake_load(config_path, camera_name, slug_a, res_a, slug_b=None, res_b=None):
        positions_a = {"0": [1.0, 1.0, 300.0, 100.0, 200.0]}
        return positions_a, ({"0": [2.0, 2.0, 600.0, 200.0, 400.0]} if slug_b is not None else None)

    monkeypatch.setattr(main_window_module, "load_led_positions", _fake_load)
    return window


def _configure_single_stream_camera(window, serial):
    window._on_device_chosen(serial, "Intel RealSense D455")
    window.stream_config_page.combo_test.setCurrentIndex(
        window.stream_config_page.combo_test.findData("IR1 only"))
    window._on_config_chosen((IR1, None, {
        "emitter_enabled": False, "auto_exposure": True, "exposure_a": None, "exposure_b": None,
    }))
    window._on_roi_chosen(([0, 0, 50, 50], None))
    window.calibration_page.last_calibration_result = dict(
        image_a_on=np.full((50, 50), 50, dtype=np.uint8), image_a_off=np.full((50, 50), 50, dtype=np.uint8),
        image_b_on=None, image_b_off=None, stream_a_otsu_threshold=127, stream_b_otsu_threshold=None,
        min_blob_area=5, row_gap_px=15, neighborhood_size=5,
    )
    camera_id = window._editing_camera_id
    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        window._on_calibration_done()
        window._on_tuning_done()
    return camera_id


def test_single_stream_camera_commits_with_no_stream_b_values(qapp, monkeypatch, tmp_path):
    window = _single_stream_window(qapp, monkeypatch, tmp_path)
    # A previous two-stream camera leaves a stream_b ROI behind in GuiState -
    # it must not leak into the single-stream camera's config.
    window.gui_state.stream_b_roi = [9, 9, 9, 9]
    monkeypatch.setattr(window.calibration_page, "set_context", lambda *a, **k: None)

    camera_id = _configure_single_stream_camera(window, "SN1")

    config = window._cameras[camera_id]["config"]
    assert config["pick_b"] is None
    for key in ("stream_b_threshold", "stream_b_xy", "stream_b_roi", "stream_b_label"):
        assert config[key] is None, key
    assert config["stream_a_threshold"] is not None
    assert window._cameras[camera_id]["test_name"] == "IR1 only"


def test_hub_blocks_a_solo_single_stream_camera_and_start_guard_refuses(qapp, monkeypatch, tmp_path):
    window = _single_stream_window(qapp, monkeypatch, tmp_path)
    monkeypatch.setattr(window.calibration_page, "set_context", lambda *a, **k: None)
    _configure_single_stream_camera(window, "SN1")
    calls = _capture_critical(monkeypatch)
    live_session_calls = []
    monkeypatch.setattr(window.live_session_page, "set_context", lambda **kwargs: live_session_calls.append(kwargs))

    assert not window.camera_hub_page.start_button.isEnabled()
    window._on_start_multi_camera_session_requested()

    assert len(calls) == 1
    assert live_session_calls == []


def test_removing_the_other_camera_leaves_start_disabled_for_the_single_stream_one(qapp, monkeypatch, tmp_path):
    window = _single_stream_window(qapp, monkeypatch, tmp_path)
    monkeypatch.setattr(window.calibration_page, "set_context", lambda *a, **k: None)
    _configure_single_stream_camera(window, "SN1")
    window._on_add_camera_requested()
    second_id = _configure_single_stream_camera(window, "SN2")
    assert window.camera_hub_page.start_button.isEnabled()

    window._on_remove_camera_requested(second_id)

    assert not window.camera_hub_page.start_button.isEnabled()


def test_two_single_stream_cameras_reach_the_multi_camera_page_with_gmsl_fps_check(qapp, monkeypatch, tmp_path):
    window = _single_stream_window(qapp, monkeypatch, tmp_path)
    monkeypatch.setattr(window.calibration_page, "set_context", lambda *a, **k: None)
    _configure_single_stream_camera(window, "SN1")
    window._on_add_camera_requested()
    _configure_single_stream_camera(window, "SN2")
    monkeypatch.setattr(type(window.camera_hub_page), "gmsl_tsc_checked", property(lambda self: True))
    received = {}
    monkeypatch.setattr(window.multi_camera_live_session_page, "set_cameras",
                        lambda ctx, cameras, **kwargs: received.update(cameras=cameras, **kwargs))

    window._on_start_multi_camera_session_requested()

    assert received["gmsl_tsc_sync"]["fps"] == 30
    assert [c["config"]["pick_b"] for c in received["cameras"]] == [None, None]
```

(If `_on_add_camera_requested` calls `device_page.refresh_devices` with the FakeCtx, that is fine - existing 2-camera tests already do this.)

- [ ] **Step 2: Run to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/test_main_window.py tests/gui/pages/test_camera_hub_page.py -k "single_stream" -v`
Expected: FAIL.

- [ ] **Step 3: Implement the Hub**:

```python
@dataclass
class CameraSummary:
    ...
    camera_id: str
    label: str
    is_master: bool
    configured: bool
    # A single-stream camera (a single-stream settings.yaml test) has
    # nothing to measure on its own - see CameraHubPage._can_start.
    single_stream: bool = False
```

In `CameraHubPage`:

```python
    SOLO_SINGLE_STREAM_MESSAGE = "A single-stream camera needs at least one other camera to compare against."

    def _is_solo_single_stream(self):
        return len(self._summaries) == 1 and self._summaries[0].single_stream

    def _can_start(self):
        if not self._summaries:
            return False
        if not all(summary.configured for summary in self._summaries):
            return False
        if self._is_solo_single_stream():
            return False
        return sum(1 for summary in self._summaries if summary.is_master) == 1
```

and at the end of `set_cameras`: `self.start_button.setToolTip(self.SOLO_SINGLE_STREAM_MESSAGE if self._is_solo_single_stream() else "")`.

- [ ] **Step 4: Implement the main window**:

1. `_slave_genlock_color_resolution_conflicts`: `for pick in (camera["config"]["pick_a"], camera["config"]["pick_b"]) if pick is not None and pick["stream_type"] == rs.stream.color`.

2. `_on_config_chosen`: wrap the nine `self.gui_state.stream_b_*` assignments in `if pick_b is not None:` with a comment "a single-stream camera has no stream B - leave the last two-stream prefill values alone".

3. `_on_roi_chosen`: `if stream_b_roi is not None: self.gui_state.stream_b_roi = list(stream_b_roi)`.

4. `_on_calibration_done`:

```python
        single_stream = pick_b is None
        slug_a = stream_slug(pick_a)
        slug_b = None if single_stream else stream_slug(pick_b)
        res_b = None if single_stream else (pick_b["width"], pick_b["height"])
        stream_a_positions, stream_b_positions = load_led_positions(
            config_path, camera_name, slug_a, (pick_a["width"], pick_a["height"]), slug_b, res_b,
        )
        stream_a_ids = list(stream_a_positions.keys())
        stream_a_xy = np.array([stream_a_positions[i][:2] for i in stream_a_ids])
        stream_a_on = np.array([stream_a_positions[i][2] for i in stream_a_ids])
        stream_a_off = np.array([stream_a_positions[i][3] for i in stream_a_ids])
        if single_stream:
            stream_b_xy = stream_b_on = stream_b_off = None
        else:
            stream_b_ids = list(stream_b_positions.keys())
            stream_b_xy = np.array([stream_b_positions[i][:2] for i in stream_b_ids])
            stream_b_on = np.array([stream_b_positions[i][2] for i in stream_b_ids])
            stream_b_off = np.array([stream_b_positions[i][3] for i in stream_b_ids])
        # ROI/label for stream B: None for a single-stream camera - NOT
        # gui_state.stream_b_roi, which may still hold a previous
        # two-stream camera's value.
        stream_b_roi = None if single_stream else self.gui_state.stream_b_roi
        stream_b_label = None if single_stream else stream_label(pick_b)
```

Replace the LED-count warning condition/message with a single-stream variant:

```python
        if single_stream:
            mismatch = len(stream_a_ids) != num_leds
            detail = "Calibration detected {} {} LED(s), but settings.yaml's test.num_leds is {}.".format(
                len(stream_a_ids), stream_label(pick_a), num_leds)
        else:
            mismatch = len(stream_a_ids) != len(stream_b_ids) or len(stream_a_ids) != num_leds
            detail = "Calibration detected {} {} LED(s) and {} {} LED(s), but settings.yaml's test.num_leds is {}.".format(
                len(stream_a_ids), stream_label(pick_a), len(stream_b_ids), stream_label(pick_b), num_leds)
        if mismatch:
            QMessageBox.warning(
                self, "LED count mismatch",
                detail + " The live session's position-gap math assumes these match - proceeding "
                "anyway, but treat position-gap results with caution until this is resolved "
                "(re-run calibration, or fix test.num_leds).",
            )
```

(Keep the two-stream message text byte-identical in meaning; if an existing test asserts on the exact old string, keep the old full string for the two-stream branch.) Use `stream_b_roi`/`stream_b_label` in `_pending_ctx` and in the `threshold_tuning_page.set_context` call instead of `self.gui_state.stream_b_roi`/`stream_label(pick_b)`.

5. `_refresh_camera_hub`: add `single_stream=camera["config"]["pick_b"] is None` to each `CameraSummary`.

6. `_on_start_multi_camera_session_requested`, 1-camera branch - first lines:

```python
            only_camera = next(iter(self._cameras.values()))
            if only_camera["config"]["pick_b"] is None:
                # Defense in depth - the hub already disables Start for this.
                QMessageBox.critical(self, "Single-stream camera needs a partner",
                                     CameraHubPage.SOLO_SINGLE_STREAM_MESSAGE)
                return
```

(import `CameraHubPage` from `gui.pages.camera_hub_page` if not already imported).

7. GMSL fps: `fps_values = sorted({camera["config"][pick]["fps"] for camera in self._cameras.values() for pick in ("pick_a", "pick_b") if camera["config"][pick] is not None})`.

8. `_apply_single_panel_target` needs no change (Task 5 made the helper accept `None`); Edit's `preferred_b=config["pick_b"]` already passes `None`.

- [ ] **Step 5: Run tests**

Run: `.venv\Scripts\python.exe -m pytest tests/gui -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add gui/main_window.py gui/pages/camera_hub_page.py tests/gui
git commit -m "feat: main window commits single-stream cameras; hub blocks a solo one"
```

---

### Task 11: Slim per-camera panel and single-stream plot export

**Files:**
- Modify: `gui/widgets/camera_live_session_panel.py`
- Modify: `domain/plot_export.py` (`_build_figure`, `export_session_plot`)
- Test: `tests/gui/widgets/test_camera_live_session_panel.py`, `tests/domain/test_plot_export.py`

**Interfaces:**
- Produces: `CameraLiveSessionPanel(camera_id, single_stream=False, parent=None)`; in single-stream mode the panel has no `stream_b_panel`/`pairing_plot`/`position_plot` in its layout (attributes set to `None`), shows a `stream_a_last_led` stat field, and writes single-image snapshots. `export_session_plot(rows, path, single_stream=False)`; `_build_figure(rows, single_stream=False)` returns a 1-axis figure (stream A frame drop) when `single_stream`.

- [ ] **Step 1: Write the failing tests**

Panel:

```python
def _prepared_single_stream_panel(tmp_path):
    panel = CameraLiveSessionPanel("cam1", single_stream=True)
    panel.prepare_for_run(
        output_dir=str(tmp_path), kept_csv_filename="kept.csv", dropped_csv_filename="dropped.csv",
        stream_a_xy=np.array([(1, 1), (2, 2)]), stream_b_xy=None,
        stream_a_roi=(0, 0, 4, 4), stream_b_roi=None,
        snapshot_every_n_pairs=20, max_snapshots=2, switch_time_ms=1.0,
    )
    return panel


def test_single_stream_panel_has_no_intra_camera_widgets(qapp):
    panel = CameraLiveSessionPanel("cam1", single_stream=True)
    assert panel.stream_b_panel is None
    assert panel.pairing_plot is None
    assert panel.position_plot is None
    assert panel.drop_plot is not None


def test_single_stream_set_camera_labels_only_titles_stream_a(qapp):
    panel = CameraLiveSessionPanel("cam1", single_stream=True)
    panel.set_camera_labels("Intel RealSense D455", "SN789", "Infrared 1", None)
    assert panel.stream_a_title_label.text() == "D455 [SN789] - Infrared 1"


def test_single_stream_periodic_snapshot_saves_stream_a_only(qapp, tmp_path):
    panel = _prepared_single_stream_panel(tmp_path)
    panel.on_frame_ready("stream_a", np.zeros((4, 4), dtype=np.uint8), 20, np.array([True, False]))
    assert os.path.exists(os.path.join(str(tmp_path), "periodic_led_state_pair00020.png"))


def test_single_stream_on_stats_ready_shows_detected_led_and_skips_gap_plots(qapp, tmp_path):
    panel = _prepared_single_stream_panel(tmp_path)
    panel.on_row_ready({"pair_index": 0, "stream_a_frame_drop": True})
    panel.on_stats_ready({"pair_index": 0, "stream_a_last_led": 3, "stream_a_frame_drop": True})
    assert panel.stats_panel._value_labels["stream_a_last_led"].text() == "3"
    assert panel.stats_panel._value_labels["stream_a_frame_drops"].text() == "1"


def test_single_stream_save_debug_snapshot_writes_one_file(qapp, tmp_path):
    panel = _prepared_single_stream_panel(tmp_path)
    panel.on_frame_ready("stream_a", np.zeros((4, 4), dtype=np.uint8), 1, np.array([True, False]))
    panel._save_led_state_debug_images()
    assert os.path.exists(os.path.join(str(tmp_path), "live_led_state_stream_a.png"))
    assert not os.path.exists(os.path.join(str(tmp_path), "live_led_state_stream_b.png"))


def test_single_stream_session_finished_writes_csvs_plot_and_drop_chart(qapp, tmp_path):
    panel = _prepared_single_stream_panel(tmp_path)
    panel.on_session_finished([{"pair_index": 0, "stream_a_frame_drop": False, "stream_b_frame_drop": False}])
    for name in ("kept.csv", "pipeline_sync_plot.png", "frame_drops_chart.png"):
        assert os.path.exists(os.path.join(str(tmp_path), name)), name
    assert not os.path.exists(os.path.join(str(tmp_path), "hw_ts_latency_chart.png"))
```

Plot export:

```python
def test_single_stream_figure_has_one_frame_drop_axis():
    from domain.plot_export import _build_figure
    rows = [{"pair_index": i, "stream_a_frame_drop": i == 1, "stream_b_frame_drop": False} for i in range(3)]
    fig = _build_figure(rows, single_stream=True)
    try:
        assert len(fig.axes) == 1
        assert [line.get_label() for line in fig.axes[0].get_lines()] == ["Stream A frame drop"]
        assert list(fig.axes[0].get_lines()[0].get_ydata()) == [0, 1, 0]
    finally:
        import matplotlib.pyplot as plt
        plt.close(fig)
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/widgets/test_camera_live_session_panel.py tests/domain/test_plot_export.py -k single_stream -v`
Expected: FAIL.

- [ ] **Step 3: Implement plot export**:

```python
def _build_figure(rows, single_stream=False):
    """... existing docstring ... single_stream (a single-stream camera -
    one stream, no intra-camera sync) builds ONE axis instead: stream A's
    frame drops only - its sync numbers live in the cross-camera export."""
    pair_indices = [row["pair_index"] for row in rows]
    stream_a_drop = [1 if row.get("stream_a_frame_drop") else 0 for row in rows]
    if single_stream:
        fig, drop_ax = plt.subplots(1, 1, figsize=(_figure_width(len(rows)), _FIGURE_HEIGHT / 3.0))
        fig.patch.set_facecolor(SURFACE)
        drop_ax.plot(pair_indices, stream_a_drop, label="Stream A frame drop", color=STREAM_A_DROP_COLOR)
        drop_ax.set_ylabel("Frame drop")
        drop_ax.set_xlabel("Pair index")
        _style_axis(drop_ax)
        fig.tight_layout()
        return fig
    ...  # existing body unchanged (it recomputes stream_a_drop the same way - fine, or reuse the variable)


def export_session_plot(rows, path, single_stream=False):
    fig = _build_figure(rows, single_stream=single_stream)
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
```

- [ ] **Step 4: Implement the panel's single-stream mode** in `gui/widgets/camera_live_session_panel.py`:

1. Constructor signature `def __init__(self, camera_id, single_stream=False, parent=None):`, store `self._single_stream = single_stream`. Update the module docstring: a single-stream camera's panel (`single_stream=True`) shows one video panel, frame drops and the detected LED only - no intra-camera HW TS Latency/Optical Sync, since it has no second stream to compare against.

2. Video row: always build `stream_a_panel` + its title. Build `stream_b_panel`/`stream_b_title_label` and add their column only when not single-stream; otherwise set both attributes to `None`.

3. Charts: build `pairing_gap_checkbox`/`pairing_plot` and `position_gap_checkbox`/`position_plot` (and their `_make_chart_header` rows) only when not single-stream; otherwise set `self.pairing_plot = self.position_plot = None`. The drop plot: add the `stream_b_frame_drops` series and label "Frame Drops (A up / B down)" only when not single-stream; in single-stream mode use label "Frame Drops", checkbox text "Frame drops", header series list `["stream_a_frame_drops"]`, and make `_set_frame_drops_visible` skip the B series when single-stream.

4. Stats panel: in single-stream mode add fields `frame_index`, `stream_a_last_led` ("Detected LED"), `switch_time_ms`, `stream_a_frame_drops` only, and no stats table. Two-stream mode: unchanged.

5. `set_camera_labels`: set stream B's title only when `self.stream_b_title_label is not None`.

6. `_save_chart_images`: build the dict from the plots that exist:

```python
        chart_files = {self.drop_plot: "frame_drops_chart.png"}
        if not self._single_stream:
            chart_files[self.pairing_plot] = "hw_ts_latency_chart.png"
            chart_files[self.position_plot] = "optical_sync_chart.png"
```

7. `prepare_for_run`: call `clear_data()` only on plots that aren't `None`.

8. `on_frame_ready`: in the `stream_a` branch, after `self.stream_a_panel.set_frame(display_image)`, add `if self._single_stream: self._maybe_save_periodic_snapshot(pair_index)` (the two-stream trigger stays in the B branch).

9. `_maybe_save_periodic_snapshot`: in single-stream mode, require only A's mask/image and write `draw_led_state_overlay(self._last_stream_a_image, self._context["stream_a_xy"], self._last_stream_a_on_mask)` to the same `periodic_led_state_pair{:05d}.png` path (no `combine_side_by_side`).

10. `on_row_ready`: unchanged (B keys are absent/False; the running stats simply never update).

11. `on_stats_ready`: guard the pairing/position blocks with `if self.pairing_plot is not None` / `if self.position_plot is not None`; add `if stats.get("stream_a_last_led") is not None: self.stats_panel.set_value("stream_a_last_led", stats["stream_a_last_led"])`; only add/update the B drop point and B drop count when not single-stream; only `_push_running_stats` when not single-stream.

12. `on_session_finished`: `export_session_plot(rows, path, single_stream=self._single_stream)`.

13. `_save_led_state_debug_images`: in single-stream mode, require only A's mask/image, write only `live_led_state_stream_a.png`, status text `"Saved debug snapshot: {}"`.

- [ ] **Step 5: Run tests**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/widgets/test_camera_live_session_panel.py tests/domain/test_plot_export.py tests/gui/pages/test_live_session_page.py -v`
Expected: PASS (LiveSessionPage calls `export_session_plot(rows, path)` - the default keeps it unchanged).

- [ ] **Step 6: Commit**

```bash
git add gui/widgets/camera_live_session_panel.py domain/plot_export.py tests/
git commit -m "feat: slim per-camera panel and plot export for single-stream cameras"
```

---

### Task 12: Multi-camera page wires single-stream cameras

**Files:**
- Modify: `gui/pages/multi_camera_live_session_page.py` (`_stream_identities`, `set_cameras`, `start_all_sessions`)
- Test: `tests/gui/pages/test_multi_camera_live_session_page.py`

**Interfaces:**
- Consumes: `LedDetectionMetric` (Task 3), `CameraLiveSessionPanel(single_stream=...)` (Task 11), reconciler fallback (Task 4).
- Produces: `_stream_identities(config)` -> `{"stream_a": slug}` when `pick_b is None`.

- [ ] **Step 1: Write the failing tests**

```python
def _single_stream_config(tmp_path, **overrides):
    return _camera_config(
        tmp_path, pick_b=None, stream_b_threshold=None, stream_b_xy=None, stream_b_roi=None,
        stream_b_label=None, **overrides,
    )


def _two_single_stream_cameras(tmp_path):
    return [
        {"camera_id": "cam1", "label": "D455 A", "is_master": True,
         "config": _single_stream_config(tmp_path, device_serial="SN1")},
        {"camera_id": "cam2", "label": "D455 B", "is_master": False,
         "config": _single_stream_config(tmp_path, device_serial="SN2")},
    ]


def test_stream_identities_omit_stream_b_for_a_single_stream_camera(tmp_path):
    from gui.pages.multi_camera_live_session_page import _stream_identities
    assert _stream_identities(_single_stream_config(tmp_path)) == {"stream_a": "infrared1"}


def test_single_stream_cameras_get_slim_panels_and_one_cross_series(qapp, tmp_path):
    page, _ = _page_with_fake_threads()
    page.set_cameras(object(), _two_single_stream_cameras(tmp_path))
    assert all(panel.stream_b_panel is None for panel in page._panels.values())
    assert page._cross_pair_series_keys == {("cam2", "infrared1"): "infrared1"}


def test_start_all_sessions_uses_led_detection_metric_for_single_stream_cameras(qapp, tmp_path):
    from engine.metrics import LedDetectionMetric
    page, fake_threads = _page_with_fake_threads()
    page.set_cameras(object(), _two_single_stream_cameras(tmp_path))

    page.start_all_sessions()

    kwargs = fake_threads["SN1"].kwargs
    assert kwargs["pick_b"] is None
    assert kwargs["stream_b_xy"] is None
    assert isinstance(kwargs["position_gap_metric"], LedDetectionMetric)
    metric_names = [m.name for m in kwargs["test_session"].config.metrics]
    assert metric_names == ["led_detection"]
    assert kwargs["test_session"].config.stream_b_fps is None


def test_mixed_run_keeps_intra_camera_metrics_for_the_two_stream_camera(qapp, tmp_path):
    page, fake_threads = _page_with_fake_threads()
    cameras = [
        {"camera_id": "cam1", "label": "D455 A", "is_master": True,
         "config": _single_stream_config(tmp_path, device_serial="SN1")},
        {"camera_id": "cam2", "label": "D455 B", "is_master": False,
         "config": _camera_config(tmp_path, device_serial="SN2")},
    ]
    page.set_cameras(object(), cameras)

    page.start_all_sessions()

    assert [m.name for m in fake_threads["SN2"].kwargs["test_session"].config.metrics] == [
        "pairing_gap_us", "position_gap_ms"]
    assert page._panels["cam2"].stream_b_panel is not None
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_multi_camera_live_session_page.py -k "single_stream or mixed_run" -v`
Expected: FAIL (`stream_slug(None)` TypeError).

- [ ] **Step 3: Implement**:

`_stream_identities`:

```python
def _stream_identities(config):
    # A single-stream camera (pick_b None) only has a stream A identity -
    # engine.cross_camera_reconciler.build_cross_camera_pair_specs already
    # skips identities a camera doesn't have.
    identities = {"stream_a": stream_slug(config["pick_a"])}
    if config["pick_b"] is not None:
        identities["stream_b"] = stream_slug(config["pick_b"])
    return identities
```

`set_cameras` - construct `CameraLiveSessionPanel(camera["camera_id"], single_stream=config["pick_b"] is None)` (move the `config = camera["config"]` line above it).

`start_all_sessions` - replace the metric construction and `TestSessionConfig` fps:

```python
            single_stream = config["pick_b"] is None
            if single_stream:
                # One stream, no intra-camera sync - only the detected LED
                # (for the cross-camera Optical Sync) is measured per camera.
                position_gap_metric = LedDetectionMetric(
                    stream_a_threshold=config["stream_a_threshold"],
                    warmup_pairs_to_skip=config["warmup_pairs_to_skip"],
                )
                metrics = [position_gap_metric]
            else:
                position_gap_metric = PositionGapMetric(...)  # existing args unchanged
                metrics = [
                    PairingGapMetric(outlier_threshold_us=config["pairing_gap_outlier_threshold_us"]),
                    position_gap_metric,
                ]
            test_session = TestSession(TestSessionConfig(
                metrics=metrics, duration_s=duration_s,
                stream_a_fps=config["pick_a"]["fps"],
                stream_b_fps=None if single_stream else config["pick_b"]["fps"],
                frame_drop_threshold_factor=config["frame_drop_threshold_factor"],
            ))
```

Import `LedDetectionMetric` alongside the existing metric imports. `prepare_for_run` and `thread_kwargs` already pass `config["stream_b_*"]`/`config["pick_b"]` - these are `None` for single-stream cameras, which Tasks 5 and 11 handle. Update this page's module docstring with one paragraph: single-stream cameras run `LedDetectionMetric` only and get a slim tab; the Cross-Camera Sync tab is their real result.

- [ ] **Step 4: Run tests**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_multi_camera_live_session_page.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gui/pages/multi_camera_live_session_page.py tests/gui/pages/test_multi_camera_live_session_page.py
git commit -m "feat: multi-camera page runs single-stream cameras cross-camera only"
```

---

### Task 13: Docs and full verification

**Files:**
- Modify: `CLAUDE.md` (new section after "Shared dual LED panels across cameras")
- Modify: `README.md` (short usage note next to the multi-camera section; find it with `grep -n "Multi-Camera\|multi-camera" README.md`)

- [ ] **Step 1: Add the CLAUDE.md section**

```markdown
### Single-stream cameras (one stream per camera, cross-camera only)

A settings.yaml test with no `stream_b_identity` (and only a `stream_a` side
per `sensor_options` entry, e.g. "IR1 only") makes that camera
SINGLE-STREAM: `pick_b is None` everywhere downstream - the one signal every
layer checks, no separate flag. `resolve_and_group`/`ContinuousCapture` open
only stream A (depth still co-enabled for an IR pick per
`camera_sync.enable_depth_for_ir_sync`); ROI Select/Calibration/Threshold
Tuning handle only stream A and `update_config_leds` writes only its slug.
In a multi-camera run such a camera runs `engine/metrics.py`'s
`LedDetectionMetric` instead of `PairingGapMetric`+`PositionGapMetric`: it
emits the same `stream_a_last_led` key the cross-camera reconciler reads,
and its `led_detection_excluded`/`_exclude_reason` replace the intra-camera
`position_gap_ms_*` exclusion the reconciler otherwise reuses
(`_own_led_exclusion`). Its per-camera tab (`CameraLiveSessionPanel(single_stream=True)`)
is slim - one video panel, frame drops, detected LED, single-image
snapshots; the Cross-Camera Sync tab is the result. Mixing single- and
two-stream cameras is allowed (pairs match on shared slugs as always).
Single-stream cameras never use dual-panel mode (Stream Config disables the
checkbox); the single-panel hub target picks the IR panel for an IR pick,
the color panel for a color pick. A run of one single-stream camera can't
start (Camera Hub disables Start, and `_on_start_multi_camera_session_requested`
refuses defensively) - `LiveSessionPage` never sees `pick_b=None`.
```

- [ ] **Step 2: Add the README note** - 3-4 sentences: how to add a single-stream test in settings.yaml (copy the "IR1 only" example), that each camera needs at least one partner camera, and that results are on the Cross-Camera Sync tab.

- [ ] **Step 3: Run the full suite**

Run: `.venv\Scripts\python.exe -m pytest -v`
Expected: all tests PASS, zero failures.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: single-stream cameras in the multi-camera test"
```

- [ ] **Step 5: Hand off for the real-hardware check** - report to the operator: two cameras, both on "IR1 only", single panel; confirm the cross-camera plots populate, the slim tabs show the LED overlay, and a solo single-stream camera can't start.
