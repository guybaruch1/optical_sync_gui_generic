# Cross-Camera Matching on Global Timestamps + Global TS Latency — Design

## Context

`CrossCameraReconciler` currently matches master/slave frame pairs using
each device's raw HW timestamp (`frame_metadata_value.frame_timestamp`),
which - as this project's own genlock investigation found - carries an
arbitrary, per-device constant epoch offset (each device's counter resets
near zero at its own `pipeline.start()` call). The reconciler learns that
constant once, from whichever correspondence its first unbounded search
finds, and applies it for the rest of the run.

Real-hardware testing (this session) found that assumption doesn't fully
hold: over a 50-second run, the reported "HW TS Latency" between a
genlocked master and slave drifted by roughly 40µs - small in absolute
terms (about 0.8µs of drift per second), but real, steady, and linear, not
noise. At that rate a run would need many hours to actually break the
matching window (`max_match_gap_us`, default 50ms), so this isn't a
matching-failure risk in practice - but it IS a reporting-accuracy problem:
every microsecond of that drift is baked into the "HW TS Latency" number as
if it were genuine physical latency between the two cameras, when it's
really just residual clock-rate mismatch the one-time offset never
accounts for after the moment it was learned.

RealSense exposes a second per-frame timestamp - `frame.get_timestamp()`,
reported in whichever domain `frame.get_frame_timestamp_domain()` says
(`rs.timestamp_domain.system_time` / `hardware_clock` / `global_time`).
When a sensor's `global_time_enabled` option is on and frames genuinely
report the `global_time` domain, that value is periodically re-corrected
against the HOST's own clock rather than free-running off each device's
own local counter - in principle giving two different devices' frames a
directly-comparable timestamp with no per-device epoch to bridge AND no
accumulating crystal-rate drift, since it's periodically re-anchored
rather than calibrated once and left alone.

This spec adds global-timestamp-based matching to `CrossCameraReconciler`
as the join mechanism, and adds a new "Global TS Latency" metric
(`global_ts_gap_us`) alongside the existing "HW TS Latency"
(`pairing_gap_us`) so the two can be directly compared, per shared stream
identity, live during a run - exactly the drift-vs-no-drift comparison
this investigation is asking for. This is entirely a multi-camera,
cross-camera concept; the single-camera path is untouched.

## 1. Capturing global timestamps - opt-in, single-camera untouched

`engine/streams.py`'s `ContinuousCapture.__init__` gains a new parameter
`capture_global_ts=False` (default off - every existing call site's
behavior stays byte-identical unless it opts in).

`frames_with_diagnostics()` always yields an 8-tuple now (previously a
6-tuple: `image_a, image_b, ts_a, ts_b, num_a, num_b`); the two new
trailing values are `None, None` whenever `capture_global_ts` is off, so
the tuple's SHAPE never varies at runtime - only whether the last two
entries are real numbers or `None`. When `capture_global_ts` is on, for
each of `frame_a`/`frame_b`:

```python
GLOBAL_TIME_DOMAIN = rs.timestamp_domain.global_time

if frame_a.get_frame_timestamp_domain() != GLOBAL_TIME_DOMAIN or \
   frame_b.get_frame_timestamp_domain() != GLOBAL_TIME_DOMAIN:
    raise RuntimeError(
        "This camera is not reporting frames in the RealSense GLOBAL_TIME "
        "timestamp domain (global_time_enabled may be disabled or "
        "unsupported on this device/driver), which the cross-camera "
        "Global TS Latency metric requires. Reconnect the camera or "
        "disable this feature and retry."
    )
global_ts_a = frame_a.get_timestamp() * 1000.0  # ms -> us, matching this project's _ts_us convention
global_ts_b = frame_b.get_timestamp() * 1000.0
```

Fail loudly, not gracefully - same "a real error reaching the operator
beats a silent, wrong fallback" convention this project already applies
to the existing HW-timestamp-metadata check right above this one, and to
`ContinuousCapture.start()`'s own no-`can_resolve()`-fallback design (see
CLAUDE.md). A silently-wrong-domain global timestamp would produce a
"Global TS Latency" number that looks plausible but means nothing - worse
than an obvious failure.

`frames()` (the plain 4-tuple wrapper other call sites like
`gui/pages/calibration_page.py`/`gui/pages/roi_select_page.py` use)
discards the two new trailing values exactly like it already discards the
existing frame-number pair - no change to its own public shape.

