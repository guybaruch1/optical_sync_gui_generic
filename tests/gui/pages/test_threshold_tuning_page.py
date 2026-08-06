from unittest.mock import MagicMock, patch

import numpy as np

from gui.pages.threshold_tuning_page import ThresholdTuningPage


class _FakeSignal:
    """Bare-bones stand-in for a Qt signal - records connected slots and lets
    tests fire them on demand, so a fake thread's `finished` can be
    triggered manually to simulate real hardware cleanup completing."""
    def __init__(self):
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *args, **kwargs):
        for slot in self._slots:
            slot(*args, **kwargs)


class _FakePreviewThread:
    """Stands in for ThresholdPreviewThread so Start never touches real
    hardware - records the args/kwargs it was constructed with so tests can
    assert on what actually got passed."""
    last_args = None
    last_kwargs = None

    def __init__(self, *args, **kwargs):
        _FakePreviewThread.last_args = args
        _FakePreviewThread.last_kwargs = kwargs
        self.frame_ready = _FakeSignal()
        self.error = _FakeSignal()
        self.finished = _FakeSignal()
        self.request_stop = MagicMock()
        self.wait = MagicMock()

    def start(self):
        pass


def _minimal_context(**overrides):
    # A real (non-zero) full-frame ROI + valid small images - unlike the
    # rest of this context (which only feeds the pre-existing
    # threshold-fraction on/off logic), set_context() now also runs LED
    # Detection Threshold Tuning's own Tier 1 recompute synchronously
    # (crop_to_roi + detect_led_centroids + draw_detected_centroids), so
    # these need to be real, valid data even for tests that don't otherwise
    # care about detection - a zero-size ROI (the old placeholder) would
    # crop to an empty array and crash.
    blank_image = np.full((20, 20), 50, dtype=np.uint8)
    ctx = dict(
        ctx=None, device_serial="123456",
        pick_a={"stream_type": "infrared", "stream_index": 1, "width": 4, "height": 4, "fps": 30, "format": "y8"},
        pick_b={"stream_type": "color", "stream_index": 0, "width": 4, "height": 4, "fps": 30, "format": "bgr8"},
        camera_controls={},
        stream_a_xy=np.array([(1, 1), (2, 2)]), stream_b_xy=np.array([(1, 1), (2, 2)]),
        stream_a_on=np.full(2, 300.0), stream_a_off=np.full(2, 100.0),
        stream_b_on=np.full(2, 600.0), stream_b_off=np.full(2, 200.0),
        num_leds=2, neighborhood_size=5, scan_direction=1, switch_time_ms=1,
        stream_a_threshold_fraction_default=0.25, stream_b_threshold_fraction_default=0.25,
        stream_a_roi=(0, 0, 20, 20), stream_b_roi=(0, 0, 20, 20), camera_name="Intel RealSense D455",
        stream_a_label="Infrared 1", stream_b_label="Color",
        config_path="config.yaml",
        image_a_on=blank_image, image_a_off=blank_image,
        image_b_on=blank_image, image_b_off=blank_image,
        stream_a_otsu_threshold=127, stream_b_otsu_threshold=127,
        min_blob_area=5, row_gap_px=15, calibration_neighborhood_size=5,
        stream_a_positions={"0": [1.0, 1.0, 300.0, 100.0, 200.0], "1": [2.0, 2.0, 300.0, 100.0, 200.0]},
        stream_b_positions={"0": [1.0, 1.0, 600.0, 200.0, 400.0], "1": [2.0, 2.0, 600.0, 200.0, 400.0]},
    )
    ctx.update(overrides)
    return ctx


def _page_with_context(**context_overrides):
    page = ThresholdTuningPage()
    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        page.set_context(**_minimal_context(**context_overrides))
    return page


def _started_page(**context_overrides):
    page = _page_with_context(**context_overrides)
    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        page._on_start_clicked()
    return page


def test_set_context_prefills_both_threshold_fraction_spinboxes_independently(qapp):
    page = _page_with_context(
        stream_a_threshold_fraction_default=0.3, stream_b_threshold_fraction_default=0.6,
    )
    assert page.stream_a_threshold_fraction_spinbox.value() == 0.3
    assert page.stream_b_threshold_fraction_spinbox.value() == 0.6


def test_set_context_prefills_switch_time_spinbox(qapp):
    page = _page_with_context(switch_time_ms=7)
    assert page.switch_time_spinbox.value() == 7


def test_set_context_prefills_switch_time_spinbox_with_a_fractional_value(qapp):
    # Regression test: set_context() used to truncate via int(round(...)),
    # silently discarding a fractional switch time before it was ever shown.
    page = _page_with_context(switch_time_ms=0.5)
    assert page.switch_time_spinbox.value() == 0.5


def test_set_context_does_not_auto_start_preview(qapp):
    page = _page_with_context()
    assert page.preview_thread is None
    assert page.start_button.isEnabled()
    assert not page.stop_button.isEnabled()


