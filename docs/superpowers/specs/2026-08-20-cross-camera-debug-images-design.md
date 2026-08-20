# Cross-Camera Debug Images — Design

## Context

Intra-camera live sessions already save two kinds of debug images -
outlier-triggered (`engine/session_engine.py`'s `_maybe_save_position_gap_outlier`,
fires whenever a pair's Optical Sync value crosses `position_gap_outlier_threshold_ms`)
and periodic (`gui/widgets/camera_live_session_panel.py`'s
`_maybe_save_periodic_snapshot`, fires every `snapshot_every_n_pairs` pairs,
capped at `max_snapshots`) - both drawing on/off LED-state circles
(`draw_led_state_overlay`) onto that camera's own two streams and combining
them side by side (`combine_side_by_side`).

The multi-camera page's "Cross-Camera Sync" tab has no equivalent at all -
it shows only plots and stats, never a frame. Worse, the frames shown on
each camera's own per-camera tab (`camera_frame_ready`) are *not* the
frames the cross-camera reconciler actually matched: `frame_ready` only
fires for every `display_stride`-th pair (default every 10th), while
`CrossCameraReconciler` matches on every pair via `row_ready`. Since each
camera's own `pair_index` counter is independent, and a cross-camera match
can land on any pair index, the throttled subset almost never contains the
specific pair a given cross-camera match actually resolved to. No image is
cached anywhere by pair index today, on either the intra-camera or
cross-camera path - only the single most-recently-received frame per
camera is kept (`CameraLiveSessionPanel`'s `_last_stream_a_image`/
`_last_stream_b_image`, overwritten every call).

This spec adds outlier-triggered and periodic debug images for the
cross-camera reconciler's own matches, showing the genuinely correct
matched frames (not an approximation), with the pair index, both raw HW
timestamps, both global timestamps, and all three cross-camera metrics
(HW TS Latency, Global TS Latency, Optical Sync) burned into the image.
Live video display on the Cross-Camera Sync tab is explicitly out of
scope for this spec (see "What doesn't change").

## 1. Per-camera unthrottled frame ring buffer

`engine/session_engine.py`'s `SessionEngineThread` gains a small,
bounded, thread-safe ring buffer of recent frame pairs, populated from
the *existing* `on_frame_pair` callback (already unthrottled - it already
fires on every single pair, today used only for the intra-camera outlier
image) rather than the throttled `frame_ready`/`on_frames` path:

```python
self._recent_frames = collections.deque(maxlen=self._recent_frames_maxlen)
self._recent_frames_lock = threading.Lock()
```

`on_frame_pair` appends `(pair_index, stream_a_image, stream_b_image)` to
this deque under the lock, on the background capture thread, on every
pair - the same thread and cadence that already writes intra-camera
outlier images, so this adds no new threading model, just one more
cheap append alongside the existing outlier-image check.

`_recent_frames_maxlen` is sized off this camera's own configured fps and
a buffer-seconds constant that matches `CrossCameraReconciler`'s own
`_PendingBuffer` sizing (`fps_hint * buffer_seconds`, default `30 * 1.0`)
- long enough that whenever the reconciler still has a row buffered
waiting for its counterpart, the corresponding image is still in this
ring buffer too. Exceeding that window is fine (older entries just age
out); falling short would mean a genuine match sometimes can't find its
image - matching the reconciler's own tolerance keeps the two buffers
"as deep as each other" by construction rather than by two independently
guessed constants.

A new public method does the locked lookup:

```python
def get_recent_frame_pair(self, pair_index):
    """(stream_a_image, stream_b_image) for the given pair_index if still
    in the ring buffer, else None - called from the GUI thread once
    CrossCameraReconciler resolves a match, to look up the actual frames
    that produced it (not an approximation from the throttled display
    path). Thread-safe: this camera's own background thread keeps
    appending to the same deque concurrently."""
```

## 2. Where the outlier/periodic decision and the actual save happen

`gui/pages/multi_camera_live_session_page.py`'s existing
`_on_cross_pair_ready(cross_row)` - already unthrottled, already the
cross-camera counterpart of `on_frame_pair` - gains the check-and-maybe-
save logic, mirroring the intra-camera design's own shape exactly: a
cheap condition checked on every call, with the expensive work (image
lookup, drawing, PNG encode, disk write) only actually running on genuine
trigger events (rare outliers, or every Nth periodic pair). This is the
same pattern `_maybe_save_position_gap_outlier` already uses on its own
unthrottled callback - not a new risk to the documented row_ready/
stats_ready cadence discipline, which is specifically about never calling
GUI-widget updates (`add_point`, etc.) on every unthrottled call; a
conditionally-rare disk write is a different, already-accepted shape.

**Outlier trigger**: Optical Sync (`position_gap_ms`) only - exactly
mirroring the intra-camera design, not HW TS Latency or Global TS
Latency. Reuses the *master* camera's own already-configured
`position_gap_outlier_threshold_ms`/`position_gap_outlier_max_snapshots`
(same "master's config is authoritative" precedent this feature already
uses for `num_leds`/`switch_time_ms`) - no new settings.yaml keys.

**Periodic trigger**: every Nth cross-camera pair, using the reconciler's
own synthetic `pair_index` (the same counter `cross_camera_sync_plot_*`
already uses as its x-axis - unambiguous across every spec, unlike either
camera's own independent per-camera pair_index). Reuses the master's own
`snapshot_every_n_pairs`/`max_snapshots`. Tracked **per (slave, identity)
spec independently** - the same per-spec independence every other
counter in this feature already has (running stats, series, etc.) -
rather than one shared counter across every configured slave/identity.

**Image lookup**: each cross-row already carries `master_pair_index`/
`slave_pair_index` and `master_camera_id`/`slave_camera_id`. The page
reaches each camera's own thread via `MultiCameraSessionController`'s
existing public `threads` property (`self._controller.threads[camera_id]`)
and calls `get_recent_frame_pair(pair_index)` on each side. If either
side's image has already aged out of its ring buffer (a genuinely late
match, or a run just starting), the save is skipped entirely for that
pair - no partial/misleading image, same "explicit exclusion over a
misleading result" convention this project uses everywhere else (frame
drops, outlier thresholds, warmup exclusion).

