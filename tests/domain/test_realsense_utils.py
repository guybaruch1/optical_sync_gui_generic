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
    draw_bundle_overlay,
    draw_led_state_overlay,
    decode_frame,
    DECODERS,
    _typical_spacing,
    _debug_circle_radius,
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


def test_detect_led_centroids_finds_bright_blob():
    image = np.zeros((50, 50), dtype=np.uint8)
    image[20:30, 20:30] = 255
    centroids, chosen_threshold = detect_led_centroids(image, None, min_area=20)
    assert len(centroids) == 1
    cx, cy = centroids[0]
    assert 20 <= cx <= 30
    assert 20 <= cy <= 30


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


def test_draw_bundle_overlay_does_not_mutate_bgr_input():
    image = np.zeros((100, 300, 3), dtype=np.uint8)

    result = draw_bundle_overlay(
        image, bundle_index=0, stream_a_frame_number=0, stream_b_frame_number=0,
        stream_a_ts_us=0.0, stream_b_ts_us=0.0, delta_us=0.0,
    )

    assert (image == 0).all()  # original untouched
    assert (result > 0).any()  # the copy has the drawn text


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
