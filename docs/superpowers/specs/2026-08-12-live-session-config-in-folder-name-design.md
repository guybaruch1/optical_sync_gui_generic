# Live Session output folder names encode the run config

**Branch:** `fix/detect-stale-repeated-frame-as-drop` (per explicit instruction).

## Problem

`output/live_session_2026-08-12_10-48-16/` tells you nothing about what the run
was actually testing - resolution, fps, duration, exposure mode, frame-sample
interval, LED switch time all have to be looked up separately (opening the CSV
or remembering what the toolbar was set to). With many runs accumulating this
makes it hard to find "the 60fps manual-exposure run" without opening each one.

## Goal

Fold the run's own configuration into the Live Session output folder name, so it
reads at a glance from the file browser alone.

## Format (confirmed with the user)

```
live_session_<timestamp>_<width>x<height>_<fps>fps_<duration>s(or "unlimited")_<auto|manual+exposure>_interval<N>_switch<ms>ms
```

Examples:
```
live_session_2026-08-12_10-48-16_1280x720_30fps_200s_manual100_interval10_switch1ms
live_session_2026-08-12_10-48-16_1280x720_30fps_unlimited_auto_interval10_switch0.5ms
```

- **Manual exposure includes only the exposure value**, not gain (confirmed choice -
  matches the literal request, keeps the name shorter).
- **Resolution/fps use stream_a's own pick** (`ctx["pick_a"]["width"/"height"/"fps"]`)
  - every `settings.yaml` `camera.stream_options` sensor_options entry pairs
    stream_a/stream_b with matching width/height/fps, so stream_a's values
    represent the run without needing to reconcile two possibly-differing sets.
- **Duration** is `"unlimited"` when the toolbar's duration spinbox reads 0
  (`start_session()`'s existing `self.duration_spinbox.value() or None` already
  produces `None` for that case).
- Numbers that can be fractional (currently only `switch_time_ms`, a
  `QDoubleSpinBox`) format without a trailing `.0` for whole values (`1ms`, not
  `1.0ms`) but keep real fractions (`0.5ms`).

## Design

### `domain/run_output.py`

- New pure function `build_live_session_config_suffix(width, height, fps,
  duration_s, auto_exposure, exposure, display_stride, switch_time_ms)` -
  returns just the suffix string (no timestamp, no `live_session_` prefix) so
  it composes with `create_run_dir` below. No Qt, no hardware - fully
  unit-testable with plain values.
- `create_run_dir(output_root, kind, now=None, suffix=None)` gains one new
  optional param. When provided, appended after the timestamp:
  `{kind}_{timestamp}_{suffix}`. `None` (the default) preserves the exact
  current filename shape - `gui/pages/calibration_page.py`'s own
  `create_run_dir(output_root, "calibration")` call is untouched by this
  change and needs no update.
- Collision-avoidance numbering (`_2`, `_3`, ...) still appends after the
  *entire* name including the suffix, unchanged logic - just operates on a
  longer `base_name` string when a suffix is present.

### `gui/pages/live_session_page.py`

- `start_session()` already reads `duration_s`/`switch_time_ms`/`display_stride`
  from the toolbar spinboxes right after calling `_begin_new_run_output()` -
  reordered so those three reads happen FIRST, then the suffix is built from
  them plus `ctx["pick_a"]`/`ctx["camera_controls"]`, then
  `_begin_new_run_output(suffix=...)` is called. Nothing between the old and
  new call site depends on `ctx["output_dir"]` already being set (confirmed by
  reading the method) - `_clear_periodic_snapshots(ctx["output_dir"])`, the
  first thing that does, stays safely after the (now later) call.
- `_begin_new_run_output(self, suffix=None)` just forwards `suffix` through to
  `create_run_dir` - no other change to its body.

## Out of scope

- Calibration's own timestamped folders (`calibration_<timestamp>/`) are NOT
  changed - the request was specifically about `live_session_*` folders.
- No change to what's written to the CSVs/PNGs themselves, or to any existing
  filename inside the run folder - only the run folder's own name changes.

## Testing

- `build_live_session_config_suffix`: auto vs manual exposure, whole vs
  fractional `switch_time_ms`, `duration_s=None` -> `"unlimited"`, exact field
  order/format for a realistic full example. (`tests/domain/test_run_output.py`)
- `create_run_dir`: `suffix=None` (default) produces the exact same filename as
  before (regression guard); `suffix="foo"` appends `_foo`; collision numbering
  still appends after a provided suffix. (`tests/domain/test_run_output.py`)
- `LiveSessionPage.start_session()`: existing tests
  (`test_begin_new_run_output_creates_a_fresh_folder_under_output_root`,
  `test_two_start_session_calls_use_two_different_run_folders`) must keep
  passing - full-suite regression check is the primary guard here, no new
  GUI-layer test needed since the suffix-building logic itself is covered at
  the pure-function level.
