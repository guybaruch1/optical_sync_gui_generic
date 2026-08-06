import os
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
from PySide6.QtWidgets import QScrollArea

from gui.pages.live_session_page import LiveSessionPage, _short_camera_name


def test_short_camera_name_returns_model_designator():
    assert _short_camera_name("Intel RealSense D455") == "D455"


def test_short_camera_name_handles_single_word_input():
    assert _short_camera_name("D455") == "D455"


def test_short_camera_name_handles_empty_string():
    assert _short_camera_name("") == ""


# --- Regression test: on a screen too small to fit two video panels + three
# charts + the toolbar at once, there was no way to reach whatever didn't
# fit - Qt just clipped/overlapped content instead of scrolling to it. The
# page's real content must live inside a resizable QScrollArea, not
# directly in the page itself. ---

def test_page_content_lives_inside_a_resizable_scroll_area(qapp):
    page = LiveSessionPage()
    scroll_areas = page.findChildren(QScrollArea)
    assert len(scroll_areas) == 1
    scroll_area = scroll_areas[0]
    assert scroll_area.widgetResizable() is True
    # The video panels (and everything else) are descendants of the scroll
    # area's own content widget, not siblings of it directly on the page -
    # confirms they're actually wrapped inside the scrollable area.
    assert scroll_area.widget() is not None
    assert page.stream_a_panel in scroll_area.widget().findChildren(type(page.stream_a_panel))


def _minimal_context(tmp_path, **overrides):
    ctx = dict(
        ctx=None, device_serial="123456",
        pick_a={"stream_type": "infrared", "stream_index": 1, "width": 4, "height": 4, "fps": 30, "format": "y8"},
        pick_b={"stream_type": "color", "stream_index": 0, "width": 4, "height": 4, "fps": 30, "format": "bgr8"},
        camera_controls={},
        switch_time_ms=1.0, scan_direction=1,
        # Already-tuned final threshold arrays (Threshold Tuning page's own
        # job now, not Live Session's) - 150.0 matches what the old
        # off=100/on=300/fraction=0.25 fixture produced, for numeric
        # consistency with existing test expectations.
        stream_a_threshold=np.full(2, 150.0), stream_b_threshold=np.full(2, 150.0),
        stream_a_xy=np.array([(1, 1), (2, 2)]), stream_b_xy=np.array([(1, 1), (2, 2)]),
        num_leds=2, neighborhood_size=5, frame_drop_threshold_factor=1.5,
        warmup_pairs_to_skip=0, pairing_gap_outlier_threshold_us=100000,
        output_root=str(tmp_path), kept_csv_filename="kept.csv", dropped_csv_filename="dropped.csv",
        snapshot_every_n_pairs=20, max_snapshots=2,
        stream_a_roi=(0, 0, 4, 4), stream_b_roi=(0, 0, 4, 4), camera_name="Intel RealSense D455",
        stream_a_label="Infrared 1", stream_b_label="Color",
    )
    ctx.update(overrides)
    return ctx


def _page_with_frame_data(qapp, tmp_path, **context_overrides):
    page = LiveSessionPage()
    page.set_context(**_minimal_context(tmp_path, **context_overrides))
    # Tests that call page internals directly (not through start_session())
    # still need a run folder minted, same as a real Start click would do.
    page._begin_new_run_output()
    page._last_stream_a_image = np.zeros((4, 4), dtype=np.uint8)
    page._last_stream_b_image = np.zeros((4, 4), dtype=np.uint8)
    page._last_stream_a_on_mask = np.array([True, False])
    page._last_stream_b_on_mask = np.array([True, False])
    return page


def test_maybe_save_periodic_snapshot_skips_when_pair_index_not_a_multiple(qapp, tmp_path):
    page = _page_with_frame_data(qapp, tmp_path)

    page._maybe_save_periodic_snapshot(pair_index=7)  # 7 % 20 != 0

    assert page._periodic_snapshot_count == 0
    assert not os.path.exists(os.path.join(page._context["output_dir"], "periodic_led_state_pair00007.png"))


def test_maybe_save_periodic_snapshot_saves_on_multiple_of_every_n(qapp, tmp_path):
    page = _page_with_frame_data(qapp, tmp_path)

    page._maybe_save_periodic_snapshot(pair_index=20)

    assert page._periodic_snapshot_count == 1
    output_dir = page._context["output_dir"]
    # ONE combined side-by-side image, not two separate stream_a/stream_b
    # files - both streams' 4px-wide overlays plus the gap column between
    # them, so the saved file must be wider than either stream alone.
    combined_path = os.path.join(output_dir, "periodic_led_state_pair00020.png")
    assert os.path.exists(combined_path)
    combined = cv2.imread(combined_path)
    assert combined.shape[1] > 4
    assert not os.path.exists(os.path.join(output_dir, "periodic_led_state_stream_a_pair00020.png"))
    assert not os.path.exists(os.path.join(output_dir, "periodic_led_state_stream_b_pair00020.png"))


