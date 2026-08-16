"""Wizard's actual multi-camera live-run page - one CameraLiveSessionPanel
tab per configured camera (each camera's own intra-camera view, unchanged
from today's single-camera experience) plus one always-visible
cross-camera section (master vs. each slave's shared stream identities),
all driven by ONE engine.multi_camera_session.MultiCameraSessionController.

Reached from gui/pages/camera_hub_page.py's "Start Multi-Camera Live
Session" via gui/main_window.py - MainWindow calls set_cameras() with
everything self._cameras/self._master_camera_id already hold (each
camera's own finished 5-page sub-flow config), then switches the stack to
this page. See docs/superpowers's multi-camera design doc's "Design
detail" section 4.

Toolbar is deliberately RUN-level, not per-camera: Duration and Frame
Sample Interval apply identically to every camera when Start All is
clicked (the same simplification the design doc's "Explicitly deferred to
v2" list flags - per-camera-independent versions of these are a follow-up,
not built here). LED switch time is NOT a toolbar control here at all -
each camera already tuned its own switch_time_ms on its own Threshold
Tuning page; that per-camera value is used as-is.

Genlock (master/slave role assignment): CameraSessionSpec.inter_cam_sync_value
is read straight off each camera's OWN config dict (config.get(
"inter_cam_sync_value")) - this page does not resolve or guess it. That
value is decided upstream, fresh at Start-time, by gui/main_window.py's
_on_start_multi_camera_session_requested via engine.streams.
resolve_inter_cam_sync_value against settings.yaml's camera.inter_cam_sync
section (keyed by exact device name - D400 vs D500-series use different
raw value schemes on the same rs.option.inter_cam_sync_mode option, see
set_inter_cam_sync_mode's own docstring). A camera model with no entry
there resolves to None here too - genlock is skipped for that camera
rather than guessing a possibly-wrong value - and MultiCameraSessionController
is what actually applies a non-None value to the real device before
starting that camera's thread (see engine/multi_camera_session.py).
Cross-camera comparison itself stays infrared-only regardless of genlock
(see engine/cross_camera_reconciler.py) - a genlock slave's color sensor
cannot stream at all while genlocked, confirmed on real hardware.

Output layout: ONE shared run folder (domain.run_output.create_run_dir,
using the master camera's own output_root - every camera's settings.yaml-
derived output_root should be identical in practice), one per-camera
subfolder underneath it (domain.run_output.create_camera_subdir), and a
combined cross_camera_sync.csv/cross_camera_sync_plot.png written once
every camera's session has finished (_on_all_sessions_finished) - skipped
entirely for a single-camera run, where there's no cross-camera concept at
all."""

import os

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSpinBox, QTabWidget,
)

from gui.widgets.camera_live_session_panel import CameraLiveSessionPanel
from gui.widgets.live_plot import LivePlot
from gui.widgets.stats_panel import StatsPanel
from engine.multi_camera_session import CameraSessionSpec, MultiCameraSessionController
from engine.cross_camera_reconciler import build_cross_camera_pair_specs
from engine.metrics import PairingGapMetric, PositionGapMetric
from engine.test_session import TestSession, TestSessionConfig
from engine.streams import stream_slug
from domain.run_output import create_run_dir, create_camera_subdir
from domain.csv_export import export_cross_camera_csv
from domain.plot_export import export_cross_camera_plot
from domain.plot_theme import CROSS_CAMERA_COLORS


class _IdentitySpec:
    """Duck-typed stand-in for engine.multi_camera_session.CameraSessionSpec,
    carrying only the 3 attributes build_cross_camera_pair_specs actually
    reads (camera_id/is_master/stream_identities) - used here purely to
    decide which cross-camera series to show; the real CameraSessionSpec
    list built in start_all_sessions carries everything else."""

    def __init__(self, camera_id, is_master, stream_identities):
        self.camera_id = camera_id
        self.is_master = is_master
        self.stream_identities = stream_identities


def _stream_identities(config):
    return {"stream_a": stream_slug(config["pick_a"]), "stream_b": stream_slug(config["pick_b"])}


