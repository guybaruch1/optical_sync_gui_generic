"""Wizard shell: a Camera Hub page in front of every run (even a single
camera - see docs/superpowers's multi-camera design doc's "Design detail"
section 4; the hub still fronts every run regardless of camera count, only
its Start destination now depends on camera count - see the paragraph
below) that fans out into the existing per-camera sub-flow (Device
select -> Stream config -> ROI select -> Calibration -> Threshold tuning),
in a QStackedWidget, persisting choices to state.gui_state as the user
moves through the wizard.

Per-camera sub-flow pages stay single, SHARED instances re-entered once per
configured camera (via the hub's Add/Edit actions) - none of their own
internals changed for multi-camera support, only this file's routing
around them. Within one camera's sub-flow, self._pick_a/self._pick_b/
self._camera_controls/self._pending_ctx are that camera's own SCRATCH state
(exactly as before this feature existed) - only committed into
self._cameras[self._editing_camera_id] once that camera's flow fully
completes (_on_tuning_done). GuiState still only stores a lossy,
JSON-friendly prefill record for the NEXT app launch's Stream Config
defaults (now reflecting whichever camera was edited most recently, a
known, deliberately-deferred limitation - see the design doc's "Explicitly
deferred to v2" list); self._cameras is this run's actual source of truth
for every configured camera's full config.

CameraHubPage's "Start Multi-Camera Live Session" switches to
gui/pages/multi_camera_live_session_page.py's MultiCameraLiveSessionPage
when 2+ cameras are configured. With exactly 1 configured camera, it
instead routes to the original single-camera gui/pages/live_session_page.py's
LiveSessionPage directly - a solo camera has no genlock partner and no
cross-camera concept, so the lighter, purpose-built single-camera page is
used instead of the multi-camera one. See
_on_start_multi_camera_session_requested for the branch. Note this is a
genuine second implementation of the single-camera view:
gui/widgets/camera_live_session_panel.py's CameraLiveSessionPanel is the
2+-camera page's own near-clone of it (chart/export/snapshot/switch-time-
gate logic all duplicated) - a behavior fix to one must be mirrored in the
other, since both are now independently reachable.
"""

import numpy as np
import pyrealsense2 as rs
from PySide6.QtWidgets import QMainWindow, QStackedWidget, QMessageBox

from gui.pages.device_select_page import DeviceSelectPage
from gui.pages.stream_config_page import StreamConfigPage
from gui.pages.roi_select_page import RoiSelectPage, stream_label
from gui.pages.calibration_page import CalibrationPage
from gui.pages.threshold_tuning_page import ThresholdTuningPage
from gui.pages.camera_hub_page import CameraHubPage, CameraSummary
from gui.pages.multi_camera_live_session_page import MultiCameraLiveSessionPage
from gui.pages.live_session_page import LiveSessionPage
from state.gui_state import GuiState, save_gui_state
from engine.streams import (
    list_video_stream_options, stream_slug,
    parse_camera_tests_config, resolve_camera_tests,
    resolve_inter_cam_sync_value, resolve_max_slave_color_resolution,
    find_device_by_serial, set_inter_cam_sync_mode, INTER_CAM_SYNC_DEFAULT,
)
from domain.calibration import load_led_positions
from settings import ensure_output_dir


