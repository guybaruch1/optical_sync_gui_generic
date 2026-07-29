"""Wizard shell: Device select -> Stream config -> ROI select ->
Calibration -> Live session, in a QStackedWidget, persisting choices to
state.gui_state as the user moves through the wizard."""

import os

import numpy as np
from PySide6.QtWidgets import QMainWindow, QStackedWidget, QMessageBox

from gui.pages.device_select_page import DeviceSelectPage
from gui.pages.stream_config_page import StreamConfigPage
from gui.pages.roi_select_page import RoiSelectPage
from gui.pages.calibration_page import CalibrationPage
from gui.pages.live_session_page import LiveSessionPage
from state.gui_state import GuiState, save_gui_state
from engine.streams import get_sensors_for_device
from domain.calibration import load_led_positions
from settings import ensure_output_dir


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
        stereo_sensor, rgb_sensor = get_sensors_for_device(self.ctx, serial)
        camera_settings = self.settings["camera"]
        preferred_ir = (camera_settings["ir"]["width"], camera_settings["ir"]["height"], camera_settings["ir"]["fps"])
        preferred_rgb = (
            camera_settings["color"]["width"], camera_settings["color"]["height"], camera_settings["color"]["fps"],
        )
        self.stream_config_page.populate(self.ctx, serial, stereo_sensor, rgb_sensor, preferred_ir, preferred_rgb)
        self.stack.setCurrentWidget(self.stream_config_page)

    def _on_config_chosen(self, config):
        ir_width, ir_height, ir_fps, rgb_width, rgb_height, rgb_fps = config
        self.gui_state.ir_width, self.gui_state.ir_height, self.gui_state.ir_fps = ir_width, ir_height, ir_fps
        self.gui_state.rgb_width, self.gui_state.rgb_height, self.gui_state.rgb_fps = rgb_width, rgb_height, rgb_fps
        save_gui_state(self.gui_state)
        self.roi_page.set_context(
            self.ctx, self.gui_state.device_serial,
            (ir_width, ir_height), ir_fps, (rgb_width, rgb_height), rgb_fps,
            settle_frames=self.settings["calibration"]["settle_frames"],
        )
        self.stack.setCurrentWidget(self.roi_page)

    def _on_roi_chosen(self, rois):
        ir_roi, rgb_roi = rois
        self.gui_state.ir_roi = list(ir_roi)
        self.gui_state.rgb_roi = list(rgb_roi)
        save_gui_state(self.gui_state)
        calib_settings = self.settings["calibration"]
        self.calibration_page.set_context(
            self.ctx, self.gui_state.device_serial,
            (self.gui_state.ir_width, self.gui_state.ir_height), self.gui_state.ir_fps,
            (self.gui_state.rgb_width, self.gui_state.rgb_height), self.gui_state.rgb_fps,
            ir_roi, rgb_roi,
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
        camera_name = self._current_device_name()
        config_path = self.settings["paths"]["config_path"]
        ir_positions, rgb_positions = load_led_positions(config_path, camera_name)

        ir_ids = list(ir_positions.keys())
        rgb_ids = list(rgb_positions.keys())
        ir_xy = np.array([ir_positions[i][:2] for i in ir_ids])
        rgb_xy = np.array([rgb_positions[i][:2] for i in rgb_ids])

        num_leds = self.settings["test"]["num_leds"]
        if len(ir_ids) != len(rgb_ids) or len(ir_ids) != num_leds:
            QMessageBox.warning(
                self,
                "LED count mismatch",
                "Calibration detected {} IR LED(s) and {} RGB LED(s), but settings.yaml's "
                "test.num_leds is {}. The live session's position-gap math assumes all three "
                "match - proceeding anyway, but treat position-gap results with caution until "
                "this is resolved (re-run calibration, or fix test.num_leds).".format(
                    len(ir_ids), len(rgb_ids), num_leds
                ),
            )

        ir_on = np.array([ir_positions[i][2] for i in ir_ids])
        ir_off = np.array([ir_positions[i][3] for i in ir_ids])
        rgb_on = np.array([rgb_positions[i][2] for i in rgb_ids])
        rgb_off = np.array([rgb_positions[i][3] for i in rgb_ids])

        threshold_fraction = self.settings["test"]["threshold_fraction"]
        ir_threshold = ir_off + threshold_fraction * (ir_on - ir_off)
        rgb_threshold = rgb_off + threshold_fraction * (rgb_on - rgb_off)

        output_dir = ensure_output_dir(self.settings)
        self.live_session_page.set_context(
            self.ctx, self.gui_state.device_serial,
            (self.gui_state.ir_width, self.gui_state.ir_height), self.gui_state.ir_fps,
            (self.gui_state.rgb_width, self.gui_state.rgb_height), self.gui_state.rgb_fps,
            switch_time_ms=self.settings["test"]["switch_time_ms"],
            scan_direction=self.settings["test"]["scan_direction"],
            ir_threshold=ir_threshold, rgb_threshold=rgb_threshold, ir_xy=ir_xy, rgb_xy=rgb_xy,
            num_leds=num_leds, neighborhood_size=self.settings["test"]["neighborhood_size"],
            frame_drop_threshold_factor=self.settings["test"]["frame_drop_threshold_factor"],
            warmup_pairs_to_skip=self.settings["test"]["warmup_pairs_to_skip"],
            pairing_gap_outlier_threshold_us=self.settings["test"]["pairing_gap_outlier_threshold_us"],
            kept_csv_path=os.path.join(output_dir, self.settings["paths"]["raw_csv_path"]),
            dropped_csv_path=os.path.join(output_dir, self.settings["paths"]["frame_drop_csv_path"]),
            output_dir=output_dir,
            snapshot_every_n_pairs=self.settings["test"]["snapshot_every_n_pairs"],
            max_snapshots=self.settings["test"]["max_snapshots"],
            ir_roi=self.gui_state.ir_roi, rgb_roi=self.gui_state.rgb_roi, camera_name=camera_name,
        )
        self.stack.setCurrentWidget(self.live_session_page)

    def _current_device_name(self):
        # Cached from DeviceSelectPage.device_chosen's payload (see
        # _on_device_chosen), not looked up by reaching into device_page's own
        # _devices list - that reach-through also meant raising if the device
        # had since disappeared (e.g. the camera was unplugged mid-wizard).
        return self._device_name
