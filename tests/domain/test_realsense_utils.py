import numpy as np
import pyrealsense2 as rs
from domain.realsense_utils import (
    sample_neighborhood_brightness,
    sample_all_neighborhood_brightness,
    apply_roi_mask,
    crop_to_roi,
    merge_close_centroids,
    detect_led_centroids,
    save_debug_detection_image,
    draw_detected_centroids,
    draw_bundle_overlay,
    draw_cross_camera_debug_overlay,
    draw_led_state_overlay,
    combine_side_by_side,
    decode_frame,
    DECODERS,
    _typical_spacing,
    _debug_circle_radius,
    safe_neighborhood_size,
    safe_row_gap_px,
)


def test_sample_neighborhood_brightness_center_patch():
    image = np.zeros((20, 20), dtype=np.uint8)
    image[8:13, 8:13] = 200
    value = sample_neighborhood_brightness(image, x=10, y=10, size=5)
    assert value == 200.0


def test_sample_neighborhood_brightness_clamps_at_edge():
    image = np.full((10, 10), 100, dtype=np.uint8)
    # Should not raise even though the window would run off the top-left edge.
    value = sample_neighborhood_brightness(image, x=0, y=0, size=5)
    assert value == 100.0


def test_sample_all_neighborhood_brightness_samples_each_position():
    image = np.zeros((20, 20, 3), dtype=np.uint8)
    image[8:13, 8:13] = 200  # BGR patch around (10, 10)
    xy_positions = [(10, 10), (2, 2)]

    result = sample_all_neighborhood_brightness(image, xy_positions, size=5)

    assert result.tolist() == [200.0, 0.0]


def test_sample_all_neighborhood_brightness_grayscale_input_not_reconverted():
    image = np.zeros((20, 20), dtype=np.uint8)
    image[8:13, 8:13] = 150

    result = sample_all_neighborhood_brightness(image, [(10, 10)], size=5)

    assert result.tolist() == [150.0]


def test_apply_roi_mask_zeroes_outside_box():
    image = np.full((10, 10, 3), 255, dtype=np.uint8)
    masked = apply_roi_mask(image, (2, 2, 3, 3))
    assert masked[0, 0].tolist() == [0, 0, 0]
    assert masked[3, 3].tolist() == [255, 255, 255]
    assert masked.shape == image.shape


def test_crop_to_roi_returns_only_the_roi_region():
    image = np.zeros((10, 10, 3), dtype=np.uint8)
    image[2:5, 2:5] = 255  # a 3x3 bright block starting at (2, 2)

    cropped = crop_to_roi(image, (2, 2, 3, 3))

    assert cropped.shape == (3, 3, 3)
    assert (cropped == 255).all()


def test_merge_close_centroids_merges_nearby_points():
    # nearest-neighbor distances here are [1.0, 1.0, ~55.9], so the median
    # (typical_spacing) is 1.0; distance_fraction must exceed 1.0 for the
    # merge_threshold to exceed the 1.0 gap between the first two points.
    centroids = [(10.0, 10.0), (11.0, 10.0), (50.0, 50.0)]
    merged = merge_close_centroids(centroids, distance_fraction=1.5)
    assert len(merged) == 2


def test_merge_close_centroids_passthrough_below_two_points():
    assert merge_close_centroids([(1.0, 1.0)]) == [(1.0, 1.0)]


def test_typical_spacing_none_below_two_points():
    assert _typical_spacing([(1.0, 1.0)]) is None


def test_typical_spacing_returns_median_nearest_neighbor_distance():
    # nearest-neighbor distances here are [1.0, 1.0, ~55.9], so the median is 1.0.
    centroids = [(10.0, 10.0), (11.0, 10.0), (50.0, 50.0)]
    assert _typical_spacing(centroids) == 1.0


def test_debug_circle_radius_falls_back_to_8_below_two_points():
    # Same fallback the original hardcoded-8px behavior relied on: with
    # nothing to compare spacing against, keep the original fixed radius.
    assert _debug_circle_radius([(25.0, 25.0)]) == 8