def _slave_genlock_color_resolution_conflicts(cameras, inter_cam_sync_settings):
    """Returns a list of human-readable conflict strings, one per camera
    whose own color stream (if any) can't safely run while that camera
    acts as a genlock SLAVE - either it exceeds the confirmed-safe
    resolution, or this camera model has no confirmed cap at all yet (real
    hardware finding: full 1280x720@30 color blocks BOTH streams entirely
    once genlocked - a USB bandwidth ceiling, not a hardware/firmware
    block - see tools/genlock_diag/diag_slave_color_bandwidth_sweep.py).
    Only checked for a camera that is NOT master and whose inter_cam_sync_
    value is not None - a camera whose genlock is skipped entirely
    (unconfirmed master/slave scheme) never hits this bandwidth
    constraint, since it just runs free-running with no external trigger
    involved at all."""
    conflicts = []
    for camera in cameras:
        if camera["is_master"] or camera["config"]["inter_cam_sync_value"] is None:
            continue
        color_pick = next(
            (pick for pick in (camera["config"]["pick_a"], camera["config"]["pick_b"])
             if pick["stream_type"] == rs.stream.color),
            None,
        )
        if color_pick is None:
            continue
        max_resolution = resolve_max_slave_color_resolution(inter_cam_sync_settings, camera["label"])
        if max_resolution is None:
            conflicts.append(
                "{}: color stream at {}x{} - this camera model's safe color resolution as a "
                "genlock slave has not been confirmed yet (no max_slave_color_resolution in "
                "settings.yaml's camera.inter_cam_sync entry).".format(
                    camera["label"], color_pick["width"], color_pick["height"],
                )
            )
            continue
        max_width, max_height = max_resolution
        if color_pick["width"] > max_width or color_pick["height"] > max_height:
            conflicts.append(
                "{}: color stream at {}x{} exceeds the confirmed safe resolution ({}x{}) for a "
                "genlock slave - full resolution blocks BOTH streams entirely.".format(
                    camera["label"], color_pick["width"], color_pick["height"], max_width, max_height,
                )
            )
    return conflicts


