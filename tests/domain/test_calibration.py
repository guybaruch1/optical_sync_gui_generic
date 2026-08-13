import pytest
import numpy as np
import yaml
from domain.calibration import (
    assign_grid_ids,
    centroids_in_grid_order,
    offset_positions,
    build_positions_with_thresholds,
    build_grid_positions,
    compute_threshold,
    update_config_leds,
    load_led_positions,
)


def test_assign_grid_ids_orders_row_major():
    # Two rows of 3, deliberately shuffled and not left-to-right.
    centroids = [(20, 10), (10, 10), (30, 10), (20, 30), (10, 30), (30, 30)]
    positions, row_layout = assign_grid_ids(centroids, row_gap_px=15)
    assert row_layout == [3, 3]
    assert positions["0"] == [10.0, 10.0]
    assert positions["1"] == [20.0, 10.0]
    assert positions["2"] == [30.0, 10.0]
    assert positions["3"] == [10.0, 30.0]


def test_assign_grid_ids_stays_split_when_row_spacing_is_tighter_than_configured_row_gap_px():
    # Regression test for Issue 4 (docs/algorithm_review_log.md): a fixed,
    # resolution-independent row_gap_px risks ending up ABOVE the real
    # row-to-row pixel pitch once resolution shrinks it (e.g. VGA vs HD for
    # the same physical panel) - here real row spacing is 14px, tighter than
    # the default row_gap_px=15, so the OLD code (curr_y - prev_y > 15)
    # never split at all and collapsed all 9 centroids into row_layout=[9],
    # scrambling every led_id. Column spacing is 10px, so
    # safe_row_gap_px caps the effective threshold at 6 (10 * 0.6), well
    # under the real 14px row gap, restoring the correct 3x3 split.
    centroids = [
        (0.0, 0.0), (10.0, 0.0), (20.0, 0.0),
        (0.0, 14.0), (10.0, 14.0), (20.0, 14.0),
        (0.0, 28.0), (10.0, 28.0), (20.0, 28.0),
    ]
    positions, row_layout = assign_grid_ids(centroids, row_gap_px=15)
    assert row_layout == [3, 3, 3]
    assert positions["0"] == [0.0, 0.0]
    assert positions["1"] == [10.0, 0.0]
    assert positions["2"] == [20.0, 0.0]
    assert positions["3"] == [0.0, 14.0]
    assert positions["8"] == [20.0, 28.0]


def test_assign_grid_ids_respects_a_configured_row_gap_px_smaller_than_the_safe_cap():
    # Companion sanity check: when the operator's OWN configured row_gap_px
    # is already tighter than what safe_row_gap_px would compute, that
    # smaller configured value must still be what actually governs (min(),
    # not the safe fraction alone) - otherwise an intentionally-tight config
    # would be silently loosened back up by this fix. Real row spacing here
    # is 4px; safe_row_gap_px would cap at 4 (max(min_gap_px=4, int(4*0.6)=2)),
    # which would NOT split (4 is not > 4) - but the explicitly configured
    # row_gap_px=3 (tighter than that safe cap) correctly still splits
    # (4 > 3), confirming min(configured, safe) - not safe alone - is used.
    centroids = [(0.0, 0.0), (30.0, 0.0), (0.0, 4.0), (30.0, 4.0)]
    positions, row_layout = assign_grid_ids(centroids, row_gap_px=3)
    assert row_layout == [2, 2]


def test_assign_grid_ids_raises_on_empty_input():
    try:
        assign_grid_ids([])
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


def test_centroids_in_grid_order_matches_led_id_index():
    # Same shuffled input as test_assign_grid_ids_orders_row_major - the
    # ordered list this returns must line up with positions["0"], ["1"], ...
    # (index i IS led_id i), so a debug image numbered by enumerate() over
    # this list shows the SAME id assign_grid_ids itself assigned - not
    # detect_led_centroids' raw, arbitrary contour-scan order.
    centroids = [(20, 10), (10, 10), (30, 10), (20, 30), (10, 30), (30, 30)]
    ordered, positions, row_layout = centroids_in_grid_order(centroids, row_gap_px=15)
    assert row_layout == [3, 3]
    assert ordered == [tuple(positions[str(i)]) for i in range(6)]
    assert ordered[0] == (10.0, 10.0)
    assert ordered[1] == (20.0, 10.0)
    assert ordered[5] == (30.0, 30.0)


