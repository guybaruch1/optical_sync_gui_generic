"""Pure image/math helpers shared across the optical-sync GUI.

Ported from optical_sync_poc_/realsense_utils.py. Everything here is
stateless and hardware-free on purpose - functions that talk to
pyrealsense2 sensors/devices live in engine/streams.py instead, so this
module can be unit-tested with plain numpy arrays.
"""

import cv2
import numpy as np
import pyrealsense2 as rs


def sample_neighborhood_brightness(image, x, y, size=5):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    half = size // 2
    xi, yi = int(round(x)), int(round(y))
    x0, x1 = max(0, xi - half), min(gray.shape[1], xi + half + 1)
    y0, y1 = max(0, yi - half), min(gray.shape[0], yi + half + 1)
    patch = gray[y0:y1, x0:x1]
    return float(patch.mean())


def safe_neighborhood_size(xy_positions, configured_size, min_size=3, spacing_fraction=0.5):
    """Caps `configured_size` (settings.yaml's calibration.neighborhood_size/
    test.neighborhood_size) at a safe fraction of the REAL measured LED
    spacing for this specific run, so the brightness-sampling window used by
    sample_neighborhood_brightness/sample_all_neighborhood_brightness can
    never geometrically reach into a neighboring LED's pixels - regardless
    of whatever resolution/ROI/stream pairing this run happens to use.
    Only ever shrinks the configured value, never grows it (a small
    configured window stays small even at generous LED spacing). Falls
    back to `configured_size` unchanged when there are fewer than two LED
    positions to measure a spacing from. Called with the same real
    xy_positions at both calibration time and live-session start, so the
    two can't silently diverge even though they're computed separately."""
    spacing = _typical_spacing(list(xy_positions))
    if spacing is None:
        return configured_size
    safe_size = max(min_size, int(spacing * spacing_fraction))
    return min(configured_size, safe_size)