**Plumbing through to the row, unconditionally present:**
- `engine/metrics.py`'s `FramePairSample` gains two new optional fields:
  `stream_a_global_ts_us: "float | None" = None`,
  `stream_b_global_ts_us: "float | None" = None`.
- `engine/session_engine.py`'s `_frame_pairs_with_brightness()` switches
  from iterating `self._capture.frames()` to iterating
  `self._capture.frames_with_diagnostics()` directly, so it can pass the
  two new values through into its own yielded tuple (discarding the frame
  numbers exactly as before).
- `engine/acquisition_loop.py`'s `AcquisitionLoop.run_until_stopped()`
  unpacks the now-8-tuple from `frame_source` and passes the two new
  values into `FramePairSample`'s two new fields.
- `engine/test_session.py`'s `TestSession.process_pair()` copies them
  straight into the row dict as `stream_a_global_ts_us`/
  `stream_b_global_ts_us` - always present, `None` for any run that never
  captured them (every existing single-camera row, harmlessly ignored by
  everything that doesn't look for them).

## 2. Wiring: only multi-camera runs turn this on

`engine/session_engine.py`'s `SessionEngineThread` gains a
`capture_global_ts=False` constructor parameter, passed straight through
to its own `ContinuousCapture(..., capture_global_ts=self.capture_global_ts)`
call in `run()`.

`gui/pages/multi_camera_live_session_page.py`'s `start_all_sessions()` is
the ONLY call site that sets `capture_global_ts=True`, added into the
`thread_kwargs` dict already built there for every configured camera (both
master and every slave - the reconciler needs it from both sides of every
matched pair). `gui/pages/live_session_page.py`'s `start_session()` never
sets it, leaving the single-camera path's default `False` - global
timestamps are a cross-camera-only concept; a single camera's own two
streams already share one clock via one frameset; requiring
`global_time_enabled` support unconditionally would risk breaking existing
single-camera runs on hardware/drivers that don't support it, for a
feature that camera has no use for.

## 3. `CrossCameraReconciler`: matching moves to global TS; HW TS Latency's meaning is preserved

**The join (matching) key switches from `{ts_role}_ts_us` (raw HW ts) to
`{ts_role}_global_ts_us`, and the whole uncalibrated/calibrated branch
split in `_ingest_side` collapses into one path** - global timestamps from
two genlocked, global-time-enabled devices are expected to already be
directly comparable, with no per-device epoch to learn before a first
match is safe to accept. `_ingest_side` drops its `offset_us`-computation
entirely and no longer needs to pass one to `build` - but `index` (already
threaded through from `ingest_row`'s own per-spec loop, used today to pick
`self._master_buffers[index]`/`self._slave_buffers[index]`) now also needs
to reach `_build_cross_row`, since that's where the still-needed HW-offset
bookkeeping moves to:

```python
def _ingest_side(self, row, ts_role, own_buffer, other_buffer, index, is_master_side, build):
    ts_us = row.get(f"{ts_role}_global_ts_us")
    if ts_us is None:
        return None
    match = other_buffer.pop_nearest(ts_us, self._max_match_gap_us)
    if match is None:
        own_buffer.push(ts_us, row)
        return None
    _, matched_row = match
    return build(matched_row)
```

`ingest_row`'s two `build=lambda ...` closures (currently
`lambda match, offset_us: self._build_cross_row(spec, row, match, offset_us)`
and its mirror) drop the now-unused `offset_us` parameter and close over
`index` instead, e.g. `build=lambda match: self._build_cross_row(index, spec, row, match)`
for the master-side branch (mirror for the slave-side branch, same
`(master_row, slave_row)` argument-order swap as today).

`_max_match_gap_us` keeps its existing meaning and default (50ms) - it's
now the ONLY window this class uses, applied to global-ts space instead of
HW-ts space.

**"HW TS Latency" keeps its exact current meaning, computed as a separate
reporting step on whatever pair the (now simpler) join found - it does
NOT switch to using global ts for its own value.** The one-time-learned
HW-ts offset doesn't disappear, it just moves from being the matcher's own
concern to being a small step applied after a match already exists, keyed
by the same `index` the matcher already used to pick this spec's buffers -
`self._hw_offset_us` replaces today's `self._offset_us` as a same-shaped
`[None] * len(pair_specs)` list, initialized in `__init__` exactly like it
is today:

