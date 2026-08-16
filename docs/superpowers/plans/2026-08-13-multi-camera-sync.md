# Multi-Camera Sync Test — Design & Implementation Plan

## Context

Today the app tests sync between exactly two sensors (Stream A / Stream B) on
one camera, end to end: one wizard flow, one `SessionEngineThread`, one
`TestSession` with two `Metric`s (`PairingGapMetric` = "HW TS Latency",
`PositionGapMetric` = "Optical Sync"), one pair of LED panels at most, one
Live Session page with 2 video panels + 3 charts.

The ask: extend this to a rig of **up to 3 cameras** (up to 6 sensors total),
where:
1. **Each camera keeps running its own existing intra-camera test exactly as
   today** — its own Device Select → Stream Config → ROI Select → Calibration
   → Threshold Tuning flow, its own ROI/threshold/calibration per sensor, its
   own choice of single- or dual-LED-panel mode.
2. **One camera is designated MASTER**, the rest are slaves. RealSense
   hardware genlock (`rs.option.inter_cam_sync_mode` on D400-series, or
   `rs.option.d500_intercam_sync_mode` on D500-series like the "RealSense
   D585 Prototype" already in `settings.yaml`) is configured accordingly. The
   physical sync cable is already wired between all 3 cameras on the real rig.
3. **New cross-camera metrics**: for every slave, for every sensor identity
   (`stream_slug`, e.g. `"infrared1"`, `"color"`) the master and that slave
   both have, measure HW TS Latency between master's and that slave's
   corresponding stream. Cameras may have different sensor setups
   (heterogeneous) — matched by identity, not fixed position; a missing
   identity on either side just means no pair for that identity, no error.
4. A new hub/overview page lets the operator add/configure/edit up to 3
   cameras non-linearly (cards, not a strict forced loop), pick the master,
   then run one multi-camera Live Session.
5. **Hardware constraint confirmed by the user**: all LED panels across all
   cameras share **one relay** (today's exact single-panel-pair wiring,
   unchanged) — not one relay per camera.

Two independent Plan-agent design passes (one generalizing the core
pairwise pipeline to an N-stream shape, one keeping every per-camera
pipeline untouched and adding a thin reconciliation layer) converged on the
same recommendation, detailed below.

## Architecture decision: keep every per-camera pipeline untouched; add a reconciliation layer

**Do not reshape `engine/metrics.py`, `engine/test_session.py`,
`engine/acquisition_loop.py`, `engine/session_engine.py`, or
`engine/streams.py`'s `ContinuousCapture` into an N-stream-aware shape.**
Run one instance of today's exact, unmodified `SessionEngineThread` (one
`ContinuousCapture`, one `TestSession`, one `PairingGapMetric` +
`PositionGapMetric` pair) per configured camera, concurrently. A new,
separate module reconciles their independent row streams into the new
cross-camera metrics.

**Why, concretely (from both design passes):**
- Genlock synchronizes hardware *exposure timing* across devices — it does
  **not** merge 3 independent `rs.pipeline()` objects into one blocking call.
  Any "unified capture loop" is still fundamentally 3 independent per-device
  polling threads underneath, plus a new synchronization/assembly problem
  that doesn't exist today. Calling that "one loop" is bookkeeping, not a
  real simplification.
- The 2-stream pipeline's "pairing" today is a *free side effect* of both
  streams coming off one `rs.pipeline()`'s one `frameset` call. That free
  pairing disappears the instant a stream crosses a device boundary, genlock
  or not — cross-camera alignment has to be actively computed either way, so
  there's no accuracy advantage to reshaping the intra-camera pipeline to
  "solve" a problem it doesn't actually have.
