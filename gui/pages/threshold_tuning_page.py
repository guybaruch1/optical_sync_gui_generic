"""Wizard step 5 (of 6) - inserted between Calibration and Live Session:
shows a LIVE video preview of both streams, each with the same green/red
on-off detection-circle overlay Live Session draws
(domain.realsense_utils.draw_led_state_overlay), so the operator can tune
each stream's OWN threshold fraction while watching which LEDs the app
currently classifies as "on" - before committing to an actual timed test
run. LED Switch Time is also live-editable here (it directly affects how
bright an LED gets before switching off, which is exactly what the
threshold fraction has to compensate for), separately from Live Session's
own switch-time control.

Unlike SessionEngineThread (which drives a TestSession/PairingGapMetric/
PositionGapMetric/CSV-recording run), engine.threshold_preview_thread's
ThresholdPreviewThread only streams video + per-LED brightness - the on/off
mask is computed HERE, in _on_frame_ready, from whatever the relevant
threshold-fraction spinbox currently reads, so a threshold change is
reflected on the very next incoming frame with no thread restart needed.

Preview only runs between Start/Stop (not auto-started on arrival) - a
"Frame Sample Interval" spinbox (same idea as Live Session's own, read
fresh at Start) throttles how often the preview actually updates, in case
full frame rate is more than the operator wants to watch. Stop is
deliberately non-blocking (mirrors LiveSessionPage.stop_session -
re-enabling happens off the thread's own `finished` signal, not
immediately); "Continue to Live Test" is NOT - it blocks until the preview
thread has genuinely finished (request_stop() + wait()) before handing off,
so Live Session's own capture/LED-panel setup can never race this page's
still-in-progress hardware cleanup.

"Continue to Live Test" hands the tuned per-stream threshold arrays
(domain.calibration.compute_threshold, applied to each stream's own
calibrated on/off values) to MainWindow via the stream_a_threshold/
stream_b_threshold properties - see main_window.py's _on_tuning_done.
stream_a_xy/stream_b_xy properties read live from self._context too (not a
copy stashed in MainWindow) - required once LED Detection Threshold Tuning
(below) can reassign these to a different-length array; MainWindow used to
read a copy frozen before this page ever ran, which happened to work only
because nothing previously ever changed these arrays after set_context().

LED Detection Threshold Tuning (above each stream's existing Threshold
Fraction control, a different axis entirely) lets the operator manually
override Calibration's automatic Otsu-based LED-position detection, per
stream, if it went badly (wrong count, missed/merged blobs) - reusing
Calibration's own already-captured on/off frames (domain.calibration.
build_grid_positions), no new camera capture needed here. A small preview
+ live "Detected: N / num_leds" count updates on every slider tick
(cheap: detect_led_centroids + merge_close_centroids + draw_detected_centroids,
no grid sort, no per-LED brightness sampling); the full pipeline (grid-order
assignment + on/off/threshold sampling, replacing self._context's
stream_a_xy/stream_a_on/stream_a_off) is debounced ~150ms after the last
drag, since build_positions_with_thresholds' per-LED brightness sampling
does its own full-frame grayscale conversion per call and would lag on a
color stream if run on every single tick. "Continue to Live Test" persists
whatever the current positions are (retuned or original, a safe no-op
rewrite if untouched) to config.yaml via update_config_leds too, not just
to Live Session in-memory - same as Calibration's own original write."""

import numpy as np

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QSpinBox, QDoubleSpinBox, QSlider, QLabel, QFrame,
    QScrollArea, QApplication, QMessageBox,
)
from PySide6.QtCore import Signal, Qt, QTimer

from gui.widgets.video_panel import VideoPanel
from gui.pages.live_session_page import _short_camera_name
from engine.threshold_preview_thread import ThresholdPreviewThread
from engine.led_panel import LEDPanel
from engine.dual_panel_control import start_scanning
from engine.streams import stream_slug
from domain.calibration import compute_threshold, build_grid_positions, update_config_leds
from domain.realsense_utils import (
    draw_led_state_overlay, crop_to_roi, detect_led_centroids, merge_close_centroids, draw_detected_centroids,
)


