# Live Session Output Validation Report

Generated 2026-08-10, updated same day with a real recorded run. This
started as a pure code/test-level audit — tracing every output a Live
Session run produces back to the exact code that produces it, cross-checked
against the existing automated test suite — and §9 below now also
cross-checks those conclusions against a real recorded session
(`live_session_2026-08-10_14-37-56`, 13,058 frame-pairs), supplied after the
first pass of this report. §10 records two concrete follow-up asks that
came out of inspecting that real data — **not implemented, by explicit
instruction; documented here only, to be fixed in a later pass.**

---

## 1. Pipeline overview — how one frame-pair becomes a row

```
ContinuousCapture.frames_with_diagnostics()   (engine/streams.py)
        |  yields: image_a, image_b, ts_a, ts_b, num_a, num_b
        v
SessionEngineThread._frame_pairs_with_brightness()   (engine/session_engine.py)
        |  adds: stream_a_bright, stream_b_bright (per-LED brightness arrays)
        v
AcquisitionLoop.run_until_stopped()   (engine/acquisition_loop.py)
        |  builds FramePairSample(pair_index, ts_a, ts_b, bright_a, bright_b)
        v
TestSession.process_pair(sample)   (engine/test_session.py)
        |  1. computes stream_a_frame_drop / stream_b_frame_drop ONCE
        |     (_is_frame_drop, own-stream-history only) and writes them
        |     onto BOTH the row and the shared `sample` object
        |  2. calls every registered Metric.update(sample):
        |       - PairingGapMetric  -> pairing_gap_us (+ _excluded/_exclude_reason)
        |       - PositionGapMetric -> position_gap_ms (+ _excluded/_exclude_reason)
        |  3. flattens everything into one dict `row`, buffers it
        v
   one `row` dict, emitted via three Qt signals:
     - row_ready   -> every single pair (drop counters, RunningStats)
     - stats_ready -> every `display_stride`-th pair (live plots, stat tiles)
     - session_finished -> once, at Stop, with ALL buffered rows
                             -> export_session_csvs() + export_session_plot()
```

Every row always carries the same shape: `pair_index`, `stream_a_ts_us`,
`stream_b_ts_us`, `stream_a_frame_drop`, `stream_b_frame_drop`, and per
metric: `<name>`, `<name>_excluded`, `<name>_exclude_reason`. Nothing
downstream (CSV, plot, stats) recomputes any metric — they all just read
this one row shape different ways.

---

## 2. The frame-drop check — what it actually does

**Code:** `engine/metrics.py:132-142`

```python
def _is_frame_drop(prev_ts, curr_ts, fps, threshold_factor):
    if prev_ts is None:
        return False
    if fps is None or fps <= 0:
        return False
    delta = curr_ts - prev_ts
    expected_delta = 1_000_000.0 / fps
    return delta < 0 or delta > expected_delta * threshold_factor
```

**What it's checking:** whether **this one stream's** current HW frame
timestamp arrived too late (or out of order) relative to **its own
immediately-previous frame**, given that stream's configured fps. It is
**not** a cross-stream check — stream A's drop flag never looks at
anything from stream B, and vice versa. It's also **not** a "frame was
lost in transit / decode failed" check in the sense of a missing/None
frame — `ContinuousCapture.frames_with_diagnostics` already `continue`s
past any pair where either frame object is falsy (`engine/streams.py:499-500`)
before a sample is ever built, so `_is_frame_drop` only ever sees frames
that *did* arrive; it flags a frame that arrived **too late** (or with a
timestamp that went backwards) compared to the expected per-frame
interval for that stream's fps.

- `expected_delta = 1_000_000 / fps` — the ideal microsecond gap between
  consecutive frames on that stream (e.g. ~33,333µs at 30fps).
- `frame_drop_threshold_factor` = `1.5` (`settings.yaml:111`, comment:
  *"a stream's consecutive HW frame_timestamp delta must exceed
  (1_000_000/fps) * frame_drop_threshold_factor (or be negative) to be
  flagged as a probable dropped frame"*). At 30fps that's a ~50,000µs
  (50ms) gap needed to trip it — i.e. skipping at least one whole real
  frame interval, not just minor jitter.
- `delta < 0` also trips it — a timestamp that goes backwards relative to
  the stream's own last frame (out-of-order delivery, or a HW clock
  wraparound edge case) is treated as a drop too, unconditionally.
