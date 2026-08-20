import os

import cv2
import numpy as np
from PySide6.QtWidgets import QScrollArea

from gui.widgets.camera_live_session_panel import CameraLiveSessionPanel


# --- Same regression coverage as LiveSessionPage's own scroll-area test -
# ported because this widget now owns that same content directly. ---

def test_panel_content_lives_inside_a_resizable_scroll_area(qapp):
    panel = CameraLiveSessionPanel("cam1")
    scroll_areas = panel.findChildren(QScrollArea)
    assert len(scroll_areas) == 1
    scroll_area = scroll_areas[0]
    assert scroll_area.widgetResizable() is True
    assert panel.stream_a_panel in scroll_area.widget().findChildren(type(panel.stream_a_panel))


def test_set_camera_labels_sets_panel_titles(qapp):
    panel = CameraLiveSessionPanel("cam1")
    panel.set_camera_labels("Intel RealSense D455", "SN789", "Infrared 1", "Color")
    assert panel.stream_a_title_label.text() == "D455 [SN789] - Infrared 1"
    assert panel.stream_b_title_label.text() == "D455 [SN789] - Color"


def _prepared_panel(qapp, tmp_path, **overrides):
    panel = CameraLiveSessionPanel("cam1")
    kwargs = dict(
        output_dir=str(tmp_path), kept_csv_filename="kept.csv", dropped_csv_filename="dropped.csv",
        stream_a_xy=np.array([(1, 1), (2, 2)]), stream_b_xy=np.array([(1, 1), (2, 2)]),
        stream_a_roi=(0, 0, 4, 4), stream_b_roi=(0, 0, 4, 4),
        snapshot_every_n_pairs=20, max_snapshots=2, switch_time_ms=1.0,
    )
    kwargs.update(overrides)
    panel.prepare_for_run(**kwargs)
    panel._last_stream_a_image = np.zeros((4, 4), dtype=np.uint8)
    panel._last_stream_b_image = np.zeros((4, 4), dtype=np.uint8)
    panel._last_stream_a_on_mask = np.array([True, False])
    panel._last_stream_b_on_mask = np.array([True, False])
    return panel


# --- prepare_for_run: takes an ALREADY-DECIDED output_dir (the caller - the
# new orchestrating page - mints one shared run folder plus one subfolder
# per camera; this panel no longer mints its own folder at all, unlike
# LiveSessionPage's original start_session()), resets plots/counters/stats,
# shows the switch-time value. ---

def test_prepare_for_run_sets_csv_paths_under_the_given_output_dir(qapp, tmp_path):
    panel = CameraLiveSessionPanel("cam1")
    output_dir = str(tmp_path / "camera_cam1_D455")

    returned_dir = panel.prepare_for_run(
        output_dir=output_dir, kept_csv_filename="kept.csv", dropped_csv_filename="dropped.csv",
        stream_a_xy=np.array([(1, 1)]), stream_b_xy=np.array([(1, 1)]),
        stream_a_roi=(0, 0, 4, 4), stream_b_roi=(0, 0, 4, 4),
        snapshot_every_n_pairs=20, max_snapshots=2, switch_time_ms=3.0,
    )

    assert returned_dir == output_dir
    assert os.path.isdir(output_dir)  # created defensively if it didn't already exist
    assert panel._context["kept_csv_path"] == os.path.join(output_dir, "kept.csv")
    assert panel._context["dropped_csv_path"] == os.path.join(output_dir, "dropped.csv")
    assert panel.stats_panel._value_labels["switch_time_ms"].text() == "3.0"


def test_prepare_for_run_resets_counters_and_plots(qapp, tmp_path):
    panel = _prepared_panel(qapp, tmp_path)
    panel._stream_a_drop_count = 5
    panel.pairing_plot.add_point("pairing_gap_us", 0, 1.0)

    panel.prepare_for_run(
        output_dir=str(tmp_path), kept_csv_filename="kept.csv", dropped_csv_filename="dropped.csv",
        stream_a_xy=np.array([(1, 1)]), stream_b_xy=np.array([(1, 1)]),
        stream_a_roi=(0, 0, 4, 4), stream_b_roi=(0, 0, 4, 4),
        snapshot_every_n_pairs=20, max_snapshots=2, switch_time_ms=1.0,
    )

    assert panel._stream_a_drop_count == 0
    assert panel.pairing_plot.get_series_data("pairing_gap_us") == ([], [])


# --- Ported directly from test_live_session_page.py: _maybe_save_periodic_
# snapshot/_crop_to_roi_if_available's own behavior is unchanged, only their
# owning class/context shape moved. ---

def test_maybe_save_periodic_snapshot_skips_when_pair_index_not_a_multiple(qapp, tmp_path):
    panel = _prepared_panel(qapp, tmp_path)

    panel._maybe_save_periodic_snapshot(pair_index=7)

    assert panel._periodic_snapshot_count == 0
    assert not os.path.exists(os.path.join(panel._context["output_dir"], "periodic_led_state_pair00007.png"))


def test_maybe_save_periodic_snapshot_saves_on_multiple_of_every_n(qapp, tmp_path):
    panel = _prepared_panel(qapp, tmp_path)

    panel._maybe_save_periodic_snapshot(pair_index=20)

    assert panel._periodic_snapshot_count == 1
    output_dir = panel._context["output_dir"]
    combined_path = os.path.join(output_dir, "periodic_led_state_pair00020.png")
    assert os.path.exists(combined_path)
    combined = cv2.imread(combined_path)
    assert combined.shape[1] > 4