```python
def _build_cross_row(self, index, spec, master_row, slave_row):
    self._pair_counter += 1
    master_hw_ts = master_row[f"{spec.master_row_role}_ts_us"]
    slave_hw_ts = slave_row[f"{spec.slave_row_role}_ts_us"]
    master_global_ts = master_row[f"{spec.master_row_role}_global_ts_us"]
    slave_global_ts = slave_row[f"{spec.slave_row_role}_global_ts_us"]
    master_frame_drop = master_row.get(f"{spec.master_row_role}_frame_drop", False)
    slave_frame_drop = slave_row.get(f"{spec.slave_row_role}_frame_drop", False)

    hw_offset_us = self._hw_offset_us[index]
    if hw_offset_us is None:
        hw_offset_us = slave_hw_ts - master_hw_ts
        self._hw_offset_us[index] = hw_offset_us

    hw_sample = FramePairSample(
        pair_index=self._pair_counter,
        stream_a_ts_us=master_hw_ts,
        stream_b_ts_us=slave_hw_ts - hw_offset_us,
        stream_a_frame_drop=master_frame_drop, stream_b_frame_drop=slave_frame_drop,
    )
    hw_result = spec.pairing_gap_metric.update(hw_sample)

    global_sample = FramePairSample(
        pair_index=self._pair_counter,
        stream_a_ts_us=master_global_ts,
        stream_b_ts_us=slave_global_ts,   # no offset correction - directly comparable
        stream_a_frame_drop=master_frame_drop, stream_b_frame_drop=slave_frame_drop,
    )
    global_result = spec.global_ts_gap_metric.update(global_sample)
    ...
```

The first pair matched for a given spec still reports `pairing_gap_us ==
0.0` by construction (exactly as today - it's what defines
`hw_offset_us`); every pair after that reports the genuine HW-clock
residual. **"Global TS Latency" (`global_ts_gap_us`) gets no such
offset at all** - it's `master_global_ts - slave_global_ts` on the exact
same matched pair, computed by a second, independent `PairingGapMetric`
instance (`spec.global_ts_gap_metric`, same `outlier_threshold_us` as the
existing one, built alongside it in `build_cross_camera_pair_specs`). If
global time behaves as expected, this number should sit near zero and
stay flat for the whole run - directly comparable, pair-for-pair, against
"HW TS Latency" for the same physical frames, which is the validation this
investigation needs.

`_build_cross_row`'s returned dict gains the new metric's three fields
(`global_ts_gap_us`, `global_ts_gap_us_excluded`,
`global_ts_gap_us_exclude_reason`) alongside the existing
`pairing_gap_us`/`_excluded`/`_exclude_reason` - both computed from ONE
matching pass, never two.

`CrossCameraPairSpec` gains one new field: `global_ts_gap_metric: object`
(a second `PairingGapMetric` instance), constructed the same way
`pairing_gap_metric` already is.

## 4. Live GUI: a third plot + stats fields per slave section

