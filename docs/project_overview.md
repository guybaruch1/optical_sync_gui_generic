# Optical Sync GUI — Project Overview

## What is this?

A Windows desktop application that measures how well an Intel RealSense camera's
IR and RGB sensors are synchronized in time, using a controllable LED test panel
as a known, precise reference signal. It's a guided 5-step wizard that walks an
operator from "camera connected" to "here's your sync-quality report" without
touching a command line.

## The problem it solves

RealSense devices stream IR and RGB as two logically separate cameras that are
expected to capture the same instant together. Verifying that in practice used
to mean running three separate, hand-invoked Python scripts in sequence (pick a
region of interest, calibrate against the LED panel, then run the actual timing
test) — each with its own manual steps, no shared state, and no live feedback
while a test was running. Mistakes (wrong ROI, stale calibration, camera
unplugged mid-run) were often only discovered after the fact, from a wall of
console output.

This project consolidates that workflow into one application with live visual
feedback at every step, so problems are visible as they happen, not after a
10-minute run has already finished.

## How the test actually works

An Image Engineering LED panel scans through its LEDs one at a time, very fast
— faster than the camera's own frame interval, by design. Both the IR and RGB
sensors watch the same panel simultaneously. By comparing **which LED each
sensor "saw" lit at a given moment**, and **when each sensor's hardware actually
captured that frame**, the app produces two independent, complementary measures
of sync quality:

- **HW TS Latency** — the raw hardware-timestamp gap between an IR frame and its
  paired RGB frame, in microseconds. This is what the camera's own clock says.
- **Optical Sync** — which LED position IR currently sees lit vs. which LED
  position RGB currently sees lit, converted into a time gap using the panel's
  known scan speed. This is what actually happened optically, independent of
  what either sensor's clock claims.

Together they answer both "do the two clocks agree?" and "does what the two
sensors actually captured agree?" — which can diverge if there's a clock
skew that isn't reflected in real capture timing, or vice versa.

The app also tracks **frame drops** per stream in real time, since a dropped
frame invalidates both measurements for that pair and needs to be visible
immediately, not discovered by data-cleaning after the fact.

## The wizard flow

1. **Device Select** — lists every connected RealSense device that has both a
   Stereo Module and an RGB Camera.
2. **Stream Config** — choose IR/RGB resolution and fps from what the camera
   actually reports supporting, with a live pairing-quality preview (frame
   numbers, hardware timestamps, and their delta burned onto the video) so you
   can sanity-check a resolution/fps combo before committing to it.
3. **ROI Select** — capture one frame with the panel fully lit, then drag a box
   around the panel on each stream.
4. **Calibration** — detects every individual LED's pixel position and its
   on/off brightness threshold, with debug images saved automatically so a
   failed detection is diagnosable, not just a cryptic error.
5. **Live Session** — runs the actual test: live IR/RGB video with a real-time
   LED on/off overlay, three live-scrolling graphs (HW TS Latency, Optical
   Sync, Frame Drops), a live stats sidebar with running min/avg/std/max, and
   Start/Stop with an optional fixed duration.

## What the operator sees during a live test

- **Dual video feed**, cropped to the calibrated region of interest, each
  labeled with the camera model and stream (e.g. "D455 - IR").
- **Three live graphs**, each independently toggleable, each with its own
  "copy as image" and "export this chart's data as CSV" buttons.
- **A stats sidebar**: live current values (frame index, HW TS latency,
  optical sync, LED switch time, per-stream frame-drop counts) plus a running
  min/avg/std/max table for the two headline metrics.
- **Per-run controls**: test duration, LED switch speed, and how often the
  display refreshes — all editable before Start, locked during a run so a
  change can't misleadingly appear to apply mid-test.

## What comes out of it

Every run writes, automatically, to an output folder:

- Raw and frame-drop CSVs (every frame pair, with exclusion reasons flagged —
  warmup, outlier, frame drop — never silently discarded)
- A static end-of-run summary plot image
- LED on/off debug snapshots, both periodically during the run and on demand,
  so a suspicious result can be visually cross-checked against the exact frame
  it came from

## Under the hood (brief)

- **PySide6** for the desktop UI, **pyqtgraph** for the live graphs,
  **pyrealsense2** for camera control, **OpenCV**/**numpy** for image
  processing, **matplotlib** for the static summary plot.
- The codebase is layered so the actual measurement logic (metric math, session
  buffering, frame-pair processing) is plain Python with no Qt or hardware
  dependency, fully unit-tested independently of both. Only a thin adapter
  layer touches real hardware and the GUI thread.
- 112 automated tests, run automatically on every push/PR via GitHub Actions.

## Status

Feature-complete for its core purpose: the full wizard flow works end-to-end,
the live session view has been visually redesigned to a reviewed mockup, and
recent work has focused on making previously-fixed settings (LED switch speed,
display refresh rate) operator-adjustable per run instead of requiring a config
file edit.
