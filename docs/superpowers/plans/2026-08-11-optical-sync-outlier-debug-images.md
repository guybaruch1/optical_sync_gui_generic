# Optical Sync Outlier Debug Images Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When Live Session's Optical Sync reading (`position_gap_ms`) magnitude hits >= a configurable threshold (default 5ms) on a non-excluded pair, save a side-by-side IR/RGB debug image (same on/off overlay style as the existing periodic LED-state snapshots) so the operator can visually confirm whether it was a real desync or a detection-algorithm artifact.

**Architecture:** `TestSession.process_pair()` already computes `position_gap_ms` for every single frame pair, unthrottled - only the GUI-facing callbacks (`on_frames`/`on_stats`) are throttled to a `display_stride` subset. Add one new optional, unthrottled `AcquisitionCallbacks.on_frame_pair` hook so `SessionEngineThread` can check every pair's outlier status and write a debug image directly on the background capture thread (no Qt signal involved, so no GUI-thread risk).

**Tech Stack:** Python, PySide6/Qt (QThread), OpenCV (`cv2.imwrite`), existing `domain.realsense_utils.draw_led_state_overlay`/`combine_side_by_side`.

## Global Constraints

- Branch: `fix/detect-stale-repeated-frame-as-drop` (explicit instruction - this feature lands alongside the unrelated `_is_frame_drop` fix already on this branch).
- Threshold check is magnitude-based: `abs(position_gap_ms) >= threshold_ms` (both directions of desync count), matching "delta above or equal to 5" from the request.
- Only fires on pairs NOT already excluded for another reason (`position_gap_ms_excluded` must be falsy) - frame_drop/warmup pairs already have a known cause.
- Must catch every qualifying pair, not just the throttled subset the GUI displays - the whole point is to catch pairs that wouldn't otherwise be visible.
- Must not touch the GUI thread / Qt signals for the actual file write - background-thread-only, to avoid any risk of reintroducing the documented GUI-freeze bug.
- Safety cap on total images saved per session (default 200), same pattern as the existing `max_snapshots` for periodic snapshots.
- Full existing test suite (`pytest -v`, `QT_QPA_PLATFORM=offscreen` on this Windows/offscreen setup) must keep passing with zero regressions after every task.
- No new CSV columns, no change to `PositionGapMetric`'s own `MetricResult`/`exclude_reason` logic - this is a side-channel debug-image trigger only.

---

### Task 1: Pure outlier-decision function in `engine/metrics.py`

**Files:**
- Modify: `engine/metrics.py` (add function at end of file, after the `PositionGapMetric` class)
- Test: `tests/engine/test_metrics.py` (add tests at end of file)

**Interfaces:**
- Consumes: nothing new - reads plain dict keys `"position_gap_ms"`/`"position_gap_ms_excluded"` off the `row` dict `TestSession.process_pair()` already produces (see `engine/test_session.py`).
- Produces: `is_position_gap_debug_outlier(row: dict, threshold_ms: float) -> bool`, consumed by Task 3.

- [ ] **Step 1: Write the failing tests**

Add to the end of `tests/engine/test_metrics.py`:

```python
def test_is_position_gap_debug_outlier_true_at_exact_positive_threshold():
    # >=, not >, matching "delta above or equal to 5".
    row = {"position_gap_ms": 5.0, "position_gap_ms_excluded": False}
    assert is_position_gap_debug_outlier(row, threshold_ms=5.0) is True


def test_is_position_gap_debug_outlier_true_at_exact_negative_threshold():
    # Magnitude-based - a -5ms gap is just as much an outlier as +5ms.
    row = {"position_gap_ms": -5.0, "position_gap_ms_excluded": False}
    assert is_position_gap_debug_outlier(row, threshold_ms=5.0) is True


def test_is_position_gap_debug_outlier_false_below_threshold():
    row = {"position_gap_ms": 4.9, "position_gap_ms_excluded": False}
    assert is_position_gap_debug_outlier(row, threshold_ms=5.0) is False


def test_is_position_gap_debug_outlier_false_when_already_excluded():
    # A frame_drop/warmup-excluded row already has a known cause - don't
    # also flag it as an unexplained optical-sync outlier.
    row = {"position_gap_ms": 50.0, "position_gap_ms_excluded": True}
    assert is_position_gap_debug_outlier(row, threshold_ms=5.0) is False


def test_is_position_gap_debug_outlier_false_when_value_is_none():
    # no_led_data/miss rows carry value=None - nothing to threshold against.
    row = {"position_gap_ms": None, "position_gap_ms_excluded": True}
    assert is_position_gap_debug_outlier(row, threshold_ms=5.0) is False
```

