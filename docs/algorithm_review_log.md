# Algorithm Review Log

Living log for the correctness review of `domain/realsense_utils.py`,
`domain/calibration.py`, and `engine/metrics.py` in this project
(`optical_sync_gui_generic`). This review carries forward from the
equivalent review of the sibling project `optical_sync_gui` (its own
`docs/algorithm_review_log.md`, on that project's
`worktree-algo-review-log` branch), but nothing is copied over
unverified: this project's generic `stream_a`/`stream_b` architecture
(`resolve_and_group` sensor-grouping, per-stream-slug `config.yaml`
keying, operator-chosen stream pairing rather than a hardcoded IR/RGB
pairing) is different enough that each issue was re-confirmed against
this repo's actual current code before being logged here. One entry per
issue found. Nothing here is applied to code until explicitly decided in
conversation - this file just tracks what was found, what was decided,
and why, so a future session doesn't have to re-derive the reasoning.

Status values: `open` (found, not yet decided) / `decided-not-fixing`
(looked at, chose to leave as-is, reason recorded) / `fixed` (implemented,
commit/PR noted).

---

## Issue 1: Fixed brightness-sampling window vs. actual LED pixel spacing

**Status:** fixed (implemented on `worktree-neighborhood-size-safety-cap`,
pushed, PR not yet opened - not merged into `main` as of this writing)

**Where:** `domain/realsense_utils.py:14` (`sample_neighborhood_brightness`),
`:24` (`sample_all_neighborhood_brightness`); consumed by
`domain/calibration.py:37` (`build_positions_with_thresholds`) and
`engine/session_engine.py:65-82` (`_frame_pairs_with_brightness`, live
sampling).

**The problem:** `neighborhood_size` is a fixed NxN pixel window (default
5), independent of image resolution, ROI size, or `num_leds`. Whether
that's safe depends on the real measured pixel distance between adjacent
LEDs in the captured frame - which the code never checks. Same root cause
as the sibling project: calibration itself isn't corrupted (uniform-state
on/off frames), but live classification is at risk if LEDs end up close
together in pixel terms, since a window centered on one LED can then bleed
into a neighbor's pixels during the live session, where adjacent LEDs are
normally in *different* on/off states. If anything the risk is broader
here than in the sibling project: this app lets an operator pick *any*
resolution/ROI/stream-pair combination (two IR streams, IR+color, two
color streams) per run via Stream Config, so there's an even wider range
of real pixel spacings a fixed constant would need to stay safe across.

**Secondary finding:** `settings.yaml`'s `calibration.neighborhood_size`
and `test.neighborhood_size` are two independently-edited keys, both
defaulting to `5`, never checked against each other - same silent-
divergence risk as the sibling project.

**Decision: Option B chosen** (recompute on the fly, no `config.yaml`
schema change) - resolution/ROI/stream-pairing vary per run by design in
this project, ruling out a persisted one-time constant (Option A) or a
warning-only approach (Option C).

**Candidate fixes (carried over from the sibling review):**
- A. Measure typical LED pixel spacing during calibration (reusing/
  extracting `merge_close_centroids`'s nearest-neighbor-distance logic,
  `domain/realsense_utils.py:60-83`) and persist a derived safe window
  size into `config.yaml`, used by both calibration and live sampling.
- B. **(chosen)** Same measurement, but recomputed on the fly from the
  already-loaded position arrays (`stream_a_xy`/`stream_b_xy` in
  `session_engine.py`, the loaded `xy_positions` in `calibration.py`) both
  at calibration time and at live session start, instead of persisting
  anything new - guarantees calibration and live use an identical value by
  construction, no schema change.
- C. Leave sampling behavior untouched; add a calibration-time warning (in
  the same style as the existing low-contrast/row-layout-mismatch warnings
  in `gui/pages/calibration_page.py:163-180`) if the configured
  `neighborhood_size` could geometrically reach a neighboring LED, so a
  human notices and retunes `settings.yaml` by hand.

**Fix applied:** new `domain/realsense_utils.py` helper,
`safe_neighborhood_size(xy_positions, configured_size, min_size=3,
spacing_fraction=0.5)`, reuses the Issue-3-fix's `_typical_spacing` helper
to measure the real median nearest-neighbor LED distance, then caps
`configured_size` at `max(min_size, int(spacing * spacing_fraction))` -
only ever shrinks the configured value, never grows it, and falls back to
`configured_size` unchanged when there are fewer than two LEDs to measure
a spacing from. `domain/calibration.py`'s `build_positions_with_thresholds`
computes this once from `xy_positions` before sampling on/off frames.
`engine/session_engine.py`'s `SessionEngineThread.__init__` computes it
once each for `stream_a_xy`/`stream_b_xy` (not per-frame), and
`_frame_pairs_with_brightness` uses the capped values. `settings.yaml`'s
`calibration.neighborhood_size`/`test.neighborhood_size` comments updated
to document these are now upper bounds, not guaranteed actual values.

