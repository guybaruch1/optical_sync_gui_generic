"""Wizard step 4: runs LED calibration in-app (same steps as
optical_sync_poc_/led_calibration.py's main()), logging progress into a
QPlainTextEdit instead of print()."""

import os
import time

import pyrealsense2 as rs
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QPlainTextEdit, QPushButton, QApplication

from domain.calibration import assign_grid_ids, build_positions_with_thresholds, update_config_leds
from domain.realsense_utils import (
    detect_led_centroids, merge_close_centroids, apply_roi_mask, save_debug_detection_image,
    ir_bytes_to_image, yuyv_to_bgr,
)
from engine.streams import (
    match_profile, disable_ir_emitter, enable_auto_exposure, get_sensors_for_device,
    capture_synced_frame_pair,
)
from engine.led_panel import LEDPanel


class CalibrationPage(QWidget):
    calibration_done = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view)
        self.run_button = QPushButton("Run Calibration")
        self.run_button.clicked.connect(self._on_run_clicked)
        layout.addWidget(self.run_button)
        self._pending_args = None

    def _log(self, message):
        self.log_view.appendPlainText(message)
        # _run_calibration runs synchronously on the GUI thread (one blocking
        # procedure a human watches once per rig setup, not worth a full
        # QThread rewrite) - processEvents() lets Qt repaint the log between
        # steps instead of the whole log appearing at once when it returns.
        QApplication.processEvents()

    def set_context(self, ctx, device_serial, ir_resolution, ir_fps, color_resolution, color_fps,
                    ir_roi, rgb_roi, config_path, camera_name, output_dir,
                    settle_frames=15, min_blob_area=20, neighborhood_size=5, row_gap_px=15,
                    min_acceptable_contrast=20):
        self._pending_args = dict(
            ctx=ctx, device_serial=device_serial, ir_resolution=ir_resolution, ir_fps=ir_fps,
            color_resolution=color_resolution, color_fps=color_fps, ir_roi=ir_roi, rgb_roi=rgb_roi,
            config_path=config_path, camera_name=camera_name, output_dir=output_dir,
            settle_frames=settle_frames, min_blob_area=min_blob_area,
            neighborhood_size=neighborhood_size, row_gap_px=row_gap_px,
            min_acceptable_contrast=min_acceptable_contrast,
        )

    def _on_run_clicked(self):
        if self._pending_args is None:
            return
        self.run_button.setEnabled(False)
        try:
            self._run_calibration(**self._pending_args)
        except Exception as exc:
            self._log("Calibration failed: {}".format(exc))
        finally:
            self.run_button.setEnabled(True)

    def _run_calibration(self, ctx, device_serial, ir_resolution, ir_fps, color_resolution, color_fps,
                          ir_roi, rgb_roi, config_path, camera_name, output_dir, settle_frames,
                          min_blob_area, neighborhood_size, row_gap_px, min_acceptable_contrast):
        stereo_sensor, rgb_sensor = get_sensors_for_device(ctx, device_serial)
        ir_profile = match_profile(stereo_sensor, rs.stream.infrared, rs.format.y8, *ir_resolution, ir_fps)
        color_profile = match_profile(rgb_sensor, rs.stream.color, rs.format.yuyv, *color_resolution, color_fps)

        if not disable_ir_emitter(stereo_sensor):
            self._log("WARNING: emitter_enabled not supported - confirm the IR projector is off manually.")
        if not enable_auto_exposure(rgb_sensor):
            self._log("WARNING: enable_auto_exposure not supported - confirm RGB auto-exposure is on manually.")

        def turn_on_all_leds():
            self._log("Turning on all LEDs...")
            LEDPanel.stop()
            LEDPanel.all_leds_on()
            time.sleep(0.5)  # let the panel actually reach full brightness

        # Same capture mechanism led_calibration.py actually used (raw
        # per-sensor open/start, counting real callback deliveries to confirm
        # settling) - NOT the rs.pipeline()-based ContinuousCapture used
        # elsewhere in this app for continuous streaming, which produced
        # spurious zero-LEDs-detected results when substituted in here.
        try:
            ir_on_raw, rgb_on_raw = capture_synced_frame_pair(
                stereo_sensor, ir_profile, rgb_sensor, color_profile,
                on_both_streaming=turn_on_all_leds,
                settle_frames=settle_frames,
            )
        finally:
            # Cleanup-only call: LEDPanel.all_leds_off() now raises if the
            # panel command itself keeps failing (see LEDPanel._run). Swallow
            # it here rather than letting it replace whatever exception the
            # try block above may have raised (a finally-block exception
            # always masks one from the try block in Python) - still surface
            # it, since the operator needs to know to check the panel by hand.
            try:
                LEDPanel.all_leds_off()
            except Exception as exc:
                self._log("WARNING: failed to turn LEDs off during cleanup: {}".format(exc))

        self._log("Turning LED panel off, capturing OFF-state frames...")
        ir_off_raw, rgb_off_raw = capture_synced_frame_pair(
            stereo_sensor, ir_profile, rgb_sensor, color_profile,
            on_both_streaming=None,
            settle_frames=settle_frames,
        )

        ir_on_image = ir_bytes_to_image(ir_on_raw, *ir_resolution)
        rgb_on_image = yuyv_to_bgr(rgb_on_raw, *color_resolution)
        ir_off_image = ir_bytes_to_image(ir_off_raw, *ir_resolution)
        rgb_off_image = yuyv_to_bgr(rgb_off_raw, *color_resolution)

        ir_masked = apply_roi_mask(ir_on_image, ir_roi)
        rgb_masked = apply_roi_mask(rgb_on_image, rgb_roi)

        self._log("Detecting LEDs in IR frame...")
        ir_centroids, ir_otsu = detect_led_centroids(ir_masked, None, min_blob_area)
        ir_centroids = merge_close_centroids(ir_centroids)
        self._log("Detected {} LED(s) in IR (Otsu threshold {}).".format(len(ir_centroids), ir_otsu))
        # Saved BEFORE assign_grid_ids, which raises on zero detections - this
        # is exactly the case where seeing the masked crop matters most, so it
        # must not be skipped by that exception.
        ir_debug_path = os.path.join(output_dir, "debug_ir_detection.png")
        save_debug_detection_image(ir_masked, ir_centroids, ir_debug_path)
        self._log("Saved debug image (masked frame + detected LEDs circled): {}".format(ir_debug_path))
        ir_positions, ir_row_layout = assign_grid_ids(ir_centroids, row_gap_px)

        self._log("Detecting LEDs in RGB frame...")
        rgb_centroids, rgb_otsu = detect_led_centroids(rgb_masked, None, min_blob_area)
        rgb_centroids = merge_close_centroids(rgb_centroids)
        self._log("Detected {} LED(s) in RGB (Otsu threshold {}).".format(len(rgb_centroids), rgb_otsu))
        rgb_debug_path = os.path.join(output_dir, "debug_rgb_detection.png")
        save_debug_detection_image(rgb_masked, rgb_centroids, rgb_debug_path)
        self._log("Saved debug image (masked frame + detected LEDs circled): {}".format(rgb_debug_path))
        rgb_positions, rgb_row_layout = assign_grid_ids(rgb_centroids, row_gap_px)

        if ir_row_layout != rgb_row_layout:
            self._log(
                "WARNING: IR row layout {} != RGB row layout {} - led_id may not match the same "
                "physical LED in both dicts.".format(ir_row_layout, rgb_row_layout)
            )

        self._log("Computing per-LED on/off/threshold values...")
        ir_positions = build_positions_with_thresholds(ir_positions, ir_on_image, ir_off_image, neighborhood_size)
        rgb_positions = build_positions_with_thresholds(rgb_positions, rgb_on_image, rgb_off_image, neighborhood_size)

        for label, positions in (("IR", ir_positions), ("RGB", rgb_positions)):
            weakest_id, weakest_contrast = min(
                ((led_id, vals[2] - vals[3]) for led_id, vals in positions.items()),
                key=lambda pair: pair[1],
            )
            self._log("{} weakest LED contrast: led_id={} on-off={:.2f}".format(label, weakest_id, weakest_contrast))
            if weakest_contrast < min_acceptable_contrast:
                self._log("  WARNING: this LED's on/off gap is small - its threshold may be unreliable.")

        update_config_leds(config_path, camera_name, ir_positions, ir_resolution, rgb_positions, color_resolution)
        self._log("Saved {} LED positions per sensor to {}".format(len(ir_positions), config_path))
        self.calibration_done.emit()