def test_maybe_save_periodic_snapshot_stops_after_max_snapshots(qapp, tmp_path):
    page = _page_with_frame_data(qapp, tmp_path, max_snapshots=1)

    page._maybe_save_periodic_snapshot(pair_index=0)  # 0 % 20 == 0 -> saves, count becomes 1
    page._maybe_save_periodic_snapshot(pair_index=20)  # count already at max_snapshots -> skipped

    assert page._periodic_snapshot_count == 1
    assert not os.path.exists(os.path.join(page._context["output_dir"], "periodic_led_state_pair00020.png"))


def test_maybe_save_periodic_snapshot_noop_without_context(qapp):
    page = LiveSessionPage()

    page._maybe_save_periodic_snapshot(pair_index=0)  # must not raise

    assert page._periodic_snapshot_count == 0


def test_maybe_save_periodic_snapshot_noop_when_every_n_is_zero(qapp, tmp_path):
    page = _page_with_frame_data(qapp, tmp_path, snapshot_every_n_pairs=0)

    page._maybe_save_periodic_snapshot(pair_index=0)

    assert page._periodic_snapshot_count == 0


def test_crop_to_roi_if_available_returns_image_unchanged_without_context(qapp):
    page = LiveSessionPage()
    image = np.zeros((4, 4), dtype=np.uint8)

    result = page._crop_to_roi_if_available(image, "stream_a")

    assert result is image


def test_crop_to_roi_if_available_returns_image_unchanged_for_zero_size_roi(qapp, tmp_path):
    page = LiveSessionPage()
    page.set_context(**_minimal_context(tmp_path, stream_a_roi=(0, 0, 0, 0)))
    image = np.zeros((4, 4), dtype=np.uint8)

    result = page._crop_to_roi_if_available(image, "stream_a")

    assert result is image


def test_crop_to_roi_if_available_crops_when_roi_present(qapp, tmp_path):
    page = LiveSessionPage()
    page.set_context(**_minimal_context(tmp_path, stream_a_roi=(1, 1, 2, 2)))
    image = np.zeros((4, 4), dtype=np.uint8)

    result = page._crop_to_roi_if_available(image, "stream_a")

    assert result.shape == (2, 2)


def test_crop_to_roi_if_available_uses_stream_b_roi_for_stream_b(qapp, tmp_path):
    page = LiveSessionPage()
    page.set_context(**_minimal_context(tmp_path, stream_a_roi=(0, 0, 0, 0), stream_b_roi=(1, 1, 3, 3)))
    image = np.zeros((4, 4), dtype=np.uint8)

    result = page._crop_to_roi_if_available(image, "stream_b")

    assert result.shape == (3, 3)


class _FakeEngineThread:
    """Stands in for SessionEngineThread so start_session() never touches
    real hardware - records the kwargs it was constructed with so tests can
    assert on what the toolbar's live values actually passed through."""
    last_kwargs = None

    def __init__(self, *args, **kwargs):
        _FakeEngineThread.last_kwargs = kwargs
        self.frame_ready = MagicMock()
        self.row_ready = MagicMock()
        self.stats_ready = MagicMock()
        self.session_finished = MagicMock()
        self.error = MagicMock()
        self.finished = MagicMock()

    def start(self):
        pass

    def wait(self):
        pass


def test_set_context_prefills_switch_time_spinbox_from_settings_value(qapp, tmp_path):
    page = LiveSessionPage()
    page.set_context(**_minimal_context(tmp_path, switch_time_ms=7))
    assert page.switch_time_spinbox.value() == 7


def test_set_context_prefills_switch_time_spinbox_with_a_fractional_value(qapp, tmp_path):
    # Regression test: set_context() used to truncate via int(round(...)),
    # silently throwing away a value already tuned to a fraction (e.g. 0.5)
    # on the Threshold Tuning page before handoff.
    page = LiveSessionPage()
    page.set_context(**_minimal_context(tmp_path, switch_time_ms=7.5))
    assert page.switch_time_spinbox.value() == 7.5


def test_start_session_passes_toolbar_switch_time_and_frame_sample_interval(qapp, tmp_path):
    page = LiveSessionPage()
    page.set_context(**_minimal_context(tmp_path, switch_time_ms=1))
    page.switch_time_spinbox.setValue(42)
    page.frame_sample_interval_spinbox.setValue(99)

    with patch("gui.pages.live_session_page.SessionEngineThread", _FakeEngineThread):
        page.start_session()

    assert _FakeEngineThread.last_kwargs["switch_time_ms"] == 42
    assert _FakeEngineThread.last_kwargs["display_stride"] == 99