- `engine/session_engine.py`'s camera-control ordering, `engine/streams.py`'s
  IR/RGB open-order depth-sync fix, and `engine/dual_panel_control.py`'s
  arm-sequence/priming logic are this repo's most exhaustively
  real-hardware-validated code (multiple rounds of plausible-but-wrong
  hypotheses each required a fresh hardware round trip to rule out — see
  `docs/algorithm_review_log.md` and `CLAUDE.md`'s dual-panel history).
  Reshaping any of them to be N-device-aware invalidates that validation.
  Keeping them as byte-for-byte-unchanged, independently-instantiated copies
  sidesteps that risk entirely — new code sits *around* proven code, never
  *inside* it.
- Cross-camera reuse of `PairingGapMetric` turns out to be genuinely free
  without reshaping anything: today's `row_ready` dict already carries
  `stream_a_ts_us`/`stream_b_ts_us`/`stream_a_frame_drop`/`stream_b_frame_drop`
  as plain scalars. A synthetic `FramePairSample` built from two matched rows
  (one from master, one from slave) feeds `PairingGapMetric.update()`
  completely unmodified.

## Scope: v1 vs. explicitly-deferred v2

**v1 (this plan):**
- Hub page, up to 3 cameras, non-linear per-camera sub-flow reuse.
- Master/slave genlock role assignment.
- Cross-camera **HW TS Latency only** (`PairingGapMetric`), every matching
  sensor identity, master vs. each slave.
- Each camera's own intra-camera test (HW TS Latency + Optical Sync)
  unchanged.
- At most **one** configured camera may use dual-panel mode per run (the
  confirmed single shared relay physically cannot gate two cameras' panel
  pairs independently — see Risks).
- New combined multi-camera Live Session UI (per-camera tabs + one
  cross-camera panel), new output file layout.

**Explicitly deferred to v2 (flag in the plan, do not build now):**
- Cross-camera **Optical Sync** (`PositionGapMetric`). Needs per-LED
  brightness data threaded through `row_ready` (not free — a real, small,
  additive change to `session_engine.py`'s `on_row` callback), *and* only
  means anything if master and slave are viewing the **same physical LED
  panel with the same LED-index convention** — an operator-declared
  assumption, not something inferable from calibration. Worth revisiting
  once the shared-relay dual-panel finding below is validated on real
  hardware, since dual-panel cameras sharing one relay are already
  inherently lockstep-scanning, which may make this cheaper than it looks
  today.
- Per-camera-independent switch time / frame sample interval controls during
  a multi-camera run (today's Live Session locks these per run; multi-camera
  raises new questions about whether slaves must match master's panel
  timing — not resolved here, flag as follow-up).
- `gui_state.json` schema migration to multi-camera (each camera's live
  config stays in-memory in `MainWindow` for v1, same "lossy prefill, not
  source of truth" convention already used).

## Design detail

### 1. New module: `engine/cross_camera_reconciler.py` (pure Python, no Qt/hardware, fully unit-testable with fakes)

```python
@dataclass
class CrossCameraPairSpec:
    master_camera_id: str
    slave_camera_id: str
    stream_identity: str        # e.g. "infrared1" — from engine.streams.stream_slug
    master_row_role: str        # "stream_a" or "stream_b" on the MASTER's own row
    slave_row_role: str         # "stream_a" or "stream_b" on the SLAVE's own row
    pairing_gap_metric: PairingGapMetric   # one instance per pair, own outlier threshold

class CrossCameraReconciler:
    def __init__(self, pair_specs: list[CrossCameraPairSpec],
                 buffer_seconds: float = 1.0, max_match_gap_us: float = 50_000):
        ...
    def ingest_row(self, camera_id: str, row: dict) -> list[dict]:
        """Called for every row_ready from every camera (master and slaves
        alike — buffering is symmetric, either side's row can arrive first
        since the two AcquisitionLoops run on independent threads at
        independent cadences). Buffers into a small deque per
        (camera_id, stream_identity), sized from expected fps * buffer_seconds
        (e.g. 30fps*1s = 30 entries). For every pair_spec this camera
        participates in, finds the counterpart buffer's nearest-in-time row;
        if within max_match_gap_us, builds a synthetic FramePairSample from
        the two rows' ts_us/frame_drop fields, calls
        pair_spec.pairing_gap_metric.update(sample), pops the matched
        counterpart entry (one master frame is never reused for two
        different slave frames), and returns the resulting cross-row(s).
        A slave row with no master row within max_match_gap_us produces NO
        cross-row - explicit exclusion, not a forced/misleading match, matching
        this project's existing convention (outlier thresholds, frame-drop
        flags, warmup exclusion) of "never silently drop, always flag or omit
        with a reason."""
```

Reuses `engine.streams.stream_slug` for identity matching (the same scheme
`config.yaml` already keys LED calibration by), so heterogeneous per-camera
sensor sets are handled for free.

### 2. New orchestration module: `engine/multi_camera_session.py`

```python
class MultiCameraSessionController(QObject):
    camera_frame_ready = Signal(str, str, object, int, object)  # camera_id, stream_name, image, pair_index, mask
    camera_row_ready = Signal(str, dict)                         # camera_id, row (unchanged shape)
    camera_stats_ready = Signal(str, dict)
    camera_session_finished = Signal(str, list)
    camera_error = Signal(str, str)
    cross_pair_ready = Signal(dict)      # unthrottled, mirrors row_ready cadence
    cross_stats_ready = Signal(dict)     # throttled, mirrors stats_ready cadence
    all_sessions_finished = Signal(dict) # camera_id -> rows

    def start_all(self): ...
    def stop_all(self): ...
```

Lives on the GUI thread as a plain `QObject`, not a `QThread` — owns N
`SessionEngineThread` instances built via **exactly the same constructor
call** `LiveSessionPage.start_session()` makes today (unmodified), fans each
one's signals through to that camera's own UI section untouched, and into
one shared `CrossCameraReconciler`.

**Startup ordering** (sequential, on the GUI thread, before starting any of
the N threads):
1. If any camera has `hardware_reset_before_start` set, reset it and wait for
   re-enumeration *first* — a hardware reset drops the device off USB and
   plausibly clears any previously-set `inter_cam_sync_mode`, so it must
   happen before role assignment, not inside that camera's own thread later
   (where it could race/undo the just-applied role).
2. Assign genlock roles for all N devices via the new
   `engine.streams.set_inter_cam_sync_mode(device, mode)` helper (see below),
   master first by convention. Abort with a clear error (not a silent
   partial run) if any device fails to apply its role.
3. Start all N `SessionEngineThread`s.

### 3. New camera-control helper: `engine/streams.py`

```python
INTER_CAM_SYNC_DEFAULT = 0
INTER_CAM_SYNC_MASTER = 1
INTER_CAM_SYNC_SLAVE = 2

def set_inter_cam_sync_mode(device, mode) -> bool:
    """Applies inter_cam_sync_mode (D400-series) or d500_intercam_sync_mode
    (D500-series, e.g. "RealSense D585 Prototype") to whichever sensor on
    `device` supports it - NOT assumed to be a fixed sensor, iterates
    device.query_sensors() and checks .supports(), same convention as
    set_emitter_enabled. Returns False (never raises) if unsupported, so the
    caller can warn rather than silently proceed unsynced."""
```

**Implemented and confirmed via direct pyrealsense2 2.58.3 introspection
(not yet real hardware) — corrects this plan's original assumption:** there
is only **one** option, `rs.option.inter_cam_sync_mode` — no separate
D500-series option. What differs per camera generation is the **value
scheme**: D400-series uses `0=default/1=master/2=slave`;
D500-series uses `rs.d500_intercam_sync_mode`'s own enum
(`none=0/rgb_master=1/pwm_master=2/external_master=3`) on that *same*
option. `set_inter_cam_sync_mode(device, mode)` is generation-agnostic by
design — it just writes whatever raw `mode` value it's given to whichever
sensor supports the option; picking the correct value per camera
model/generation is the caller's (orchestration layer's) job, done once real
multi-camera hardware is available. Which sensor actually carries the option
(believed depth/stereo, not color, per public docs) is still **not yet
verified on real hardware** — flagged as a required verification step before
this is load-bearing (see Risks).

### 4. GUI: new hub page + reused per-camera sub-flow

- **New `gui/pages/camera_hub_page.py`** — `CameraHubPage`: one card per
  configured camera (name, role badge, configured/needs-setup state), "Add
  Camera", "Edit" (re-enters the *existing, unmodified* Device Select →
  Stream Config → ROI Select → Calibration → Threshold Tuning flow for just
  that `camera_id`), "Set as Master" (exactly one at a time), "Start
  Multi-Camera Live Session".
- **`gui/main_window.py`** changes from owning one `self._pick_a`/`_pick_b`/
  `_camera_controls` to owning `self._cameras: dict[camera_id,
  CameraConfigState]` + `self._master_camera_id` + `self._editing_camera_id`.
  The existing page *instances* (`device_page`, `stream_config_page`,
  `roi_page`, `calibration_page`, `threshold_tuning_page`) stay single,
  shared instances re-entered per camera exactly as today — routed back to
  the hub at the end of each camera's flow instead of straight into Live
  Session. **No changes to any of those 5 page classes' internals.**
- **New `gui/widgets/camera_live_session_panel.py`** — `CameraLiveSessionPanel`:
  today's `LiveSessionPage` video-row + 3-plots + `StatsPanel` block,
  extracted into a reusable widget parameterized by `camera_id`/labels,
  logic ported near-verbatim from `_on_frame_ready`/`_on_row_ready`/
  `_on_stats_ready`.
- **New `gui/pages/multi_camera_live_session_page.py`**: a `QTabWidget`, one
  `CameraLiveSessionPanel` tab per camera (each camera's intra-camera view
  unchanged from today), plus one always-visible cross-camera section (one
  `LivePlot` with one `add_series` per matched identity pair — reusing the
  existing multi-series/checkbox-toggle pattern the frame-drops plot already
  has — and one `StatsPanel` group per pair).

### 5. Output files

Per-camera: call `domain.csv_export.export_session_csvs` and
`domain.plot_export.export_session_plot` **unmodified**, once per camera,
into a per-camera subfolder:

```
output/live_session_<timestamp>/
  camera_<camera_id>_<label>/{pipeline_sync_raw.csv, pipeline_sync_frame_drops.csv, pipeline_sync_plot.png, ...}   # byte-for-byte today's shape
  cross_camera_sync.csv
  cross_camera_sync_plot.png
```

New, purely additive functions (no existing signature changes):
`domain.csv_export.export_cross_camera_csv(cross_rows, path)`,
`domain.plot_export.export_cross_camera_plot(cross_rows, path)` — one line
per `(slave_camera_id, stream_identity)` pair, `domain.plot_theme` palette
extended with a couple more colors for up to 4 pairs (1 master × up to 2
slaves × up to 2 shared identities). Small additive helper in
`domain/run_output.py` for the per-camera subfolder, `create_run_dir` itself
unchanged.

### 6. Dual-panel: generalize port config, keep the sequencing mechanism unchanged

Confirmed: all panels across all cameras share **one relay** (today's exact
wiring). This means:
- `engine/dual_panel_control.py`'s port config (`stream_a_panel_port`/
  `stream_b_panel_port`) needs to generalize from 2 fixed names to a
  `(camera_id, stream_identity) -> port` mapping, since up to 6 panels can
  now exist on the one shared Acroname hub.
