# Single-stream cameras in the multi-camera test

Date: 2026-09-29
Status: approved design, pending spec review

## Goal

Let a multi-camera run use ONE stream per camera (e.g. IR1 on camera 1 vs
IR1 on camera 2) and measure cross-camera sync on that stream only. With a
single stream there is no intra-camera (stream A vs stream B) sync, so the
Cross-Camera Sync tab is the meaningful view; each single-stream camera's
own tab is reduced to what still makes sense for one stream.

## Decisions (agreed with the operator)

1. **Enabled via settings.yaml tests.** A test under
   `camera.stream_options.<device name>` with only `stream_a_identity` and
   `sensor_options` entries with only `stream_a` is a single-stream test. It
   appears in Stream Config's existing Test combo. No new checkbox/toggle.
2. **Mixing allowed.** One run may contain single-stream and two-stream
   cameras. Cross-camera pairs are built on shared stream slugs, as today
   (`build_cross_camera_pair_specs` already skips identities a camera lacks).
3. **Single LED panel only** for single-stream cameras. The dual-panel
   checkbox is disabled and unticked when the chosen test has no stream B.
   The existing single-panel hub target picks the panel: an infrared pick
   -> IR panel (`stream_a`), a color pick -> color panel (`stream_b`).
4. **Live view:** Cross-Camera Sync tab first (unchanged), plus a slim
   per-camera tab for each single-stream camera: one video panel with the
   LED on/off overlay, stream frame drops (plot + count), detected-LED
   readout, and single-image debug snapshots. No intra-camera HW TS Latency
   or Optical Sync plots/stats. Two-stream cameras keep their full tab.
