import os
from unittest.mock import patch

import cv2
import numpy as np
import pyrealsense2 as rs

from gui.pages.calibration_page import CalibrationPage


def _minimal_context(output_root, **overrides):
    ctx = dict(
        ctx=None, device_serial="123456",
        pick_a={"stream_type": "infrared", "stream_index": 1, "sensor_index": 0,
                "width": 4, "height": 4, "fps": 30, "format": "y8"},
        pick_b={"stream_type": "color", "stream_index": 0, "sensor_index": 1,
                "width": 4, "height": 4, "fps": 30, "format": "bgr8"},
        camera_controls=[], stream_a_roi=(0, 0, 4, 4), stream_b_roi=(0, 0, 4, 4),
        config_path="config.yaml", camera_name="Intel RealSense D455",
        output_root=output_root,
    )
    ctx.update(overrides)
    return ctx


def test_set_context_mints_a_fresh_folder_under_output_root(qapp, tmp_path):
    page = CalibrationPage()

    page.set_context(**_minimal_context(str(tmp_path)))

    output_dir = page._pending_args["output_dir"]
    assert os.path.isdir(output_dir)
    assert os.path.dirname(output_dir) == str(tmp_path)


def test_two_page_visits_mint_two_different_folders(qapp, tmp_path):
    page = CalibrationPage()

    page.set_context(**_minimal_context(str(tmp_path)))
    first_output_dir = page._pending_args["output_dir"]
    page.set_context(**_minimal_context(str(tmp_path)))
    second_output_dir = page._pending_args["output_dir"]

    assert first_output_dir != second_output_dir


def test_repeated_run_clicks_within_one_visit_share_the_same_folder(qapp, tmp_path):
    page = CalibrationPage()
    page.set_context(**_minimal_context(str(tmp_path)))
    seen_output_dirs = []

    def _fake_run_calibration(**kwargs):
        seen_output_dirs.append(kwargs["output_dir"])

    with patch.object(page, "_run_calibration", side_effect=_fake_run_calibration):
        page._on_run_clicked()
        page._on_run_clicked()

    assert len(seen_output_dirs) == 2
    assert seen_output_dirs[0] == seen_output_dirs[1]


# --- last_calibration_result: retains a successful run's already-captured
# on/off frames + Otsu thresholds for ThresholdTuningPage's LED Detection
# Threshold Tuning section, without needing its own camera capture. These
# tests run the REAL _run_calibration end to end (real detect_led_centroids/
# merge_close_centroids/build_grid_positions/update_config_leds), mocking
# only the hardware-facing boundary (device resolution + frame capture). ---

def _synthetic_frame_bytes(pattern):
    return pattern.tobytes()


def _make_2x2_grid_frame(width, height, blob_value, background_value):
    frame = np.full((height, width), background_value, dtype=np.uint8)
    for row in range(2):
        for col in range(2):
            cy = 15 + row * 30
            cx = 15 + col * 30
            cv2.circle(frame, (cx, cy), 6, blob_value, -1)
    return frame


def _real_hardware_context(output_root, config_path):
    width = height = 60
    pick_a = {"stream_type": rs.stream.infrared, "stream_index": 1,
              "width": width, "height": height, "fps": 30, "format": rs.format.y8}
    pick_b = {"stream_type": rs.stream.color, "stream_index": 0,
              "width": width, "height": height, "fps": 30, "format": rs.format.bgr8}
    return dict(
        ctx=None, device_serial="123456", pick_a=pick_a, pick_b=pick_b,
        camera_controls=[], stream_a_roi=(0, 0, width, height), stream_b_roi=(0, 0, width, height),
        config_path=config_path, camera_name="Intel RealSense D455",
        output_root=output_root,
    )


