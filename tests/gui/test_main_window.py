from unittest.mock import MagicMock, patch

import numpy as np
import pyrealsense2 as rs
from PySide6.QtWidgets import QMessageBox

import gui.main_window as main_window_module
from gui.main_window import MainWindow
from state.gui_state import GuiState


class FakeCtx:
    """Just enough of rs.context() for DeviceSelectPage.refresh_devices,
    called unconditionally by MainWindow.__init__ - no real devices needed
    for these tests, which exercise _on_device_chosen directly."""
    def query_devices(self):
        return []


IR1 = {"sensor_index": 0, "stream_type": rs.stream.infrared, "stream_index": 1,
       "format": rs.format.y8, "width": 1280, "height": 720, "fps": 30}
COLOR0 = {"sensor_index": 1, "stream_type": rs.stream.color, "stream_index": 0,
          "format": rs.format.bgr8, "width": 1280, "height": 720, "fps": 30}


def _ir_vs_rgb_test(name="IR vs RGB sync", width=1280, height=720, fps=30):
    return {
        "test_name": name,
        "stream_a_identity": {"stream_type": "infrared", "stream_index": 1},
        "stream_b_identity": {"stream_type": "color", "stream_index": 0},
        "sensor_options": [{
            "stream_a": {"width": width, "height": height, "fps": fps, "format": "y8"},
            "stream_b": {"width": width, "height": height, "fps": fps, "format": "bgr8"},
        }],
    }


def _minimal_settings(stream_options):
    return {
        "camera": {
            "stream_options": stream_options,
            "stream_a": {"width": 1280, "height": 720, "fps": 30},
            "stream_b": {"width": 1280, "height": 720, "fps": 30},
        },
    }


def _make_window(qapp, settings):
    return MainWindow(FakeCtx(), GuiState(), settings)


def _capture_critical(monkeypatch):
    calls = []
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *a, **k: calls.append((a, k)) or QMessageBox.Ok))
    return calls


# --- Sub-finding 6b: malformed camera.stream_options entry must show a
# friendly error, not crash with a raw KeyError/ValueError ---

def test_on_device_chosen_shows_error_for_missing_stream_a_identity_key(qapp, monkeypatch):
    test = _ir_vs_rgb_test()
    del test["stream_a_identity"]  # malformed: required key missing entirely
    settings = _minimal_settings({"Intel RealSense D455": [test]})
    window = _make_window(qapp, settings)
    calls = _capture_critical(monkeypatch)

    window._on_device_chosen("SN123", "Intel RealSense D455")

    assert len(calls) == 1
    assert "invalid" in calls[0][0][2].lower()
    assert window.stack.currentWidget() is not window.stream_config_page


def test_on_device_chosen_shows_error_for_unknown_stream_type_string(qapp, monkeypatch):
    test = _ir_vs_rgb_test()
    test["stream_a_identity"]["stream_type"] = "colour"  # typo
    settings = _minimal_settings({"Intel RealSense D455": [test]})
    window = _make_window(qapp, settings)
    calls = _capture_critical(monkeypatch)

    window._on_device_chosen("SN123", "Intel RealSense D455")

    assert len(calls) == 1
    assert "invalid" in calls[0][0][2].lower()
    assert window.stack.currentWidget() is not window.stream_config_page


# --- Sub-finding 6c: a device where NOTHING configured matches must show an
# error, not a silently empty/unusable Stream Select page ---

def test_on_device_chosen_shows_error_when_no_test_matches_device(qapp, monkeypatch):
    settings = _minimal_settings({"Intel RealSense D455": [
        _ir_vs_rgb_test(width=1920, height=1080, fps=60),  # doesn't match what the device reports below
    ]})
    window = _make_window(qapp, settings)
    calls = _capture_critical(monkeypatch)
    monkeypatch.setattr(main_window_module, "list_video_stream_options", lambda ctx, serial: [IR1, COLOR0])

    window._on_device_chosen("SN123", "Intel RealSense D455")

    assert len(calls) == 1
    assert "Intel RealSense D455" in calls[0][0][2]
    assert window.stack.currentWidget() is not window.stream_config_page