**Verification:** full suite green (175 passed) after independent review
of the diff. New tests in `tests/domain/test_realsense_utils.py` cover
`safe_neighborhood_size`'s fallback/no-op/capping/flooring branches; a new
test in `tests/domain/test_calibration.py` spies on
`sample_neighborhood_brightness` to confirm the capped size is what
actually reaches the real sampling calls, not just that the helper's own
math is right in isolation. Independently re-verified during the 2026-07-30
code-review pass (separate fresh-eyes agent, no context from the
implementation) - found nothing wrong: confirmed only-shrinks/never-grows
behavior, correct fallback on `None`/empty/`<2`-point inputs, and that
`stream_a_xy` really is an `(N,2)` coordinate array (not a 5-field position
record) at the real call site (`gui/main_window.py:171`).

**Open questions for the user (rig-specific, not needed to implement
Option B, but still useful context for tuning `spacing_fraction`/`min_size`
later if real runs show the margin is too tight or too loose):**
- Typical ROI size in pixels and resolution actually used in practice on
  this project's rigs.
- Physical panel layout (rows x columns for the configured `num_leds`) and
  LED pitch.
- Whether a real `config.yaml` from an actual calibration run on this
  project has been inspected for today's actual nearest-neighbor spacing
  (not yet done here, unlike the sibling review which had a real D435I
  example to check against).

---

## Issue 2: Frame drops don't exclude "HW TS Latency" (`PairingGapMetric`), only "Optical Sync"

**Status:** fixed (merged into `main` via PR #1, commit `932426d`)

**Where:** `engine/metrics.py:105-119` (`PairingGapMetric.update`),
`gui/pages/live_session_page.py:594,615` (stats/plot gating on
`pairing_gap_us_excluded`). Frame-drop detection itself lives entirely
inside `PositionGapMetric.update` (`engine/metrics.py:160-189`, via
`_is_frame_drop` at `:122-132`), which is the only place
`stream_a_frame_drop`/`stream_b_frame_drop` get computed.

**The problem:** identical bug to the sibling project, confirmed by reading
this repo's actual `engine/metrics.py`. `PairingGapMetric` only excludes a
pair when `abs(gap) > outlier_threshold_us` (100ms, `settings.yaml`'s
`test.pairing_gap_outlier_threshold_us`) - it has no knowledge of frame
drops at all. `docs/project_overview.md:46-48` documents the intended
design explicitly: "a dropped frame invalidates both measurements for that
pair and needs to be visible immediately." `docs/technical_deep_dive.md:157`
independently confirms `frame_drop` is only wired up as one of
`PositionGapMetric`'s own exclusion reasons (§7), and §8's own
`PairingGapMetric` writeup (`docs/technical_deep_dive.md:163-179`) never
mentions frame drops at all - the documentation itself only ever describes
frame-drop exclusion in the Optical Sync context, even while the overview
promises "both measurements." A frame-drop-affected pair still counts as
valid HW TS Latency data in the live plot and the running min/avg/std/max
stats, unless it also happens to exceed the much looser 100ms outlier
threshold.

**Full map - every consumer of frame-drop status, current behavior
(confirmed against `gui/pages/live_session_page.py`):**

| Consumer | Reads | Current behavior on a drop |
|---|---|---|
| `PositionGapMetric` exclusion | computes `stream_a_drop`/`stream_b_drop` itself | Correctly excludes (`frame_drop` reason, `:185-186`) - but only reached after its own `no_led_data`/`miss` checks (`:169-170`, `:179-180`), so a drop pair that also lacks LED data gets labeled `no_led_data` instead (sub-finding 2b) |
| `PairingGapMetric` exclusion | nothing - no drop awareness at all | **Never excludes** for a drop. Only its own 100ms outlier check. Core bug |
| Live RunningStats (`_on_row_ready`, `live_session_page.py:594,596`) | each metric's own `_excluded` flag | Optical Sync stats correctly skip drop pairs; HW TS Latency stats do not |
| Live plot points (`_on_stats_ready`, `:615,619`) | same `_excluded` flags | Same split: Optical Sync plot gaps out (NaN) correctly, HW TS Latency plot doesn't |
| Frame-drop counters/plot (`_stream_a_drop_count`/`_stream_b_drop_count`, `:581-584`, `:622-623`, `:634-637`) | `row["stream_a_frame_drop"]`/`row["stream_b_frame_drop"]` directly | Correct - reads the raw booleans, not any metric's derived exclusion |
| CSV split (`domain/csv_export.py:51-54`, `export_session_csvs`) | any `*_exclude_reason` column `== "frame_drop"` (string match) | Only works today because `PositionGapMetric` happens to write that literal string when it wins the priority race - see sub-finding 2b |
| Debug snapshots (periodic + on-demand) | nothing | Never gated on drop status - same as sibling project, arguably fine since these are visual sanity-checks, not measurement |

**Sub-finding 2b - CSV split is string-matching a side effect, not reading
the source of truth:** confirmed identical in this repo -
`export_session_csvs` (`domain/csv_export.py:50-54`) decides "was this row a
frame drop" by scanning for any `*_exclude_reason` column whose value
literally equals `"frame_drop"`, instead of checking the always-present
`row["stream_a_frame_drop"]`/`row["stream_b_frame_drop"]` booleans directly.
This only works because `PositionGapMetric`'s priority order (`no_led_data`
> `miss` > `frame_drop` > `warmup`, `engine/metrics.py:169-188`) sometimes
produces that exact string. A drop pair that *also* has no LED data gets
labeled `no_led_data` instead - and would be **misrouted into the "kept"
CSV** despite genuinely being a dropped frame.

