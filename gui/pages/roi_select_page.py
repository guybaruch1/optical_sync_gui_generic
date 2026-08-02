"""Wizard step 3: same ROI-selection approach as
optical_sync_poc_/roi_picker.py - capture one settled frame per stream
with all LEDs lit (for visibility), then use cv2.selectROI's native popup
window to draw each box directly in image pixel space.

Generalized from the old hardcoded IR-sensor/RGB-sensor version to the
generic pick_a/pick_b picks the Stream Select page now produces:
resolve_and_group figures out whether the two picks live on one shared
sensor object or two distinct ones, camera_controls (also from Stream
Select) carries whatever emitter/exposure settings each of those groups
needs applied before capturing, and capture_synced_frame_pair itself now
takes that groups list directly instead of a hardcoded 4-arg
(stereo_sensor, ir_profile, rgb_sensor, color_profile) signature.

Chosen over an embedded Qt drag-and-drop over a live QLabel preview: that
approach required converting a rubber-band selection from on-screen widget
pixels back to native image pixels (the widget is stretched to whatever
size the layout gives it), and that conversion turned out to be a real
source of bugs in practice. cv2.selectROI sidesteps the whole problem -
it returns coordinates in the same array passed to it, regardless of how
its own window is sized/zoomed on screen, exactly like the original
standalone script relied on.
"""

import time

import cv2
import pyrealsense2 as rs
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QPushButton

from domain.realsense_utils import decode_frame
from engine.streams import (
    find_device_by_serial, resolve_and_group, capture_synced_frame_pair,
    set_emitter_enabled, enable_auto_exposure, set_manual_exposure,
)
from engine.dual_panel_control import turn_all_leds_on, turn_all_leds_off


_STREAM_TYPE_LABELS = {
    rs.stream.infrared: "Infrared",
    rs.stream.color: "Color",
}


def stream_label(pick):
    """Human-readable label for a stream pick, e.g. "Infrared 1" / "Color 2"
    - a cv2.selectROI window title doesn't need the resolution/fps/format
    detail gui/pages/stream_config_page.py's sensor-options combo entries
    show, just which physical stream this is."""
    stream_type = pick["stream_type"]
    type_label = _STREAM_TYPE_LABELS.get(stream_type, stream_type.name.capitalize())
    return "{} {}".format(type_label, pick["stream_index"])


def _select_roi(image, window_title):
    """Interactive ROI selection via a native OpenCV popup window. Returns
    (x, y, w, h) in the given image's own pixel coordinates, or None if
    cancelled (Enter=confirm, C=cancel, matching the original script)."""
    display = image
    if len(image.shape) == 2:  # IR is single-channel; selectROI wants something displayable
        display = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)

    x, y, w, h = map(int, cv2.selectROI(window_title, display, showCrosshair=True, fromCenter=False))
    cv2.destroyWindow(window_title)
    if w == 0 or h == 0:
        return None
    return x, y, w, h


def _apply_camera_controls(groups, camera_controls):
    """Applies the ONE global camera_controls dict (from Stream Select's
    _read_camera_controls) uniformly to every resolved sensor group -
    Stream Select no longer configures emitter/exposure/gain per resolved
    sensor group, just once for both streams together. A sensor that
    doesn't support a given setting (e.g. emitter control on a color-only
    sensor) just gets a surfaced warning for that setting, same as before.
    Returns a list of warning strings for any setting any sensor doesn't
    support, so the caller can surface them without silently proceeding."""
    warnings = []
    for sensor, profiles in groups:
        # The "Disable IR emitter" checkbox is global (always shown/checked
        # regardless of what's picked - see stream_config_page.py), but
        # emitter control only makes sense for a group that actually
        # includes an infrared stream. Without this gate, a pure
        # color+color (Dual RGB) pairing would get a spurious "not
        # supported" warning on every single run, since the checkbox
        # defaults to checked but no sensor in the group supports emitter
        # control at all.
        group_has_infrared = any(p.stream_type() == rs.stream.infrared for p in profiles)
        if camera_controls["emitter_enabled"] is not None and group_has_infrared:
            if not set_emitter_enabled(sensor, camera_controls["emitter_enabled"]):
                warnings.append(
                    "WARNING: emitter_enabled not supported on sensor - confirm the "
                    "emitter state manually."
                )
        if camera_controls["auto_exposure"]:
            if not enable_auto_exposure(sensor):
                warnings.append(
                    "WARNING: enable_auto_exposure not supported on sensor - confirm "
                    "auto-exposure manually."
                )
        else:
            if not set_manual_exposure(sensor, camera_controls["exposure"], camera_controls["gain"]):
                warnings.append(
                    "WARNING: manual exposure/gain not supported on sensor - confirm "
                    "exposure settings manually."
                )
    return warnings