def test_start_session_passes_a_fractional_toolbar_switch_time(qapp, tmp_path):
    page = LiveSessionPage()
    page.set_context(**_minimal_context(tmp_path, switch_time_ms=1))
    page.switch_time_spinbox.setValue(2.5)

    with patch("gui.pages.live_session_page.SessionEngineThread", _FakeEngineThread):
        page.start_session()

    assert _FakeEngineThread.last_kwargs["switch_time_ms"] == 2.5


def test_start_session_passes_context_threshold_arrays_straight_through(qapp, tmp_path):
    # Threshold tuning already happened on the Threshold Tuning wizard page
    # before this one - start_session() must use ctx["stream_a_threshold"]/
    # ctx["stream_b_threshold"] as-is, with no recomputation from a
    # fraction/on/off (that plumbing no longer exists on this page at all).
    page = LiveSessionPage()
    page.set_context(**_minimal_context(
        tmp_path, stream_a_threshold=np.full(2, 175.0), stream_b_threshold=np.full(2, 225.0),
    ))

    with patch("gui.pages.live_session_page.SessionEngineThread", _FakeEngineThread):
        page.start_session()

    position_gap_metric = _FakeEngineThread.last_kwargs["position_gap_metric"]
    assert list(position_gap_metric.stream_a_threshold) == [175.0, 175.0]
    assert list(position_gap_metric.stream_b_threshold) == [225.0, 225.0]


def test_frame_sample_interval_of_one_shows_a_warning(qapp):
    page = LiveSessionPage()

    page.frame_sample_interval_spinbox.setValue(1)
    assert page.frame_sample_interval_warning_label.text() != ""

    page.frame_sample_interval_spinbox.setValue(10)
    assert page.frame_sample_interval_warning_label.text() == ""


def test_start_session_locks_duration_switch_time_and_frame_sample_interval(qapp, tmp_path):
    page = LiveSessionPage()
    page.set_context(**_minimal_context(tmp_path))

    with patch("gui.pages.live_session_page.SessionEngineThread", _FakeEngineThread):
        page.start_session()

    assert not page.duration_spinbox.isEnabled()
    assert not page.switch_time_spinbox.isEnabled()
    assert not page.frame_sample_interval_spinbox.isEnabled()

    page._on_engine_thread_finished()

    assert page.duration_spinbox.isEnabled()
    assert page.switch_time_spinbox.isEnabled()
    assert page.frame_sample_interval_spinbox.isEnabled()


# --- Per-run output folder: every Start click must mint its OWN
# output/live_session_<timestamp>/ folder so a new run never overwrites a
# previous run's CSVs/graphs/snapshots. ---

def test_begin_new_run_output_creates_a_fresh_folder_under_output_root(qapp, tmp_path):
    page = LiveSessionPage()
    page.set_context(**_minimal_context(tmp_path))

    output_dir = page._begin_new_run_output()

    assert os.path.isdir(output_dir)
    assert os.path.dirname(output_dir) == str(tmp_path)
    assert page._context["kept_csv_path"] == os.path.join(output_dir, "kept.csv")
    assert page._context["dropped_csv_path"] == os.path.join(output_dir, "dropped.csv")


def test_two_start_session_calls_use_two_different_run_folders(qapp, tmp_path):
    page = LiveSessionPage()
    page.set_context(**_minimal_context(tmp_path))

    with patch("gui.pages.live_session_page.SessionEngineThread", _FakeEngineThread):
        page.start_session()
        first_output_dir = page._context["output_dir"]
        page._on_engine_thread_finished()
        page.start_session()
        second_output_dir = page._context["output_dir"]

    assert first_output_dir != second_output_dir
    assert os.path.isdir(first_output_dir)
    assert os.path.isdir(second_output_dir)


# --- The 3 live charts (HW TS Latency / Optical Sync / Frame Drops) must
# auto-save as PNG files, not only be manually copyable to the clipboard. ---

def test_save_chart_images_writes_three_named_png_files(qapp, tmp_path):
    page = LiveSessionPage()
    page.pairing_plot.add_point("pairing_gap_us", 0, 10.0)
    page.position_plot.add_point("position_gap_ms", 0, 1.0)
    page.drop_plot.add_point("stream_a_frame_drops", 0, 1)

    page._save_chart_images(str(tmp_path))

    for filename in ("hw_ts_latency_chart.png", "optical_sync_chart.png", "frame_drops_chart.png"):
        path = os.path.join(str(tmp_path), filename)
        assert os.path.exists(path)
        assert os.path.getsize(path) > 0