**Sub-finding 2c - dead config key:** `settings.yaml:59-61`'s
`test.max_fd_snapshots: 10` ("cap on FD-triggered on/off overlay snapshots
per run, separate bucket from periodic sampling") is never read anywhere in
this repo's `.py` source (grepped the whole codebase, only hit is the
`settings.yaml` comment/key itself) - same documented-but-never-implemented
feature as the sibling project.

**Decision: Option A chosen** (hoist frame-drop detection to a shared source
of truth). See "Fix applied" below for what actually landed.

**Candidate fixes (carried over from the sibling review):**
- A. Hoist frame-drop detection out of `PositionGapMetric` into something
  both metrics consult (e.g. computed once per pair in
  `engine/test_session.py`'s `TestSession.process_pair`, or attached to
  `FramePairSample`), so both metrics correctly exclude on a drop. Matches
  the documented intent exactly. Should also fix 2b by having
  `TestSession`/`export_session_csvs` key off that same shared,
  always-present drop signal directly instead of the derived
  exclude_reason strings.
- B. Cheaper patch: in `TestSession.process_pair`, after both metrics run,
  OR `row["stream_a_frame_drop"]`/`row["stream_b_frame_drop"]` into
  `row["pairing_gap_us_excluded"]` retroactively, without touching
  `PairingGapMetric` itself. Smaller diff, but `PairingGapMetric` used/
  tested in isolation would still report the wrong exclusion. Would need a
  separate, equally cheap patch to `export_session_csvs` for 2b (check the
  raw `stream_a_frame_drop`/`stream_b_frame_drop` columns directly, not
  exclude_reason strings).
- C. Decide the current behavior is intentional (HW TS Latency is a pure
  hardware-timestamp diff, arguably still meaningful across a dropped
  frame) and fix `docs/project_overview.md`'s wording instead of the code,
  relying on the CSV's existing per-row `stream_a_frame_drop`/
  `stream_b_frame_drop` columns for anyone who wants to filter it out
  themselves. Leaves the live stats tile/plot contaminated by default.
  Doesn't address 2b (CSV misrouting) on its own either way.
- For 2c (dead `max_fd_snapshots`): either implement FD-triggered
  snapshotting for real, or remove the unused key/comment from
  `settings.yaml` so the file stops documenting a feature that doesn't
  exist.

**Objective going forward (per user, same as sibling project):**
frame-drop pairs should be ignored consistently everywhere
`pairing_gap_us`/`position_gap_ms` are aggregated or displayed as valid
data - decide on A/B/C above, then apply the same decision to sub-findings
2b and 2c so there's one consistent, source-of-truth notion of "this pair
doesn't count," not a patchwork of independent checks that happen to agree
today by coincidence.

**Not yet done for this project (unlike the sibling review):** no real
screenshot or recorded `output/pipeline_sync_frame_drops.csv` from an
actual run on this project has been inspected yet to visually/data-confirm
the bug reproduced in practice here (before the fix), only that the code
path was identical.