class RoiSelectPage(QWidget):
    roi_chosen = Signal(tuple)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pending_args = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Click below to light all LEDs and capture a frame, then drag a box "
            "on each popup window (Enter=confirm, C=cancel)."
        ))
        self.capture_button = QPushButton("Capture && Select ROI")
        self.capture_button.clicked.connect(self._on_capture_clicked)
        layout.addWidget(self.capture_button)
        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

    def set_context(self, ctx, device_serial, pick_a, pick_b, camera_controls, settle_frames=15,
                     dual_panel_config=None):
        self._pending_args = dict(
            ctx=ctx, device_serial=device_serial, pick_a=pick_a, pick_b=pick_b,
            camera_controls=camera_controls, settle_frames=settle_frames,
            dual_panel_config=dual_panel_config,
        )
        self.status_label.setText("")

    def _on_capture_clicked(self):
        if self._pending_args is None:
            return
        self.capture_button.setEnabled(False)
        try:
            self._capture_and_select(**self._pending_args)
        except Exception as exc:
            self.status_label.setText("Error: {}".format(exc))
        finally:
            self.capture_button.setEnabled(True)

    def _capture_and_select(self, ctx, device_serial, pick_a, pick_b, camera_controls, settle_frames,
                             dual_panel_config):
        device = find_device_by_serial(ctx, device_serial)
        groups = resolve_and_group(device, pick_a, pick_b)

        warnings = _apply_camera_controls(groups, camera_controls)
        if warnings:
            self.status_label.setText("\n".join(warnings))

        def turn_on_all_leds():
            turn_all_leds_on(dual_panel_config)
            time.sleep(0.5)  # let the panel(s) actually reach full brightness

        # Same capture mechanism roi_picker.py actually used - see
        # calibration_page.py's matching comment for why this replaced the
        # rs.pipeline()-based ContinuousCapture that was here before.
        try:
            frames = capture_synced_frame_pair(
                groups,
                on_both_streaming=turn_on_all_leds,
                settle_frames=settle_frames,
            )
        finally:
            # Cleanup-only call: LEDPanel.all_leds_off() now raises if the
            # panel command itself keeps failing (see LEDPanel._run). Swallow
            # it here rather than letting it replace whatever exception the
            # try block above may have raised (a finally-block exception
            # always masks one from the try block in Python) - still surface
            # it, since the operator needs to know to check the panel by hand.
            try:
                turn_all_leds_off(dual_panel_config)
            except Exception as exc:
                self.status_label.setText("Warning: failed to turn LEDs off during cleanup: {}".format(exc))

        image_a = decode_frame(
            frames[(pick_a["stream_type"], pick_a["stream_index"])],
            pick_a["format"], pick_a["width"], pick_a["height"],
        )
        image_b = decode_frame(
            frames[(pick_b["stream_type"], pick_b["stream_index"])],
            pick_b["format"], pick_b["width"], pick_b["height"],
        )

        label_a = stream_label(pick_a)
        label_b = stream_label(pick_b)

        roi_a = _select_roi(image_a, "{} - drag ROI, Enter=OK, C=Cancel".format(label_a))
        if roi_a is None:
            self.status_label.setText("{} ROI selection cancelled - try again.".format(label_a))
            return

        roi_b = _select_roi(image_b, "{} - drag ROI, Enter=OK, C=Cancel".format(label_b))
        if roi_b is None:
            self.status_label.setText("{} ROI selection cancelled - try again.".format(label_b))
            return

        self.status_label.setText("ROI selected: {}={} {}={}".format(label_a, roi_a, label_b, roi_b))
        self.roi_chosen.emit((roi_a, roi_b))
