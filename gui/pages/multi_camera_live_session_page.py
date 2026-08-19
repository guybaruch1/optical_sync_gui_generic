"""Wizard's actual multi-camera live-run page - a tab widget with one
always-first "Cross-Camera Sync" tab (one section per slave camera, each
with its own HW TS Latency + Optical Sync graphs and stats panel - shown
directly for a single slave, or inside an inner tab widget once there are
2) followed by one CameraLiveSessionPanel tab per configured camera (each
camera's own intra-camera view, unchanged from today's single-camera
experience), all driven by ONE
engine.multi_camera_session.MultiCameraSessionController.

Reached from gui/pages/camera_hub_page.py's "Start Multi-Camera Live
Session" via gui/main_window.py - MainWindow calls set_cameras() with
everything self._cameras/self._master_camera_id already hold (each
camera's own finished 5-page sub-flow config), then switches the stack to
this page. See docs/superpowers's multi-camera design doc's "Design
detail" section 4.

Toolbar is deliberately RUN-level, not per-camera: Duration, LED Switch
Time, and Frame Sample Interval all apply identically to every camera when
Start All is clicked. LED Switch Time replaces every configured camera's
own individually-tuned switch_time_ms (set on that camera's own Threshold
Tuning page) for the run - it's a per-test parameter, not per-camera,
since it configures the LED panel itself (one physical panel stepping at
one real rate, even in the shared-single-panel case with 2+ cameras), not
any one camera. Duration/Frame Sample Interval staying run-level rather
than per-camera-independent is the design doc's own "Explicitly deferred
to v2" simplification, unrelated to this.

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
Cross-camera comparison pairs any shared stream identity the operator
configured (infrared or color) - see engine/cross_camera_reconciler.py
and gui/main_window.py's own resolution-ceiling guard for a genlock
slave's color stream.

Output layout: ONE shared run folder (domain.run_output.create_run_dir,
using the master camera's own output_root - every camera's settings.yaml-
derived output_root should be identical in practice), one per-camera
subfolder underneath it (domain.run_output.create_camera_subdir), and a
combined cross_camera_sync.csv plus one cross_camera_sync_plot_{slave-slug}.png
per slave, all written once every camera's session has finished
(_on_all_sessions_finished) - skipped entirely for a single-camera run,
where there's no cross-camera concept at all."""

import os

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSpinBox, QDoubleSpinBox, QTabWidget,
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
from domain.running_stats import RunningStats


class _IdentitySpec:
    """Duck-typed stand-in for engine.multi_camera_session.CameraSessionSpec,
    carrying only the 5 attributes build_cross_camera_pair_specs actually
    reads (camera_id/is_master/stream_identities/num_leds/switch_time_ms) -
    used here purely to decide which cross-camera series to show; the real
    CameraSessionSpec list built in start_all_sessions carries everything
    else."""

    def __init__(self, camera_id, is_master, stream_identities, num_leds, switch_time_ms):
        self.camera_id = camera_id
        self.is_master = is_master
        self.stream_identities = stream_identities
        self.num_leds = num_leds
        self.switch_time_ms = switch_time_ms


def _stream_identities(config):
    return {"stream_a": stream_slug(config["pick_a"]), "stream_b": stream_slug(config["pick_b"])}


def _slave_vs_master_title(slave_role, master_display):
    """Shared by the live cross-camera section header (_build_slave_section)
    and the static-export plot title (_on_all_sessions_finished) - the
    design spec requires these two to read identically, so both call this
    one helper instead of each hand-writing its own .format(...) that could
    silently drift apart."""
    return "{}: {}  vs.  Master: {}".format(slave_role["tag"].title(), slave_role["display"], master_display)