**Fix applied:** `TestSessionConfig` now carries
`stream_a_fps`/`stream_b_fps`/`frame_drop_threshold_factor`, and
`TestSession.process_pair` computes `stream_a_frame_drop`/`stream_b_frame_drop`
once per pair (owning the rolling previous-timestamp state that
`PositionGapMetric` used to own), writes them straight into the row, and
mutates the `FramePairSample` so every metric's own `update(sample)` sees the
same flags. `PairingGapMetric.update` now excludes with `exclude_reason=
"frame_drop"` when either stream dropped (taking priority over its own
`"syncer_outlier"` reason) - this is the actual bug fix. `PositionGapMetric`
now just reads `sample.stream_a_frame_drop`/`stream_b_frame_drop` instead of
computing them itself; its own `no_led_data`/`miss`/`frame_drop`/`warmup`
priority order is unchanged.

**2b fixed the same way:** `domain/csv_export.py`'s `export_session_csvs`
dropped the `drop_reason`-string-matching heuristic entirely and now checks
`row.get("stream_a_frame_drop") or row.get("stream_b_frame_drop")` directly -
immune to whatever `exclude_reason` priority a given metric happens to pick.
Regression test added (`tests/domain/test_csv_export.py`) covering exactly
the misrouting scenario: a pair labeled `"no_led_data"` but with
`stream_b_frame_drop=True` now correctly routes to the dropped file.

**2c fixed:** `settings.yaml`'s dead `test.max_fd_snapshots: 10` key and its
comment removed (chosen over implementing the feature for real).

**Verification:** full suite green (169 passed) after independent review of
the diff (`engine/metrics.py`, `engine/test_session.py`,
`domain/csv_export.py`, `gui/pages/live_session_page.py`, `settings.yaml`,
plus updated/new tests in `tests/engine/test_metrics.py`,
`tests/engine/test_test_session.py`, `tests/domain/test_csv_export.py`).
`engine/session_engine.py` confirmed to need no changes (it only ever
receives an already-built `test_session`/`position_gap_metric`).

**Not yet done (after the fix):** `docs/project_overview.md`'s "both
measurements" claim is now actually true in code; `docs/technical_deep_dive.md`
§7-9 still describes frame-drop detection as living inside
`PositionGapMetric` only - worth a follow-up doc pass, not done as part of
this fix (out of scope of the agent brief). No real recorded run's output
re-checked post-fix either.

**Re-verified in the 2026-07-30 code-review pass** (fresh-eyes agent, no
context from the original implementation): confirmed the hoist is correct
end to end - `TestSession.process_pair` owns the rolling-timestamp state
and mutates `sample` before any metric runs, `PairingGapMetric` excludes
with the right reason priority (drop > outlier), `PositionGapMetric`'s own
priority order is unchanged, and no stale caller anywhere in the repo still
passes the old `PositionGapMetric(stream_a_fps=..., ...)` constructor
signature or `export_session_csvs(..., drop_reason=...)` argument. Nothing
wrong found.

---

## Issue 3: Debug overlay circle radius is a fixed constant, same root cause as Issue 1

**Status:** fixed (merged into `main` via PR #1, commit `932426d`)

**Where:** `domain/realsense_utils.py:133` (`save_debug_detection_image`)
and `:149` (`draw_led_state_overlay`) - both call
`cv2.circle(..., 8, ...)`, a hardcoded 8px radius (16px diameter) with no
relationship to actual LED pixel spacing, resolution, or ROI. Confirmed
identical to the sibling project's code.

**The problem:** same as the sibling project - at a resolution/ROI where
real LED spacing is smaller than ~16px, adjacent debug circles would fully
overlap into a solid blob, making individual LEDs undistinguishable. This
doesn't corrupt the actual measurement (purely a visualization), but
defeats the overlay's stated purpose (per `draw_led_state_overlay`'s own
docstring: letting an operator visually confirm `PositionGapMetric`'s
on/off call is correct for a given frame) - and fails exactly in the
tight-spacing runs where that check matters most. Not yet confirmed via a
real screenshot on this project specifically (the sibling project's
confirmation was a D455 screenshot on that project) - logged here on code
inspection alone, pending a real tight-spacing run on this project to
visually confirm.

**Candidate fixes (carried over from the sibling review, not yet chosen):**
- Whatever spacing-measurement mechanism gets chosen for Issue 1 (A/B)
  could also drive this radius (e.g. some fraction of measured spacing,
  capped so circles never touch), fixing both with one shared computation.
- Could also be fixed independently/more simply of Issue 1, since it's
  cosmetic only - e.g. just cap the radius at some fraction of the
  nearest-neighbor distance already computed by `merge_close_centroids` at
  calibration time, without changing the actual sampling window at all.

**Decision:** fixed standalone now, independent of Issue 1 (per user).

