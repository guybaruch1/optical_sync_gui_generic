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


def centroids_in_grid_order(centroids, row_gap_px=15):
    """Reorders centroids into the exact row-major order assign_grid_ids
    itself assigns as led_id (index i in the returned list IS led_id i) -
    lets a debug-image caller number LEDs by their REAL grid ID instead of
    detect_led_centroids' raw, arbitrary contour-scan order, which bears no
    relation to the actual led_id config.yaml/Threshold Tuning/Live Session
    use for that same LED (an earlier version of the debug image drew that
    raw order directly, which just happened to look grid-like enough - row
    by row, but each row descending right-to-left with occasional
    neighbor swaps - to read as "wrong" rather than obviously arbitrary).

    Returns (ordered_centroids, positions, row_layout) - the same
    positions/row_layout assign_grid_ids itself returns, computed only
    once, so a caller needing both doesn't call assign_grid_ids twice.
    Raises the same RuntimeError as assign_grid_ids when centroids is
    empty - there's no grid order to produce in that case."""
    positions, row_layout = assign_grid_ids(centroids, row_gap_px)
    ordered = [tuple(positions[str(i)]) for i in range(len(positions))]
    return ordered, positions, row_layout


def offset_positions(positions, roi):
    """assign_grid_ids's/centroids_in_grid_order's centroids come from a
    crop_to_roi'd image, so they're in that CROPPED image's own coordinates
    (origin at the ROI's own top-left corner) - shifts every position back
    to full-frame coordinates by the ROI's own (x, y) origin, since
    build_positions_with_thresholds needs to sample brightness from the
    full-frame on/off images, and everything downstream (Threshold Tuning,
    Live Session) expects stream_a_xy/stream_b_xy in full-frame
    coordinates too. Shared by gui/pages/calibration_page.py's own
    detection flow and gui/pages/threshold_tuning_page.py's LED Detection
    Threshold Tuning retuning - moved here (was calibration_page.py-private)
    once a second caller needed it."""
    roi_x, roi_y = roi[0], roi[1]
    return {led_id: [x + roi_x, y + roi_y] for led_id, (x, y) in positions.items()}


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


def build_grid_positions(centroids, roi, on_frame, off_frame, row_gap_px, neighborhood_size):
    """Collapses the 3-step sequence both CalibrationPage's own auto-detect
    flow and ThresholdTuningPage's LED Detection Threshold Tuning retuning
    need, so neither duplicates it: grid-order assignment
    (centroids_in_grid_order) -> offset back to full-frame coordinates
    (offset_positions) -> per-LED on/off/threshold sampling
    (build_positions_with_thresholds). `centroids` must already be
    merge_close_centroids'd, in the CROPPED image's own coordinates (same
    coordinate space crop_to_roi/detect_led_centroids produce).

    Returns (positions, row_layout, debug_centroids) - positions is the
    final {led_id: [x, y, on, off, threshold]} dict (full-frame
    coordinates), debug_centroids is the grid-ordered (still cropped-frame)
    centroid list a caller can hand to save_debug_detection_image for
    correctly-numbered debug PNGs. Raises RuntimeError (propagated from
    centroids_in_grid_order) when centroids is empty - same contract as
    before this was extracted."""
    debug_centroids, positions, row_layout = centroids_in_grid_order(centroids, row_gap_px)
    positions = offset_positions(positions, roi)
    positions = build_positions_with_thresholds(positions, on_frame, off_frame, neighborhood_size)
    return positions, row_layout, debug_centroids


def compute_threshold(on_values, off_values, fraction):
    # Calibration itself (above) always assumes a full exposure and fixes
    # fraction at 0.5. At runtime - live-tunable per stream on the
    # Threshold Tuning wizard page - a faster LED switch time only reaches
    # a FRACTION of that calibrated brightness, so the live on/off cutoff
    # needs to be rescaled down from the same on/off values calibration
    # already measured, per stream (different sensors' brightness/exposure
    # characteristics differ, hence a per-stream fraction rather than one
    # shared value).
    return off_values + fraction * (on_values - off_values)


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
