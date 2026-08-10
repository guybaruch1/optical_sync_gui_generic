"""QThread wrapper for Stream Select's live pairing-quality preview:
streams the two picked streams continuously via ContinuousCapture, burns
a bundle/frame-number/timestamp/delta overlay onto Stream A's frame, and
prints the same info to the console."""

from PySide6.QtCore import QThread, Signal

from engine.streams import ContinuousCapture
from domain.realsense_utils import draw_bundle_overlay


class StreamPreviewThread(QThread):
    frame_ready = Signal(object)
    error = Signal(str)

    def __init__(self, ctx, device_serial, pick_a, pick_b, display_stride=10,
                 enable_depth_for_ir_sync=True, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.device_serial = device_serial
        self.pick_a = pick_a
        self.pick_b = pick_b
        self.display_stride = display_stride
        # Kept in step with SessionEngineThread's/ThresholdPreviewThread's own
        # setting so this pairing-quality preview shows the same IR/RGB sync
        # fix the real run downstream will use - see
        # ContinuousCapture._depth_sync_stream.
        self.enable_depth_for_ir_sync = enable_depth_for_ir_sync
        self._stop_requested = False
        self._capture = None

    def request_stop(self):
        self._stop_requested = True

    def run(self):
        try:
            self._capture = ContinuousCapture(
                self.device_serial, self.pick_a, self.pick_b,
                enable_depth_for_ir_sync=self.enable_depth_for_ir_sync,
            )
            self._capture.start()

            bundle_index = 0
            for image_a, image_b, ts_a, ts_b, num_a, num_b in self._capture.frames_with_diagnostics():
                if self._stop_requested:
                    break

                if bundle_index % self.display_stride == 0:
                    delta_us = ts_a - ts_b
                    print(
                        "Bundle {:>6} | Stream A Frame {:>6} | Stream B Frame {:>6} | "
                        "Stream A Timestamp {:>14.0f} | Stream B Timestamp {:>14.0f} | Delta {:>7.1f} us".format(
                            bundle_index, num_a, num_b, ts_a, ts_b, delta_us,
                        )
                    )
                    overlay_image = draw_bundle_overlay(image_a, bundle_index, num_a, num_b, ts_a, ts_b, delta_us)
                    self.frame_ready.emit(overlay_image)

                bundle_index += 1
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            if self._capture is not None:
                self._capture.stop()