class MultiCameraLiveSessionPage(QWidget):
    def __init__(self, thread_factory=None, device_lookup=None, sync_setter=None,
                 camera_start_stagger_s=None, controller_factory=None, parent=None):
        super().__init__(parent)
        # Injectable for testing (mirrors MultiCameraSessionController's own
        # injectable collaborators) - None means "use the real ones",
        # exactly as that controller already defaults.
        self._thread_factory = thread_factory
        self._device_lookup = device_lookup
        self._sync_setter = sync_setter
        # None here means "let MultiCameraSessionController use its own
        # real default" - only overridden by tests that want start_all_
        # sessions() to run at test speed instead of paying the real,
        # multi-second-per-extra-camera USB-collision-avoidance delay (see
        # that controller's own __init__ docstring for why the delay
        # exists at all).
        self._camera_start_stagger_s = camera_start_stagger_s
        self._controller_factory = controller_factory or MultiCameraSessionController

        self._ctx = None
        self._cameras = []
        self._panels = {}
        self._cross_pair_series_keys = {}  # (slave_camera_id, stream_identity) -> series_key
        self._controller = None
        self._run_dir = None
        self._cross_rows = []

        layout = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Duration (s, 0 = manual stop):"))
        self.duration_spinbox = QSpinBox()
        self.duration_spinbox.setRange(0, 3600)
        toolbar.addWidget(self.duration_spinbox)
        self.start_button = QPushButton("Start All")
        self.start_button.clicked.connect(self.start_all_sessions)
        self.stop_button = QPushButton("Stop All")
        self.stop_button.clicked.connect(self.stop_all_sessions)
        self.stop_button.setEnabled(False)
        toolbar.addWidget(self.start_button)
        toolbar.addWidget(self.stop_button)
        toolbar.addWidget(QLabel("Frame Sample Interval:"))
        self.frame_sample_interval_spinbox = QSpinBox()
        self.frame_sample_interval_spinbox.setRange(1, 2000)
        self.frame_sample_interval_spinbox.setValue(10)
        toolbar.addWidget(self.frame_sample_interval_spinbox)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        self._cross_section_widget = QWidget()
        self._cross_section_layout = QVBoxLayout(self._cross_section_widget)
        layout.addWidget(self._cross_section_widget)
        self.cross_plot = None
        self.cross_stats_panel = None

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

    def set_cameras(self, ctx, cameras):
        """cameras: list of {"camera_id", "label", "is_master", "config"} -
        exactly what MainWindow's self._cameras/self._master_camera_id
        already hold, built fresh by MainWindow's own _refresh_camera_hub-
        style helper right before switching to this page."""
        self._ctx = ctx
        self._cameras = cameras

        self.tabs.clear()
        self._panels = {}
        for camera in cameras:
            panel = CameraLiveSessionPanel(camera["camera_id"])
            config = camera["config"]
            panel.set_camera_labels(camera["label"], config["stream_a_label"], config["stream_b_label"])
            tab_label = camera["label"] + (" [MASTER]" if camera["is_master"] else "")
            self.tabs.addTab(panel, tab_label)
            self._panels[camera["camera_id"]] = panel

        self._rebuild_cross_camera_section(cameras)

    def _rebuild_cross_camera_section(self, cameras):
        while self._cross_section_layout.count():
            item = self._cross_section_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self._cross_pair_series_keys = {}
        self.cross_plot = LivePlot()
        self.cross_plot.setLabel("left", "Cross-Camera HW TS Latency (us)")
        self.cross_plot.setLabel("bottom", "Pair Index")
        self.cross_stats_panel = StatsPanel()
        self.cross_stats_panel.setFixedWidth(220)
        self.cross_stats_panel.add_section_header("Cross-Camera HW TS Latency")

        self._cross_section_layout.addWidget(QLabel("Cross-Camera Sync (master vs. each slave)"))

        if len(cameras) < 2:
            self._cross_section_layout.addWidget(
                QLabel("Add a second camera to see cross-camera sync.")
            )
            return

        identity_specs = [
            _IdentitySpec(camera["camera_id"], camera["is_master"], _stream_identities(camera["config"]))
            for camera in cameras
        ]
        try:
            # outlier_threshold_us here only shapes the PairingGapMetric
            # instances this call constructs for its own throwaway use
            # (deciding which series to show) - start_all_sessions builds
            # the REAL ones the controller actually uses, with each run's
            # own configured threshold.
            pair_specs = build_cross_camera_pair_specs(identity_specs, outlier_threshold_us=100_000)
        except ValueError:
            # No master designated - shouldn't be reachable once Start is
            # actually clickable (CameraHubPage._can_start already requires
            # exactly one), but guard defensively rather than crash the page.
            pair_specs = []

        labels_by_id = {camera["camera_id"]: camera["label"] for camera in cameras}
        for index, spec in enumerate(pair_specs):
            series_key = "{}::{}".format(spec.slave_camera_id, spec.stream_identity)
            display_name = "{} {}".format(labels_by_id[spec.slave_camera_id], spec.stream_identity)
            color = CROSS_CAMERA_COLORS[index % len(CROSS_CAMERA_COLORS)]
            self.cross_plot.add_series(series_key, color=color, display_name=display_name)
            self.cross_stats_panel.add_field(series_key, display_name)
            self._cross_pair_series_keys[(spec.slave_camera_id, spec.stream_identity)] = series_key

        row = QHBoxLayout()
        row.addWidget(self.cross_plot, stretch=1)
        row.addWidget(self.cross_stats_panel)
        self._cross_section_layout.addLayout(row)

    def start_all_sessions(self):
        if not self._cameras:
            return
        duration_s = self.duration_spinbox.value() or None
        display_stride = self.frame_sample_interval_spinbox.value()

        # ONE shared run folder for the whole multi-camera run, one
        # subfolder per camera underneath it - every configured camera's
        # own files land together under one run instead of scattered
        # across independent top-level output/live_session_<timestamp>/
        # folders. output_root is read from the master's own config -
        # every camera's own settings.yaml-derived output_root should be
        # identical in practice (one app, one settings.yaml), same
        # assumption pairing_gap_outlier_threshold_us below already makes.
        master_config = next(c["config"] for c in self._cameras if c["is_master"])
        self._run_dir = create_run_dir(master_config["output_root"], "live_session")
        self._cross_rows = []

        camera_specs = []
        for camera in self._cameras:
            camera_id = camera["camera_id"]
            config = camera["config"]
            panel = self._panels[camera_id]

            position_gap_metric = PositionGapMetric(
                stream_a_threshold=config["stream_a_threshold"], stream_b_threshold=config["stream_b_threshold"],
                num_leds=config["num_leds"], switch_time_ms=config["switch_time_ms"],
                warmup_pairs_to_skip=config["warmup_pairs_to_skip"],
            )
            metrics = [
                PairingGapMetric(outlier_threshold_us=config["pairing_gap_outlier_threshold_us"]),
                position_gap_metric,
            ]
            test_session = TestSession(TestSessionConfig(
                metrics=metrics, duration_s=duration_s,
                stream_a_fps=config["pick_a"]["fps"], stream_b_fps=config["pick_b"]["fps"],
                frame_drop_threshold_factor=config["frame_drop_threshold_factor"],
            ))
            test_session.start()

            camera_output_dir = create_camera_subdir(self._run_dir, camera_id, camera["label"])
            output_dir = panel.prepare_for_run(
                output_dir=camera_output_dir, kept_csv_filename=config["kept_csv_filename"],
                dropped_csv_filename=config["dropped_csv_filename"],
                stream_a_xy=config["stream_a_xy"], stream_b_xy=config["stream_b_xy"],
                stream_a_roi=config["stream_a_roi"], stream_b_roi=config["stream_b_roi"],
                snapshot_every_n_pairs=config["snapshot_every_n_pairs"], max_snapshots=config["max_snapshots"],
                switch_time_ms=config["switch_time_ms"],
            )

            thread_kwargs = dict(
                pick_a=config["pick_a"], pick_b=config["pick_b"], camera_controls=config["camera_controls"],
                test_session=test_session,
                stream_a_xy=config["stream_a_xy"], stream_b_xy=config["stream_b_xy"],
                neighborhood_size=config["neighborhood_size"], scan_direction=config["scan_direction"],
                switch_time_ms=config["switch_time_ms"], display_stride=display_stride,
                position_gap_metric=position_gap_metric, dual_panel_config=config["dual_panel_config"],
                enable_depth_for_ir_sync=config["enable_depth_for_ir_sync"],
                output_dir=output_dir,
                position_gap_outlier_threshold_ms=config["position_gap_outlier_threshold_ms"],
                position_gap_outlier_max_snapshots=config["position_gap_outlier_max_snapshots"],
            )

            camera_specs.append(CameraSessionSpec(
                camera_id=camera_id, is_master=camera["is_master"],
                inter_cam_sync_value=config.get("inter_cam_sync_value"),
                stream_identities=_stream_identities(config),
                device_serial=config["device_serial"],
                hardware_reset_before_start=config["hardware_reset_before_start"],
                hardware_reset_settle_s=config["hardware_reset_settle_s"],
                thread_kwargs=thread_kwargs,
            ))

        controller_kwargs = dict(
            pairing_gap_outlier_threshold_us=master_config["pairing_gap_outlier_threshold_us"],
        )
        if self._thread_factory is not None:
            controller_kwargs["thread_factory"] = self._thread_factory
        if self._device_lookup is not None:
            controller_kwargs["device_lookup"] = self._device_lookup
        if self._sync_setter is not None:
            controller_kwargs["sync_setter"] = self._sync_setter
        if self._camera_start_stagger_s is not None:
            controller_kwargs["camera_start_stagger_s"] = self._camera_start_stagger_s

        self._controller = self._controller_factory(camera_specs, **controller_kwargs)
        self._controller.camera_frame_ready.connect(self._on_camera_frame_ready)
        self._controller.camera_row_ready.connect(self._on_camera_row_ready)
        self._controller.camera_stats_ready.connect(self._on_camera_stats_ready)
        self._controller.camera_session_finished.connect(self._on_camera_session_finished)
        self._controller.camera_error.connect(self._on_camera_error)
        self._controller.cross_pair_ready.connect(self._on_cross_pair_ready)
        self._controller.cross_stats_ready.connect(self._on_cross_stats_ready)
        self._controller.all_sessions_finished.connect(self._on_all_sessions_finished)

        self.status_label.setText("")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.duration_spinbox.setEnabled(False)
        self.frame_sample_interval_spinbox.setEnabled(False)

        self._controller.start_all(self._ctx)

    def stop_all_sessions(self):
        if self._controller is not None:
            self._controller.stop_all()

    def _on_camera_frame_ready(self, camera_id, stream_name, image, pair_index, mask):
        panel = self._panels.get(camera_id)
        if panel is not None:
            panel.on_frame_ready(stream_name, image, pair_index, mask)

    def _on_camera_row_ready(self, camera_id, row):
        panel = self._panels.get(camera_id)
        if panel is not None:
            panel.on_row_ready(row)

    def _on_camera_stats_ready(self, camera_id, row):
        panel = self._panels.get(camera_id)
        if panel is not None:
            panel.on_stats_ready(row)

    def _on_camera_session_finished(self, camera_id, rows):
        panel = self._panels.get(camera_id)
        if panel is not None:
            panel.on_session_finished(rows)

    def _on_camera_error(self, camera_id, message):
        panel = self._panels.get(camera_id)
        if panel is not None:
            panel.on_error(message)

    def _on_cross_pair_ready(self, cross_row):
        self._cross_rows.append(cross_row)
        series_key = self._cross_pair_series_keys.get(
            (cross_row["slave_camera_id"], cross_row["stream_identity"])
        )
        if series_key is None:
            return
        value = cross_row["pairing_gap_us"]
        if cross_row.get("pairing_gap_us_excluded"):
            value = float("nan")
        self.cross_plot.add_point(series_key, cross_row["pair_index"], value)

    def _on_cross_stats_ready(self, latest_by_pair):
        for key, series_key in self._cross_pair_series_keys.items():
            row = latest_by_pair.get(key)
            if row is not None:
                self.cross_stats_panel.set_value(series_key, row["pairing_gap_us"])

    def _on_all_sessions_finished(self, rows_by_camera):
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.duration_spinbox.setEnabled(True)
        self.frame_sample_interval_spinbox.setEnabled(True)

        # Only when a cross-camera comparison actually exists (>=2 cameras,
        # >=1 shared stream identity) - with a single camera there's no
        # cross-camera concept at all, and writing an empty-but-valid
        # cross_camera_sync.csv would just be confusing clutter.
        if self._cross_pair_series_keys:
            export_cross_camera_csv(self._cross_rows, os.path.join(self._run_dir, "cross_camera_sync.csv"))
            export_cross_camera_plot(self._cross_rows, os.path.join(self._run_dir, "cross_camera_sync_plot.png"))
