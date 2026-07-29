"""Pure image/math helpers shared across the optical-sync GUI.

Ported from optical_sync_poc_/realsense_utils.py. Everything here is
stateless and hardware-free on purpose - functions that talk to
pyrealsense2 sensors/devices live in engine/streams.py instead, so this
module can be unit-tested with plain numpy arrays.
"""

import cv2
import numpy as np


def sample_neighborhood_brightness(image, x, y, size=5):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    half = size // 2
    xi, yi = int(round(x)), int(round(y))
    x0, x1 = max(0, xi - half), min(gray.shape[1], xi + half + 1)
    y0, y1 = max(0, yi - half), min(gray.shape[0], yi + half + 1)
    patch = gray[y0:y1, x0:x1]
    return float(patch.mean())


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


def merge_close_centroids(centroids, distance_fraction=0.5):
    if len(centroids) < 2:
        return centroids

    pts = np.array(centroids)

    nn_dists = []
    for i in range(len(pts)):
        d = np.linalg.norm(pts - pts[i], axis=1)
        d[i] = np.inf
        nn_dists.append(d.min())
    typical_spacing = np.median(nn_dists)
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
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image.copy()
    chosen_threshold, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
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


def ir_bytes_to_image(raw_bytes, width, height):
    return np.frombuffer(raw_bytes, dtype=np.uint8).reshape((height, width)).copy()


def yuyv_to_bgr(raw_bytes, width, height):
    arr = np.frombuffer(raw_bytes, dtype=np.uint8).reshape((height, width, 2))
    return cv2.cvtColor(arr, cv2.COLOR_YUV2BGR_YUYV)


def save_debug_detection_image(image, centroids, path):
    debug_img = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR) if len(image.shape) == 2 else image.copy()
    for i, (x, y) in enumerate(centroids):
        cv2.circle(debug_img, (int(x), int(y)), 8, (0, 255, 0), 1)
        cv2.putText(debug_img, str(i), (int(x) + 10, int(y)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.4, (0, 0, 255), 1)
    cv2.imwrite(path, debug_img)


def draw_led_state_overlay(image, xy_positions, on_mask):
    """Debug snapshot for the live session: circles each calibrated LED
    position green if the live threshold classification says it's on right
    now, red if off - lets you visually confirm PositionGapMetric's on/off
    call is actually correct for a given frame, the same way
    save_debug_detection_image lets calibration's blob detection be
    sanity-checked."""
    debug_img = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR) if len(image.shape) == 2 else image.copy()
    for (x, y), is_on in zip(xy_positions, on_mask):
        color = (0, 255, 0) if is_on else (0, 0, 255)  # BGR: green=on, red=off
        cv2.circle(debug_img, (int(x), int(y)), 8, color, 2)
    return debug_img


def draw_bundle_overlay(image, bundle_index, ir_frame_number, color_frame_number, ir_ts_us, color_ts_us, delta_us):
    """Burns a live pairing-quality diagnostic overlay (bundle counter,
    each stream's own HW frame number, HW timestamps, and their delta) onto
    a copy of the given frame - used by the Stream Config page's live
    preview so pairing quality can be sanity-checked by eye for a given
    resolution/fps before committing to it in the wizard."""
    debug_img = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR) if len(image.shape) == 2 else image.copy()
    font = cv2.FONT_HERSHEY_SIMPLEX
    lines = [
        ("Bundle: {}".format(bundle_index), (0, 255, 0)),
        ("IR Frame: {}  |  Color Frame: {}".format(ir_frame_number, color_frame_number), (0, 255, 255)),
        ("IR Timestamp: {:.0f}  |  Color Timestamp: {:.0f}".format(ir_ts_us, color_ts_us), (0, 255, 255)),
        ("Delta: {:.1f} us".format(delta_us), (255, 255, 0)),
    ]
    y = 25
    for text, color in lines:
        cv2.putText(debug_img, text, (10, y), font, 0.6, color, 2)
        y += 25
    return debug_img