class ThresholdTuningPage(QWidget):
    tuning_done = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._context = None
        self.preview_thread = None
        # The switch-time value actually applied to hardware, as of the
        # last successful confirm (or the last set_context() prefill) - see
        # _on_switch_time_spinbox_changed/_on_confirm_switch_time_clicked.
        # None until set_context() runs once.
        self._last_applied_switch_time_ms = None

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)
        content_widget = QWidget()
        content_widget.setStyleSheet("QWidget { background-color: #f2f0ea; }")
        layout = QVBoxLayout(content_widget)
        scroll_area.setWidget(content_widget)
        outer_layout.addWidget(scroll_area)

        video_row = QHBoxLayout()
        self.stream_a_panel = VideoPanel(force_square=True)
        self.stream_b_panel = VideoPanel(force_square=True)
        for panel in (self.stream_a_panel, self.stream_b_panel):
            panel.setStyleSheet("background-color: #3a3a3a; border-radius: 4px;")
        # Placeholder text until set_context() fills in the actual camera
        # model name + stream label - stream identity isn't known until then.
        self.stream_a_title_label = QLabel("Stream A")
        self.stream_b_title_label = QLabel("Stream B")
        for title_label in (self.stream_a_title_label, self.stream_b_title_label):
            title_label.setStyleSheet(
                "color: #555555; font-weight: 600; font-size: 9pt;"
                "text-transform: uppercase; letter-spacing: 1px; border: none; background: transparent;"
            )

        self.stream_a_threshold_fraction_spinbox = QDoubleSpinBox()
        self.stream_b_threshold_fraction_spinbox = QDoubleSpinBox()
        for spinbox in (self.stream_a_threshold_fraction_spinbox, self.stream_b_threshold_fraction_spinbox):
            spinbox.setRange(0.0, 1.0)
            spinbox.setSingleStep(0.01)
            spinbox.setDecimals(2)
            spinbox.setValue(0.25)
            spinbox.setToolTip(
                "Fraction between this stream's calibrated off/on brightness used as the live "
                "on/off cutoff: threshold = off + fraction*(on-off). Drag while watching the "
                "preview above - each change is reflected on the very next frame."
            )

        # Per-stream debounce timers for LED Detection Threshold Tuning's
        # commit step (see _on_detection_threshold_changed/
        # _commit_detection_threshold) - one per stream since retuning one
        # stream must never restart the other's pending commit.
        self._stream_a_detection_commit_timer = QTimer(self)
        self._stream_a_detection_commit_timer.setSingleShot(True)
        self._stream_a_detection_commit_timer.timeout.connect(lambda: self._commit_detection_threshold("stream_a"))
        self._stream_b_detection_commit_timer = QTimer(self)
        self._stream_b_detection_commit_timer.setSingleShot(True)
        self._stream_b_detection_commit_timer.timeout.connect(lambda: self._commit_detection_threshold("stream_b"))
        self._stream_a_pending_centroids = []
        self._stream_b_pending_centroids = []
        # Tracked so a change to one stream's detected count can also
        # refresh the OTHER stream's mismatch styling (Live Session's math
        # needs both streams' LED counts to agree, not just each vs
        # num_leds) - None until the first detection ever runs.
        self._stream_a_last_detected_count = None
        self._stream_b_last_detected_count = None

        stream_a_column = QVBoxLayout()
        stream_a_column.addWidget(self.stream_a_title_label)
        stream_a_column.addWidget(self.stream_a_panel)
        stream_a_column.addLayout(self._build_detection_tuning_row("stream_a"))
        stream_a_fraction_row = QHBoxLayout()
        stream_a_fraction_row.addWidget(QLabel("Threshold Fraction:"))
        stream_a_fraction_row.addWidget(self.stream_a_threshold_fraction_spinbox)
        stream_a_column.addLayout(stream_a_fraction_row)

        stream_b_column = QVBoxLayout()
        stream_b_column.addWidget(self.stream_b_title_label)
        stream_b_column.addWidget(self.stream_b_panel)
        stream_b_column.addLayout(self._build_detection_tuning_row("stream_b"))
        stream_b_fraction_row = QHBoxLayout()
        stream_b_fraction_row.addWidget(QLabel("Threshold Fraction:"))
        stream_b_fraction_row.addWidget(self.stream_b_threshold_fraction_spinbox)
        stream_b_column.addLayout(stream_b_fraction_row)

        video_row.addLayout(stream_a_column)
        video_row.addLayout(stream_b_column)
        video_row.addStretch(1)
        layout.addLayout(video_row)

        toolbar_frame = QFrame()
        toolbar_frame.setStyleSheet(
            "QFrame { background-color: #e9e7e1; border-top: 1px solid #d8d5cd; }"
        )
        control_row = QHBoxLayout(toolbar_frame)
        control_row.setContentsMargins(10, 8, 10, 8)
        self.start_button = QPushButton("Start")
        self.start_button.setStyleSheet(
            "QPushButton { background-color: #2f6fed; color: white; border: 1px solid #2f6fed;"
            " border-radius: 4px; padding: 5px 14px; }"
            "QPushButton:disabled { background-color: #b7c7f0; color: #eef2fc; border: 1px solid #b7c7f0; }"
        )
        self.start_button.clicked.connect(self._on_start_clicked)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self._on_stop_clicked)
        self.stop_button.setEnabled(False)
        control_row.addWidget(self.start_button)
        control_row.addWidget(self.stop_button)

        control_row.addWidget(QLabel("LED Switch Time (ms):"))
        self.switch_time_spinbox = QDoubleSpinBox()
        # Same floor/precision reasoning as gui/pages/live_session_page.py's
        # own switch_time_spinbox: 0.1 is the finest step
        # LEDPanel.set_speed_ms's "--setTime {:.4f}" (4 decimal places of
        # SECONDS) can actually represent (engine/led_panel.py).
        self.switch_time_spinbox.setRange(0.1, 10000.0)
        self.switch_time_spinbox.setDecimals(1)
        self.switch_time_spinbox.setSingleStep(0.5)
        self.switch_time_spinbox.setValue(1.0)
        # Deliberately NOT wired to apply on every tick - each apply is a
        # real, multi-second hardware call (worse in dual-panel mode, a
        # full per-panel hub-switch+reconfigure), so clicking the spin
        # arrows from e.g. 1 to 5 used to fire it 4 times (once per
        # intermediate value) instead of once for the value actually
        # wanted. Worse than just being slow: the handler calls
        # QApplication.processEvents() mid-body so its own status-label
        # update repaints before the blocking call - which also let a
        # SECOND queued tick re-enter the handler while the first was
        # still mid-flight, two overlapping attempts to open the same
        # relay COM port, observed on real hardware as "Failed to update
        # LED switch time: WriteFile failed (PermissionError(13, 'Access
        # is denied.', ...))". This handler only compares against the
        # last-APPLIED value and toggles the Confirm button - no hardware
        # call - so it's safe to fire on every tick.
        self.switch_time_spinbox.valueChanged.connect(self._on_switch_time_spinbox_changed)
        control_row.addWidget(self.switch_time_spinbox)
        self.confirm_switch_time_button = QPushButton("Confirm")
        self.confirm_switch_time_button.setEnabled(False)  # nothing to confirm until the value actually changes
        self.confirm_switch_time_button.setToolTip(
            "Apply the LED Switch Time above to the panel(s) - collects however many spin-box "
            "ticks happened since the last confirm into one hardware call."
        )
        self.confirm_switch_time_button.clicked.connect(self._on_confirm_switch_time_clicked)
        control_row.addWidget(self.confirm_switch_time_button)

        control_row.addWidget(QLabel("Frame Sample Interval:"))
        self.frame_sample_interval_spinbox = QSpinBox()
        self.frame_sample_interval_spinbox.setRange(1, 2000)
        # Matches Live Session's own frame_sample_interval default - how
        # many frame-pairs between preview updates. Read fresh at Start
        # (baked into the thread's constructor), so - like Live Session's
        # own toolbar control - changing it mid-run wouldn't take effect
        # until the next Start, hence locked while a preview is running.
        self.frame_sample_interval_spinbox.setValue(10)
        self.frame_sample_interval_spinbox.setToolTip(
            "Frame-pairs between preview updates - lower to watch every frame, raise to slow "
            "the preview down. Takes effect on the next Start."
        )
        control_row.addWidget(self.frame_sample_interval_spinbox)

        control_row.addStretch(1)
        self.continue_button = QPushButton("Continue to Live Test")
        self.continue_button.setStyleSheet(
            "QPushButton { background-color: #2f6fed; color: white; border: 1px solid #2f6fed;"
            " border-radius: 4px; padding: 5px 14px; }"
        )
        self.continue_button.clicked.connect(self._on_continue_clicked)
        control_row.addWidget(self.continue_button)
        layout.addWidget(toolbar_frame)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

    def _build_detection_tuning_row(self, stream_name):
        # One per stream (stream_name: "stream_a"/"stream_b") - manual
        # override of Calibration's automatic Otsu-based LED-position
        # detection, in case it went badly. Widget attribute names all
        # follow "{stream_name}_..." so the per-tick/debounced-commit
        # handlers below can address either stream's widgets generically via
        # getattr() instead of duplicating this method's logic per stream.
        column = QVBoxLayout()
        title_label = QLabel("LED Detection Threshold Tuning")
        title_label.setStyleSheet("color: #555555; font-weight: 600; font-size: 8pt; border: none; background: transparent;")
        column.addWidget(title_label)

        detection_panel = VideoPanel(force_square=True)
        detection_panel.setMaximumSize(160, 160)
        detection_panel.setStyleSheet("background-color: #3a3a3a; border-radius: 4px;")
        setattr(self, "{}_detection_panel".format(stream_name), detection_panel)
        column.addWidget(detection_panel)

        detected_count_label = QLabel("Detected: - / -")
        setattr(self, "{}_detected_count_label".format(stream_name), detected_count_label)
        column.addWidget(detected_count_label)

        slider = QSlider(Qt.Horizontal)
        slider.setRange(0, 255)
        spinbox = QSpinBox()
        spinbox.setRange(0, 255)
        spinbox.setToolTip(
            "Manual pixel-brightness cutoff for detecting an LED blob (0-255) - overrides "
            "Calibration's automatic Otsu threshold if it picked badly (wrong count, "
            "missed/merged blobs). Drag while watching the preview above."
        )
        setattr(self, "{}_detection_slider".format(stream_name), slider)
        setattr(self, "{}_detection_spinbox".format(stream_name), spinbox)
        self._link_slider_and_spinbox(
            slider, spinbox, lambda value: self._on_detection_threshold_changed(stream_name, value)
        )
        slider_row = QHBoxLayout()
        slider_row.addWidget(QLabel("Detection Threshold:"))
        slider_row.addWidget(slider)
        slider_row.addWidget(spinbox)
        column.addLayout(slider_row)

        reset_button = QPushButton("Reset to Auto")
        reset_button.clicked.connect(lambda: self._reset_stream_to_auto(stream_name))
        setattr(self, "{}_reset_to_auto_button".format(stream_name), reset_button)
        column.addWidget(reset_button)

        return column

    def _link_slider_and_spinbox(self, slider, spinbox, on_change):
        # Keeps the two mirrored (drag OR type a precise value) without a
        # feedback loop - each side's own setValue() call to sync the other
        # is signal-blocked, so on_change() fires exactly once per genuine
        # user-driven change, from whichever widget the user actually used.
        def _slider_changed(value):
            spinbox.blockSignals(True)
            spinbox.setValue(value)
            spinbox.blockSignals(False)
            on_change(value)

        def _spinbox_changed(value):
            slider.blockSignals(True)
            slider.setValue(value)
            slider.blockSignals(False)
            on_change(value)

        slider.valueChanged.connect(_slider_changed)
        spinbox.valueChanged.connect(_spinbox_changed)

    def _on_detection_threshold_changed(self, stream_name, value):
        # Tier 1 - every tick, cheap: redetect + redraw + recount only. No
        # grid sort, no per-LED brightness sampling, no self._context
        # mutation - see the module docstring's "two-tier recompute"
        # rationale. The actual context update is debounced (below).
        ctx = self._context
        if ctx is None:
            return
        cropped = ctx["{}_cropped_on".format(stream_name)]
        centroids, _ = detect_led_centroids(cropped, value, ctx["min_blob_area"])
        centroids = merge_close_centroids(centroids)
        setattr(self, "_{}_pending_centroids".format(stream_name), centroids)

        preview = draw_detected_centroids(cropped, centroids)
        getattr(self, "{}_detection_panel".format(stream_name)).set_frame(preview)
        self._update_detected_count_label(stream_name, len(centroids))

        getattr(self, "_{}_detection_commit_timer".format(stream_name)).start(150)

    def _update_detected_count_label(self, stream_name, count):
        ctx = self._context
        num_leds = ctx["num_leds"] if ctx is not None else None
        other_stream = "stream_b" if stream_name == "stream_a" else "stream_a"

        setattr(self, "_{}_last_detected_count".format(stream_name), count)
        other_count = getattr(self, "_{}_last_detected_count".format(other_stream))

        for name, this_count, compare_count in (
            (stream_name, count, other_count), (other_stream, other_count, count),
        ):
            if this_count is None:
                continue
            label = getattr(self, "{}_detected_count_label".format(name))
            label.setText("Detected: {} / {}".format(this_count, num_leds))
            mismatched = (num_leds is not None and this_count != num_leds) or (
                compare_count is not None and this_count != compare_count
            )
            label.setStyleSheet("color: #b00020; font-weight: 600;" if mismatched else "color: #555555;")

    def _commit_detection_threshold(self, stream_name):
        # Tier 2 - debounced ~150ms after the last slider/spinbox tick:
        # the actual grid-order assignment + per-LED on/off/threshold
        # sampling, replacing self._context's stream_a_xy/on/off (and
        # stream_a_positions, needed by _on_continue_clicked's
        # update_config_leds call) so the existing moving-LED preview
        # (which reads self._context live in _on_frame_ready) reactively
        # reflects the retune on its very next frame.
        ctx = self._context
        if ctx is None:
            return
        centroids = getattr(self, "_{}_pending_centroids".format(stream_name))
        if not centroids:
            return  # nothing detected - leave the last-good context untouched
        roi = ctx["{}_roi".format(stream_name)]
        image_on = ctx["{}_image_on".format(stream_name)]
        image_off = ctx["{}_image_off".format(stream_name)]
        try:
            positions, _row_layout, _debug_centroids = build_grid_positions(
                centroids, roi, image_on, image_off, ctx["row_gap_px"], ctx["calibration_neighborhood_size"],
            )
        except RuntimeError:
            return  # 0 centroids after all (shouldn't happen given the guard above) - leave context untouched

        ctx["{}_positions".format(stream_name)] = positions
        ids = list(positions.keys())
        ctx["{}_xy".format(stream_name)] = np.array([positions[i][:2] for i in ids])
        ctx["{}_on".format(stream_name)] = np.array([positions[i][2] for i in ids])
        ctx["{}_off".format(stream_name)] = np.array([positions[i][3] for i in ids])

    def _reset_stream_to_auto(self, stream_name):
        # Reproduces Calibration's original Otsu result byte-for-byte -
        # Otsu and the manual path both apply the same cv2.THRESH_BINARY
        # logic at whatever value is chosen, so setting the slider back to
        # Otsu's own computed value is equivalent to re-running Otsu. No
        # separate code path needed - setValue() naturally re-triggers the
        # same Tier 1/Tier 2 pipeline above.
        if self._context is None:
            return
        slider = getattr(self, "{}_detection_slider".format(stream_name))
        slider.setValue(self._context["{}_otsu_threshold".format(stream_name)])

    def set_context(self, ctx, device_serial, pick_a, pick_b, camera_controls,
                     stream_a_xy, stream_b_xy, stream_a_on, stream_a_off, stream_b_on, stream_b_off,
                     num_leds, neighborhood_size, scan_direction, switch_time_ms,
                     stream_a_threshold_fraction_default, stream_b_threshold_fraction_default,
                     stream_a_roi, stream_b_roi, camera_name, stream_a_label, stream_b_label,
                     config_path, image_a_on, image_a_off, image_b_on, image_b_off,
                     stream_a_otsu_threshold, stream_b_otsu_threshold,
                     min_blob_area, row_gap_px, calibration_neighborhood_size,
                     stream_a_positions, stream_b_positions,
                     dual_panel_config=None, enable_depth_for_ir_sync=True):
        self._context = dict(
            ctx=ctx, device_serial=device_serial, pick_a=pick_a, pick_b=pick_b, camera_controls=camera_controls,
            stream_a_xy=stream_a_xy, stream_b_xy=stream_b_xy,
            stream_a_on=stream_a_on, stream_a_off=stream_a_off,
            stream_b_on=stream_b_on, stream_b_off=stream_b_off,
            num_leds=num_leds, neighborhood_size=neighborhood_size, scan_direction=scan_direction,
            stream_a_roi=stream_a_roi, stream_b_roi=stream_b_roi, dual_panel_config=dual_panel_config,
            enable_depth_for_ir_sync=enable_depth_for_ir_sync,
            # LED Detection Threshold Tuning's own state - config_path/
            # stream_a_positions/stream_b_positions are what
            # _on_continue_clicked persists via update_config_leds;
            # stream_a_image_on/off are Calibration's own already-captured
            # frames (deliberately NOT reusing the "image_a_on" kwarg name
            # internally - every other per-stream context key here is
            # prefixed "stream_a_"/"stream_b_", so this matches that
            # convention for the generic getattr()-based access the
            # detection-tuning handlers above use).
            config_path=config_path, camera_name=camera_name,
            stream_a_image_on=image_a_on, stream_a_image_off=image_a_off,
            stream_b_image_on=image_b_on, stream_b_image_off=image_b_off,
            stream_a_otsu_threshold=stream_a_otsu_threshold, stream_b_otsu_threshold=stream_b_otsu_threshold,
            min_blob_area=min_blob_area, row_gap_px=row_gap_px,
            calibration_neighborhood_size=calibration_neighborhood_size,
            stream_a_positions=stream_a_positions, stream_b_positions=stream_b_positions,
            # Cropped ONCE here, not per detection-threshold tick -
            # detect_led_centroids/draw_detected_centroids both operate on
            # this same cropped "on" frame throughout the page's lifetime.
            stream_a_cropped_on=crop_to_roi(image_a_on, stream_a_roi),
            stream_b_cropped_on=crop_to_roi(image_b_on, stream_b_roi),
        )
        self.stream_a_threshold_fraction_spinbox.setValue(stream_a_threshold_fraction_default)
        self.stream_b_threshold_fraction_spinbox.setValue(stream_b_threshold_fraction_default)
        # float(), not int(round(...)) - settings.yaml's switch_time_ms can
        # already be fractional, and truncating it here would silently
        # throw that precision away before the operator even sees it.
        #
        # Set BEFORE setValue() below, not after - this IS the prefilled
        # value (settings.yaml's own default, nothing to confirm yet), and
        # setValue() won't even fire valueChanged if the spinbox already
        # happens to hold this same value (e.g. revisiting this page with
        # an unchanged camera) - the explicit refresh call right after
        # covers exactly that case, same reasoning as the detection-slider
        # comment a few lines below.
        self._last_applied_switch_time_ms = float(switch_time_ms)
        self.switch_time_spinbox.setValue(float(switch_time_ms))
        self._update_confirm_switch_time_button_state()
        short_name = _short_camera_name(camera_name)
        self.stream_a_title_label.setText("{} - {}".format(short_name, stream_a_label))
        self.stream_b_title_label.setText("{} - {}".format(short_name, stream_b_label))
        self.status_label.setText("")

        self._stream_a_last_detected_count = None
        self._stream_b_last_detected_count = None
        # setValue() to the SAME value a widget already holds (e.g.
        # revisiting this page with an unchanged camera) won't fire
        # valueChanged, so the detection preview/count could otherwise show
        # stale data from a previous visit - explicitly (re)run the
        # recompute for both streams regardless of whether the slider value
        # actually changes.
        self.stream_a_detection_slider.setValue(stream_a_otsu_threshold)
        self.stream_b_detection_slider.setValue(stream_b_otsu_threshold)
        self._on_detection_threshold_changed("stream_a", self.stream_a_detection_slider.value())
        self._on_detection_threshold_changed("stream_b", self.stream_b_detection_slider.value())

        # Defensive - a stale preview from a previous context (if
        # set_context is ever called again) must not keep running against
        # the new one's stream_a_xy/on/off arrays.
        self._stop_preview_blocking()

    def _on_start_clicked(self):
        ctx = self._context
        self.preview_thread = ThresholdPreviewThread(
            ctx["ctx"], ctx["device_serial"], ctx["pick_a"], ctx["pick_b"], ctx["camera_controls"],
            stream_a_xy=ctx["stream_a_xy"], stream_b_xy=ctx["stream_b_xy"],
            neighborhood_size=ctx["neighborhood_size"], scan_direction=ctx["scan_direction"],
            switch_time_ms=self.switch_time_spinbox.value(),
            display_stride=self.frame_sample_interval_spinbox.value(),
            dual_panel_config=ctx["dual_panel_config"],
            enable_depth_for_ir_sync=ctx["enable_depth_for_ir_sync"],
        )
        self.preview_thread.frame_ready.connect(self._on_frame_ready)
        self.preview_thread.error.connect(self._on_error)
        # Gate re-enabling Start on the thread's own `finished` signal, not
        # on the Stop button click itself - same reason
        # LiveSessionPage.stop_session/_on_engine_thread_finished do this:
        # `finished` only fires once run() (and its hardware-cleanup
        # `finally` block) has actually completed, so a new Start can never
        # race the old thread's still-in-progress camera/LED-panel teardown.
        self.preview_thread.finished.connect(self._on_preview_thread_finished)
        self.preview_thread.start()

        self.status_label.setText("")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.frame_sample_interval_spinbox.setEnabled(False)
        # See _update_confirm_switch_time_button_state's own comment - for
        # dual-panel this disables Confirm through _on_preview_thread_finished
        # below (the SAME running+stopping window Start stays disabled for);
        # for single-panel it's a no-op here (stays exactly as it already
        # was), since live switch-time changes while watching remain safe
        # there.
        self._update_confirm_switch_time_button_state()
        self._set_detection_controls_enabled(False)

    def _on_stop_clicked(self):
        if self.preview_thread is not None:
            self.preview_thread.request_stop()

    def _on_preview_thread_finished(self):
        self.preview_thread = None
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.frame_sample_interval_spinbox.setEnabled(True)
        # Not a blind setEnabled(True) - Confirm's availability still
        # reflects whether the spinbox actually holds an unconfirmed value,
        # same as right after set_context() or a successful apply.
        self._update_confirm_switch_time_button_state()
        self._set_detection_controls_enabled(True)

    def _stop_preview_blocking(self):
        # Unlike _on_stop_clicked (non-blocking - the Stop button just asks
        # nicely and lets `finished` re-enable things whenever cleanup
        # actually completes), callers that need hardware to be genuinely
        # free before proceeding (set_context's defensive stop,
        # _on_continue_clicked below) must actually wait for it.
        if self.preview_thread is not None:
            self.preview_thread.request_stop()
            self.preview_thread.wait()
            self.preview_thread = None
            self.start_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            self.frame_sample_interval_spinbox.setEnabled(True)
            self._set_detection_controls_enabled(True)

    def _set_detection_controls_enabled(self, enabled):
        # ThresholdPreviewThread bakes stream_a_xy/stream_b_xy in at
        # construction time (_on_start_clicked) - retuning detection while a
        # preview is running would change self._context's array length out
        # from under the thread's already-fixed brightness-array shape,
        # silently zip()-truncating _on_frame_ready's overlay to a wrong,
        # partial result. Locked for the same reason
        # frame_sample_interval_spinbox already is during a run.
        for stream_name in ("stream_a", "stream_b"):
            getattr(self, "{}_detection_slider".format(stream_name)).setEnabled(enabled)
            getattr(self, "{}_detection_spinbox".format(stream_name)).setEnabled(enabled)
            getattr(self, "{}_reset_to_auto_button".format(stream_name)).setEnabled(enabled)

    def _on_error(self, message):
        self.status_label.setText(message)

    def _on_switch_time_spinbox_changed(self, value):
        # Pure UI state - no hardware call. Enables Confirm the moment the
        # spinbox no longer matches what's actually applied, disables it
        # once it does (e.g. the operator ticks it back to the current
        # value, or right after a successful confirm sets
        # _last_applied_switch_time_ms to match). Safe to fire on every
        # single spin-arrow tick.
        self._update_confirm_switch_time_button_state()

    def _update_confirm_switch_time_button_state(self):
        unconfirmed = self.switch_time_spinbox.value() != self._last_applied_switch_time_ms
        # Only actually unsafe for DUAL-panel: the preview thread's own
        # start_scanning()/stop_scanning() calls (thread start / thread-stop
        # hardware cleanup) touch the SAME shared relay connection a
        # Confirm click would also touch, mid-scan - confirmed on real
        # hardware to still produce "WriteFile failed (PermissionError...)"
        # even with engine/dual_panel_control.py's _dual_panel_lock in
        # place (that lock only serializes the two calls against each
        # other, it doesn't make reconfiguring an ACTIVELY-STEPPING panel
        # mid-scan itself safe). Single-panel's LEDPanel.set_speed_ms() is
        # a stateless, independent subprocess call with no persistent
        # handle to race against the preview thread's own capture loop
        # (camera-only once started, no ongoing LED-panel touches) - live
        # switch-time changes while watching stay safe there, matching
        # this control's original design intent, so this is a no-op for
        # single-panel regardless of preview_thread.
        #
        # preview_thread is not None for the ENTIRE dual-panel running/
        # stopping window (set in _on_start_clicked, only cleared in
        # _on_preview_thread_finished once the thread's own hardware
        # cleanup has actually completed) - this must stay disabled for
        # that whole window regardless of what the spinbox holds, since
        # _on_switch_time_spinbox_changed calls this on every tick and the
        # spinbox stays editable (just not appliable) while a preview is
        # active.
        dual_panel_config = self._context["dual_panel_config"] if self._context is not None else None
        unsafe_to_apply_now = self.preview_thread is not None and dual_panel_config is not None
        self.confirm_switch_time_button.setEnabled(unconfirmed and not unsafe_to_apply_now)

    def _on_confirm_switch_time_clicked(self):
        # No thread restart needed - LEDPanel is a stateless static-method
        # CLI wrapper (engine/led_panel.py), safe to call from the GUI
        # thread while the preview thread's own capture loop keeps running,
        # since it only talks to the LED panel hardware, never the camera.
        #
        # Disabling the spinbox/Confirm/Start for the duration of this call
        # is what actually prevents the reentrancy that used to cause
        # "WriteFile failed (PermissionError...)" - this whole method runs
        # synchronously on the GUI thread, so a disabled Confirm button
        # structurally cannot be clicked again until this returns (Qt never
        # delivers clicks to a disabled widget), even though
        # QApplication.processEvents() below still pumps the event loop for
        # the status-label repaint.
        value = self.switch_time_spinbox.value()
        # Restore Start to whatever it already was afterward, not
        # unconditionally True - if a preview is currently running, Start
        # is already disabled by the normal Start/Stop state machine
        # (_on_start_clicked/_on_preview_thread_finished) and must STAY
        # disabled once this returns, not get force-re-enabled underneath it.
        was_start_enabled = self.start_button.isEnabled()
        self.switch_time_spinbox.setEnabled(False)
        self.confirm_switch_time_button.setEnabled(False)
        self.start_button.setEnabled(False)
        try:
            dual_panel_config = self._context["dual_panel_config"] if self._context is not None else None
            if dual_panel_config is not None:
                # Unlike the single-panel case, a simple set_speed_ms() call
                # only ever reaches whichever of the 2 panels is currently
                # Acroname-hub-exposed - any config change needs the WHOLE
                # per-panel provisioning dance (+ re-pulsing the relay)
                # redone, per the operator's own confirmation. Visibly
                # slower than the single-panel case's instant update - no
                # way around the hardware constraint.
                self.status_label.setText("Reconfiguring both LED panels...")
                QApplication.processEvents()
                start_scanning(value, self._context["scan_direction"], dual_panel_config)
                self.status_label.setText("")
            else:
                LEDPanel.set_speed_ms(value)
            # Only recorded on SUCCESS - a failed apply leaves this stale,
            # so Confirm stays enabled below for an easy retry with no need
            # to nudge the spinbox first.
            self._last_applied_switch_time_ms = value
        except Exception as exc:
            self.status_label.setText("Failed to update LED switch time: {}".format(exc))
        finally:
            self.switch_time_spinbox.setEnabled(True)
            self.start_button.setEnabled(was_start_enabled)
            self._update_confirm_switch_time_button_state()

    def _on_frame_ready(self, stream_name, image, frame_index, brightness):
        if self._context is None:
            return
        if stream_name == "stream_a":
            threshold = compute_threshold(
                self._context["stream_a_on"], self._context["stream_a_off"],
                self.stream_a_threshold_fraction_spinbox.value(),
            )
            xy = self._context["stream_a_xy"]
        else:
            threshold = compute_threshold(
                self._context["stream_b_on"], self._context["stream_b_off"],
                self.stream_b_threshold_fraction_spinbox.value(),
            )
            xy = self._context["stream_b_xy"]
        mask = brightness > threshold
        display_image = draw_led_state_overlay(image, xy, mask)
        # Cropped AFTER the overlay is drawn, not before - same reason as
        # LiveSessionPage._on_frame_ready: overlay circles are positioned in
        # full-frame coordinates.
        roi = self._context["stream_a_roi"] if stream_name == "stream_a" else self._context["stream_b_roi"]
        if roi is not None and roi[2] > 0 and roi[3] > 0:
            display_image = crop_to_roi(display_image, roi)
        if stream_name == "stream_a":
            self.stream_a_panel.set_frame(display_image)
        else:
            self.stream_b_panel.set_frame(display_image)

    @property
    def stream_a_threshold(self):
        return compute_threshold(
            self._context["stream_a_on"], self._context["stream_a_off"],
            self.stream_a_threshold_fraction_spinbox.value(),
        )

    @property
    def stream_b_threshold(self):
        return compute_threshold(
            self._context["stream_b_on"], self._context["stream_b_off"],
            self.stream_b_threshold_fraction_spinbox.value(),
        )

    @property
    def stream_a_xy(self):
        # Read live from self._context, NOT a copy stashed elsewhere -
        # LED Detection Threshold Tuning can REASSIGN this key (a different
        # LED count needs a new array, not an in-place mutation) once the
        # operator retunes detection, and MainWindow._on_tuning_done must
        # see that reassignment, not a value frozen before Threshold Tuning
        # ever ran. Mirrors stream_a_threshold/stream_b_threshold's own
        # already-live-read convention.
        return self._context["stream_a_xy"]

    @property
    def stream_b_xy(self):
        return self._context["stream_b_xy"]

    @property
    def switch_time_ms(self):
        return self.switch_time_spinbox.value()

    def _on_continue_clicked(self):
        ctx = self._context
        slug_a, slug_b = stream_slug(ctx["pick_a"]), stream_slug(ctx["pick_b"])
        res_a = (ctx["pick_a"]["width"], ctx["pick_a"]["height"])
        res_b = (ctx["pick_b"]["width"], ctx["pick_b"]["height"])
        stream_a_ids = list(ctx["stream_a_positions"].keys())
        stream_b_ids = list(ctx["stream_b_positions"].keys())
        if len(stream_a_ids) != len(stream_b_ids) or len(stream_a_ids) != ctx["num_leds"]:
            QMessageBox.warning(
                self,
                "LED count mismatch",
                "Detection tuning found {} LED(s) for one stream and {} for the other, but "
                "settings.yaml's test.num_leds is {}. The live session's position-gap math "
                "assumes all three match - proceeding anyway, but treat position-gap results "
                "with caution until this is resolved (retune detection, or fix "
                "test.num_leds).".format(len(stream_a_ids), len(stream_b_ids), ctx["num_leds"]),
            )
        # Persists whatever the CURRENT positions are - the original
        # Calibration-computed ones if the operator never touched the
        # detection-threshold sliders (a safe no-op rewrite), or the
        # retuned ones if they did. Same helper CalibrationPage's own
        # _run_calibration already uses to write its first-ever result.
        update_config_leds(
            ctx["config_path"], ctx["camera_name"],
            slug_a, ctx["stream_a_positions"], res_a,
            slug_b, ctx["stream_b_positions"], res_b,
        )
        self._stop_preview_blocking()
        self.tuning_done.emit()