def test_debug_circle_radius_falls_back_to_8_for_wide_spacing():
    # Spacing (~42px) * 0.3 exceeds the 8px ceiling, so it's clamped to 8 -
    # never bigger than the original fixed radius.
    assert _debug_circle_radius([(10.0, 10.0), (40.0, 40.0)]) == 8


def test_debug_circle_radius_scales_down_for_tight_spacing():
    # 10px apart -> spacing * 0.3 = 3, comfortably under the 8px ceiling
    # and above the 2px floor.
    assert _debug_circle_radius([(20.0, 25.0), (30.0, 25.0)]) == 3


def test_debug_circle_radius_floors_at_2_for_very_tight_spacing():
    # 3px apart -> spacing * 0.3 < 1, floored at 2 so the circle never
    # vanishes entirely.
    assert _debug_circle_radius([(20.0, 25.0), (23.0, 25.0)]) == 2


def test_safe_neighborhood_size_falls_back_to_configured_below_two_points():
    # Can't measure a spacing from fewer than two LEDs - trust the
    # configured value unchanged (matches _debug_circle_radius's same
    # fewer-than-two-points fallback).
    assert safe_neighborhood_size([(25.0, 25.0)], configured_size=5) == 5


def test_safe_neighborhood_size_leaves_configured_value_unchanged_for_wide_spacing():
    # LEDs 40px apart -> spacing * 0.5 = 20, well above the configured 5,
    # so the configured value is never grown, only ever shrunk.
    assert safe_neighborhood_size([(10.0, 10.0), (50.0, 10.0)], configured_size=5) == 5


def test_safe_neighborhood_size_caps_at_safe_fraction_of_tight_spacing():
    # LEDs 6px apart -> spacing * 0.5 = 3, tighter than the configured 5,
    # so the window shrinks to stay safe.
    assert safe_neighborhood_size([(10.0, 10.0), (16.0, 10.0)], configured_size=5) == 3


def test_safe_neighborhood_size_floors_at_min_size_for_very_tight_spacing():
    # LEDs 2px apart -> spacing * 0.5 = 1, floored at the default min_size=3
    # so the window never shrinks to something too small to average at all.
    assert safe_neighborhood_size([(10.0, 10.0), (12.0, 10.0)], configured_size=5) == 3


def test_safe_neighborhood_size_never_exceeds_configured_even_if_min_size_is_higher():
    # A custom min_size higher than the configured value must still never
    # grow the result past what was actually configured.
    assert safe_neighborhood_size(
        [(10.0, 10.0), (12.0, 10.0)], configured_size=2, min_size=3,
    ) == 2


def test_safe_row_gap_px_falls_back_to_configured_below_two_points():
    # Can't measure a spacing from fewer than two LEDs - trust the
    # configured value unchanged (same fallback safe_neighborhood_size and
    # _debug_circle_radius already use).
    assert safe_row_gap_px([(25.0, 25.0)], configured_gap_px=15) == 15


def test_safe_row_gap_px_leaves_configured_value_unchanged_for_wide_spacing():
    # LEDs 40px apart -> spacing * 0.6 = 24, well above the configured 15,
    # so the configured value is never grown, only ever shrunk.
    assert safe_row_gap_px([(10.0, 10.0), (50.0, 10.0)], configured_gap_px=15) == 15


def test_safe_row_gap_px_caps_at_safe_fraction_of_tight_spacing():
    # LEDs 10px apart -> spacing * 0.6 = 6, tighter than the configured 15,
    # so the row-split threshold shrinks to stay below the real spacing -
    # this is the exact VGA failure mode: a fixed row_gap_px that's fine at
    # HD ends up ABOVE real spacing once resolution shrinks it.
    assert safe_row_gap_px([(10.0, 10.0), (20.0, 10.0)], configured_gap_px=15) == 6


