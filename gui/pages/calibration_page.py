"""Wizard step 4: runs LED calibration in-app (same steps as
optical_sync_poc_/led_calibration.py's main()), logging progress into a
QPlainTextEdit instead of print().

Generalized from the old hardcoded IR-sensor/RGB-sensor version to the
generic pick_a/pick_b picks the Stream Select page now produces - same
capture/decode adaptation as gui/pages/roi_select_page.py (see that file's
module docstring for why capture_synced_frame_pair over the groups list
replaces the old raw stereo/rgb-sensor signature). Debug detection image
filenames and log/warning text now use each pick's own slug/label (e.g.
"infrared1"/"Infrared 1") instead of the old hardcoded "ir"/"rgb", so two
different stream-pair calibration runs on the same camera don't clobber
each other's debug PNGs, and update_config_leds writes per-stream-pair
slug-keyed blocks into config.yaml (domain/calibration.py, Task 10).

set_context() also mints a fresh timestamped output/calibration_<timestamp>/
folder (domain.run_output.create_run_dir) once per page visit, so a later
calibration run on this same camera doesn't overwrite an earlier one's debug
PNGs. This is once per VISIT, not once per "Run Calibration" click - clicking
Run multiple times in one visit (e.g. while tuning ROI) intentionally shares
that same folder.

The debug detection PNG is numbered using domain.calibration.
build_grid_positions's row-major order (index i IS led_id i, the same ID
config.yaml/Threshold Tuning/Live Session actually use for that LED), not
detect_led_centroids' own raw, arbitrary contour-scan order - an earlier
version numbered the raw order directly, which didn't correspond to any
real LED ID at all despite looking superficially grid-like. Falls back to
that raw order only when zero LEDs were detected (build_grid_positions
raises in that case, same as assign_grid_ids/centroids_in_grid_order) -
there's no real grid order to show yet, but the debug image still needs to
exist for exactly that failure case.

self.last_calibration_result retains each stream's already-captured on/off
frames and Otsu-chosen detection threshold after a successful run (None
before any run, or if the last run failed) - read by MainWindow to give
gui/pages/threshold_tuning_page.py's LED Detection Threshold Tuning section
a manual detection-threshold override to work with, reusing these same
frames rather than capturing its own."""

import os
import time

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QPlainTextEdit, QPushButton, QApplication

from domain.calibration import build_grid_positions, update_config_leds
from domain.run_output import create_run_dir
from domain.realsense_utils import (
    detect_led_centroids, merge_close_centroids, crop_to_roi, save_debug_detection_image,
    decode_frame,
)
from engine.streams import (
    find_device_by_serial, resolve_and_group, capture_synced_frame_pair, stream_slug, group_for_pick,
)
from engine.led_panel import LEDPanel
from engine.dual_panel_control import turn_all_leds_on, turn_all_leds_off, switched_to_stream_panel
from gui.pages.roi_select_page import stream_label, _apply_camera_controls