**Fix applied:** the nearest-neighbor median-spacing computation already
inlined inside `merge_close_centroids` was extracted into a shared helper,
`_typical_spacing(points)` (returns `None` below 2 points, else the median
nearest-neighbor distance) - `merge_close_centroids` now calls it instead of
duplicating the logic. A new `_debug_circle_radius(points)` derives the
radius as `8 if spacing is None else max(2, min(8, int(spacing * 0.3)))` -
i.e. today's 8px stays the ceiling (identical behavior when there are fewer
than 2 points or spacing is wide), floored at 2px, scaled down at 0.3x
measured spacing in between. Both `save_debug_detection_image` (from
`centroids`) and `draw_led_state_overlay` (from `xy_positions`) now call
this instead of hardcoding `8` - no signature/caller changes needed.

**Verification:** full suite green (169 passed) after independent review of
the diff (`domain/realsense_utils.py`). New tests added in
`tests/domain/test_realsense_utils.py`: direct unit tests for
`_typical_spacing`/`_debug_circle_radius` covering all branches, plus two
rendered-image tests confirming two circles 10px apart no longer merge into
one blob. Existing single-point tests confirmed to still pass unchanged
(fewer-than-2-points case falls back to the original fixed 8px). Confirmed
`tests/domain/test_calibration.py` doesn't reference these functions, so no
changes needed there.

---

## Issue 4 (candidate, newly found - not yet fully scoped): `assign_grid_ids`'s row-splitting is a fixed pixel-gap constant, same root cause class as Issues 1/3

**Status:** open

**Where:** `domain/calibration.py:14-34` (`assign_grid_ids`), called from
`gui/pages/calibration_page.py:152,161`.

**The problem:** `assign_grid_ids` sorts ALL detected centroids by y
globally, then starts a new row whenever consecutive y-values differ by
more than a fixed `row_gap_px` (default 15, `settings.yaml`'s
`calibration.row_gap_px`). This is the same "fixed constant vs. real,
per-run pixel geometry" shape as Issues 1 and 3: whether 15px safely
distinguishes "next LED in the same row" from "first LED of the next row"
depends entirely on the actual panel geometry and camera framing for that
specific run, which the code never measures. A tilted panel/camera, or a
run where real row-to-row spacing is smaller than `row_gap_px`, could
silently misassign `led_id`s (wrong row grouping) with no error raised.

The only existing safety net is `calibration_page.py:163-167`'s
`row_layout_a != row_layout_b` warning - but that only compares stream A's
row layout against stream B's, not against any independently-known-correct
panel layout (e.g. an expected rows x columns for the configured
`num_leds`). Two streams could agree with each other on a *wrong* row
split (e.g. both under-detecting row boundaries the same way) and this
warning would stay silent.

**Candidate fixes (not yet fleshed out to the same depth as Issues 1-3 -
flagging for discussion, not yet analyzed with alternatives/tradeoffs):**
- Could ride on the same spacing-measurement mechanism proposed for Issue 1
  (median nearest-neighbor distance via `merge_close_centroids`) to derive
  a safe `row_gap_px` per run instead of a fixed constant.
- Could validate `row_layout` against an expected panel shape (if one is
  ever configured/known) rather than only cross-checking stream A vs.
  stream B against each other.

**Open questions for the user:** is the physical panel layout (rows x
columns) known/fixed per rig, such that an expected `row_layout` could be
validated against? Has a real miscount ever actually been observed in
practice, or is this purely a code-inspection finding so far (unlike
Issues 1-3, which all have some form of real evidence behind them)?

**Sub-finding 4a (found in the 2026-07-30 code-review pass) - single-linkage
chaining lets ONE noise centroid silently bridge two real rows, independent
of how well `row_gap_px` is tuned:** the row-split test only compares each
centroid to its immediate predecessor in the globally-y-sorted list
(`curr[1] - prev[1] > row_gap_px`, `domain/calibration.py:21`), not to the
row's first point - classic single-linkage clustering, which chains through
a bridging point even when the real geometry is fine. Worked example: real
rows at y≈10 and y≈40 (30px apart, well clear of the default
`row_gap_px=15`), plus one stray centroid (dust/reflection/blob-merge
artifact) at y=25 sorts between them. Consecutive y-diffs are 15 and 15 -
neither exceeds `row_gap_px=15` (`>`, not `>=`), so the noise point chains
both real rows into ONE row-plus-noise blob, then re-sorted by x - `led_id`s
silently scrambled across what should've been two independent rows, with no
exception and no warning (the existing `row_layout_a != row_layout_b`
cross-check only compares row *counts* between streams, not which physical
rows they are - both streams could bridge the same way and stay silent).
This is a materially different, and arguably more urgent, failure mode than
"pick a better `row_gap_px`": no constant choice fixes it, since a single
outlier detection defeats it regardless. Not currently tested (existing
`tests/domain/test_calibration.py` coverage is a clean two-row case with no
noise point). Worth folding into whatever fix gets chosen for the main
Issue 4 finding, or fixing independently (e.g. compare each candidate row
split against the row's own first point, not just the immediate
predecessor, to prevent one bridging point from merging two rows) -
tradeoffs not yet analyzed, flagging for discussion like the rest of Issue 4.