- Both streams get this check independently every pair; either one alone
  tripping sets that stream's own `stream_a_frame_drop`/`stream_b_frame_drop`
  flag. `PairingGapMetric`/`PositionGapMetric` then react to
  `stream_a_frame_drop OR stream_b_frame_drop` (see §4) — a drop on
  *either* stream invalidates *that pair* for both metrics, even though
  only one stream actually dropped a frame.
- Owned by `TestSession` (`engine/test_session.py:31-32,39-50`), computed
  exactly once per pair from a rolling `_prev_stream_a_ts`/`_prev_stream_b_ts`
  state, then written onto the row **and** mutated onto the shared
  `FramePairSample` before any metric runs — so every metric that reads
  `sample.stream_a_frame_drop`/`stream_b_frame_drop` sees the identical
  flag, not a per-metric recomputation.

**Test coverage** (`tests/engine/test_metrics.py`):
- `test_is_frame_drop_false_when_fps_is_zero` / `..._negative` — guards the
  divide-by-zero edge case, returns `False` rather than raising.
- `test_is_frame_drop_still_detects_a_real_drop_with_valid_fps` — a
  500,000µs jump at 30fps (~33,333µs expected) trips `True`.
- End-to-end via `tests/engine/test_test_session.py`'s
  `test_process_pair_detects_frame_drop_end_to_end_and_excludes_every_metric`
  — confirms a drop on stream A alone (`stream_a_ts_us` jumps to 500,000
  while `stream_b_ts_us` stays on schedule) sets `stream_a_frame_drop=True`,
  `stream_b_frame_drop=False`, **and** that `PairingGapMetric`'s row still
  gets `pairing_gap_us_excluded=True`/`exclude_reason="frame_drop"` even
  though only one side actually dropped.

**Verdict: correct, and checking the right thing.** It is a per-stream
timing-regularity check against that stream's own history, not a
cross-stream check and not a "did the frame arrive at all" check — exactly
what the frame-drop concept is supposed to mean here (a probable *dropped*
frame inferred from a gap in an otherwise-regular per-stream cadence).

---

## 3. Is "HW TS" really a hardware timestamp?

**Yes — confirmed by direct code read, not just by name.**

`engine/streams.py:502-518` (`ContinuousCapture.frames_with_diagnostics`):

```python
metadata = rs.frame_metadata_value.frame_timestamp
if not (frame_a.supports_frame_metadata(metadata) and frame_b.supports_frame_metadata(metadata)):
    raise RuntimeError(
        "This camera/driver does not expose per-frame HW timestamp metadata "
        "(frame_metadata_value.frame_timestamp), which the sync metrics require. ..."
    )
...
ts_a = frame_a.get_frame_metadata(metadata)
ts_b = frame_b.get_frame_metadata(metadata)
```

This is `stream_a_ts_us`/`stream_b_ts_us` throughout the rest of the
pipeline — the values `PairingGapMetric` subtracts and `_is_frame_drop`
diffs against history. There are exactly **three** distinct timestamp
concepts librealsense exposes, and the codebase uses only one of them here:

| API | Domain | Used here? |
|---|---|---|
| `frame.get_frame_metadata(rs.frame_metadata_value.frame_timestamp)` | **per-sensor hardware clock**, µs | **Yes — the only one used for `stream_a_ts_us`/`stream_b_ts_us`** |
| `frame.get_timestamp()` | `global_time` domain, ms, driver-normalized | Not used anywhere in this repo (`grep` for `get_timestamp(` across the whole codebase: zero hits) |
| `frame.get_frame_metadata(rs.frame_metadata_value.time_of_arrival)` | **host/system clock** (USB arrival time) | Not used anywhere in this repo |

The codebase itself documents this as a deliberate, explicit choice, not
an accident. From `engine/streams.py`'s own runtime error text (quoted
above) and — more explicitly — from the sibling `optical_sync_poc_`
project this code was ported from (`optical_sync_poc_/CLAUDE.md:84-86`,
the authoritative design-decision record this project's own metric math
traces back to):