def sample_all_neighborhood_brightness(image, xy_positions, size=5):
    """Like sample_neighborhood_brightness, but for many LED positions on
    the same frame - converts BGR to grayscale once up front instead of
    once per LED. Calling sample_neighborhood_brightness directly in a
    per-LED loop re-converts the same full-resolution frame on every call;
    at num_leds=100 that was slow enough for the live-session acquisition
    loop to fall behind the camera's real fps and self-induce frame drops
    (confirmed from a real run's HW timestamps: consecutive intervals were
    exact 2x/3x multiples of the camera's true frame interval, never a
    fuzzy in-between value - the signature of the loop being too slow to
    call wait_for_frames() again in time, not a hardware/config issue)."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    return np.array([sample_neighborhood_brightness(gray, x, y, size) for (x, y) in xy_positions])


def apply_roi_mask(image, roi):
    x, y, w, h = roi
    mask = np.zeros_like(image)
    mask[y:y + h, x:x + w] = image[y:y + h, x:x + w]
    return mask


def crop_to_roi(image, roi):
    """Unlike apply_roi_mask (same full frame size, everything outside the
    ROI zeroed), this actually crops down to the ROI's own dimensions - for
    display contexts (the live session's video panels) where the ROI
    region is the only part worth showing at all, not just highlighting it
    within the full frame."""
    x, y, w, h = roi
    # .copy() - a plain slice is a non-contiguous view into the original
    # array, which QImage construction (VideoPanel.set_frame) can't read
    # directly (confirmed: raises BufferError: underlying buffer is not
    # C-contiguous without this).
    return image[y:y + h, x:x + w].copy()


def _typical_spacing(points):
    """Median nearest-neighbor distance across points, or None if there are
    fewer than two points to compare (no neighbor distance is computable).
    Shared by merge_close_centroids (clustering threshold) and the debug
    overlay drawers (scaling the drawn circle radius down so adjacent
    circles don't overlap when real LEDs are packed tightly)."""
    if len(points) < 2:
        return None

    pts = np.array(points)
    nn_dists = []
    for i in range(len(pts)):
        d = np.linalg.norm(pts - pts[i], axis=1)
        d[i] = np.inf
        nn_dists.append(d.min())
    return float(np.median(nn_dists))


def _debug_circle_radius(points):
    """Derives the debug-overlay circle radius from the typical spacing
    between the given points, capped at the original fixed 8px (never
    bigger than before) and floored at 2px (never vanishes), so adjacent
    circles stay visually distinct instead of overlapping into a blob when
    LEDs are packed closer than ~16px apart. Falls back to the original
    fixed 8px when there are fewer than two points to compare."""
    spacing = _typical_spacing(points)
    if spacing is None:
        return 8
    return max(2, min(8, int(spacing * 0.3)))


def merge_close_centroids(centroids, distance_fraction=0.5):
    if len(centroids) < 2:
        return centroids

    pts = np.array(centroids)
    typical_spacing = _typical_spacing(centroids)
    merge_threshold = typical_spacing * distance_fraction

    merged = []
    used = np.zeros(len(pts), dtype=bool)
    for i in range(len(pts)):
        if used[i]:
            continue
        d = np.linalg.norm(pts - pts[i], axis=1)
        cluster_idx = np.where((d < merge_threshold) & (~used))[0]
        used[cluster_idx] = True
        merged.append(tuple(pts[cluster_idx].mean(axis=0)))
    return merged


def detect_led_centroids(image, threshold, min_area):
    """threshold=None: automatic Otsu (this function's original, and still
    default, behavior). threshold=<int 0-255>: a fixed manual value instead
    - lets a caller override Otsu when it picks badly for a given frame's
    actual exposure/contrast (gui/pages/threshold_tuning_page.py's LED
    Detection Threshold Tuning section; matches tools/panel_drift/
    panel_drift_calibrate.py's own _detect_centroids_at_threshold exactly).
    Returns (centroids, chosen_threshold) either way - chosen_threshold is
    Otsu's own computed value in the first case, an echo of the given
    threshold in the second (cv2.threshold's own return value already
    provides this, no special-casing needed)."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image.copy()
    if threshold is None:
        chosen_threshold, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    else:
        chosen_threshold, binary = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)
    kernel = np.ones((3, 3), np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    centroids = []
    for cnt in contours:
        if cv2.contourArea(cnt) < min_area:
            continue
        (cx, cy), _ = cv2.minEnclosingCircle(cnt)
        centroids.append((cx, cy))
    return centroids, chosen_threshold


DECODERS = {
    # Infrared formats
    rs.format.y8:    lambda b, w, h: np.frombuffer(b, np.uint8).reshape((h, w)).copy(),
    # NOTE: rs.format.y16 is deliberately NOT included, even though D400
    # stereo modules advertise it - it decodes to a uint16 array, and
    # nothing downstream (detect_led_centroids's cv2 calls, VideoPanel.
    # set_frame's QImage.Format_Grayscale8 assumption) handles anything but
    # 8-bit. engine/streams.py's list_video_stream_options_from_device
    # filters the Stream Select picker down to formats present in this dict
    # for exactly this reason - don't re-add y16 here without also fixing
    # every 8-bit-only consumer downstream.
    # Color formats — a color sensor can report any of these depending on model/driver
    rs.format.yuyv:  lambda b, w, h: cv2.cvtColor(np.frombuffer(b, np.uint8).reshape((h, w, 2)), cv2.COLOR_YUV2BGR_YUYV),
    rs.format.uyvy:  lambda b, w, h: cv2.cvtColor(np.frombuffer(b, np.uint8).reshape((h, w, 2)), cv2.COLOR_YUV2BGR_UYVY),
    rs.format.bgr8:  lambda b, w, h: np.frombuffer(b, np.uint8).reshape((h, w, 3)).copy(),
    rs.format.rgb8:  lambda b, w, h: cv2.cvtColor(np.frombuffer(b, np.uint8).reshape((h, w, 3)), cv2.COLOR_RGB2BGR),
    rs.format.bgra8: lambda b, w, h: cv2.cvtColor(np.frombuffer(b, np.uint8).reshape((h, w, 4)), cv2.COLOR_BGRA2BGR),
    rs.format.rgba8: lambda b, w, h: cv2.cvtColor(np.frombuffer(b, np.uint8).reshape((h, w, 4)), cv2.COLOR_RGBA2BGR),
    rs.format.mjpeg: lambda b, w, h: cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR),
}


def decode_frame(raw_bytes, fmt, width, height):
    if fmt not in DECODERS:
        raise RuntimeError(f"No decoder for format {fmt} - pick a different format in Stream Select, or add one to DECODERS.")
    return DECODERS[fmt](raw_bytes, width, height)


# How much save_debug_detection_image upscales the base image before drawing
# anything - the native cropped-ROI frame is only a few pixels per LED for
# a dense grid (e.g. 100 LEDs), nowhere near enough to render a 1-3 digit
# led_id legibly at any font scale, and naive zooming in an image viewer
# just blurs/pixelates a PNG that never had the resolution to begin with.
# Confirmed on real hardware: an operator trying to visually compare
# led_id numbering direction between two streams' debug images (checking
# whether Calibration's row-major numbering assumption actually matches
# between them) could not read the printed numbers at all, even zoomed in.
_DEBUG_IMAGE_SCALE = 4


def save_debug_detection_image(image, centroids, path):
    debug_img = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR) if len(image.shape) == 2 else image.copy()
    # Radius is derived from the PRE-scale spacing between points (the
    # existing spacing-based sizing logic is unchanged) - only the final
    # drawn radius/coordinates/font get scaled up, so a tightly-packed grid
    # still gets small-but-now-legible circles, not disproportionately
    # huge ones relative to LED spacing.
    radius = _debug_circle_radius(centroids) * _DEBUG_IMAGE_SCALE
    # INTER_NEAREST, not a smoothing interpolation - this is a verification
    # image, not a presentation one; nearest-neighbor keeps the original
    # LED-blob edges crisp instead of blurring them across the upscale.
    debug_img = cv2.resize(debug_img, None, fx=_DEBUG_IMAGE_SCALE, fy=_DEBUG_IMAGE_SCALE,
                            interpolation=cv2.INTER_NEAREST)
    # Text thickness also scaled - the original fixed 1px stroke would look
    # spindly/thin next to a font this much bigger, hurting the very
    # legibility this scale-up exists for.
    text_thickness = max(1, _DEBUG_IMAGE_SCALE // 2)
    for i, (x, y) in enumerate(centroids):
        scaled_x, scaled_y = int(x) * _DEBUG_IMAGE_SCALE, int(y) * _DEBUG_IMAGE_SCALE
        cv2.circle(debug_img, (scaled_x, scaled_y), radius, (0, 255, 0), 1)
        cv2.putText(debug_img, str(i), (scaled_x + 10 * _DEBUG_IMAGE_SCALE, scaled_y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4 * _DEBUG_IMAGE_SCALE, (0, 0, 255), text_thickness)
    cv2.imwrite(path, debug_img)


def draw_detected_centroids(image, centroids):
    """Circles-only draw (no numbering, no disk write) - for
    gui/pages/threshold_tuning_page.py's LED Detection Threshold Tuning
    live per-tick preview, where a fresh detection needs to be shown on
    every slider drag. Deliberately independent of save_debug_detection_image
    (not called by it, doesn't call it) rather than a shared circles-drawing
    step factored out of it - that function's existing per-point
    circle-then-text interleaving must stay byte-identical for its existing
    callers, and a two-pass "draw all circles, then all text" refactor could
    change the final pixels wherever a later LED's circle overlaps an
    earlier one's number label at tight spacing. Small, already-idiomatic
    duplication (draw_led_state_overlay below already repeats the same
    grayscale-conversion prelude independently) traded for that guarantee."""
    debug_img = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR) if len(image.shape) == 2 else image.copy()
    radius = _debug_circle_radius(centroids)
    for (x, y) in centroids:
        cv2.circle(debug_img, (int(x), int(y)), radius, (0, 255, 0), 1)
    return debug_img


def draw_led_state_overlay(image, xy_positions, on_mask):
    """Debug snapshot for the live session: circles each calibrated LED
    position green if the live threshold classification says it's on right
    now, red if off - lets you visually confirm PositionGapMetric's on/off
    call is actually correct for a given frame, the same way
    save_debug_detection_image lets calibration's blob detection be
    sanity-checked."""
    debug_img = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR) if len(image.shape) == 2 else image.copy()
    radius = _debug_circle_radius(xy_positions)
    for (x, y), is_on in zip(xy_positions, on_mask):
        color = (0, 255, 0) if is_on else (0, 0, 255)  # BGR: green=on, red=off
        cv2.circle(debug_img, (int(x), int(y)), radius, color, 2)
    return debug_img


def combine_side_by_side(image_a, image_b, gap_px=10, gap_color=(60, 60, 60)):
    """Combines two BGR debug images into one, Stream A on the left/Stream B
    on the right, separated by a thin gap column - lets a single saved PNG
    be cross-checked at a glance instead of two separate files for the same
    pair_index. Stream A/B can be different resolutions (e.g. an infrared
    stream vs. a color stream), so the shorter image is vertically letterboxed
    (padded with gap_color, not stretched/resized) to match the taller one's
    height before concatenating - resizing would distort one stream's pixel
    scale relative to the other's, misleading anyone comparing LED positions
    across the two halves."""
    height = max(image_a.shape[0], image_b.shape[0])

    def _pad_to_height(image):
        if image.shape[0] == height:
            return image
        pad = height - image.shape[0]
        top = pad // 2
        bottom = pad - top
        return cv2.copyMakeBorder(image, top, bottom, 0, 0, cv2.BORDER_CONSTANT, value=gap_color)

    gap = np.full((height, gap_px, 3), gap_color, dtype=np.uint8)
    return np.hstack([_pad_to_height(image_a), gap, _pad_to_height(image_b)])


def draw_bundle_overlay(image, bundle_index, stream_a_frame_number, stream_b_frame_number, stream_a_ts_us, stream_b_ts_us, delta_us):
    """Burns a live pairing-quality diagnostic overlay (bundle counter,
    each stream's own HW frame number, HW timestamps, and their delta) onto
    a copy of the given frame - used by the Stream Config page's live
    preview so pairing quality can be sanity-checked by eye for a given
    resolution/fps before committing to it in the wizard."""
    debug_img = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR) if len(image.shape) == 2 else image.copy()
    font = cv2.FONT_HERSHEY_SIMPLEX
    lines = [
        ("Bundle: {}".format(bundle_index), (0, 255, 0)),
        ("Stream A Frame: {}  |  Stream B Frame: {}".format(stream_a_frame_number, stream_b_frame_number), (0, 255, 255)),
        ("Stream A Timestamp: {:.0f}  |  Stream B Timestamp: {:.0f}".format(stream_a_ts_us, stream_b_ts_us), (0, 255, 255)),
        ("Delta: {:.1f} us".format(delta_us), (255, 255, 0)),
    ]
    y = 25
    for text, color in lines:
        cv2.putText(debug_img, text, (10, y), font, 0.6, color, 2)
        y += 25
    return debug_img
