"""Standalone tool - NOT part of the shipped app, no automated tests (same
"hardware-only, no tests by design" bucket as engine/led_panel.py,
engine/acroname_hub.py, tools/dual_panel_diag/diag_*.py).

One-time setup for tools/panel_drift/panel_drift_measure.py: calibrates BOTH physical
LED panels' positions/thresholds as seen by a SINGLE camera stream, so the
measurement script can measure drift between the two panels using only
that one stream as the timing reference - this eliminates the sensor-type
confound of comparing two different physical imagers (e.g. IR1 vs IR2),
which is what the existing dual-panel wizard flow always does.

Deliberately does NOT touch engine.streams.resolve_and_group (which
rejects pick_a == pick_b - the real wizard's "two distinct streams"
invariant stays untouched) - this script builds its own single-profile
(sensor, [profile]) group directly. Also deliberately does NOT use
engine.dual_panel_control's turn_all_leds_on/off/switched_to_stream_panel
(those assume a single stream's own panel, or both panels driven
together) - controlling "panel A on, panel B off" independently of any
camera stream identity is a new access pattern only this niche test
needs, so it's inlined here rather than added to that shared module.

PICK below must point to a single stream that can see BOTH physical
panels simultaneously in its own field of view - edit it if you want to
test with a different stream than the default (D455 infrared/1).

Run from the repo root: python tools/panel_drift/panel_drift_calibrate.py
Writes output/panel_drift_calibration.yaml, consumed by
tools/panel_drift/panel_drift_measure.py. Re-run this whenever the panels/camera are
physically moved.

For each panel, an ROI-select window comes up first, then a threshold-
select window (see select_threshold_interactively) with a live trackbar -
drag it until the overlaid detected-LED count/circles look right for
THIS frame's actual exposure, then Space/Enter to confirm. Deliberately
NOT an automatic Otsu threshold (domain.realsense_utils.detect_led_centroids's
default) - real-hardware testing showed Otsu can fail badly on one
panel's frame while working fine on the other in the same run.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import cv2
import numpy as np
import yaml
import pyrealsense2 as rs

from settings import load_settings, ensure_output_dir
from state.gui_state import load_gui_state
from engine.streams import find_device_by_serial, capture_synced_frame_pair
from engine.led_panel import LEDPanel
from domain.realsense_utils import (
    crop_to_roi, merge_close_centroids, decode_frame, save_debug_detection_image,
)
from domain.calibration import assign_grid_ids, build_positions_with_thresholds
from gui.pages.roi_select_page import _select_roi

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SETTINGS_PATH = os.path.join(REPO_ROOT, "settings.yaml")

# Which single stream to test - MUST match the PICK in
# tools/panel_drift/panel_drift_measure.py exactly (the measurement script re-declares
# its own copy rather than importing this one, matching this project's
# existing convention of duplicating small constants across hardware-only
# scripts rather than sharing them - see tools/dual_panel_diag/diag_*.py). Edit both files
# together if you change this.
PICK = {
    "stream_type": rs.stream.infrared,
    "stream_index": 1,
    "format": rs.format.y8,
    "width": 1280,
    "height": 720,
    "fps": 30,
}

# Falls back to this if gui_state.json has no device_serial saved (i.e. the
# wizard's Device Select page has never been run on this machine). Leave as
# None to require gui_state.json; set to your camera's serial (a string,
# e.g. "123456789012") to bypass it entirely.
DEVICE_SERIAL = None


def resolve_device_serial():
    if DEVICE_SERIAL:
        return DEVICE_SERIAL
    gui_state = load_gui_state(os.path.join(REPO_ROOT, "gui_state.json"))
    if gui_state.device_serial:
        return gui_state.device_serial
    raise RuntimeError(
        "No device_serial found in gui_state.json (run the wizard's Device Select page at least "
        "once) - or edit this script's DEVICE_SERIAL constant directly."
    )


def find_sensor_and_profile(device, pick):
    """Same sensor/profile lookup resolve_and_group uses internally, but for
    a single pick - deliberately not calling resolve_and_group itself,
    since it exists purely to keep this niche script's needs away from that
    function's "reject identical picks" contract."""
    for sensor_index, sensor in enumerate(device.query_sensors()):
        for p in sensor.profiles:
            if not p.is_video_stream_profile():
                continue
            if p.stream_type() != pick["stream_type"] or p.stream_index() != pick["stream_index"]:
                continue
            if p.format() != pick["format"] or p.fps() != pick["fps"]:
                continue
            vp = p.as_video_stream_profile()
            if vp.width() != pick["width"] or vp.height() != pick["height"]:
                continue
            return sensor, p
    raise RuntimeError(
        "No sensor/profile on this device matches PICK {!r} - check the connected camera "
        "actually offers this exact stream_type/stream_index/format/width/height/fps.".format(pick)
    )