> **`pipeline_sync_test_diff.py`'s `ir_ts`/`rgb_ts` come from HW
> `frame_timestamp` metadata** (`frame.get_frame_metadata(rs.frame_metadata_value.frame_timestamp)`,
> **microseconds**, a per-sensor hardware clock), not `frame.get_timestamp()`'s
> `global_time` domain (milliseconds) used everywhere else in this project.
> This is a deliberate, explicit choice...

and the sibling project's own diagnostic script,
`optical_sync_poc_/scratch/timestamp_domain_check.py`, is the artifact that
originally established the distinction: it separately samples
`frame.get_timestamp()` + `frame.get_frame_timestamp_domain()` **and**
`rs.frame_metadata_value.time_of_arrival`, describing the latter as
*"always host/system clock, regardless of the primary domain."*
`frame_timestamp` is a different metadata field than both.

**Known, accepted tradeoff (not a bug):** because `frame_timestamp` is a
**per-sensor** clock, `pairing_gap_us = stream_a_ts_us - stream_b_ts_us`
is only meaningful as a *pairing quality* signal to the extent the two
sensors' independent HW clocks share (or stay close to) a common epoch. If
they don't, some or all of a nonzero `pairing_gap_us` reading reflects a
real clock offset between the two sensors rather than actual pairing
error. This is explicitly called out in both the ported docstring
(`engine/metrics.py:1-11`'s module docstring references the POC script)
and the POC's own `analyze_pairing_gap` docstring — kept as a deliberate
design choice, not a hidden assumption. `_is_frame_drop`'s per-stream
check is unaffected by this caveat, since it never compares across
streams — only a stream's own consecutive frames.

**Verdict: yes, "HW TS" is a genuine per-frame hardware timestamp** (not a
system/global-time reading, not a USB-arrival timestamp), exactly as the
`metadata = rs.frame_metadata_value.frame_timestamp` line and the
project's own documentation say. The one caveat worth remembering when
reading `pairing_gap_us` is that it's a *cross-sensor* HW-clock diff, so a
nonzero baseline could be partly clock-offset rather than pure latency —
this is a documented, deliberate tradeoff, not a defect in "is this really
HW TS."

---

## 4. Exclusion / outlier reference — every reason, every consumer

Five `exclude_reason` values exist across the two metrics:

| Reason | Set by | Meaning |
|---|---|---|
| `syncer_outlier` | `PairingGapMetric` | `abs(pairing_gap_us) > pairing_gap_outlier_threshold_us` (100,000µs, `settings.yaml:116`) — a cross-stream HW-timestamp diff implausibly large to be real pairing (or reflects clock-offset drift, per §3's caveat) |
| `frame_drop` | `PairingGapMetric` **and** `PositionGapMetric` | `stream_a_frame_drop or stream_b_frame_drop` was `True` for this pair (§2). Takes **priority** over `syncer_outlier`/`warmup` in the reason string when more than one condition is true |
| `no_led_data` | `PositionGapMetric` | `stream_a_bright`/`stream_b_bright` is `None` (LED positions not calibrated/available for this run) |
| `miss` | `PositionGapMetric` | Brightness data exists but nothing crossed either stream's on-threshold for that pair (`find_last_on_led` returned `None`) |
| `warmup` | `PositionGapMetric` | First `warmup_pairs_to_skip` pairs (`15`, `settings.yaml:123`) of the run — covers auto-exposure convergence / initial buffered-frame burst |

`PositionGapMetric`'s own priority order (`engine/metrics.py:165-189`):
`no_led_data` > `miss` > `frame_drop` > `warmup`. Confirmed by
`tests/engine/test_metrics.py` and cross-checked against
`tests/domain/test_csv_export.py:47`'s explicit comment stating this order.

**Consumer-by-consumer, confirmed against current code:**

| Consumer | What it reads | Behavior on an excluded row |
|---|---|---|
| Kept CSV (`pipeline_sync_raw.csv`) | `row["stream_a_frame_drop"] \| row["stream_b_frame_drop"]` directly | **Row is still written here** unless it's specifically a frame-drop row. A `syncer_outlier`/`warmup`/`miss`/`no_led_data`-excluded (but not frame-drop) row stays in the kept file, flagged via its own `*_excluded`/`*_exclude_reason` columns |
| Dropped CSV (`pipeline_sync_frame_drops.csv`) | Same boolean check | Only rows where `stream_a_frame_drop` or `stream_b_frame_drop` is `True` land here — regardless of what any metric's `exclude_reason` string says |
| Live `RunningStats` (`_on_row_ready`, `live_session_page.py:625-628`) | `row["pairing_gap_us_excluded"]` / `row["position_gap_ms_excluded"]` | **Skipped entirely** — an excluded value never enters the running mean/std/min/max, for either metric, on every single pair (not throttled) |
| Live plots (`_on_stats_ready`, `live_session_page.py:644-651`) | Same `*_excluded` flags, throttled to every `display_stride`-th pair | Value is replaced with `float("nan")` before `add_point()` — pyqtgraph's `connect="finite"` (set at series creation, `gui/widgets/live_plot.py`'s `add_series`) breaks the line at that point instead of drawing through it |
| Static end-of-run plot (`domain/plot_export.py`'s `_to_plot_value`) | Same `*_excluded` flags, over ALL rows (not throttled) | Same NaN convention, applied independently from the raw buffered rows — confirmed by `tests/domain/test_plot_export.py::test_to_plot_value_nans_out_excluded_values` |
| Frame-drop counters/plot (`_stream_a_drop_count`/`_stream_b_drop_count`) | Raw `row["stream_a_frame_drop"]`/`row["stream_b_frame_drop"]` booleans directly | Not a metric-exclusion consumer at all — this is the actual drop *count*, unaffected by any metric's exclusion logic |
| Debug snapshots (periodic + on-demand) | Nothing — always saved | Not gated on any exclusion; these are visual sanity-checks of the on/off call, not measurement data |

**Historical note — this used to be broken, now fixed (verified against
current code, not just the log):** `docs/algorithm_review_log.md`'s Issue
2 documents that `PairingGapMetric` originally had *zero* frame-drop
awareness — a dropped-frame pair would still count as valid HW TS Latency
data unless it also happened to exceed the much looser 100ms outlier
threshold, and the CSV split (2b) originally string-matched
`exclude_reason == "frame_drop"` rather than reading the raw boolean flags,
which could misroute a drop labeled `no_led_data` into the *kept* file.
Both are fixed in the current code: `engine/metrics.py`'s
`PairingGapMetric.update` (`:113-129`) now checks
`is_drop = sample.stream_a_frame_drop or sample.stream_b_frame_drop`
and prioritizes `"frame_drop"` over `"syncer_outlier"` in the reason
string; `domain/csv_export.py`'s `export_session_csvs` (`:51`) routes on
the raw booleans directly. Regression tests exist for exactly this
scenario:
`test_pairing_gap_metric_excludes_on_frame_drop_even_within_outlier_threshold`
and
`test_export_session_csvs_routes_by_boolean_flag_even_when_exclude_reason_is_no_led_data`.

**Verdict: today's exclusion logic is internally consistent** — every
consumer that aggregates or displays `pairing_gap_us`/`position_gap_ms` as
valid data correctly skips a frame-drop pair, and the CSV split is
immune to which `exclude_reason` string a given metric happens to produce.

---

## 5. The two-CSV split, precisely

**Code:** `domain/csv_export.py:33-59` (`export_session_csvs`)

```python
for row in rows:
    is_frame_drop = bool(row.get("stream_a_frame_drop") or row.get("stream_b_frame_drop"))
    if is_frame_drop:
        dropped_writer.writerow(row)
    else:
        kept_writer.writerow(row)
```

- **File names:** `output/pipeline_sync_raw.csv` (kept) and
  `output/pipeline_sync_frame_drops.csv` (dropped) — from
  `settings.yaml:151-156`'s `paths.raw_csv_path`/`paths.frame_drop_csv_path`,
  joined with `paths.output_dir` in `gui/main_window.py:254-255`.
- **The split is exclusively about frame drops** — the name
  "`pipeline_sync_frame_drops.csv`" is literal, not "excluded rows in
  general." A `syncer_outlier`/`warmup`/`miss`/`no_led_data` row with no
  frame drop is **still in the kept file** — this is by design, same
  convention the original POC script used (`domain/csv_export.py`'s module
  docstring: *"only a frame-drop exclusion gets its own file, every other
  exclusion reason (miss/warmup/outlier) stays in the kept file, just
  flagged via its own column"*).
- **What "kept" actually guarantees:** every row's own `pairing_gap_us_excluded`/
  `pairing_gap_us_exclude_reason` and `position_gap_ms_excluded`/
  `position_gap_ms_exclude_reason` columns are present in both files (same
  fieldnames throughout — `export_session_csvs` builds one shared
  `fieldnames` list from every row seen, `:34-38`), so "kept" does **not**
  mean "clean data" — it means "not a frame-drop row." An outlier or
  warmup row sitting in the kept CSV is still flagged and still excluded
  from the live stats/plots per §4 — you have to filter on the `_excluded`
  columns yourself if you want a purely-clean subset from the kept file.
- **Both files are always written**, even for zero rows of either kind
  (`export_session_csvs`'s header is written unconditionally,
  `test_export_session_csvs_empty_rows` confirms `(n_kept, n_dropped) == (0, 0)`
  still produces two valid files).
- **Written twice per run, same data both times:** once automatically at
  Stop (`live_session_page.py:686`, `_on_session_finished`), and again
  on-demand if the toolbar's "Export CSV" button is clicked later
  (`_reexport_last_session_csvs`, `:365-374`) — both calls pass the exact
  same buffered `rows` list, so re-exporting just overwrites the same two
  files with identical content, it does not merge or duplicate rows across
  multiple exports.

---

## 6. Full output file inventory

All paths relative to `settings.yaml`'s `paths.output_dir` (default `output/`).

| File | Written by | When | Notes |
|---|---|---|---|
| `pipeline_sync_raw.csv` | `export_session_csvs` | At Stop, and again on manual "Export CSV" | Kept rows — see §5 |
| `pipeline_sync_frame_drops.csv` | `export_session_csvs` | Same as above | Dropped (frame-drop) rows — see §5 |
| `pipeline_sync_plot.png` | `export_session_plot` | At Stop only | Static matplotlib 2-panel plot: pairing gap + position gap (NaN'd for excluded, §4) on top, per-pair frame-drop spike (0/1, not cumulative) on bottom |
| `live_led_state_stream_a.png` / `_stream_b.png` | `_save_led_state_debug_images` | At Stop automatically, or any time via "Save Debug Snapshot" | Full-frame on/off overlay (green=on, red=off) from the most recent frame's cached mask |
| `periodic_led_state_stream_a_pair#####.png` / `_stream_b_...` | `_maybe_save_periodic_snapshot` | Every `snapshot_every_n_pairs` pairs (`20`) during the run, capped at `max_snapshots` (`15`) per stream | Filename embeds `pair_index` so it cross-references the CSV's own `pair_index` column directly; all cleared at the start of the *next* Start (`_clear_periodic_snapshots`), so stale files from a previous run never linger |
| `<series_key>_chart_export.csv` (e.g. `pairing_gap_us_chart_export.csv`) | `_export_chart_csv` | On-demand, per-chart "Export CSV" button | Exports exactly what's currently plotted on that chart (`LivePlot.get_series_data`) — **not** a `TestSession` row export; if a series is showing NaN'd-out excluded points, those NaNs are exported too, verbatim |
| `debug_<slug>_detection.png` (e.g. `debug_infrared1_detection.png`, `debug_color_detection.png`) | Calibration page | During calibration, not Live Session | Per-stream-slug (not `stream_a`/`stream_b`), since two different pairings on the same camera can share a calibrated stream. These two files are the only ones currently present in this repo's `output/` — confirming no Live Session has actually been run/recorded here yet |

---

## 7. Known open caveats (not bugs in the exclusion logic itself, but ways a bad measurement could still slip through)

These are documented as **open** (not yet fixed) in `docs/algorithm_review_log.md`
as of this writing. Neither is a flaw in the CSV/plot/stats routing audited
above — both are about whether the *underlying measurement itself* can be
silently wrong in a way none of the five exclude_reason checks would catch.

- **Issue 5 — `find_last_on_led` picks the *longest* on-run with no sanity
  bound.** `engine/metrics.py:47-94` treats the longest contiguous run of
  "on" LEDs as the current scan position, on the assumption that any
  shorter lit run is transient noise. A stuck/miscalibrated LED cluster
  that's persistently "on" longer than the real moving scan position would
  silently and consistently win — this never trips `miss` (something *is*
  on), never trips `syncer_outlier` (it's a `position_gap_ms` computation,
  not `pairing_gap_us`), and never trips `frame_drop`. It would just
  produce a stable, plausible-looking, wrong `position_gap_ms` value that
  sails straight into the kept CSV and the live stats as if it were good
  data. A related sub-finding (5a) notes the tie-break between
  equal-length runs is index-order-dependent, not evidence-based.
- **Issue 4 — calibration's row-grouping can silently mis-bin LED
  centroids.** `domain/calibration.py:14-34`'s `assign_grid_ids` uses
  single-linkage chaining (each centroid compared only to its immediate
  y-sorted predecessor, not to its row's first point) against a fixed
  `row_gap_px` (15px) — a single stray/noise centroid sitting between two
  real rows can silently bridge them into one mis-ordered row, scrambling
  `led_id`s with no exception and no warning. This would corrupt
  calibration's stored LED positions/thresholds upstream of every Live
  Session run using that calibration — again, nothing in the five
  exclude_reason checks would catch it, since it doesn't manifest as a
  timing or brightness anomaly on any single pair.

Neither issue currently has a fix landed. Worth keeping in mind when
judging *why* a run's numbers look wrong, separately from whether the
kept/dropped/excluded routing audited in §2–§5 is doing its job (it is).

---

## 8. What this report did NOT verify (as of the first pass)

- No real recorded Live Session `pipeline_sync_raw.csv`/
  `pipeline_sync_frame_drops.csv` existed yet in this repo's `output/` to
  spot-check row-for-row against a live run — the first pass of this audit
  was a trace of the code paths plus the existing automated tests
  (`tests/engine/test_metrics.py`, `tests/engine/test_test_session.py`,
  `tests/domain/test_csv_export.py`, `tests/domain/test_plot_export.py`,
  `tests/domain/test_calibration.py`, `tests/domain/test_running_stats.py` —
  all read in full for this report), not a real-data comparison.
  **Superseded by §9** — a real recorded session was supplied and
  cross-checked afterward.
- Issues 4 and 5 above are logged as code-inspection findings in
  `docs/algorithm_review_log.md`; neither has confirmed real-hardware
  evidence of actually occurring in practice on this project (unlike, e.g.,
  the now-fixed Issue 2, which had a concrete before/after code diff and
  regression tests).
- `ContinuousCapture`'s real `rs.pipeline()` internals are hardware-only
  and untested by design (per `CLAUDE.md`) — this report traces what the
  code does, not a live hardware run confirming it.

---

## 9. Real-data cross-check — `live_session_2026-08-10_14-37-56`

Supplied files: `pipeline_sync_raw.csv`, `pipeline_sync_frame_drops.csv`,
`pairing_gap_us_chart_export.csv`, `position_gap_ms_chart_export.csv`,
`stream_a_frame_drops_chart_export.csv`. All numbers below are computed
directly from these files (via a one-off script over the actual rows, not
eyeballed), pair-index-matched against each other.

### 9.1 Row accounting — the two-CSV split is exact

| | Count |
|---|---|
| Kept rows (`pipeline_sync_raw.csv`) | 11,639 |
| Dropped rows (`pipeline_sync_frame_drops.csv`) | 1,419 |
| **Total pairs** | **13,058** (pair_index 0–13,057, none missing) |

Kept + dropped = every pair exactly once — confirms §5's routing claim on
real data, not just in the unit tests.

### 9.2 `miss`/`warmup` priority order — confirmed exactly as designed

Pair-by-pair inspection of pair_index 0–15 (`warmup_pairs_to_skip=15`):

| pair_index | frame_drop (a, b) | `position_gap_ms_exclude_reason` |
|---|---|---|
| 0–3 | False, False | `miss` (no LED detected yet — scan hadn't started) |
| 4–14 | False, False | `warmup` |
| 15 | False, False | *(none — first clean pair)* |

4 `miss` + 11 `warmup` = 15 = `warmup_pairs_to_skip` exactly, and pair 15 is
the first pair with no exclusion — matches `_pair_count <= warmup_pairs_to_skip`
(`engine/metrics.py:167`) and the `no_led_data > miss > frame_drop > warmup`
priority order to the pair. Across the whole 13,058-pair run, `position_gap_ms_exclude_reason`
in the kept file is only ever `miss` (4 rows), `warmup` (11 rows), or empty
(11,624 rows) — no `no_led_data` occurred (LED positions were available
all run) and no *kept* row is ever `frame_drop` (confirming, on real data,
that the boolean-flag CSV routing from §5 is airtight — a frame-drop row
never leaks into the kept file no matter what its `exclude_reason` string
says).

### 9.3 The outlier check (`syncer_outlier`, 100,000µs) never fired once

Every single one of the 11,639 kept rows has `pairing_gap_us_exclude_reason`
empty — `syncer_outlier` occurred **zero** times in this entire run. Not
because the check is broken; because real `pairing_gap_us` values in this
run are either `0` (11,455 rows) or a small handful of `-16666`/`-16667`
(184 rows, see §9.5) — nowhere near the 100,000µs bar. This says nothing
about whether 100,000µs is the *right* bar (§9.5 argues it's too loose to
catch a real, recurring anomaly) — only that, as configured, it never
excluded anything on this run.

### 9.4 Do the `pairing_gap_us`/`position_gap_ms` graphs ever plot a real frame-drop pair as clean data? — No, but they also don't visibly flag most drops either

Matched every frame-drop pair_index against both chart-export CSVs
(`pairing_gap_us_chart_export.csv`, `position_gap_ms_chart_export.csv`,
each sampled at the `display_stride=10` cadence, same as the live plots):

| | Count |
|---|---|
| Frame-drop pairs that happen to land on a sampled (every-10th) pair_index | 82 |
| ...of those, correctly plotted as `nan` | **82 (100%)** |
| ...of those, incorrectly plotted as a real number | **0** |
| Frame-drop pairs that do NOT land on a sampled pair_index (i.e. invisible to these two charts either way) | 1,337 (94.2% of all 1,419 drops) |

So the narrow claim "does a dropped pair's own real number ever get
plotted" is **false on this real run** — confirms §4's code-level claim
holds in practice, zero counterexamples out of 82 opportunities.

**But this is very likely what actually reads as "the graph shows FD
frames" when watching a run:** `pairing_gap_us`/`position_gap_ms` only
check the *exact* sampled pair's own exclusion flag (§4) — unlike the
frame-drop marker plot, which uses `_stream_a_drop_since_last_plot`/
`_stream_b_drop_since_last_plot` (`live_session_page.py`) to flag "a drop
happened *somewhere* in the last `display_stride` pairs," these two charts
have no equivalent. §9.6 below shows the per-pair drop rate climbs from
~5% early in the run to ~21% by the second half — at a 21% per-pair drop
rate, a 10-pair sampling window has roughly an 89% chance of containing
*at least one* drop (`1 - 0.79^10`), yet the pairing/position-gap lines
only break when the *landed-on* sample itself is one of those drops (which
only happened 82 times out of 1,419). The rest of the time, the line
quietly continues, connecting whichever two non-dropped samples happened to
land nearest each other, straight through a stretch that may have had
several real drops in between. The data is not corrupted (every dropped
pair is still correctly excluded from the buffered row, the CSV, and the
running stats), but the two main charts under-communicate just how
drop-heavy a given stretch of the run actually was, in a way the
frame-drop marker plot right below them does not.

### 9.5 A real, recurring, currently-uncaught anomaly: exactly ±1-frame-interval gaps

184 kept rows (1.58% of all kept rows) have `pairing_gap_us_excluded=False`
**and** `abs(pairing_gap_us) >= 5,000` — every single one of them is
exactly `-16666` (179 rows) or `-16667` (5 rows) microseconds — i.e. almost
precisely `-1,000,000/60` (one frame interval at 60fps). 183 of the 184 also
have `position_gap_ms_excluded=False` with a simultaneous `position_gap_ms`
of `-15`, `-16`, or `-17` (156/10/17 rows respectively); the remaining one
(pair 2) has `position_gap_ms` excluded via `miss` instead. Example rows:
pair 26, 51, 88, 101, 126, 151, 176, 189, 214, 311, 372, 385, 468, 563, 646,
797, 890, 915, 927, ... (spread throughout the entire run, not clustered).

None of these 184 rows are excluded by anything today:
- Not `frame_drop` — each stream's own consecutive-frame HW-timestamp
  check passes (§2), since the anomaly isn't in either stream's own
  cadence, it's between the two streams.
- Not `syncer_outlier` — 16,666µs is far under the 100,000µs threshold
  (§9.3).

This is a concrete, real-data instance of exactly the kind of gap the
outlier/frame-drop net was never designed to catch: a small but suspicious,
suspiciously-exact, recurring cross-stream pairing offset that both current
checks are blind to by construction (frame-drop only looks within one
stream's own history; outlier's threshold is 6x too loose to catch a
one-frame-interval-sized wobble). Directly relevant to the "should there be
a stricter clean CSV" ask in §10.1.

### 9.6 Frame-drop rate climbs steadily over the run's duration

Per-1,000-pair-index drop rate across the run:

| Pair range | Drop rate |
|---|---|
| 0–999 | 5.4% |
| 1000–1999 | 0.0% |
| 2000–2999 | 0.4% |
| 3000–3999 | 0.2% |
| 4000–4999 | 6.1% |
| 5000–5999 | 8.4% |
| 6000–6999 | 9.0% |
| 7000–7999 | 13.8% |
| 8000–8999 | 17.1% |
| 9000–9999 | 19.3% |
| 10000–10999 | 18.3% |
| 11000–11999 | 21.5% |
| 12000–12999 | 21.3% |

A real, monotonic-ish escalation from near-zero to ~21% over the course of
a single run (not a code/logic finding — a hardware/environment
observation from this specific recorded session, flagged here because it's
exactly the kind of thing the "graphs under-communicate drop-heavy
stretches" finding in §9.4 would hide from a quick glance at the charts).

---

## 10. Requested follow-up work — documented only, **not implemented** (per instruction)

Two concrete gaps came out of §9, both explicitly to be fixed in a later
pass, not this one:

### 10.1 A third, genuinely-clean CSV

Today there are exactly two CSVs (§5), and "kept" ≠ "clean": on the real
run in §9, the kept file's 11,639 rows break down as 11,624 rows
unexcluded on both metrics, of which 183 still carry the §9.5
±1-frame-interval anomaly unflagged — leaving exactly **11,441** rows that
are both unexcluded on every metric *and* free of the §9.5 anomaly, out of
13,058 total pairs (the other 4 `miss` + 11 `warmup` + 1,419 `frame_drop`
rows account for the remaining 1,617). No
existing file represents "only the rows with zero exclusion on any metric,
for any reason." The requested fix: a third output file (e.g.
`pipeline_sync_clean.csv`) written by `export_session_csvs` (or a sibling
function next to it in `domain/csv_export.py`) containing only rows where
neither `pairing_gap_us_excluded` nor `position_gap_ms_excluded` is `True`
— i.e. not `frame_drop`, not `warmup`, not `miss`, not `no_led_data`, not
`syncer_outlier`. (Whether the §9.5-style ±1-frame anomaly should also gain
its own exclusion reason so it's caught by this same check, rather than
slipping through as "clean," is a related open question for that fix —
flagged here, not decided.)

### 10.2 Pairing-gap/position-gap charts should reflect drops in their whole sampling window, not just the landed-on sample

Per §9.4: `_on_stats_ready`'s `pairing_gap_us`/`position_gap_ms` plotting
only NaNs a point when the exact sampled pair itself was excluded — it
should instead NaN a point whenever ANY pair since the last plotted point
was frame-dropped, the same way the frame-drop marker plot already tracks
`_stream_a_drop_since_last_plot`/`_stream_b_drop_since_last_plot`
(`gui/pages/live_session_page.py`). This would make a drop-heavy stretch
visibly break the line on all three charts consistently, instead of only
the frame-drop marker plot showing it while the other two silently connect
through.
