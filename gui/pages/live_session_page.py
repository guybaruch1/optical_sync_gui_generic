"""Wizard step 6 (final) - the live sync-test view: dual video panels (each
showing a live LED on/off detection overlay), three live plots (HW TS
latency, optical sync, frame drops), a live stats sidebar, and Start/Stop
with an optional fixed duration, LED switch time, and frame sample
interval (display_stride) - all three read live from the toolbar at
Start, not from settings.yaml/ctx, and locked (setEnabled(False)) for the
duration of a run so a change can't misleadingly appear to apply to an
already-running thread. None of the three are persisted anywhere, so a
fresh app launch always starts back from settings.yaml's/the hardcoded
defaults, never whatever was last typed. The on/off threshold itself is no
longer tuned here at all - that now happens, per-stream, on the
Threshold Tuning page (gui/pages/threshold_tuning_page.py) immediately
before this one; set_context() receives the already-final
stream_a_threshold/stream_b_threshold arrays. Saves periodic LED on/off debug
snapshots during the run (every settings.yaml test.snapshot_every_n_pairs
pairs, capped at test.max_snapshots per stream) as ONE combined image per
pair - both streams' overlays side by side (domain.realsense_utils.
combine_side_by_side), not two separate stream_a/stream_b files, so they're
cross-checkable in a single glance/file. Filename includes the pair_index
so it can be cross-checked against what was on screen and against the
CSV's pair_index column. At Stop, writes the CSVs
(domain.csv_export.export_session_csvs), a static end-of-run plot image
(domain.plot_export.export_session_plot), PNG snapshots of the 3 live
charts themselves (_save_chart_images), and one final LED on/off debug
snapshot for each stream - the same final snapshot can also be saved on
demand mid-session via the "Save Debug Snapshot" button.

Every Start click mints its OWN fresh timestamped
output/live_session_<timestamp>/ folder (domain.run_output.create_run_dir,
via _begin_new_run_output(), called first thing in start_session()) - a new
run never overwrites a previous run's CSVs/graphs/snapshots the way one
flat, fixed-filename output directory used to. The toolbar's "Export CSV"
button and the periodic-snapshot/debug-image helpers below all read
self._context["output_dir"]/["kept_csv_path"]/["dropped_csv_path"], which
_begin_new_run_output() sets once per Start and which then stay pointed at
that same run's folder until the next Start - so re-exporting mid-session
or right after Stop still targets the run that's actually in progress or
just finished, not a new folder.

"HW TS Latency" and "Optical Sync" are the user-facing names for the
underlying pairing_gap_us/position_gap_ms metrics (engine.metrics) - the
data/series/dict keys stay as pairing_gap_us/position_gap_ms throughout
(CSV columns, stats_panel field keys, LivePlot series keys); only the
displayed labels (checkboxes, axis titles, legend, stat tiles) use the
renamed terms, via LivePlot.add_series's display_name param.

pairing_gap_us and position_gap_ms each get their own single-axis plot,
not one dual-axis chart sharing left/right y-axes - the dataviz skill's
non-negotiables flag dual-axis charts as a genuine readability
anti-pattern (the alignment of two independently-scaled measures is
arbitrary and can suggest a correlation that isn't in the data), not
just a rendering-reliability risk. An earlier dual-axis attempt (for
pairing gap vs. frame drops, then again for pairing gap vs. position
gap) had also had a real rendering bug at one point - see git history
of gui/widgets/dual_axis_live_plot.py (deleted) if that widget is ever
needed again.

Visual layout/styling (page background, per-chart header rows, stat
tiles, chart colors, toolbar) matches the design mockup from the
claude.ai/design project "GUI layout redesign options"
(file "Optical Sync GUI.dc.html") - imported via the DesignSync MCP tool.
The per-chart "Copy" button copies that chart as an image to the
clipboard; the per-chart "Export CSV" button writes that chart's own
plotted series (domain.csv_export.export_series_csv) to output_dir; the
toolbar's "Export CSV" button re-writes the last completed session's CSVs
(the same rows _on_session_finished already wrote once at Stop); the
"Stats" section's min/avg/std/max table is backed by
domain.running_stats.RunningStats, updated every pair (same cadence as the
frame-drop counters) and pushed to the table on the same throttled cadence
the live plots update on. The frame-drops checkbox reuses
LivePlot.set_series_visible - the same mechanism the other two checkboxes
use.

This page is stream-agnostic: "stream_a"/"stream_b" refer to whichever
two (stream_type, stream_index) picks the wizard's earlier pages resolved
(e.g. an infrared stream and a color stream, or two infrared streams) -
nothing here assumes a specific RealSense stream type. Panel titles use
stream_a_label/stream_b_label (human-readable strings computed by the
caller, e.g. "Infrared 1"/"Color") rather than a hardcoded "IR"/"RGB"."""

