"""Wizard shell: Device select -> Stream config -> ROI select ->
Calibration -> Live session, in a QStackedWidget, persisting choices to
state.gui_state as the user moves through the wizard.

Generalized from the old hardcoded IR/RGB-sensor version to the generic
pick_a/pick_b stream picks the Stream Config page now produces (Task 18):
this file itself is the only place that still needs to remember the LIVE
pick_a/pick_b/camera_controls values across wizard steps within one run
(as self._pick_a/self._pick_b/self._camera_controls instance attributes) -
GuiState only stores a lossy, JSON-friendly prefill record (no `format`/
`sensor_index`) for the NEXT app launch's Stream Config defaults, not a
full pick reconstruction usable later in this same run.
"""

import os

import numpy as np
from PySide6.QtWidgets import QMainWindow, QStackedWidget, QMessageBox

from gui.pages.device_select_page import DeviceSelectPage
from gui.pages.stream_config_page import StreamConfigPage
from gui.pages.roi_select_page import RoiSelectPage, stream_label
from gui.pages.calibration_page import CalibrationPage
from gui.pages.live_session_page import LiveSessionPage
from state.gui_state import GuiState, save_gui_state
from engine.streams import list_video_stream_options, stream_slug
from domain.calibration import load_led_positions
from settings import ensure_output_dir


def _controls_for_pick(pick, camera_controls):
    return next(c for c in camera_controls if pick["sensor_index"] in c["sensor_indices"])