def test_safe_row_gap_px_floors_at_min_gap_px_for_very_tight_spacing():
    # LEDs 5px apart -> spacing * 0.6 = 3, floored at the default
    # min_gap_px=4 so the threshold never shrinks to something that would
    # start splitting a single real row into spurious multiple rows purely
    # from ordinary centroid-detection y-jitter.
    assert safe_row_gap_px([(10.0, 10.0), (15.0, 10.0)], configured_gap_px=15) == 4


def test_safe_row_gap_px_never_exceeds_configured_even_if_min_gap_px_is_higher():
    # A custom min_gap_px higher than the configured value must still never
    # grow the result past what was actually configured.
    assert safe_row_gap_px(
        [(10.0, 10.0), (15.0, 10.0)], configured_gap_px=2, min_gap_px=4,
    ) == 2


def test_detect_led_centroids_finds_bright_blob():
    image = np.zeros((50, 50), dtype=np.uint8)
    image[20:30, 20:30] = 255
    centroids, chosen_threshold = detect_led_centroids(image, None, min_area=20)
    assert len(centroids) == 1
    cx, cy = centroids[0]
    assert 20 <= cx <= 30
    assert 20 <= cy <= 30


def test_detect_led_centroids_manual_threshold_finds_the_same_blob():
    # threshold=<int> is the manual override path (LED Detection Threshold
    # Tuning) - must still find the same blob a manual value comfortably
    # below its brightness (100) and above the background (0) would catch.
    image = np.zeros((50, 50), dtype=np.uint8)
    image[20:30, 20:30] = 200
    centroids, chosen_threshold = detect_led_centroids(image, 100, min_area=20)
    assert chosen_threshold == 100  # echoes the given value, not Otsu's own pick
    assert len(centroids) == 1
    cx, cy = centroids[0]
    assert 20 <= cx <= 30
    assert 20 <= cy <= 30


def test_detect_led_centroids_manual_threshold_above_blob_brightness_finds_nothing():
    # A manual threshold set ABOVE the blob's own brightness must exclude
    # it - confirms this is a real fixed cutoff, not silently falling back
    # to Otsu regardless of what's passed (the bug this function used to have).
    image = np.zeros((50, 50), dtype=np.uint8)
    image[20:30, 20:30] = 100
    centroids, chosen_threshold = detect_led_centroids(image, 150, min_area=20)
    assert chosen_threshold == 150
    assert centroids == []


def test_detect_led_centroids_separates_multiple_blurred_blobs_on_a_cropped_image():
    # Regression: a masked-but-not-cropped full frame (apply_roi_mask) has
    # a histogram dominated by masked-out zero pixels outside the ROI
    # (realistically, the ROI is a small fraction of a full camera frame,
    # e.g. 200x200 within 1280x720), so Otsu splits "zero background" vs
    # "everything inside the ROI" instead of "LED" vs "gap between LEDs" -
    # merging an entire grid of separate, blurred (realistic camera glow)
    # LEDs into ONE contour. Only exercising a single blob on an all-zero
    # background (the older test above) never caught this - reproducing it
    # needs multiple blobs, a non-zero gap background, blur, AND a
    # realistically small ROI-to-full-frame ratio.
    import cv2

    roi = (500, 300, 200, 200)
    roi_x, roi_y, roi_w, roi_h = roi
    frame = np.zeros((720, 1280), dtype=np.uint8)
    frame[roi_y:roi_y + roi_h, roi_x:roi_x + roi_w] = 40  # dim gray gap background
    for row in range(10):
        for col in range(10):
            cy = roi_y + 10 + row * 18
            cx = roi_x + 10 + col * 18
            cv2.circle(frame, (cx, cy), 7, 230, -1)
    frame = cv2.GaussianBlur(frame, (9, 9), sigmaX=3)

    masked = apply_roi_mask(frame, roi)
    cropped = crop_to_roi(frame, roi)

    centroids_masked, _ = detect_led_centroids(masked, None, min_area=5)
    centroids_cropped, _ = detect_led_centroids(cropped, None, min_area=5)

    assert len(centroids_masked) == 1  # confirms the bug reproduces with apply_roi_mask
    assert len(centroids_cropped) == 100  # crop_to_roi correctly separates all 100


