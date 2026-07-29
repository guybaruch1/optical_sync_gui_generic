import os
from unittest.mock import MagicMock, patch

import numpy as np

from gui.pages.live_session_page import LiveSessionPage, _short_camera_name


def test_short_camera_name_returns_model_designator():
    assert _short_camera_name("Intel RealSense D455") == "D455"


def test_short_camera_name_handles_single_word_input():
    assert _short_camera_name("D455") == "D455"


def test_short_camera_name_handles_empty_string():
    assert _short_camera_name("") == ""


def _minimal_context(tmp_path, **overrides):
    ctx = dict(
        ctx=None, device_serial="123456",
        pick_a={"stream_type": "infrared", "stream_index": 1, "width": 4, "height": 4, "fps": 30, "format": "y8"},
        pick_b={"stream_type": "color", "stream_index": 0, "width": 4, "height": 4, "fps": 30, "format": "bgr8"},
        camera_controls=[],
        switch_time_ms=1.0, scan_direction=1,
        stream_a_threshold=np.full(2, 150.0), stream_b_threshold=np.full(2, 150.0),
        stream_a_xy=np.array([(1, 1), (2, 2)]), stream_b_xy=np.array([(1, 1), (2, 2)]),
        num_leds=2, neighborhood_size=5, frame_drop_threshold_factor=1.5,
        warmup_pairs_to_skip=0, pairing_gap_outlier_threshold_us=100000,
        kept_csv_path=str(tmp_path / "kept.csv"), dropped_csv_path=str(tmp_path / "dropped.csv"),
        output_dir=str(tmp_path), snapshot_every_n_pairs=20, max_snapshots=2,
        stream_a_roi=(0, 0, 4, 4), stream_b_roi=(0, 0, 4, 4), camera_name="Intel RealSense D455",
        stream_a_label="Infrared 1", stream_b_label="Color",
    )
    ctx.update(overrides)
    return ctx


def _page_with_frame_data(qapp, tmp_path, **context_overrides):
    page = LiveSessionPage()
    page.set_context(**_minimal_context(tmp_path, **context_overrides))
    page._last_stream_a_image = np.zeros((4, 4), dtype=np.uint8)
    page._last_stream_b_image = np.zeros((4, 4), dtype=np.uint8)
    page._last_stream_a_on_mask = np.array([True, False])
    page._last_stream_b_on_mask = np.array([True, False])
    return page


def test_maybe_save_periodic_snapshot_skips_when_pair_index_not_a_multiple(qapp, tmp_path):
    page = _page_with_frame_data(qapp, tmp_path)

    page._maybe_save_periodic_snapshot(pair_index=7)  # 7 % 20 != 0

    assert page._periodic_snapshot_count == 0
    assert not os.path.exists(os.path.join(str(tmp_path), "periodic_led_state_stream_a_pair00007.png"))


def test_maybe_save_periodic_snapshot_saves_on_multiple_of_every_n(qapp, tmp_path):
    page = _page_with_frame_data(qapp, tmp_path)

    page._maybe_save_periodic_snapshot(pair_index=20)

    assert page._periodic_snapshot_count == 1
    assert os.path.exists(os.path.join(str(tmp_path), "periodic_led_state_stream_a_pair00020.png"))
    assert os.path.exists(os.path.join(str(tmp_path), "periodic_led_state_stream_b_pair00020.png"))


def test_maybe_save_periodic_snapshot_stops_after_max_snapshots(qapp, tmp_path):
    page = _page_with_frame_data(qapp, tmp_path, max_snapshots=1)

    page._maybe_save_periodic_snapshot(pair_index=0)  # 0 % 20 == 0 -> saves, count becomes 1
    page._maybe_save_periodic_snapshot(pair_index=20)  # count already at max_snapshots -> skipped

    assert page._periodic_snapshot_count == 1
    assert not os.path.exists(os.path.join(str(tmp_path), "periodic_led_state_stream_a_pair00020.png"))


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


def test_start_session_passes_toolbar_switch_time_and_frame_sample_interval(qapp, tmp_path):
    page = LiveSessionPage()
    page.set_context(**_minimal_context(tmp_path, switch_time_ms=1))
    page.switch_time_spinbox.setValue(42)
    page.frame_sample_interval_spinbox.setValue(99)

    with patch("gui.pages.live_session_page.SessionEngineThread", _FakeEngineThread):
        page.start_session()

    assert _FakeEngineThread.last_kwargs["switch_time_ms"] == 42
    assert _FakeEngineThread.last_kwargs["display_stride"] == 99


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