def test_start_button_starts_preview_with_correct_args(qapp):
    page = _page_with_context(device_serial="abc123", switch_time_ms=3, scan_direction=1)
    page.frame_sample_interval_spinbox.setValue(5)

    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        page._on_start_clicked()

    assert page.preview_thread is not None
    assert _FakePreviewThread.last_args[1] == "abc123"
    assert _FakePreviewThread.last_kwargs["switch_time_ms"] == 3
    assert _FakePreviewThread.last_kwargs["scan_direction"] == 1
    assert _FakePreviewThread.last_kwargs["display_stride"] == 5


def test_start_button_starts_preview_with_a_fractional_switch_time(qapp):
    page = _page_with_context(device_serial="abc123", switch_time_ms=1)
    page.switch_time_spinbox.setValue(0.5)

    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        page._on_start_clicked()

    assert _FakePreviewThread.last_kwargs["switch_time_ms"] == 0.5


def test_start_button_disables_itself_enables_stop_and_locks_frame_sample_interval(qapp):
    page = _started_page()
    assert not page.start_button.isEnabled()
    assert page.stop_button.isEnabled()
    assert not page.frame_sample_interval_spinbox.isEnabled()


def test_stop_button_requests_stop_without_blocking(qapp):
    page = _started_page()
    thread = page.preview_thread

    page._on_stop_clicked()

    thread.request_stop.assert_called_once()
    thread.wait.assert_not_called()
    # Non-blocking - re-enabling only happens once `finished` actually fires,
    # not immediately on the Stop click.
    assert page.preview_thread is thread
    assert not page.start_button.isEnabled()


def test_finished_signal_reenables_start_and_unlocks_frame_sample_interval(qapp):
    page = _started_page()
    thread = page.preview_thread

    thread.finished.emit()

    assert page.preview_thread is None
    assert page.start_button.isEnabled()
    assert not page.stop_button.isEnabled()
    assert page.frame_sample_interval_spinbox.isEnabled()


def test_stream_a_threshold_property_reflects_current_spinbox_value(qapp):
    page = _page_with_context()  # stream_a: off=100, on=300
    page.stream_a_threshold_fraction_spinbox.setValue(0.5)
    assert list(page.stream_a_threshold) == [200.0, 200.0]


def test_stream_b_threshold_property_is_independent_from_stream_a(qapp):
    page = _page_with_context()  # stream_b: off=200, on=600
    page.stream_a_threshold_fraction_spinbox.setValue(0.5)  # -> 200.0 for stream_a
    page.stream_b_threshold_fraction_spinbox.setValue(0.75)  # -> 200 + 0.75*400 = 500.0 for stream_b
    assert list(page.stream_a_threshold) == [200.0, 200.0]
    assert list(page.stream_b_threshold) == [500.0, 500.0]


def test_switch_time_ms_property_returns_current_spinbox_value(qapp):
    page = _page_with_context(switch_time_ms=1)
    page.switch_time_spinbox.setValue(42)
    assert page.switch_time_ms == 42


def test_switch_time_ms_property_returns_a_fractional_spinbox_value(qapp):
    page = _page_with_context(switch_time_ms=1)
    page.switch_time_spinbox.setValue(0.5)
    assert page.switch_time_ms == 0.5


# --- LED Detection Threshold Tuning: manual override of Calibration's
# Otsu-based LED-position detection, per stream. ---

def _two_blob_image():
    import cv2
    image = np.full((40, 40), 20, dtype=np.uint8)
    cv2.circle(image, (10, 10), 5, 200, -1)
    cv2.circle(image, (30, 30), 5, 200, -1)
    return image


def _detection_tuning_context(**overrides):
    two_blobs = _two_blob_image()
    return _minimal_context(
        image_a_on=two_blobs, image_a_off=np.full((40, 40), 20, dtype=np.uint8),
        stream_a_roi=(0, 0, 40, 40), stream_a_otsu_threshold=100, min_blob_area=10,
        **overrides,
    )


def test_detection_slider_inits_to_cached_otsu_value_per_stream(qapp):
    page = _page_with_context(stream_a_otsu_threshold=140, stream_b_otsu_threshold=90)
    assert page.stream_a_detection_slider.value() == 140
    assert page.stream_b_detection_slider.value() == 90


def test_moving_detection_slider_updates_detected_count_label(qapp):
    page = ThresholdTuningPage()
    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        page.set_context(**_detection_tuning_context())

    page.stream_a_detection_slider.setValue(100)  # below the blobs' 200 -> finds both
    assert page.stream_a_detected_count_label.text() == "Detected: 2 / 2"

    page.stream_a_detection_slider.setValue(255)  # above every pixel -> finds nothing
    assert page.stream_a_detected_count_label.text() == "Detected: 0 / 2"