Each slave section (`_build_slave_section` in
`gui/pages/multi_camera_live_session_page.py`) gains a third `LivePlot`,
"Global TS Latency (us)", built and wired exactly like the existing
`pairing_plot` - one series per shared stream identity, same color
assignment, same `add_point`/exclusion handling. The combined stats panel
gains `{identity}_global_ts_gap_us` fields (label: "{identity} Global TS
Latency (us)") with their own `RunningStats` entries
(`self._cross_running_stats[(slave_camera_id, identity,
"global_ts_gap_us")]`), refreshed at the exact same throttled
`cross_stats_ready` cadence the existing two metrics already use.
`_reset_cross_run_state()` clears this new plot's data the same way it
already clears the other two.

## 5. Static plot export: one more line, same axis as HW TS Latency

`domain/plot_export.py`'s `_build_cross_camera_figure` plots
`global_ts_gap_us` as a second line on the SAME microsecond y-axis
`pairing_gap_us` already uses (not a fourth separate subplot) - both are
latency-shaped, µs-scale numbers, and the whole point is a direct visual
comparison between them on one chart, per stream identity.

## 6. CSV export: no code changes

`domain/csv_export.py`'s `export_cross_camera_csv`/`export_session_csvs`
already derive their column list dynamically from whatever keys exist
across the rows actually passed in - the three new
`global_ts_gap_us`/`_excluded`/`_exclude_reason` columns appear
automatically once the reconciler starts producing them; nothing in this
file needs to change.

## What doesn't change

- Single-camera `LiveSessionPage`/`ContinuousCapture` default behavior -
  `capture_global_ts` stays `False`, no new hardware requirement imposed,
  no plumbing change visible to that path beyond the two new always-`None`
  row keys.
- `PositionGapMetric`/"Optical Sync" (intra-camera or cross-camera) -
  completely untouched by this spec.
- Existing `pairing_gap_us` ("HW TS Latency") semantics, data key, CSV
  column, and its one-time offset-calibration reasoning - unchanged; still
  computed and reported exactly as today, just via a join that no longer
  depends on it being correct.
- `engine/multi_camera_session.py`'s `MultiCameraSessionController` -
  no changes; it already passes `thread_kwargs` straight through to
  `SessionEngineThread` without inspecting its contents.

## Critical files

- `engine/streams.py` - `ContinuousCapture.__init__`/
  `frames_with_diagnostics`/`frames` (Section 1).
- `engine/metrics.py` - `FramePairSample`'s two new fields (Section 1).
- `engine/session_engine.py` - `SessionEngineThread.__init__`/`run`/
  `_frame_pairs_with_brightness` (Sections 1-2).
- `engine/acquisition_loop.py` - `AcquisitionLoop.run_until_stopped`'s
  tuple unpacking (Section 1).
- `engine/test_session.py` - `TestSession.process_pair`'s row dict
  (Section 1).
- `engine/cross_camera_reconciler.py` - `CrossCameraPairSpec`,
  `build_cross_camera_pair_specs`, `CrossCameraReconciler._ingest_side`/
  `_build_cross_row` (Section 3, the core of this spec).
- `gui/pages/multi_camera_live_session_page.py` - `start_all_sessions`'s
  `thread_kwargs` (Section 2), `_build_slave_section`/
  `_reset_cross_run_state`/`_on_cross_stats_ready`-equivalent handlers
  (Section 4).
- `domain/plot_export.py` - `_build_cross_camera_figure` (Section 5).
- No changes: `domain/csv_export.py` (Section 6), `gui/pages/live_session_page.py`
  beyond confirming it never sets `capture_global_ts`,
  `engine/multi_camera_session.py`.

## Testing

- `tests/engine/test_streams.py`: `capture_global_ts=False` (default)
  leaves `frames_with_diagnostics()`'s behavior/tuple values identical to
  today for the two new trailing entries (`None, None`); `True` reads and
  converts `get_timestamp()` correctly for both frames; a frame reporting
  a non-`global_time` domain raises the documented `RuntimeError` when
  `capture_global_ts=True`, and is never checked at all when `False`.
- `tests/engine/test_acquisition_loop.py`/`test_session.py`: a frame
  source yielding real global-ts values threads them into
  `FramePairSample` and the row dict unchanged; a frame source yielding
  `None, None` (today's shape) still works exactly as before.
- `tests/engine/test_cross_camera_reconciler.py`: rewrite matching tests
  to key off `{role}_global_ts_us` instead of `{role}_ts_us`; confirm
  matching no longer needs an unbounded first search (a huge raw HW-ts gap
  between rows with CLOSE global-ts values still matches immediately, no
  calibration pair needed); confirm `pairing_gap_us`'s own one-time
  HW-offset calibration and reporting semantics are unchanged (first
  match still `0.0`, later matches show the genuine HW residual); add
  tests establishing `global_ts_gap_us` is the direct, unadjusted
  `master_global_ts - slave_global_ts` for the same matched pair, with its
  own independent frame-drop/outlier exclusion; confirm two specs sharing
  a master still calibrate/report their own `hw_offset_us` and
  `global_ts_gap_us` independently.
- `tests/gui/pages/test_multi_camera_live_session_page.py`: the new
  "Global TS Latency" plot/series/stats fields exist per slave section and
  per shared identity; `thread_kwargs` passed to the thread factory
  includes `capture_global_ts=True` for every camera; `_reset_cross_run_state`
  clears the new plot's data too.
- `tests/domain/test_plot_export.py`: the exported cross-camera figure
  includes a `global_ts_gap_us` line on the same axis as `pairing_gap_us`.
