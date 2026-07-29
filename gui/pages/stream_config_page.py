"""Wizard step 2: pick any two streams the connected device offers (IR, RGB,
or two of the same type sharing one sensor), configure per-sensor camera
controls (IR emitter, auto/manual exposure+gain), and preview live pairing
quality (bundle/frame-number/timestamp/delta overlay, plus matching console
logging) for the currently selected pair before committing to it.

Generalized from the old hardcoded IR-combo/RGB-combo pair: engine.streams.
list_video_stream_options already returns fully-specified stream options -
sensor_index/stream_type/stream_index/format/width/height/fps all bundled
into one dict per profile - so a SINGLE combo per side, listing every
fully-specified option as one label (e.g. "Infrared 1 - 1280x720@30fps
(y8)"), is enough. A second resolution/fps combo per side would only be
useful if stream *identity* (stream_type/stream_index) and stream
*resolution/fps* needed independent narrowing - they don't here, since
picking a different resolution/fps for the same stream identity is just
picking a different entry in the same combo. Two combos per side would add
a layer of "first narrow identity, then narrow resolution" indirection for
no real benefit given the option list is already small and fully expanded.
"""

import pyrealsense2 as rs
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QComboBox, QPushButton,
    QGroupBox, QCheckBox, QRadioButton, QButtonGroup, QSpinBox,
)

from engine.streams import list_video_stream_options
from engine.stream_preview_thread import StreamPreviewThread
from gui.widgets.video_panel import VideoPanel


_STREAM_TYPE_LABELS = {
    rs.stream.infrared: "Infrared",
    rs.stream.color: "Color",
}


def _stream_option_label(option):
    """Formats one list_video_stream_options() entry as a combo-box label,
    e.g. "Infrared 1 - 1280x720@30fps (y8)" / "Color 1 - 1280x720@30fps
    (bgr8)". stream_index is ALWAYS appended, for both infrared and color:
    a Dual RGB device (or any device exposing two color stream indices on
    one sensor) can otherwise produce two options that are identical in
    every other field (same sensor, same resolution/fps/format), which
    would render as two visually indistinguishable combo entries - the
    operator would have no way to tell which one they were picking for
    Stream A vs Stream B. Always including stream_index keeps every label
    unique regardless of how many streams of a given type the device
    exposes."""
    stream_type = option["stream_type"]
    type_label = _STREAM_TYPE_LABELS.get(stream_type, stream_type.name.capitalize())
    type_label = "{} {}".format(type_label, option["stream_index"])
    return "{} - {}x{}@{}fps ({})".format(
        type_label, option["width"], option["height"], option["fps"], option["format"].name,
    )


def group_camera_controls(pick_a, pick_b):
    """Device-independent mirror of engine.streams.resolve_and_group's
    grouping decision, for UI layout purposes only: decides how many
    emitter/exposure control groups Stream Select should show, purely from
    the two picks' own sensor_index fields - no live `device` handle
    needed. Same sensor_index -> the two picks will end up opened on one
    physical sensor object at capture time (resolve_and_group returns one
    group for them), so show ONE control group; different sensor_index ->
    two sensor objects, so show TWO. The real resolve_and_group (which
    needs a live device to map sensor_index -> actual sensor object) is
    only called later, at capture time in gui/main_window.py.

    Returns a list of {"sensor_indices": [...], "has_infrared": bool}
    dicts, one per group - "has_infrared" is True if ANY pick folded into
    that group is an infrared stream (determines whether the group's
    "Disable IR emitter" checkbox should be shown at all)."""
    if pick_a["sensor_index"] == pick_b["sensor_index"]:
        picks_per_group = [[pick_a, pick_b]]
    else:
        picks_per_group = [[pick_a], [pick_b]]

    groups = []
    for picks in picks_per_group:
        groups.append({
            "sensor_indices": sorted({p["sensor_index"] for p in picks}),
            "has_infrared": any(p["stream_type"] == rs.stream.infrared for p in picks),
        })
    return groups


