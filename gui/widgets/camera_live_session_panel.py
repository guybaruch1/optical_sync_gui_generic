"""One camera's own live-session VIEW (dual video panels, 3 charts, a live
stats sidebar, per-chart Copy/Export CSV, Save Debug Snapshot) - extracted
from gui/pages/live_session_page.py's single-camera LiveSessionPage so
gui/pages/multi_camera_live_session_page.py can show one of these per
configured camera (up to 3) inside a QTabWidget, alongside a shared
cross-camera metrics panel.

Deliberately does NOT own a SessionEngineThread, Start/Stop buttons, or a
duration/switch-time/frame-sample-interval toolbar - those are genuinely
RUN-level (span every camera together), not per-camera, so they live on the
new multi-camera page instead, which owns ONE
engine.multi_camera_session.MultiCameraSessionController driving every
camera's own (still completely unmodified) SessionEngineThread and relays
its per-camera-tagged signals into the matching panel's on_frame_ready/
on_row_ready/on_stats_ready/on_session_finished/on_error - the same
role LiveSessionPage's own engine_thread.*.connect(...) calls used to play,
just one level up. Everything else (video display, plotting, stats,
per-camera CSV/snapshot export) is a byte-for-byte port of LiveSessionPage's
own logic - see that file's module docstring for the full rationale behind
each piece (row_ready-vs-stats_ready cadence split, NaN-for-excluded
convention, combined side-by-side periodic snapshots, etc.), unchanged here.

Known v1 simplification (see docs/superpowers's multi-camera design doc's
suggested implementation order - this is step 5's scope, step 6's job to
improve): each panel still mints its OWN independent output/
live_session_<timestamp>/ folder via prepare_for_run(), exactly like
LiveSessionPage's single-camera _begin_new_run_output() always has - NOT
yet the nicer shared-parent-folder-with-per-camera-subfolders layout the
design doc describes for the finished feature."""

import os

import cv2
from PySide6.QtCore import QSize, QRectF, Qt
from PySide6.QtGui import QPainter, QPen, QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QFrame, QPushButton, QApplication, QScrollArea,
)

from gui.widgets.video_panel import VideoPanel
from gui.widgets.live_plot import LivePlot
from gui.widgets.stats_panel import StatsPanel
from domain.csv_export import export_session_csvs, export_series_csv
from domain.plot_export import export_session_plot
from domain.realsense_utils import draw_led_state_overlay, crop_to_roi, combine_side_by_side
from domain.running_stats import RunningStats
from domain.run_output import create_run_dir


def _build_copy_icon(color="#555555", size=18):
    # Identical to live_session_page.py's own helper - duplicated rather
    # than imported, since the two pages/widgets are meant to stay
    # independently readable (LiveSessionPage keeps working standalone for
    # anyone reading its own history) rather than sharing a private helper
    # across files for a few lines of drawing code.
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color))
    pen.setWidthF(1.4)

    back = QRectF(size * 0.12, size * 0.12, size * 0.58, size * 0.58)
    front = QRectF(size * 0.34, size * 0.34, size * 0.58, size * 0.58)

    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawRoundedRect(back, 2, 2)

    painter.setCompositionMode(QPainter.CompositionMode_Clear)
    painter.setPen(Qt.NoPen)
    painter.setBrush(Qt.black)
    painter.drawRoundedRect(front, 2, 2)

    painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawRoundedRect(front, 2, 2)
    painter.end()
    return QIcon(pixmap)


def _short_camera_name(camera_name):
    parts = camera_name.split()
    return parts[-1] if parts else camera_name