def _connect_hub():
    # Imported lazily, same reason engine/dual_panel_control.py's
    # _connect_hub does - the real `brainstem` SDK only needs to be
    # installed on a machine that actually runs this dual-panel-only tool.
    from engine.acroname_hub import AcronameHub

    hub = AcronameHub()
    if not hub.try_connect():
        raise RuntimeError("Failed to connect to the Acroname hub - check it's connected and powered.")
    return hub


def _switch_to_panel(dual_panel_config, which):
    """which: 'a' or 'b'. Reuses settings.yaml dual_panel.stream_a_panel_port/
    stream_b_panel_port purely as arbitrary port labels here - this test has
    only ONE camera stream, so "stream_a"/"stream_b" no longer means
    anything about which side of the camera a panel is on, just which
    physical panel each port number belongs to."""
    my_port = dual_panel_config["stream_{}_panel_port".format(which)]
    other = "b" if which == "a" else "a"
    other_port = dual_panel_config["stream_{}_panel_port".format(other)]
    hub = _connect_hub()
    try:
        hub.enable_ports([my_port], False, delay_in_seconds=0)
        hub.disable_ports([other_port])
        time.sleep(dual_panel_config["hub_switch_settle_s"])
    finally:
        hub.disconnect()


def turn_panel_on(dual_panel_config, which):
    _switch_to_panel(dual_panel_config, which)
    # set_mode(5), NOT all_leds_on() (which sends --stop before --setMode 5) -
    # confirmed via real-hardware testing (see engine/led_panel.py's
    # set_mode docstring / engine/dual_panel_control.py's start_scanning) that
    # a --stop sent to a panel poisons it out of trigger-mode stepping on its
    # NEXT arm, even if the --stop happened earlier in an unrelated call
    # (here, during calibration) rather than immediately before the
    # trigger-mode sequence itself - NOT a full power cycle, just the next
    # start_scanning() call (dual_panel_control.stop_scanning() now avoids
    # this itself by calling LEDPanel.reset() instead of .stop(), but this
    # calibration script isn't stop_scanning() and has no reason to send
    # --stop in the first place). This calibration step runs before tools/
    # panel_drift_measure.py's start_scanning() call, so it must never send
    # --stop either, or the later trigger-mode arming silently fails to
    # actually step even though start_scanning() itself looks correct in
    # isolation.
    LEDPanel.set_mode(5)  # all LEDs on
    time.sleep(0.5)  # let the panel actually reach full brightness


def turn_panel_off(dual_panel_config, which):
    _switch_to_panel(dual_panel_config, which)
    LEDPanel.set_mode(3)  # all LEDs off - same no-stop reasoning as turn_panel_on


def capture_frame(groups, pick, settle_frames):
    frames = capture_synced_frame_pair(groups, settle_frames=settle_frames)
    raw = frames[(pick["stream_type"], pick["stream_index"])]
    return decode_frame(raw, pick["format"], pick["width"], pick["height"])