class MainWindow(QMainWindow):
    def __init__(self, ctx, gui_state: GuiState, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Optical Sync GUI")
        self.ctx = ctx
        self.gui_state = gui_state
        self.settings = settings

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.device_page = DeviceSelectPage()
        self.stream_config_page = StreamConfigPage()
        self.roi_page = RoiSelectPage()
        self.calibration_page = CalibrationPage()
        self.live_session_page = LiveSessionPage()
        self._device_name = None
        # Live pick_a/pick_b/camera_controls, stashed here in
        # _on_config_chosen and read back in _on_roi_chosen/
        # _on_calibration_done - GuiState alone can't round-trip these (it
        # deliberately doesn't store `format`/`sensor_index`, see module
        # docstring), and CalibrationPage.calibration_done is a bare signal
        # with no payload.
        self._pick_a = None
        self._pick_b = None
        self._camera_controls = None

        for page in (self.device_page, self.stream_config_page, self.roi_page,
                     self.calibration_page, self.live_session_page):
            self.stack.addWidget(page)

        self.device_page.device_chosen.connect(self._on_device_chosen)
        self.stream_config_page.config_chosen.connect(self._on_config_chosen)
        self.roi_page.roi_chosen.connect(self._on_roi_chosen)
        self.calibration_page.calibration_done.connect(self._on_calibration_done)

        self.device_page.refresh_devices(self.ctx)
        self.stack.setCurrentWidget(self.device_page)

    def _on_device_chosen(self, serial, name):
        self.gui_state.device_serial = serial
        self._device_name = name
        save_gui_state(self.gui_state)
        stream_options = list_video_stream_options(self.ctx, serial)
        camera_settings = self.settings["camera"]
        self.stream_config_page.populate(
            self.ctx, serial, stream_options,
            preferred_a=camera_settings["stream_a"], preferred_b=camera_settings["stream_b"],
        )
        self.stack.setCurrentWidget(self.stream_config_page)

    def _on_config_chosen(self, config):
        pick_a, pick_b, camera_controls = config
        self._pick_a = pick_a
        self._pick_b = pick_b
        self._camera_controls = camera_controls

        control_a = _controls_for_pick(pick_a, camera_controls)
        control_b = _controls_for_pick(pick_b, camera_controls)
        self.gui_state.stream_a_type = pick_a["stream_type"].name
        self.gui_state.stream_a_index = pick_a["stream_index"]
        self.gui_state.stream_a_width = pick_a["width"]
        self.gui_state.stream_a_height = pick_a["height"]
        self.gui_state.stream_a_fps = pick_a["fps"]
        self.gui_state.stream_a_emitter_enabled = control_a["emitter_enabled"]
        self.gui_state.stream_a_auto_exposure = control_a["auto_exposure"]
        self.gui_state.stream_a_exposure = control_a["exposure"]
        self.gui_state.stream_a_gain = control_a["gain"]
        self.gui_state.stream_b_type = pick_b["stream_type"].name
        self.gui_state.stream_b_index = pick_b["stream_index"]
        self.gui_state.stream_b_width = pick_b["width"]
        self.gui_state.stream_b_height = pick_b["height"]
        self.gui_state.stream_b_fps = pick_b["fps"]
        self.gui_state.stream_b_emitter_enabled = control_b["emitter_enabled"]
        self.gui_state.stream_b_auto_exposure = control_b["auto_exposure"]
        self.gui_state.stream_b_exposure = control_b["exposure"]
        self.gui_state.stream_b_gain = control_b["gain"]
        save_gui_state(self.gui_state)

        self.roi_page.set_context(
            self.ctx, self.gui_state.device_serial, pick_a, pick_b, camera_controls,
            settle_frames=self.settings["calibration"]["settle_frames"],
        )
        self.stack.setCurrentWidget(self.roi_page)

    def _on_roi_chosen(self, rois):
        stream_a_roi, stream_b_roi = rois
        self.gui_state.stream_a_roi = list(stream_a_roi)
        self.gui_state.stream_b_roi = list(stream_b_roi)
        save_gui_state(self.gui_state)

        calib_settings = self.settings["calibration"]
        self.calibration_page.set_context(
            self.ctx, self.gui_state.device_serial,
            self._pick_a, self._pick_b, self._camera_controls,
            stream_a_roi, stream_b_roi,
            config_path=self.settings["paths"]["config_path"],
            camera_name=self._current_device_name(),
            output_dir=ensure_output_dir(self.settings),
            settle_frames=calib_settings["settle_frames"],
            min_blob_area=calib_settings["min_blob_area"],
            neighborhood_size=calib_settings["neighborhood_size"],
            row_gap_px=calib_settings["row_gap_px"],
            min_acceptable_contrast=calib_settings["min_acceptable_contrast"],
        )
        self.stack.setCurrentWidget(self.calibration_page)

    def _on_calibration_done(self):
        pick_a, pick_b, camera_controls = self._pick_a, self._pick_b, self._camera_controls
        camera_name = self._current_device_name()
        config_path = self.settings["paths"]["config_path"]
        slug_a, slug_b = stream_slug(pick_a), stream_slug(pick_b)
        stream_a_positions, stream_b_positions = load_led_positions(config_path, camera_name, slug_a, slug_b)

        stream_a_ids = list(stream_a_positions.keys())
        stream_b_ids = list(stream_b_positions.keys())
        stream_a_xy = np.array([stream_a_positions[i][:2] for i in stream_a_ids])
        stream_b_xy = np.array([stream_b_positions[i][:2] for i in stream_b_ids])

        num_leds = self.settings["test"]["num_leds"]
        if len(stream_a_ids) != len(stream_b_ids) or len(stream_a_ids) != num_leds:
            QMessageBox.warning(
                self,
                "LED count mismatch",
                "Calibration detected {} {} LED(s) and {} {} LED(s), but settings.yaml's "
                "test.num_leds is {}. The live session's position-gap math assumes all three "
                "match - proceeding anyway, but treat position-gap results with caution until "
                "this is resolved (re-run calibration, or fix test.num_leds).".format(
                    len(stream_a_ids), stream_label(pick_a), len(stream_b_ids), stream_label(pick_b), num_leds
                ),
            )

        stream_a_on = np.array([stream_a_positions[i][2] for i in stream_a_ids])
        stream_a_off = np.array([stream_a_positions[i][3] for i in stream_a_ids])
        stream_b_on = np.array([stream_b_positions[i][2] for i in stream_b_ids])
        stream_b_off = np.array([stream_b_positions[i][3] for i in stream_b_ids])

        threshold_fraction = self.settings["test"]["threshold_fraction"]
        stream_a_threshold = stream_a_off + threshold_fraction * (stream_a_on - stream_a_off)
        stream_b_threshold = stream_b_off + threshold_fraction * (stream_b_on - stream_b_off)

        output_dir = ensure_output_dir(self.settings)
        self.live_session_page.set_context(
            self.ctx, self.gui_state.device_serial, pick_a, pick_b, camera_controls,
            switch_time_ms=self.settings["test"]["switch_time_ms"],
            scan_direction=self.settings["test"]["scan_direction"],
            stream_a_threshold=stream_a_threshold, stream_b_threshold=stream_b_threshold,
            stream_a_xy=stream_a_xy, stream_b_xy=stream_b_xy,
            num_leds=num_leds, neighborhood_size=self.settings["test"]["neighborhood_size"],
            frame_drop_threshold_factor=self.settings["test"]["frame_drop_threshold_factor"],
            warmup_pairs_to_skip=self.settings["test"]["warmup_pairs_to_skip"],
            pairing_gap_outlier_threshold_us=self.settings["test"]["pairing_gap_outlier_threshold_us"],
            kept_csv_path=os.path.join(output_dir, self.settings["paths"]["raw_csv_path"]),
            dropped_csv_path=os.path.join(output_dir, self.settings["paths"]["frame_drop_csv_path"]),
            output_dir=output_dir,
            snapshot_every_n_pairs=self.settings["test"]["snapshot_every_n_pairs"],
            max_snapshots=self.settings["test"]["max_snapshots"],
            stream_a_roi=self.gui_state.stream_a_roi, stream_b_roi=self.gui_state.stream_b_roi,
            camera_name=camera_name,
            stream_a_label=stream_label(pick_a), stream_b_label=stream_label(pick_b),
        )
        self.stack.setCurrentWidget(self.live_session_page)

    def _current_device_name(self):
        # Cached from DeviceSelectPage.device_chosen's payload (see
        # _on_device_chosen), not looked up by reaching into device_page's own
        # _devices list - that reach-through also meant raising if the device
        # had since disappeared (e.g. the camera was unplugged mid-wizard).
        return self._device_name
