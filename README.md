# Optical Sync GUI

A PySide6 desktop app for measuring IR/RGB timing sync on an Intel RealSense
camera against an Image Engineering LED panel. It replaces three separate
command-line scripts (ROI picker, LED calibration, pipeline sync test) with
one guided wizard: pick your device and stream settings, draw the ROI,
calibrate the LED panel, then run a live sync test with a real-time video
feed and graph.

## Features

- **Device selection** — lists every connected RealSense device with both a
  Stereo Module and an RGB Camera.
- **Stream configuration** — pick IR/RGB resolution and fps from what the
  camera actually supports, with a **live pairing-quality preview**: a
  bundle counter, per-stream HW frame number, HW timestamps, and their delta
  are burned onto the video and printed to the console, so you can sanity
  check pairing before committing to a combo.
- **ROI selection** — capture one frame with all LEDs lit, then drag a box
  on each stream via a native OpenCV popup window.
- **LED calibration** — detects every LED's pixel position and per-LED
  on/off/threshold values, with live progress logging and saved debug
  images (masked frame + detected LEDs circled) so you can see exactly what
  went wrong if detection fails.
- **Live sync session** — dual IR/RGB video panels (each showing a live
  LED on/off detection overlay), a live scrolling plot of two metrics
  (HW-timestamp pairing gap, and LED-position gap while the panel is
  scanning), a second live plot of per-pair frame drops, a live stats
  sidebar (frame index, HW timestamp gap, position gap, LED switch time,
  IR/RGB frame-drop counts), Start/Stop with an optional fixed duration, a
  "Save Debug Snapshot" button for an on-demand LED on/off correctness
  check, and CSVs plus a summary plot image written at the end.

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

## Getting the code

```powershell
git clone https://github.com/guybaruch1/optical_sync_gui.git
cd optical_sync_gui
```

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

1. **Device Select** — pick your connected RealSense device.
2. **Stream Config** — pick IR/RGB resolution and fps. Optionally click
   **Start Preview** to check pairing quality live before clicking **Next**
   (this stops the preview automatically).
3. **ROI Select** — click **Capture & Select ROI**; a popup shows a frame
   with all LEDs lit. Drag a box around the panel on the IR window, press
   Enter, then repeat for the RGB window.
4. **Calibration** — click **Run Calibration** and watch the log. On
   failure, check `output/debug_ir_detection.png` /
   `output/debug_rgb_detection.png` — these show the exact masked region
   and whatever was detected, even when zero LEDs were found.
5. **Live Session** — set an optional duration (0 = manual stop), click
   **Start**, watch both video feeds (each with a live LED on/off overlay),
   the live plot (toggle either metric series on/off), and the second plot
   of per-pair frame drops. Click **Save Debug Snapshot** any time to
   check the LED on/off classification against the live video. Click
   **Stop** (or let the duration elapse) to write the CSVs, a summary plot
   image, and a final debug snapshot under `output/`.

## Configuration files

- **`settings.yaml`** — the one file meant to be hand-edited between runs:
  default camera resolution/fps (pre-selected in the Stream Config
  dropdowns, not enforced), calibration tuning (`settle_frames`,
  `row_gap_px`, `min_blob_area`, `neighborhood_size`,
  `min_acceptable_contrast`), and live-test tuning (`scan_direction`,
  `switch_time_ms`, `num_leds`, `threshold_fraction`,
  `frame_drop_threshold_factor`, `warmup_pairs_to_skip`,
  `pairing_gap_outlier_threshold_us`). Nothing in the app writes to this
  file.
- **`config.yaml`** — auto-generated by the Calibration step; overwritten
  wholesale (per connected camera model) on every calibration run. Never
  hand-edit this.
- **`gui_state.json`** — the GUI's own state (last device, resolution/fps,
  ROI). Written automatically as you move through the wizard; gitignored,
  since it's machine-specific.

## Output

Everything lands under `output/` (created automatically):

- `debug_ir_detection.png`, `debug_rgb_detection.png` — calibration's masked
  frame with detected LEDs circled and numbered.
- `pipeline_sync_raw.csv` — every kept frame-pair from a live session
  (timestamps, pairing gap, position gap, exclusion flags, and per-stream
  `ir_frame_drop`/`rgb_frame_drop` booleans).
- `pipeline_sync_frame_drops.csv` — same schema, only the frame-drop-excluded
  rows.
- `pipeline_sync_plot.png` — a static end-of-session plot (pairing gap,
  position gap, and a per-pair frame-drop spike, all vs. pair index),
  rendered from the same rows as the CSVs.
- `live_led_state_ir.png`, `live_led_state_rgb.png` — LED on/off debug
  snapshot from the most recent live-session frame: each calibrated LED
  position circled green if currently classified "on", red if "off" - lets
  you visually confirm the threshold classification is actually correct.
  Written automatically at Stop, or any time via **Save Debug Snapshot**
  (this same overlay is also shown live on the IR/RGB video panels during
  the session, not just in the saved files).
- `periodic_led_state_ir_pair00020.png`, `periodic_led_state_rgb_pair00020.png`,
  etc. — the same on/off debug overlay, saved automatically every
  `test.snapshot_every_n_pairs` pairs during a live session (up to
  `test.max_snapshots` per stream), for spot-checking detection quality
  over the course of a run rather than just at the end. The pair index in
  the filename matches the CSV's `pair_index` column and what was on
  screen at that exact moment. Cleared at the start of each new session so
  files from a previous run don't linger.

## Troubleshooting

- **Stream Config dropdowns are empty, or don't offer a resolution/fps you
  expect** — the list comes entirely from live hardware enumeration
  (`sensor.profiles`), filtered to one fixed pixel format per stream (`y8`
  for IR, `yuyv` for color). If your camera reports that combo under a
  different format, it won't appear; this may need widening that filter
  once the actual available formats are known for your camera.
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
