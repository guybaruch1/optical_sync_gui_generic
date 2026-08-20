# Cross-Camera Optical Sync — Design

## Context

The multi-camera sync feature currently shows only ONE cross-camera
metric: HW TS Latency (`engine/cross_camera_reconciler.py`'s
`build_cross_camera_pair_specs`/`CrossCameraReconciler`, reusing
`PairingGapMetric` unmodified, one comparison per (slave, shared stream
identity) pair against the single designated master). The original
multi-camera design plan explicitly deferred cross-camera **Optical Sync**
(`PositionGapMetric`) to v2, flagging two open prerequisites: (1)
`row_ready` doesn't carry per-LED brightness/detection data across camera
boundaries today, and (2) it only means anything if master and slave are
viewing the same physical LED panel.

The operator has now confirmed: in the normal (single-panel) case, all
cameras in a run view one shared LED panel — this is what makes the
comparison physically meaningful, and it's the operator's actual, primary
test. Optical Sync in dual-panel mode uses the identical comparison
algorithm — only the per-stream ROI/threshold differ, which is already
true today regardless of panel topology, so no special-casing is needed.

This spec adds cross-camera Optical Sync as a second cross-camera metric,
alongside a UI restructuring (both metrics move into their own tab) and a
fix to a real, pre-existing efficiency problem in the cross-camera
plotting code, generalizing cleanly to up to 3 cameras (1 master + 2
slaves) via the architecture already in place.

## 1. Data flow: threading LED detection through row_ready

Today, `engine/metrics.py`'s `PositionGapMetric.update()` computes which
LED is "on" per stream (`find_last_on_led`) but only ever exposes the
result as instance attributes (`last_stream_a_on_mask`/
`last_stream_b_on_mask`) — a live, mutable side-channel the GUI reads
directly for debug snapshots. That side-channel is **unsafe to reuse for
cross-camera matching**: the cross-camera reconciler buffers unmatched
rows for up to `buffer_seconds` (~1s, ~30 frames) before a match is found,
so by match time the *live* mask would already reflect several frames
later than the one actually being matched — silently wrong data.

The fix: `PositionGapMetric.update()` additionally returns the *detected
LED index* (a plain int, not the full array) via `MetricResult.extra` —
e.g. `{"stream_a_last_led": 7, "stream_b_last_led": 7}` (or `None` if that
side had no data/no clear "on" LED). `extra` already exists specifically
to fold small scalars into the row/CSV, so `TestSession.process_pair()`
needs no changes — this rides the existing mechanism straight into
`row_ready`, immutably captured per-row (not a live reference), safe to
buffer and match later exactly like `stream_a_ts_us` already is.

