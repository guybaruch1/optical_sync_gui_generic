"""QThread wrapper for the Stream Config page's live pairing-quality
preview: streams IR+RGB continuously via ContinuousCapture, burns a
bundle/frame-number/timestamp/delta overlay onto the IR frame, and prints
the same information to the console - lets you sanity-check pairing
quality for a chosen resolution/fps before committing to it in the wizard.
Deliberately separate from engine.session_engine.SessionEngineThread: this
has nothing to do with metrics/TestSession, it's a lightweight, read-only
preview.
"""

from PySide6.QtCore import QThread, Signal

from engine.streams import ContinuousCapture, disable_ir_emitter, enable_auto_exposure, get_sensors_for_device
from domain.realsense_utils import draw_bundle_overlay


class StreamPreviewThread(QThread):
    frame_ready = Signal(object)
    error = Signal(str)

    def __init__(self, ctx, device_serial, ir_resolution, ir_fps, color_resolution, color_fps,
                 display_stride=10, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.device_serial = device_serial
        self.ir_resolution = ir_resolution
        self.ir_fps = ir_fps
        self.color_resolution = color_resolution
        self.color_fps = color_fps
        self.display_stride = display_stride
        self._stop_requested = False
        self._capture = None

    def request_stop(self):
        self._stop_requested = True

    def run(self):
        try:
            stereo_sensor, rgb_sensor = get_sensors_for_device(self.ctx, self.device_serial)
            if not disable_ir_emitter(stereo_sensor):
                self.error.emit(
                    "This sensor/firmware does not expose emitter_enabled - confirm the IR projector is off manually."
                )
            if not enable_auto_exposure(rgb_sensor):
                self.error.emit(
                    "This sensor/firmware does not expose enable_auto_exposure - confirm RGB auto-exposure is on manually."
                )

            self._capture = ContinuousCapture(self.ir_resolution, self.ir_fps, self.color_resolution, self.color_fps)
            self._capture.start()

            bundle_index = 0
            for ir_image, rgb_image, ir_ts_us, rgb_ts_us, ir_frame_number, color_frame_number \
                    in self._capture.frames_with_diagnostics():
                if self._stop_requested:
                    break

                if bundle_index % self.display_stride == 0:
                    delta_us = ir_ts_us - rgb_ts_us
                    print(
                        "Bundle {:>6} | IR Frame {:>6} | Color Frame {:>6} | "
                        "IR Timestamp {:>14.0f} | Color Timestamp {:>14.0f} | Delta {:>7.1f} us".format(
                            bundle_index, ir_frame_number, color_frame_number, ir_ts_us, rgb_ts_us, delta_us,
                        )
                    )
                    overlay_image = draw_bundle_overlay(
                        ir_image, bundle_index, ir_frame_number, color_frame_number,
                        ir_ts_us, rgb_ts_us, delta_us,
                    )
                    self.frame_ready.emit(overlay_image)

                bundle_index += 1
        except Exception as exc:  # surfaced to the UI rather than crashing the worker thread silently
            self.error.emit(str(exc))
        finally:
            if self._capture is not None:
                self._capture.stop()