def test_save_debug_detection_image_writes_file_and_marks_centroids(tmp_path):
    image = np.zeros((50, 50), dtype=np.uint8)
    path = str(tmp_path / "debug.png")

    save_debug_detection_image(image, [(25, 25)], path)

    import cv2
    assert (tmp_path / "debug.png").exists()
    saved = cv2.imread(path)
    assert saved is not None
    assert saved.shape == (50, 50, 3)  # grayscale input converted to BGR for drawing
    # A green circle outline was drawn around (25, 25); its ring should be
    # visible a few pixels off-center even though the exact center pixel
    # isn't guaranteed to be on the 1px-wide circle outline itself.
    ring_pixel = saved[25, 25 + 8]
    assert ring_pixel.tolist() == [0, 255, 0]


def test_save_debug_detection_image_shrinks_circles_for_tight_spacing(tmp_path):
    image = np.zeros((50, 50), dtype=np.uint8)
    path = str(tmp_path / "debug.png")
    centroids = [(20, 25), (30, 25)]  # 10px apart -> radius should shrink to 3

    save_debug_detection_image(image, centroids, path)

    import cv2
    saved = cv2.imread(path)
    # With the old fixed 8px radius these two circles (16px wide each, 10px
    # apart) would fully overlap. With the shrunk radius they must not - the
    # midpoint between the two centers should be untouched background.
    midpoint_pixel = saved[25, 25]
    assert midpoint_pixel.tolist() != [0, 255, 0]


def test_draw_detected_centroids_marks_circles_without_numbering():
    # Points far enough apart that _debug_circle_radius falls back to its
    # default 8px (same distances test_draw_led_state_overlay's own circle
    # test uses), so the radius here is unambiguous.
    image = np.zeros((50, 50), dtype=np.uint8)
    centroids = [(10, 10), (40, 40)]

    result = draw_detected_centroids(image, centroids)

    assert result.shape == (50, 50, 3)  # grayscale converted to BGR for drawing
    assert result is not image  # doesn't mutate/return the caller's own array
    assert (image == 0).all()  # original untouched
    assert result[10, 10 + 8].tolist() == [0, 255, 0]  # circle drawn at the first centroid
    # No numbering text (save_debug_detection_image draws "0"/"1" a few px
    # right of each point) - well past the circle stays plain background.
    assert result[10, 10 + 20].tolist() == [0, 0, 0]


def test_draw_detected_centroids_does_not_write_to_disk(tmp_path, monkeypatch):
    import cv2

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("draw_detected_centroids must not touch disk")

    monkeypatch.setattr(cv2, "imwrite", _fail_if_called)

    draw_detected_centroids(np.zeros((20, 20), dtype=np.uint8), [(10, 10)])


def test_draw_bundle_overlay_converts_grayscale_and_draws_text():
    image = np.zeros((100, 300), dtype=np.uint8)

    result = draw_bundle_overlay(
        image, bundle_index=1690, stream_a_frame_number=1950, stream_b_frame_number=1958,
        stream_a_ts_us=4287559946, stream_b_ts_us=4287559980, delta_us=-34.0,
    )

    assert result.shape == (100, 300, 3)  # grayscale input converted to BGR for drawing
    assert result is not image  # never mutates the caller's array
    assert (result > 0).any()  # some text pixels were actually drawn


