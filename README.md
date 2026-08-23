# Optical Sync GUI

A PySide6 desktop app for measuring timing sync between video streams from
RealSense cameras against an LED panel used as a known, precise timing
reference. A **Camera Hub** lets you configure up to 3 physical cameras
(each through its own guided sub-flow: pick the device, pick a named test
and camera controls, draw the ROI, calibrate against the LED panel, tune
detection thresholds against a live preview), then Start routes to whichever
kind of live test actually applies: a **single-camera test** measuring sync
between that one camera's own two streams, or, once 2+ cameras are
configured, a **multi-camera test** that additionally reconciles timing
*across* the separate physical cameras.

## Objective

This app measures how well two chosen video streams stay synchronized in
time, using a switchable LED panel as the reference signal.

The LED panel switches through its LEDs one at a time, in a fixed
left-to-right, top-to-bottom order, at a known, fixed switch speed (as fast
as 0.1ms per LED - deliberately faster than either stream's own frame
interval). Both streams being compared watch the panel while their camera
continuously captures frames from each.

Because auto-exposure is normally enabled, each stream's own exposure
window length is independent and can span multiple LED-switch intervals -
so the two sides can legitimately "see" a different number of LEDs having
turned on during their own exposure. What has to match, if the two streams
are actually synchronized, is the LAST LED index each frame caught,
regardless of how many earlier ones its own exposure window happened to
also catch. The app detects that last-active LED position per frame, per
stream (LED calibration and threshold tuning are what make this detection
reliable), and calculates the delta between the two last-detected indices.
Combined with the panel's own known switch speed, that delta converts
directly into a synchronization delay - this is the app's **"Optical
Sync"** metric (`position_gap_ms` internally).

Independently, the app also compares the two streams' own hardware frame
timestamps directly - the **"HW TS Latency"** metric (`pairing_gap_us`
internally). Together, the two metrics answer two different questions: do
the two streams' own clocks agree with each other, and does what the two
streams actually captured, optically, agree - these can diverge if there's
a clock skew that isn't reflected in real capture timing, or vice versa. A
third live measurement, frame drops per stream, flags any pair where one
side dropped or repeated a frame, since that invalidates both of the above
measurements for that pair.

Multi-camera tests (below) add a fourth metric, **"Global TS Latency"**
(`global_ts_gap_us`), for the specific problem of comparing timestamps
across two entirely separate physical cameras.

## Test types

The app supports two structurally different kinds of test, both reachable
from the same Camera Hub.

### Single-camera tests

Configure exactly one camera in the Camera Hub and click Start: the app
routes straight to a single-camera Live Session measuring sync between
that camera's own Stream A and Stream B - no cross-camera concept involved.
Which two streams you compare determines the LED-panel setup needed:

- **Same-sensor-type pairs** (e.g. "IR1 vs IR2 sync" on the stereo module,
  or "Color vs Color2 sync" on a Dual-RGB device) - both streams see the
  same wavelength band, so one LED panel, visible equally to both, is
  enough.
- **Cross-sensor-type pairs** (e.g. "IR vs RGB sync") - an infrared sensor
  typically has a built-in filter blocking visible light, so it can't
  reliably see a panel lit for the RGB sensor's benefit, and vice versa -
  one shared panel doesn't work here. Stream Config's opt-in "Use dual LED
  panel" checkbox switches this camera's whole flow to driving two
  independent panels, each stepping through the same synchronized sequence
  off one shared trigger relay, one panel positioned for each stream (see
  `settings.yaml`'s `dual_panel:` section for the wiring it expects).
  Because two independent panels (not one shared one) are used, a small
  drift accumulates between them over a long enough run -
  `tools/panel_drift/` has the scripts used to measure this on a real rig;
  keep individual runs short enough that accumulated drift stays well below
  whatever sync-accuracy threshold the test is measuring.

### Multi-camera tests

Configure 2 or more cameras in the Camera Hub, designate exactly one as
**Master**, and Start routes to the Multi-Camera Live Session instead. This
measures sync *between separate physical cameras*, not just within one:
each configured camera still runs its own ordinary single-camera capture
(its own Stream A/B, its own LED panel setup per the rules above), but the
app additionally cross-matches frames between the Master and each Slave
camera using RealSense's GLOBAL_TIME-domain timestamp (directly comparable
across devices, unlike each device's own arbitrary, drifting hardware-clock
epoch), and reports HW TS Latency, Global TS Latency, and Optical Sync
*per Master/Slave pair*. Where the connected camera model has a
real-hardware-confirmed genlock scheme (`settings.yaml`'s
`camera.inter_cam_sync`), the app also applies hardware genlock
(`rs.option.inter_cam_sync_mode`) automatically, master first, before
starting capture - camera models with no confirmed entry there simply skip
genlock rather than guess a value that hasn't been validated on real
hardware. At most one configured camera may use the dual-LED-panel mode at
a time, since it depends on a single shared relay/hub connection.