def _patch_hardware_boundary(on_frame_a, off_frame_a, on_frame_b, off_frame_b, pick_a, pick_b):
    frames_on = {
        (pick_a["stream_type"], pick_a["stream_index"]): _synthetic_frame_bytes(on_frame_a),
        (pick_b["stream_type"], pick_b["stream_index"]): _synthetic_frame_bytes(
            cv2.cvtColor(on_frame_b, cv2.COLOR_GRAY2BGR)),
    }
    frames_off = {
        (pick_a["stream_type"], pick_a["stream_index"]): _synthetic_frame_bytes(off_frame_a),
        (pick_b["stream_type"], pick_b["stream_index"]): _synthetic_frame_bytes(
            cv2.cvtColor(off_frame_b, cv2.COLOR_GRAY2BGR)),
    }
    return patch.multiple(
        "gui.pages.calibration_page",
        find_device_by_serial=lambda ctx, serial: object(),
        resolve_and_group=lambda device, pick_a, pick_b: [],
        _apply_camera_controls=lambda groups, camera_controls: [],
        turn_all_leds_on=lambda dual_panel_config: None,
        turn_all_leds_off=lambda dual_panel_config: None,
        capture_synced_frame_pair=lambda groups, on_both_streaming=None, settle_frames=15: (
            on_both_streaming() if on_both_streaming else None, frames_on if on_both_streaming else frames_off
        )[1],
    )


def test_last_calibration_result_is_none_before_any_run(qapp):
    page = CalibrationPage()
    assert page.last_calibration_result is None


def test_last_calibration_result_populated_after_a_successful_run(qapp, tmp_path):
    config_path = str(tmp_path / "config.yaml")
    with open(config_path, "w") as f:
        f.write("leds: {}\n")
    ctx = _real_hardware_context(str(tmp_path), config_path)
    pick_a, pick_b = ctx["pick_a"], ctx["pick_b"]

    on_frame_a = _make_2x2_grid_frame(60, 60, blob_value=220, background_value=20)
    off_frame_a = np.full((60, 60), 20, dtype=np.uint8)
    on_frame_b = _make_2x2_grid_frame(60, 60, blob_value=220, background_value=20)
    off_frame_b = np.full((60, 60), 20, dtype=np.uint8)

    page = CalibrationPage()
    page.set_context(**ctx)

    with _patch_hardware_boundary(on_frame_a, off_frame_a, on_frame_b, off_frame_b, pick_a, pick_b), \
         patch("time.sleep"):
        page._on_run_clicked()

    result = page.last_calibration_result
    assert result is not None
    assert result["image_a_on"].shape[:2] == (60, 60)
    assert result["image_b_on"].shape[:2] == (60, 60)
    assert isinstance(result["stream_a_otsu_threshold"], int)
    assert isinstance(result["stream_b_otsu_threshold"], int)
    assert result["min_blob_area"] == 20  # calibration_page.py's set_context default
    assert result["row_gap_px"] == 15
    assert result["neighborhood_size"] == 5


def test_last_calibration_result_unchanged_after_a_failed_run(qapp, tmp_path):
    config_path = str(tmp_path / "config.yaml")
    with open(config_path, "w") as f:
        f.write("leds: {}\n")
    ctx = _real_hardware_context(str(tmp_path), config_path)
    pick_a, pick_b = ctx["pick_a"], ctx["pick_b"]

    on_frame_a = _make_2x2_grid_frame(60, 60, blob_value=220, background_value=20)
    off_frame_a = np.full((60, 60), 20, dtype=np.uint8)

    page = CalibrationPage()
    page.set_context(**ctx)
    assert page.last_calibration_result is None

    # Force the failure directly (rather than relying on a synthetic image
    # that happens to yield zero real detections - a uniform frame doesn't:
    # Otsu still finds one spurious full-frame "blob") - build_grid_positions
    # raising is exactly the real "no LEDs detected" failure shape
    # (RuntimeError, propagated from assign_grid_ids/centroids_in_grid_order).
    # _run_calibration must propagate this before ever reaching the
    # last_calibration_result assignment.
    with _patch_hardware_boundary(on_frame_a, off_frame_a, on_frame_a, off_frame_a, pick_a, pick_b), \
         patch("time.sleep"), \
         patch("gui.pages.calibration_page.build_grid_positions", side_effect=RuntimeError("no LEDs detected")):
        page._on_run_clicked()

    assert page.last_calibration_result is None
