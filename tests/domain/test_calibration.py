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


def test_update_config_leds_writes_camera_subblock(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"leds": {"Other Camera": {"ir": {}, "rgb": {}}}}))

    update_config_leds(
        str(config_path),
        camera_name="Test Camera",
        ir_positions={"0": [1.0, 2.0, 255.0, 100.0, 177.5]},
        ir_res=(1280, 720),
        rgb_positions={"0": [3.0, 4.0, 250.0, 90.0, 170.0]},
        rgb_res=(1280, 720),
    )

    written = yaml.safe_load(config_path.read_text())
    assert "Other Camera" in written["leds"]  # untouched sibling block preserved
    assert written["leds"]["Test Camera"]["ir"]["positions"]["0"] == [1.0, 2.0, 255.0, 100.0, 177.5]
    assert written["leds"]["Test Camera"]["rgb"]["frame_width"] == 1280


def test_load_led_positions_returns_ir_and_rgb_dicts(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({
        "leds": {
            "Test Camera": {
                "ir": {"positions": {"0": [1.0, 2.0, 255.0, 100.0, 177.5]}},
                "rgb": {"positions": {"0": [3.0, 4.0, 250.0, 90.0, 170.0]}},
            }
        }
    }))
    ir_positions, rgb_positions = load_led_positions(str(config_path), "Test Camera")
    assert ir_positions["0"] == [1.0, 2.0, 255.0, 100.0, 177.5]
    assert rgb_positions["0"] == [3.0, 4.0, 250.0, 90.0, 170.0]


def test_load_led_positions_raises_for_uncalibrated_camera(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"leds": {"Other Camera": {"ir": {}, "rgb": {}}}}))
    try:
        load_led_positions(str(config_path), "Never Calibrated Camera")
        assert False, "expected KeyError"
    except KeyError:
        pass