---

## Issue 5 (candidate, newly found - not yet fully scoped): `find_last_on_led` picks the longest "on" run, vulnerable to a persistently-lit LED cluster

**Status:** open

**Where:** `engine/metrics.py:45-92` (`find_last_on_led`), called from
`PositionGapMetric.update` (`:176-177`).

**The problem:** `find_last_on_led` finds every contiguous run of "on" LEDs
(handling wraparound between the last and first LED index, since the panel
scans sequentially and wraps row-to-row) and returns the *last index of the
longest run* as "the current scan position." This implicitly assumes the
longest lit run is always the genuine current position, and any shorter lit
run is transient noise. That assumption breaks if some other cause produces
a *persistent* multi-LED "on" cluster that's longer than the real,
genuinely-moving scan position's run - e.g. a stuck/failing LED, a
threshold miscalibration that reads a whole region as "on" (bad contrast,
possibly compounded by Issue 1's window-bleed), or backglow/slow decay on a
subset of LEDs. In that scenario the function would silently and
consistently return the wrong, stable index every frame - it never has a
"this doesn't look like a valid single scan position" failure mode, unlike
the `miss` case (`stream_a_last is None`) it already handles when *nothing*
is on.

**Candidate fixes (not yet fleshed out to the same depth as Issues 1-3 -
flagging for discussion, not yet analyzed with alternatives/tradeoffs):**
- Sanity-check the winning run's length against an expected "single LED lit
  at a time" bound (e.g. flag/exclude if the longest run is implausibly
  long relative to `switch_time_ms`/fps, rather than silently accepting
  any length).
- Track run length stability across frames and warn if the same long run
  persists across many consecutive pairs (a real scan position should keep
  moving; a stuck cluster wouldn't).

**Open questions for the user:** has a stuck-LED or persistent-cluster
failure mode actually been observed in practice, or is this purely a
theoretical code-reading concern so far? Is there any existing hardware-
level safeguard (LED panel self-diagnostics, etc.) that would make this
unreachable in practice?

**Sub-finding 5a (found in the 2026-07-30 code-review pass) - the tie-break
between equal-length runs is order-dependent, not evidence-based, which
compounds the main finding above:** ties use strict `>` (`engine/metrics.py`,
the `if length > best_len` checks), so the *first*-listed candidate wins a
tie, and candidate order is always `middle_runs + [wrap]` - meaning a normal
run always beats the wraparound run on an exact-length tie, and among
several disjoint non-adjacent runs of equal length, the lowest-index one
always wins, with no basis (recency, proximity to the previous frame's
position, etc.) for that choice. Verified by hand: `n=10`, boundary runs of
length 1 each (wrap length 2) plus a middle run at indices (4,5) also length
2 - the middle run wins purely because it's listed first, even though both
are equally "valid" by the function's own logic. Practically this needs two
*disjoint, non-adjacent* equal-length on-runs (ordinary adjacent-index LED
transitions already merge into one run via the contiguity check, so this is
rarer than it first looks) - but it directly compounds the main Issue 5
concern: a stuck/noisy cluster doesn't even need to be *longer* than the
real scan run to win, only *tied* with it, and an arbitrary index-order rule
decides the winner instead of any real signal.

---

## Audited, no bugs found (2026-07-30 code-review pass)

Two more functions from the originally-planned broader audit scope were
checked in depth by hand-tracing edge cases (empty/single-element inputs,
boundary values, floating-point precision, numerical stability over many
updates) - no bugs found in either:

- `engine/metrics.py`'s `compute_position_gap` (circular-distance math): the
  `diff > half` vs. `diff <= -half` asymmetry looks suspicious at first
  glance but is actually correct - for even `n`, both `diff == +half` and
  `diff == -half` canonicalize to the same antipodal `+half` output
  (consistent, not a discontinuity); for odd `n`, `half = n/2.0` is never an
  integer, so an integer `diff` can never land exactly on the boundary,
  making the asymmetry moot. Checked at n=2, n=9, n=10 with no wraparound
  bug found at any boundary.
- `domain/running_stats.py`'s `RunningStats` (Welford's online algorithm):
  correct at count=1 (`std` returns 0.0 by its own explicit guard), correct
  with repeated identical values (`_m2` stays exactly `0.0`, not just
  near-zero), correct with mixed-sign values. `std` divides by `count`
  (population, not sample/Bessel-corrected) - a definitional choice, not a
  bug, and the one real caller (`live_session_page.py`) already guards
  `count == 0` before reading `mean`/`std`.