## Features

- **Camera Hub** — the app's home screen. Lists every configured camera as
  a card (device model, a `[MASTER]`/`(needs setup)` badge, Edit/Set as
  Master/Remove buttons), an "Add Camera" button (disabled past 3
  configured cameras), and a "Start Multi-Camera Live Session" button
  enabled once at least one camera is fully configured and exactly one is
  Master. Add walks a brand-new camera through the full sub-flow below;
  Edit jumps straight to that camera's Stream Config, prefilled with its
  previous choices, skipping Device Select entirely since the device is
  already known.
- **Device selection** — lists every connected RealSense device not
  already configured on another Hub card (no Stereo-Module/RGB-Camera
  requirement — `engine.streams.list_devices` has no PID/sensor
  restriction). A D535/D585-family device shows a purely informational "-
  Dual RGB"/"- Dedicated RGB" suffix on its entry; actually switching that
  mode happens one page later, on Stream Config.
- **Stream configuration** — a **Test** picker listing the named tests
  configured for this camera model in `settings.yaml`'s
  `camera.stream_options` (e.g. "IR1 vs IR2 sync", "IR vs RGB sync" - each
  test fixes which two physical streams it compares), and a **Sensor
  Options** picker for a resolution/fps/format pairing under that test,
  intersected with whatever the connected device actually reports. For a
  D535/D585-family device, an "RGB Mode" choice (Dual RGB / Dedicated RGB)
  also lives here, defaulted to the device's current mode - picking the
  other one and clicking Next switches it (a few seconds, device
  re-enumerates) and refreshes the Test/Sensor Options lists against the
  new mode's capabilities before letting you proceed. The "Use dual LED
  panel" checkbox (see [Test types](#test-types) above) lives here too,
  since panel needs depend on which Test is picked, not on the camera
  alone. Below all of that, ONE global "Camera Controls" group applied to
  both streams together regardless of how they resolve to physical sensors
  at capture time: an IR-emitter-disable checkbox (a no-op with a surfaced
  warning if neither stream is infrared), a shared auto/manual exposure
  MODE choice, and two independent exposure spinboxes (Exposure A /
  Exposure B) since different sensors have different brightness
  characteristics - there is no gain control at all; Auto mode always lets
  the camera's own continuous AE algorithm drive gain. A **live
  pairing-quality preview** follows: a bundle counter, per-stream HW frame
  number, HW timestamps, and their delta are burned onto Stream A's video
  and printed to the console, so you can sanity check pairing before
  committing to a combo.
- **ROI selection** — capture one frame with all LEDs lit, then drag a box
  on each stream via a native OpenCV popup window.
- **LED calibration** — detects every LED's pixel position and per-LED
  on/off/threshold values, with live progress logging and saved debug
  images (masked frame + detected LEDs circled) so you can see exactly what
  went wrong if detection fails.