def test_on_device_chosen_omits_test_with_no_matching_options_but_still_succeeds(qapp, monkeypatch):
    # One test matches the device, one doesn't - the whole camera isn't
    # rejected, the unusable test is just left out of the picker.
    settings = _minimal_settings({"Intel RealSense D455": [
        _ir_vs_rgb_test("usable test"),
        _ir_vs_rgb_test("unusable test", width=1920, height=1080, fps=60),
    ]})
    window = _make_window(qapp, settings)
    calls = _capture_critical(monkeypatch)
    monkeypatch.setattr(main_window_module, "list_video_stream_options", lambda ctx, serial: [IR1, COLOR0])

    window._on_device_chosen("SN123", "Intel RealSense D455")

    assert calls == []
    assert window.stack.currentWidget() is window.stream_config_page
    assert window.stream_config_page.combo_test.count() == 1
    assert window.stream_config_page.combo_test.itemText(0) == "usable test"


def test_on_device_chosen_succeeds_when_test_matches(qapp, monkeypatch):
    settings = _minimal_settings({"Intel RealSense D455": [_ir_vs_rgb_test()]})
    window = _make_window(qapp, settings)
    calls = _capture_critical(monkeypatch)
    monkeypatch.setattr(main_window_module, "list_video_stream_options", lambda ctx, serial: [IR1, COLOR0])

    window._on_device_chosen("SN123", "Intel RealSense D455")

    assert calls == []
    assert window.stack.currentWidget() is window.stream_config_page
    assert window.stream_config_page.combo_test.count() == 1
    assert window.stream_config_page.combo_sensor_options.count() == 1
    assert window.stream_config_page.pick_a == IR1
    assert window.stream_config_page.pick_b == COLOR0


def test_on_device_chosen_passes_camera_sync_enable_depth_to_stream_config(qapp, monkeypatch):
    # Read here (before calibration/tuning even exist yet) so Stream
    # Select's own pairing-quality preview reflects the same IR/RGB sync fix
    # the real run downstream will use.
    settings = _minimal_settings({"Intel RealSense D455": [_ir_vs_rgb_test()]})
    settings["camera_sync"] = {"enable_depth_for_ir_sync": False}
    window = _make_window(qapp, settings)
    monkeypatch.setattr(main_window_module, "list_video_stream_options", lambda ctx, serial: [IR1, COLOR0])

    window._on_device_chosen("SN123", "Intel RealSense D455")

    assert window.stream_config_page._enable_depth_for_ir_sync is False


def test_on_device_chosen_defaults_camera_sync_enable_depth_when_section_absent(qapp, monkeypatch):
    settings = _minimal_settings({"Intel RealSense D455": [_ir_vs_rgb_test()]})
    window = _make_window(qapp, settings)
    monkeypatch.setattr(main_window_module, "list_video_stream_options", lambda ctx, serial: [IR1, COLOR0])

    window._on_device_chosen("SN123", "Intel RealSense D455")

    assert window.stream_config_page._enable_depth_for_ir_sync is True


def test_on_device_chosen_still_shows_no_entry_error_for_unconfigured_camera(qapp, monkeypatch):
    settings = _minimal_settings({})
    window = _make_window(qapp, settings)
    calls = _capture_critical(monkeypatch)

    window._on_device_chosen("SN123", "Some Unconfigured Camera")

    assert len(calls) == 1
    assert "no entry" in calls[0][0][2].lower()
    assert window.stack.currentWidget() is not window.stream_config_page