---

## Issue 6 (found in the 2026-07-30 code-review pass, on `main` via merged PR #2): Stream Select's new per-camera config feature has three gaps - one real UX regression, two missing-validation crashes/silent-failures

**Status:** fixed (implemented on `worktree-fix-issue6-stream-config-gaps`, not yet merged into `main`)

This issue covers three related gaps found while reviewing the merged
"replace live device-format picker with per-camera YAML-curated stream
lists" work (PR #2, commits `bf4b687`/`9d7ea6e`/merge `5b44e69`, now on
`main`). All three stem from the same feature and were found together, but
are logged as sub-findings since each needs its own fix decision.

**Sub-finding 6a - "Disable IR emitter" checkbox regression: consolidating
to one global camera-control block dropped the infrared-awareness that
used to gate it, causing a spurious warning on every pure color+color
(Dual RGB) run.** Confirmed directly against current `main`:
`gui/pages/stream_config_page.py`'s `_build_camera_control_group`
(`:205-214`) now shows the "Disable IR emitter" checkbox unconditionally,
always defaulted to checked (`emitter_checkbox.setChecked(True)`) -
previously (`group_camera_controls`'s per-group `has_infrared` flag) it
was only shown/applied when a resolved group actually contained an
infrared stream. `_read_camera_controls` (`:260`) turns "checked" into
`camera_controls["emitter_enabled"] = False` (checked = "please disable
the emitter") unconditionally, and `_apply_camera_controls`
(`gui/pages/roi_select_page.py`) / `engine/session_engine.py`'s inline
duplicate now call `set_emitter_enabled(sensor, False)` on every resolved
sensor in every run, with no gating on whether that sensor is even
infrared-capable. For a pure color+color (Dual RGB) stream pairing - per
`CLAUDE.md` a first-class, explicitly-generalized-for topology of this
app - `set_emitter_enabled` returns `False` (the sensor doesn't support
`rs.option.emitter_enabled` at all) on every single run, surfacing a
"WARNING: emitter_enabled not supported on sensor - confirm the emitter
state manually" message every time, unless the operator manually unchecks
the box each session. Not a crash, but a real, easily-hit UX regression
traded away by the "global controls" simplification. No test exercises
this path (the existing
`test_camera_controls_group_always_present_regardless_of_picks` only
checks the checkbox widget exists, not the warning behavior).

**Decision: Option A chosen** (gate emitter application per-group on
whether it actually contains an infrared stream, keep the checkbox global).

*Candidate fixes:*
- A. **(chosen)** Keep the checkbox global/always-visible (per the user's
  explicit "camera control will be global to both streams" design ask),
  but only *attempt* `set_emitter_enabled` (and thus only ever warn) on a
  resolved group whose real profiles actually include an infrared stream -
  `resolve_and_group`'s returned `(sensor, profiles)` groups already carry
  real `rs.stream_profile` objects with a callable `.stream_type()`, so
  this needs no new data, just an `any(p.stream_type() == rs.stream.infrared
  for p in profiles)` gate inside `_apply_camera_controls`/
  `session_engine.py`'s duplicate, applied identically in both places.
- B. Default the checkbox unchecked instead of checked, so a pure
  color+color run's default behavior is "leave exposure/emitter alone" -
  smaller diff, but changes the default behavior for genuine IR pairs too
  (previously always defaulted to emitter-disabled, which every prior
  version of this app's camera setup treated as the safe default given the
  structured-light projector corrupts LED detection if left on).
- C. Leave as-is, document that the warning is expected/benign for
  color+color pairs and should just be dismissed/ignored. Cheapest, but
  trains operators to ignore warnings on every single run, which risks
  masking a real "emitter left on" problem on an actual IR pair later.

**Fix applied (6a):** `gui/pages/roi_select_page.py`'s
`_apply_camera_controls` and `engine/session_engine.py`'s inline duplicate
both now compute `group_has_infrared = any(p.stream_type() ==
rs.stream.infrared for p in profiles)` per group, and only attempt
`set_emitter_enabled` (thus only ever warn about it) when
`camera_controls["emitter_enabled"] is not None and group_has_infrared`.
`session_engine.py` needed a new `import pyrealsense2 as rs` (didn't
previously import it). The checkbox itself stays global/always-visible/
always-defaulted-checked in `stream_config_page.py`, unchanged - only the
*application* of emitter control is now gated per resolved group, not the
UI. `auto_exposure`/`exposure`/`gain` remain ungated by stream type
(exposure control is meaningful for color sensors too).

**Sub-finding 6b - a malformed `camera.stream_options` entry in
`settings.yaml` crashes the whole app with a raw, unhandled exception
instead of a friendly message.** `gui/main_window.py`'s `_on_device_chosen`
(`:73-96`) wraps the "camera has no entry at all" case in a `QMessageBox`
(`:79-86`), but the very next lines - `per_camera_options["stream_a"]`/
`["stream_b"]` (`:88-89`, raw `KeyError` if either key is misspelled or
missing) and `parse_stream_options_config` (`engine/streams.py`, raises a
bare `ValueError` on an unknown `stream_type`/`format` string, or
`KeyError` on a missing `stream_index`/`width`/etc. key) - have no
surrounding `try/except`. `main.py` installs no custom exception hook, so
an unhandled exception raised inside this Qt slot is fatal in PySide6 (the
whole app aborts) rather than showing the same kind of clean, curated
error the adjacent "no entry" branch already demonstrates is the intended
UX. Given `settings.yaml` is explicitly the one hand-edited file in this
project, a simple typo in a freshly hand-written `stream_options` block
(e.g. `stream_type: colour`, or forgetting `stream_index`) crashes the
whole GUI instead of pointing at the mistake.

*Candidate fix (straightforward, not really a design tradeoff like 6a):*
wrap the `per_camera_options["stream_a"]`/`["stream_b"]` lookups and the
`parse_stream_options_config` calls in a `try/except (KeyError, ValueError)`
inside `_on_device_chosen`, surfacing the same kind of `QMessageBox.critical`
the "no entry" branch already uses, with the actual bad value/missing key
named in the message.

**Fix applied (6b):** exactly the candidate fix - `_on_device_chosen` now
wraps both `per_camera_options["stream_a"]`/`["stream_b"]` and the
`parse_stream_options_config` calls in `try/except (KeyError, ValueError)`,
showing a `QMessageBox.critical` naming the camera, the exception type, and
the exception's own message, instead of letting it crash the app.

**Sub-finding 6c - a curated list with no entries matching what the
connected device actually reports leaves Stream Select silently unusable,
with no error at all.** `_on_device_chosen` never checks whether
`filter_options_by_curated_list(...)` actually returned anything for
either side before calling `populate()`. If a camera's curated `stream_a`/
`stream_b` entries (right device name, but e.g. a different firmware
version reporting a different fps/format set) don't match anything the
live device reports, `StreamConfigPage` ends up with one or both combo
boxes empty. `pick_a`/`pick_b` then return `None` (`combo.currentData()`
on an empty combo), so `_on_next_clicked`/`_on_start_preview_clicked` both
just silently `return` with no status message - the operator is left on a
normal-looking page whose buttons do nothing, with no clue why. More
likely to actually occur now than with the old unfiltered picker (which
could never produce an empty list), since curated entries match on exact
`width`/`height`/`fps`/`format` values.

*Candidate fix (straightforward):* after filtering, if either resulting
list is empty, show a `QMessageBox.critical` (same style as the "no entry"
case) naming which side is empty and that this camera's curated entries
didn't match anything the connected device reports, instead of silently
switching to an unusable Stream Config page.

**Fix applied (6c):** exactly the candidate fix - after filtering,
`_on_device_chosen` now checks whether either resulting list is empty and,
if so, shows a `QMessageBox.critical` naming which side(s) came up empty
and that the camera's curated entries didn't match anything the connected
device reports, instead of silently switching to an unusable Stream Config
page.

**Verification:** all three fixes implemented on
`worktree-fix-issue6-stream-config-gaps`. New test coverage added where
none existed before: `tests/gui/test_main_window.py` (new file) covers
6b (malformed `stream_type`/missing key both surface the friendly error,
not a crash) and 6c (an empty-intersection side surfaces the friendly
error, naming only the actually-empty side; a fully-matching camera still
succeeds and populates both combos); `tests/gui/pages/test_roi_select_page.py`
(new file, `_apply_camera_controls` had zero prior coverage) covers 6a -
a color-only group no longer attempts/warns about emitter control, an
infrared group still does, and a mixed multi-group call gates each group
independently. Full suite green (180 passed).