def test_maybe_save_periodic_snapshot_stops_after_max_snapshots(qapp, tmp_path):
    panel = _prepared_panel(qapp, tmp_path, max_snapshots=1)

    panel._maybe_save_periodic_snapshot(pair_index=0)
    panel._maybe_save_periodic_snapshot(pair_index=20)

    assert panel._periodic_snapshot_count == 1


def test_maybe_save_periodic_snapshot_noop_without_context(qapp):
    panel = CameraLiveSessionPanel("cam1")

    panel._maybe_save_periodic_snapshot(pair_index=0)  # must not raise

    assert panel._periodic_snapshot_count == 0


def test_crop_to_roi_if_available_returns_image_unchanged_without_context(qapp):
    panel = CameraLiveSessionPanel("cam1")
    image = np.zeros((4, 4), dtype=np.uint8)

    assert panel._crop_to_roi_if_available(image, "stream_a") is image


def test_crop_to_roi_if_available_crops_when_roi_present(qapp, tmp_path):
    panel = _prepared_panel(qapp, tmp_path, stream_a_roi=(1, 1, 2, 2))
    image = np.zeros((4, 4), dtype=np.uint8)

    result = panel._crop_to_roi_if_available(image, "stream_a")

    assert result.shape == (2, 2)


# --- on_frame_ready/on_row_ready/on_stats_ready: the public callback API the
# new orchestrating page's MultiCameraSessionController wiring drives - same
# behavior as LiveSessionPage's private _on_frame_ready/_on_row_ready/
# _on_stats_ready, just renamed public (no owning SessionEngineThread of its
# own to connect a Qt signal to directly anymore). ---

def test_on_frame_ready_updates_the_right_video_panel(qapp, tmp_path):
    panel = _prepared_panel(qapp, tmp_path)
    image = np.zeros((4, 4), dtype=np.uint8)

    panel.on_frame_ready("stream_a", image, 0, None)

    assert panel._last_stream_a_image is image


def test_on_row_ready_counts_frame_drops(qapp, tmp_path):
    panel = _prepared_panel(qapp, tmp_path)

    panel.on_row_ready({"stream_a_frame_drop": True, "stream_b_frame_drop": False})
    panel.on_row_ready({"stream_a_frame_drop": False, "stream_b_frame_drop": True})

    assert panel._stream_a_drop_count == 1
    assert panel._stream_b_drop_count == 1


def test_on_row_ready_updates_running_stats_skipping_excluded(qapp, tmp_path):
    panel = _prepared_panel(qapp, tmp_path)

    panel.on_row_ready({"pairing_gap_us": 100.0, "pairing_gap_us_excluded": False})
    panel.on_row_ready({"pairing_gap_us": 99999.0, "pairing_gap_us_excluded": True})

    assert panel._hw_ts_latency_stats.count == 1
    assert panel._hw_ts_latency_stats.mean == 100.0


def test_on_stats_ready_plots_a_point_and_updates_stats_panel(qapp, tmp_path):
    panel = _prepared_panel(qapp, tmp_path)

    panel.on_stats_ready({"pair_index": 5, "pairing_gap_us": 42.0, "pairing_gap_us_excluded": False})

    assert panel.pairing_plot.get_series_data("pairing_gap_us") == ([5], [42.0])
    assert panel.stats_panel._value_labels["pairing_gap_us"].text() == "42.0"


def test_on_stats_ready_nans_out_excluded_values_on_the_plot(qapp, tmp_path):
    panel = _prepared_panel(qapp, tmp_path)

    panel.on_stats_ready({"pair_index": 1, "pairing_gap_us": 99999.0, "pairing_gap_us_excluded": True})

    _, ys = panel.pairing_plot.get_series_data("pairing_gap_us")
    assert len(ys) == 1
    assert ys[0] != ys[0]  # NaN


def test_on_session_finished_writes_csvs_and_plot(qapp, tmp_path):
    panel = _prepared_panel(qapp, tmp_path)
    rows = [{
        "pair_index": 0, "stream_a_ts_us": 0.0, "stream_b_ts_us": 0.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
        "pairing_gap_us": 1.0, "pairing_gap_us_excluded": False, "pairing_gap_us_exclude_reason": None,
    }]

    panel.on_session_finished(rows)

    output_dir = panel._context["output_dir"]
    assert os.path.exists(os.path.join(output_dir, "kept.csv"))
    assert os.path.exists(os.path.join(output_dir, "pipeline_sync_plot.png"))
    assert panel._last_session_rows == rows


def test_on_error_sets_status_label(qapp):
    panel = CameraLiveSessionPanel("cam1")

    panel.on_error("camera unplugged")

    assert "camera unplugged" in panel.status_label.text()


def test_save_chart_images_writes_three_named_png_files(qapp, tmp_path):
    panel = CameraLiveSessionPanel("cam1")
    panel.pairing_plot.add_point("pairing_gap_us", 0, 10.0)
    panel.position_plot.add_point("position_gap_ms", 0, 1.0)
    panel.drop_plot.add_point("stream_a_frame_drops", 0, 1)

    panel._save_chart_images(str(tmp_path))

    for filename in ("hw_ts_latency_chart.png", "optical_sync_chart.png", "frame_drops_chart.png"):
        path = os.path.join(str(tmp_path), filename)
        assert os.path.exists(path)
        assert os.path.getsize(path) > 0