**Output location and naming**: written into the shared run folder
(`self._run_dir`, the same level `cross_camera_sync.csv` and
`cross_camera_sync_plot_{slave-slug}.png` already live at) - not a
per-camera subfolder, since this is a cross-camera artifact:
- `cross_camera_optical_sync_outlier_{slave-slug}_{identity}_pair{index:05d}.png`
- `cross_camera_periodic_{slave-slug}_{identity}_pair{index:05d}.png`

## 3. New overlay: `domain/realsense_utils.py`'s `draw_cross_camera_debug_overlay`

A new function, styled exactly like the existing `draw_bundle_overlay`
(same `cv2.putText` convention - stacked lines, `cv2.FONT_HERSHEY_SIMPLEX`,
green/yellow/cyan text, starting at `(10, 25)`, `y += 25` per line), drawn
once onto the *master's* image before `combine_side_by_side` combines it
with the slave's (unmodified) image - mirroring where `draw_bundle_overlay`
already places its own text (on `image_a` only, in
`engine/stream_preview_thread.py`), not duplicated onto both sides.

Per your "everything available" choice, every field this feature computes
gets its own line:
- Cross pair index (`cross_row["pair_index"]`)
- Master pair index / Slave pair index (`master_pair_index`/`slave_pair_index`)
- Master HW ts / Slave HW ts (`master_ts_us`/`slave_ts_us`)
- Master global ts / Slave global ts (read directly off the master/slave
  rows the same way `_build_cross_row` already does, since `_build_cross_row`'s
  own returned dict doesn't currently carry the raw global ts values -
  see "Critical files" below for the small addition needed)
- HW TS Latency (`pairing_gap_us`), Global TS Latency (`global_ts_gap_us`),
  Optical Sync (`position_gap_ms`) - whichever of the three triggered the
  outlier and the other two, all three always shown regardless of which
  one fired.