def test_on_config_chosen_persists_last_test_name(qapp, monkeypatch):
    settings = _minimal_settings({"Intel RealSense D455": [_ir_vs_rgb_test("IR vs RGB sync")]})
    settings["calibration"] = {"settle_frames": 15}
    window = _make_window(qapp, settings)
    monkeypatch.setattr(main_window_module, "list_video_stream_options", lambda ctx, serial: [IR1, COLOR0])
    monkeypatch.setattr(main_window_module, "save_gui_state", lambda state: None)
    monkeypatch.setattr(window.roi_page, "set_context", lambda *a, **k: None)
    window._on_device_chosen("SN123", "Intel RealSense D455")

    window._on_config_chosen((IR1, COLOR0, {
        "emitter_enabled": False, "auto_exposure": True, "exposure_a": None, "exposure_b": None,
    }))

    assert window.gui_state.last_test_name == "IR vs RGB sync"


# --- Calibration -> Threshold Tuning -> Live Session handoff: threshold
# tuning (per-stream, with a live detection preview) moved to its own page
# between Calibration and Live Session - _on_calibration_done now populates
# ThresholdTuningPage instead of LiveSessionPage directly, and a new
# _on_tuning_done carries the tuned values the rest of the way. ---

class _FakePreviewThread:
    def __init__(self, *args, **kwargs):
        self.frame_ready = MagicMock()
        self.error = MagicMock()

    def start(self):
        pass

    def request_stop(self):
        pass

    def wait(self):
        pass


def _full_settings(stream_options):
    settings = _minimal_settings(stream_options)
    settings["calibration"] = {"settle_frames": 15}
    settings["paths"] = {
        "config_path": "config.yaml", "raw_csv_path": "raw.csv", "frame_drop_csv_path": "drops.csv",
    }
    settings["test"] = {
        "num_leds": 1, "scan_direction": 1, "switch_time_ms": 1, "neighborhood_size": 5,
        "stream_a_threshold_fraction": 0.25, "stream_b_threshold_fraction": 0.25,
        "frame_drop_threshold_factor": 1.5, "warmup_pairs_to_skip": 0,
        "pairing_gap_outlier_threshold_us": 100000, "snapshot_every_n_pairs": 20, "max_snapshots": 15,
        "position_gap_outlier_threshold_ms": 5, "position_gap_outlier_max_snapshots": 200,
    }
    settings["dual_panel"] = {
        "stream_a_panel_port": 1, "stream_b_panel_port": 0, "relay_port": 6,
        "relay_com_port": "COM6", "hub_switch_settle_s": 3.0,
    }
    return settings


def _window_after_config_chosen(qapp, monkeypatch, tmp_path, dual_panel=False):
    settings = _full_settings({"Intel RealSense D455": [_ir_vs_rgb_test()]})
    window = _make_window(qapp, settings)
    monkeypatch.setattr(main_window_module, "list_video_stream_options", lambda ctx, serial: [IR1, COLOR0])
    monkeypatch.setattr(main_window_module, "save_gui_state", lambda state: None)
    monkeypatch.setattr(window.roi_page, "set_context", lambda *a, **k: None)
    monkeypatch.setattr(main_window_module, "ensure_output_dir", lambda settings: str(tmp_path))
    monkeypatch.setattr(
        main_window_module, "load_led_positions",
        lambda *a, **k: ({"0": [1.0, 1.0, 300.0, 100.0, 200.0]}, {"0": [2.0, 2.0, 600.0, 200.0, 400.0]}),
    )
    window.device_page.dual_panel_checkbox.setChecked(dual_panel)
    window._on_device_chosen("SN123", "Intel RealSense D455")
    window._on_config_chosen((IR1, COLOR0, {
        "emitter_enabled": False, "auto_exposure": True, "exposure_a": None, "exposure_b": None,
    }))
    # This test harness skips ROI Select and Calibration entirely (jumps
    # straight to _on_calibration_done), so neither gui_state's ROI fields
    # nor calibration_page.last_calibration_result get populated by a real
    # run the way the actual wizard flow would - fill in the same shape
    # _on_calibration_done now needs (crop_to_roi requires a real ROI,
    # LED Detection Threshold Tuning needs Calibration's retained frames).
    window.gui_state.stream_a_roi = [0, 0, 50, 50]
    window.gui_state.stream_b_roi = [0, 0, 50, 50]
    window.calibration_page.last_calibration_result = dict(
        image_a_on=np.full((50, 50), 50, dtype=np.uint8), image_a_off=np.full((50, 50), 50, dtype=np.uint8),
        image_b_on=np.full((50, 50), 50, dtype=np.uint8), image_b_off=np.full((50, 50), 50, dtype=np.uint8),
        stream_a_otsu_threshold=127, stream_b_otsu_threshold=127,
        min_blob_area=5, row_gap_px=15, neighborhood_size=5,
    )
    return window


