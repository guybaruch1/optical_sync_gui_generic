# Cross-Camera Graphs Per-Slave Restructuring — Design

## Context

The multi-camera live session page's "Cross-Camera Sync" tab (added by the
cross-camera Optical Sync feature — see
`docs/superpowers/specs/2026-08-17-cross-camera-optical-sync-design.md`)
currently shows exactly two graphs total, regardless of how many cameras
are running: one for HW TS Latency, one for Optical Sync. Each graph draws
one line per (slave camera, shared stream identity) pair, flattened
together on the same axes. With 1 slave sharing 2 identities (the common
2-camera case), that's 2 lines per graph — fine. With 2 slaves each
sharing 2 identities (the 3-camera max case), that's 4 lines crammed onto
each graph, and there's no way to tell at a glance which line belongs to
which physical camera without reading the legend closely.

Separately: the per-camera tabs only tag the master with `[MASTER]` today;
slaves get no role indicator at all. And the cross-camera stats panels
show far less live information than the per-camera tabs' own stats
panels — no running pair-index counter, no min/avg/std/max stats, no LED
switch time.

This spec restructures the cross-camera graphs to group by slave camera
first (mirroring the per-camera tab's own graph+stats layout, once per
slave), adds serial-number + master/slave-role labeling throughout, and
brings the cross-camera stats panels to full parity with what the
per-camera tabs already show live.

**Explicitly deferred to a separate future spec:** saving real debug
images when the cross-camera Optical Sync value exceeds the outlier
threshold (mirroring the existing per-camera
`_maybe_save_position_gap_outlier` feature). Tracing the actual threading
model surfaced a genuine architectural fork (cross-camera outlier
detection runs on the GUI thread via Qt's queued signal delivery, while
the frame images and the safe place to write them live on each camera's
own background capture thread) that deserves its own design pass rather
than being folded into this one.

**No engine-layer or run-architecture changes at all.** Two things are
already true today, confirmed by tracing the existing code before writing
this spec: (1) `MultiCameraSessionController.start_all()` already starts
every configured camera's thread together — master and every slave run
*concurrently* on independent `SessionEngineThread`s (staggered only by a
couple seconds at startup to avoid a USB enumeration collision), so one
"Start All" click already computes master-vs-slave1 and master-vs-slave2
in parallel from a single run, not sequential per-slave runs; (2)
`CrossCameraPairSpec`/`build_cross_camera_pair_specs`/
`CrossCameraReconciler` already carry `slave_camera_id` and
`stream_identity` on every cross-row — exactly the grouping key this
restructuring needs. Everything in this spec is a GUI (and static-export)
presentation change over already-available data.

## 1. Role & label computation

A single new helper in `gui/pages/multi_camera_live_session_page.py`
computes "Slave 1"/"Slave 2" role numbering once, reused everywhere a
role/label/serial needs displaying (per-camera tabs, cross-camera section
headers, static-export titles/filenames) — so the numbering is never
computed two different ways. Numbering is assigned in the order cameras
appear in the `cameras` list (excluding master), the same order the
per-camera tabs already iterate in today. For each camera it produces:
a role tag (`"MASTER"`, `"SLAVE 1"`, `"SLAVE 2"`), a filename-safe slug
(`"master"`, `"slave1"`, `"slave2"`), and a display string combining the
camera's own label and its `device_serial` (already available via
`config["device_serial"]`, no new plumbing needed to obtain it) —
`"D455 B (SN 231622071234)"`.

## 2. Per-camera tab labeling

Every per-camera tab gains the role tag it's currently missing. Today:
`tab_label = camera["label"] + (" [MASTER]" if camera["is_master"] else "")`
— only the master is tagged. New: every camera gets its role tag from the
Section 1 helper — `"D455 A [MASTER]"`, `"D455 B [SLAVE 1]"`,
`"D455 C [SLAVE 2]"`. Serial number is not added to the tab label itself
(width-constrained, and label+role is enough to identify a tab at a
glance) — the serial appears in the cross-camera section headers (Section
3), where an operator is actually cross-referencing which physical unit
is which.

## 3. Cross-camera section: one graph pair + one stats panel per slave

`_rebuild_cross_camera_section` builds one section per slave, each
containing a header line, two stacked graphs (HW TS Latency, Optical
Sync), and **one combined stats panel** next to them — mirroring
`CameraLiveSessionPanel`'s own `graphs_column` + single `stats_panel`
layout exactly (two graphs, one shared panel), not the two-separate-panels
shape from the original cross-camera feature.

- **0 slaves:** unchanged — the existing "Add a second camera..."
  placeholder.
- **1 slave:** shown directly under the "Cross-Camera Sync" tab — header
  line (`"Slave 1: D455 B (SN 231622071234)  vs.  Master: D455 A (SN
  987654321098)"`) followed by the two graphs and the one stats panel. No
  inner tab widget — a single always-selected tab would be redundant
  chrome for the common 2-camera case.
- **2 slaves:** an inner `QTabWidget` appears, one tab per slave (short
  label: `"Slave 1: D455 B"`), each tab containing the same header line +
  two graphs + one stats panel.

Within a slave's own graphs, lines are named by stream identity alone
(`"infrared1"`, `"color"`) — no longer prefixed with the camera name,
since the slave is already established by the section/tab.
`CROSS_CAMERA_COLORS` now cycles per identity within one slave's graph,
not across every slave×identity combination globally.

Data structures: `self.cross_plot`/`self.cross_stats_panel` (HW-TS) and
`self.cross_position_plot`/`self.cross_position_stats_panel` (Optical) —
currently single shared instances — become one dict keyed by
`slave_camera_id`, each holding that slave's `pairing_plot`,
`position_plot`, and one shared `stats_panel`. `_cross_pair_series_keys`
stays keyed by `(slave_camera_id, stream_identity)`, but the series key
itself can now just be the stream-identity string, since each slave has
its own widget instances (no cross-slave collisions to prefix against).