def test_centroids_in_grid_order_raises_on_empty_input_same_as_assign_grid_ids():
    try:
        centroids_in_grid_order([])
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


def test_build_positions_with_thresholds_computes_midpoint():
    on_frame = np.full((20, 20), 200, dtype=np.uint8)
    off_frame = np.full((20, 20), 100, dtype=np.uint8)
    xy_positions = {"0": (10, 10)}
    result = build_positions_with_thresholds(xy_positions, on_frame, off_frame, neighborhood_size=5)
    x, y, on_value, off_value, threshold = result["0"]
    assert (x, y) == (10, 10)
    assert on_value == 200.0
    assert off_value == 100.0
    assert threshold == 150.0


def test_offset_positions_shifts_by_roi_origin():
    positions = {"0": [10.0, 10.0], "1": [20.0, 30.0]}
    result = offset_positions(positions, roi=(100, 50, 200, 200))
    assert result["0"] == [110.0, 60.0]
    assert result["1"] == [120.0, 80.0]


def test_build_grid_positions_matches_the_old_inline_sequence():
    # Same shuffled 2-row grid as test_assign_grid_ids_orders_row_major, in
    # CROPPED coordinates - build_grid_positions must reproduce exactly what
    # centroids_in_grid_order -> offset_positions -> build_positions_with_thresholds
    # chained by hand would have produced.
    centroids = [(20, 10), (10, 10), (30, 10), (20, 30), (10, 30), (30, 30)]
    roi = (100, 50, 200, 200)
    on_frame = np.full((300, 300), 200, dtype=np.uint8)
    off_frame = np.full((300, 300), 100, dtype=np.uint8)

    positions, row_layout, debug_centroids = build_grid_positions(
        centroids, roi, on_frame, off_frame, row_gap_px=15, neighborhood_size=5,
    )

    assert row_layout == [3, 3]
    assert debug_centroids[0] == (10.0, 10.0)  # still in CROPPED coordinates
    x, y, on_value, off_value, threshold = positions["0"]
    assert (x, y) == (110.0, 60.0)  # offset back to full-frame coordinates
    assert on_value == 200.0
    assert off_value == 100.0
    assert threshold == 150.0


def test_build_grid_positions_raises_on_empty_centroids():
    with pytest.raises(RuntimeError):
        build_grid_positions([], (0, 0, 10, 10), np.zeros((10, 10)), np.zeros((10, 10)), 15, 5)


def test_compute_threshold_at_half_fraction_matches_calibrations_own_midpoint():
    on_values = np.array([300.0, 300.0])
    off_values = np.array([100.0, 100.0])
    result = compute_threshold(on_values, off_values, fraction=0.5)
    assert list(result) == [200.0, 200.0]


def test_compute_threshold_scales_between_off_and_on():
    on_values = np.array([300.0])
    off_values = np.array([100.0])
    result = compute_threshold(on_values, off_values, fraction=0.25)
    assert list(result) == [150.0]


def test_compute_threshold_is_independent_per_stream_for_different_brightness_ranges():
    # Two streams with different brightness ranges (e.g. IR vs RGB) tuned
    # at different fractions must not bleed into each other's result.
    stream_a_threshold = compute_threshold(np.array([300.0]), np.array([100.0]), fraction=0.25)
    stream_b_threshold = compute_threshold(np.array([600.0]), np.array([200.0]), fraction=0.75)
    assert list(stream_a_threshold) == [150.0]
    assert list(stream_b_threshold) == [500.0]


def test_build_positions_with_thresholds_caps_window_at_safe_size_for_tight_spacing(monkeypatch):
    # Regression test for Issue 1 (docs/algorithm_review_log.md): a fixed
    # configured neighborhood_size, unrelated to real LED pixel spacing,
    # risks bleeding into a neighboring LED's pixels. Confirms the actual
    # sampling calls receive the capped safe size, not the raw configured
    # value, when two LEDs are only 6px apart (safe_neighborhood_size(6px
    # spacing, configured=5) -> 3, per its own directly-tested math).
    calls = []

    def spy_sample(image, x, y, size):
        calls.append(size)
        return 150.0

    monkeypatch.setattr("domain.calibration.sample_neighborhood_brightness", spy_sample)

    on_frame = np.full((20, 20), 200, dtype=np.uint8)
    off_frame = np.full((20, 20), 100, dtype=np.uint8)
    xy_positions = {"0": (10.0, 10.0), "1": (16.0, 10.0)}  # 6px apart

    build_positions_with_thresholds(xy_positions, on_frame, off_frame, neighborhood_size=5)

    assert calls == [3, 3, 3, 3]  # on+off for each of 2 LEDs, all capped to 3, not the configured 5