def test_on_calibration_done_populates_threshold_tuning_page_and_switches_to_it(qapp, monkeypatch, tmp_path):
    window = _window_after_config_chosen(qapp, monkeypatch, tmp_path)

    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        window._on_calibration_done()

    assert window.stack.currentWidget() is window.threshold_tuning_page
    # stream_a: off=100/on=300, settings default fraction 0.25 -> 100+0.25*200=150
    assert list(window.threshold_tuning_page.stream_a_threshold) == [150.0]
    # stream_b: off=200/on=600, same default fraction 0.25 -> 200+0.25*400=300
    assert list(window.threshold_tuning_page.stream_b_threshold) == [300.0]


def test_on_calibration_done_threads_calibration_result_into_threshold_tuning_context(qapp, monkeypatch, tmp_path):
    # LED Detection Threshold Tuning's own state (the already-captured
    # frames + Otsu thresholds CalibrationPage retained) must actually reach
    # ThresholdTuningPage's context, not just the pre-existing on/off-value
    # arrays.
    window = _window_after_config_chosen(qapp, monkeypatch, tmp_path)

    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        window._on_calibration_done()

    ctx = window.threshold_tuning_page._context
    calib_result = window.calibration_page.last_calibration_result
    assert ctx["stream_a_image_on"] is calib_result["image_a_on"]
    assert ctx["stream_b_image_on"] is calib_result["image_b_on"]
    assert ctx["stream_a_otsu_threshold"] == calib_result["stream_a_otsu_threshold"]
    assert ctx["config_path"] == window.settings["paths"]["config_path"]


def test_on_tuning_done_reads_stream_xy_live_from_threshold_tuning_page_not_a_stale_snapshot(qapp, monkeypatch, tmp_path):
    # Regression test: _on_tuning_done used to read stream_a_xy/stream_b_xy
    # from self._pending_ctx - a snapshot frozen in _on_calibration_done,
    # BEFORE Threshold Tuning ever ran. That accidentally worked only
    # because nothing previously ever reassigned
    # threshold_tuning_page._context["stream_a_xy"] after set_context().
    # LED Detection Threshold Tuning can now do exactly that (a retune can
    # change the LED count) - simulate one here and confirm Live Session
    # gets the PAGE's current value, not the stale pre-tuning one.
    window = _window_after_config_chosen(qapp, monkeypatch, tmp_path)

    camera_id = window._editing_camera_id
    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        window._on_calibration_done()
        retuned_stream_a_xy = np.array([(9.0, 9.0), (8.0, 8.0), (7.0, 7.0)])  # deliberately a different shape
        window.threshold_tuning_page._context["stream_a_xy"] = retuned_stream_a_xy
        window._on_tuning_done()

    assert window._cameras[camera_id]["config"]["stream_a_xy"] is retuned_stream_a_xy


# --- settings.yaml camera_sync: read-through. Deliberately tolerant of a
# hand-maintained settings.yaml that predates the section entirely (note
# _full_settings above has no camera_sync key at all). ---

def test_camera_sync_falls_back_to_defaults_when_section_absent(qapp, monkeypatch, tmp_path):
    window = _window_after_config_chosen(qapp, monkeypatch, tmp_path)
    camera_id = window._editing_camera_id

    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        window._on_calibration_done()
        window._on_tuning_done()

    ctx = window._cameras[camera_id]["config"]
    assert ctx["enable_depth_for_ir_sync"] is True
    assert ctx["hardware_reset_before_start"] is False
    assert ctx["hardware_reset_settle_s"] == 8.0


