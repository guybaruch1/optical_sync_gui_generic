"""LED grid assignment and per-LED on/off/threshold math.

Ported from optical_sync_poc_/led_calibration.py. See that file's module
docstring for the full rationale (per-LED thresholds instead of one
global constant, row-major grid numbering assumption, etc.) - this module
keeps only the pure computation, not the camera/LED-panel orchestration.
"""

import yaml

from domain.realsense_utils import sample_neighborhood_brightness, safe_neighborhood_size


def assign_grid_ids(centroids, row_gap_px=15):
    if not centroids:
        raise RuntimeError("No LEDs detected at all - check threshold/min_area/framing.")

    sorted_pts = sorted(centroids, key=lambda p: p[1])
    rows = [[sorted_pts[0]]]
    for prev, curr in zip(sorted_pts, sorted_pts[1:]):
        if curr[1] - prev[1] > row_gap_px:
            rows.append([])
        rows[-1].append(curr)
    rows = [sorted(row, key=lambda p: p[0]) for row in rows]

    positions = {}
    led_id = 0
    for row in rows:
        for (x, y) in row:
            positions[str(led_id)] = [round(float(x), 2), round(float(y), 2)]
            led_id += 1

    row_layout = [len(row) for row in rows]
    return positions, row_layout


def build_positions_with_thresholds(xy_positions, on_frame, off_frame, neighborhood_size):
    # Caps neighborhood_size at what's actually safe for THIS run's real
    # measured LED pixel spacing (see safe_neighborhood_size's docstring) -
    # a fixed configured window can otherwise bleed into a neighboring LED's
    # pixels at tight spacing, corrupting that LED's on/off threshold.
    safe_size = safe_neighborhood_size(list(xy_positions.values()), neighborhood_size)
    result = {}
    for led_id, (x, y) in xy_positions.items():
        on_value = sample_neighborhood_brightness(on_frame, x, y, safe_size)
        off_value = sample_neighborhood_brightness(off_frame, x, y, safe_size)
        threshold = off_value + 0.5 * (on_value - off_value)
        result[led_id] = [x, y, round(on_value, 2), round(off_value, 2), round(threshold, 2)]
    return result


def update_config_leds(config_path, camera_name, stream_a_slug, stream_a_positions, stream_a_res,
                        stream_b_slug, stream_b_positions, stream_b_res):
    with open(config_path, "r") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("leds", {})
    cfg["leds"].setdefault(camera_name, {})
    cfg["leds"][camera_name][stream_a_slug] = {
        "frame_width": stream_a_res[0], "frame_height": stream_a_res[1], "positions": stream_a_positions,
    }
    cfg["leds"][camera_name][stream_b_slug] = {
        "frame_width": stream_b_res[0], "frame_height": stream_b_res[1], "positions": stream_b_positions,
    }
    with open(config_path, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)


def load_led_positions(config_path, camera_name, stream_a_slug, stream_a_res, stream_b_slug, stream_b_res):
    """stream_a_res/stream_b_res are (width, height) tuples for the
    CURRENTLY-picked stream resolution - checked against what
    update_config_leds stored at calibration time, since Stream Select lets
    an operator freely pick any resolution and silently sampling calibrated
    pixel coordinates against a differently-sized live frame produces
    garbage position_gap_ms results with no warning otherwise."""
    with open(config_path, "r") as f:
        cfg = yaml.safe_load(f)
    leds_by_camera = cfg.get("leds", {})
    camera_entry = leds_by_camera.get(camera_name, {})
    if stream_a_slug not in camera_entry or stream_b_slug not in camera_entry:
        raise KeyError(
            "No LED calibration yet for camera {!r} streams {!r}/{!r} - run calibration with "
            "this exact stream pair first.".format(camera_name, stream_a_slug, stream_b_slug)
        )

    for slug, current_res in ((stream_a_slug, stream_a_res), (stream_b_slug, stream_b_res)):
        entry = camera_entry[slug]
        stored_res = (entry["frame_width"], entry["frame_height"])
        if stored_res != tuple(current_res):
            raise RuntimeError(
                "Calibration for camera {!r} stream {!r} was done at resolution {}, but the "
                "currently-picked resolution is {} - re-run calibration at the current "
                "resolution before starting a live session.".format(
                    camera_name, slug, stored_res, tuple(current_res)
                )
            )

    return camera_entry[stream_a_slug]["positions"], camera_entry[stream_b_slug]["positions"]