def _camera_roles(cameras):
    """Computes each camera's master/slave-N role once, reused everywhere a
    role/label/serial needs displaying (per-camera tabs, cross-camera
    section headers, static-export titles/filenames) - so the numbering is
    never computed two different ways. Slave numbering is assigned in the
    order cameras appear in `cameras` (excluding master), the same order
    the per-camera tabs already iterate in."""
    roles = {}
    slave_number = 0
    for camera in cameras:
        camera_id = camera["camera_id"]
        display = "{} (SN {})".format(camera["label"], camera["config"]["device_serial"])
        if camera["is_master"]:
            roles[camera_id] = {"tag": "MASTER", "slug": "master", "display": display}
        else:
            slave_number += 1
            roles[camera_id] = {
                "tag": "SLAVE {}".format(slave_number),
                "slug": "slave{}".format(slave_number),
                "display": display,
            }
    return roles


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
        # The switch-time value last acknowledged via Confirm - a real gate
        # (see _update_confirm_switch_time_button_state): start_all_sessions()
        # reads THIS, not the spinbox directly, for every configured camera.
        self._last_confirmed_switch_time_ms = 1.0
        # True for the entire span between start_all_sessions() and
        # _on_all_sessions_finished() - start_button's own enabled state is
        # the AND of "not currently running" and "no pending unconfirmed
        # switch-time edit" (see _update_confirm_switch_time_button_state).
        self._session_running = False

        layout = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Duration (s, 0 = manual stop):"))
        self.duration_spinbox = QSpinBox()
        self.duration_spinbox.setRange(0, 3600)
        toolbar.addWidget(self.duration_spinbox)

        toolbar.addWidget(QLabel("LED Switch Time (ms):"))
        self.switch_time_spinbox = QDoubleSpinBox()
        self.switch_time_spinbox.setRange(0.1, 10000.0)
        self.switch_time_spinbox.setDecimals(1)
        self.switch_time_spinbox.setSingleStep(0.5)
        self.switch_time_spinbox.setValue(1.0)
        self.switch_time_spinbox.valueChanged.connect(self._on_switch_time_spinbox_changed)
        toolbar.addWidget(self.switch_time_spinbox)
        self.confirm_switch_time_button = QPushButton("Confirm")
        self.confirm_switch_time_button.setEnabled(False)
        self.confirm_switch_time_button.setToolTip(
            "Confirm the LED Switch Time above. This is a per-test parameter shared by "
            "every camera (it configures the LED panel, not any one camera) - "
            "start_all_sessions() reads the last confirmed value, and Start All stays "
            "disabled until any edit here is confirmed or reverted."
        )
        self.confirm_switch_time_button.clicked.connect(self._on_confirm_switch_time_clicked)
        toolbar.addWidget(self.confirm_switch_time_button)

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

        self._cross_tab_widget = QWidget()
        self._cross_tab_layout = QVBoxLayout(self._cross_tab_widget)
        # slave_camera_id -> {"pairing_plot": LivePlot, "position_plot": LivePlot,
        # "stats_panel": StatsPanel} - one full section's worth of widgets per slave.
        self._slave_sections = {}
        # (slave_camera_id, stream_identity, metric_name) -> RunningStats,
        # metric_name is "pairing_gap_us" or "position_gap_ms" - accumulated
        # unthrottled in _on_cross_pair_ready, pushed to the stats panel only
        # on the throttled _on_cross_stats_ready tick.
        self._cross_running_stats = {}

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

    def set_cameras(self, ctx, cameras):
        """cameras: list of {"camera_id", "label", "is_master", "config"} -
        exactly what MainWindow's self._cameras/self._master_camera_id
        already hold, built fresh by MainWindow's own _refresh_camera_hub-
        style helper right before switching to this page."""
        self._ctx = ctx
        self._cameras = cameras

        # Prefill the switch-time spinbox from the MASTER camera's own tuned
        # config value (the same "master's config is authoritative"
        # precedent num_leds/switch_time_ms already use elsewhere for the
        # cross-camera Optical Sync feature) - mirrors LiveSessionPage.
        # set_context()'s own prefill-from-that-camera's-tuned-value. Falls
        # back to __init__'s fixed 1.0 default (left untouched) if no master
        # is flagged (shouldn't happen once Start is actually clickable, but
        # this page never crashes on a "shouldn't happen" input).
        master_config = next((c["config"] for c in cameras if c["is_master"]), None)
        if master_config is not None:
            self._last_confirmed_switch_time_ms = master_config["switch_time_ms"]
            self.switch_time_spinbox.setValue(master_config["switch_time_ms"])
            self._update_confirm_switch_time_button_state()

        self.tabs.clear()
        self._panels = {}

        # Cross-camera tab first - it's the operator's primary test.
        self._rebuild_cross_camera_section(cameras)
        self.tabs.addTab(self._cross_tab_widget, "Cross-Camera Sync")

        roles = _camera_roles(cameras)
        for camera in cameras:
            panel = CameraLiveSessionPanel(camera["camera_id"])
            config = camera["config"]
            panel.set_camera_labels(camera["label"], config["stream_a_label"], config["stream_b_label"])
            tab_label = "{} [{}]".format(camera["label"], roles[camera["camera_id"]]["tag"])
            self.tabs.addTab(panel, tab_label)
            self._panels[camera["camera_id"]] = panel

    def _rebuild_cross_camera_section(self, cameras):
        while self._cross_tab_layout.count():
            item = self._cross_tab_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self._cross_pair_series_keys = {}
        self._slave_sections = {}
        self._cross_running_stats = {}

        self._cross_tab_layout.addWidget(QLabel("Cross-Camera Sync (master vs. each slave)"))

        if len(cameras) < 2:
            self._cross_tab_layout.addWidget(
                QLabel("Add a second camera to see cross-camera sync.")
            )
            return

        roles = _camera_roles(cameras)
        master_camera = next((c for c in cameras if c["is_master"]), None)
        if master_camera is None:
            self._cross_tab_layout.addWidget(
                QLabel("Designate a master camera to see cross-camera sync.")
            )
            return
        master_display = roles[master_camera["camera_id"]]["display"]

        identity_specs = [
            _IdentitySpec(
                camera["camera_id"], camera["is_master"], _stream_identities(camera["config"]),
                num_leds=camera["config"]["num_leds"], switch_time_ms=camera["config"]["switch_time_ms"],
            )
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

        # Grouped by slave, preserving each slave's own identity order -
        # build_cross_camera_pair_specs already returns identities sorted
        # per slave, so no re-sorting needed here.
        specs_by_slave = {}
        for spec in pair_specs:
            specs_by_slave.setdefault(spec.slave_camera_id, []).append(spec)

        slave_cameras = [camera for camera in cameras if not camera["is_master"]]

        if len(slave_cameras) == 1:
            slave = slave_cameras[0]
            section_widget = self._build_slave_section(
                slave, roles, master_display, specs_by_slave.get(slave["camera_id"], [])
            )
            self._cross_tab_layout.addWidget(section_widget)
        else:
            inner_tabs = QTabWidget()
            for slave in slave_cameras:
                section_widget = self._build_slave_section(
                    slave, roles, master_display, specs_by_slave.get(slave["camera_id"], [])
                )
                tab_label = "{}: {}".format(roles[slave["camera_id"]]["tag"].title(), slave["label"])
                inner_tabs.addTab(section_widget, tab_label)
            self._cross_tab_layout.addWidget(inner_tabs)

    def _build_slave_section(self, slave, roles, master_display, specs):
        """One slave's worth of cross-camera UI: a header line, two stacked
        graphs (HW TS Latency, Optical Sync), and one combined stats panel -
        mirrors CameraLiveSessionPanel's own graphs_column + single
        stats_panel layout exactly, scoped to this one slave's shared
        identities. Registers this slave's series keys and RunningStats
        instances into self._cross_pair_series_keys/self._cross_running_stats
        as a side effect - _on_cross_pair_ready/_on_cross_stats_ready
        (Task 4) read those to route incoming cross-rows here."""
        slave_camera_id = slave["camera_id"]
        slave_role = roles[slave_camera_id]

        section_widget = QWidget()
        section_layout = QVBoxLayout(section_widget)

        header_text = _slave_vs_master_title(slave_role, master_display)
        section_layout.addWidget(QLabel(header_text))

        pairing_plot = LivePlot()
        pairing_plot.setLabel("left", "HW TS Latency (us)")
        pairing_plot.setLabel("bottom", "Pair Index")

        position_plot = LivePlot()
        position_plot.setLabel("left", "Optical Sync (ms)")
        position_plot.setLabel("bottom", "Pair Index")

        stats_panel = StatsPanel()
        stats_panel.setFixedWidth(220)
        stats_panel.add_section_header("Live Data")
        stats_panel.add_field("pair_index", "Pair Index")
        for spec in specs:
            identity = spec.stream_identity
            stats_panel.add_field("{}_pairing_gap_us".format(identity), "{} HW TS Latency (us)".format(identity))
            stats_panel.add_field("{}_position_gap_ms".format(identity), "{} Optical Sync (ms)".format(identity))
        stats_panel.add_field("switch_time_ms", "LED Switch Time (ms)")
        stats_panel.add_section_header("Stats")
        stats_rows = []
        for spec in specs:
            identity = spec.stream_identity
            stats_rows.append(("{}_hw_ts_latency".format(identity), "{} HW TS Latency".format(identity)))
            stats_rows.append(("{}_optical_sync".format(identity), "{} Optical Sync".format(identity)))
        stats_panel.add_stats_table(stats_rows)
        if specs:
            stats_panel.set_value("switch_time_ms", specs[0].switch_time_ms)

        for index, spec in enumerate(specs):
            identity = spec.stream_identity
            color = CROSS_CAMERA_COLORS[index % len(CROSS_CAMERA_COLORS)]
            pairing_plot.add_series(identity, color=color, display_name=identity)
            position_plot.add_series(identity, color=color, display_name=identity)
            self._cross_pair_series_keys[(slave_camera_id, identity)] = identity
            self._cross_running_stats[(slave_camera_id, identity, "pairing_gap_us")] = RunningStats()
            self._cross_running_stats[(slave_camera_id, identity, "position_gap_ms")] = RunningStats()

        self._slave_sections[slave_camera_id] = {
            "pairing_plot": pairing_plot, "position_plot": position_plot, "stats_panel": stats_panel,
        }

        graphs_column = QVBoxLayout()
        graphs_column.addWidget(pairing_plot, stretch=1)
        graphs_column.addWidget(position_plot, stretch=1)

        middle_row = QHBoxLayout()
        middle_row.addLayout(graphs_column, stretch=1)
        middle_row.addWidget(stats_panel)
        section_layout.addLayout(middle_row)

        return section_widget

    def _reset_cross_run_state(self):
        """Mirrors CameraLiveSessionPanel.prepare_for_run's own per-run reset
        of ITS plots/stats, for the cross-camera widgets: without this,
        repeated Start-All clicks in the same page visit leave
        self._cross_running_stats' min/avg/std/max permanently polluted by
        every previous run's samples (min/max in particular never recover),
        and the plots keep drawing the new run's points (pair_index
        restarting from 1) on top of the previous run's leftover data. Also
        refreshes each slave's own "LED Switch Time (ms)" display field to
        the value THIS run will actually use - it was built once, at
        set_cameras() time, from each camera's own original per-camera
        config, which no longer matches once the operator confirms a
        per-test override that differs from it."""
        for section in self._slave_sections.values():
            section["pairing_plot"].clear_data()
            section["position_plot"].clear_data()
            section["stats_panel"].set_value("switch_time_ms", self._last_confirmed_switch_time_ms)
        for key in self._cross_running_stats:
            self._cross_running_stats[key] = RunningStats()

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
        self._reset_cross_run_state()

        camera_specs = []
        for camera in self._cameras:
            camera_id = camera["camera_id"]
            config = camera["config"]
            panel = self._panels[camera_id]

            position_gap_metric = PositionGapMetric(
                stream_a_threshold=config["stream_a_threshold"], stream_b_threshold=config["stream_b_threshold"],
                num_leds=config["num_leds"], switch_time_ms=self._last_confirmed_switch_time_ms,
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
                switch_time_ms=self._last_confirmed_switch_time_ms,
            )

            thread_kwargs = dict(
                pick_a=config["pick_a"], pick_b=config["pick_b"], camera_controls=config["camera_controls"],
                test_session=test_session,
                stream_a_xy=config["stream_a_xy"], stream_b_xy=config["stream_b_xy"],
                neighborhood_size=config["neighborhood_size"], scan_direction=config["scan_direction"],
                switch_time_ms=self._last_confirmed_switch_time_ms, display_stride=display_stride,
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
                num_leds=config["num_leds"], switch_time_ms=self._last_confirmed_switch_time_ms,
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
        self._session_running = True
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.duration_spinbox.setEnabled(False)
        self.switch_time_spinbox.setEnabled(False)
        self.confirm_switch_time_button.setEnabled(False)
        self.frame_sample_interval_spinbox.setEnabled(False)

        self._controller.start_all(self._ctx)

    def stop_all_sessions(self):
        if self._controller is not None:
            self._controller.stop_all()

    def _on_switch_time_spinbox_changed(self, value):
        self._update_confirm_switch_time_button_state()

    def _update_confirm_switch_time_button_state(self):
        unconfirmed = self.switch_time_spinbox.value() != self._last_confirmed_switch_time_ms
        self.confirm_switch_time_button.setEnabled(unconfirmed)
        # Not gated while a session is running - start_all_sessions()'s own
        # lock (setEnabled(False)) already owns start_button for that span.
        if not self._session_running:
            self.start_button.setEnabled(not unconfirmed)

    def _on_confirm_switch_time_clicked(self):
        self._last_confirmed_switch_time_ms = self.switch_time_spinbox.value()
        self._update_confirm_switch_time_button_state()

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
        # O(1) bookkeeping only - no add_point here. Fires unthrottled, once
        # per cross-camera match; plotting on this cadence caused a real GUI
        # freeze for the analogous intra-camera case (see CLAUDE.md's
        # row_ready/stats_ready cadence split). Both graphs' add_point calls,
        # and the actual stats-panel pushes, happen only in
        # _on_cross_stats_ready, below - RunningStats.update() here is the
        # one exception, matching CameraLiveSessionPanel.on_row_ready's own
        # unthrottled accumulation (cheap, no plotting).
        self._cross_rows.append(cross_row)

        key = (cross_row["slave_camera_id"], cross_row["stream_identity"])
        pairing_stats = self._cross_running_stats.get(key + ("pairing_gap_us",))
        if pairing_stats is not None and not cross_row.get("pairing_gap_us_excluded"):
            pairing_stats.update(cross_row["pairing_gap_us"])
        position_stats = self._cross_running_stats.get(key + ("position_gap_ms",))
        if (position_stats is not None and cross_row.get("position_gap_ms") is not None
                and not cross_row.get("position_gap_ms_excluded")):
            position_stats.update(cross_row["position_gap_ms"])

    def _on_cross_stats_ready(self, latest_by_pair):
        rows_by_slave = {}
        for (slave_camera_id, identity), row in latest_by_pair.items():
            rows_by_slave.setdefault(slave_camera_id, []).append((identity, row))

        for slave_camera_id, identity_rows in rows_by_slave.items():
            section = self._slave_sections.get(slave_camera_id)
            if section is None:
                continue
            stats_panel = section["stats_panel"]
            pairing_plot = section["pairing_plot"]
            position_plot = section["position_plot"]

            # A slave sharing multiple identities can have each identity's
            # own match complete independently, landing different
            # pair_index values in the same tick - show the most recently
            # completed match across all of this slave's identities as the
            # single "is this still updating" heartbeat.
            stats_panel.set_value("pair_index", max(row["pair_index"] for _, row in identity_rows))

            for identity, row in identity_rows:
                series_key = self._cross_pair_series_keys.get((slave_camera_id, identity))
                if series_key is None:
                    continue

                stats_panel.set_value("{}_pairing_gap_us".format(identity), row["pairing_gap_us"])
                pairing_value = row["pairing_gap_us"]
                if row.get("pairing_gap_us_excluded"):
                    pairing_value = float("nan")
                pairing_plot.add_point(series_key, row["pair_index"], pairing_value)

                if row.get("position_gap_ms") is not None:
                    stats_panel.set_value("{}_position_gap_ms".format(identity), row["position_gap_ms"])
                    position_value = row["position_gap_ms"]
                    if row.get("position_gap_ms_excluded"):
                        position_value = float("nan")
                    position_plot.add_point(series_key, row["pair_index"], position_value)

                pairing_stats = self._cross_running_stats.get((slave_camera_id, identity, "pairing_gap_us"))
                if pairing_stats is not None:
                    self._push_running_stats(stats_panel, "{}_hw_ts_latency".format(identity), pairing_stats)
                position_stats = self._cross_running_stats.get((slave_camera_id, identity, "position_gap_ms"))
                if position_stats is not None:
                    self._push_running_stats(stats_panel, "{}_optical_sync".format(identity), position_stats)

    def _push_running_stats(self, stats_panel, key, stats):
        if stats.count == 0:
            return
        stats_panel.set_value("{}_min".format(key), round(stats.min, 1))
        stats_panel.set_value("{}_avg".format(key), round(stats.mean, 1))
        stats_panel.set_value("{}_std".format(key), round(stats.std, 1))
        stats_panel.set_value("{}_max".format(key), round(stats.max, 1))

    def _on_all_sessions_finished(self, rows_by_camera):
        self._session_running = False
        self.stop_button.setEnabled(False)
        self.duration_spinbox.setEnabled(True)
        self.switch_time_spinbox.setEnabled(True)
        # Not a blind setEnabled(True) on start_button - its availability
        # also reflects whether the spinbox actually holds an unconfirmed
        # value (see _update_confirm_switch_time_button_state).
        self._update_confirm_switch_time_button_state()
        self.frame_sample_interval_spinbox.setEnabled(True)

        # Only when a cross-camera comparison actually exists (>=2 cameras,
        # >=1 shared stream identity) - with a single camera there's no
        # cross-camera concept at all, and writing an empty-but-valid
        # cross_camera_sync.csv would just be confusing clutter.
        if self._cross_pair_series_keys:
            export_cross_camera_csv(self._cross_rows, os.path.join(self._run_dir, "cross_camera_sync.csv"))

            roles = _camera_roles(self._cameras)
            master_camera = next(c for c in self._cameras if c["is_master"])
            master_display = roles[master_camera["camera_id"]]["display"]

            slave_ids = sorted({row["slave_camera_id"] for row in self._cross_rows})
            for slave_camera_id in slave_ids:
                slave_role = roles[slave_camera_id]
                rows_for_slave = [row for row in self._cross_rows if row["slave_camera_id"] == slave_camera_id]
                title = _slave_vs_master_title(slave_role, master_display)
                path = os.path.join(
                    self._run_dir, "cross_camera_sync_plot_{}.png".format(slave_role["slug"])
                )
                export_cross_camera_plot(rows_for_slave, path, title)