- The **hub-switch-and-configure loop** (`_run_on_both_panels`) generalizes
  from "exactly 2 named ports" to "loop over however many ports are actually
  configured for this run" — a bounded, mechanical change to *what gets
  iterated*, not a rewrite of *how* each one is configured or how the relay
  is gated.
- The **relay-as-gate mechanism itself** (`_relay_on`/`_relay_off`, the
  keepalive thread, the double-arm-on-first-arm-since-calibration priming,
  `reset()` not `stop()`) stays completely unchanged — it doesn't care how
  many panels are wired to it, only that it's closed or open.
- **v1 constraint**: because it's genuinely one shared relay, two cameras
  cannot independently run dual-panel mode at different times within the
  same relay gate without interfering with each other's scan position — the
  orchestrator (`MultiCameraSessionController`) enforces "at most one
  configured camera uses dual-panel mode per multi-camera run" for v1,
  rather than attempting to prove a more permissive scheme is safe without
  real-hardware validation.
- Any change to the generalized port-loop must be validated the way this
  project's own dual-panel history insists on: an automated real-hardware
  diagnostic sweep (following the existing `tools/dual_panel_diag/` pattern)
  before trusting it, not code-review confidence alone.

## Known risks / required real-hardware validation (do not treat as settled)

1. **Which sensor carries `rs.option.inter_cam_sync_mode`** (believed to be
   the depth/stereo sensor, not color — unconfirmed on this project's real
   rig/firmware for either the D455 or the D585 Prototype), and **whether
   D500-series' `rs.d500_intercam_sync_mode` value scheme
   (`none=0/rgb_master=1/pwm_master=2/external_master=3`, confirmed via SDK
   introspection) is what this specific D585 Prototype firmware actually
   expects** — verify empirically on the real rig before hardening which
   raw value the orchestration layer passes per camera model.