Also update the import line near the top of `tests/engine/test_metrics.py` from:

```python
from engine.metrics import (
    FramePairSample,
    find_last_on_led,
    compute_position_gap,
    PairingGapMetric,
    PositionGapMetric,
    _is_frame_drop,
)
```

to:

```python
from engine.metrics import (
    FramePairSample,
    find_last_on_led,
    compute_position_gap,
    PairingGapMetric,
    PositionGapMetric,
    _is_frame_drop,
    is_position_gap_debug_outlier,
)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/engine/test_metrics.py -v`
Expected: FAIL with `ImportError: cannot import name 'is_position_gap_debug_outlier'`

- [ ] **Step 3: Write the implementation**

Add to the end of `engine/metrics.py` (after the `PositionGapMetric` class):

```python
def is_position_gap_debug_outlier(row, threshold_ms):
    """Decides whether a frame pair's position_gap_ms ("Optical Sync" in the
    UI) is large enough, and not already explained by another exclusion
    reason, to be worth saving a side-by-side IR/RGB debug image for - see
    engine/session_engine.py's _maybe_save_position_gap_outlier, the only
    caller. Deliberately independent of PositionGapMetric's own exclusion
    logic (no new MetricResult/exclude_reason) - this is a side-channel
    debug-image trigger, not a metric change.

    Magnitude-based (abs(value) >= threshold_ms): a large negative gap is
    just as much an outlier as a large positive one. Already-excluded rows
    (frame_drop/warmup/no_led_data/miss) return False - those already have a
    known cause, and no_led_data/miss rows carry value=None anyway."""
    value = row.get("position_gap_ms")
    if value is None or row.get("position_gap_ms_excluded"):
        return False
    return abs(value) >= threshold_ms
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/engine/test_metrics.py -v`
Expected: all PASS (previous tests + 5 new ones)

- [ ] **Step 5: Commit**

```bash
git add engine/metrics.py tests/engine/test_metrics.py
git commit -m "feat: add is_position_gap_debug_outlier pure decision function"
```

---

### Task 2: Unthrottled `on_frame_pair` hook in `engine/acquisition_loop.py`

**Files:**
- Modify: `engine/acquisition_loop.py`
- Test: `tests/engine/test_acquisition_loop.py` (add tests at end of file)

**Interfaces:**
- Consumes: nothing new.
- Produces: `AcquisitionCallbacks.on_frame_pair: "callable | None" = None` field, and the guarantee that when set, it is called as `on_frame_pair(stream_a_image, stream_b_image, row)` on **every** pair (not throttled by `display_stride`), immediately after `on_row(row)`. Consumed by Task 3.

- [ ] **Step 1: Write the failing tests**

Add to the end of `tests/engine/test_acquisition_loop.py`:

