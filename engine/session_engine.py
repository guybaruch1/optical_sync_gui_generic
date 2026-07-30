"""Thin QThread adapter: wires real hardware (engine.streams,
engine.led_panel) into engine.acquisition_loop.AcquisitionLoop and
translates its plain-Python callbacks into Qt signals.

Deliberately as small as possible - all the actual logic (frame-pair
processing, metric computation, session buffering) already lives in
AcquisitionLoop/TestSession/Metric, which are unit-tested without Qt or
hardware. This class exists only so that logic can run on a background
thread and reach the UI safely.
"""

from PySide6.QtCore import QThread, Signal

from engine.acquisition_loop import AcquisitionLoop, AcquisitionCallbacks
from engine.streams import (
    ContinuousCapture, find_device_by_serial, resolve_and_group,
    set_emitter_enabled, enable_auto_exposure, set_manual_exposure,
)
from engine.led_panel import LEDPanel
from domain.realsense_utils import sample_all_neighborhood_brightness


class SessionEngineThread(QThread):
    # 4th payload item is (stream_a_on_mask, stream_b_on_mask) - a snapshot copy taken
    # synchronously on this thread at the same pair_index as the image, or
    # None if position_gap_metric wasn't provided. Bundling it into the
    # signal (rather than having the GUI thread read
    # position_gap_metric.last_stream_a_on_mask/last_stream_b_on_mask later) is
    # deliberate: those attributes are overwritten every pair by this
    # background thread, which keeps running unblocked while a queued
    # cross-thread signal waits to be processed on the GUI thread - by the
    # time a slot actually ran, a live read was already many pairs stale,
    # showing the on/off overlay offset from the frame it was drawn on.
    frame_ready = Signal(str, object, int, object)
    row_ready = Signal(dict)
    stats_ready = Signal(dict)
    session_finished = Signal(list)
    error = Signal(str)

    def __init__(self, ctx, device_serial, pick_a, pick_b, camera_controls,
                 test_session, stream_a_xy=None, stream_b_xy=None, neighborhood_size=5,
                 scan_direction=None, switch_time_ms=None,
                 display_stride=10, position_gap_metric=None, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.device_serial = device_serial
        self.pick_a = pick_a
        self.pick_b = pick_b
        self.camera_controls = camera_controls
        self.test_session = test_session
        self.stream_a_xy = stream_a_xy
        self.stream_b_xy = stream_b_xy
        self.neighborhood_size = neighborhood_size
        self.scan_direction = scan_direction
        self.switch_time_ms = switch_time_ms
        self.display_stride = display_stride
        self.position_gap_metric = position_gap_metric
        self._stop_requested = False
        self._capture = None
        self._start_time = None

    def request_stop(self):
        self._stop_requested = True

    def _frame_pairs_with_brightness(self):
        """Adapts ContinuousCapture.frames()'s 4-tuple (image, image, ts, ts)
        into the 6-tuple AcquisitionLoop/FramePairSample need, by sampling
        brightness at each calibrated LED position. This is deliberately done
        here, not inside ContinuousCapture itself: ContinuousCapture is a
        generic hardware-capture primitive with no notion of LED positions or
        metrics (gui/pages/calibration_page.py, a later task, consumes its raw
        4-tuple directly for exactly that reason)."""
        for stream_a_image, stream_b_image, stream_a_ts_us, stream_b_ts_us in self._capture.frames():
            stream_a_bright = (
                sample_all_neighborhood_brightness(stream_a_image, self.stream_a_xy, self.neighborhood_size)
                if self.stream_a_xy is not None else None
            )
            stream_b_bright = (
                sample_all_neighborhood_brightness(stream_b_image, self.stream_b_xy, self.neighborhood_size)
                if self.stream_b_xy is not None else None
            )
            yield stream_a_image, stream_b_image, stream_a_ts_us, stream_b_ts_us, stream_a_bright, stream_b_bright

    def run(self):
        import time

        try:
            device = find_device_by_serial(self.ctx, self.device_serial)
            groups = resolve_and_group(device, self.pick_a, self.pick_b)
            # Applies the ONE global self.camera_controls dict (from Stream Select)
            # uniformly to every resolved sensor group - Stream Select no longer
            # configures emitter/exposure/gain per resolved sensor group, just once
            # for both streams together. Mirrors gui/pages/roi_select_page.py's
            # _apply_camera_controls - duplicated here rather than imported since
            # this is hardware-thread code, not GUI code.
            for sensor, _profiles in groups:
                if self.camera_controls["emitter_enabled"] is not None:
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
                    if not set_manual_exposure(sensor, self.camera_controls["exposure"], self.camera_controls["gain"]):
                        self.error.emit(
                            "WARNING: manual exposure/gain not supported on sensor - confirm "
                            "exposure settings manually."
                        )

            # Puts the panel into single-LED scanning mode at the configured
            # speed/direction and actually starts it moving - ported from
            # pipeline_sync_test_diff.py's main(), which does this immediately
            # before its capture loop. Without this the panel never scans at
            # all during a live session (it's left in whatever mode
            # calibration/ROI selection last put it in, typically off), so
            # PositionGapMetric would only ever see misses.
            if self.switch_time_ms is not None:
                LEDPanel.stop()
                LEDPanel.response_time_measurement_mode()
                LEDPanel.set_direction_single(self.scan_direction if self.scan_direction is not None else 1)
                LEDPanel.set_speed_ms(self.switch_time_ms)
                LEDPanel.start()

            self._capture = ContinuousCapture(self.device_serial, self.pick_a, self.pick_b)
            self._capture.start()
            self._start_time = time.time()

            def on_frames(stream_a_image, stream_b_image, pair_index):
                # Read+copy here, synchronously, still within the same
                # acquisition-loop iteration that just processed this exact
                # pair_index (process_pair() runs immediately before
                # on_frames() in AcquisitionLoop.run_until_stopped) - the
                # copy is what makes it safe to read on the GUI thread
                # later, since last_stream_a_on_mask/last_stream_b_on_mask will keep
                # changing underneath it on this thread in the meantime.
                if self.position_gap_metric is not None:
                    stream_a_mask = self.position_gap_metric.last_stream_a_on_mask
                    stream_b_mask = self.position_gap_metric.last_stream_b_on_mask
                    stream_a_mask = stream_a_mask.copy() if stream_a_mask is not None else None
                    stream_b_mask = stream_b_mask.copy() if stream_b_mask is not None else None
                else:
                    stream_a_mask = stream_b_mask = None
                self.frame_ready.emit("stream_a", stream_a_image, pair_index, stream_a_mask)
                self.frame_ready.emit("stream_b", stream_b_image, pair_index, stream_b_mask)

            def on_row(row):
                self.row_ready.emit(row)

            def on_stats(stats):
                self.stats_ready.emit(stats)

            callbacks = AcquisitionCallbacks(on_frames=on_frames, on_row=on_row, on_stats=on_stats)
            loop = AcquisitionLoop(
                self._frame_pairs_with_brightness(), self.test_session, callbacks,
                display_stride=self.display_stride,
            )
            rows = loop.run_until_stopped(
                is_stop_requested=lambda: self._stop_requested,
                elapsed_s_fn=lambda: time.time() - self._start_time,
            )
            self.session_finished.emit(rows)
        except Exception as exc:  # surfaced to the UI rather than crashing the worker thread silently
            self.error.emit(str(exc))
        finally:
            if self._capture is not None:
                self._capture.stop()
            # Cleanup-only call: LEDPanel.stop() now raises if the panel command
            # itself keeps failing (see LEDPanel._run). This runs with no
            # surrounding try/except in QThread.run(), so let a cleanup failure
            # reach the UI via the error signal instead of crashing the thread
            # unhandled or masking whatever exception the try block above raised.
            try:
                LEDPanel.stop()
            except Exception as exc:
                self.error.emit("Failed to stop LED panel during cleanup: {}".format(exc))