2. **Whether genlock covers RGB timing or only depth/IR** — this project
   already has its own single-camera IR/RGB sync fix
   (`enable_depth_for_ir_sync`); it's plausible (unconfirmed) that each slave
   device may still need depth co-enabled for genuine cross-camera RGB
   alignment, not just as a bandwidth trade-off like today.
3. **Whether the per-frame HW-timestamp metadata this app already reads is
   comparable *across* two genlocked devices**, vs. only validated so far
   *within* one device's two sensors. Needs a dedicated real-hardware
   measurement pass, the same way today's single-camera ~3.5ms/~11.3ms
   offsets were originally discovered — don't assume cross-camera
   `pairing_gap_us` is trustworthy until measured.
4. **USB bandwidth** — up to 6 sensors, each camera potentially co-enabling
   depth-for-IR-sync; three cameras could approach several hundred MB/s
   aggregate. Confirm independent host controllers per camera; be ready to
   disable `enable_depth_for_ir_sync` on some cameras under bandwidth
   pressure.
5. **Thread/GIL contention** — 3 concurrent `QThread`s each doing per-pair
   numpy brightness sampling; measure per-camera frame-drop rate with all
   three running concurrently before assuming pure reuse is free.
6. **✅ Confirmed on real hardware and fixed**: two cameras sharing a USB
   hub/controller (e.g. an Acroname hub) can disrupt each other's device
   enumeration if their `rs.pipeline().start()` calls happen at nearly the
   same moment — starting both camera threads back-to-back with zero delay
   reproduced this on the real rig every time (`resolve_and_group: no
   matching profile found... after a reconnect` on the second camera).
   Fixed with `MultiCameraSessionController`'s `camera_start_stagger_s`
   (default 2.0s, applied before starting every camera after the first) —
   a real-hardware-tunable guess, not a proven-correct value; keep raising
   it if collisions are still observed with 3 cameras. The LED-panel
   version of this exact same mechanism (camera vs. LED panel sharing a
   hub) is documented separately in `CLAUDE.md`.