```python
def test_run_until_stopped_calls_on_frame_pair_every_pair_unthrottled():
    session = TestSession(TestSessionConfig(metrics=[CountingMetric()]))
    session.start()
    frame_pairs_seen = []
    callbacks = AcquisitionCallbacks(
        on_frames=lambda stream_a, stream_b, idx: None,
        on_row=lambda row: None,
        on_stats=lambda stats: None,
        on_frame_pair=lambda stream_a, stream_b, row: frame_pairs_seen.append(row["pair_index"]),
    )
    loop = AcquisitionLoop(fake_frame_source(5), session, callbacks, display_stride=10)
    loop.run_until_stopped(is_stop_requested=lambda: False, elapsed_s_fn=lambda: 0.0)

    # Unlike on_frames (throttled to every display_stride pairs), on_frame_pair
    # fires for every single pair - it must be able to see pairs the GUI's
    # own throttled callbacks never do.
    assert frame_pairs_seen == [0, 1, 2, 3, 4]


def test_run_until_stopped_works_without_on_frame_pair():
    # on_frame_pair is optional (defaults to None) - every existing caller
    # that doesn't pass it (SessionEngineThread before this change,
    # tools/panel_drift/panel_drift_measure.py, the other tests in this
    # file) must keep working unchanged.
    session = TestSession(TestSessionConfig(metrics=[CountingMetric()]))
    session.start()
    callbacks = AcquisitionCallbacks(on_frames=lambda *a: None, on_row=lambda r: None, on_stats=lambda s: None)
    loop = AcquisitionLoop(fake_frame_source(3), session, callbacks, display_stride=10)
    rows = loop.run_until_stopped(is_stop_requested=lambda: False, elapsed_s_fn=lambda: 0.0)
    assert len(rows) == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/engine/test_acquisition_loop.py -v`
Expected: `test_run_until_stopped_calls_on_frame_pair_every_pair_unthrottled` FAILs with `TypeError: __init__() got an unexpected keyword argument 'on_frame_pair'`. `test_run_until_stopped_works_without_on_frame_pair` passes already (no behavior change needed for that case) - that's fine, it's a regression guard for the next step.

- [ ] **Step 3: Write the implementation**

In `engine/acquisition_loop.py`, change:

```python
@dataclass
class AcquisitionCallbacks:
    on_frames: callable
    on_row: callable
    on_stats: callable
```

to:

```python
@dataclass
class AcquisitionCallbacks:
    on_frames: callable
    on_row: callable
    on_stats: callable
    on_frame_pair: "callable | None" = None
```

Then change the body of `run_until_stopped`:

```python
            row = self.test_session.process_pair(sample)
            self.callbacks.on_row(row)

            if pair_index % self.display_stride == 0:
                self.callbacks.on_frames(stream_a_image, stream_b_image, pair_index)
                self.callbacks.on_stats(row)
```

to:

```python
            row = self.test_session.process_pair(sample)
            self.callbacks.on_row(row)
            if self.callbacks.on_frame_pair is not None:
                self.callbacks.on_frame_pair(stream_a_image, stream_b_image, row)

            if pair_index % self.display_stride == 0:
                self.callbacks.on_frames(stream_a_image, stream_b_image, pair_index)
                self.callbacks.on_stats(row)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/engine/test_acquisition_loop.py -v`
Expected: all PASS (previous tests + 2 new ones)

- [ ] **Step 5: Run the full suite to check for regressions**

Run: `QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest -q`
Expected: all PASS, same total count as before plus the 7 new tests from Task 1+2 (this also exercises `tools/panel_drift/panel_drift_measure.py`'s own `AcquisitionCallbacks(...)` construction indirectly via its own tests, if any - confirms the new optional field didn't break it).

- [ ] **Step 6: Commit**

```bash
git add engine/acquisition_loop.py tests/engine/test_acquisition_loop.py
git commit -m "feat: add unthrottled on_frame_pair hook to AcquisitionLoop"
```

---

### Task 3: Wire the debug-image write into `engine/session_engine.py`

**Files:**
- Modify: `engine/session_engine.py`

