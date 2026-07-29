import pytest
import numpy as np
import yaml
from domain.calibration import (
    assign_grid_ids,
    build_positions_with_thresholds,
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


def test_assign_grid_ids_raises_on_empty_input():
    try:
        assign_grid_ids([])
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