def test_camera_sync_settings_are_read_and_passed_to_live_session(qapp, monkeypatch, tmp_path):
    window = _window_after_config_chosen(qapp, monkeypatch, tmp_path)
    camera_id = window._editing_camera_id
    window.settings["camera_sync"] = {
        "enable_depth_for_ir_sync": False,
        "hardware_reset_before_start": True,
        "hardware_reset_settle_s": 2.5,
    }

    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        window._on_calibration_done()
        window._on_tuning_done()

    ctx = window._cameras[camera_id]["config"]
    assert ctx["enable_depth_for_ir_sync"] is False
    assert ctx["hardware_reset_before_start"] is True
    assert ctx["hardware_reset_settle_s"] == 2.5


def test_camera_sync_enable_depth_for_ir_sync_also_reaches_threshold_tuning(qapp, monkeypatch, tmp_path):
    # The preview must use the same sync fix the real run will, or it could
    # show a different inter-sensor offset than the session it's previewing.
    window = _window_after_config_chosen(qapp, monkeypatch, tmp_path)
    window.settings["camera_sync"] = {"enable_depth_for_ir_sync": False}

    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        window._on_calibration_done()

    assert window.threshold_tuning_page._context["enable_depth_for_ir_sync"] is False


def test_on_tuning_done_passes_tuned_per_stream_thresholds_to_live_session(qapp, monkeypatch, tmp_path):
    window = _window_after_config_chosen(qapp, monkeypatch, tmp_path)
    camera_id = window._editing_camera_id

    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        window._on_calibration_done()
        # Tune each stream independently, away from the settings.yaml default.
        window.threshold_tuning_page.stream_a_threshold_fraction_spinbox.setValue(0.5)
        window.threshold_tuning_page.stream_b_threshold_fraction_spinbox.setValue(0.75)
        window.threshold_tuning_page.switch_time_spinbox.setValue(5)
        window._on_tuning_done()

    assert window.stack.currentWidget() is window.camera_hub_page
    ctx = window._cameras[camera_id]["config"]
    assert list(ctx["stream_a_threshold"]) == [200.0]  # 100 + 0.5*200
    assert list(ctx["stream_b_threshold"]) == [500.0]  # 200 + 0.75*400
    assert ctx["switch_time_ms"] == 5


def test_on_tuning_done_passes_a_fractional_switch_time_to_live_session(qapp, monkeypatch, tmp_path):
    window = _window_after_config_chosen(qapp, monkeypatch, tmp_path)
    camera_id = window._editing_camera_id

    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        window._on_calibration_done()
        window.threshold_tuning_page.switch_time_spinbox.setValue(0.5)
        window._on_tuning_done()

    ctx = window._cameras[camera_id]["config"]
    assert ctx["switch_time_ms"] == 0.5


# --- Dual LED panel: a manual Device Select checkbox, NOT inferred from
# camera/test/hardware - self._dual_panel_config is None (identical to
# today's default behavior) unless the operator checks the box, in which
# case it's built from settings.yaml's dual_panel: section and threaded
# through every downstream page's set_context(). ---

def test_dual_panel_config_is_none_when_checkbox_unchecked(qapp, monkeypatch, tmp_path):
    window = _window_after_config_chosen(qapp, monkeypatch, tmp_path, dual_panel=False)
    assert window._dual_panel_config is None

    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        window._on_calibration_done()
    assert window.threshold_tuning_page._context["dual_panel_config"] is None


def test_dual_panel_config_built_from_settings_when_checkbox_checked(qapp, monkeypatch, tmp_path):
    window = _window_after_config_chosen(qapp, monkeypatch, tmp_path, dual_panel=True)
    assert window._dual_panel_config == window.settings["dual_panel"]

    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        window._on_calibration_done()
        camera_id = window._editing_camera_id
        window._on_tuning_done()

    assert window.threshold_tuning_page._context["dual_panel_config"] == window.settings["dual_panel"]
    assert window._cameras[camera_id]["config"]["dual_panel_config"] == window.settings["dual_panel"]