**Interfaces:**
- Consumes: `is_position_gap_debug_outlier(row, threshold_ms)` from Task 1; `AcquisitionCallbacks.on_frame_pair` from Task 2; `domain.realsense_utils.draw_led_state_overlay(image, xy_positions, on_mask)` and `combine_side_by_side(image_a, image_b)` (both already used identically by `gui/pages/live_session_page.py`'s `_maybe_save_periodic_snapshot`).
- Produces: `SessionEngineThread.__init__` gains three new keyword params - `output_dir=None`, `position_gap_outlier_threshold_ms=None`, `position_gap_outlier_max_snapshots=200` - consumed by Task 4's `LiveSessionPage.start_session()` call site.

No automated test for this task - `engine/session_engine.py` has no automated tests by design (hardware/Qt-thread code, same bucket as the rest of that file per `CLAUDE.md`). Verification is: full suite still passes (nothing here is exercised by tests, so this confirms no import/syntax errors), plus a manual code-review read-through in Step 4.

- [ ] **Step 1: Update imports**

In `engine/session_engine.py`, change:

```python
import pyrealsense2 as rs
from PySide6.QtCore import QThread, Signal

from engine.acquisition_loop import AcquisitionLoop, AcquisitionCallbacks
from engine.streams import (
    ContinuousCapture, find_device_by_serial, resolve_and_group,
    set_emitter_enabled, enable_auto_exposure, set_manual_exposure,
)
from engine.dual_panel_control import start_scanning, stop_scanning
from domain.realsense_utils import sample_all_neighborhood_brightness, safe_neighborhood_size
```

to:

```python
import os

import cv2
import pyrealsense2 as rs
from PySide6.QtCore import QThread, Signal

from engine.acquisition_loop import AcquisitionLoop, AcquisitionCallbacks
from engine.metrics import is_position_gap_debug_outlier
from engine.streams import (
    ContinuousCapture, find_device_by_serial, resolve_and_group,
    set_emitter_enabled, enable_auto_exposure, set_manual_exposure,
)
from engine.dual_panel_control import start_scanning, stop_scanning
from domain.realsense_utils import (
    sample_all_neighborhood_brightness, safe_neighborhood_size,
    draw_led_state_overlay, combine_side_by_side,
)
```

- [ ] **Step 2: Extend `__init__`**

Change the signature:

```python
    def __init__(self, ctx, device_serial, pick_a, pick_b, camera_controls,
                 test_session, stream_a_xy=None, stream_b_xy=None, neighborhood_size=5,
                 scan_direction=None, switch_time_ms=None,
                 display_stride=10, position_gap_metric=None, dual_panel_config=None,
                 enable_depth_for_ir_sync=True, hardware_reset_before_start=False,
                 hardware_reset_settle_s=8.0, parent=None):
```

to:

```python
    def __init__(self, ctx, device_serial, pick_a, pick_b, camera_controls,
                 test_session, stream_a_xy=None, stream_b_xy=None, neighborhood_size=5,
                 scan_direction=None, switch_time_ms=None,
                 display_stride=10, position_gap_metric=None, dual_panel_config=None,
                 enable_depth_for_ir_sync=True, hardware_reset_before_start=False,
                 hardware_reset_settle_s=8.0, output_dir=None,
                 position_gap_outlier_threshold_ms=None, position_gap_outlier_max_snapshots=200,
                 parent=None):
```

Then, right after the existing line `self.position_gap_metric = position_gap_metric`, add:

```python
        # Optical Sync outlier debug images - see _maybe_save_position_gap_outlier.
        # output_dir/position_gap_outlier_threshold_ms are both None-able (rather
        # than required) so tests/tools constructing this class without wiring
        # this feature (e.g. any future direct construction that doesn't care
        # about it) don't need to pass them - _maybe_save_position_gap_outlier
        # is a no-op when either is None.
        self.output_dir = output_dir
        self.position_gap_outlier_threshold_ms = position_gap_outlier_threshold_ms
        self.position_gap_outlier_max_snapshots = position_gap_outlier_max_snapshots
        self._position_gap_outlier_count = 0
```

- [ ] **Step 3: Add the `_maybe_save_position_gap_outlier` method**

Add this new method right after `_frame_pairs_with_brightness` (i.e. right before `def run(self):`):

```python
    def _maybe_save_position_gap_outlier(self, stream_a_image, stream_b_image, row):
        """Saves a side-by-side IR/RGB debug image (same on/off overlay style
        as gui/pages/live_session_page.py's periodic LED-state snapshots) for
        a pair whose position_gap_ms ("Optical Sync") magnitude crossed
        position_gap_outlier_threshold_ms - lets the operator check by eye
        whether a given outlier reading was a real physical desync or an
        algorithm/detection artifact.

        Runs synchronously on THIS background thread, called from
        AcquisitionLoop's unthrottled on_frame_pair hook (unlike the periodic
        snapshot above, which only sees the throttled display_stride subset
        on_frames gets) - it deliberately does NOT go through a Qt signal,
        since the write itself doesn't need the GUI thread at all and most
        individual outlier pairs wouldn't otherwise land on a displayed
        sample."""
        if self.output_dir is None or self.position_gap_outlier_threshold_ms is None:
            return
        if self.position_gap_metric is None:
            return
        if not is_position_gap_debug_outlier(row, self.position_gap_outlier_threshold_ms):
            return
        if self._position_gap_outlier_count >= self.position_gap_outlier_max_snapshots:
            return

        stream_a_mask = self.position_gap_metric.last_stream_a_on_mask
        stream_b_mask = self.position_gap_metric.last_stream_b_on_mask
        if stream_a_mask is None or stream_b_mask is None:
            return

        pair_index = row["pair_index"]
        path = os.path.join(self.output_dir, "optical_sync_outlier_pair{:05d}.png".format(pair_index))
        stream_a_debug = draw_led_state_overlay(stream_a_image, self.stream_a_xy, stream_a_mask)
        stream_b_debug = draw_led_state_overlay(stream_b_image, self.stream_b_xy, stream_b_mask)
        cv2.imwrite(path, combine_side_by_side(stream_a_debug, stream_b_debug))
        self._position_gap_outlier_count += 1
```

- [ ] **Step 4: Wire the new callback into `run()`**

In `run()`, change:

```python
            def on_row(row):
                self.row_ready.emit(row)

            def on_stats(stats):
                self.stats_ready.emit(stats)

            callbacks = AcquisitionCallbacks(on_frames=on_frames, on_row=on_row, on_stats=on_stats)
```

to:

```python
            def on_row(row):
                self.row_ready.emit(row)

            def on_stats(stats):
                self.stats_ready.emit(stats)

            def on_frame_pair(stream_a_image, stream_b_image, row):
                self._maybe_save_position_gap_outlier(stream_a_image, stream_b_image, row)

            callbacks = AcquisitionCallbacks(
                on_frames=on_frames, on_row=on_row, on_stats=on_stats, on_frame_pair=on_frame_pair,
            )
```

Then re-read the full method (`run()`) top to bottom once to confirm nothing else references `callbacks` in a way that would break from this change, and that indentation/placement matches the surrounding code exactly.

- [ ] **Step 5: Run the full suite to check for regressions**

Run: `QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest -q`
Expected: all PASS, same count as after Task 2 (this task adds no new tests - it only touches untested-by-design hardware-thread code - so this step exists purely to catch an import/syntax error).

- [ ] **Step 6: Commit**

```bash
git add engine/session_engine.py
git commit -m "feat: write Optical Sync outlier debug images from SessionEngineThread"
```

---

### Task 4: Settings + GUI plumbing

**Files:**
- Modify: `settings.yaml`
- Modify: `gui/pages/live_session_page.py`
- Modify: `gui/main_window.py`

**Interfaces:**
- Consumes: `SessionEngineThread.__init__`'s new `output_dir`/`position_gap_outlier_threshold_ms`/`position_gap_outlier_max_snapshots` params from Task 3.
- Produces: `settings.yaml`'s `test.position_gap_outlier_threshold_ms`/`test.position_gap_outlier_max_snapshots` reach `SessionEngineThread` at Start, exactly mirroring how `test.pairing_gap_outlier_threshold_us`/`test.max_snapshots` already do.

No new automated tests for this task - it's pure plumbing of already-tested values through GUI code with no branching logic of its own (same category as the other `ctx[...]` passthroughs in this file, none of which have dedicated tests). Verification is the full suite (to catch a wiring typo breaking any existing GUI test that constructs `LiveSessionPage.set_context()`/`MainWindow`) plus a manual read-through.

- [ ] **Step 1: Add the new settings.yaml keys**

In `settings.yaml`, under the `test:` section, change:

```yaml
  # analyze_pairing_gap's cross-stream sanity-check threshold, in
  # MICROSECONDS (stream_a_ts_us/stream_b_ts_us are HW frame_timestamp,
  # µs-scale). 100_000us is the same real-world cutoff as an old 100ms
  # default would have been.
  pairing_gap_outlier_threshold_us: 100000
  # Ignore this many frame-pairs at the START of the run when computing the
```

to:

```yaml
  # analyze_pairing_gap's cross-stream sanity-check threshold, in
  # MICROSECONDS (stream_a_ts_us/stream_b_ts_us are HW frame_timestamp,
  # µs-scale). 100_000us is the same real-world cutoff as an old 100ms
  # default would have been.
  pairing_gap_outlier_threshold_us: 100000
  # Save a side-by-side IR/RGB debug image (same on/off overlay style as the
  # periodic LED-state snapshots above) for every pair whose position_gap_ms
  # ("Optical Sync" in the UI) magnitude is >= this many ms - lets you check
  # by eye whether a given outlier reading was a real physical desync or an
  # algorithm/detection artifact. Checked on EVERY pair (not throttled to
  # display_stride, unlike the periodic snapshots above), since most
  # individual outlier pairs wouldn't otherwise land on a displayed sample.
  # Only fires on pairs position_gap_ms did NOT already exclude for another
  # reason (frame_drop/warmup) - those already have a known cause.
  position_gap_outlier_threshold_ms: 5
  # Safety cap, same idea as max_snapshots above - a sustained real desync
  # could otherwise flag many consecutive pairs and write a very large
  # number of files / slow the capture loop with disk I/O.
  position_gap_outlier_max_snapshots: 200
  # Ignore this many frame-pairs at the START of the run when computing the
```

- [ ] **Step 2: Extend `LiveSessionPage.set_context`**

In `gui/pages/live_session_page.py`, change the method signature:

```python
    def set_context(self, ctx, device_serial, pick_a, pick_b, camera_controls, switch_time_ms, scan_direction,
                     stream_a_threshold, stream_b_threshold,
                     stream_a_xy, stream_b_xy, num_leds, neighborhood_size,
                     frame_drop_threshold_factor, warmup_pairs_to_skip, pairing_gap_outlier_threshold_us,
                     output_root, kept_csv_filename, dropped_csv_filename, snapshot_every_n_pairs, max_snapshots,
                     stream_a_roi, stream_b_roi, camera_name, stream_a_label, stream_b_label,
                     dual_panel_config=None, enable_depth_for_ir_sync=True,
                     hardware_reset_before_start=False, hardware_reset_settle_s=8.0):
```

to:

```python
    def set_context(self, ctx, device_serial, pick_a, pick_b, camera_controls, switch_time_ms, scan_direction,
                     stream_a_threshold, stream_b_threshold,
                     stream_a_xy, stream_b_xy, num_leds, neighborhood_size,
                     frame_drop_threshold_factor, warmup_pairs_to_skip, pairing_gap_outlier_threshold_us,
                     position_gap_outlier_threshold_ms, position_gap_outlier_max_snapshots,
                     output_root, kept_csv_filename, dropped_csv_filename, snapshot_every_n_pairs, max_snapshots,
                     stream_a_roi, stream_b_roi, camera_name, stream_a_label, stream_b_label,
                     dual_panel_config=None, enable_depth_for_ir_sync=True,
                     hardware_reset_before_start=False, hardware_reset_settle_s=8.0):
```

And inside that method, change:

```python
            pairing_gap_outlier_threshold_us=pairing_gap_outlier_threshold_us,
            # Raw root + filename templates, not a pre-joined output_dir/
```

to:

```python
            pairing_gap_outlier_threshold_us=pairing_gap_outlier_threshold_us,
            position_gap_outlier_threshold_ms=position_gap_outlier_threshold_ms,
            position_gap_outlier_max_snapshots=position_gap_outlier_max_snapshots,
            # Raw root + filename templates, not a pre-joined output_dir/
```

- [ ] **Step 3: Pass the new values into `SessionEngineThread` in `start_session()`**

In `gui/pages/live_session_page.py`'s `start_session()`, change:

```python
        self.engine_thread = SessionEngineThread(
            ctx["ctx"], ctx["device_serial"], ctx["pick_a"], ctx["pick_b"], ctx["camera_controls"], test_session,
            stream_a_xy=ctx["stream_a_xy"], stream_b_xy=ctx["stream_b_xy"], neighborhood_size=ctx["neighborhood_size"],
            scan_direction=ctx["scan_direction"], switch_time_ms=switch_time_ms,
            display_stride=display_stride, position_gap_metric=position_gap_metric,
            dual_panel_config=ctx["dual_panel_config"],
            enable_depth_for_ir_sync=ctx["enable_depth_for_ir_sync"],
            hardware_reset_before_start=ctx["hardware_reset_before_start"],
            hardware_reset_settle_s=ctx["hardware_reset_settle_s"],
        )
```

to:

```python
        self.engine_thread = SessionEngineThread(
            ctx["ctx"], ctx["device_serial"], ctx["pick_a"], ctx["pick_b"], ctx["camera_controls"], test_session,
            stream_a_xy=ctx["stream_a_xy"], stream_b_xy=ctx["stream_b_xy"], neighborhood_size=ctx["neighborhood_size"],
            scan_direction=ctx["scan_direction"], switch_time_ms=switch_time_ms,
            display_stride=display_stride, position_gap_metric=position_gap_metric,
            dual_panel_config=ctx["dual_panel_config"],
            enable_depth_for_ir_sync=ctx["enable_depth_for_ir_sync"],
            hardware_reset_before_start=ctx["hardware_reset_before_start"],
            hardware_reset_settle_s=ctx["hardware_reset_settle_s"],
            output_dir=ctx["output_dir"],
            position_gap_outlier_threshold_ms=ctx["position_gap_outlier_threshold_ms"],
            position_gap_outlier_max_snapshots=ctx["position_gap_outlier_max_snapshots"],
        )
```

(`ctx["output_dir"]` is already set by `_begin_new_run_output()`, called at the top of `start_session()` before this point - confirm this by checking that `self._begin_new_run_output()` appears earlier in the same method before reaching this edit.)

- [ ] **Step 4: Thread the settings through `MainWindow`**

In `gui/main_window.py`, in the `_pending_ctx = dict(...)` block, change:

```python
            pairing_gap_outlier_threshold_us=self.settings["test"]["pairing_gap_outlier_threshold_us"],
            output_root=ensure_output_dir(self.settings),
```

to:

```python
            pairing_gap_outlier_threshold_us=self.settings["test"]["pairing_gap_outlier_threshold_us"],
            position_gap_outlier_threshold_ms=self.settings["test"]["position_gap_outlier_threshold_ms"],
            position_gap_outlier_max_snapshots=self.settings["test"]["position_gap_outlier_max_snapshots"],
            output_root=ensure_output_dir(self.settings),
```

Then in `_on_tuning_done()`'s call to `self.live_session_page.set_context(...)`, change:

```python
            pairing_gap_outlier_threshold_us=pending["pairing_gap_outlier_threshold_us"],
            output_root=pending["output_root"],
```

to:

```python
            pairing_gap_outlier_threshold_us=pending["pairing_gap_outlier_threshold_us"],
            position_gap_outlier_threshold_ms=pending["position_gap_outlier_threshold_ms"],
            position_gap_outlier_max_snapshots=pending["position_gap_outlier_max_snapshots"],
            output_root=pending["output_root"],
```

- [ ] **Step 5: Run the full suite to check for regressions**

Run: `QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest -q`
Expected: all PASS, same count as after Task 3. If any GUI test constructs `LiveSessionPage.set_context(...)` or `MainWindow`'s tuning-done flow with positional args instead of keywords, a mismatched new required positional param would break it here - if that happens, check `tests/gui/pages/test_live_session_page.py` and `tests/gui/test_main_window.py` for the call sites and fix them to pass the two new values (read the failing test's traceback for the exact call site and add `position_gap_outlier_threshold_ms=5, position_gap_outlier_max_snapshots=200` - or whatever fixture value matches that test's existing style - to the failing call).

- [ ] **Step 6: Commit**

```bash
git add settings.yaml gui/pages/live_session_page.py gui/main_window.py
git commit -m "feat: thread position_gap_outlier settings through to SessionEngineThread"
```

---

### Task 5: Document in CLAUDE.md and final verification

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: nothing (documentation only).
- Produces: nothing consumed by other tasks - this is the last task.

- [ ] **Step 1: Add a CLAUDE.md subsection**

In `CLAUDE.md`, right after the `### _is_frame_drop treats a repeated timestamp as a drop too, not just backwards/too-slow` subsection (added in an earlier commit on this same branch) and before `### Live Session pipeline (the core runtime loop)`, insert:

```markdown
### Optical Sync outlier debug images run on the background thread, not throttled

`engine/acquisition_loop.py`'s `AcquisitionCallbacks` has a fourth, optional
`on_frame_pair(stream_a_image, stream_b_image, row)` field, called unconditionally
every pair (unlike `on_frames`/`on_stats`, both throttled to `display_stride`).
`engine/session_engine.py`'s `SessionEngineThread` wires this to
`_maybe_save_position_gap_outlier`, which saves a side-by-side IR/RGB debug image
(same `draw_led_state_overlay`/`combine_side_by_side` calls as the existing
periodic LED-state snapshot) whenever `engine.metrics.is_position_gap_debug_outlier`
says a pair's `position_gap_ms` ("Optical Sync") magnitude crossed
`settings.yaml`'s `test.position_gap_outlier_threshold_ms` (default 5) - lets the
operator check by eye whether a given outlier reading was a real physical desync or
a detection-algorithm artifact.

This has to run unthrottled and on the background capture thread on purpose:
`position_gap_ms` is computed every pair inside `TestSession.process_pair()`, but
the GUI-facing callbacks only ever see a throttled `display_stride` subset (default
every 10th pair) - checking only that subset would miss most individual outlier
pairs. The write itself never touches a Qt signal, so it can't reintroduce the
GUI-thread signal-backlog freeze documented in the "Live Session pipeline" section
below (that bug was specifically about queued cross-thread Qt work, not
background-thread file I/O). `position_gap_outlier_max_snapshots` (default 200)
caps how many images one session can write, the same safety-cap idea as
`max_snapshots` for the periodic snapshots.

Deliberately independent of `PositionGapMetric`'s own exclusion logic - no new CSV
column, no new `exclude_reason` - `is_position_gap_debug_outlier` only reads the
row's already-computed `position_gap_ms`/`position_gap_ms_excluded` and returns
`False` for anything already excluded for another reason (frame_drop/warmup/etc.),
since those already have a known cause.
```

- [ ] **Step 2: Run the full test suite one final time**

Run: `QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest -q`
Expected: all PASS.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document Optical Sync outlier debug images in CLAUDE.md"
```
