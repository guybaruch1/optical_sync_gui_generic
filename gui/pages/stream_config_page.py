"""Wizard step 2: pick FPS/resolution for the IR and RGB streams, and
preview live pairing quality (bundle/frame-number/timestamp/delta overlay,
plus matching console logging) for the currently selected combo before
committing to it."""

import pyrealsense2 as rs
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QComboBox, QPushButton,
)

from engine.streams import list_supported_profiles
from engine.stream_preview_thread import StreamPreviewThread
from gui.widgets.video_panel import VideoPanel


class StreamConfigPage(QWidget):
    config_chosen = Signal(tuple)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ctx = None
        self.device_serial = None
        self.preview_thread = None

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.ir_combo = QComboBox()
        self.rgb_combo = QComboBox()
        form.addRow(QLabel("IR resolution/fps:"), self.ir_combo)
        form.addRow(QLabel("RGB resolution/fps:"), self.rgb_combo)
        layout.addLayout(form)

        preview_row = QHBoxLayout()
        self.start_preview_button = QPushButton("Start Preview")
        self.start_preview_button.clicked.connect(self._on_start_preview_clicked)
        self.stop_preview_button = QPushButton("Stop Preview")
        self.stop_preview_button.clicked.connect(self._on_stop_preview_clicked)
        self.stop_preview_button.setEnabled(False)
        preview_row.addWidget(self.start_preview_button)
        preview_row.addWidget(self.stop_preview_button)
        layout.addLayout(preview_row)

        self.preview_panel = VideoPanel()
        layout.addWidget(self.preview_panel, stretch=1)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

        self.next_button = QPushButton("Next")
        self.next_button.clicked.connect(self._on_next_clicked)
        layout.addWidget(self.next_button)

    def populate(self, ctx, device_serial, stereo_sensor, rgb_sensor, preferred_ir=None, preferred_rgb=None):
        """preferred_ir/preferred_rgb are optional (width, height, fps) tuples
        (e.g. from settings.yaml's camera.ir/camera.color) - if the connected
        camera actually reports that exact combo, it's pre-selected in the
        dropdown instead of leaving it at whatever comes first; the user can
        still pick something else if it doesn't suit this rig/camera."""
        self.ctx = ctx
        self.device_serial = device_serial

        ir_profiles = list_supported_profiles(stereo_sensor, rs.stream.infrared, rs.format.y8)
        rgb_profiles = list_supported_profiles(rgb_sensor, rs.stream.color, rs.format.yuyv)

        self.ir_combo.clear()
        for width, height, fps in ir_profiles:
            self.ir_combo.addItem("{}x{}@{}fps".format(width, height, fps), userData=(width, height, fps))
        self._preselect(self.ir_combo, preferred_ir)

        self.rgb_combo.clear()
        for width, height, fps in rgb_profiles:
            self.rgb_combo.addItem("{}x{}@{}fps".format(width, height, fps), userData=(width, height, fps))
        self._preselect(self.rgb_combo, preferred_rgb)

    def _preselect(self, combo, preferred):
        """Uses findText() rather than findData(): PySide6's findData() is
        unreliable for tuple userData (returns -1 even for an exact match
        once the combo has more than a couple of entries), which silently
        defeated settings.yaml's preferred resolution/fps and left the combo
        on whatever sorted first."""
        if preferred is None:
            return
        width, height, fps = preferred
        index = combo.findText("{}x{}@{}fps".format(width, height, fps))
        if index != -1:
            combo.setCurrentIndex(index)

    def _on_start_preview_clicked(self):
        ir_choice = self.ir_combo.currentData()
        rgb_choice = self.rgb_combo.currentData()
        if ir_choice is None or rgb_choice is None:
            return
        ir_width, ir_height, ir_fps = ir_choice
        rgb_width, rgb_height, rgb_fps = rgb_choice

        self.status_label.setText("")
        self.preview_thread = StreamPreviewThread(
            self.ctx, self.device_serial, (ir_width, ir_height), ir_fps, (rgb_width, rgb_height), rgb_fps,
        )
        self.preview_thread.frame_ready.connect(self.preview_panel.set_frame)
        self.preview_thread.error.connect(self._on_preview_error)
        self.preview_thread.start()

        self.start_preview_button.setEnabled(False)
        self.stop_preview_button.setEnabled(True)
        self.ir_combo.setEnabled(False)
        self.rgb_combo.setEnabled(False)

    def _on_stop_preview_clicked(self):
        self._stop_preview()

    def _stop_preview(self):
        if self.preview_thread is not None:
            self.preview_thread.request_stop()
            self.preview_thread.wait()
            self.preview_thread = None
        self.start_preview_button.setEnabled(True)
        self.stop_preview_button.setEnabled(False)
        self.ir_combo.setEnabled(True)
        self.rgb_combo.setEnabled(True)

    def _on_preview_error(self, message):
        self.status_label.setText("Error: {}".format(message))
        self._stop_preview()

    def _on_next_clicked(self):
        ir_choice = self.ir_combo.currentData()
        rgb_choice = self.rgb_combo.currentData()
        if ir_choice is not None and rgb_choice is not None:
            self._stop_preview()
            ir_width, ir_height, ir_fps = ir_choice
            rgb_width, rgb_height, rgb_fps = rgb_choice
            self.config_chosen.emit((ir_width, ir_height, ir_fps, rgb_width, rgb_height, rgb_fps))