class StreamConfigPage(QWidget):
    config_chosen = Signal(tuple)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ctx = None
        self.device_serial = None
        self.preview_thread = None
        self._stream_options = []
        self._camera_control_widgets = []  # list of per-group widget dicts, see _build_camera_control_group

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.combo_a = QComboBox()
        self.combo_b = QComboBox()
        form.addRow(QLabel("Stream A:"), self.combo_a)
        form.addRow(QLabel("Stream B:"), self.combo_b)
        layout.addLayout(form)

        self.combo_a.currentIndexChanged.connect(self._refresh_camera_control_groups)
        self.combo_b.currentIndexChanged.connect(self._refresh_camera_control_groups)

        self.camera_controls_layout = QVBoxLayout()
        layout.addLayout(self.camera_controls_layout)

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

    @property
    def pick_a(self):
        """The currently-selected Stream A option dict (or None before a
        selection exists), always read live from the combo rather than
        cached - so it can never drift out of sync with what the operator
        actually has selected right now. Part of this page's documented
        produced interface (alongside populate()/config_chosen) - Task 18's
        gui/main_window.py rewiring reads this to get the live picks
        without needing to reach into combo_a/combo_b directly."""
        return self.combo_a.currentData()

    @property
    def pick_b(self):
        """The currently-selected Stream B option dict (or None). See
        pick_a's docstring."""
        return self.combo_b.currentData()

    def populate(self, ctx, device_serial, stream_options, preferred_a=None, preferred_b=None):
        """preferred_a/preferred_b are optional partial-match dicts (e.g.
        {"width": 1280, "height": 720, "fps": 30} from settings.yaml's
        camera.stream_a/stream_b) - the first option matching every key
        given is pre-selected in the combo instead of leaving it at
        whatever comes first; the user can still pick something else if it
        doesn't suit this rig/camera."""
        self.ctx = ctx
        self.device_serial = device_serial
        self._stream_options = list(stream_options)

        for combo, preferred in ((self.combo_a, preferred_a), (self.combo_b, preferred_b)):
            combo.blockSignals(True)
            combo.clear()
            for option in self._stream_options:
                combo.addItem(_stream_option_label(option), userData=option)
            combo.blockSignals(False)
            self._preselect(combo, preferred)

        self._avoid_collision(self.combo_a, self.combo_b)
        self._refresh_camera_control_groups()

    def _preselect(self, combo, preferred):
        if not preferred:
            return
        for index in range(combo.count()):
            option = combo.itemData(index)
            if all(option.get(key) == value for key, value in preferred.items()):
                combo.setCurrentIndex(index)
                return

    def _avoid_collision(self, combo_a, combo_b):
        """Called once after both combos have been preselected: if they
        landed on the exact same (stream_type, stream_index) - which
        happens whenever preferred_a/preferred_b match the same single
        option, e.g. settings.yaml's stream_a/stream_b defaults being
        identical dicts on first launch - advance combo_b to the first
        OTHER option that differs from combo_a's current selection, so the
        wizard never opens on an unusable "Stream A == Stream B" state. If
        every available option is the same stream (a single-stream device),
        leave combo_b where it is; the explicit guards in
        _on_next_clicked/_on_start_preview_clicked and engine.streams.
        resolve_and_group's own check still catch that before anything bad
        happens."""
        option_a = combo_a.currentData()
        option_b = combo_b.currentData()
        if option_a is None or option_b is None:
            return
        if (option_a["stream_type"], option_a["stream_index"]) != (option_b["stream_type"], option_b["stream_index"]):
            return
        for index in range(combo_b.count()):
            option = combo_b.itemData(index)
            if (option["stream_type"], option["stream_index"]) != (option_a["stream_type"], option_a["stream_index"]):
                combo_b.setCurrentIndex(index)
                return

    def _refresh_camera_control_groups(self):
        self._clear_camera_control_groups()

        pick_a = self.pick_a
        pick_b = self.pick_b
        if pick_a is None or pick_b is None:
            return

        groups = group_camera_controls(pick_a, pick_b)
        multiple = len(groups) > 1
        for group in groups:
            self._camera_control_widgets.append(self._build_camera_control_group(group, multiple))

    def _clear_camera_control_groups(self):
        while self.camera_controls_layout.count():
            item = self.camera_controls_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._camera_control_widgets = []

    def _build_camera_control_group(self, group, multiple):
        title = "Camera Controls"
        if multiple:
            title = "{} - Sensor {}".format(title, group["sensor_indices"][0])
        box = QGroupBox(title)
        box_layout = QVBoxLayout(box)

        emitter_checkbox = None
        if group["has_infrared"]:
            emitter_checkbox = QCheckBox("Disable IR emitter")
            # Default to checked (emitter disabled): both prior projects this
            # generalizes (optical_sync_gui, optical_sync_gui_d585) hardcoded
            # the IR emitter OFF unconditionally, since the structured-light
            # projector pattern corrupts LED blob detection and calibrated
            # on/off thresholds if left on. Manual opt-out (unchecking) is
            # still available for whoever needs the emitter on.
            emitter_checkbox.setChecked(True)
            box_layout.addWidget(emitter_checkbox)

        auto_radio = QRadioButton("Auto exposure")
        manual_radio = QRadioButton("Manual exposure")
        auto_radio.setChecked(True)
        exposure_mode_group = QButtonGroup(box)
        exposure_mode_group.addButton(auto_radio)
        exposure_mode_group.addButton(manual_radio)
        box_layout.addWidget(auto_radio)
        box_layout.addWidget(manual_radio)

        exposure_row = QHBoxLayout()
        exposure_row.addWidget(QLabel("Exposure:"))
        exposure_spin = QSpinBox()
        exposure_spin.setRange(1, 1000000)
        exposure_spin.setValue(8500)
        exposure_spin.setEnabled(False)
        exposure_row.addWidget(exposure_spin)
        exposure_row.addWidget(QLabel("Gain:"))
        gain_spin = QSpinBox()
        gain_spin.setRange(0, 128)
        gain_spin.setValue(16)
        gain_spin.setEnabled(False)
        exposure_row.addWidget(gain_spin)
        box_layout.addLayout(exposure_row)

        manual_radio.toggled.connect(exposure_spin.setEnabled)
        manual_radio.toggled.connect(gain_spin.setEnabled)

        self.camera_controls_layout.addWidget(box)

        return {
            "sensor_indices": group["sensor_indices"],
            "group_box": box,
            "emitter_checkbox": emitter_checkbox,
            "auto_radio": auto_radio,
            "manual_radio": manual_radio,
            "exposure_spin": exposure_spin,
            "gain_spin": gain_spin,
        }

    def camera_control_group_count(self):
        return len(self._camera_control_widgets)

    def _read_camera_controls(self):
        controls = []
        for group in self._camera_control_widgets:
            auto_exposure = group["auto_radio"].isChecked()
            emitter_checkbox = group["emitter_checkbox"]
            controls.append({
                "sensor_indices": group["sensor_indices"],
                "emitter_enabled": (
                    None if emitter_checkbox is None else not emitter_checkbox.isChecked()
                ),
                "auto_exposure": auto_exposure,
                "exposure": None if auto_exposure else group["exposure_spin"].value(),
                "gain": None if auto_exposure else group["gain_spin"].value(),
            })
        return controls

    def _on_start_preview_clicked(self):
        pick_a = self.pick_a
        pick_b = self.pick_b
        if pick_a is None or pick_b is None:
            return
        if pick_a["stream_type"] == pick_b["stream_type"] and pick_a["stream_index"] == pick_b["stream_index"]:
            self.status_label.setText(
                "Stream A and Stream B must be different streams - pick two different combo entries."
            )
            return

        self.status_label.setText("")
        self.preview_thread = StreamPreviewThread(self.ctx, self.device_serial, pick_a, pick_b)
        self.preview_thread.frame_ready.connect(self.preview_panel.set_frame)
        self.preview_thread.error.connect(self._on_preview_error)
        self.preview_thread.start()

        self.start_preview_button.setEnabled(False)
        self.stop_preview_button.setEnabled(True)
        self.combo_a.setEnabled(False)
        self.combo_b.setEnabled(False)

    def _on_stop_preview_clicked(self):
        self._stop_preview()

    def _stop_preview(self):
        if self.preview_thread is not None:
            self.preview_thread.request_stop()
            self.preview_thread.wait()
            self.preview_thread = None
        self.start_preview_button.setEnabled(True)
        self.stop_preview_button.setEnabled(False)
        self.combo_a.setEnabled(True)
        self.combo_b.setEnabled(True)

    def _on_preview_error(self, message):
        self.status_label.setText("Error: {}".format(message))
        self._stop_preview()

    def _on_next_clicked(self):
        pick_a = self.pick_a
        pick_b = self.pick_b
        if pick_a is None or pick_b is None:
            return
        if pick_a["stream_type"] == pick_b["stream_type"] and pick_a["stream_index"] == pick_b["stream_index"]:
            self.status_label.setText(
                "Stream A and Stream B must be different streams - pick two different combo entries."
            )
            return
        self._stop_preview()
        camera_controls = self._read_camera_controls()
        self.config_chosen.emit((pick_a, pick_b, camera_controls))
