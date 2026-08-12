# Optical Sync outlier debug images

**Branch:** `fix/detect-stale-repeated-frame-as-drop` (per explicit instruction - lands
alongside the unrelated `_is_frame_drop` delta==0 fix already on this branch).

## Problem

When Live Session's "Optical Sync" reading (`position_gap_ms`) shows a large value,
there is currently no way to tell, after the fact, whether that was a real physical
desync between the two streams or an artifact of the LED on/off detection algorithm
(e.g. a borderline threshold call). The raw frames that produced an outlier reading
are never saved - only the scalar CSV row survives.

## Goal

For every frame pair where `|position_gap_ms| >= 5` (and the pair wasn't already
excluded for another reason), save a side-by-side IR/RGB debug image - same on/off
overlay style as the existing periodic LED-state snapshots - so the operator can
visually confirm whether the LEDs really were out of sync in that frame.

## Key constraint driving the design

`position_gap_ms` is computed for **every** frame pair inside
`TestSession.process_pair()`, but the GUI/callback layer (`AcquisitionLoop`'s
`on_frames`/`on_stats`) only sees a throttled subset (every `display_stride`-th pair,
default 10) - the raw images for in-between pairs are used for LED-brightness
sampling and then discarded. Checking only the throttled subset would miss the large
majority of individual outlier pairs (confirmed relevant from the earlier
`_is_frame_drop` investigation this session, where a similar throttling gap hid a
real phenomenon). This must run on every pair, not just the displayed ones.

## Design

### 1. `engine/acquisition_loop.py` - new optional unthrottled hook

Add one new field to `AcquisitionCallbacks`:

```python
@dataclass
class AcquisitionCallbacks:
    on_frames: callable
    on_row: callable
    on_stats: callable
    on_frame_pair: "callable | None" = None
```

Called unconditionally every iteration (right after `on_row`, before the
`display_stride` throttle check), only when not `None`:

```python
row = self.test_session.process_pair(sample)
self.callbacks.on_row(row)
if self.callbacks.on_frame_pair is not None:
    self.callbacks.on_frame_pair(stream_a_image, stream_b_image, row)

if pair_index % self.display_stride == 0:
    self.callbacks.on_frames(stream_a_image, stream_b_image, pair_index)
    self.callbacks.on_stats(row)
```

Default `None` keeps every existing caller (`SessionEngineThread`,
`tools/panel_drift/panel_drift_measure.py`, all `test_acquisition_loop.py` tests)
unaffected - none of them pass a 4th field today.

### 2. `engine/metrics.py` - pure decision function (unit-testable)

```python
def is_position_gap_debug_outlier(row, threshold_ms):
    value = row.get("position_gap_ms")
    if value is None or row.get("position_gap_ms_excluded"):
        return False
    return abs(value) >= threshold_ms
```

- Magnitude-based (`abs(value) >= threshold_ms`) - both directions of desync count.
- Skips rows already excluded for another reason (`frame_drop`/`warmup`/etc.) - those
  already have a known cause, per explicit decision during design.
- `>=`, not `>`, matching "delta above or equal to 5" from the request.

### 3. `engine/session_engine.py` - the actual write, on the background thread

New constructor params: `output_dir=None`, `position_gap_outlier_threshold_ms=None`,
`position_gap_outlier_max_snapshots=200`. New instance counter
`self._position_gap_outlier_count = 0`.

`run()` wires a new local callback:

```python
def on_frame_pair(stream_a_image, stream_b_image, row):
    self._maybe_save_position_gap_outlier(stream_a_image, stream_b_image, row)
```

passed into `AcquisitionCallbacks(..., on_frame_pair=on_frame_pair)`.

`_maybe_save_position_gap_outlier` mirrors the existing
`LiveSessionPage._maybe_save_periodic_snapshot` pattern (same
`draw_led_state_overlay` + `combine_side_by_side` + `cv2.imwrite` calls), but reads
the on/off masks off `self.position_gap_metric.last_stream_a_on_mask`/
`last_stream_b_on_mask` (already updated for the current pair by the
`process_pair()` call that just ran) instead of a GUI-cached copy, and is gated by:

- `is_position_gap_debug_outlier(row, self.position_gap_outlier_threshold_ms)`
- `self._position_gap_outlier_count < self.position_gap_outlier_max_snapshots`
  (the safety cap - a sustained real desync could otherwise flag many consecutive
  pairs and write a very large number of files / slow the capture loop with disk
  I/O)

No Qt signal is involved in the write itself - it happens synchronously on the
background capture thread, so it cannot reintroduce the GUI-thread signal-backlog
freeze documented elsewhere in this codebase (that was specifically about queued
cross-thread Qt work, not background-thread file I/O).

**Filename:** `optical_sync_outlier_pair{pair_index:05d}.png` in the run's own
output folder (fresh per-run timestamped folder already exists - no stale-file
collision risk, no clear-on-start needed).

### 4. Settings plumbing (`settings.yaml` -> `MainWindow` -> `LiveSessionPage` -> `SessionEngineThread`)

New `settings.yaml` keys under `test:`, next to `pairing_gap_outlier_threshold_us`:

```yaml
position_gap_outlier_threshold_ms: 5
position_gap_outlier_max_snapshots: 200
```

Threaded through `MainWindow._pending_ctx` -> `LiveSessionPage.set_context()`'s
`ctx` dict -> `SessionEngineThread(...)` constructor call in `start_session()`,
exactly the same path `pairing_gap_outlier_threshold_us`/`max_snapshots` already
follow.

## Out of scope

- No new CSV columns, no change to `PositionGapMetric`'s own exclusion logic or
  `MetricResult`/`exclude_reason` - this is a side-channel debug-image trigger only.
- `tools/panel_drift/panel_drift_measure.py` (a separate standalone tool that also
  uses `AcquisitionLoop`) is unaffected - it doesn't pass `on_frame_pair`.
- No GUI-visible feedback when an outlier image is saved (matches the existing
  periodic-snapshot precedent, which is also silent).

## Testing

- `is_position_gap_debug_outlier`: boundary at exactly `threshold_ms` (`>=`, not
  `>`), both positive and negative magnitudes, already-excluded rows return
  `False`, `None` value returns `False`. (`tests/engine/test_metrics.py`)
- `AcquisitionLoop`: `on_frame_pair` fires on every pair (not throttled), receives
  `(stream_a_image, stream_b_image, row)`; omitting it (`None`, the default) does
  not error. (`tests/engine/test_acquisition_loop.py`)
- `SessionEngineThread`'s own write path stays untested by design, same bucket as
  the rest of that file (hardware/Qt-thread code, per CLAUDE.md).
- Full existing suite must still pass with zero regressions.