5. **Solo single-stream camera cannot start.** Camera Hub's Start is
   disabled (tooltip: "A single-stream camera needs at least one other
   camera to compare against") when the run has exactly one camera and it
   is single-stream. `LiveSessionPage` is never reached with `pick_b=None`.
6. **Depth-for-IR-sync** keeps following `camera_sync.enable_depth_for_ir_sync`
   for an infrared single pick (behavior consistent with two-stream runs).

## Approach: `pick_b = None` threaded through the existing A/B code

No new pages, no N-stream generalization. Everywhere that consumes
`pick_b` gets an explicit "no stream B" branch. The single source of truth
for "is this camera single-stream" is `pick_b is None`.

### Config / schema (`engine/streams.py`, `settings.yaml`)

- `parse_camera_tests_config`: `stream_b_identity` optional. If absent,
  every `sensor_options` entry must have only `stream_a` (a `stream_b` there
  is a config error, and vice versa - fail loudly).
- `resolve_camera_tests`: for a single-stream test, keep options whose
  stream A matches the device; the option's `pick_b` is `None`.
- `settings.yaml`: add example single-stream tests ("IR1 only", "Color
  only") for the D455 and D585 entries, and a comment documenting the shape.

### Capture layer (`engine/streams.py`)

- `resolve_and_group(device, pick_a, None)` -> one group with one profile.
- `exposure_for_group(..., pick_b=None, ...)` -> `exposure_a` always.
- `ContinuousCapture(serial, pick_a, None)`: `_depth_sync_stream`,
  `_build_config` enable only stream A (+ depth when applicable);
  `frames_with_diagnostics` no longer waits for frame B, skips B metadata
  checks/decoding, and yields the same 8-tuple with B entries `None`;
  `_read_global_ts_us` accepts a missing frame B (returns `None` for B).
- `capture_synced_frame_pair` already works with one group - unchanged.

### Stream Config (`gui/pages/stream_config_page.py`, `engine/stream_preview_thread.py`)

- Sensor-option label shows only stream A when `pick_b` is None.
- Exposure B spinbox/label hidden; `read_camera_controls()` returns
  `exposure_b: None`.
- `_streams_are_identical` / preview / Next early-returns accept `pick_b is None`.
- Dual-panel checkbox disabled + unticked for single-stream tests.
- Preview thread: single-stream mode shows stream A only, no A/B delta.
- Emits `(pick_a, None, controls)`.

### ROI Select / Calibration / Threshold Tuning

- ROI Select: one capture, one `selectROI`, emits `(roi_a, None)`.
- Calibration: detect on stream A only; skip A-vs-B row-layout comparison;
  `last_calibration_result` B fields `None`.
- `domain/calibration.py`: `update_config_leds` / `load_led_positions`
  accept `stream_b_slug=None` (write/read stream A only; other slugs on the
  camera untouched as today).
- Threshold Tuning: B column (video, fraction spinbox, labels) hidden;
  `stream_b_threshold` / `stream_b_xy` return `None`; LED-count check
  compares nothing for B. `ThresholdPreviewThread` samples only stream A.

### Main window (`gui/main_window.py`)

- `_on_config_chosen`: GuiState `stream_b_*` fields left unchanged when
  `pick_b` is None.
- `_on_roi_chosen`, `_on_calibration_done`, `_on_tuning_done`: B values
  become `None`; A/B LED-count mismatch warning skipped.
- `single_panel_stream_for_picks(pick_a, None)` (`engine/dual_panel_control.py`)
  -> IR pick `"stream_a"`, color pick `"stream_b"`.
- Slave color-resolution conflict check and GMSL fps collection skip a
  `None` pick.
- Edit prefill passes `preferred_b=None` for single-stream cameras.
- `CameraHubPage._can_start` / Start gating per decision 5 (the hub needs
  to know per camera whether it is single-stream; main window passes it
  with the camera card data).

### Metrics (`engine/metrics.py`, `engine/test_session.py`)

- New `LedDetectionMetric` (name `"led_detection"`), used instead of
  `PairingGapMetric` + `PositionGapMetric` for a single-stream camera:
  - thresholds stream A's brightness, `find_last_on_led`, stores
    `last_stream_a_on_mask` (for overlays/snapshots, like
    `PositionGapMetric`);
  - value = detected LED index (or `None`); `extra =
    {"stream_a_last_led": idx}`;
  - exclusions in the same priority order as `PositionGapMetric`:
    `no_led_data`, `miss`, `frame_drop`, `warmup`.
- `TestSession`: already tolerates `stream_b_ts_us=None` (never flags a B
  drop); add a test that locks this in.

### Cross-camera reconciler (`engine/cross_camera_reconciler.py`)

- `_compute_cross_position_gap`: per side, use `position_gap_ms_excluded` /
  `_exclude_reason` if the row has them (two-stream camera - unchanged),
  otherwise `led_detection_excluded` / `_exclude_reason` (single-stream
  camera). The frame-drop check stays first, as today. For a single-stream
  side, `frame_drop` is already handled by the existing check, so the
  fallback only contributes `warmup`/`no_led_data`/`miss`.
- `build_cross_camera_pair_specs`: unchanged.

### Session engine (`engine/session_engine.py`)

- `on_frames` emits only stream A's image when there is no B.
- `_maybe_save_position_gap_outlier`: no-op for single-stream cameras (its
  trigger, intra-camera `position_gap_ms`, does not exist). Cross-camera
  outlier images keep working through the multi-camera page's existing
  recent-frame path (role `stream_a`).
- The mask source for overlays is whichever metric provides
  `last_stream_a_on_mask` (`PositionGapMetric` or `LedDetectionMetric`).

### Multi-camera page and per-camera panel

- `multi_camera_live_session_page.py`:
  - `_stream_identities` omits `stream_b` when `pick_b` is None;
  - metric list: `[LedDetectionMetric]` for single-stream, else unchanged;
  - `TestSessionConfig.stream_b_fps` = `None`-safe for single-stream;
  - `prepare_for_run` / `thread_kwargs` pass B as `None`;
  - panel labels omit stream B.
- `CameraLiveSessionPanel`: a `single_stream` mode (set from the config)
  that hides the B video panel and the intra-camera HW TS / Optical Sync
  plots and stat tiles; keeps stream A frame drops (plot + count), shows
  the last detected LED index, and saves single-image periodic/manual
  snapshots (`draw_led_state_overlay` on stream A only).
- End-of-session export for a single-stream camera: raw/frame-drop CSVs as
  today (B columns empty); `domain/plot_export.py` produces a
  single-stream figure (stream A frame drops only, no intra-camera gap
  axes). The cross-camera exports are unchanged.

## Error handling

- Malformed single-stream test in settings.yaml (e.g. `stream_b` side in
  an option of a test with no `stream_b_identity`) -> `ValueError` at
  parse time, same as today's malformed-test handling.
- `ContinuousCapture` with one stream: missing HW-timestamp or global-ts
  metadata on stream A still raises the same `RuntimeError`s as today.
- Solo single-stream start is prevented at the Hub, and also guarded in
  `_on_start_multi_camera_session_requested` (message box) as a
  defense-in-depth check.

## Testing

All unit/widget tests, no hardware:

- `tests/engine/test_streams.py`: parse/resolve single-stream tests (valid
  and malformed); `resolve_and_group` with `pick_b=None`;
  `exposure_for_group` with `None`; `ContinuousCapture` config building
  (depth + A only) with one pick.
- `tests/engine/test_metrics.py`: `LedDetectionMetric` value, `extra`,
  each exclusion reason and their order.
- `tests/engine/test_test_session.py`: single-stream samples.
- `tests/engine/test_cross_camera_reconciler.py`: cross Optical Sync
  between two single-stream cameras, and single-stream vs two-stream
  (mixed); warmup exclusion from `led_detection_*`.
- `tests/domain/test_calibration.py`: single-slug write/read.
- Page/widget tests: Stream Config (single-stream test shown, exposure B
  hidden, dual panel disabled, emits `None`), ROI Select, Calibration,
  Threshold Tuning (B hidden, `None` properties), camera live-session
  panel single-stream layout and snapshots, multi-camera page
  (`_camera_config(pick_b=None)` variants, metric selection, identities).
- `tests/gui/test_main_window.py`: full single-stream sub-flow commit,
  Hub Start gating for a solo single-stream camera, single-panel target
  with one pick, conflict/GMSL checks skipping `None`.
- Existing suite must stay green: two-stream behavior is unchanged.

Real-hardware check (operator): two cameras, both on an "IR1 only" test,
single panel - verify cross-camera plots populate and the per-camera slim
tabs show the LED overlay.

## Out of scope

- Dual LED panel for single-stream cameras.
- Solo single-stream runs on `LiveSessionPage`.
- N streams per camera.
- Changing which stream slugs cross-camera pairing matches on.
