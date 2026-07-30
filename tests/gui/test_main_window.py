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