def test_update_config_leds_writes_per_stream_slugs(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"leds": {"Other Camera": {"color": {}}}}))

    update_config_leds(
        str(config_path), camera_name="Test Camera",
        stream_a_slug="infrared1", stream_a_positions={"0": [1.0, 2.0, 255.0, 100.0, 177.5]}, stream_a_res=(1280, 720),
        stream_b_slug="color", stream_b_positions={"0": [3.0, 4.0, 250.0, 90.0, 170.0]}, stream_b_res=(1280, 720),
    )

    written = yaml.safe_load(config_path.read_text())
    assert "Other Camera" in written["leds"]  # untouched sibling block preserved
    assert written["leds"]["Test Camera"]["infrared1"]["positions"]["0"] == [1.0, 2.0, 255.0, 100.0, 177.5]
    assert written["leds"]["Test Camera"]["color"]["frame_width"] == 1280


def test_load_led_positions_returns_slug_keyed_dicts(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({
        "leds": {"Test Camera": {
            "infrared1": {"frame_width": 1280, "frame_height": 720, "positions": {"0": [1.0, 2.0, 255.0, 100.0, 177.5]}},
            "infrared2": {"frame_width": 1280, "frame_height": 720, "positions": {"0": [3.0, 4.0, 250.0, 90.0, 170.0]}},
        }}
    }))
    stream_a_positions, stream_b_positions = load_led_positions(
        str(config_path), "Test Camera", "infrared1", (1280, 720), "infrared2", (1280, 720)
    )
    assert stream_a_positions["0"] == [1.0, 2.0, 255.0, 100.0, 177.5]
    assert stream_b_positions["0"] == [3.0, 4.0, 250.0, 90.0, 170.0]


def test_load_led_positions_raises_for_uncalibrated_stream_pair(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"leds": {"Test Camera": {"color": {}}}}))
    with pytest.raises(KeyError):
        load_led_positions(str(config_path), "Test Camera", "infrared1", (1280, 720), "infrared2", (1280, 720))


def test_load_led_positions_raises_when_stored_resolution_does_not_match_current(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({
        "leds": {"Test Camera": {
            "infrared1": {"frame_width": 1280, "frame_height": 720, "positions": {"0": [1.0, 2.0, 255.0, 100.0, 177.5]}},
            "infrared2": {"frame_width": 1280, "frame_height": 720, "positions": {"0": [3.0, 4.0, 250.0, 90.0, 170.0]}},
        }}
    }))
    with pytest.raises(RuntimeError):
        # calibrated at 1280x720, but the currently-picked resolution is 640x480
        load_led_positions(str(config_path), "Test Camera", "infrared1", (640, 480), "infrared2", (1280, 720))


def test_update_config_leds_preserves_other_stream_slugs_on_same_camera(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"leds": {}}))

    update_config_leds(
        str(config_path), camera_name="Test Camera",
        stream_a_slug="infrared1", stream_a_positions={"0": [1.0, 2.0, 255.0, 100.0, 177.5]}, stream_a_res=(1280, 720),
        stream_b_slug="infrared2", stream_b_positions={"0": [3.0, 4.0, 250.0, 90.0, 170.0]}, stream_b_res=(1280, 720),
    )
    update_config_leds(
        str(config_path), camera_name="Test Camera",
        stream_a_slug="color", stream_a_positions={"0": [5.0, 6.0, 200.0, 80.0, 140.0]}, stream_a_res=(640, 480),
        stream_b_slug="color2", stream_b_positions={"0": [7.0, 8.0, 210.0, 85.0, 147.5]}, stream_b_res=(640, 480),
    )

    written = yaml.safe_load(config_path.read_text())
    camera_entry = written["leds"]["Test Camera"]
    # Both the first pair's slugs AND the second pair's slugs must coexist
    assert "infrared1" in camera_entry and "infrared2" in camera_entry
    assert "color" in camera_entry and "color2" in camera_entry
    assert camera_entry["infrared1"]["positions"]["0"] == [1.0, 2.0, 255.0, 100.0, 177.5]
    assert camera_entry["color"]["positions"]["0"] == [5.0, 6.0, 200.0, 80.0, 140.0]