## Critical files

New:
- `engine/cross_camera_reconciler.py`
- `engine/multi_camera_session.py`
- `gui/pages/camera_hub_page.py`
- `gui/widgets/camera_live_session_panel.py`
- `gui/pages/multi_camera_live_session_page.py`

Modified (additive only, no reshaping of existing behavior):
- `engine/streams.py` — add `set_inter_cam_sync_mode`
- `gui/main_window.py` — per-camera state dict instead of single
  pick_a/pick_b/camera_controls; routing to/from the new hub page
- `domain/csv_export.py`, `domain/plot_export.py` — add
  `export_cross_camera_csv`/`export_cross_camera_plot`
- `domain/run_output.py` — add per-camera subfolder helper
- `engine/dual_panel_control.py` — generalize port config from 2 fixed names
  to a `(camera_id, stream_identity) -> port` mapping; generalize the
  hub-switch loop's iteration count; relay/priming mechanism itself
  unchanged

Explicitly unmodified (verified by tracing consumers in both design passes):
`engine/metrics.py`, `engine/test_session.py`, `engine/acquisition_loop.py`,
`engine/session_engine.py`, `engine/streams.py`'s `resolve_and_group`/
`ContinuousCapture`/camera-control setters, `engine/led_panel.py`,
`engine/acroname_hub.py`, all 5 existing per-camera wizard page classes,
`domain/calibration.py`, `domain/realsense_utils.py`.