No changes needed to `MultiCameraSessionController`/
`CrossCameraReconciler`: `cross_pair_ready` and `cross_stats_ready`
already carry `slave_camera_id` on every row — exactly the routing key
`_on_cross_pair_ready` (stays O(1) — unchanged shape, see Section 6 for
its one small addition) and `_on_cross_stats_ready` (now dispatches to the
correct slave's widgets instead of one shared pair) need.

## 4. Static plot export: one PNG per slave

`domain/plot_export.py`'s `export_cross_camera_plot`/
`_build_cross_camera_figure` change from "one call, one combined figure"
to "one call per slave, each producing its own 2-subplot figure" — the
looping and per-slave title/filename construction lives in the GUI page
(which already owns the Section 1 role-numbering logic), not in the
domain layer, keeping `plot_export.py` free of GUI/role concepts:

- `_build_cross_camera_figure(cross_rows, title)` gains a `title` param
  (`fig.suptitle(title)`); its internal grouping key simplifies from
  `(slave_camera_id, stream_identity)` to just `stream_identity`, since
  the caller now pre-filters `cross_rows` to one slave before calling.
  Subplot y-labels stay `"HW TS Latency (us)"`/`"Optical Sync (ms)"`
  (metric+units only); lines are named by identity alone, matching the
  GUI's simplification.
- `export_cross_camera_plot(cross_rows, path, title)` — same shape, one
  new param.
- `_on_all_sessions_finished` loops over each distinct `slave_camera_id`
  in `self._cross_rows`, filters that slave's rows, builds its title
  (same text as the live header) and filename
  (`cross_camera_sync_plot_slave1.png`, `..._slave2.png`) via the Section
  1 helper, and calls `export_cross_camera_plot` once per slave.

**No change** to `export_cross_camera_csv` — stays one combined
`cross_camera_sync.csv` with every slave's rows together (already fully
generic, already has `slave_camera_id`/`stream_identity` columns a
downstream analyst can filter/group by themselves).

## 5. Live-data parity with the per-camera tabs

Each slave's combined stats panel gains the same fields the per-camera
panel's "Live Data" + "Stats" sections already show, scoped to that
slave's own shared identities:

- **Pair Index** — one field per slave section (not per identity). A
  slave sharing multiple identities can have rows for each identity land
  in the same `_on_cross_stats_ready` tick with different `pair_index`
  values (each identity's own match completes independently); the field
  shows the **maximum** `pair_index` among that slave's rows in the
  current tick, i.e. the most recently completed match across all of that
  slave's identities — mirroring the per-camera panel's `frame_index`
  field as a visible heartbeat that the comparison is actively updating,
  not a per-identity value.
- **LED Switch Time (ms)** — one static field per slave section: the
  master's own `switch_time_ms` (the authoritative value
  `_compute_cross_position_gap`/`CrossCameraPairSpec.switch_time_ms`
  actually uses for every identity under that slave).
- **Per identity** (e.g. `infrared1`, `color`): an "HW TS Latency (us)"
  field and an "Optical Sync (ms)" field, each showing the latest raw
  value — exactly today's per-series `set_value` calls, now living in one
  shared panel instead of two.
- **Per identity, per metric — min/avg/std/max**: one
  `domain.running_stats.RunningStats` instance per `(slave_camera_id,
  stream_identity, metric)` triple, so an IR comparison and a color
  comparison under the same slave track separately. Accumulated with a
  plain `.update(value)` call inside `_on_cross_pair_ready` — still O(1),
  no new plotting cost, matching the existing efficiency constraint — then
  pushed to the panel only on the throttled `_on_cross_stats_ready` tick,
  identical cadence discipline to `CameraLiveSessionPanel._push_running_stats`.

Frame-drop counts are **not** duplicated into the cross-camera panels —
those already live on each camera's own per-camera tab.

## Critical files

- `gui/pages/multi_camera_live_session_page.py` — the Section 1 role
  helper; per-camera tab labels (Section 2); `_rebuild_cross_camera_section`
  rewritten for one graph-pair+stats-panel section per slave, inner tabs
  only at 2+ slaves (Section 3); `_on_cross_pair_ready`/
  `_on_cross_stats_ready` updated to route per-slave and accumulate
  `RunningStats` (Sections 3, 5); `_on_all_sessions_finished` loops
  per-slave for the static export (Section 4).
- `domain/plot_export.py` — `_build_cross_camera_figure`/
  `export_cross_camera_plot` gain a `title` param and simplify their
  internal grouping key (Section 4).
- No changes: `engine/cross_camera_reconciler.py`,
  `engine/multi_camera_session.py`, `domain/csv_export.py`.

## Testing

- New tests for the Section 1 role-labeling helper (numbering order,
  filename slugs, display strings).
- `tests/gui/pages/test_multi_camera_live_session_page.py`: per-camera tab
  labels include the new slave role tags; 1-slave case has no inner tab
  widget and shows the header+graphs+stats directly; 2-slave case builds
  an inner tab per slave with the right short labels; per-slave widget
  routing (a cross-row for slave 2 only updates slave 2's plots/stats,
  never slave 1's); `_on_cross_pair_ready` stays free of any plotting
  call; `RunningStats`-backed min/avg/std/max fields appear and update on
  the throttled cadence only.
- `tests/domain/test_plot_export.py`: `_build_cross_camera_figure`/
  `export_cross_camera_plot` with the new `title` param; grouping by
  identity alone within a single-slave row set; multiple calls (one per
  slave) produce distinct figures/files with the right per-slave content.