# --- Multi-camera hub: _on_tuning_done now commits the just-finished
# camera's config into self._cameras and returns to the new CameraHubPage,
# rather than populating LiveSessionPage directly - "hub in front of every
# run, including a single camera" (see docs/superpowers's multi-camera
# design doc's "Design detail" section 4). LiveSessionPage itself is
# untouched and still fully covered by its own test file - MainWindow
# simply no longer routes to it; the new multi-camera Live Session page
# that eventually will is a later step, not built yet. ---

def test_main_window_starts_on_camera_hub_page(qapp):
    settings = _minimal_settings({})
    window = _make_window(qapp, settings)

    assert window.stack.currentWidget() is window.camera_hub_page


def test_add_camera_requested_switches_to_device_select_and_assigns_a_slot(qapp):
    settings = _minimal_settings({})
    window = _make_window(qapp, settings)

    window._on_add_camera_requested()

    assert window.stack.currentWidget() is window.device_page
    assert window._editing_camera_id is not None


def test_completing_a_cameras_flow_commits_it_and_returns_to_hub(qapp, monkeypatch, tmp_path):
    window = _window_after_config_chosen(qapp, monkeypatch, tmp_path)
    camera_id = window._editing_camera_id

    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        window._on_calibration_done()
        window._on_tuning_done()

    assert window.stack.currentWidget() is window.camera_hub_page
    assert window._editing_camera_id is None
    assert camera_id in window._cameras
    assert window._cameras[camera_id]["label"] == "Intel RealSense D455"
    assert window._cameras[camera_id]["config"]["switch_time_ms"] == 1


def test_first_committed_camera_becomes_master_automatically(qapp, monkeypatch, tmp_path):
    window = _window_after_config_chosen(qapp, monkeypatch, tmp_path)
    camera_id = window._editing_camera_id

    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        window._on_calibration_done()
        window._on_tuning_done()

    assert window._master_camera_id == camera_id


def test_second_committed_camera_is_not_master_by_default(qapp, monkeypatch, tmp_path):
    window = _window_after_config_chosen(qapp, monkeypatch, tmp_path)
    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        window._on_calibration_done()
        window._on_tuning_done()
    first_master = window._master_camera_id

    window._on_add_camera_requested()
    second_camera_id = window._editing_camera_id
    window._on_device_chosen("SN456", "Intel RealSense D455")
    window._on_config_chosen((IR1, COLOR0, {
        "emitter_enabled": False, "auto_exposure": True, "exposure_a": None, "exposure_b": None,
    }))
    window.gui_state.stream_a_roi = [0, 0, 50, 50]
    window.gui_state.stream_b_roi = [0, 0, 50, 50]
    window.calibration_page.last_calibration_result = dict(
        image_a_on=np.full((50, 50), 50, dtype=np.uint8), image_a_off=np.full((50, 50), 50, dtype=np.uint8),
        image_b_on=np.full((50, 50), 50, dtype=np.uint8), image_b_off=np.full((50, 50), 50, dtype=np.uint8),
        stream_a_otsu_threshold=127, stream_b_otsu_threshold=127,
        min_blob_area=5, row_gap_px=15, neighborhood_size=5,
    )
    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        window._on_calibration_done()
        window._on_tuning_done()

    assert window._master_camera_id == first_master  # unchanged
    assert second_camera_id in window._cameras
    assert second_camera_id != first_master