def test_zero_centroids_leaves_context_xy_unchanged(qapp):
    page = ThresholdTuningPage()
    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        page.set_context(**_detection_tuning_context())
    original_xy = page._context["stream_a_xy"]

    page.stream_a_detection_slider.setValue(255)  # finds nothing
    page._commit_detection_threshold("stream_a")  # bypass the debounce timer, same effect

    assert page.stream_a_detected_count_label.text() == "Detected: 0 / 2"
    assert page._context["stream_a_xy"] is original_xy  # untouched, not corrupted


def test_commit_detection_threshold_updates_context_on_success(qapp):
    page = ThresholdTuningPage()
    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        page.set_context(**_detection_tuning_context())

    page.stream_a_detection_slider.setValue(100)  # finds both real blobs
    page._commit_detection_threshold("stream_a")

    assert len(page._context["stream_a_xy"]) == 2
    assert len(page._context["stream_a_positions"]) == 2


def test_detection_spinbox_and_slider_stay_linked(qapp):
    page = _page_with_context()
    page.stream_a_detection_spinbox.setValue(77)
    assert page.stream_a_detection_slider.value() == 77
    page.stream_a_detection_slider.setValue(33)
    assert page.stream_a_detection_spinbox.value() == 33


def test_reset_to_auto_restores_the_cached_otsu_value(qapp):
    page = _page_with_context(stream_a_otsu_threshold=140)
    page.stream_a_detection_slider.setValue(50)

    page.stream_a_reset_to_auto_button.click()

    assert page.stream_a_detection_slider.value() == 140


def test_detection_controls_disabled_while_preview_running(qapp):
    page = _started_page()
    assert not page.stream_a_detection_slider.isEnabled()
    assert not page.stream_a_detection_spinbox.isEnabled()
    assert not page.stream_a_reset_to_auto_button.isEnabled()

    page.preview_thread.finished.emit()

    assert page.stream_a_detection_slider.isEnabled()
    assert page.stream_a_detection_spinbox.isEnabled()
    assert page.stream_a_reset_to_auto_button.isEnabled()


def test_on_frame_ready_computes_mask_from_current_spinbox_value_not_the_original_default(qapp, monkeypatch):
    page = _page_with_context()  # stream_a: off=100, on=300, default fraction=0.25 -> threshold 150
    page.stream_a_threshold_fraction_spinbox.setValue(0.5)  # -> threshold 200, not the original 150

    captured = {}

    def fake_draw_overlay(image, xy, mask):
        captured["mask"] = list(mask)
        return image

    monkeypatch.setattr("gui.pages.threshold_tuning_page.draw_led_state_overlay", fake_draw_overlay)

    image = np.zeros((4, 4), dtype=np.uint8)
    brightness = np.array([150.0, 250.0])  # LED0 below 200 -> off, LED1 above 200 -> on
    page._on_frame_ready("stream_a", image, 0, brightness)

    assert captured["mask"] == [False, True]


def test_continue_button_blocks_until_preview_thread_stopped_and_emits_tuning_done(qapp):
    page = _started_page()
    thread = page.preview_thread

    received = []
    page.tuning_done.connect(lambda: received.append(True))
    with patch("gui.pages.threshold_tuning_page.update_config_leds") as mock_update, \
         patch("gui.pages.threshold_tuning_page.stream_slug", side_effect=["infrared1", "color"]):
        page._on_continue_clicked()

    thread.request_stop.assert_called_once()
    thread.wait.assert_called_once()
    assert received == [True]
    assert page.preview_thread is None
    assert page.start_button.isEnabled()
    mock_update.assert_called_once()


def test_continue_button_is_a_noop_stop_when_nothing_is_running(qapp):
    page = _page_with_context()  # never started

    received = []
    page.tuning_done.connect(lambda: received.append(True))
    with patch("gui.pages.threshold_tuning_page.update_config_leds"), \
         patch("gui.pages.threshold_tuning_page.stream_slug", side_effect=["infrared1", "color"]):
        page._on_continue_clicked()  # must not raise

    assert received == [True]


def test_continue_button_persists_current_positions_to_config(qapp):
    page = _page_with_context()

    with patch("gui.pages.threshold_tuning_page.update_config_leds") as mock_update, \
         patch("gui.pages.threshold_tuning_page.stream_slug", side_effect=["infrared1", "color"]):
        page._on_continue_clicked()

    args = mock_update.call_args.args
    assert args[0] == "config.yaml"  # config_path from _minimal_context
    assert args[2] == "infrared1" and args[5] == "color"
    assert args[3] == page._context["stream_a_positions"]
    assert args[6] == page._context["stream_b_positions"]


def test_continue_button_warns_on_led_count_mismatch(qapp):
    page = _page_with_context(
        stream_a_positions={"0": [1.0, 1.0, 300.0, 100.0, 200.0]},  # 1 LED, num_leds=2
    )

    with patch("gui.pages.threshold_tuning_page.update_config_leds"), \
         patch("gui.pages.threshold_tuning_page.stream_slug", side_effect=["infrared1", "color"]), \
         patch("gui.pages.threshold_tuning_page.QMessageBox") as mock_message_box:
        page._on_continue_clicked()

    mock_message_box.warning.assert_called_once()