- **Threshold tuning** — a live video preview of both streams with the same
  green/red on/off detection overlay the live session uses, so you can
  retune detection before committing to a live test. Each stream has its
  own independently-editable Detection Threshold slider (with a live
  "Detected: N / num_leds" count, or "Reset to Auto" to go back to
  Calibration's own value) and Threshold Fraction spinbox, plus a shared
  LED Switch Time spinbox - all live-editable while the preview runs, with
  no thread restart needed to see a change take effect. "Continue" commits
  this camera into the Camera Hub and returns to it.
- **Single-camera Live Session** — reached automatically when exactly one
  camera is configured. Dual video panels for Stream A/Stream B (each
  showing a live LED on/off detection overlay), a live scrolling plot of
  HW TS Latency and Optical Sync, a second live plot of per-pair frame
  drops, a live stats sidebar with a min/avg/std/max table, Start/Stop with
  an optional fixed duration, per-run editable LED switch time and frame
  sample interval, a "Save Debug Snapshot" button, and CSVs plus a summary
  plot image written at the end.
- **Multi-Camera Live Session** — reached once 2+ cameras are configured
  with one Master. One "Cross-Camera Sync" tab per Master/Slave pair (three
  live plots - HW TS Latency, Global TS Latency, Optical Sync - plus a
  stats panel), alongside one ordinary single-camera-style tab per
  configured camera. One shared toolbar (duration, LED switch time with a
  Confirm gate, frame sample interval, Start All/Stop All) drives every
  camera's capture together, staggering each camera's own start slightly to
  avoid a real-hardware USB-enumeration collision. Output is one shared,
  timestamped run folder with a subfolder per camera plus a combined
  cross-camera CSV and per-Slave summary plot.

## Prerequisites

- **Windows**, Python 3.10+ (developed against 3.13).
- **RealSense SDK/drivers installed** — not just the `pyrealsense2` pip
  package. Install the RealSense Viewer or SDK installer so Windows
  recognizes the camera at the OS level.
- **RealSense per-frame metadata enabled.** Windows sometimes disables this
  by default; if you see `RuntimeError: This camera/driver does not expose
  per-frame HW timestamp metadata...`, this needs a one-time enablement step
  at the OS/driver level (see librealsense's docs on Windows metadata
  support), then reconnect the camera.
- **`LED-Panel.exe` on PATH** (or in the folder you launch the app from) —
  the CLI for the LED panel.
- A connected RealSense camera and LED panel for anything past the Camera
  Hub's Add Camera step.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## Verify the install

This runs without any hardware connected — a good first check after
cloning or moving to a new machine:

```powershell
.venv\Scripts\python.exe -m pytest -v
```

All tests under `domain/`, the pure-logic parts of `engine/`, `state/`, and
the GUI widgets should pass (hardware-facing code has no automated tests by
design — see [Architecture & project structure](#architecture--project-structure)).

## Running the app

```powershell
.venv\Scripts\python.exe main.py
```

The window opens maximized on the **Camera Hub**, empty at first launch.

### Adding a camera

Click **Add Camera** to walk through that camera's own sub-flow:

1. **Device Select** — pick a connected RealSense device not already
   configured on another Hub card, then click **Next**.
2. **Stream Config** — pick a named test (defined per camera model in
   `settings.yaml`'s `camera.stream_options`) and a sensor-options
   resolution/fps/format pairing for it. If the device is a D535/D585-
   family device, an "RGB Mode" choice appears here - picking the other
   mode and clicking **Next** switches it (a few seconds) and refreshes
   this page's Test/Sensor Options against the new mode's capabilities
   rather than proceeding immediately; click **Next** again once you're
   happy with the refreshed list. If this camera/test needs two physically
   separate LED panels (see [Test types](#test-types) above), check "Use
   dual LED panel" here. Set the global camera controls (IR emitter
   disable, a shared auto/manual exposure mode, and independent Exposure
   A/Exposure B values for Manual - no gain control). Optionally click
   **Start Preview** to check pairing quality live before clicking **Next**
   (this stops the preview automatically).
3. **ROI Select** — click **Capture & Select ROI**; a popup shows a frame
   with all LEDs lit. Drag a box around the panel on Stream A's window,
   press Enter, then repeat for Stream B's window.
4. **Calibration** — click **Run Calibration** and watch the log. On
   failure, check the debug images named after each stream's own slug (e.g.
   `output/calibration_<timestamp>/debug_infrared1_detection.png`,
   `output/calibration_<timestamp>/debug_color_detection.png` — one fresh
   timestamped subfolder per time you arrive at this page, shared by however
   many times you click Run Calibration in that one visit) — these show the
   exact masked region and whatever was detected, even when zero LEDs were
   found. With "Use dual LED panel" checked, each stream is fully calibrated
   one at a time (its own panel on, capture, off, capture) rather than both
   panels together, so the log will show this happening sequentially per
   stream.
5. **Threshold Tuning** — if Calibration's automatic detection didn't go
   well (wrong LED count, missed/merged blobs — check the debug images from
   step 4), each stream has its own **LED Detection Threshold Tuning**
   section above the Threshold Fraction control: drag the Detection
   Threshold slider while watching the small preview and the "Detected: N /
   num_leds" count update live, or click **Reset to Auto** to go back to
   Calibration's own value. No new capture needed - this reuses the same
   on/off frames Calibration already took. Otherwise, click **Start** for a
   live video preview of both streams, each with the same on/off overlay
   the live session uses (tune the Frame Sample Interval first if you want
   it slower/faster - locked once running). Drag each stream's own
   Threshold Fraction (and LED Switch Time, shared by both) while watching
   which LEDs are classified "on" right now, click **Stop** any time, then
   click **Continue** once you're happy with both streams' classification
   (this stops the preview automatically if it's still running, saves
   whatever the current LED positions are — retuned or original — back to
   `config.yaml`, commits this camera into the Camera Hub, and returns to
   it).

Editing an already-configured camera (**Edit** on its Hub card) jumps
straight to step 2, prefilled with everything it was last configured with -
the device is already known, so there's no reason to repeat step 1.

### Running a test

With at least one camera fully configured and exactly one designated
**Master** (the first camera you add is made Master automatically; use
**Set as Master** on any other card to change it), click **Start
Multi-Camera Live Session**:

- **Exactly one configured camera** — routes straight to a single-camera
  **Live Session**: set an optional duration (0 = manual stop), tune the
  LED switch time and frame sample interval if needed (locked once a
  session is running), click **Start**, watch both video feeds, the live
  plots (toggle either metric series on/off), and the frame-drops plot.
  Click **Save Debug Snapshot** any time to check the LED on/off
  classification against the live video. Click **Stop** (or let the
  duration elapse) to write the CSVs, a summary plot image, PNGs of the
  live charts, and a final debug snapshot, all under a fresh
  `output/live_session_<timestamp>/` folder minted by that Start click —
  an earlier run's files are never overwritten.
- **Two or more configured cameras** — routes to the **Multi-Camera Live
  Session**: the same per-camera Start/Stop/duration/switch-time/sample-
  interval controls now apply to every camera together (**Start All**/
  **Stop All**), each camera's own tab looking exactly like the
  single-camera Live Session above, plus a "Cross-Camera Sync" tab per
  Master/Slave pair showing HW TS Latency, Global TS Latency, and Optical
  Sync live. Stopping writes one shared timestamped run folder containing
  each camera's own CSVs/plots/snapshots in its own subfolder, plus a
  combined cross-camera CSV and per-Slave summary plot at the top level.

## Configuration files

- **`settings.yaml`** — the one file meant to be hand-edited between runs:
  per-camera-model named tests and their resolution/fps/format options
  (`camera.stream_options`), default stream A/B resolution/fps
  (`camera.stream_a`/`camera.stream_b`, pre-selected in the Stream Config
  dropdowns, not enforced), real-hardware-confirmed genlock values per
  camera model for multi-camera runs (`camera.inter_cam_sync` - a model
  with no entry here safely skips genlock rather than guessing an
  unconfirmed value, and `max_slave_color_resolution` caps how large a
  genlock Slave's own color stream can be before it blocks both streams
  outright on USB bandwidth), calibration tuning (`settle_frames`,
  `row_gap_px`, `min_blob_area`, `neighborhood_size`,
  `min_acceptable_contrast`), live-test tuning (`scan_direction`,
  `switch_time_ms`, `num_leds`, `stream_a_threshold_fraction`/
  `stream_b_threshold_fraction` (starting defaults for the Threshold Tuning
  page's per-stream spinboxes), `frame_drop_threshold_factor`,
  `warmup_pairs_to_skip`, `pairing_gap_outlier_threshold_us`), inter-sensor
  sync tuning (`camera_sync.enable_depth_for_ir_sync` - co-enables the
  stereo module's depth stream whenever IR is one of the two picks, which
  is what actually makes IR/RGB (or IR/IR) come out synchronized regardless
  of which sensor `rs.pipeline()` happens to open first, at the cost of USB
  bandwidth; `camera_sync.capture_global_ts` - enables the GLOBAL_TIME-
  domain timestamp capture multi-camera cross-matching and the "Global TS
  Latency" metric depend on, requiring every configured camera to support
  it), and the optional dual-LED-panel rig's wiring
  (`dual_panel.stream_a_panel_port`/`stream_b_panel_port`/`relay_port` -
  Acroname hub USB port numbers, keyed per STREAM since the mapping isn't
  necessarily 0=A/1=B on your rig, `relay_com_port`, `hub_switch_settle_s` -
  only read when Stream Config's "Use dual LED panel" checkbox is checked;
  the latter is a real-hardware-tuned guess, keep raising it if panel
  commands still seem unreliable). Nothing in the app writes to this file.
- **`config.yaml`** — auto-generated by the Calibration step. Each
  calibration run updates only its own two stream slugs' entries under the
  connected camera name (e.g. `infrared1`, `color`, `color2`) — any other
  previously-calibrated stream slug on that same camera is left untouched,
  not overwritten wholesale — so recalibrating one stream doesn't invalidate
  another stream's saved positions. Never hand-edit this.
- **`gui_state.json`** — a lossy, JSON-friendly prefill record of the last
  device/stream/ROI/camera-control choices, used only to pre-select Stream
  Config defaults on the NEXT app launch. Written automatically as you move
  through a camera's sub-flow; gitignored, since it's machine-specific. The
  Camera Hub's own real state for the CURRENT run (every configured
  camera's picks, which one is Master) lives only in memory for that run,
  not in this file.

## Output

Everything lands under `output/` (created automatically), each run in its
own timestamped subfolder so a new run never overwrites a previous one:

- `output/calibration_<timestamp>/` — one per Calibration page visit
  (shared by however many times you click **Run Calibration** in that one
  visit):
  - `debug_<slug>_detection.png` for each of Stream A/Stream B (e.g.
    `debug_infrared1_detection.png`, `debug_color_detection.png`) —
    calibration's masked frame with detected LEDs circled and numbered.
- `output/live_session_<timestamp>/` — one per single-camera **Start**
  click:
  - `pipeline_sync_raw.csv` — every kept frame-pair from the run
    (timestamps, pairing gap, position gap, exclusion flags, and per-stream
    `stream_a_frame_drop`/`stream_b_frame_drop` booleans).
  - `pipeline_sync_frame_drops.csv` — same schema, only the
    frame-drop-excluded rows.
  - `pipeline_sync_plot.png` — a static end-of-session plot: pairing gap
    (us), position gap (ms), and per-stream frame drop (mirrored +1/-1 so a
    simultaneous A+B drop can't hide one behind the other), each on its own
    axis, all vs. pair index, dark-themed to match the live charts. Figure
    width scales with how many pairs the run had (capped) so a long run's
    line stays legible instead of a fixed-size smear — no data is decimated
    to do this, every point is still plotted.
  - `hw_ts_latency_chart.png`, `optical_sync_chart.png`,
    `frame_drops_chart.png` — a PNG snapshot of each of the 3 live charts
    exactly as they looked at Stop (same image the chart's own "Copy"
    button would put on the clipboard, just auto-saved to disk too).
  - `live_led_state_stream_a.png`, `live_led_state_stream_b.png` — LED
    on/off debug snapshot from the most recent live-session frame: each
    calibrated LED position circled green if currently classified "on", red
    if "off" - lets you visually confirm the threshold classification is
    actually correct. Written automatically at Stop, or any time via
    **Save Debug Snapshot** (this same overlay is also shown live on the
    Stream A/Stream B video panels during the session, not just in the
    saved files).
  - `periodic_led_state_pair00020.png`, etc. — the same on/off debug
    overlay, saved automatically every `test.snapshot_every_n_pairs` pairs
    during the run (up to `test.max_snapshots` per stream), for
    spot-checking detection quality over the course of a run rather than
    just at the end. Both streams are combined into one side-by-side image
    (Stream A left, Stream B right) so they're cross-checkable in a single
    file. The pair index in the filename matches the CSV's `pair_index`
    column and what was on screen at that exact moment.
- `output/multi_camera_session_<timestamp>/` — one per **Start All** click:
  one subfolder per configured camera, each holding exactly the files
  listed above for that camera's own capture, plus at the top level a
  combined `cross_camera_sync.csv` (every matched Master/Slave pair, with
  HW TS Latency, Global TS Latency, and Optical Sync) and one
  `cross_camera_sync_plot_<slave-slug>.png` summary plot per Slave.

## Troubleshooting

- **Stream Config dropdowns are empty, or don't offer a resolution/fps you
  expect** — the list comes entirely from live hardware enumeration
  (`sensor.profiles`), filtered only to infrared/color video streams (no
  hardcoded pixel-format filter — every format the device reports for those
  stream types is listed). If an option genuinely isn't showing up, check
  that the device exposes it as an infrared or color video-stream profile
  at all (a non-video or non-infrared/color stream type won't appear by
  design).
- **`RuntimeError: ... does not expose per-frame HW timestamp metadata`** —
  see the metadata note under Prerequisites.
- **Calibration detects 0 LEDs** — check the debug images described above
  first. Common causes: the ROI doesn't actually frame the panel, the panel
  isn't at full brightness, or the rig is at a different distance than
  whatever `settings.yaml`'s tuning values were last validated against.
- **`LEDPanel` commands fail / calibration never turns the panel on** —
  confirm `LED-Panel.exe` is reachable (on PATH or in the launch directory).
- **"HW TS Latency" shows a large, consistent offset between Stream A and
  Stream B** — check `settings.yaml`'s `camera_sync.enable_depth_for_ir_sync`
  is `true`. Whether IR or RGB gets opened first inside `rs.pipeline()` (not
  controllable, and not affected by `enable_stream()` call order) decides
  whether the two sensors' hardware timestamps come out synchronized;
  co-enabling the stereo module's depth stream fixes this regardless of open
  order, at the cost of USB bandwidth. No-op for a color+color (Dual RGB)
  pairing, since there's no stereo module involved.
- **Dual LED panel takes noticeably longer on its first Start right after
  Calibration/ROI Select, then is fast afterward** — expected. The panels
  need one full relay-close/open "priming" cycle after Calibration/ROI
  Select touches them before they'll reliably respond to a trigger;
  `start_scanning()` handles this automatically (a one-time double-arm),
  and every later Start/Stop in the same session reuses the already-armed
  configuration instead of re-provisioning it.
- **Start is blocked with a genlock/color-resolution error on a
  multi-camera run** — a genlock Slave's own color stream hardware-syncs
  correctly only up to `settings.yaml`'s
  `camera.inter_cam_sync.<model>.max_slave_color_resolution`; above that it
  blocks BOTH streams entirely (a real USB-bandwidth ceiling, not a
  firmware limitation). Pick a lower resolution for that camera's color
  stream, or a camera model with a confirmed, larger limit.
- **A multi-camera run raises an error about `GLOBAL_TIME`** — every
  configured camera needs `global_time_enabled` support for the "Global TS
  Latency" cross-camera metric; if a rig's hardware/driver doesn't support
  it, turn off `settings.yaml`'s `camera_sync.capture_global_ts` (you lose
  that one metric, HW TS Latency and Optical Sync are unaffected).

## Architecture & project structure

```
domain/    pure image/math/calibration logic - no Qt, no pyrealsense2, fully unit-tested
engine/    hardware + live-session logic - pyrealsense2, LED panel control, QThread workers
gui/       PySide6 widgets and wizard pages
state/     the GUI's own persisted state (gui_state.json)
tests/     mirrors the structure above; hardware-facing code is verified manually instead
tools/     one-off, real-hardware diagnostic scripts (camera exposure reset,
           dual-LED-panel arm-sequence sweeps, genlock quality sweeps, LED
           panel drift measurement) - not part of the app, run manually when
           chasing a specific hardware issue
docs/      design/investigation notes (algorithm review log, technical deep
           dive, output validation report, and, under docs/superpowers/,
           the dated specs/plans that record the multi-camera/Camera Hub
           architecture as it was actually built) - record WHY things are
           the way they are; the top-level docs/*.md files predate the
           Camera Hub and are historical, not current architecture
```

Run the full test suite with `pytest -v` from the project root (`pytest.ini`
sets `pythonpath = .` so the above packages resolve without installing
anything).

### Layering

`domain -> engine -> gui`, plus `state`, in strictly one direction - nothing
in `domain/` imports `engine/` or `gui/`, and `engine/` never imports `gui/`:

- **`domain/`** holds pure image/math/calibration/export logic: LED
  position/threshold detection, the running-stats math, CSV export, plot
  export. No Qt, no `pyrealsense2` - every function here takes plain numpy
  arrays in and out, which is what makes it exhaustively unit-testable
  without any hardware or display attached.
- **`engine/`** splits into a pure-Python core and a thin hardware/Qt shell.
  The core (`metrics.py`'s `Metric` implementations, `test_session.py`'s
  `TestSession`, `acquisition_loop.py`'s `AcquisitionLoop`, `rgb_mode.py`'s
  PID-based mode lookup, `cross_camera_reconciler.py`'s GLOBAL_TIME
  matching) is unit-tested against fakes. The shell (`streams.py`,
  `led_panel.py`, `session_engine.py`, `dual_panel_control.py`,
  `multi_camera_session.py`) is where real device/subprocess/serial calls
  and cross-thread orchestration happen; most of `streams.py`'s own logic
  is still pure enough to test against fake sensor/device objects, but the
  genuinely hardware-only parts (an open `rs.pipeline()`, the LED panel
  CLI, the Acroname hub/relay, actual USB mode-switch re-enumeration) have
  no automated tests by design - they're verified against real hardware
  instead.
- **`gui/`** is the PySide6 app: `gui/pages/camera_hub_page.py` is the root
  view; the per-camera sub-flow pages (`device_select_page.py`,
  `stream_config_page.py`, `roi_select_page.py`, `calibration_page.py`,
  `threshold_tuning_page.py`) and the two live-test pages
  (`live_session_page.py`, `multi_camera_live_session_page.py`) live
  alongside it, plus reusable widgets under `gui/widgets/` (including
  `camera_live_session_panel.py`, the per-camera view duplicated into the
  multi-camera page's own per-camera tabs). `gui/main_window.py`'s
  `MainWindow` wires all of it together and owns the run's actual
  source-of-truth state (`self._cameras`, `self._master_camera_id`) - a
  `GuiState`/`gui_state.json` prefill record is a separate, much lossier
  concern (see Configuration files above).

### Camera Hub and the per-camera sub-flow

`CameraHubPage` is a "dumb" view holding no camera state of its own - it
only renders whatever `CameraSummary` list `MainWindow` feeds it and emits
signals (add/edit/remove/set-master/start requested). Add and Edit both
route through the SAME five sub-flow pages, just entering at a different
point: Add starts at Device Select (a fresh device, picks default), Edit
jumps straight to Stream Config prefilled with that camera's previously
committed choices (the device is already known, so re-picking it would be
pointless). Both converge on Threshold Tuning's "Continue", which commits
the camera's config into `MainWindow._cameras[camera_id]` and returns to
the Hub. The first camera ever added is auto-made Master; any other camera
can be promoted via its own card's "Set as Master" button.

### Resolving two stream picks to physical sensors

Stream Config produces two independent picks (`pick_a`/`pick_b`) with no
idea whether they land on the same physical sensor or two different ones.
`engine.streams.resolve_and_group(device, pick_a, pick_b)` answers that:
if both picks share a `sensor_index`, they resolve to ONE sensor object
opened with two stream profiles together (the Dual-RGB shape); if they
differ, they resolve to TWO distinct sensor objects (the Stereo Module +
RGB Camera shape). Everything downstream - camera-control application,
one-shot captures for ROI Select/Calibration, the live `ContinuousCapture`
- works off this resolved `groups` list, since `sensor.open()`/`.start()`
has to be called once per distinct sensor with all of that sensor's wanted
profiles at once, not once per pick.

### Camera controls

Camera Controls presents one shared group in the UI, but is applied once
per RESOLVED sensor group underneath, not once per pick. Emitter enable and
the auto/manual exposure mode apply identically everywhere; in Manual mode,
`exposure_for_group` decides which of the two exposure values (Exposure A /
Exposure B) a given resolved group actually gets - a group made of only
Stream A's profile gets Exposure A, only Stream B's gets Exposure B, and a
group holding both (the Dual-RGB shape) can only ever take one real value
in hardware, so it gets Exposure A. There is no gain control anywhere in
the app - switching back to Auto just re-enables the camera's own
continuous AE algorithm, which drives gain live from the real scene, same
as a power-cycle would.

### Keeping two sensors' hardware timestamps synchronized

Whether Stream A and Stream B's hardware timestamps come out synchronized
depends on which sensor `rs.pipeline()` happens to open FIRST internally -
something `config.enable_stream()` call order does not control. Co-enabling
the stereo module's depth stream alongside IR (`camera_sync.
enable_depth_for_ir_sync`, on by default) keeps the two synchronized
regardless of that open order; it costs extra USB bandwidth, so it stays a
toggle rather than an unconditional default. See the Troubleshooting entry
above.

### Threshold tuning's live preview

Unlike the live session, the threshold-tuning preview thread emits raw
per-LED brightness, not a precomputed on/off mask - the GUI computes the
threshold and the mask itself from whatever the relevant spinbox currently
reads, on every incoming frame, so a threshold change is reflected
immediately with no thread restart.

### Dual LED panel control

Both panels share one Acroname USB hub (only one panel's USB connection is
visible to the OS at a time) and one external trigger relay wired to both
panels' trigger inputs. The relay is a GATE, not a one-shot pulse - both
panels keep stepping in lockstep only while it stays closed; releasing it
freezes both wherever they are. `engine/dual_panel_control.py` centralizes
the hub-switching, relay control, and a real-hardware-confirmed quirk: a
panel needs one full relay-close/open "priming" cycle after Calibration/
ROI Select touches it before it reliably responds to the next trigger, so
`start_scanning()` automatically double-arms on the first Start after
Calibration/ROI Select (tracked via a module-level flag), and skips
reconfiguring entirely on a repeat Start with unchanged settings, since
only the hub switch itself - not the handful of LED-panel CLI calls - is
what actually costs time. Because the hub/relay are a single shared
resource, at most one configured camera may use dual-panel mode in any one
multi-camera run.

### Multi-camera orchestration and cross-camera reconciliation

`engine/multi_camera_session.py`'s `MultiCameraSessionController` runs one
completely unmodified `SessionEngineThread` per configured camera, on the
GUI thread (not itself a `QThread`) - it applies genlock roles (Master
first, all-or-nothing: if any camera's role write fails, every already-
applied role is reverted) before starting, then starts each camera's thread
staggered by a couple of seconds, a real-hardware-found USB-enumeration-
collision avoidance delay. It relays every thread's signals tagged with
that camera's id, and feeds every `row_ready` into a shared
`CrossCameraReconciler`.

`engine/cross_camera_reconciler.py` matches Master/Slave frame rows using
RealSense's GLOBAL_TIME-domain timestamp rather than each device's own raw
hardware timestamp, since GLOBAL_TIME is directly comparable across
devices from the first row - a raw HW timestamp has an arbitrary,
per-device epoch that only a one-time, drifting offset calibration can
bridge (a real-hardware finding: that calibration drifts roughly 40us over
50s, small but silently baked into "HW TS Latency" as if it were genuine
physical latency). "Global TS Latency" is the plain, NEVER offset-corrected
diff of the two sides' global timestamps for the same matched pair -
reported alongside "HW TS Latency" specifically so the two can be compared;
if global time behaves as documented, it should stay near zero, unlike its
HW-ts counterpart. "Optical Sync" for a cross-camera pair reuses the same
last-detected-LED-index math as the single-camera case, using the Master's
own `num_leds`/switch-time as authoritative.

### Naming: UI labels vs. internal data keys

The UI shows "HW TS Latency", "Optical Sync", and "Global TS Latency" as
display names, but the underlying `Metric.name`/CSV columns are
`pairing_gap_us`/`position_gap_ms`/`global_ts_gap_us` throughout `engine/`
and `domain/csv_export.py`. Only display text (chart axis titles, stat tile
labels) uses the renamed terms - don't assume a UI label matches an
underlying data key when tracing a value through the pipeline.
