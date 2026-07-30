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

def test_on_device_chosen_shows_error_for_missing_stream_a_key(qapp, monkeypatch):
    settings = _minimal_settings({"Intel RealSense D455": {"stream_b": [dict(
        stream_type="color", stream_index=0, width=1280, height=720, fps=30, format="bgr8",
    )]}})  # stream_a key missing entirely
    window = _make_window(qapp, settings)
    calls = _capture_critical(monkeypatch)

    window._on_device_chosen("SN123", "Intel RealSense D455")

    assert len(calls) == 1
    assert "invalid" in calls[0][0][2].lower()
    assert window.stack.currentWidget() is not window.stream_config_page


def test_on_device_chosen_shows_error_for_unknown_stream_type_string(qapp, monkeypatch):
    settings = _minimal_settings({"Intel RealSense D455": {
        "stream_a": [dict(stream_type="colour", stream_index=0, width=1280, height=720, fps=30, format="bgr8")],
        "stream_b": [dict(stream_type="color", stream_index=0, width=1280, height=720, fps=30, format="bgr8")],
    }})
    window = _make_window(qapp, settings)
    calls = _capture_critical(monkeypatch)

    window._on_device_chosen("SN123", "Intel RealSense D455")

    assert len(calls) == 1
    assert "invalid" in calls[0][0][2].lower()
    assert window.stack.currentWidget() is not window.stream_config_page


# --- Sub-finding 6c: a curated list matching nothing the device reports
# must show an error, not a silently empty/unusable Stream Select page ---

def test_on_device_chosen_shows_error_when_no_device_options_match_curated_list(qapp, monkeypatch):
    settings = _minimal_settings({"Intel RealSense D455": {
        "stream_a": [dict(stream_type="infrared", stream_index=1, width=1920, height=1080, fps=60, format="y8")],
        "stream_b": [dict(stream_type="color", stream_index=0, width=1280, height=720, fps=30, format="bgr8")],
    }})
    window = _make_window(qapp, settings)
    calls = _capture_critical(monkeypatch)
    # Device only reports IR1/COLOR0 at 1280x720 - stream_a's curated entry
    # (1920x1080@60) doesn't match anything, stream_b's does.
    monkeypatch.setattr(main_window_module, "list_video_stream_options", lambda ctx, serial: [IR1, COLOR0])

    window._on_device_chosen("SN123", "Intel RealSense D455")

    assert len(calls) == 1
    message = calls[0][0][2].lower()
    assert "stream a" in message
    assert "stream b" not in message  # only the empty side is named
    assert window.stack.currentWidget() is not window.stream_config_page


def test_on_device_chosen_succeeds_when_curated_entries_all_match(qapp, monkeypatch):
    settings = _minimal_settings({"Intel RealSense D455": {
        "stream_a": [dict(stream_type="infrared", stream_index=1, width=1280, height=720, fps=30, format="y8")],
        "stream_b": [dict(stream_type="color", stream_index=0, width=1280, height=720, fps=30, format="bgr8")],
    }})
    window = _make_window(qapp, settings)
    calls = _capture_critical(monkeypatch)
    monkeypatch.setattr(main_window_module, "list_video_stream_options", lambda ctx, serial: [IR1, COLOR0])

    window._on_device_chosen("SN123", "Intel RealSense D455")

    assert calls == []
    assert window.stack.currentWidget() is window.stream_config_page
    assert window.stream_config_page.combo_a.count() == 1
    assert window.stream_config_page.combo_b.count() == 1
