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
stream_b_threshold properties - see main_window.py's _on_tuning_done."""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QSpinBox, QDoubleSpinBox, QLabel, QFrame, QScrollArea,
    QApplication,
)
from PySide6.QtCore import Signal

from gui.widgets.video_panel import VideoPanel
from gui.pages.live_session_page import _short_camera_name
from engine.threshold_preview_thread import ThresholdPreviewThread
from engine.led_panel import LEDPanel
from engine.dual_panel_control import start_scanning
from domain.calibration import compute_threshold
from domain.realsense_utils import draw_led_state_overlay, crop_to_roi


class ThresholdTuningPage(QWidget):
    tuning_done = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._context = None
        self.preview_thread = None

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

        stream_a_column = QVBoxLayout()
        stream_a_column.addWidget(self.stream_a_title_label)
        stream_a_column.addWidget(self.stream_a_panel)
        stream_a_fraction_row = QHBoxLayout()
        stream_a_fraction_row.addWidget(QLabel("Threshold Fraction:"))
        stream_a_fraction_row.addWidget(self.stream_a_threshold_fraction_spinbox)
        stream_a_column.addLayout(stream_a_fraction_row)

        stream_b_column = QVBoxLayout()
        stream_b_column.addWidget(self.stream_b_title_label)
        stream_b_column.addWidget(self.stream_b_panel)
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
        self.switch_time_spinbox = QSpinBox()
        self.switch_time_spinbox.setRange(1, 10000)
        self.switch_time_spinbox.setValue(1)
        self.switch_time_spinbox.valueChanged.connect(self._on_switch_time_changed)
        control_row.addWidget(self.switch_time_spinbox)

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

    def set_context(self, ctx, device_serial, pick_a, pick_b, camera_controls,
                     stream_a_xy, stream_b_xy, stream_a_on, stream_a_off, stream_b_on, stream_b_off,
                     num_leds, neighborhood_size, scan_direction, switch_time_ms,
                     stream_a_threshold_fraction_default, stream_b_threshold_fraction_default,
                     stream_a_roi, stream_b_roi, camera_name, stream_a_label, stream_b_label,
                     dual_panel_config=None):
        self._context = dict(
            ctx=ctx, device_serial=device_serial, pick_a=pick_a, pick_b=pick_b, camera_controls=camera_controls,
            stream_a_xy=stream_a_xy, stream_b_xy=stream_b_xy,
            stream_a_on=stream_a_on, stream_a_off=stream_a_off,
            stream_b_on=stream_b_on, stream_b_off=stream_b_off,
            num_leds=num_leds, neighborhood_size=neighborhood_size, scan_direction=scan_direction,
            stream_a_roi=stream_a_roi, stream_b_roi=stream_b_roi, dual_panel_config=dual_panel_config,
        )
        self.stream_a_threshold_fraction_spinbox.setValue(stream_a_threshold_fraction_default)
        self.stream_b_threshold_fraction_spinbox.setValue(stream_b_threshold_fraction_default)
        self.switch_time_spinbox.setValue(int(round(switch_time_ms)))
        short_name = _short_camera_name(camera_name)
        self.stream_a_title_label.setText("{} - {}".format(short_name, stream_a_label))
        self.stream_b_title_label.setText("{} - {}".format(short_name, stream_b_label))
        self.status_label.setText("")
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

    def _on_stop_clicked(self):
        if self.preview_thread is not None:
            self.preview_thread.request_stop()

    def _on_preview_thread_finished(self):
        self.preview_thread = None
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.frame_sample_interval_spinbox.setEnabled(True)

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

    def _on_error(self, message):
        self.status_label.setText(message)

    def _on_switch_time_changed(self, value):
        # No thread restart needed - LEDPanel is a stateless static-method
        # CLI wrapper (engine/led_panel.py), safe to call from the GUI
        # thread while the preview thread's own capture loop keeps running,
        # since it only talks to the LED panel hardware, never the camera.
        dual_panel_config = self._context["dual_panel_config"] if self._context is not None else None
        try:
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
        except Exception as exc:
            self.status_label.setText("Failed to update LED switch time: {}".format(exc))

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
    def switch_time_ms(self):
        return self.switch_time_spinbox.value()

    def _on_continue_clicked(self):
        self._stop_preview_blocking()
        self.tuning_done.emit()
