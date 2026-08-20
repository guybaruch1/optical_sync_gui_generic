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
from engine.streams import find_device_by_serial
from engine.rgb_mode import get_mode
from gui.pages.roi_select_page import stream_label
from gui.widgets.video_panel import VideoPanel


_STREAM_TYPE_SHORT_LABELS = {"infrared": "IR", "color": "RGB"}

# Matches _build_camera_control_group's own hardcoded widget defaults
# (emitter_checkbox - "Disable IR emitter" - checked by default, i.e.
# emitter_enabled False; auto_radio checked by default; exposure spins
# default to 8500) - populate() applies this explicitly for a fresh Add (no
# preferred_camera_controls given), same reasoning as preferred_dual_panel's
# own explicit reset.
DEFAULT_CAMERA_CONTROLS = {
    "emitter_enabled": False, "auto_exposure": True, "exposure_a": 8500, "exposure_b": 8500,
}


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
    back_requested = Signal()
    mode_switch_requested = Signal(str)  # target_mode ("dual"/"dedicated")

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
        # The device's CURRENT RGB mode ("dual"/"dedicated"/None), fetched at
        # populate() time - None either means an unrecognized device (no
        # Dual/Dedicated RGB concept at all) or a lookup failure, both of
        # which hide mode_group_box entirely. _on_next_clicked compares the
        # radio selection against this to decide whether a switch is even
        # needed.
        self._current_rgb_mode = None

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.combo_test = QComboBox()
        self.combo_sensor_options = QComboBox()
        form.addRow(QLabel("Test:"), self.combo_test)
        form.addRow(QLabel("Sensor Options:"), self.combo_sensor_options)
        layout.addLayout(form)

        # Moved here from Device Select: switching RGB mode changes which
        # streams the device exposes, which would invalidate whatever Test/
        # Sensor Options this page already resolved - the switch has to
        # happen (and refresh those lists) on the SAME page that shows them,
        # not one page earlier where nothing downstream exists to invalidate
        # yet. Defaulted to whatever mode the device is actually in right
        # now (populate()); hidden entirely for a device with no recognized
        # Dual/Dedicated RGB concept (e.g. a D435/D455).
        self.mode_group_box = QGroupBox("RGB Mode")
        mode_layout = QVBoxLayout(self.mode_group_box)
        self.dual_radio = QRadioButton("Dual RGB (2C)")
        self.dedicated_radio = QRadioButton("Dedicated RGB (3C)")
        mode_button_group = QButtonGroup(self.mode_group_box)
        mode_button_group.addButton(self.dual_radio)
        mode_button_group.addButton(self.dedicated_radio)
        mode_layout.addWidget(self.dual_radio)
        mode_layout.addWidget(self.dedicated_radio)
        self.mode_group_box.setVisible(False)
        layout.addWidget(self.mode_group_box)

        # Manual operator toggle, per-camera-FLOW (this page is revisited
        # once per camera, and dual-panel need depends on which Test is
        # picked here - e.g. one D585 doing IR-vs-RGB needs two panels, the
        # same D585 doing IR-vs-IR needs one) - not a whole-test setting, and
        # not yet auto-inferred from the Test choice. Moved here from Device
        # Select so it sits next to the choice it actually depends on, and
        # so Edit (which skips Device Select) can still reach it. See
        # engine/dual_panel_control.py and settings.yaml's dual_panel:
        # section (hub port numbers/relay COM port - fixed wiring specifics,
        # not a per-run choice).
        self.dual_panel_checkbox = QCheckBox("Use dual LED panel (Acroname hub + external trigger)")
        layout.addWidget(self.dual_panel_checkbox)

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

        nav_row = QHBoxLayout()
        self.back_button = QPushButton("Back")
        self.back_button.clicked.connect(self._on_back_clicked)
        nav_row.addWidget(self.back_button)
        self.next_button = QPushButton("Next")
        self.next_button.clicked.connect(self._on_next_clicked)
        nav_row.addWidget(self.next_button)
        layout.addLayout(nav_row)

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
                 preferred_test_name=None, enable_depth_for_ir_sync=True, preferred_dual_panel=False,
                 preferred_camera_controls=None):
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
        quality preview's own ContinuousCapture - see _on_start_preview_clicked.
        preferred_dual_panel pre-checks dual_panel_checkbox (Edit's own
        previous choice for this camera); defaults False/unchecked for a
        fresh Add, and is always applied explicitly (never left as-is) since
        this page's one instance is reused across every camera's own visit.
        preferred_camera_controls is the same idea for the camera-control
        widgets (emitter/exposure) - a dict shaped like _read_camera_
        controls' own output (Edit's own previous choice); None falls back
        to DEFAULT_CAMERA_CONTROLS, always applied explicitly for the same
        stale-carryover reason."""
        self.ctx = ctx
        self.device_serial = device_serial
        self._tests = list(tests)
        self._preferred_a = preferred_a
        self._preferred_b = preferred_b
        self._enable_depth_for_ir_sync = enable_depth_for_ir_sync
        # Always set, never left as-is - this page's SAME instance is reused
        # across every camera's own sub-flow visit, so a previous camera's
        # checked state must not silently leak into this one's default.
        self.dual_panel_checkbox.setChecked(bool(preferred_dual_panel))
        self._apply_camera_controls_to_widgets(preferred_camera_controls or DEFAULT_CAMERA_CONTROLS)

        try:
            self._current_rgb_mode = get_mode(find_device_by_serial(ctx, device_serial))
        except Exception:
            # The device can vanish between MainWindow resolving it and this
            # lookup (e.g. unplugged mid-wizard) - fall back to "no mode
            # concept" rather than raise out of populate().
            self._current_rgb_mode = None
        if self._current_rgb_mode is None:
            self.mode_group_box.setVisible(False)
        else:
            self.mode_group_box.setVisible(True)
            self.dual_radio.setChecked(self._current_rgb_mode == "dual")
            self.dedicated_radio.setChecked(self._current_rgb_mode == "dedicated")

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
        self._update_exposure_labels()

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
        # Placeholder text until _update_exposure_labels() fills in the
        # actual stream each spinbox controls, once a test is selected (see
        # populate()/_populate_sensor_options) - stays generic "Exposure A"/
        # "Exposure B" only in the brief window before that first happens.
        exposure_a_label = QLabel("Exposure A:")
        exposure_row.addWidget(exposure_a_label)
        exposure_a_spin = QSpinBox()
        exposure_a_spin.setRange(1, 1000000)
        exposure_a_spin.setValue(8500)
        exposure_a_spin.setEnabled(False)
        exposure_row.addWidget(exposure_a_spin)
        exposure_b_label = QLabel("Exposure B:")
        exposure_row.addWidget(exposure_b_label)
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
            "exposure_a_label": exposure_a_label,
            "exposure_a_spin": exposure_a_spin,
            "exposure_b_label": exposure_b_label,
            "exposure_b_spin": exposure_b_spin,
        }

    def _update_exposure_labels(self):
        """Relabels the two exposure spinboxes with the actual stream each
        one currently controls (e.g. "Exposure (Infrared 1):" - the same
        stream_label() ROI Select's own window titles use), rather than
        generic position-based "Exposure A"/"Exposure B" text. A test's
        stream_a/stream_b identities are fixed by settings.yaml (see this
        module's own docstring) and don't change across sensor_options
        selections within the same test, so this only needs re-running when
        the selected TEST changes - called from _populate_sensor_options,
        which runs on both populate() and combo_test's own currentIndexChanged.
        Falls back to the generic labels if no pick exists yet (e.g. an
        empty tests list)."""
        pick_a, pick_b = self.pick_a, self.pick_b
        w = self._camera_controls
        w["exposure_a_label"].setText(
            "Exposure ({}):".format(stream_label(pick_a)) if pick_a is not None else "Exposure A:"
        )
        w["exposure_b_label"].setText(
            "Exposure ({}):".format(stream_label(pick_b)) if pick_b is not None else "Exposure B:"
        )

    def read_camera_controls(self):
        """Returns the single global camera-control dict - applied
        uniformly to every resolved sensor group (see
        gui.pages.roi_select_page._apply_camera_controls), which then picks
        exposure_a vs exposure_b per group via engine.streams.
        exposure_for_group depending on which stream that group actually
        contains. No "gain" key - manual exposure mode never touches gain
        at all (see engine.streams.set_manual_exposure's docstring for
        why), so there is nothing for this dict to carry for it. Public
        (unlike most of this page's helpers) - part of this page's
        documented produced interface alongside pick_a/pick_b/
        current_test_name: gui/main_window.py's own mode-switch handler
        reads this to carry the operator's current camera-control choices
        through a Test/Sensor Options refresh."""
        w = self._camera_controls
        auto_exposure = w["auto_radio"].isChecked()
        return {
            "emitter_enabled": not w["emitter_checkbox"].isChecked(),
            "auto_exposure": auto_exposure,
            "exposure_a": None if auto_exposure else w["exposure_a_spin"].value(),
            "exposure_b": None if auto_exposure else w["exposure_b_spin"].value(),
        }

    def _apply_camera_controls_to_widgets(self, camera_controls):
        """The inverse of read_camera_controls - lets populate() prefill
        this page's camera-control widgets from a previously-read dict
        (Edit's own previous choice for this camera), rather than always
        starting from the hardcoded widget defaults DEFAULT_CAMERA_CONTROLS
        below."""
        w = self._camera_controls
        w["emitter_checkbox"].setChecked(not camera_controls["emitter_enabled"])
        if camera_controls["auto_exposure"]:
            w["auto_radio"].setChecked(True)
        else:
            w["manual_radio"].setChecked(True)
            if camera_controls["exposure_a"] is not None:
                w["exposure_a_spin"].setValue(camera_controls["exposure_a"])
            if camera_controls["exposure_b"] is not None:
                w["exposure_b_spin"].setValue(camera_controls["exposure_b"])

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

    def _on_back_clicked(self):
        # Same silent auto-stop precedent _on_next_clicked already uses - a
        # pairing-quality preview has no work to lose, so Back doesn't need
        # to ask before stopping it, only before leaving with it still open.
        self._stop_preview()
        self.back_requested.emit()

    def note_mode_switch_applied(self, new_mode):
        """Called by gui/main_window.py's _on_stream_config_mode_switch_
        requested after a real ensure_mode() call has already succeeded,
        even on the branch where the subsequent Test/Sensor Options
        re-populate then fails (e.g. no configured test matches this
        camera's new mode, so populate() - and therefore the fresh
        get_mode() lookup that would normally refresh _current_rgb_mode -
        never runs). Without this, _current_rgb_mode would stay stale at
        the PRE-switch value, making the next Next click think another
        switch is still needed and re-trigger ensure_mode() for a switch
        that already succeeded - an unrecoverable retry loop, since
        nothing about the device's real mode would ever change again."""
        self._current_rgb_mode = new_mode
        self.dual_radio.setChecked(new_mode == "dual")
        self.dedicated_radio.setChecked(new_mode == "dedicated")

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
        if self._current_rgb_mode is not None:
            target_mode = "dual" if self.dual_radio.isChecked() else "dedicated"
            if target_mode != self._current_rgb_mode:
                # Don't proceed - the switch changes which streams the
                # device exposes, so whatever Test/Sensor Options are
                # currently selected may no longer even be valid.
                # MainWindow.py's own handler applies the switch and calls
                # populate() again with the refreshed device capabilities;
                # the operator reviews the (possibly changed) result and
                # clicks Next again to actually proceed.
                self.mode_switch_requested.emit(target_mode)
                return
        self._stop_preview()
        camera_controls = self.read_camera_controls()
        self.config_chosen.emit((pick_a, pick_b, camera_controls))
