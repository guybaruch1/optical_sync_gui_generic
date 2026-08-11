"""Wizard step 2: pick a named test (e.g. "IR1 vs IR2 sync", "IR vs RGB
sync" - settings.yaml's camera.stream_options, per connected camera model)
and a resolution/fps/format "sensor options" pairing for it, configure
camera controls (IR emitter + a shared auto/manual exposure MODE, but two
independent exposure VALUES underneath it - see _build_camera_control_
group), and preview live pairing quality (bundle/frame-number/timestamp/
delta overlay, plus matching console logging) for the currently selected
pair before committing to it.

Generalized from the old two-independent-combo version (separate "Stream
A"/"Stream B" pickers): a test's two streams are a FIXED identity
(stream_type/stream_index never changes across sensor options) chosen by
settings.yaml, not independently narrowed by the operator - so there's
exactly one remaining choice, which resolution/fps/format pairing to run
that test at. This also removes the old collision-avoidance problem
entirely ("Stream A" and "Stream B" landing on the same stream): a test's
two streams differ by construction (engine.streams.resolve_camera_tests
only ever pairs a test's own distinct stream_a_identity/stream_b_identity).

The emitter checkbox and auto/manual MODE stay ONE global choice regardless
of how Stream A/B resolve to physical sensors at capture time
(engine.streams.resolve_and_group can still fold them onto one shared
sensor or split them across two - that's an orthogonal, capture-time
concern) - simpler for the operator than juggling per-sensor-group mode
toggles. Exposure's actual VALUE is the one exception: two independent
spinboxes, one per stream, since different sensors (IR vs RGB, or two
different IR sensors) have genuinely different brightness characteristics -
engine.streams.exposure_for_group routes each one to whichever resolved
sensor group actually contains that stream. A group containing BOTH
streams (the Dual-RGB shape, one shared physical sensor) can only ever
have one real exposure value in hardware regardless of what the UI offers
per stream - Stream A's value wins in that case (see exposure_for_group's
own docstring)."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QComboBox, QPushButton,
    QGroupBox, QCheckBox, QRadioButton, QButtonGroup, QSpinBox,
)

from engine.stream_preview_thread import StreamPreviewThread
from gui.widgets.video_panel import VideoPanel


_STREAM_TYPE_SHORT_LABELS = {"infrared": "IR", "color": "RGB"}


def _side_short_label(pick):
    return _STREAM_TYPE_SHORT_LABELS.get(pick["stream_type"].name, pick["stream_type"].name.capitalize())


def _full_side_desc(pick):
    return "{}x{}@{}fps ({}, {})".format(
        pick["width"], pick["height"], pick["fps"], _side_short_label(pick), pick["format"].name,
    )


def _sensor_option_label(option):
    """Formats one engine.streams.resolve_camera_tests sensor-options entry
    ({"pick_a", "pick_b"}) as a combo-box label. When both sides share
    resolution/fps (the common case), shown once - e.g. "1280x720 @ 30fps
    (y8)" if the formats also match, or "1280x720 @ 30fps (IR: y8, RGB:
    bgr8)" if only the formats differ. Falls back to describing both sides
    in full (e.g. for a device where the two streams' max resolutions
    genuinely differ) if resolution/fps themselves differ between sides."""
    pick_a, pick_b = option["pick_a"], option["pick_b"]
    same_res_fps = (
        (pick_a["width"], pick_a["height"], pick_a["fps"]) == (pick_b["width"], pick_b["height"], pick_b["fps"])
    )
    if not same_res_fps:
        return "{} vs {}".format(_full_side_desc(pick_a), _full_side_desc(pick_b))
    res_fps = "{}x{} @ {}fps".format(pick_a["width"], pick_a["height"], pick_a["fps"])
    if pick_a["format"] == pick_b["format"]:
        return "{} ({})".format(res_fps, pick_a["format"].name)
    return "{} ({}: {}, {}: {})".format(
        res_fps, _side_short_label(pick_a), pick_a["format"].name, _side_short_label(pick_b), pick_b["format"].name,
    )


class StreamConfigPage(QWidget):
    config_chosen = Signal(tuple)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ctx = None
        self.device_serial = None
        self.preview_thread = None
        self._tests = []
        self._preferred_a = None
        self._preferred_b = None
        # settings.yaml camera_sync.enable_depth_for_ir_sync, set via
        # populate() - so this page's own pairing-quality preview shows the
        # same IR/RGB sync fix (or lack of it) the real run downstream will
        # actually use, rather than always defaulting to depth-on.
        self._enable_depth_for_ir_sync = True

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.combo_test = QComboBox()
        self.combo_sensor_options = QComboBox()
        form.addRow(QLabel("Test:"), self.combo_test)
        form.addRow(QLabel("Sensor Options:"), self.combo_sensor_options)
        layout.addLayout(form)

        self.combo_test.currentIndexChanged.connect(self._on_test_changed)

        self._camera_controls = self._build_camera_control_group()
        layout.addWidget(self._camera_controls["group_box"])

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
        selection exists), always read live from the sensor-options combo
        rather than cached - so it can never drift out of sync with what
        the operator actually has selected right now. Part of this page's
        documented produced interface (alongside populate()/config_chosen) -
        gui/main_window.py reads this to get the live picks without needing
        to reach into the combos directly."""
        option = self.combo_sensor_options.currentData()
        return option["pick_a"] if option is not None else None

    @property
    def pick_b(self):
        """The currently-selected Stream B option dict (or None). See
        pick_a's docstring."""
        option = self.combo_sensor_options.currentData()
        return option["pick_b"] if option is not None else None

    @property
    def current_test_name(self):
        """The currently-selected test's name (or None before a selection
        exists) - read by gui/main_window.py to persist as GuiState's
        last_test_name prefill hint, since config_chosen's own emitted
        payload stays (pick_a, pick_b, camera_controls) with no "test"
        concept in it at all - ROI Select/Calibration/Live Session
        downstream never need to know tests exist."""
        return self.combo_test.currentData()

    def populate(self, ctx, device_serial, tests, preferred_a=None, preferred_b=None,
                 preferred_test_name=None, enable_depth_for_ir_sync=True):
        """`tests` is engine.streams.resolve_camera_tests's output, already
        filtered by gui/main_window.py to tests with at least one
        sensor_options entry matching this connected device - each entry is
        {"test_name": str, "options": [{"pick_a", "pick_b"}, ...]}.

        preferred_test_name pre-selects that test if present in `tests`
        (GuiState's last-used test name); otherwise the first test in the
        list is used. preferred_a is an optional partial-match dict (e.g.
        {"width": 1280, "height": 720, "fps": 30} from settings.yaml's
        camera.stream_a) - the first sensor-options entry whose `pick_a`
        matches every key given is pre-selected within whichever test ends
        up selected; the user can still pick something else if it doesn't
        suit this rig/camera. preferred_b is accepted for interface
        symmetry with the old two-combo version but not currently used for
        preselection (only pick_a is matched, same as this project's other
        preselection logic). enable_depth_for_ir_sync is settings.yaml's
        camera_sync.enable_depth_for_ir_sync, forwarded into the pairing-
        quality preview's own ContinuousCapture - see _on_start_preview_clicked."""
        self.ctx = ctx
        self.device_serial = device_serial
        self._tests = list(tests)
        self._preferred_a = preferred_a
        self._preferred_b = preferred_b
        self._enable_depth_for_ir_sync = enable_depth_for_ir_sync

        self.combo_test.blockSignals(True)
        self.combo_test.clear()
        for test in self._tests:
            self.combo_test.addItem(test["test_name"], userData=test["test_name"])

        test_index = 0
        if preferred_test_name is not None:
            found = self.combo_test.findData(preferred_test_name)
            if found != -1:
                test_index = found
        if self.combo_test.count():
            self.combo_test.setCurrentIndex(test_index)
        self.combo_test.blockSignals(False)

        self._populate_sensor_options(test_index)

    def _on_test_changed(self, index):
        self._populate_sensor_options(index)

    def _populate_sensor_options(self, test_index):
        self.combo_sensor_options.blockSignals(True)
        self.combo_sensor_options.clear()
        if 0 <= test_index < len(self._tests):
            for option in self._tests[test_index]["options"]:
                self.combo_sensor_options.addItem(_sensor_option_label(option), userData=option)
        self.combo_sensor_options.blockSignals(False)
        self._preselect_sensor_options()

    def _preselect_sensor_options(self):
        if not self._preferred_a:
            return
        for index in range(self.combo_sensor_options.count()):
            option = self.combo_sensor_options.itemData(index)
            if all(option["pick_a"].get(key) == value for key, value in self._preferred_a.items()):
                self.combo_sensor_options.setCurrentIndex(index)
                return

    def _build_camera_control_group(self):
        """ONE global emitter checkbox + auto/manual MODE toggle, shared by
        both streams together - but exposure ITSELF is two independent
        values underneath that shared mode, one spinbox per stream (see
        exposure_a_spin/exposure_b_spin below). Different sensors (IR vs
        RGB, or two different IR sensors) have different brightness
        characteristics - same reasoning Threshold Tuning's own independent
        per-stream threshold fraction already uses - so one shared exposure
        value across both streams doesn't fit every rig. Applied at capture
        time via gui.pages.roi_select_page._apply_camera_controls (and its
        duplicated inline copies), which route each stream's own exposure
        value to whichever resolved sensor group actually contains that
        stream (engine.streams.exposure_for_group). The "Disable IR
        emitter" checkbox is always present; it's simply a no-op (with a
        surfaced warning) if neither resolved sensor actually supports
        emitter control."""
        box = QGroupBox("Camera Controls")
        box_layout = QVBoxLayout(box)

        emitter_checkbox = QCheckBox("Disable IR emitter")
        # Default to checked (emitter disabled): every prior version of this
        # app's camera setup (before per-sensor-group controls were added,
        # and the sibling optical_sync_gui project) hardcoded the IR emitter
        # OFF unconditionally, since the structured-light projector pattern
        # corrupts LED blob detection and calibrated on/off thresholds if
        # left on. Manual opt-out (unchecking) is still available for
        # whoever needs the emitter on.
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
        exposure_row.addWidget(QLabel("Exposure A:"))
        exposure_a_spin = QSpinBox()
        exposure_a_spin.setRange(1, 1000000)
        exposure_a_spin.setValue(8500)
        exposure_a_spin.setEnabled(False)
        exposure_row.addWidget(exposure_a_spin)
        exposure_row.addWidget(QLabel("Exposure B:"))
        exposure_b_spin = QSpinBox()
        exposure_b_spin.setRange(1, 1000000)
        exposure_b_spin.setValue(8500)
        exposure_b_spin.setEnabled(False)
        exposure_row.addWidget(exposure_b_spin)
        box_layout.addLayout(exposure_row)

        manual_radio.toggled.connect(exposure_a_spin.setEnabled)
        manual_radio.toggled.connect(exposure_b_spin.setEnabled)

        return {
            "group_box": box,
            "emitter_checkbox": emitter_checkbox,
            "auto_radio": auto_radio,
            "manual_radio": manual_radio,
            "exposure_a_spin": exposure_a_spin,
            "exposure_b_spin": exposure_b_spin,
        }

    def _read_camera_controls(self):
        """Returns the single global camera-control dict - applied
        uniformly to every resolved sensor group (see
        gui.pages.roi_select_page._apply_camera_controls), which then picks
        exposure_a vs exposure_b per group via engine.streams.
        exposure_for_group depending on which stream that group actually
        contains. No "gain" key - manual exposure mode never touches gain
        at all (see engine.streams.set_manual_exposure's docstring for
        why), so there is nothing for this dict to carry for it."""
        w = self._camera_controls
        auto_exposure = w["auto_radio"].isChecked()
        return {
            "emitter_enabled": not w["emitter_checkbox"].isChecked(),
            "auto_exposure": auto_exposure,
            "exposure_a": None if auto_exposure else w["exposure_a_spin"].value(),
            "exposure_b": None if auto_exposure else w["exposure_b_spin"].value(),
        }

    def _streams_are_identical(self, pick_a, pick_b):
        return pick_a["stream_type"] == pick_b["stream_type"] and pick_a["stream_index"] == pick_b["stream_index"]

    def _on_start_preview_clicked(self):
        pick_a = self.pick_a
        pick_b = self.pick_b
        if pick_a is None or pick_b is None:
            return
        if self._streams_are_identical(pick_a, pick_b):
            # Defense-in-depth only: a well-formed settings.yaml test always
            # has distinct stream_a_identity/stream_b_identity, but a
            # misconfigured one could accidentally define the same stream
            # twice - engine.streams.resolve_and_group would raise on this
            # too, catch it here first with a clearer message.
            self.status_label.setText(
                "This test's Stream A and Stream B are the same physical stream - fix its "
                "settings.yaml entry."
            )
            return

        self.status_label.setText("")
        self.preview_thread = StreamPreviewThread(
            self.ctx, self.device_serial, pick_a, pick_b,
            enable_depth_for_ir_sync=self._enable_depth_for_ir_sync,
        )
        self.preview_thread.frame_ready.connect(self.preview_panel.set_frame)
        self.preview_thread.error.connect(self._on_preview_error)
        self.preview_thread.start()

        self.start_preview_button.setEnabled(False)
        self.stop_preview_button.setEnabled(True)
        self.combo_test.setEnabled(False)
        self.combo_sensor_options.setEnabled(False)

    def _on_stop_preview_clicked(self):
        self._stop_preview()

    def _stop_preview(self):
        if self.preview_thread is not None:
            self.preview_thread.request_stop()
            self.preview_thread.wait()
            self.preview_thread = None
        self.start_preview_button.setEnabled(True)
        self.stop_preview_button.setEnabled(False)
        self.combo_test.setEnabled(True)
        self.combo_sensor_options.setEnabled(True)

    def _on_preview_error(self, message):
        self.status_label.setText("Error: {}".format(message))
        self._stop_preview()

    def _on_next_clicked(self):
        pick_a = self.pick_a
        pick_b = self.pick_b
        if pick_a is None or pick_b is None:
            return
        if self._streams_are_identical(pick_a, pick_b):
            self.status_label.setText(
                "This test's Stream A and Stream B are the same physical stream - fix its "
                "settings.yaml entry."
            )
            return
        self._stop_preview()
        camera_controls = self._read_camera_controls()
        self.config_chosen.emit((pick_a, pick_b, camera_controls))