## Suggested implementation order

**Status as of this writing: steps 1-6 implemented and committed on
`worktree-multi-camera-sync` (pushed to origin), plus genlock wiring (the
v1 simplification step 5 originally deferred) now landed on top - 536 tests
pass (424 baseline + 112 new). Step 7 remains, genuinely blocked without
the physical rig.**

1. ✅ `set_inter_cam_sync_mode` + unit tests against a fake sensor. Writing
   these tests caught a wrong assumption in this plan's own original
   draft — see the corrected "Design detail" section 3 above (one option,
   not two; D500-series uses a different VALUE scheme on the same option).
2. ✅ `CrossCameraReconciler` + `build_cross_camera_pair_specs` + unit tests
   with fake rows.
3. ✅ `MultiCameraSessionController`, wiring N (fake, in tests) real-shaped
   `SessionEngineThread`s to the reconciler. Also added a guard this plan
   called for but the first implementation pass missed: `start_all()` now
   rejects (raises, starts nothing) more than one camera configured for
   dual-panel mode — caught and fixed in the same session, see "Design
   detail" section 6.
4. ✅ `CameraHubPage` + `MainWindow` per-camera state refactor. Hub sits in
   front of every run, including a single camera (a decision made during
   implementation, not originally pinned down in this doc's first draft —
   see the earlier "hub scope" question/answer in the conversation this
   plan came from). `_on_tuning_done` now commits into `self._cameras`
   and returns to the hub instead of populating `LiveSessionPage`.
5. ✅ Multi-camera Live Session page — `CameraLiveSessionPanel` extracted
   from `LiveSessionPage` (untouched, still fully covered by its own
   tests), `MultiCameraLiveSessionPage` (tabs + cross-camera panel) built
   and wired for real into the hub's "Start Multi-Camera Live Session".
   Two v1 simplifications landed here, since superseded (see below):
   (a) genlock role assignment was NOT attempted yet — every
   `CameraSessionSpec.inter_cam_sync_value` was `None`; (b) each camera
   still minted its own independent output folder rather than the nicer
   shared-parent-plus-subfolders layout step 6 describes.
6. ✅ Output file changes. `domain/run_output.py`'s `create_camera_subdir`
   + `domain/csv_export.py`'s `export_cross_camera_csv` +
   `domain/plot_export.py`'s `export_cross_camera_plot` +
   `domain/plot_theme.py`'s `CROSS_CAMERA_COLORS` (canonical palette,
   shared by the live plot and the static export). `CameraLiveSessionPanel.
   prepare_for_run` changed to take an already-decided `output_dir`
   (contained entirely within this feature's own new code, never released
   elsewhere) instead of minting its own — the orchestrating page now owns
   one shared run folder + one subfolder per camera end to end, plus a
   combined `cross_camera_sync.csv`/`cross_camera_sync_plot.png` written
   once every camera's session finishes.