def _offset_positions(positions, roi):
    """Same pattern as gui/pages/calibration_page.py's _offset_positions -
    assign_grid_ids' centroids come from a crop_to_roi'd image, so they're
    in that CROPPED image's own coordinates. Shift back to full-frame
    coordinates so build_positions_with_thresholds samples brightness from
    the right pixels."""
    roi_x, roi_y = roi[0], roi[1]
    return {led_id: [x + roi_x, y + roi_y] for led_id, (x, y) in positions.items()}


def _detect_centroids_at_threshold(gray, threshold_value, min_area, kernel):
    _, binary = cv2.threshold(gray, threshold_value, 255, cv2.THRESH_BINARY)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    centroids = []
    for cnt in contours:
        if cv2.contourArea(cnt) < min_area:
            continue
        (cx, cy), _ = cv2.minEnclosingCircle(cnt)
        centroids.append((cx, cy))
    return centroids


def select_threshold_interactively(image, min_area, window_title, initial_threshold=127):
    """domain.realsense_utils.detect_led_centroids always uses Otsu's
    automatic threshold and ignores whatever threshold value is passed to
    it - fine most of the time, but real-hardware testing showed it can
    fail badly on a frame whose exposure/contrast doesn't fit Otsu's
    bimodal assumption (one panel's frame found only 25/100 LEDs via Otsu
    while the other found 99/100, same rig, same run). Lets the operator
    drag a trackbar and see the live detected-blob count/overlay update
    immediately, to dial in a threshold that actually works for THIS
    frame's exposure instead of trusting an automatic guess picked in
    advance. Space/Enter confirms; 'c' cancels (returns None, None)."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image.copy()
    kernel = np.ones((3, 3), np.uint8)

    cv2.namedWindow(window_title)
    cv2.createTrackbar("Threshold", window_title, initial_threshold, 255, lambda _: None)
    print(
        "Adjust the 'Threshold' slider in the '{}' window until the detected LED count looks "
        "right, then press SPACE/ENTER to confirm (or 'c' to cancel).".format(window_title)
    )

    chosen_threshold = None
    while True:
        threshold_value = cv2.getTrackbarPos("Threshold", window_title)
        centroids = _detect_centroids_at_threshold(gray, threshold_value, min_area, kernel)

        display = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        for (x, y) in centroids:
            cv2.circle(display, (int(x), int(y)), 4, (0, 255, 0), 1)
        cv2.putText(
            display, "threshold={} detected={}".format(threshold_value, len(centroids)),
            (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2,
        )
        cv2.imshow(window_title, display)

        key = cv2.waitKey(30) & 0xFF
        if key in (13, 32):  # Enter or Space
            chosen_threshold = threshold_value
            break
        if key == ord("c"):
            break

    cv2.destroyWindow(window_title)
    if chosen_threshold is None:
        return None, None
    return _detect_centroids_at_threshold(gray, chosen_threshold, min_area, kernel), chosen_threshold


def calibrate_one_panel(label, on_frame, off_frame, min_blob_area, row_gap_px, neighborhood_size,
                         min_acceptable_contrast, output_dir, slug):
    roi = _select_roi(on_frame, "Select ROI for {} (panel lit)".format(label))
    if roi is None:
        raise RuntimeError("ROI selection for {} was cancelled.".format(label))

    cropped = crop_to_roi(on_frame, roi)
    print("Detecting LEDs in {} frame...".format(label))
    centroids, chosen_threshold = select_threshold_interactively(
        cropped, min_blob_area, "Adjust threshold for {} (panel lit)".format(label),
    )
    if centroids is None:
        raise RuntimeError("Threshold selection for {} was cancelled.".format(label))
    centroids = merge_close_centroids(centroids)
    print("Detected {} LED(s) in {} (threshold {}, manually chosen).".format(len(centroids), label, chosen_threshold))

    debug_path = os.path.join(output_dir, "debug_panel_drift_{}_detection.png".format(slug))
    save_debug_detection_image(cropped, centroids, debug_path)
    print("Saved debug image: {}".format(debug_path))

    positions, row_layout = assign_grid_ids(centroids, row_gap_px)
    positions = _offset_positions(positions, roi)
    positions = build_positions_with_thresholds(positions, on_frame, off_frame, neighborhood_size)

    weakest_id, weakest_contrast = min(
        ((led_id, vals[2] - vals[3]) for led_id, vals in positions.items()),
        key=lambda pair: pair[1],
    )
    print("{} weakest LED contrast: led_id={} on-off={:.2f}".format(label, weakest_id, weakest_contrast))
    if weakest_contrast < min_acceptable_contrast:
        print("  WARNING: this LED's on/off gap is small - its threshold may be unreliable.")

    return positions, row_layout


def main():
    settings = load_settings(SETTINGS_PATH)
    output_dir = ensure_output_dir(settings)
    dual_panel_config = settings["dual_panel"]
    calibration_settings = settings["calibration"]

    device_serial = resolve_device_serial()
    ctx = rs.context()
    device = find_device_by_serial(ctx, device_serial)
    sensor, profile = find_sensor_and_profile(device, PICK)
    groups = [(sensor, [profile])]

    settle_frames = calibration_settings["settle_frames"]
    row_gap_px = calibration_settings["row_gap_px"]
    min_blob_area = calibration_settings["min_blob_area"]
    neighborhood_size = calibration_settings["neighborhood_size"]
    min_acceptable_contrast = calibration_settings["min_acceptable_contrast"]

    print("Turning both panels off...")
    turn_panel_off(dual_panel_config, "a")
    turn_panel_off(dual_panel_config, "b")
    print("Capturing OFF-state frame...")
    off_frame = capture_frame(groups, PICK, settle_frames)

    print("Turning panel A on (panel B off)...")
    turn_panel_on(dual_panel_config, "a")
    print("Capturing panel-A-ON frame...")
    on_frame_a = capture_frame(groups, PICK, settle_frames)
    turn_panel_off(dual_panel_config, "a")

    print("Turning panel B on (panel A off)...")
    turn_panel_on(dual_panel_config, "b")
    print("Capturing panel-B-ON frame...")
    on_frame_b = capture_frame(groups, PICK, settle_frames)
    turn_panel_off(dual_panel_config, "b")

    positions_a, row_layout_a = calibrate_one_panel(
        "Panel A", on_frame_a, off_frame, min_blob_area, row_gap_px, neighborhood_size,
        min_acceptable_contrast, output_dir, "panel_a",
    )
    positions_b, row_layout_b = calibrate_one_panel(
        "Panel B", on_frame_b, off_frame, min_blob_area, row_gap_px, neighborhood_size,
        min_acceptable_contrast, output_dir, "panel_b",
    )

    if row_layout_a != row_layout_b:
        print(
            "WARNING: Panel A row layout {} != Panel B row layout {} - led_id may not correspond "
            "to the same physical LED position on both panels.".format(row_layout_a, row_layout_b)
        )

    out_path = os.path.join(output_dir, "panel_drift_calibration.yaml")
    with open(out_path, "w") as f:
        yaml.safe_dump({
            "pick": {
                "stream_type": PICK["stream_type"].name,
                "stream_index": PICK["stream_index"],
                "format": PICK["format"].name,
                "width": PICK["width"],
                "height": PICK["height"],
                "fps": PICK["fps"],
            },
            "panel_a": {"positions": positions_a},
            "panel_b": {"positions": positions_b},
        }, f, sort_keys=False)

    print("Saved {} panel-A LED(s) and {} panel-B LED(s) to {}".format(
        len(positions_a), len(positions_b), out_path
    ))


if __name__ == "__main__":
    main()