class CameraLiveSessionPanel(QWidget):
    def __init__(self, camera_id, parent=None):
        super().__init__(parent)
        self.camera_id = camera_id
        self._context = None
        self._stream_a_drop_count = 0
        self._stream_b_drop_count = 0
        self._stream_a_drop_since_last_plot = False
        self._stream_b_drop_since_last_plot = False
        self._last_stream_a_image = None
        self._last_stream_b_image = None
        self._last_stream_a_on_mask = None
        self._last_stream_b_on_mask = None
        self._periodic_snapshot_count = 0
        self._hw_ts_latency_stats = RunningStats()
        self._optical_sync_stats = RunningStats()
        self._last_session_rows = None

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
        self.stream_a_title_label = QLabel("Stream A")
        self.stream_b_title_label = QLabel("Stream B")
        for title_label in (self.stream_a_title_label, self.stream_b_title_label):
            title_label.setStyleSheet(
                "color: #555555; font-weight: 600; font-size: 9pt;"
                "text-transform: uppercase; letter-spacing: 1px; border: none; background: transparent;"
            )
        stream_a_column = QVBoxLayout()
        stream_a_column.addWidget(self.stream_a_title_label)
        stream_a_column.addWidget(self.stream_a_panel)
        stream_b_column = QVBoxLayout()
        stream_b_column.addWidget(self.stream_b_title_label)
        stream_b_column.addWidget(self.stream_b_panel)
        video_row.addLayout(stream_a_column)
        video_row.addLayout(stream_b_column)
        video_row.addStretch(1)
        layout.addLayout(video_row)

        self.pairing_gap_checkbox = QCheckBox("HW TS Latency (us)")
        self.pairing_gap_checkbox.setChecked(True)
        self.pairing_gap_checkbox.toggled.connect(
            lambda checked: self.pairing_plot.set_series_visible("pairing_gap_us", checked)
        )
        self.position_gap_checkbox = QCheckBox("Optical Sync (ms)")
        self.position_gap_checkbox.setChecked(True)
        self.position_gap_checkbox.toggled.connect(
            lambda checked: self.position_plot.set_series_visible("position_gap_ms", checked)
        )
        self.frame_drops_checkbox = QCheckBox("Frame drops (A/B)")
        self.frame_drops_checkbox.setChecked(True)
        self.frame_drops_checkbox.toggled.connect(self._set_frame_drops_visible)

        self.pairing_plot = LivePlot()
        self.pairing_plot.setLabel("left", "HW TS Latency (us)")
        self.pairing_plot.setLabel("bottom", "Pair Index")
        self.pairing_plot.add_series("pairing_gap_us", color="#4a7fe0", display_name="HW TS Latency (us)")

        self.position_plot = LivePlot()
        self.position_plot.setLabel("left", "Optical Sync (ms)")
        self.position_plot.setLabel("bottom", "Pair Index")
        self.position_plot.add_series("position_gap_ms", color="#3fbf9e", display_name="Optical Sync (ms)")

        self.drop_plot = LivePlot()
        self.drop_plot.setLabel("left", "Frame Drops (A up / B down)")
        self.drop_plot.setLabel("bottom", "Pair Index")
        self.drop_plot.add_series("stream_a_frame_drops", color="#e08a3f")
        self.drop_plot.add_series("stream_b_frame_drops", color="#c0587a")

        graphs_column = QVBoxLayout()
        graphs_column.addLayout(self._make_chart_header(self.pairing_gap_checkbox, self.pairing_plot,
                                                          ["pairing_gap_us"]))
        graphs_column.addWidget(self.pairing_plot, stretch=2)
        graphs_column.addLayout(self._make_chart_header(self.position_gap_checkbox, self.position_plot,
                                                          ["position_gap_ms"]))
        graphs_column.addWidget(self.position_plot, stretch=2)
        graphs_column.addLayout(self._make_chart_header(self.frame_drops_checkbox, self.drop_plot,
                                                          ["stream_a_frame_drops", "stream_b_frame_drops"]))
        graphs_column.addWidget(self.drop_plot, stretch=1)

        self.stats_panel = StatsPanel()
        self.stats_panel.setFixedWidth(220)
        self.stats_panel.add_section_header("Live Data")
        self.stats_panel.add_field("frame_index", "Frame Index")
        self.stats_panel.add_field("pairing_gap_us", "HW TS Latency (us)")
        self.stats_panel.add_field("position_gap_ms", "Optical Sync (ms)")
        self.stats_panel.add_field("switch_time_ms", "LED Switch Time (ms)")
        self.stats_panel.add_field("stream_a_frame_drops", "Stream A Frame Drops")
        self.stats_panel.add_field("stream_b_frame_drops", "Stream B Frame Drops")
        self.stats_panel.add_section_header("Stats")
        self.stats_panel.add_stats_table([
            ("hw_ts_latency", "HW TS Latency"),
            ("optical_sync", "Optical Sync"),
        ])

        middle_row = QHBoxLayout()
        middle_row.addLayout(graphs_column, stretch=1)
        middle_row.addWidget(self.stats_panel)
        layout.addLayout(middle_row)

        toolbar_frame = QFrame()
        toolbar_frame.setStyleSheet(
            "QFrame { background-color: #e9e7e1; border-top: 1px solid #d8d5cd; }"
        )
        control_row = QHBoxLayout(toolbar_frame)
        control_row.setContentsMargins(10, 8, 10, 8)
        control_row.addStretch(1)
        self.export_csv_button = QPushButton("Export CSV")
        self.export_csv_button.clicked.connect(self._reexport_last_session_csvs)
        self.save_debug_button = QPushButton("Save Debug Snapshot")
        self.save_debug_button.clicked.connect(self._save_led_state_debug_images)
        control_row.addWidget(self.export_csv_button)
        control_row.addWidget(self.save_debug_button)
        layout.addWidget(toolbar_frame)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

    def set_camera_labels(self, camera_name, stream_a_label, stream_b_label):
        short_name = _short_camera_name(camera_name)
        self.stream_a_title_label.setText("{} - {}".format(short_name, stream_a_label))
        self.stream_b_title_label.setText("{} - {}".format(short_name, stream_b_label))

    def _make_chart_header(self, checkbox, plot_widget, series_names):
        row = QHBoxLayout()
        row.addWidget(checkbox)
        row.addStretch(1)
        copy_button = QPushButton()
        copy_button.setIcon(_build_copy_icon())
        copy_button.setIconSize(QSize(14, 14))
        copy_button.setFixedSize(24, 24)
        copy_button.setToolTip("Copy chart as image")
        copy_button.clicked.connect(lambda: self._copy_chart_image(plot_widget))
        export_button = QPushButton("Export CSV")
        export_button.clicked.connect(lambda: self._export_chart_csv(plot_widget, series_names))
        row.addWidget(copy_button)
        row.addWidget(export_button)
        return row

    def _copy_chart_image(self, plot_widget):
        QApplication.clipboard().setPixmap(plot_widget.grab())
        self.status_label.setText("Chart copied to clipboard as an image.")

    def _save_chart_images(self, output_dir):
        chart_files = {
            self.pairing_plot: "hw_ts_latency_chart.png",
            self.position_plot: "optical_sync_chart.png",
            self.drop_plot: "frame_drops_chart.png",
        }
        for plot_widget, filename in chart_files.items():
            plot_widget.grab().save(os.path.join(output_dir, filename), "PNG")

    def _export_chart_csv(self, plot_widget, series_names):
        if self._context is None:
            self.status_label.setText("No session data yet - click Start first.")
            return
        x_values, _ = plot_widget.get_series_data(series_names[0])
        if not x_values:
            self.status_label.setText("No chart data yet - wait a moment after Start.")
            return
        series_y_by_name = {name: plot_widget.get_series_data(name)[1] for name in series_names}
        path = os.path.join(self._context["output_dir"], "{}_chart_export.csv".format(series_names[0]))
        export_series_csv(path, x_values, series_y_by_name)
        self.status_label.setText("Exported chart CSV: {}".format(path))

    def _reexport_last_session_csvs(self):
        if self._last_session_rows is None:
            self.status_label.setText("No completed session yet - run Start then Stop first.")
            return
        export_session_csvs(
            self._last_session_rows, self._context["kept_csv_path"], self._context["dropped_csv_path"]
        )
        self.status_label.setText(
            "Re-exported CSVs: {}, {}".format(self._context["kept_csv_path"], self._context["dropped_csv_path"])
        )

    def _set_frame_drops_visible(self, checked):
        self.drop_plot.set_series_visible("stream_a_frame_drops", checked)
        self.drop_plot.set_series_visible("stream_b_frame_drops", checked)

    def prepare_for_run(self, output_root, kept_csv_filename, dropped_csv_filename,
                         stream_a_xy, stream_b_xy, stream_a_roi, stream_b_roi,
                         snapshot_every_n_pairs, max_snapshots, switch_time_ms, run_output_suffix=None):
        """Mints THIS camera's own fresh output/live_session_<timestamp>/
        folder (see module docstring's "known v1 simplification"), resets
        every plot/counter/running-stat exactly like LiveSessionPage's own
        start_session() reset block used to (minus anything
        SessionEngineThread-related, now built by the caller instead), and
        shows the run's switch time. Returns the output_dir, so the caller
        can pass it straight into that camera's own SessionEngineThread
        construction."""
        self._context = dict(
            stream_a_xy=stream_a_xy, stream_b_xy=stream_b_xy,
            stream_a_roi=stream_a_roi, stream_b_roi=stream_b_roi,
            snapshot_every_n_pairs=snapshot_every_n_pairs, max_snapshots=max_snapshots,
            kept_csv_filename=kept_csv_filename, dropped_csv_filename=dropped_csv_filename,
        )
        output_dir = create_run_dir(output_root, "live_session", suffix=run_output_suffix)
        self._context["output_dir"] = output_dir
        self._context["kept_csv_path"] = os.path.join(output_dir, kept_csv_filename)
        self._context["dropped_csv_path"] = os.path.join(output_dir, dropped_csv_filename)

        self.pairing_plot.clear_data()
        self.position_plot.clear_data()
        self.drop_plot.clear_data()
        self._stream_a_drop_count = 0
        self._stream_b_drop_count = 0
        self._stream_a_drop_since_last_plot = False
        self._stream_b_drop_since_last_plot = False
        self._last_stream_a_image = None
        self._last_stream_b_image = None
        self._last_stream_a_on_mask = None
        self._last_stream_b_on_mask = None
        self._periodic_snapshot_count = 0
        self._hw_ts_latency_stats = RunningStats()
        self._optical_sync_stats = RunningStats()
        self._clear_periodic_snapshots(output_dir)
        self.stats_panel.set_value("switch_time_ms", switch_time_ms)
        self.status_label.setText("")
        return output_dir

    def on_frame_ready(self, stream_name, image, pair_index, on_mask):
        if stream_name == "stream_a":
            self._last_stream_a_image = image
            self._last_stream_a_on_mask = on_mask
        else:
            self._last_stream_b_image = image
            self._last_stream_b_on_mask = on_mask

        display_image = draw_led_state_overlay(image, self._overlay_xy(stream_name), on_mask) \
            if on_mask is not None and self._context is not None else image
        display_image = self._crop_to_roi_if_available(display_image, stream_name)
        if stream_name == "stream_a":
            self.stream_a_panel.set_frame(display_image)
        else:
            self.stream_b_panel.set_frame(display_image)
            self._maybe_save_periodic_snapshot(pair_index)

    def _crop_to_roi_if_available(self, image, stream_name):
        if self._context is None:
            return image
        roi = self._context["stream_a_roi"] if stream_name == "stream_a" else self._context["stream_b_roi"]
        if roi is None or roi[2] <= 0 or roi[3] <= 0:
            return image
        return crop_to_roi(image, roi)

    def _overlay_xy(self, stream_name):
        return self._context["stream_a_xy"] if stream_name == "stream_a" else self._context["stream_b_xy"]

    def _maybe_save_periodic_snapshot(self, pair_index):
        if self._context is None:
            return
        every_n = self._context["snapshot_every_n_pairs"]
        max_snapshots = self._context["max_snapshots"]
        if every_n <= 0 or pair_index % every_n != 0:
            return
        if self._periodic_snapshot_count >= max_snapshots:
            return
        if self._last_stream_a_on_mask is None or self._last_stream_b_on_mask is None:
            return
        if self._last_stream_a_image is None or self._last_stream_b_image is None:
            return

        output_dir = self._context["output_dir"]
        combined_path = os.path.join(output_dir, "periodic_led_state_pair{:05d}.png".format(pair_index))
        stream_a_debug = draw_led_state_overlay(
            self._last_stream_a_image, self._context["stream_a_xy"], self._last_stream_a_on_mask
        )
        stream_b_debug = draw_led_state_overlay(
            self._last_stream_b_image, self._context["stream_b_xy"], self._last_stream_b_on_mask
        )
        cv2.imwrite(combined_path, combine_side_by_side(stream_a_debug, stream_b_debug))
        self._periodic_snapshot_count += 1

    def _clear_periodic_snapshots(self, output_dir):
        import glob
        for path in glob.glob(os.path.join(output_dir, "periodic_led_state_*.png")):
            os.remove(path)

    def on_row_ready(self, row):
        if row.get("stream_a_frame_drop"):
            self._stream_a_drop_count += 1
            self._stream_a_drop_since_last_plot = True
        if row.get("stream_b_frame_drop"):
            self._stream_b_drop_count += 1
            self._stream_b_drop_since_last_plot = True

        if row.get("pairing_gap_us") is not None and not row.get("pairing_gap_us_excluded"):
            self._hw_ts_latency_stats.update(row["pairing_gap_us"])
        if row.get("position_gap_ms") is not None and not row.get("position_gap_ms_excluded"):
            self._optical_sync_stats.update(row["position_gap_ms"])

    def on_stats_ready(self, stats):
        pair_index = stats["pair_index"]
        self.stats_panel.set_value("frame_index", pair_index)

        if stats.get("pairing_gap_us") is not None:
            self.stats_panel.set_value("pairing_gap_us", stats["pairing_gap_us"])
            pairing_value = stats["pairing_gap_us"] if not stats.get("pairing_gap_us_excluded") else float("nan")
            self.pairing_plot.add_point("pairing_gap_us", pair_index, pairing_value)
        if stats.get("position_gap_ms") is not None:
            self.stats_panel.set_value("position_gap_ms", stats["position_gap_ms"])
            position_value = stats["position_gap_ms"] if not stats.get("position_gap_ms_excluded") else float("nan")
            self.position_plot.add_point("position_gap_ms", pair_index, position_value)

        self.stats_panel.set_value("stream_a_frame_drops", self._stream_a_drop_count)
        self.stats_panel.set_value("stream_b_frame_drops", self._stream_b_drop_count)
        self.drop_plot.add_point(
            "stream_a_frame_drops", pair_index, 1 if self._stream_a_drop_since_last_plot else 0
        )
        self.drop_plot.add_point(
            "stream_b_frame_drops", pair_index, -1 if self._stream_b_drop_since_last_plot else 0
        )
        self._stream_a_drop_since_last_plot = False
        self._stream_b_drop_since_last_plot = False

        self._push_running_stats("hw_ts_latency", self._hw_ts_latency_stats)
        self._push_running_stats("optical_sync", self._optical_sync_stats)

    def _push_running_stats(self, key, stats):
        if stats.count == 0:
            return
        self.stats_panel.set_value("{}_min".format(key), round(stats.min, 1))
        self.stats_panel.set_value("{}_avg".format(key), round(stats.mean, 1))
        self.stats_panel.set_value("{}_std".format(key), round(stats.std, 1))
        self.stats_panel.set_value("{}_max".format(key), round(stats.max, 1))

    def on_session_finished(self, rows):
        self._last_session_rows = rows
        export_session_csvs(rows, self._context["kept_csv_path"], self._context["dropped_csv_path"])
        export_session_plot(rows, os.path.join(self._context["output_dir"], "pipeline_sync_plot.png"))
        self._save_chart_images(self._context["output_dir"])
        self._save_led_state_debug_images()

    def _save_led_state_debug_images(self):
        if self._context is None:
            self.status_label.setText("No active session - click Start first.")
            return
        if self._last_stream_a_on_mask is None or self._last_stream_b_on_mask is None:
            self.status_label.setText("No frame data yet - wait a moment after Start and try again.")
            return
        if self._last_stream_a_image is None or self._last_stream_b_image is None:
            self.status_label.setText("No frame data yet - wait a moment after Start and try again.")
            return

        output_dir = self._context["output_dir"]
        stream_a_path = os.path.join(output_dir, "live_led_state_stream_a.png")
        stream_b_path = os.path.join(output_dir, "live_led_state_stream_b.png")
        try:
            stream_a_debug = draw_led_state_overlay(
                self._last_stream_a_image, self._context["stream_a_xy"], self._last_stream_a_on_mask
            )
            stream_b_debug = draw_led_state_overlay(
                self._last_stream_b_image, self._context["stream_b_xy"], self._last_stream_b_on_mask
            )
            stream_a_ok = cv2.imwrite(stream_a_path, stream_a_debug)
            stream_b_ok = cv2.imwrite(stream_b_path, stream_b_debug)
        except Exception as exc:
            self.status_label.setText("Failed to save debug snapshot: {}".format(exc))
            return

        if stream_a_ok and stream_b_ok:
            self.status_label.setText("Saved debug snapshot: {}, {}".format(stream_a_path, stream_b_path))
        else:
            self.status_label.setText("Failed to write one or both debug snapshot files to {}".format(output_dir))

    def on_error(self, message):
        self.status_label.setText("Error: {}".format(message))