**Reuse the same matched pair, don't re-match.** Rather than a second
independent buffering/matching pass keyed by LED index, the cross-camera
Optical Sync value is computed from the *same* (master_row, slave_row)
pair the HW-timestamp reconciler already finds (using the calibration fix
already in place). Once a match exists, `engine/cross_camera_reconciler.py`'s
`_build_cross_row` computes a second value from it —
`compute_position_gap(master_row[f"{master_row_role}_last_led"],
slave_row[f"{slave_row_role}_last_led"], num_leds) * switch_time_ms`
(reusing `engine/metrics.py`'s existing free functions unmodified) — no
new stateful metric class, no second match.

## 2. Exclusion rules (single algorithm, no panel-topology special-casing)

Mirrors the existing cross-camera HW TS Latency exclusion structure:

1. **Frame drop first** — either camera's own `stream_a_frame_drop`/
   `stream_b_frame_drop` (whichever row role applies), same priority
   `PairingGapMetric`'s `is_drop` check already uses.
2. **`no_led_data`/`miss`** — reuse each camera's own already-computed
   `position_gap_ms_excluded`/`position_gap_ms_exclude_reason` (from that
   camera's OWN intra-camera `PositionGapMetric`) for whichever side's row
   carries it. No new detection-failure logic invented — if a camera
   couldn't detect a valid on-LED for its own intra-camera test that
   frame, the cross-camera comparison is excluded too.
3. **No `num_leds` mismatch validation.** Master's own `num_leds` is
   authoritative for the circular wraparound math (same "master's config
   wins" reasoning already used for `switch_time_ms`) — the slave's own
   configured `num_leds` is never read or validated against it.

Applies identically regardless of whether either camera is single- or
dual-panel — confirmed by the operator that the comparison algorithm
itself never differs by panel topology, only each stream's own
already-independent ROI/threshold does (true today, unrelated to this
feature).

## 3. UI restructuring

**New tab, not an always-visible strip.** `gui/pages/multi_camera_live_session_page.py`'s
cross-camera section (`_cross_section_widget`, today rendered below
`self.tabs`) moves *into* `self.tabs` as its own tab — placed **first**
(before the per-camera tabs), since cross-camera Optical Sync is the
operator's primary test. Inside that tab: two stacked graph+stats rows —
"Cross-Camera HW TS Latency" (unchanged) and "Cross-Camera Optical Sync"
(new) — literally duplicating the existing `LivePlot` + `StatsPanel` row
construction, once per metric, each with its own per-(slave, identity)
series exactly like the existing HW TS Latency plot already has.

**Efficiency fix, folded in.** `_on_cross_pair_ready` currently calls
`self.cross_plot.add_point(...)` on *every* unthrottled `cross_pair_ready`
emission — the same anti-pattern CLAUDE.md documents as having caused a
real GUI freeze for intra-camera plotting (`row_ready` must stay O(1);
actual plotting belongs on the throttled `stats_ready` cadence). Adding a
second plot without fixing this would double an already-real risk.
Fix: `_on_cross_pair_ready` becomes O(1) bookkeeping only (record the
latest row per pair-key, append to `self._cross_rows` for CSV/plot
export); the actual `add_point` calls for *both* graphs move into the
already-throttled `_on_cross_stats_ready`, which already has the "latest
matched row per pair" data (`latest_by_pair`) needed to plot from.

Both metrics ride the same `cross_pair_ready`/`cross_stats_ready` signals
— no new signal. `_build_cross_row`'s returned dict gains
`position_gap_ms`/`position_gap_ms_excluded`/`position_gap_ms_exclude_reason`
(bare names matching the intra-camera key convention, same precedent as
`pairing_gap_us` being reused bare for the cross-camera HW TS value)
alongside the existing HW TS Latency fields.

## 4. 3-camera generalization

No new design work needed — the existing architecture already generalizes
to N slaves by construction: `build_cross_camera_pair_specs` already loops
over every non-master camera and builds one `CrossCameraPairSpec` per
(slave, shared identity) pair against the single master; computing Optical
Sync inside the same `_build_cross_row` means it's automatically produced
per-pair-spec for however many slaves exist (1 or 2), and the existing
per-pair-spec series-creation loop in `_rebuild_cross_camera_section` just
needs to add a series to both plots instead of one.

The one real plumbing addition: `CrossCameraPairSpec` needs master's own
`num_leds`/`switch_time_ms` available to it (currently only carries
camera IDs, stream identity, and row roles) — new fields on the dataclass,
populated from the master's own camera spec by `build_cross_camera_pair_specs`
(the duck-typed `camera_specs` contract — `CameraSessionSpec`, the GUI
page's `_IdentitySpec`, and test fakes — needs `num_leds`/`switch_time_ms`
attributes added alongside the existing `camera_id`/`is_master`/
`stream_identities`).

## Critical files

- `engine/metrics.py` — `PositionGapMetric.update()` gains the
  `stream_a_last_led`/`stream_b_last_led` `extra` fields.
- `engine/cross_camera_reconciler.py` — `CrossCameraPairSpec` gains
  `num_leds`/`switch_time_ms`; `build_cross_camera_pair_specs` populates
  them from master; `_build_cross_row` computes the second (Optical Sync)
  value from the same matched pair.
- `engine/multi_camera_session.py` — `CameraSessionSpec` (or its
  construction site) needs `num_leds`/`switch_time_ms` available to pass
  through to `build_cross_camera_pair_specs`.
- `gui/pages/multi_camera_live_session_page.py` — tab restructuring (new
  "Cross-Camera Sync" tab, first position, two graph+stats rows), the
  `_on_cross_pair_ready`/`_on_cross_stats_ready` throttling fix,
  `_IdentitySpec`/`_stream_identities` extended with `num_leds`/
  `switch_time_ms`.
- `domain/csv_export.py`/`domain/plot_export.py`'s
  `export_cross_camera_csv`/`export_cross_camera_plot` — likely need to
  also export the new `position_gap_ms` field alongside `pairing_gap_us`.

## Testing

- `engine/metrics.py`: new tests confirming `PositionGapMetric.update()`'s
  `extra` carries the detected LED index for both streams (present when
  detected, absent/`None` on `miss`/`no_led_data`).
- `engine/cross_camera_reconciler.py`: new tests for the Optical Sync
  computation on an already-matched pair — correct value, frame-drop
  exclusion, reused miss/no_led_data exclusion from the per-camera rows,
  master's `num_leds`/`switch_time_ms` used regardless of slave's own
  values, extended to 2-slave (3-camera) scenarios.
- `gui/pages/multi_camera_live_session_page.py`: new tab structure exists
  with two plots; `_on_cross_pair_ready` no longer calls `add_point`
  directly (regression guard for the efficiency fix); `_on_cross_stats_ready`
  now plots both metrics; existing HW TS Latency behavior/tests unaffected.