import glob
import os

import cv2
from PySide6.QtCore import Qt, QSize, QRectF
from PySide6.QtGui import QIcon, QPixmap, QPainter, QPen, QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QSpinBox, QDoubleSpinBox, QLabel, QCheckBox, QFrame,
    QApplication, QScrollArea,
)

from gui.widgets.video_panel import VideoPanel
from gui.widgets.live_plot import LivePlot
from gui.widgets.stats_panel import StatsPanel
from engine.session_engine import SessionEngineThread
from engine.test_session import TestSession, TestSessionConfig
from engine.metrics import PairingGapMetric, PositionGapMetric
from domain.csv_export import export_session_csvs, export_series_csv
from domain.plot_export import export_session_plot
from domain.realsense_utils import draw_led_state_overlay, crop_to_roi, combine_side_by_side
from domain.running_stats import RunningStats
from domain.run_output import create_run_dir, build_live_session_config_suffix


def _build_copy_icon(color="#555555", size=18):
    # Drawn in code, not a Unicode symbol glyph (e.g. the earlier "⧉") -
    # the previous glyph wasn't in the default Windows UI font and
    # rendered as a blank box. A painted icon looks identical on every
    # system regardless of font glyph coverage. Two overlapping rounded
    # squares - the standard "copy" icon shape - with the back square's
    # corner erased under the front one so it reads as layered, not two
    # crossing outlines.
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
    # Device names from pyrealsense2 are consistently "Intel RealSense
    # <model>" (e.g. "Intel RealSense D455") - the model designator is
    # what's actually useful in a compact video panel title, not the
    # vendor prefix repeated on both panels.
    parts = camera_name.split()
    return parts[-1] if parts else camera_name


class LiveSessionPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.engine_thread = None
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
        # The switch-time value last acknowledged via Confirm - now a REAL
        # gate (see _update_confirm_switch_time_button_state): start_session()
        # reads THIS, not the spinbox directly, so an unconfirmed edit is
        # never silently used. None until set_context() first prefills the
        # spinbox (together with this).
        self._last_confirmed_switch_time_ms = None
        # True for the entire span between start_session() and
        # _on_engine_thread_finished() - start_button's own enabled state is
        # the AND of "not currently running" and "no pending unconfirmed
        # switch-time edit" (see _update_confirm_switch_time_button_state).
        self._session_running = False

        # The page's real content lives in content_widget/layout, inside a
        # QScrollArea - not directly in self. Without this, a lower-
        # resolution screen that can't fit two video panels + three charts +
        # the toolbar at once had no way to reach whatever didn't fit: Qt
        # just clipped/compressed everything down to (and past) each
        # widget's minimum size with no scrollbar to recover the rest,
        # which is what produced overlapping/garbled stat-tile text and a
        # chart cut off past the window's visible bottom edge.
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
        # model name + stream label (e.g. "D455 - Infrared 1") - stream
        # identity isn't known until then.
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

        # Colors match the design mockup's chart lines (blue/aqua/orange).
        # stream_b_frame_drops' color is my own choice - the mockup
        # simplifies frame drops to one line, but this app genuinely tracks
        # the two streams separately (see the module docstring), so it
        # still needs two distinct, harmonious colors.
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
        # Split by stream (not one combined flag) so you can see which
        # stream is actually dropping frames, not just that one recently
        # did - the data was already split (stream_a_frame_drop/
        # stream_b_frame_drop), only the graph collapsed it into one series.
        self.drop_plot.add_series("stream_a_frame_drops", color="#e08a3f")
        self.drop_plot.add_series("stream_b_frame_drops", color="#c0587a")

        # Each graph gets its own header row (checkbox + Copy/Export CSV)
        # directly above it, all three in one column with equal width, but
        # the frame-drops graph gets half the height of the other two -
        # it's a simpler 0/1 signal that doesn't need as much vertical
        # room, matching the design mockup.
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
        control_row.addWidget(QLabel("Duration (s, 0 = manual stop):"))
        self.duration_spinbox = QSpinBox()
        self.duration_spinbox.setRange(0, 3600)
        control_row.addWidget(self.duration_spinbox)
        self.start_button = QPushButton("Start")
        self.start_button.setStyleSheet(
            "QPushButton { background-color: #2f6fed; color: white; border: 1px solid #2f6fed;"
            " border-radius: 4px; padding: 5px 14px; }"
            # Setting an explicit background-color above opts this button out
            # of Qt's automatic disabled/greyed palette - without a :disabled
            # rule of its own, setEnabled(False) still blocks clicks but the
            # button stays the same solid blue, looking just as clickable as
            # when it's actually safe to click.
            "QPushButton:disabled { background-color: #b7c7f0; color: #eef2fc; border: 1px solid #b7c7f0; }"
        )
        self.start_button.clicked.connect(self.start_session)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop_session)
        self.stop_button.setEnabled(False)
        control_row.addWidget(self.start_button)
        control_row.addWidget(self.stop_button)

        control_row.addWidget(QLabel("LED Switch Time (ms):"))
        self.switch_time_spinbox = QDoubleSpinBox()
        # Floor of 0.1, not 0 - a switch time of exactly 0 isn't physically
        # meaningful, and 0.1ms is the finest step LEDPanel.set_speed_ms's
        # own "--setTime {:.4f}" (4 decimal places of SECONDS) can actually
        # represent - engine/led_panel.py, offering more decimals here would
        # just suggest false precision the hardware command can't honor.
        self.switch_time_spinbox.setRange(0.1, 10000.0)
        self.switch_time_spinbox.setDecimals(1)
        self.switch_time_spinbox.setSingleStep(0.5)
        # Overridden with the settings.yaml default in set_context(); kept
        # editable per-run (like duration) so switch speed can be tuned
        # without hand-editing settings.yaml between runs.
        self.switch_time_spinbox.setValue(1.0)
        # valueChanged only toggles Confirm's (and, transitively, Start's)
        # enabled state here - no hardware call happens from typing into
        # the box. Unlike gui/pages/threshold_tuning_page.py's own Confirm
        # button (which applies a new speed live, to an already-running LED
        # panel), there is no live-apply step here: this page's
        # start_session() reads self._last_confirmed_switch_time_ms, and
        # Confirm is what advances that value - an edit sitting unconfirmed
        # in the box is never used for a run, and start_button itself stays
        # disabled while one is pending (see
        # _update_confirm_switch_time_button_state).
        self.switch_time_spinbox.valueChanged.connect(self._on_switch_time_spinbox_changed)
        control_row.addWidget(self.switch_time_spinbox)
        self.confirm_switch_time_button = QPushButton("Confirm")
        self.confirm_switch_time_button.setEnabled(False)
        self.confirm_switch_time_button.setToolTip(
            "Confirm the LED Switch Time above. start_session() reads the last confirmed "
            "value, not the box's raw current value - an unconfirmed edit is never used for "
            "a run, and Start stays disabled until you confirm or revert it."
        )
        self.confirm_switch_time_button.clicked.connect(self._on_confirm_switch_time_clicked)
        control_row.addWidget(self.confirm_switch_time_button)

        control_row.addWidget(QLabel("Frame Sample Interval:"))
        self.frame_sample_interval_spinbox = QSpinBox()
        self.frame_sample_interval_spinbox.setRange(1, 2000)
        # Matches AcquisitionLoop/SessionEngineThread's own display_stride
        # default - how many frame-pairs between video-panel and live-plot
        # updates. Every pair is still processed/metriced/recorded either
        # way; this only throttles how often the GUI actually redraws.
        self.frame_sample_interval_spinbox.setValue(10)
        self.frame_sample_interval_spinbox.setToolTip(
            "Frame-pairs between video/plot updates (every pair is still recorded). "
            "1 updates on every frame and can freeze the GUI - see the warning below the field."
        )
        control_row.addWidget(self.frame_sample_interval_spinbox)
        # Non-blocking, live warning (not a raised minimum, not a blocking
        # dialog) - 1 defeats the whole point of display_stride throttling
        # (every frame triggers the expensive video/plot-update callbacks
        # instead of every 10th), reproducing the exact "unbounded backlog
        # of queued cross-thread Qt signal work" freeze _on_row_ready's own
        # docstring describes as the reason that throttling exists at all.
        # Still a legal, working value for whoever genuinely wants it -
        # this only makes the risk visible the moment it's selected.
        self.frame_sample_interval_warning_label = QLabel("")
        self.frame_sample_interval_warning_label.setStyleSheet("QLabel { color: #b00020; }")
        self.frame_sample_interval_spinbox.valueChanged.connect(self._on_frame_sample_interval_changed)
        control_row.addWidget(self.frame_sample_interval_warning_label)

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
        # Auto-saves the same grab() pixmap _copy_chart_image already
        # produces for the clipboard, one PNG per live chart, into this
        # run's own output folder - previously the only way to get these
        # onto disk was manually clicking "Copy" per chart and pasting
        # elsewhere; nothing saved them as files automatically.
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

    def _on_frame_sample_interval_changed(self, value):
        # 1 is still a legal, working value (see the toolbar setup comment) -
        # this only surfaces the risk the moment it's selected, live,
        # without blocking or forcing a different value.
        if value == 1:
            self.frame_sample_interval_warning_label.setText(
                "⚠ 1 updates on every frame - may freeze the GUI"
            )
        else:
            self.frame_sample_interval_warning_label.setText("")

    def set_context(self, ctx, device_serial, pick_a, pick_b, camera_controls, switch_time_ms, scan_direction,
                     stream_a_threshold, stream_b_threshold,
                     stream_a_xy, stream_b_xy, num_leds, neighborhood_size,
                     frame_drop_threshold_factor, warmup_pairs_to_skip, pairing_gap_outlier_threshold_us,
                     position_gap_outlier_threshold_ms, position_gap_outlier_max_snapshots,
                     output_root, kept_csv_filename, dropped_csv_filename, snapshot_every_n_pairs, max_snapshots,
                     stream_a_roi, stream_b_roi, camera_name, stream_a_label, stream_b_label,
                     dual_panel_config=None, enable_depth_for_ir_sync=True,
                     hardware_reset_before_start=False, hardware_reset_settle_s=8.0):
        self._context = dict(
            ctx=ctx, device_serial=device_serial, pick_a=pick_a, pick_b=pick_b, camera_controls=camera_controls,
            switch_time_ms=switch_time_ms, scan_direction=scan_direction,
            # Final, already-tuned per-LED threshold arrays - tuning itself
            # (per-stream, with a live detection preview) already happened
            # on the Threshold Tuning wizard page before this one, so
            # there's nothing left to recompute here.
            stream_a_threshold=stream_a_threshold, stream_b_threshold=stream_b_threshold,
            stream_a_xy=stream_a_xy, stream_b_xy=stream_b_xy,
            num_leds=num_leds, neighborhood_size=neighborhood_size,
            frame_drop_threshold_factor=frame_drop_threshold_factor,
            warmup_pairs_to_skip=warmup_pairs_to_skip,
            pairing_gap_outlier_threshold_us=pairing_gap_outlier_threshold_us,
            position_gap_outlier_threshold_ms=position_gap_outlier_threshold_ms,
            position_gap_outlier_max_snapshots=position_gap_outlier_max_snapshots,
            # Raw root + filename templates, not a pre-joined output_dir/
            # kept_csv_path/dropped_csv_path - start_session() mints a fresh
            # run folder (_begin_new_run_output) and joins these on every
            # Start, so a new run never overwrites a previous one's files.
            output_root=output_root, kept_csv_filename=kept_csv_filename,
            dropped_csv_filename=dropped_csv_filename,
            snapshot_every_n_pairs=snapshot_every_n_pairs, max_snapshots=max_snapshots,
            stream_a_roi=stream_a_roi, stream_b_roi=stream_b_roi,
            stream_a_label=stream_a_label, stream_b_label=stream_b_label,
            dual_panel_config=dual_panel_config,
            # settings.yaml camera_sync: - see that section's own comment for
            # what each does and why they exist.
            enable_depth_for_ir_sync=enable_depth_for_ir_sync,
            hardware_reset_before_start=hardware_reset_before_start,
            hardware_reset_settle_s=hardware_reset_settle_s,
        )
        self.stats_panel.set_value("switch_time_ms", switch_time_ms)
        # settings.yaml's value is only the starting point shown in the
        # toolbar - the spinbox itself (read in start_session(), not
        # ctx["switch_time_ms"]) is what a run actually uses, so it can be
        # tuned per-run without hand-editing settings.yaml. Not persisted
        # anywhere, so a fresh app launch always starts back from this
        # settings.yaml default, not whatever was last typed. float(), not
        # int(round(...)) - the incoming value can already be fractional
        # (e.g. tuned to 0.5 on Threshold Tuning), and truncating it here
        # would silently throw that precision away.
        #
        # Set BEFORE setValue() below, not after - this IS the prefilled
        # value (nothing to confirm yet), and setValue() won't fire
        # valueChanged if the spinbox already happens to hold this same
        # value (e.g. revisiting this page with an unchanged camera) - the
        # explicit refresh call right after covers exactly that case.
        self._last_confirmed_switch_time_ms = float(switch_time_ms)
        self.switch_time_spinbox.setValue(float(switch_time_ms))
        self._update_confirm_switch_time_button_state()
        short_name = _short_camera_name(camera_name)
        self.stream_a_title_label.setText("{} - {}".format(short_name, stream_a_label))
        self.stream_b_title_label.setText("{} - {}".format(short_name, stream_b_label))

    def _begin_new_run_output(self, suffix=None):
        # Mints a fresh timestamped output/live_session_<timestamp>[_<config
        # suffix>]/ folder for THIS run and joins the CSV filename templates
        # onto it - called from start_session() (after the toolbar values
        # the suffix is built from are already read) so every other
        # output_dir/kept_csv_path/dropped_csv_path reader below (and
        # everything _on_session_finished/_export_chart_csv/
        # _save_led_state_debug_images/_reexport_last_session_csvs read
        # later) already sees this run's own folder, not a shared/overwritten
        # one from a previous Start click.
        ctx = self._context
        output_dir = create_run_dir(ctx["output_root"], "live_session", suffix=suffix)
        ctx["output_dir"] = output_dir
        ctx["kept_csv_path"] = os.path.join(output_dir, ctx["kept_csv_filename"])
        ctx["dropped_csv_path"] = os.path.join(output_dir, ctx["dropped_csv_filename"])
        return output_dir

    def start_session(self):
        ctx = self._context
        duration_s = self.duration_spinbox.value() or None
        # Read the last CONFIRMED value, not whatever the spinbox currently
        # shows - Confirm is a real gate now (see
        # _update_confirm_switch_time_button_state): an edit sitting
        # unconfirmed in the box must never silently be used for a run, and
        # Start itself stays disabled while one is pending anyway. Used for
        # BOTH the metric's math and the LED panel's actual scan speed
        # (below) - they must agree, or position_gap_ms would be computed
        # against a switch time the panel wasn't really using.
        switch_time_ms = self._last_confirmed_switch_time_ms
        display_stride = self.frame_sample_interval_spinbox.value()
        # Read BEFORE _begin_new_run_output() (unlike before this feature
        # existed) - the output folder's own name now encodes these three
        # plus the camera pick/exposure mode, so they all have to be known
        # first. Nothing between the old call site and here reads
        # ctx["output_dir"], so this reordering is safe.
        run_output_suffix = build_live_session_config_suffix(
            width=ctx["pick_a"]["width"], height=ctx["pick_a"]["height"], fps=ctx["pick_a"]["fps"],
            duration_s=duration_s,
            auto_exposure=ctx["camera_controls"]["auto_exposure"],
            exposure_a=ctx["camera_controls"]["exposure_a"], exposure_b=ctx["camera_controls"]["exposure_b"],
            display_stride=display_stride, switch_time_ms=switch_time_ms,
        )
        self._begin_new_run_output(suffix=run_output_suffix)
        position_gap_metric = PositionGapMetric(
            stream_a_threshold=ctx["stream_a_threshold"], stream_b_threshold=ctx["stream_b_threshold"],
            num_leds=ctx["num_leds"], switch_time_ms=switch_time_ms,
            warmup_pairs_to_skip=ctx["warmup_pairs_to_skip"],
        )
        metrics = [
            PairingGapMetric(outlier_threshold_us=ctx["pairing_gap_outlier_threshold_us"]),
            position_gap_metric,
        ]
        test_session = TestSession(TestSessionConfig(
            metrics=metrics, duration_s=duration_s,
            stream_a_fps=ctx["pick_a"]["fps"], stream_b_fps=ctx["pick_b"]["fps"],
            frame_drop_threshold_factor=ctx["frame_drop_threshold_factor"],
        ))
        test_session.start()

        # A new session's pair_index restarts at 0 - without clearing, its
        # points would draw right on top of/alongside whatever the previous
        # session left on these graphs, and any manual zoom/pan from the
        # previous session would carry over too (clear() also resets to
        # auto-range).
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
        self._clear_periodic_snapshots(ctx["output_dir"])

        if self.engine_thread is not None:
            # Defense-in-depth: the Start button shouldn't be clickable
            # again until _on_engine_thread_finished has already fired (see
            # below), so this should return immediately. But if it somehow
            # isn't done yet, block until it is rather than let a second
            # ContinuousCapture/LEDPanel session race the first one for the
            # same physical camera - that race is what caused
            # "QThread: Destroyed while thread '' is still running" and the
            # crash/freeze it led to.
            self.engine_thread.wait()

        self.engine_thread = SessionEngineThread(
            ctx["ctx"], ctx["device_serial"], ctx["pick_a"], ctx["pick_b"], ctx["camera_controls"], test_session,
            stream_a_xy=ctx["stream_a_xy"], stream_b_xy=ctx["stream_b_xy"], neighborhood_size=ctx["neighborhood_size"],
            scan_direction=ctx["scan_direction"], switch_time_ms=switch_time_ms,
            display_stride=display_stride, position_gap_metric=position_gap_metric,
            dual_panel_config=ctx["dual_panel_config"],
            enable_depth_for_ir_sync=ctx["enable_depth_for_ir_sync"],
            hardware_reset_before_start=ctx["hardware_reset_before_start"],
            hardware_reset_settle_s=ctx["hardware_reset_settle_s"],
            output_dir=ctx["output_dir"],
            position_gap_outlier_threshold_ms=ctx["position_gap_outlier_threshold_ms"],
            position_gap_outlier_max_snapshots=ctx["position_gap_outlier_max_snapshots"],
        )
        self.engine_thread.frame_ready.connect(self._on_frame_ready)
        self.engine_thread.row_ready.connect(self._on_row_ready)
        self.engine_thread.stats_ready.connect(self._on_stats_ready)
        self.engine_thread.session_finished.connect(self._on_session_finished)
        self.engine_thread.error.connect(self._on_error)
        # QThread's own finished signal - unlike session_finished/error
        # (emitted inside SessionEngineThread.run()'s try block), this only
        # fires once run() has fully returned, including its finally block
        # (stopping the camera pipeline, stopping the LED panel). Gating
        # "Start is clickable again" on this, not on session_finished/error,
        # is the actual fix - re-enabling Start any earlier let a new
        # session's camera/LED-panel calls race the old thread's still-running
        # cleanup for the same physical hardware.
        self.engine_thread.finished.connect(self._on_engine_thread_finished)
        self.engine_thread.start()

        self._session_running = True
        self.status_label.setText("")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        # Changing any of these mid-run wouldn't retroactively apply to the
        # thread that already started with the values read above, and would
        # misleadingly suggest it did - lock them for the same span Start
        # itself is locked (re-enabled together in _on_engine_thread_finished).
        self.duration_spinbox.setEnabled(False)
        self.switch_time_spinbox.setEnabled(False)
        self.confirm_switch_time_button.setEnabled(False)
        self.frame_sample_interval_spinbox.setEnabled(False)

    def stop_session(self):
        if self.engine_thread is not None:
            self.engine_thread.request_stop()

    def _on_switch_time_spinbox_changed(self, value):
        # Pure UI state - matches gui/pages/threshold_tuning_page.py's own
        # identically-named handler, but there is no hardware call to gate
        # here at all - start_session() always reads the spinbox's current
        # value fresh regardless of this button. See switch_time_spinbox's
        # own setup comment for why this exists purely for UI parity.
        self._update_confirm_switch_time_button_state()

    def _update_confirm_switch_time_button_state(self):
        unconfirmed = self.switch_time_spinbox.value() != self._last_confirmed_switch_time_ms
        self.confirm_switch_time_button.setEnabled(unconfirmed)
        # Not gated while a session is running - start_session()'s own lock
        # (setEnabled(False)) already owns start_button for that span; this
        # only decides start_button's state for the "not currently running"
        # span, where it's otherwise always available.
        if not self._session_running:
            self.start_button.setEnabled(not unconfirmed)

    def _on_confirm_switch_time_clicked(self):
        # No hardware call - purely acknowledges the current value so
        # Confirm disables itself again, matching Threshold Tuning's own
        # Confirm button's visual behavior.
        self._last_confirmed_switch_time_ms = self.switch_time_spinbox.value()
        self._update_confirm_switch_time_button_state()

    def _on_frame_ready(self, stream_name, image, pair_index, on_mask):
        # image and on_mask arrive together, already paired correctly by
        # SessionEngineThread (a snapshot copy taken on the background
        # thread at this exact pair_index) - this method must not read any
        # live/mutable state to recover the mask itself, only use what was
        # handed to it, or the same stale-read bug comes right back.
        if stream_name == "stream_a":
            self._last_stream_a_image = image
            self._last_stream_a_on_mask = on_mask
        else:
            self._last_stream_b_image = image
            self._last_stream_b_on_mask = on_mask

        display_image = draw_led_state_overlay(image, self._overlay_xy(stream_name), on_mask) \
            if on_mask is not None and self._context is not None else image
        # Cropped AFTER the overlay is drawn, not before - the overlay's
        # circles are positioned in full-frame coordinates (matching
        # stream_a_xy/stream_b_xy), so cropping first would misplace them
        # relative to the now-smaller image.
        display_image = self._crop_to_roi_if_available(display_image, stream_name)
        if stream_name == "stream_a":
            self.stream_a_panel.set_frame(display_image)
        else:
            self.stream_b_panel.set_frame(display_image)
            # "stream_b" is always the second of the pair emitted per
            # iteration (see SessionEngineThread.on_frames), so by this
            # point _last_stream_a_image/_last_stream_a_on_mask have already
            # been updated too.
            self._maybe_save_periodic_snapshot(pair_index)

    def _crop_to_roi_if_available(self, image, stream_name):
        # Only affects the live preview - _last_stream_a_image/
        # _last_stream_b_image (used for the saved debug snapshots) stay
        # full-frame, since seeing the ROI's placement in context is more
        # useful there than a tightly-cropped view.
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
        # pair_index in the filename lets you directly verify the saved
        # detection picture matches the frame that was on screen at that
        # exact moment - the same number the live display and the CSV's
        # pair_index column both use. One combined side-by-side image (not
        # two separate stream_a/stream_b files) so both streams for a given
        # pair are cross-checkable in a single glance/file.
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
        # Stale files from a previous run (e.g. one that ran longer and
        # reached higher pair_index values) would otherwise linger alongside
        # this run's snapshots and make "same frame index" cross-checking
        # ambiguous about which run a given file belongs to.
        for path in glob.glob(os.path.join(output_dir, "periodic_led_state_*.png")):
            os.remove(path)

    def _on_row_ready(self, row):
        # Fired on EVERY frame-pair (not throttled) - this must stay O(1)
        # and cheap. It used to also call add_point() (pyqtgraph setData())
        # here, up to 4 times per pair; even after bounding each series'
        # history, that was still too expensive to sustain every single
        # pair at up to 30fps, so a backlog of queued GUI-thread work built
        # up continuously and only became visible once the user tried to
        # interact (Stop, Save Debug Snapshot) and that click had to wait
        # behind the entire backlog - looking exactly like a freeze. Plot
        # updates now happen in _on_stats_ready instead, which only fires
        # every display_stride pairs. Only cheap counter bookkeeping stays
        # here, so the drop counts remain exact even though the plots don't
        # sample every single pair.
        if row.get("stream_a_frame_drop"):
            self._stream_a_drop_count += 1
            self._stream_a_drop_since_last_plot = True
        if row.get("stream_b_frame_drop"):
            self._stream_b_drop_count += 1
            self._stream_b_drop_since_last_plot = True

        # Every pair, like the drop counters above - RunningStats.update()
        # is an O(1) Welford step, cheap enough to sustain unthrottled
        # (unlike add_point()/setData(), see the big comment on this
        # method). Excluded pairs are skipped, same convention as the
        # plots (an excluded pair can carry a wild value, e.g. during
        # auto-exposure warmup, that would otherwise skew the running mean).
        if row.get("pairing_gap_us") is not None and not row.get("pairing_gap_us_excluded"):
            self._hw_ts_latency_stats.update(row["pairing_gap_us"])
        if row.get("position_gap_ms") is not None and not row.get("position_gap_ms_excluded"):
            self._optical_sync_stats.update(row["position_gap_ms"])

    def _on_stats_ready(self, stats):
        # Fired only at the throttled display_stride cadence (same frames
        # the video panels update on) - this is also where plot updates
        # happen now (see _on_row_ready), keeping the expensive pyqtgraph
        # setData() calls at a rate the GUI thread can actually sustain.
        pair_index = stats["pair_index"]
        self.stats_panel.set_value("frame_index", pair_index)

        # Same NaN-for-excluded-values convention as
        # optical_sync_poc_/pipeline_sync_test_diff.py's own plotting
        # (`np.where(valid, gap_ms, nan)`) - an excluded pair can carry a
        # wild real value (e.g. a multi-hundred-thousand-us pairing gap
        # during auto-exposure warmup) that would otherwise force the whole
        # y-axis to that scale.
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
        # Whether THIS stream dropped since the last plotted point, not just
        # this exact pair's own value - otherwise an isolated drop on one of
        # the ~9 skipped pairs between throttled samples would silently
        # never show up as a spike. Plotted as two series (not one combined
        # flag) so you can see which stream is actually the problem. Stream
        # B is mirrored to -1 (not +1) so a simultaneous stream_a+stream_b
        # drop never draws one line exactly on top of the other, hiding it -
        # stream A spikes up, stream B spikes down, and they can never
        # occlude each other.
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

    def _on_session_finished(self, rows):
        self._last_session_rows = rows
        export_session_csvs(rows, self._context["kept_csv_path"], self._context["dropped_csv_path"])
        export_session_plot(rows, os.path.join(self._context["output_dir"], "pipeline_sync_plot.png"))
        self._save_chart_images(self._context["output_dir"])
        self._save_led_state_debug_images()
        # Button re-enabling happens in _on_engine_thread_finished, not here -
        # this fires before SessionEngineThread.run()'s finally block (camera
        # pipeline/LED panel cleanup) has actually completed.

    def _on_engine_thread_finished(self):
        # QThread.finished - fires only once run() has fully returned,
        # finally block included, so it's safe to let the user start a new
        # session now (the camera/LED panel are actually free).
        self._session_running = False
        self.stop_button.setEnabled(False)
        self.duration_spinbox.setEnabled(True)
        self.switch_time_spinbox.setEnabled(True)
        # Not a blind setEnabled(True) on start_button - its availability
        # also reflects whether the spinbox actually holds an unconfirmed
        # value (see _update_confirm_switch_time_button_state).
        self._update_confirm_switch_time_button_state()
        self.frame_sample_interval_spinbox.setEnabled(True)

    def _save_led_state_debug_images(self):
        # Also wired to the "Save Debug Snapshot" button for an on-demand
        # check mid-session, not just the automatic one at Stop. Uses the
        # cached masks populated by _on_frame_ready from the signal payload
        # (already correctly paired to _last_stream_a_image/
        # _last_stream_b_image at the moment they arrived) - never reads a
        # live metric object, which was the source of the frame/detection
        # offset bug. Always reports what happened via status_label -
        # previously this returned silently on every path (not-ready,
        # success, and failure all looked identical), which is why the
        # button appeared "not working" even when it may have been
        # succeeding.
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

    def _on_error(self, message):
        # Surfaces a hardware failure (e.g. camera unplugged mid-session) to
        # the operator. Button re-enabling happens in
        # _on_engine_thread_finished, not here - this fires before
        # SessionEngineThread.run()'s finally block (camera pipeline/LED
        # panel cleanup) has actually completed.
        self.status_label.setText("Error: {}".format(message))