def test_two_fully_configured_cameras_both_reach_the_live_session_page(qapp, monkeypatch, tmp_path):
    # Regression check for a real bug report: "only one camera ran" when
    # clicking Start Multi-Camera Live Session after configuring 2 cameras
    # through the hub. Drives the ENTIRE real MainWindow flow for both
    # cameras (not a hand-built cameras list, unlike
    # test_multi_camera_live_session_page.py's own tests) to prove the
    # MainWindow -> MultiCameraLiveSessionPage handoff itself carries BOTH
    # cameras through correctly.
    window = _window_after_config_chosen(qapp, monkeypatch, tmp_path)
    first_camera_id = window._editing_camera_id
    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        window._on_calibration_done()
        window._on_tuning_done()

    window._on_add_camera_requested()
    second_camera_id = window._editing_camera_id
    window._on_device_chosen("SN456", "Intel RealSense D455")
    window._on_config_chosen((IR1, COLOR0, {
        "emitter_enabled": False, "auto_exposure": True, "exposure_a": None, "exposure_b": None,
    }))
    window.gui_state.stream_a_roi = [0, 0, 50, 50]
    window.gui_state.stream_b_roi = [0, 0, 50, 50]
    window.calibration_page.last_calibration_result = dict(
        image_a_on=np.full((50, 50), 50, dtype=np.uint8), image_a_off=np.full((50, 50), 50, dtype=np.uint8),
        image_b_on=np.full((50, 50), 50, dtype=np.uint8), image_b_off=np.full((50, 50), 50, dtype=np.uint8),
        stream_a_otsu_threshold=127, stream_b_otsu_threshold=127,
        min_blob_area=5, row_gap_px=15, neighborhood_size=5,
    )
    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        window._on_calibration_done()
        window._on_tuning_done()

    assert set(window._cameras.keys()) == {first_camera_id, second_camera_id}
    assert window.camera_hub_page.start_button.isEnabled()

    window._on_start_multi_camera_session_requested()

    page = window.multi_camera_live_session_page
    assert page.tabs.count() == 2
    assert set(page._panels.keys()) == {first_camera_id, second_camera_id}
    assert len(page._cameras) == 2


def test_master_change_requested_updates_master(qapp, monkeypatch, tmp_path):
    window = _window_after_config_chosen(qapp, monkeypatch, tmp_path)
    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        window._on_calibration_done()
        window._on_tuning_done()
    window._cameras["some_other_cam"] = {"label": "other", "config": {}}

    window._on_master_change_requested("some_other_cam")

    assert window._master_camera_id == "some_other_cam"


def test_remove_camera_requested_removes_it(qapp, monkeypatch, tmp_path):
    window = _window_after_config_chosen(qapp, monkeypatch, tmp_path)
    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        window._on_calibration_done()
        window._on_tuning_done()
    camera_id = list(window._cameras.keys())[0]

    window._on_remove_camera_requested(camera_id)

    assert camera_id not in window._cameras
    assert window._master_camera_id is None  # no cameras left


def test_removing_the_master_promotes_a_remaining_camera(qapp, monkeypatch, tmp_path):
    window = _window_after_config_chosen(qapp, monkeypatch, tmp_path)
    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        window._on_calibration_done()
        window._on_tuning_done()
    master_id = window._master_camera_id
    window._cameras["some_other_cam"] = {"label": "other", "config": {}}

    window._on_remove_camera_requested(master_id)

    assert window._master_camera_id == "some_other_cam"


def test_edit_camera_requested_switches_to_device_select_reusing_the_camera_id(qapp, monkeypatch, tmp_path):
    window = _window_after_config_chosen(qapp, monkeypatch, tmp_path)
    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        window._on_calibration_done()
        window._on_tuning_done()
    camera_id = list(window._cameras.keys())[0]

    window._on_edit_camera_requested(camera_id)

    assert window.stack.currentWidget() is window.device_page
    assert window._editing_camera_id == camera_id


def test_start_multi_camera_session_requested_does_nothing_with_no_cameras(qapp):
    # Not reachable via the real hub (Start is disabled with 0 cameras -
    # see CameraHubPage._can_start), but guard defensively rather than crash
    # if something else ever calls this directly.
    settings = _minimal_settings({})
    window = _make_window(qapp, settings)

    window._on_start_multi_camera_session_requested()  # must not raise

    assert window.stack.currentWidget() is window.camera_hub_page


