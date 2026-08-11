"""QThread for the Threshold Tuning wizard page: continuous video + per-LED
brightness sampling for both streams, with NO TestSession/AcquisitionLoop/
metrics/CSV - that machinery exists to drive a timed, recorded test run,
which this page deliberately isn't. Unlike engine.session_engine's
SessionEngineThread (which emits a precomputed on/off mask), this thread
emits raw per-LED brightness so the GUI page can recompute the on/off mask
instantly from whatever its live threshold-fraction spinbox currently reads,
without needing to restart capture every time the user nudges it."""

import pyrealsense2 as rs
from PySide6.QtCore import QThread, Signal

from engine.streams import (
    ContinuousCapture, find_device_by_serial, resolve_and_group,
    set_emitter_enabled, enable_auto_exposure, set_manual_exposure,
)
from engine.dual_panel_control import start_scanning, stop_scanning
from domain.realsense_utils import sample_all_neighborhood_brightness, safe_neighborhood_size


class ThresholdPreviewThread(QThread):
    frame_ready = Signal(str, object, int, object)  # (stream_name, image, frame_index, brightness)
    error = Signal(str)

    def __init__(self, ctx, device_serial, pick_a, pick_b, camera_controls,
                 stream_a_xy, stream_b_xy, neighborhood_size=5,
                 scan_direction=None, switch_time_ms=None, display_stride=10,
                 dual_panel_config=None, enable_depth_for_ir_sync=True, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.device_serial = device_serial
        self.pick_a = pick_a
        self.pick_b = pick_b
        self.camera_controls = camera_controls
        self.stream_a_xy = stream_a_xy
        self.stream_b_xy = stream_b_xy
        self.dual_panel_config = dual_panel_config
        # Kept in step with SessionEngineThread's own setting so this
        # preview streams the two sensors with the same IR/RGB sync fix the
        # real timed run will use - otherwise the preview could show a
        # different inter-sensor offset than the session it's meant to be
        # previewing. No hardware_reset_before_start counterpart here on
        # purpose: this preview is started/stopped repeatedly while tuning,
        # and an 8s reset per Start would make it unusable.
        self.enable_depth_for_ir_sync = enable_depth_for_ir_sync
        # See SessionEngineThread's identical comment - capped once here at
        # what's actually safe for THIS run's real measured LED spacing.
        self._stream_a_safe_size = safe_neighborhood_size(stream_a_xy, neighborhood_size)
        self._stream_b_safe_size = safe_neighborhood_size(stream_b_xy, neighborhood_size)
        self.scan_direction = scan_direction
        self.switch_time_ms = switch_time_ms
        # How many frame-pairs between video-panel updates - every pair is
        # still received, but brightness sampling + the frame_ready emit are
        # both skipped in between, same reason SessionEngineThread throttles
        # its own frame_ready: doing the (per-LED brightness-sampling +
        # cross-thread Qt signal + overlay-drawing) work on EVERY single
        # frame is exactly the unthrottled pattern that caused a real GUI
        # freeze elsewhere in this app (see CLAUDE.md's Live Session
        # pipeline section) - live-editable via this page's own toolbar
        # spinbox, read fresh each time Start is clicked.
        self.display_stride = display_stride
        self._stop_requested = False
        self._capture = None

    def request_stop(self):
        self._stop_requested = True

    def run(self):
        try:
            device = find_device_by_serial(self.ctx, self.device_serial)
            groups = resolve_and_group(device, self.pick_a, self.pick_b)
            # Same camera-controls application as SessionEngineThread.run() -
            # duplicated rather than imported/shared since this is
            # hardware-thread code, not GUI code (see
            # gui/pages/roi_select_page.py's _apply_camera_controls and
            # engine/session_engine.py's own inline copy of this exact
            # block for the established convention this follows).
            for sensor, profiles in groups:
                group_has_infrared = any(p.stream_type() == rs.stream.infrared for p in profiles)
                if self.camera_controls["emitter_enabled"] is not None and group_has_infrared:
                    if not set_emitter_enabled(sensor, self.camera_controls["emitter_enabled"]):
                        self.error.emit(
                            "WARNING: emitter_enabled not supported on sensor - confirm the "
                            "emitter state manually."
                        )
                if self.camera_controls["auto_exposure"]:
                    if not enable_auto_exposure(sensor):
                        self.error.emit(
                            "WARNING: enable_auto_exposure not supported on sensor - confirm "
                            "auto-exposure manually."
                        )
                else:
                    if not set_manual_exposure(sensor, self.camera_controls["exposure"]):
                        self.error.emit(
                            "WARNING: manual exposure not supported on sensor - confirm "
                            "exposure settings manually."
                        )

            if self.switch_time_ms is not None:
                start_scanning(self.switch_time_ms, self.scan_direction, self.dual_panel_config)

            self._capture = ContinuousCapture(
                self.device_serial, self.pick_a, self.pick_b,
                enable_depth_for_ir_sync=self.enable_depth_for_ir_sync,
            )
            self._capture.start()

            frame_index = 0
            for stream_a_image, stream_b_image, _stream_a_ts_us, _stream_b_ts_us in self._capture.frames():
                if self._stop_requested:
                    break
                if frame_index % self.display_stride == 0:
                    stream_a_bright = sample_all_neighborhood_brightness(
                        stream_a_image, self.stream_a_xy, self._stream_a_safe_size
                    )
                    stream_b_bright = sample_all_neighborhood_brightness(
                        stream_b_image, self.stream_b_xy, self._stream_b_safe_size
                    )
                    self.frame_ready.emit("stream_a", stream_a_image, frame_index, stream_a_bright)
                    self.frame_ready.emit("stream_b", stream_b_image, frame_index, stream_b_bright)
                frame_index += 1
        except Exception as exc:  # surfaced to the UI rather than crashing the worker thread silently
            self.error.emit(str(exc))
        finally:
            if self._capture is not None:
                self._capture.stop()
            try:
                stop_scanning(self.dual_panel_config)
            except Exception as exc:
                self.error.emit("Failed to stop LED panel during cleanup: {}".format(exc))
