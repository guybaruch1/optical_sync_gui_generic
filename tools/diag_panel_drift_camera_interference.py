"""Diagnostic script - NOT part of the shipped app, no automated tests.

Isolates whether OPENING/STREAMING A REALSENSE CAMERA PIPELINE alongside
already-armed dual-panel stepping is what stops the panels from
continuing to step, for tools/panel_drift_measure.py's "both panels stuck
on LED 0, physically confirmed not stepping" report.

engine.dual_panel_control.start_scanning's dual-panel sequence is already
confirmed (tools/diag_app_start_scanning.py, tools/
diag_baseline_demo_sequence.py) to produce continuous stepping ON ITS
OWN, with NO camera involved at all - so the one new variable
panel_drift_measure.py introduces is opening a bare rs.pipeline() and
pulling frames from it right after arming. This script reproduces that
exact same two-stage sequence with an explicit pause+prompt between each
stage, so you can report exactly WHICH stage the stepping actually stops
at (if it does):

  STAGE 1: dual_panel_control.start_scanning() only, no camera at all.
  STAGE 2: same armed state, but a camera pipeline is opened and actively
           pulls frames (like panel_drift_measure.py's frame_source loop).

Run from the repo root: python tools/diag_panel_drift_camera_interference.py
Watch BOTH physical panels continuously through both stages - not just at
the end - and report at which stage (if either) they stop advancing.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pyrealsense2 as rs

from settings import load_settings
from state.gui_state import load_gui_state
import engine.dual_panel_control as dual_panel_control

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Must match tools/panel_drift_measure.py's PICK - this is only testing
# camera interference, not detection, so an exact LED-position match
# doesn't matter, just the same stream identity/resolution/fps.
PICK = {
    "stream_type": rs.stream.infrared,
    "stream_index": 1,
    "format": rs.format.y8,
    "width": 1280,
    "height": 720,
    "fps": 30,
}

DEVICE_SERIAL = None


def resolve_device_serial():
    if DEVICE_SERIAL:
        return DEVICE_SERIAL
    gui_state = load_gui_state(os.path.join(REPO_ROOT, "gui_state.json"))
    if gui_state.device_serial:
        return gui_state.device_serial
    raise RuntimeError("No device_serial found in gui_state.json - edit this script's DEVICE_SERIAL constant.")


def main():
    settings = load_settings(os.path.join(REPO_ROOT, "settings.yaml"))
    dual_panel_config = settings["dual_panel"]
    test_settings = settings["test"]

    print("Arming dual-panel scanning (switch_time_ms={})...".format(test_settings["switch_time_ms"]))
    dual_panel_control.start_scanning(
        test_settings["switch_time_ms"], test_settings["scan_direction"], dual_panel_config,
    )

    print()
    print("=== STAGE 1: no camera opened at all ===")
    print("WATCH BOTH PHYSICAL PANELS NOW for 10s - are they stepping continuously?")
    time.sleep(10)
    print("STAGE 1 done.")
    print()

    device_serial = resolve_device_serial()
    config = rs.config()
    config.enable_device(device_serial)
    config.enable_stream(
        PICK["stream_type"], PICK["stream_index"], PICK["width"], PICK["height"], PICK["format"], PICK["fps"],
    )
    pipeline = rs.pipeline()
    pipeline.start(config)

    print("=== STAGE 2: camera pipeline now open, actively pulling frames ===")
    print("WATCH BOTH PHYSICAL PANELS NOW for 10s - do they KEEP stepping, or stop/change right now?")
    stage2_start = time.time()
    frame_count = 0
    while time.time() - stage2_start < 10.0:
        pipeline.wait_for_frames()
        frame_count += 1
    print("STAGE 2 done - pulled {} frames.".format(frame_count))
    print()

    pipeline.stop()
    print("Disarming dual-panel scanning...")
    dual_panel_control.stop_scanning(dual_panel_config)
    print("Done - report which stage (1, 2, both, or neither) had continuous stepping.")


if __name__ == "__main__":
    main()