class MainWindow(QMainWindow):
    def __init__(self, ctx, gui_state: GuiState, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Optical Sync GUI")
        self.ctx = ctx
        self.gui_state = gui_state
        self.settings = settings

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.device_page = DeviceSelectPage()
        self.stream_config_page = StreamConfigPage()
        self.roi_page = RoiSelectPage()
        self.calibration_page = CalibrationPage()
        self.threshold_tuning_page = ThresholdTuningPage()
        self.camera_hub_page = CameraHubPage()
        self.multi_camera_live_session_page = MultiCameraLiveSessionPage()
        self.live_session_page = LiveSessionPage()
        self._device_name = None
        # Stashed in _on_calibration_done, consumed in _on_tuning_done -
        # everything the eventual multi-camera Live Session controller will
        # still need that Threshold Tuning itself has no use for (CSV paths,
        # output_dir, frame-drop/pairing-gap tuning, etc.). See module
        # docstring for why this can't just live in GuiState/settings alone.
        self._pending_ctx = None
        # Live pick_a/pick_b/camera_controls, stashed here in
        # _on_config_chosen and read back in _on_roi_chosen/
        # _on_calibration_done - GuiState alone can't round-trip these (it
        # deliberately doesn't store `format`/`sensor_index`, see module
        # docstring), and CalibrationPage.calibration_done is a bare signal
        # with no payload. All THREE of these are one camera's own SCRATCH
        # state during its sub-flow - committed into self._cameras (see
        # below) once that camera's flow fully completes.
        self._pick_a = None
        self._pick_b = None
        self._camera_controls = None
        # Resolved once in _on_device_chosen from Device Select's manual
        # "Use dual LED panel" checkbox - None for the normal single-panel
        # case (every camera/test the operator hasn't opted into dual-panel
        # mode for), settings.yaml's dual_panel: section dict otherwise.
        # Threaded into every downstream page's set_context() from here on,
        # since ROI Select is the very next wizard step and its LED-panel
        # calls need this too.
        self._dual_panel_config = None

        # camera_id -> {"label": str, "config": dict} for every camera that
        # has FINISHED its own 5-page sub-flow at least once - this run's
        # actual source of truth (GuiState is only ever a lossy prefill, see
        # module docstring). A camera only appears here once fully
        # committed by _on_tuning_done; while a camera is mid-flow it's
        # tracked only by self._editing_camera_id, with no placeholder card
        # shown on the hub - simplest v1 behavior, matching how the
        # single-camera wizard never showed an intermediate "hub" state
        # either.
        self._cameras = {}
        self._master_camera_id = None
        # Which camera_id the 5-page sub-flow currently in progress will
        # commit into once it finishes - set by _on_add_camera_requested/
        # _on_edit_camera_requested, or lazily assigned by _on_device_chosen
        # itself if still None (keeps direct-call testing/tooling working
        # without requiring every caller to go through the hub first).
        self._editing_camera_id = None
        self._next_camera_slot = 1

        for page in (self.device_page, self.stream_config_page, self.roi_page,
                     self.calibration_page, self.threshold_tuning_page, self.camera_hub_page,
                     self.multi_camera_live_session_page, self.live_session_page):
            self.stack.addWidget(page)

        self.device_page.device_chosen.connect(self._on_device_chosen)
        self.stream_config_page.config_chosen.connect(self._on_config_chosen)
        self.roi_page.roi_chosen.connect(self._on_roi_chosen)
        self.calibration_page.calibration_done.connect(self._on_calibration_done)
        self.threshold_tuning_page.tuning_done.connect(self._on_tuning_done)
        # Back: each page just switches the stack to the previous page in the
        # flow - no set_context()/populate() call, so whatever that page
        # already holds (picks, ROI, calibration log, ...) is exactly what's
        # shown, no work redone. Each page's own back_requested handler is
        # responsible for stopping/confirming whatever it has running first
        # (see each page's own _on_back_clicked) - by the time the signal
        # reaches here it's already safe to switch away.
        self.device_page.back_requested.connect(lambda: self.stack.setCurrentWidget(self.camera_hub_page))
        self.stream_config_page.back_requested.connect(lambda: self.stack.setCurrentWidget(self.device_page))
        self.roi_page.back_requested.connect(lambda: self.stack.setCurrentWidget(self.stream_config_page))
        self.calibration_page.back_requested.connect(lambda: self.stack.setCurrentWidget(self.roi_page))
        self.threshold_tuning_page.back_requested.connect(
            lambda: self.stack.setCurrentWidget(self.calibration_page)
        )
        self.live_session_page.back_requested.connect(lambda: self.stack.setCurrentWidget(self.camera_hub_page))
        self.multi_camera_live_session_page.back_requested.connect(
            lambda: self.stack.setCurrentWidget(self.camera_hub_page)
        )
        self.camera_hub_page.add_camera_requested.connect(self._on_add_camera_requested)
        self.camera_hub_page.edit_camera_requested.connect(self._on_edit_camera_requested)
        self.camera_hub_page.master_change_requested.connect(self._on_master_change_requested)
        self.camera_hub_page.remove_camera_requested.connect(self._on_remove_camera_requested)
        self.camera_hub_page.start_multi_camera_session_requested.connect(
            self._on_start_multi_camera_session_requested
        )

        self.stack.setCurrentWidget(self.camera_hub_page)

    def _on_device_chosen(self, serial, name):
        # Device Select is always the entry point into a camera's sub-flow -
        # ensure an editing camera_id exists even if we got here without
        # going through the hub's Add/Edit actions first (e.g. a direct
        # call, same as every pre-existing test in this file does).
        if self._editing_camera_id is None:
            self._editing_camera_id = self._new_camera_slot_id()
        self.gui_state.device_serial = serial
        self._device_name = name
        save_gui_state(self.gui_state)
        camera_settings = self.settings["camera"]
        raw_tests = camera_settings.get("stream_options", {}).get(name)
        if not raw_tests:
            QMessageBox.critical(
                self, "No Stream Select options configured",
                "settings.yaml's camera.stream_options has no entry for camera {!r} - add one "
                "with its own named tests before using Stream Select with this camera.".format(name),
            )
            return
        try:
            parsed_tests = parse_camera_tests_config(raw_tests)
        except (KeyError, ValueError) as exc:
            QMessageBox.critical(
                self, "Invalid Stream Select configuration",
                "settings.yaml's camera.stream_options entry for camera {!r} is invalid "
                "({}: {}) - fix its test entries before using Stream Select with this "
                "camera.".format(name, type(exc).__name__, exc),
            )
            return

        device_options = list_video_stream_options(self.ctx, serial)
        resolved_tests = resolve_camera_tests(device_options, parsed_tests)
        # Tests with no sensor_options entry matching what this specific
        # device/firmware reports are dropped rather than shown - see
        # engine.streams.resolve_camera_tests's docstring.
        usable_tests = [t for t in resolved_tests if t["options"]]
        if not usable_tests:
            QMessageBox.critical(
                self, "No matching Stream Select options",
                "None of camera {!r}'s configured tests in settings.yaml's camera.stream_options "
                "matched anything this connected device actually reports - check those tests' "
                "sensor_options against what this specific device/firmware supports.".format(name),
            )
            return

        self.stream_config_page.populate(
            self.ctx, serial, usable_tests,
            preferred_a=camera_settings["stream_a"], preferred_b=camera_settings["stream_b"],
            preferred_test_name=self.gui_state.last_test_name,
            # settings.yaml camera_sync.enable_depth_for_ir_sync - read here
            # (rather than only later in _on_calibration_done, where the rest
            # of camera_sync is read) so Stream Select's own pairing-quality
            # preview shows the same IR/RGB sync fix the real run downstream
            # will use.
            enable_depth_for_ir_sync=(self.settings.get("camera_sync") or {}).get(
                "enable_depth_for_ir_sync", True
            ),
        )
        self.stack.setCurrentWidget(self.stream_config_page)

    def _on_config_chosen(self, config):
        pick_a, pick_b, camera_controls = config
        self._pick_a = pick_a
        self._pick_b = pick_b
        self._camera_controls = camera_controls

        # config_chosen's payload stays (pick_a, pick_b, camera_controls) -
        # ROI Select/Calibration/Live Session downstream never need to know
        # a "test" concept exists at all. Only this prefill-persistence spot
        # cares which named test produced this pick, so it's read directly
        # off the still-current page rather than added to the signal payload.
        self.gui_state.last_test_name = self.stream_config_page.current_test_name

        # Manual, per-camera-flow choice (see stream_config_page.py's own
        # comment on dual_panel_checkbox for why it lives here rather than
        # on Device Select or as a whole-test setting) - read directly off
        # the still-current page, same reasoning as current_test_name above.
        self._dual_panel_config = (
            self.settings["dual_panel"] if self.stream_config_page.dual_panel_checkbox.isChecked() else None
        )

        # camera_controls' emitter/auto_exposure MODE is one shared choice
        # (GuiState just mirrors the same value into both stream_a_*/
        # stream_b_* fields, it's just a lossy prefill record, see this
        # module's docstring) - but exposure itself is genuinely per-stream
        # now (exposure_a/exposure_b), so each side gets its OWN value here
        # too. camera_controls has no "gain" key at all anymore - manual
        # exposure never touches gain (see engine.streams.
        # set_manual_exposure's docstring) - so stream_a_gain/stream_b_gain
        # are left at None; the GuiState fields themselves stay for
        # backward compatibility with old gui_state.json files, just
        # unused going forward.
        self.gui_state.stream_a_type = pick_a["stream_type"].name
        self.gui_state.stream_a_index = pick_a["stream_index"]
        self.gui_state.stream_a_width = pick_a["width"]
        self.gui_state.stream_a_height = pick_a["height"]
        self.gui_state.stream_a_fps = pick_a["fps"]
        self.gui_state.stream_a_emitter_enabled = camera_controls["emitter_enabled"]
        self.gui_state.stream_a_auto_exposure = camera_controls["auto_exposure"]
        self.gui_state.stream_a_exposure = camera_controls["exposure_a"]
        self.gui_state.stream_a_gain = None
        self.gui_state.stream_b_type = pick_b["stream_type"].name
        self.gui_state.stream_b_index = pick_b["stream_index"]
        self.gui_state.stream_b_width = pick_b["width"]
        self.gui_state.stream_b_height = pick_b["height"]
        self.gui_state.stream_b_fps = pick_b["fps"]
        self.gui_state.stream_b_emitter_enabled = camera_controls["emitter_enabled"]
        self.gui_state.stream_b_auto_exposure = camera_controls["auto_exposure"]
        self.gui_state.stream_b_exposure = camera_controls["exposure_b"]
        self.gui_state.stream_b_gain = None
        save_gui_state(self.gui_state)

        self.roi_page.set_context(
            self.ctx, self.gui_state.device_serial, pick_a, pick_b, camera_controls,
            settle_frames=self.settings["calibration"]["settle_frames"],
            dual_panel_config=self._dual_panel_config,
        )
        self.stack.setCurrentWidget(self.roi_page)

    def _on_roi_chosen(self, rois):
        stream_a_roi, stream_b_roi = rois
        self.gui_state.stream_a_roi = list(stream_a_roi)
        self.gui_state.stream_b_roi = list(stream_b_roi)
        save_gui_state(self.gui_state)

        calib_settings = self.settings["calibration"]
        self.calibration_page.set_context(
            self.ctx, self.gui_state.device_serial,
            self._pick_a, self._pick_b, self._camera_controls,
            stream_a_roi, stream_b_roi,
            config_path=self.settings["paths"]["config_path"],
            camera_name=self._current_device_name(),
            output_root=ensure_output_dir(self.settings),
            settle_frames=calib_settings["settle_frames"],
            min_blob_area=calib_settings["min_blob_area"],
            neighborhood_size=calib_settings["neighborhood_size"],
            row_gap_px=calib_settings["row_gap_px"],
            min_acceptable_contrast=calib_settings["min_acceptable_contrast"],
            dual_panel_config=self._dual_panel_config,
        )
        self.stack.setCurrentWidget(self.calibration_page)

    def _on_calibration_done(self):
        pick_a, pick_b, camera_controls = self._pick_a, self._pick_b, self._camera_controls
        camera_name = self._current_device_name()
        config_path = self.settings["paths"]["config_path"]
        slug_a, slug_b = stream_slug(pick_a), stream_slug(pick_b)
        stream_a_positions, stream_b_positions = load_led_positions(
            config_path, camera_name,
            slug_a, (pick_a["width"], pick_a["height"]),
            slug_b, (pick_b["width"], pick_b["height"]),
        )

        stream_a_ids = list(stream_a_positions.keys())
        stream_b_ids = list(stream_b_positions.keys())
        stream_a_xy = np.array([stream_a_positions[i][:2] for i in stream_a_ids])
        stream_b_xy = np.array([stream_b_positions[i][:2] for i in stream_b_ids])

        num_leds = self.settings["test"]["num_leds"]
        # .get() with defaults rather than a hard lookup - an existing
        # hand-maintained settings.yaml predating this section shouldn't
        # break, it just gets the same behavior as leaving both off (except
        # enable_depth_for_ir_sync, which is safe enough to default on - see
        # that section's own comment in settings.yaml).
        camera_sync = self.settings.get("camera_sync") or {}
        camera_sync = {
            "enable_depth_for_ir_sync": camera_sync.get("enable_depth_for_ir_sync", True),
            "hardware_reset_before_start": camera_sync.get("hardware_reset_before_start", False),
            "hardware_reset_settle_s": camera_sync.get("hardware_reset_settle_s", 8.0),
        }
        if len(stream_a_ids) != len(stream_b_ids) or len(stream_a_ids) != num_leds:
            QMessageBox.warning(
                self,
                "LED count mismatch",
                "Calibration detected {} {} LED(s) and {} {} LED(s), but settings.yaml's "
                "test.num_leds is {}. The live session's position-gap math assumes all three "
                "match - proceeding anyway, but treat position-gap results with caution until "
                "this is resolved (re-run calibration, or fix test.num_leds).".format(
                    len(stream_a_ids), stream_label(pick_a), len(stream_b_ids), stream_label(pick_b), num_leds
                ),
            )

        stream_a_on = np.array([stream_a_positions[i][2] for i in stream_a_ids])
        stream_a_off = np.array([stream_a_positions[i][3] for i in stream_a_ids])
        stream_b_on = np.array([stream_b_positions[i][2] for i in stream_b_ids])
        stream_b_off = np.array([stream_b_positions[i][3] for i in stream_b_ids])

        # Everything Live Session will still need once tuning is done, but
        # that Threshold Tuning itself has no use for - see _on_tuning_done.
        # output_root/kept_csv_filename/dropped_csv_filename are raw pieces,
        # not a pre-joined output_dir/kept_csv_path/dropped_csv_path - each
        # Start click on Live Session mints its OWN fresh timestamped run
        # folder (see LiveSessionPage._begin_new_run_output), so nothing
        # here can be pre-joined once and reused across multiple runs.
        # stream_a_xy/stream_b_xy are NOT stashed here (unlike before) -
        # LED Detection Threshold Tuning on that page can now REASSIGN its
        # own copy of these arrays (a retune can change the LED count), so
        # _on_tuning_done reads them live off threshold_tuning_page's own
        # properties instead of a snapshot frozen at this point, which would
        # go stale the moment a retune actually changes anything.
        self._pending_ctx = dict(
            device_serial=self.gui_state.device_serial, pick_a=pick_a, pick_b=pick_b,
            camera_controls=camera_controls,
            scan_direction=self.settings["test"]["scan_direction"],
            num_leds=num_leds, neighborhood_size=self.settings["test"]["neighborhood_size"],
            frame_drop_threshold_factor=self.settings["test"]["frame_drop_threshold_factor"],
            warmup_pairs_to_skip=self.settings["test"]["warmup_pairs_to_skip"],
            pairing_gap_outlier_threshold_us=self.settings["test"]["pairing_gap_outlier_threshold_us"],
            position_gap_outlier_threshold_ms=self.settings["test"]["position_gap_outlier_threshold_ms"],
            position_gap_outlier_max_snapshots=self.settings["test"]["position_gap_outlier_max_snapshots"],
            output_root=ensure_output_dir(self.settings),
            kept_csv_filename=self.settings["paths"]["raw_csv_path"],
            dropped_csv_filename=self.settings["paths"]["frame_drop_csv_path"],
            snapshot_every_n_pairs=self.settings["test"]["snapshot_every_n_pairs"],
            max_snapshots=self.settings["test"]["max_snapshots"],
            stream_a_roi=self.gui_state.stream_a_roi, stream_b_roi=self.gui_state.stream_b_roi,
            camera_name=camera_name,
            stream_a_label=stream_label(pick_a), stream_b_label=stream_label(pick_b),
            dual_panel_config=self._dual_panel_config,
            # settings.yaml camera_sync: - the two inter-sensor-sync knobs,
            # stashed here so _on_tuning_done can hand them to Live Session.
            enable_depth_for_ir_sync=camera_sync["enable_depth_for_ir_sync"],
            hardware_reset_before_start=camera_sync["hardware_reset_before_start"],
            hardware_reset_settle_s=camera_sync["hardware_reset_settle_s"],
        )
        # Already-captured on/off frames + each stream's Otsu-chosen
        # detection threshold, retained by CalibrationPage after its own
        # successful run - lets ThresholdTuningPage's LED Detection
        # Threshold Tuning section offer a manual override with no new
        # camera capture of its own.
        calib_result = self.calibration_page.last_calibration_result
        self.threshold_tuning_page.set_context(
            self.ctx, self.gui_state.device_serial, pick_a, pick_b, camera_controls,
            stream_a_xy=stream_a_xy, stream_b_xy=stream_b_xy,
            stream_a_on=stream_a_on, stream_a_off=stream_a_off,
            stream_b_on=stream_b_on, stream_b_off=stream_b_off,
            num_leds=num_leds, neighborhood_size=self.settings["test"]["neighborhood_size"],
            scan_direction=self.settings["test"]["scan_direction"],
            switch_time_ms=self.settings["test"]["switch_time_ms"],
            stream_a_threshold_fraction_default=self.settings["test"]["stream_a_threshold_fraction"],
            stream_b_threshold_fraction_default=self.settings["test"]["stream_b_threshold_fraction"],
            stream_a_roi=self.gui_state.stream_a_roi, stream_b_roi=self.gui_state.stream_b_roi,
            camera_name=camera_name,
            stream_a_label=stream_label(pick_a), stream_b_label=stream_label(pick_b),
            config_path=config_path,
            image_a_on=calib_result["image_a_on"], image_a_off=calib_result["image_a_off"],
            image_b_on=calib_result["image_b_on"], image_b_off=calib_result["image_b_off"],
            stream_a_otsu_threshold=calib_result["stream_a_otsu_threshold"],
            stream_b_otsu_threshold=calib_result["stream_b_otsu_threshold"],
            min_blob_area=calib_result["min_blob_area"], row_gap_px=calib_result["row_gap_px"],
            calibration_neighborhood_size=calib_result["neighborhood_size"],
            stream_a_positions=stream_a_positions, stream_b_positions=stream_b_positions,
            dual_panel_config=self._dual_panel_config,
            enable_depth_for_ir_sync=camera_sync["enable_depth_for_ir_sync"],
        )
        self.stack.setCurrentWidget(self.threshold_tuning_page)

    def _on_tuning_done(self):
        pending = self._pending_ctx
        # Everything the eventual multi-camera Live Session controller will
        # need for THIS camera - same set of values LiveSessionPage.
        # set_context() used to receive directly; now stored per-camera
        # instead, keyed by the camera_id this whole sub-flow was for.
        config = dict(
            device_serial=pending["device_serial"], pick_a=pending["pick_a"], pick_b=pending["pick_b"],
            camera_controls=pending["camera_controls"],
            # The tuned switch time (not settings.yaml's raw default) - this
            # is what was actually previewed on the Threshold Tuning page,
            # so it's the more accurate starting point for the real test.
            switch_time_ms=self.threshold_tuning_page.switch_time_ms,
            scan_direction=pending["scan_direction"],
            stream_a_threshold=self.threshold_tuning_page.stream_a_threshold,
            stream_b_threshold=self.threshold_tuning_page.stream_b_threshold,
            # Read live off the page's own properties, not a copy frozen in
            # _pending_ctx before Threshold Tuning ever ran - LED Detection
            # Threshold Tuning can reassign these (a retune may change the
            # LED count), and _pending_ctx's own snapshot would go stale the
            # moment that happens.
            stream_a_xy=self.threshold_tuning_page.stream_a_xy, stream_b_xy=self.threshold_tuning_page.stream_b_xy,
            num_leds=pending["num_leds"], neighborhood_size=pending["neighborhood_size"],
            frame_drop_threshold_factor=pending["frame_drop_threshold_factor"],
            warmup_pairs_to_skip=pending["warmup_pairs_to_skip"],
            pairing_gap_outlier_threshold_us=pending["pairing_gap_outlier_threshold_us"],
            position_gap_outlier_threshold_ms=pending["position_gap_outlier_threshold_ms"],
            position_gap_outlier_max_snapshots=pending["position_gap_outlier_max_snapshots"],
            output_root=pending["output_root"],
            kept_csv_filename=pending["kept_csv_filename"],
            dropped_csv_filename=pending["dropped_csv_filename"],
            snapshot_every_n_pairs=pending["snapshot_every_n_pairs"],
            max_snapshots=pending["max_snapshots"],
            stream_a_roi=pending["stream_a_roi"], stream_b_roi=pending["stream_b_roi"],
            camera_name=pending["camera_name"],
            stream_a_label=pending["stream_a_label"], stream_b_label=pending["stream_b_label"],
            dual_panel_config=pending["dual_panel_config"],
            enable_depth_for_ir_sync=pending["enable_depth_for_ir_sync"],
            hardware_reset_before_start=pending["hardware_reset_before_start"],
            hardware_reset_settle_s=pending["hardware_reset_settle_s"],
        )

        camera_id = self._editing_camera_id
        is_first_camera = not self._cameras
        self._cameras[camera_id] = {"label": pending["camera_name"], "config": config}
        if is_first_camera:
            self._master_camera_id = camera_id
        self._editing_camera_id = None

        self._refresh_camera_hub()
        self.stack.setCurrentWidget(self.camera_hub_page)

    def _new_camera_slot_id(self):
        slot_id = "camera_{}".format(self._next_camera_slot)
        self._next_camera_slot += 1
        return slot_id

    def _refresh_camera_hub(self):
        summaries = [
            CameraSummary(
                camera_id=camera_id,
                # Two configured cameras of the same model are otherwise
                # indistinguishable on this page - the serial is what
                # actually tells them apart on a real multi-camera rig.
                label="{} [{}]".format(camera["label"], camera["config"]["device_serial"]),
                is_master=(camera_id == self._master_camera_id), configured=True,
            )
            for camera_id, camera in self._cameras.items()
        ]
        self.camera_hub_page.set_cameras(summaries)

    def _on_add_camera_requested(self):
        self._editing_camera_id = self._new_camera_slot_id()
        # Hides every already-configured camera's device from the picker -
        # the same physical camera can't be added to the test twice.
        already_configured_serials = {
            camera["config"]["device_serial"] for camera in self._cameras.values()
        }
        self.device_page.refresh_devices(self.ctx, exclude_serials=already_configured_serials)
        self.stack.setCurrentWidget(self.device_page)

    def _on_edit_camera_requested(self, camera_id):
        # Simplest v1 behavior: re-run that camera's ENTIRE sub-flow from
        # Device Select, same as adding a new one - no pre-population of
        # its previous choices beyond whatever GuiState's own lossy prefill
        # already offers. The camera's previously-committed config is left
        # in self._cameras untouched until the sub-flow completes again
        # (_on_tuning_done overwrites it under the same camera_id).
        self._editing_camera_id = camera_id
        self.device_page.refresh_devices(self.ctx)
        self.stack.setCurrentWidget(self.device_page)

    def _on_master_change_requested(self, camera_id):
        self._master_camera_id = camera_id
        self._refresh_camera_hub()

    def _on_remove_camera_requested(self, camera_id):
        self._cameras.pop(camera_id, None)
        if self._master_camera_id == camera_id:
            remaining = list(self._cameras.keys())
            self._master_camera_id = remaining[0] if remaining else None
        self._refresh_camera_hub()

    def _on_start_multi_camera_session_requested(self):
        # Not reachable via the real hub with zero cameras (Start is
        # disabled - see CameraHubPage._can_start), but guard defensively
        # rather than switch to an empty page if something else calls this.
        if not self._cameras:
            return
        if len(self._cameras) == 1:
            # A solo camera has no genlock partner and no cross-camera
            # concept at all - route to the lighter, purpose-built
            # single-camera page instead of the multi-camera one, skipping
            # genlock/slave-color-resolution resolution entirely (it's
            # meaningless without a second camera). The per-camera config
            # dict's own keys already match set_context()'s parameters
            # exactly - see _on_tuning_done's own comment.
            only_camera = next(iter(self._cameras.values()))
            # Best-effort self-heal, mirroring engine/multi_camera_session.py's
            # own _reset_genlock_roles: a camera left stuck in
            # INTER_CAM_SYNC_SLAVE from an earlier crashed/killed multi-camera
            # run would otherwise sit waiting here for a genlock trigger it
            # will never receive when run solo. A single device-lookup/set
            # failure (e.g. no hardware connected) must not block starting the
            # session - swallow it and proceed regardless.
            try:
                device = find_device_by_serial(self.ctx, only_camera["config"]["device_serial"])
                set_inter_cam_sync_mode(device, INTER_CAM_SYNC_DEFAULT)
            except Exception:
                pass
            self.live_session_page.set_context(ctx=self.ctx, **only_camera["config"])
            self.stack.setCurrentWidget(self.live_session_page)
            return
        # Genlock role resolution happens fresh HERE, at Start-time, not
        # earlier - the master assignment can change at any point in the hub
        # (Set as Master, remove-the-master promotion) up until the operator
        # actually starts the run, so re-resolving off self._master_camera_id
        # every Start is what keeps this correct rather than stale.
        inter_cam_sync_settings = self.settings["camera"].get("inter_cam_sync", {})
        camera_sync_settings = self.settings.get("camera_sync") or {}
        cameras = [
            {"camera_id": camera_id, "label": camera["label"],
             "is_master": (camera_id == self._master_camera_id),
             "config": {
                 **camera["config"],
                 "inter_cam_sync_value": resolve_inter_cam_sync_value(
                     inter_cam_sync_settings, camera["label"],
                     is_master=(camera_id == self._master_camera_id),
                 ),
                 # Only ever added here, on the 2+-camera path - never into
                 # the base camera["config"] dict the 1-camera branch above
                 # splats directly into LiveSessionPage.set_context(), which
                 # must never receive this (see settings.yaml's own comment
                 # for why - a cross-camera-only concept).
                 "capture_global_ts": camera_sync_settings.get("capture_global_ts", True),
             }}
            for camera_id, camera in self._cameras.items()
        ]
        conflicts = _slave_genlock_color_resolution_conflicts(cameras, inter_cam_sync_settings)
        if conflicts:
            QMessageBox.critical(
                self, "Slave camera color resolution too high for genlock",
                "The following camera(s) can't safely run their configured color stream "
                "while acting as a genlock slave:\n\n{}\n\nLower that stream's resolution "
                "in Stream Config, or make this camera the master instead.".format(
                    "\n".join(conflicts)
                ),
            )
            return
        self.multi_camera_live_session_page.set_cameras(self.ctx, cameras)
        self.stack.setCurrentWidget(self.multi_camera_live_session_page)

    def _current_device_name(self):
        # Cached from DeviceSelectPage.device_chosen's payload (see
        # _on_device_chosen), not looked up by reaching into device_page's own
        # _devices list - that reach-through also meant raising if the device
        # had since disappeared (e.g. the camera was unplugged mid-wizard).
        return self._device_name