def test_draw_led_state_overlay_marks_on_led_green_and_off_led_red():
    image = np.zeros((50, 50), dtype=np.uint8)
    xy_positions = [(10, 10), (40, 40)]
    on_mask = [True, False]

    result = draw_led_state_overlay(image, xy_positions, on_mask)

    assert result.shape == (50, 50, 3)  # grayscale input converted to BGR for drawing
    assert result is not image  # never mutates the caller's array
    assert result[10, 10 + 8].tolist() == [0, 255, 0]  # on -> green ring
    assert result[40, 40 + 8].tolist() == [0, 0, 255]  # off -> red ring


def test_draw_led_state_overlay_shrinks_circles_for_tight_spacing():
    image = np.zeros((50, 50), dtype=np.uint8)
    xy_positions = [(20, 25), (30, 25)]  # 10px apart -> radius should shrink to 3
    on_mask = [True, True]

    result = draw_led_state_overlay(image, xy_positions, on_mask)

    # With the old fixed 8px radius these two circles would fully overlap.
    # With the shrunk radius the midpoint between the two centers should be
    # untouched background, not part of either green ring.
    midpoint_pixel = result[25, 25]
    assert midpoint_pixel.tolist() != [0, 255, 0]


def test_draw_led_state_overlay_does_not_mutate_bgr_input():
    image = np.zeros((50, 50, 3), dtype=np.uint8)

    result = draw_led_state_overlay(image, [(25, 25)], [True])

    assert (image == 0).all()  # original untouched
    assert (result > 0).any()  # the copy has the drawn circle


def test_combine_side_by_side_same_height_concatenates_with_gap():
    image_a = np.full((20, 10, 3), 100, dtype=np.uint8)
    image_b = np.full((20, 15, 3), 200, dtype=np.uint8)

    result = combine_side_by_side(image_a, image_b, gap_px=4)

    assert result.shape == (20, 10 + 4 + 15, 3)
    assert (result[:, :10] == 100).all()          # stream A on the left, untouched
    assert (result[:, 10 + 4:] == 200).all()       # stream B on the right, untouched


def test_combine_side_by_side_pads_shorter_image_to_match_taller_height():
    # Stream A (e.g. infrared) shorter than stream B (e.g. color) - the
    # shorter one must be letterboxed (padded), never resized/stretched,
    # so LED pixel positions stay comparable across the two halves.
    image_a = np.full((10, 5, 3), 100, dtype=np.uint8)
    image_b = np.full((20, 5, 3), 200, dtype=np.uint8)

    result = combine_side_by_side(image_a, image_b, gap_px=2, gap_color=(0, 0, 0))

    assert result.shape == (20, 5 + 2 + 5, 3)
    # Stream A's own 10 rows of real content still show its original value
    # somewhere in the padded column, not stretched into new rows.
    stream_a_column = result[:, :5]
    assert (stream_a_column == 100).any()
    assert (stream_a_column == 0).any()  # the letterbox padding


def test_combine_side_by_side_gap_column_uses_gap_color():
    image_a = np.full((10, 5, 3), 100, dtype=np.uint8)
    image_b = np.full((10, 5, 3), 200, dtype=np.uint8)

    result = combine_side_by_side(image_a, image_b, gap_px=3, gap_color=(1, 2, 3))

    gap_column = result[:, 5:5 + 3]
    assert (gap_column == np.array([1, 2, 3], dtype=np.uint8)).all()


def test_draw_bundle_overlay_does_not_mutate_bgr_input():
    image = np.zeros((100, 300, 3), dtype=np.uint8)

    result = draw_bundle_overlay(
        image, bundle_index=0, stream_a_frame_number=0, stream_b_frame_number=0,
        stream_a_ts_us=0.0, stream_b_ts_us=0.0, delta_us=0.0,
    )

    assert (image == 0).all()  # original untouched
    assert (result > 0).any()  # the copy has the drawn text


