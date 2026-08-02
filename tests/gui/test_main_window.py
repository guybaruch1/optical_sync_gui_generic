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
        "emitter_enabled": False, "auto_exposure": True, "exposure": None, "gain": None,
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
    }
    settings["dual_panel"] = {
        "panel_a_port": 0, "panel_b_port": 1, "relay_port": 6,
        "relay_com_port": "COM6", "relay_pulse_duration_s": 0.2,
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
        "emitter_enabled": False, "auto_exposure": True, "exposure": None, "gain": None,
    }))
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


def test_on_tuning_done_passes_tuned_per_stream_thresholds_to_live_session(qapp, monkeypatch, tmp_path):
    window = _window_after_config_chosen(qapp, monkeypatch, tmp_path)

    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        window._on_calibration_done()
        # Tune each stream independently, away from the settings.yaml default.
        window.threshold_tuning_page.stream_a_threshold_fraction_spinbox.setValue(0.5)
        window.threshold_tuning_page.stream_b_threshold_fraction_spinbox.setValue(0.75)
        window.threshold_tuning_page.switch_time_spinbox.setValue(5)
        window._on_tuning_done()

    assert window.stack.currentWidget() is window.live_session_page
    ctx = window.live_session_page._context
    assert list(ctx["stream_a_threshold"]) == [200.0]  # 100 + 0.5*200
    assert list(ctx["stream_b_threshold"]) == [500.0]  # 200 + 0.75*400
    assert ctx["switch_time_ms"] == 5


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
        window._on_tuning_done()

    assert window.threshold_tuning_page._context["dual_panel_config"] == window.settings["dual_panel"]
    assert window.live_session_page._context["dual_panel_config"] == window.settings["dual_panel"]
