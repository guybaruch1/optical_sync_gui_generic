# Optical Sync GUI

A PySide6 desktop app for measuring timing sync between ANY two video
streams a connected Intel RealSense camera offers - two IR streams, IR+RGB,
or two color streams on a Dual-RGB device - against an Image Engineering
LED panel. It replaces three separate command-line scripts (ROI picker, LED
calibration, pipeline sync test) with one guided wizard: pick your device,
pick two streams ("Stream A"/"Stream B") and their camera controls, draw
the ROI on each, calibrate the LED panel, then run a live sync test with a
real-time video feed and graphs.

## Features

- **Device selection** — lists every connected RealSense device (no
  Stereo-Module/RGB-Camera requirement — `engine.streams.list_devices` has
  no PID/sensor restriction). For D535/D585-family devices running in
  "Dedicated RGB" mode, offers to switch to "Dual RGB" mode before
  proceeding (devices that aren't a recognized D535/D585 variant skip this
  step entirely).
- **Stream configuration** — two independent pickers, Stream A and Stream
  B, each listing every fully-specified stream option the device actually
  reports (any infrared or color stream, any resolution/fps/format) as one
  combo entry (e.g. "Infrared 1 - 1280x720@30fps (y8)"). Below the pickers,
  one "Camera Controls" group per distinct physical sensor the two picks
  resolve to (one group if both picks share a sensor, e.g. two color
  streams on a Dual RGB device; two groups otherwise): an IR-emitter-disable
  checkbox (only shown if that group includes an infrared stream) and an
  auto/manual exposure+gain choice. A **live pairing-quality preview**
  follows: a bundle counter, per-stream HW frame number, HW timestamps, and
  their delta are burned onto Stream A's video and printed to the console,
  so you can sanity check pairing before committing to a combo.
- **ROI selection** — capture one frame with all LEDs lit, then drag a box
  on each stream via a native OpenCV popup window.
- **LED calibration** — detects every LED's pixel position and per-LED
  on/off/threshold values, with live progress logging and saved debug
  images (masked frame + detected LEDs circled) so you can see exactly what
  went wrong if detection fails.
- **Live sync session** — dual video panels for Stream A/Stream B (each
  showing a live LED on/off detection overlay), a live scrolling plot of
  two metrics (HW-timestamp pairing gap, and LED-position gap while the
  panel is scanning), a second live plot of per-pair frame drops, a live
  stats sidebar (frame index, HW timestamp gap, position gap, LED switch
  time, per-stream frame-drop counts, and a min/avg/std/max table for the
  two gap metrics), Start/Stop with an optional fixed duration, per-run
  editable LED switch time and frame sample interval, a "Save Debug
  Snapshot" button for an on-demand LED on/off correctness check, and CSVs
  plus a summary plot image written at the end.

## Prerequisites

- **Windows**, Python 3.10+ (developed against 3.13).
- **Intel RealSense SDK/drivers installed** — not just the `pyrealsense2` pip
  package. Install the Intel RealSense Viewer or SDK installer so Windows
  recognizes the camera at the OS level.
- **RealSense per-frame metadata enabled.** Windows sometimes disables this
  by default; if you see `RuntimeError: This camera/driver does not expose
  per-frame HW timestamp metadata...`, this needs a one-time enablement step
  at the OS/driver level (see Intel's librealsense docs on Windows metadata
  support), then reconnect the camera.
- **`LED-Panel.exe` on PATH** (or in the folder you launch the app from) —
  the CLI for the Image Engineering LED panel.
- A connected RealSense camera and LED panel for anything past the Device
  Select step.

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
design — see [Project structure](#project-structure)).

## Running the app

```powershell
.venv\Scripts\python.exe main.py
```

The window opens maximized. Walk through the wizard:

1. **Device Select** — pick your connected RealSense device. If it's a
   D535/D585-family device currently in "Dedicated RGB" mode, you'll be
   offered a one-time switch to "Dual RGB" mode before continuing.
2. **Stream Config** — pick Stream A and Stream B independently from every
   stream the device reports, and set each resolved sensor's camera
   controls (IR emitter disable, auto/manual exposure+gain). Optionally
   click **Start Preview** to check pairing quality live before clicking
   **Next** (this stops the preview automatically).
3. **ROI Select** — click **Capture & Select ROI**; a popup shows a frame
   with all LEDs lit. Drag a box around the panel on Stream A's window,
   press Enter, then repeat for Stream B's window.
4. **Calibration** — click **Run Calibration** and watch the log. On
   failure, check the debug images named after each stream's own slug (e.g.
   `output/debug_infrared1_detection.png`, `output/debug_color_detection.png`)
   — these show the exact masked region and whatever was detected, even
   when zero LEDs were found.
