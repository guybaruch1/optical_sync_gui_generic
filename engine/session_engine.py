"""Thin QThread adapter: wires real hardware (engine.streams,
engine.led_panel) into engine.acquisition_loop.AcquisitionLoop and
translates its plain-Python callbacks into Qt signals.

Deliberately as small as possible - all the actual logic (frame-pair
processing, metric computation, session buffering) already lives in
AcquisitionLoop/TestSession/Metric, which are unit-tested without Qt or
hardware. This class exists only so that logic can run on a background
thread and reach the UI safely.

Two settings.yaml camera_sync: knobs land here because they change how the
CAMERA is brought up, which shifts the very inter-sensor offset this app
measures:

- enable_depth_for_ir_sync (passed down to ContinuousCapture - see its
  _depth_sync_stream/_build_config). This is the CONFIRMED root-cause fix:
  on real hardware, rs.pipeline() gives no control over the order it
  internally OPENS the two sensors, and that order decides whether IR and
  RGB come out synchronized (RGB-before-IR produces a fixed ~11.3ms offset;
  IR-before-RGB, or co-enabling depth alongside both regardless of order,
  measures the true ~3.5ms). Co-enabling depth matches Intel's documented
  firmware requirement that depth and IR be configured together - see
  CLAUDE.md's "IR/RGB sync depends on stream open order" section for the
  full story, including the earlier color_stream_first/enable-ORDER
  experiment this replaced (proven ineffective: config.enable_stream()
  order does not influence the pipeline's internal open order at all).
  Costs USB bandwidth (confirmed on real hardware to increase frame drops),
  so it stays a settings.yaml-driven toggle rather than an unconditional
  default.
- hardware_reset_before_start/hardware_reset_settle_s (applied in run()
  before anything else touches the device) - kept as an independent manual
  recovery knob (e.g. after a camera left in a bad state by a stuck
  auto-exposure round trip - see engine/streams.py's enable_auto_exposure),
  even though it was also A/B-tested against the ~11.3ms/~3.5ms discrepancy
  above and, on its own, did not change the result.
"""

import os

import cv2
import pyrealsense2 as rs
from PySide6.QtCore import QThread, Signal