def test_draw_cross_camera_debug_overlay_converts_grayscale_and_draws_text():
    image = np.zeros((100, 300), dtype=np.uint8)

    result = draw_cross_camera_debug_overlay(
        image, cross_pair_index=42, master_pair_index=100, slave_pair_index=98,
        master_ts_us=1_000_000.0, slave_ts_us=1_000_010.0,
        master_global_ts_us=2_000_000.0, slave_global_ts_us=2_000_012.0,
        pairing_gap_us=-5.0, global_ts_gap_us=-12.0, position_gap_ms=1.5,
    )

    assert result.shape == (100, 300, 3)  # grayscale input converted to BGR for drawing
    assert result is not image  # never mutates the caller's array
    assert (result > 0).any()  # some text pixels were actually drawn


def test_draw_cross_camera_debug_overlay_does_not_mutate_bgr_input():
    image = np.zeros((100, 300, 3), dtype=np.uint8)

    result = draw_cross_camera_debug_overlay(
        image, cross_pair_index=0, master_pair_index=0, slave_pair_index=0,
        master_ts_us=0.0, slave_ts_us=0.0, master_global_ts_us=0.0, slave_global_ts_us=0.0,
        pairing_gap_us=0.0, global_ts_gap_us=0.0, position_gap_ms=0.0,
    )

    assert (image == 0).all()  # original untouched
    assert (result > 0).any()  # the copy has the drawn text


def test_draw_cross_camera_debug_overlay_handles_none_position_gap():
    image = np.zeros((50, 200), dtype=np.uint8)

    # Must not raise when Optical Sync is a "miss" (position_gap_ms=None) -
    # a real, common case (no clear on-LED detected that frame).
    result = draw_cross_camera_debug_overlay(
        image, cross_pair_index=1, master_pair_index=1, slave_pair_index=1,
        master_ts_us=0.0, slave_ts_us=0.0, master_global_ts_us=0.0, slave_global_ts_us=0.0,
        pairing_gap_us=0.0, global_ts_gap_us=0.0, position_gap_ms=None,
    )

    assert result.shape == (50, 200, 3)


def test_decode_frame_y8_reshapes_correctly():
    raw = bytes(range(6))  # 2x3, 1 byte/pixel
    image = decode_frame(raw, rs.format.y8, width=3, height=2)
    assert image.shape == (2, 3)
    assert image[0].tolist() == [0, 1, 2]


def test_decode_frame_bgr8_reshapes_correctly():
    raw = bytes(range(12))  # 2x2 bgr8, 3 bytes/pixel
    image = decode_frame(raw, rs.format.bgr8, width=2, height=2)
    assert image.shape == (2, 2, 3)


def test_decode_frame_yuyv_returns_bgr_shape():
    width, height = 4, 2
    raw = bytes([128] * (width * height * 2))
    image = decode_frame(raw, rs.format.yuyv, width, height)
    assert image.shape == (height, width, 3)


def test_decode_frame_raises_for_unsupported_format():
    import pytest
    with pytest.raises(RuntimeError):
        decode_frame(b"", rs.format.z16, width=4, height=4)


def test_y16_is_not_a_decodable_format():
    # y16 (16-bit-per-pixel) is advertised by D400 stereo modules but
    # nothing downstream handles anything but 8-bit - it must never be
    # offered as a pickable Stream Select option (see
    # engine/streams.py's list_video_stream_options_from_device, which
    # filters against this dict).
    assert rs.format.y16 not in DECODERS


def test_decode_frame_raises_for_y16():
    import pytest
    # Defense in depth, in case y16 is ever re-added to DECODERS by mistake -
    # decode_frame should still refuse it via its existing "not in DECODERS"
    # RuntimeError.
    with pytest.raises(RuntimeError):
        decode_frame(b"", rs.format.y16, width=4, height=4)


def test_draw_bundle_overlay_uses_stream_a_stream_b_naming():
    image = np.zeros((100, 300), dtype=np.uint8)
    result = draw_bundle_overlay(
        image, bundle_index=1, stream_a_frame_number=10, stream_b_frame_number=11,
        stream_a_ts_us=1000.0, stream_b_ts_us=1005.0, delta_us=-5.0,
    )
    assert result.shape == (100, 300, 3)