def test_start_multi_camera_session_requested_switches_to_the_new_page_with_cameras(qapp, monkeypatch, tmp_path):
    window = _window_after_config_chosen(qapp, monkeypatch, tmp_path)
    camera_id = window._editing_camera_id
    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        window._on_calibration_done()
        window._on_tuning_done()

    window._on_start_multi_camera_session_requested()

    assert window.stack.currentWidget() is window.multi_camera_live_session_page
    assert camera_id in window.multi_camera_live_session_page._panels


# --- Genlock (inter_cam_sync_mode) role resolution: MainWindow embeds the
# raw per-camera-model value fresh at Start-time (using whichever camera is
# CURRENTLY master), via engine.streams.resolve_inter_cam_sync_value against
# settings.yaml's camera.inter_cam_sync section - see that function's own
# docstring for why the raw value can't be guessed/hardcoded in Python. ---

def test_start_multi_camera_session_requested_embeds_inter_cam_sync_value_for_master_and_slave(qapp, monkeypatch, tmp_path):
    settings = _full_settings({"Intel RealSense D455": [_ir_vs_rgb_test()]})
    settings["camera"]["inter_cam_sync"] = {"Intel RealSense D455": {"master": 1, "slave": 2}}
    window = _make_window(qapp, settings)
    monkeypatch.setattr(main_window_module, "list_video_stream_options", lambda ctx, serial: [IR1, COLOR0])
    monkeypatch.setattr(main_window_module, "save_gui_state", lambda state: None)
    monkeypatch.setattr(window.roi_page, "set_context", lambda *a, **k: None)
    monkeypatch.setattr(main_window_module, "ensure_output_dir", lambda settings: str(tmp_path))
    monkeypatch.setattr(
        main_window_module, "load_led_positions",
        lambda *a, **k: ({"0": [1.0, 1.0, 300.0, 100.0, 200.0]}, {"0": [2.0, 2.0, 600.0, 200.0, 400.0]}),
    )

    def _configure_one_camera(serial):
        window._on_device_chosen(serial, "Intel RealSense D455")
        window._on_config_chosen((IR1, COLOR0, {
            "emitter_enabled": False, "auto_exposure": True, "exposure_a": None, "exposure_b": None,
        }))
        window.gui_state.stream_a_roi = [0, 0, 50, 50]
        window.gui_state.stream_b_roi = [0, 0, 50, 50]
        window.calibration_page.last_calibration_result = dict(
            image_a_on=np.full((50, 50), 50, dtype=np.uint8), image_a_off=np.full((50, 50), 50, dtype=np.uint8),
            image_b_on=np.full((50, 50), 50, dtype=np.uint8), image_b_off=np.full((50, 50), 50, dtype=np.uint8),
            stream_a_otsu_threshold=127, stream_b_otsu_threshold=127,
            min_blob_area=5, row_gap_px=15, neighborhood_size=5,
        )
        camera_id = window._editing_camera_id
        with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
            window._on_calibration_done()
            window._on_tuning_done()
        return camera_id

    master_id = _configure_one_camera("SN123")
    window._on_add_camera_requested()
    slave_id = _configure_one_camera("SN456")
    assert window._master_camera_id == master_id  # first camera stays master

    window._on_start_multi_camera_session_requested()

    page = window.multi_camera_live_session_page
    configs_by_id = {c["camera_id"]: c["config"] for c in page._cameras}
    assert configs_by_id[master_id]["inter_cam_sync_value"] == 1
    assert configs_by_id[slave_id]["inter_cam_sync_value"] == 2


def test_start_multi_camera_session_requested_leaves_inter_cam_sync_value_none_for_unconfigured_camera_model(qapp, monkeypatch, tmp_path):
    # No camera.inter_cam_sync entry at all for this device name - genlock is
    # skipped entirely rather than guessing a possibly-wrong raw value.
    window = _window_after_config_chosen(qapp, monkeypatch, tmp_path)

    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        window._on_calibration_done()
        window._on_tuning_done()

    window._on_start_multi_camera_session_requested()

    page = window.multi_camera_live_session_page
    only_config = page._cameras[0]["config"]
    assert only_config["inter_cam_sync_value"] is None