class CalibrationPage(QWidget):
    calibration_done = Signal()
    back_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view)
        self.run_button = QPushButton("Run Calibration")
        self.run_button.clicked.connect(self._on_run_clicked)
        layout.addWidget(self.run_button)
        self.back_button = QPushButton("Back")
        self.back_button.clicked.connect(self.back_requested.emit)
        layout.addWidget(self.back_button)
        self._pending_args = None
        # None until a run completes successfully - read by MainWindow to
        # thread the already-captured on/off frames (plus the Otsu threshold
        # each stream found) into ThresholdTuningPage's LED Detection
        # Threshold Tuning section, so it can offer a manual override
        # without needing its own camera capture. Set only once, as the
        # very last statement before calibration_done fires (see
        # _run_calibration) - a later failed run leaves this at whatever
        # the last SUCCESSFUL run left it, never partially overwritten.
        self.last_calibration_result = None

    def _log(self, message):
        self.log_view.appendPlainText(message)
        # _run_calibration runs synchronously on the GUI thread (one blocking
        # procedure a human watches once per rig setup, not worth a full
        # QThread rewrite) - processEvents() lets Qt repaint the log between
        # steps instead of the whole log appearing at once when it returns.
        QApplication.processEvents()

    def set_context(self, ctx, device_serial, pick_a, pick_b, camera_controls, stream_a_roi, stream_b_roi,
                     config_path, camera_name, output_root,
                     settle_frames=15, min_blob_area=20, neighborhood_size=5, row_gap_px=15,
                     min_acceptable_contrast=20, dual_panel_config=None):
        # Mints ONE fresh timestamped output/calibration_<timestamp>/ folder
        # per page visit (i.e. per set_context() call), not per "Run
        # Calibration" click - a user re-clicking Run a few times while
        # tuning ROI/threshold within one visit shares that one folder
        # rather than scattering a throwaway subfolder per click.
        output_dir = create_run_dir(output_root, "calibration")
        self._pending_args = dict(
            ctx=ctx, device_serial=device_serial, pick_a=pick_a, pick_b=pick_b,
            camera_controls=camera_controls, stream_a_roi=stream_a_roi, stream_b_roi=stream_b_roi,
            config_path=config_path, camera_name=camera_name, output_dir=output_dir,
            settle_frames=settle_frames, min_blob_area=min_blob_area,
            neighborhood_size=neighborhood_size, row_gap_px=row_gap_px,
            min_acceptable_contrast=min_acceptable_contrast, dual_panel_config=dual_panel_config,
        )

    def _on_run_clicked(self):
        if self._pending_args is None:
            return
        self.run_button.setEnabled(False)
        # _run_calibration pumps processEvents() via _log() while it runs,
        # so Back is genuinely clickable mid-run unless disabled here too -
        # there's no clean way to cancel a run partway through, so this just
        # prevents leaving mid-run rather than attempting that.
        self.back_button.setEnabled(False)
        try:
            self._run_calibration(**self._pending_args)
        except Exception as exc:
            self._log("Calibration failed: {}".format(exc))
        finally:
            self.run_button.setEnabled(True)
            self.back_button.setEnabled(True)

    def _run_calibration(self, ctx, device_serial, pick_a, pick_b, camera_controls, stream_a_roi, stream_b_roi,
                          config_path, camera_name, output_dir, settle_frames,
                          min_blob_area, neighborhood_size, row_gap_px, min_acceptable_contrast,
                          dual_panel_config):
        device = find_device_by_serial(ctx, device_serial)
        groups = resolve_and_group(device, pick_a, pick_b)

        for warning in _apply_camera_controls(groups, camera_controls, pick_a, pick_b):
            self._log(warning)

        if dual_panel_config is not None:
            # 2 physically separate panels, one per stream - capturing
            # both streams' on/off frames from one simultaneous moment
            # needs both panels lit in perfect sync, which isn't reliable
            # (and isn't even needed: calibration never compares timing
            # across streams the way Live Session does). Simpler and more
            # robust: fully calibrate one stream - panel on, capture, panel
            # off, capture - then the other, only switching the Acroname
            # hub once per stream instead of repeatedly mid-capture.
            image_a_on, image_a_off = self._capture_on_off_for_stream(
                groups, pick_a, "stream_a", dual_panel_config, settle_frames)
            image_b_on, image_b_off = self._capture_on_off_for_stream(
                groups, pick_b, "stream_b", dual_panel_config, settle_frames)
        else:
            def turn_on_all_leds():
                self._log("Turning on all LEDs...")
                turn_all_leds_on(dual_panel_config)
                time.sleep(0.5)  # let the panel actually reach full brightness

            # Same capture mechanism led_calibration.py actually used (raw
            # per-sensor open/start, counting real callback deliveries to
            # confirm settling) - NOT the rs.pipeline()-based
            # ContinuousCapture used elsewhere in this app for continuous
            # streaming, which produced spurious zero-LEDs-detected
            # results when substituted in here. See roi_select_page.py's
            # matching comment.
            try:
                frames_on = capture_synced_frame_pair(
                    groups,
                    on_both_streaming=turn_on_all_leds,
                    settle_frames=settle_frames,
                )
            finally:
                # Cleanup-only call: LEDPanel.all_leds_off() now raises if
                # the panel command itself keeps failing (see
                # LEDPanel._run). Swallow it here rather than letting it
                # replace whatever exception the try block above may have
                # raised (a finally-block exception always masks one from
                # the try block in Python) - still surface it, since the
                # operator needs to know to check the panel by hand.
                try:
                    turn_all_leds_off(dual_panel_config)
                except Exception as exc:
                    self._log("WARNING: failed to turn LEDs off during cleanup: {}".format(exc))

            self._log("Turning LED panel off, capturing OFF-state frames...")
            frames_off = capture_synced_frame_pair(
                groups,
                on_both_streaming=None,
                settle_frames=settle_frames,
            )

            def decode(frames, pick):
                return decode_frame(
                    frames[(pick["stream_type"], pick["stream_index"])],
                    pick["format"], pick["width"], pick["height"],
                )

            image_a_on = decode(frames_on, pick_a)
            image_b_on = decode(frames_on, pick_b)
            image_a_off = decode(frames_off, pick_a)
            image_b_off = decode(frames_off, pick_b)

        label_a, label_b = stream_label(pick_a), stream_label(pick_b)
        slug_a, slug_b = stream_slug(pick_a), stream_slug(pick_b)
        res_a, res_b = (pick_a["width"], pick_a["height"]), (pick_b["width"], pick_b["height"])

        # Cropped, not just masked - detect_led_centroids' Otsu threshold
        # needs a histogram dominated by the LEDs and their own gaps, not
        # diluted by a huge sea of masked-out zero pixels from the rest of
        # the full frame (that was splitting "zero background" vs
        # "everything inside the ROI" as the two classes, merging the
        # entire LED grid into one blob instead of separating individual
        # LEDs from the gaps between them).
        cropped_a = crop_to_roi(image_a_on, stream_a_roi)
        cropped_b = crop_to_roi(image_b_on, stream_b_roi)

        self._log("Detecting LEDs in {} frame...".format(label_a))
        centroids_a, otsu_a = detect_led_centroids(cropped_a, None, min_blob_area)
        centroids_a = merge_close_centroids(centroids_a)
        self._log("Detected {} LED(s) in {} (Otsu threshold {}).".format(len(centroids_a), label_a, otsu_a))
        debug_path_a = os.path.join(output_dir, "debug_{}_detection.png".format(slug_a))
        try:
            # build_grid_positions numbers the debug image in the SAME
            # row-major order assign_grid_ids itself assigns as led_id -
            # NOT detect_led_centroids' raw, arbitrary contour-scan order
            # (an earlier version drew that raw order directly, which bore
            # no relation to the actual led_id config.yaml/Threshold
            # Tuning/Live Session use for that same LED, while still
            # happening to look grid-like enough to read as "wrong" rather
            # than obviously arbitrary).
            positions_a, row_layout_a, debug_centroids_a = build_grid_positions(
                centroids_a, stream_a_roi, image_a_on, image_a_off, row_gap_px, neighborhood_size,
            )
        except RuntimeError:
            # No LEDs detected at all - there's no real grid order to
            # show yet, but a debug image (in raw, arbitrary detection
            # order) still needs to exist for exactly this failure case,
            # where seeing the cropped ROI/threshold matters most.
            save_debug_detection_image(cropped_a, centroids_a, debug_path_a)
            raise
        save_debug_detection_image(cropped_a, debug_centroids_a, debug_path_a)
        self._log("Saved debug image (cropped ROI + detected LEDs circled, numbered by grid ID): {}".format(debug_path_a))

        self._log("Detecting LEDs in {} frame...".format(label_b))
        centroids_b, otsu_b = detect_led_centroids(cropped_b, None, min_blob_area)
        centroids_b = merge_close_centroids(centroids_b)
        self._log("Detected {} LED(s) in {} (Otsu threshold {}).".format(len(centroids_b), label_b, otsu_b))
        debug_path_b = os.path.join(output_dir, "debug_{}_detection.png".format(slug_b))
        try:
            positions_b, row_layout_b, debug_centroids_b = build_grid_positions(
                centroids_b, stream_b_roi, image_b_on, image_b_off, row_gap_px, neighborhood_size,
            )
        except RuntimeError:
            save_debug_detection_image(cropped_b, centroids_b, debug_path_b)
            raise
        save_debug_detection_image(cropped_b, debug_centroids_b, debug_path_b)
        self._log("Saved debug image (cropped ROI + detected LEDs circled, numbered by grid ID): {}".format(debug_path_b))

        if row_layout_a != row_layout_b:
            self._log(
                "WARNING: {} row layout {} != {} row layout {} - led_id may not match the same "
                "physical LED in both dicts.".format(label_a, row_layout_a, label_b, row_layout_b)
            )

        for label, positions in ((label_a, positions_a), (label_b, positions_b)):
            weakest_id, weakest_contrast = min(
                ((led_id, vals[2] - vals[3]) for led_id, vals in positions.items()),
                key=lambda pair: pair[1],
            )
            self._log("{} weakest LED contrast: led_id={} on-off={:.2f}".format(label, weakest_id, weakest_contrast))
            if weakest_contrast < min_acceptable_contrast:
                self._log("  WARNING: this LED's on/off gap is small - its threshold may be unreliable.")

        update_config_leds(config_path, camera_name, slug_a, positions_a, res_a, slug_b, positions_b, res_b)
        self._log("Saved {} LED positions per stream ({}={}, {}={}) to {}".format(
            len(positions_a), label_a, slug_a, label_b, slug_b, config_path
        ))
        # Retained for ThresholdTuningPage's LED Detection Threshold Tuning
        # section - lets it offer a manual detection-threshold override
        # using these SAME already-captured frames, with no new camera
        # capture of its own. Set as the very last statement before
        # calibration_done fires - see __init__'s comment on this attribute.
        self.last_calibration_result = dict(
            image_a_on=image_a_on, image_a_off=image_a_off,
            image_b_on=image_b_on, image_b_off=image_b_off,
            stream_a_otsu_threshold=int(round(otsu_a)), stream_b_otsu_threshold=int(round(otsu_b)),
            min_blob_area=min_blob_area, row_gap_px=row_gap_px, neighborhood_size=neighborhood_size,
        )
        self.calibration_done.emit()

    def _capture_on_off_for_stream(self, groups, pick, stream_name, dual_panel_config, settle_frames):
        # Only this stream's own sensor needs to be opened/started - no
        # need to involve the other stream's sensor, since calibration
        # never compares timing across streams. Both captures happen
        # inside the SAME switched_to_stream_panel block, so the Acroname
        # hub is only switched once for this whole stream, not once per
        # on/off toggle.
        label = stream_label(pick)
        group = group_for_pick(groups, pick)
        with switched_to_stream_panel(dual_panel_config, stream_name):
            self._log("Turning on {} LEDs...".format(label))
            LEDPanel.stop()
            LEDPanel.all_leds_on()
            time.sleep(0.5)  # let the panel actually reach full brightness
            try:
                frames_on = capture_synced_frame_pair(group, settle_frames=settle_frames)
            finally:
                try:
                    LEDPanel.all_leds_off()
                except Exception as exc:
                    self._log("WARNING: failed to turn {} LEDs off during cleanup: {}".format(label, exc))

            self._log("Turning {} LEDs off, capturing OFF-state frame...".format(label))
            frames_off = capture_synced_frame_pair(group, settle_frames=settle_frames)

        image_on = decode_frame(
            frames_on[(pick["stream_type"], pick["stream_index"])],
            pick["format"], pick["width"], pick["height"],
        )
        image_off = decode_frame(
            frames_off[(pick["stream_type"], pick["stream_index"])],
            pick["format"], pick["width"], pick["height"],
        )
        return image_on, image_off