from engine.acquisition_loop import AcquisitionLoop, AcquisitionCallbacks
from engine.metrics import is_position_gap_debug_outlier
from engine.streams import (
    ContinuousCapture, find_device_by_serial, resolve_and_group,
    set_emitter_enabled, enable_auto_exposure, set_manual_exposure, exposure_for_group,
)
from engine.dual_panel_control import start_scanning, stop_scanning
from domain.realsense_utils import (
    sample_all_neighborhood_brightness, safe_neighborhood_size,
    draw_led_state_overlay, combine_side_by_side,
)


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
                 display_stride=10, position_gap_metric=None, dual_panel_config=None,
                 enable_depth_for_ir_sync=True, hardware_reset_before_start=False,
                 hardware_reset_settle_s=8.0, output_dir=None,
                 position_gap_outlier_threshold_ms=None, position_gap_outlier_max_snapshots=200,
                 parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.device_serial = device_serial
        self.pick_a = pick_a
        self.pick_b = pick_b
        self.camera_controls = camera_controls
        self.test_session = test_session
        self.dual_panel_config = dual_panel_config
        # Two independent inter-sensor-sync knobs, both settings.yaml-driven
        # (camera_sync:) - see ContinuousCapture._depth_sync_stream and
        # run()'s reset block below for what each actually does and why.
        self.enable_depth_for_ir_sync = enable_depth_for_ir_sync
        self.hardware_reset_before_start = hardware_reset_before_start
        self.hardware_reset_settle_s = hardware_reset_settle_s
        self.stream_a_xy = stream_a_xy
        self.stream_b_xy = stream_b_xy
        self.neighborhood_size = neighborhood_size
        # Capped once here (not per-frame) at what's actually safe for THIS
        # run's real measured LED pixel spacing - see
        # domain.realsense_utils.safe_neighborhood_size's docstring. Computed
        # from the same real xy_positions domain.calibration.
        # build_positions_with_thresholds used at calibration time, so the
        # two can't silently diverge even though they're computed separately.
        self._stream_a_safe_size = (
            safe_neighborhood_size(stream_a_xy, neighborhood_size) if stream_a_xy is not None else neighborhood_size
        )
        self._stream_b_safe_size = (
            safe_neighborhood_size(stream_b_xy, neighborhood_size) if stream_b_xy is not None else neighborhood_size
        )
        self.scan_direction = scan_direction
        self.switch_time_ms = switch_time_ms
        self.display_stride = display_stride
        self.position_gap_metric = position_gap_metric
        # Optical Sync outlier debug images - see _maybe_save_position_gap_outlier.
        # output_dir/position_gap_outlier_threshold_ms are both None-able (rather
        # than required) so tests/tools constructing this class without wiring
        # this feature (e.g. any future direct construction that doesn't care
        # about it) don't need to pass them - _maybe_save_position_gap_outlier
        # is a no-op when either is None.
        self.output_dir = output_dir
        self.position_gap_outlier_threshold_ms = position_gap_outlier_threshold_ms
        self.position_gap_outlier_max_snapshots = position_gap_outlier_max_snapshots
        self._position_gap_outlier_count = 0
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
                sample_all_neighborhood_brightness(stream_a_image, self.stream_a_xy, self._stream_a_safe_size)
                if self.stream_a_xy is not None else None
            )
            stream_b_bright = (
                sample_all_neighborhood_brightness(stream_b_image, self.stream_b_xy, self._stream_b_safe_size)
                if self.stream_b_xy is not None else None
            )
            yield stream_a_image, stream_b_image, stream_a_ts_us, stream_b_ts_us, stream_a_bright, stream_b_bright

    def _maybe_save_position_gap_outlier(self, stream_a_image, stream_b_image, row):
        """Saves a side-by-side IR/RGB debug image (same on/off overlay style
        as gui/pages/live_session_page.py's periodic LED-state snapshots) for
        a pair whose position_gap_ms ("Optical Sync") magnitude crossed
        position_gap_outlier_threshold_ms - lets the operator check by eye
        whether a given outlier reading was a real physical desync or an
        algorithm/detection artifact.

        Runs synchronously on THIS background thread, called from
        AcquisitionLoop's unthrottled on_frame_pair hook (unlike the periodic
        snapshot above, which only sees the throttled display_stride subset
        on_frames gets) - it deliberately does NOT go through a Qt signal,
        since the write itself doesn't need the GUI thread at all and most
        individual outlier pairs wouldn't otherwise land on a displayed
        sample."""
        if self.output_dir is None or self.position_gap_outlier_threshold_ms is None:
            return
        if self.position_gap_metric is None:
            return
        if not is_position_gap_debug_outlier(row, self.position_gap_outlier_threshold_ms):
            return
        if self._position_gap_outlier_count >= self.position_gap_outlier_max_snapshots:
            return

        stream_a_mask = self.position_gap_metric.last_stream_a_on_mask
        stream_b_mask = self.position_gap_metric.last_stream_b_on_mask
        if stream_a_mask is None or stream_b_mask is None:
            return

        pair_index = row["pair_index"]
        path = os.path.join(self.output_dir, "optical_sync_outlier_pair{:05d}.png".format(pair_index))
        stream_a_debug = draw_led_state_overlay(stream_a_image, self.stream_a_xy, stream_a_mask)
        stream_b_debug = draw_led_state_overlay(stream_b_image, self.stream_b_xy, stream_b_mask)
        cv2.imwrite(path, combine_side_by_side(stream_a_debug, stream_b_debug))
        self._position_gap_outlier_count += 1

    def run(self):
        import time

        try:
            if self.hardware_reset_before_start:
                # A manual recovery knob, independent of enable_depth_for_ir_sync
                # above - e.g. clears a camera left with a stuck manual
                # exposure/gain from before engine/streams.py's
                # enable_auto_exposure was fixed to restore both on switch-back.
                # A reset invalidates the current device handle and drops the
                # device off USB for several seconds, so the handle MUST be
                # re-acquired afterwards rather than reused.
                self.error.emit("Hardware-resetting the camera (settling for {:.0f}s)...".format(
                    self.hardware_reset_settle_s
                ))
                find_device_by_serial(self.ctx, self.device_serial).hardware_reset()
                time.sleep(self.hardware_reset_settle_s)

            device = find_device_by_serial(self.ctx, self.device_serial)
            groups = resolve_and_group(device, self.pick_a, self.pick_b)
            # Applies the ONE global self.camera_controls dict (from Stream Select)
            # uniformly to every resolved sensor group - Stream Select no longer
            # configures emitter/exposure/gain per resolved sensor group, just once
            # for both streams together. Mirrors gui/pages/roi_select_page.py's
            # _apply_camera_controls - duplicated here rather than imported since
            # this is hardware-thread code, not GUI code.
            for sensor, profiles in groups:
                # See gui/pages/roi_select_page.py's _apply_camera_controls
                # for why this gate exists - without it, a pure color+color
                # (Dual RGB) pairing gets a spurious "not supported" warning
                # every run, since the emitter checkbox defaults to checked
                # regardless of whether either stream is actually infrared.
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
                    exposure = exposure_for_group(
                        profiles, self.pick_a, self.pick_b,
                        self.camera_controls["exposure_a"], self.camera_controls["exposure_b"],
                    )
                    if not set_manual_exposure(sensor, exposure):
                        self.error.emit(
                            "WARNING: manual exposure not supported on sensor - confirm "
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
                start_scanning(self.switch_time_ms, self.scan_direction, self.dual_panel_config)

            self._capture = ContinuousCapture(
                self.device_serial, self.pick_a, self.pick_b,
                enable_depth_for_ir_sync=self.enable_depth_for_ir_sync,
            )
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

            def on_frame_pair(stream_a_image, stream_b_image, row):
                self._maybe_save_position_gap_outlier(stream_a_image, stream_b_image, row)

            callbacks = AcquisitionCallbacks(
                on_frames=on_frames, on_row=on_row, on_stats=on_stats, on_frame_pair=on_frame_pair,
            )
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
                stop_scanning(self.dual_panel_config)
            except Exception as exc:
                self.error.emit("Failed to stop LED panel during cleanup: {}".format(exc))