6.5. ✅ **Genlock wiring** (real hardware confirmed connected via the sync
   cable between two D455s, per the operator - not yet confirmed to
   actually genlock, see below). `engine/streams.py` gained
   `resolve_inter_cam_sync_value(inter_cam_sync_settings, camera_name,
   is_master)` — looks up the raw per-camera-model master/slave value from
   a new `settings.yaml` `camera.inter_cam_sync` section (keyed by exact
   device name, same convention as `camera.stream_options`), returning
   `None` (skip genlock) for any camera model with no entry rather than
   guessing. `gui/main_window.py`'s `_on_start_multi_camera_session_requested`
   now resolves this fresh at Start-time (using whichever camera is
   CURRENTLY master) and embeds it into each camera's own config dict as
   `inter_cam_sync_value`; `gui/pages/multi_camera_live_session_page.py`'s
   `start_all_sessions()` now reads `config.get("inter_cam_sync_value")`
   instead of hardcoding `None`. `MultiCameraSessionController.start_all`'s
   own role-assignment loop (built in step 3, previously dormant since
   every value was `None`) now actually applies genlock roles to real
   devices whenever a camera resolves to a non-`None` value.
7. ⬜ Dual-panel port generalization — last, and only with real-hardware
   diagnostic-sweep validation before trusting it, per the project's own
   established practice. Genuinely blocked without the physical rig.

**What this means practically right now:** the app is fully runnable
end-to-end for a multi-camera sync test (up to 3 cameras, software-side
cross-camera HW TS Latency reconciliation, at-most-one-dual-panel-camera
enforced, one organized run folder per multi-camera session with a
combined cross-camera CSV/plot), and genlock role assignment is now wired
for camera models with a configured `camera.inter_cam_sync` entry (only
"RealSense D455" so far, master=1/slave=2 - a D400-series guess consistent
with the SDK's documented scheme, not yet confirmed on this project's own
rig). **Still needs real-hardware validation** per this doc's own "Known
risks" section above (items 1-3): does applying these values actually
produce a shared clock between the two D455s (does cross-camera
`pairing_gap_us` come out small and stable, not the ~5.1-minute offset
measured before this fix), and does the assumed depth/stereo sensor
actually carry the option on this hardware. That validation is the
immediate next step, on the operator's real rig.

## Verification

- Unit tests for every new pure-Python piece (`set_inter_cam_sync_mode`
  against a fake sensor, `CrossCameraReconciler` against fake row streams
  covering: exact-time match, no-match-within-window exclusion, symmetric
  master/slave arrival order, heterogeneous sensor identities) — run via
  `.venv\Scripts\python.exe -m pytest -v`, same convention as the existing
  424 tests.
- No existing test should need to change (nothing existing is reshaped).
- Real-hardware verification, in order: (a) 2-camera master/slave genlock
  role assignment doesn't crash/warn unexpectedly on both a D455 and the
  D585 Prototype; (b) cross-camera HW TS Latency numbers are stable and
  sane run-to-run (the same "measure before trusting" discipline this
  project already applies to every other timing claim); (c) 3-camera
  concurrent run with no dual-panel cameras, checking frame-drop rates
  per camera; (d) add one dual-panel camera into a 3-camera run last, with
  a diagnostic-sweep-style validation before relying on it.

## Superseded by

A follow-up conversation (same day) asked for a UX redesign of steps 4-5's
wizard flow (multi-select all cameras up front, capture all "LEDs on" ROI
photos together, per-sensor ROI selection across all cameras on one page,
batched calibration, batched threshold tuning) - see the newer design doc
this prompted, once written, for what actually superseded the "Add Camera
one at a time, non-linear hub" flow described in section 4 above. The
underlying engine-layer architecture (sections 1-3, 5-6) is expected to
stay valid; only the GUI wizard flow around configuring N cameras changes.