5. **Live Session** — set an optional duration (0 = manual stop), tune the
   LED switch time and frame sample interval if needed (locked once a
   session is running), click **Start**, watch both video feeds (each with
   a live LED on/off overlay), the live plots (toggle either metric series
   on/off), and the frame-drops plot. Click **Save Debug Snapshot** any
   time to check the LED on/off classification against the live video.
   Click **Stop** (or let the duration elapse) to write the CSVs, a summary
   plot image, and a final debug snapshot under `output/`.

## Configuration files

- **`settings.yaml`** — the one file meant to be hand-edited between runs:
  default stream A/B resolution/fps under `camera.stream_a`/`camera.stream_b`
  (pre-selected in the Stream Config dropdowns, not enforced), calibration
  tuning (`settle_frames`, `row_gap_px`, `min_blob_area`,
  `neighborhood_size`, `min_acceptable_contrast`), and live-test tuning
  (`scan_direction`, `switch_time_ms`, `num_leds`, `threshold_fraction`,
  `frame_drop_threshold_factor`, `warmup_pairs_to_skip`,
  `pairing_gap_outlier_threshold_us`). Nothing in the app writes to this
  file.
- **`config.yaml`** — auto-generated by the Calibration step; overwritten
  wholesale (per connected camera model) on every calibration run, with LED
  positions keyed per-stream by slug (e.g. `infrared1`, `color`, `color2`)
  under that camera — recalibrating one stream doesn't invalidate another
  stream's saved positions. Never hand-edit this.
- **`gui_state.json`** — the GUI's own state (last device; `stream_a_*`/
  `stream_b_*` type/index/resolution/fps/ROI/camera-control fields). Written
  automatically as you move through the wizard; gitignored, since it's
  machine-specific.

## Output

Everything lands under `output/` (created automatically):

- `debug_<slug>_detection.png` for each of Stream A/Stream B (e.g.
  `debug_infrared1_detection.png`, `debug_color_detection.png`) —
  calibration's masked frame with detected LEDs circled and numbered.
- `pipeline_sync_raw.csv` — every kept frame-pair from a live session
  (timestamps, pairing gap, position gap, exclusion flags, and per-stream
  `stream_a_frame_drop`/`stream_b_frame_drop` booleans).
- `pipeline_sync_frame_drops.csv` — same schema, only the frame-drop-excluded
  rows.
- `pipeline_sync_plot.png` — a static end-of-session plot (pairing gap,
  position gap, and a per-pair frame-drop spike, all vs. pair index),
  rendered from the same rows as the CSVs.
- `live_led_state_stream_a.png`, `live_led_state_stream_b.png` — LED on/off
  debug snapshot from the most recent live-session frame: each calibrated
  LED position circled green if currently classified "on", red if "off" -
  lets you visually confirm the threshold classification is actually
  correct. Written automatically at Stop, or any time via **Save Debug
  Snapshot** (this same overlay is also shown live on the Stream A/Stream B
  video panels during the session, not just in the saved files).
- `periodic_led_state_stream_a_pair00020.png`,
  `periodic_led_state_stream_b_pair00020.png`, etc. — the same on/off debug
  overlay, saved automatically every `test.snapshot_every_n_pairs` pairs
  during a live session (up to `test.max_snapshots` per stream), for
  spot-checking detection quality over the course of a run rather than just
  at the end. The pair index in the filename matches the CSV's `pair_index`
  column and what was on screen at that exact moment. Cleared at the start
  of each new session so files from a previous run don't linger.

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

## Project structure

```
domain/    pure image/math/calibration logic - no Qt, no pyrealsense2, fully unit-tested
engine/    hardware + live-session logic - pyrealsense2, LED panel control, QThread workers
gui/       PySide6 widgets and wizard pages
state/     the GUI's own persisted state (gui_state.json)
tests/     mirrors the structure above; hardware-facing code is verified manually instead
```

Run the full test suite with `pytest -v` from the project root (`pytest.ini`
sets `pythonpath = .` so the above packages resolve without installing
anything).