## 4. What doesn't change

- **No live video panels on the Cross-Camera Sync tab** - explicitly
  descoped per your decision. The tab continues to show only plots and
  stats; this spec only adds saved debug images, the same way the
  existing intra-camera outlier/periodic mechanisms are saved-file-only
  features with no corresponding live video change of their own.
- **`CrossCameraReconciler` stays pure Python, no Qt/hardware/images** -
  the ring buffer, the image lookup, and the actual file writing all live
  in the Qt-aware layers (`SessionEngineThread`, `MultiCameraLiveSessionPage`),
  exactly matching this module's own documented boundary ("Deliberately
  does NOT touch engine.session_engine/engine.test_session/
  engine.acquisition_loop").
- **No new settings.yaml keys** - both triggers reuse the master camera's
  own already-configured values.
- **`draw_led_state_overlay`/`combine_side_by_side`** - reused unmodified
  for the "combine two images side by side" half of this; only the new
  `draw_cross_camera_debug_overlay` text-burning function is new.
- **Intra-camera outlier/periodic images** - completely untouched;
  this is a wholly separate, additive mechanism.

## Critical files

- `engine/session_engine.py` - `SessionEngineThread`: new ring buffer,
  `get_recent_frame_pair`, `on_frame_pair` callback extended to also
  append to it.
- `domain/realsense_utils.py` - new `draw_cross_camera_debug_overlay`
  function.
- `engine/cross_camera_reconciler.py` - `_build_cross_row`'s returned
  dict needs two more raw fields (`master_global_ts_us`/`slave_global_ts_us`)
  so the overlay can show them without re-deriving them from the raw rows
  a second time at the GUI layer - both values are already computed
  locally inside `_build_cross_row`, just not currently returned.
- `gui/pages/multi_camera_live_session_page.py` - `_on_cross_pair_ready`
  gains the outlier/periodic check-and-save logic; new per-spec periodic
  counters alongside the existing `_cross_running_stats`-style per-spec
  state; `_reset_cross_run_state` clears the new counters and any
  previous run's debug-image files from `self._run_dir` (mirroring
  `_clear_periodic_snapshots`'s own glob-and-delete pattern).
- No changes: `engine/multi_camera_session.py` (its existing public
  `threads` property already provides everything needed - see Section 2),
  `domain/plot_export.py`, `domain/csv_export.py`, intra-camera debug
  image code.

## Testing

- `engine/session_engine.py`: the ring buffer holds the last N frame
  pairs by pair_index and evicts the oldest past that; `get_recent_frame_pair`
  returns the right pair for an index still in the buffer, `None` for one
  that's aged out; thread-safety is structural (a lock), not itself
  something a single-threaded test can prove race-free - matches this
  file's own existing "no automated tests by design" convention for its
  genuinely hardware/thread-bound internals, but the ring buffer's own
  pure append/evict/lookup logic (given a fake `on_frame_pair` call
  sequence) is plain Python and should be unit-tested directly.
- `domain/realsense_utils.py`: `draw_cross_camera_debug_overlay` draws
  the expected number of text lines with the expected values, mirroring
  `draw_bundle_overlay`'s own existing test coverage style if one exists,
  or establishing the same pattern if not.
- `engine/cross_camera_reconciler.py`: `_build_cross_row`'s returned dict
  now also carries `master_global_ts_us`/`slave_global_ts_us` with the
  correct raw values (unadjusted, same "transparency" convention as the
  existing raw `master_ts_us`/`slave_ts_us` fields).
- `gui/pages/multi_camera_live_session_page.py`: an outlier-triggering
  cross-row saves the expected file with the expected name; a periodic
  Nth cross-row saves the expected file, independently per spec; a match
  whose image has aged out of either camera's ring buffer is skipped, no
  file written, no exception; per-spec periodic/outlier caps are
  respected; `_reset_cross_run_state` clears stale files from a previous
  run in the same page visit.
