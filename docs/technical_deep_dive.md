# Optical Sync GUI — Technical Deep Dive: Algorithms & Math

Companion to `docs/project_overview.md`. That document explains what the app
does and why; this one derives every formula it actually computes, with the
exact code location, the reasoning behind each constant, and worked numeric
examples for the parts that aren't obvious from the formula alone.

## 1. The physical setup, in one paragraph

The LED panel has `num_leds` individually addressable LEDs (`settings.yaml`:
`test.num_leds`, default 100), wired in a fixed physical sequence. It scans
through them one at a time — LED *i* on, then off, then LED *i+1* on — each
held for `switch_time_ms` before advancing (`LEDPanel.set_speed_ms`,
`engine/led_panel.py`). Both cameras watch the same physical panel. Everything
below is about turning "which LED does each camera currently see lit" and
"when did each camera's hardware actually timestamp its frame" into two
numbers describing sync quality.

## 2. Per-LED brightness sampling

`domain/realsense_utils.py:13` `sample_neighborhood_brightness(image, x, y, size)`:

```
half = size // 2
patch = grayscale[y-half : y+half+1, x-half : x+half+1]   # clamped at image edges
brightness = mean(patch)
```

An `size × size` window (default 5×5, `test.neighborhood_size`), not a single
pixel — averages out sensor noise and sub-pixel LED-position error from
calibration. `sample_all_neighborhood_brightness` (`:23`) converts BGR→grayscale
**once** per frame and calls this per LED, rather than once per LED per frame —
at `num_leds=100` the redundant per-LED conversion was measurably slow enough
to make the acquisition loop fall behind real time and self-induce frame drops
(see `_is_frame_drop` below for how that's detected).

## 3. Calibration: per-LED on/off threshold

`domain/calibration.py:37` `build_positions_with_thresholds`, run once during
the Calibration wizard step, with the panel fully **on** and fully **off**:

```
on_value  = sample_neighborhood_brightness(on_frame,  x, y, neighborhood_size)
off_value = sample_neighborhood_brightness(off_frame, x, y, neighborhood_size)
threshold = off_value + 0.5 * (on_value - off_value)     # the midpoint
```

Per-LED, not one global constant — brightness varies across the panel with
exposure, angle, and lens vignetting, so a single global cutoff would
misclassify LEDs near the panel's edges.

## 4. Live threshold: rescaled for switch speed

Calibration's midpoint assumes an LED held on for a **full exposure**. A live
session can run `switch_time_ms` far below the camera's frame interval (that's
the point of the test — see §9), so within one exposure window an LED is only
lit for a *fraction* of it, and never reaches its calibrated `on_value`.
`gui/main_window.py`'s `_on_calibration_done` recomputes the threshold used
during the actual test:

```
ir_threshold  = ir_off  + threshold_fraction * (ir_on  - ir_off)
rgb_threshold = rgb_off + threshold_fraction * (rgb_on - rgb_off)
```

`threshold_fraction` (`settings.yaml`: `test.threshold_fraction`, default
`0.25`) is deliberately **below** calibration's fixed `0.5` — tune it down
further for faster switch speeds relative to the camera's exposure time, up
towards `0.5` if `switch_time_ms` is closer to a full frame interval.

## 5. On/off classification, live

`engine/metrics.py:172-173`, every frame pair:

```
ir_on  = ir_bright  > ir_threshold     # elementwise, one bool per LED
rgb_on = rgb_bright > rgb_threshold
```

`ir_bright`/`rgb_bright` are the per-LED brightness arrays from §2, sampled at
the calibrated `ir_xy`/`rgb_xy` pixel positions this exact frame.

## 6. Finding the "current" LED: `find_last_on_led`

`engine/metrics.py:45`. At any instant, either exactly one LED is truly on, or
a short contiguous run is (multiple adjacent LEDs can read "on" simultaneously
when `switch_time_ms` is fast relative to exposure — the shutter integrates
across the LED transition). The algorithm:

1. Collect the indices where `on` is `True`.
2. Group them into maximal contiguous runs (`i` and `i+1` in the same run).
3. **Wraparound check**: the LED sequence is circular (the scan wraps from the
   last LED back to LED 0). If the *first* run starts at index `0` **and** the
   *last* run ends at index `n-1`, those two runs may really be one run that
   straddles the wrap point — e.g. LEDs `98, 99, 0, 1` all reading on because
   the scan just wrapped past the end. The algorithm stitches them into one
   "wrap" candidate with their combined length.
4. Pick the **longest** run (wrap candidate included) — the true currently-lit
   LED produces the longest on-streak; a shorter run elsewhere is more likely
   sampling/threshold noise.
5. Return the **last** index in that run (not the first, not the middle) — the
   most-recently-lit LED in the run is the best estimate of "where the scan is
   right now."

Worked wraparound example (`n = 100`): `on` is `True` at indices
`{97, 98, 99, 0, 1}`. `np.where(on)[0]` returns array positions in ascending
numeric order regardless of scan order, i.e. `[0, 1, 97, 98, 99]`, so the runs
get built as `(0, 1)` (listed first) then `(97, 99)` (listed last). That makes
`runs[0][0] == 0` and `runs[-1][1] == n-1` both true, so the wraparound branch
fires: the two runs stitch into one candidate of combined length
`(1-0+1) + (99-97+1) = 2 + 3 = 5`, and the function returns index `1` (the end
of the run-starting-at-0 half) with a reported run length of 5. Index 1 is
correct — the most recently lit LED given this on-set really is LED 1. The
stitching's actual purpose is making this 5-LED streak win against any
unrelated 2-3 LED noise blob elsewhere on the panel that would otherwise be
mistaken for the true position; it doesn't change which index a non-wrapping
run would already report.

## 7. Position Gap / "Optical Sync": `compute_position_gap`

`engine/metrics.py:95`. Given each stream's last-lit LED index:

```
diff = ir_last - rgb_last
half = n / 2
if diff > half:   diff -= n
if diff <= -half: diff += n
return diff
```

This maps the raw index difference onto the **shortest signed distance**
around the circular sequence — without it, a scan that's *actually* only 3
LEDs apart could report a raw diff of 97 (out of 100) just because it crossed
the wrap point between the two captures.

Worked example (`n = 100`): `ir_last = 2`, `rgb_last = 97` →
`raw diff = 2 - 97 = -95`. Since `-95 ≤ -50` (half), add `n`: `-95 + 100 = 5`.
Correct answer: IR's position is 5 LEDs ahead of RGB's in scan order, not 95
behind.

Finally, `engine/metrics.py:183`:

```
gap_ms = diff * switch_time_ms
```

converts an LED-index difference into a time difference, using the panel's
actual configured dwell time per LED (read live from the toolbar — see
`live_session_page.py`'s `switch_time_spinbox`, not a fixed config value).

**Exclusion reasons** (`PositionGapMetric.update`, in priority order): a value
is still computed and recorded either way (never silently dropped from the
CSV), but flagged:
- `no_led_data` — brightness sampling wasn't available at all this frame.
- `miss` — one or both streams show *no* LED on (`find_last_on_led` returned
  `None`) — panel likely between visible states, or occlusion.
- `frame_drop` — see §9 — a dropped frame invalidates the physical pairing
  assumption for this sample.
- `warmup` — the first `warmup_pairs_to_skip` pairs of a run (default 15) are
  recorded but excluded from live stats, to avoid auto-exposure convergence or
  an initial buffered-frame burst reading as a real gap or drop.

## 8. HW TS Latency / "Pairing Gap": `PairingGapMetric`

`engine/metrics.py:105-119`. Far simpler — no LED math at all, purely the two
streams' own hardware-reported capture timestamps:

```
gap = ir_ts_us - rgb_ts_us
excluded = abs(gap) > outlier_threshold_us     # settings.yaml: 100_000 (100ms)
```

`ir_ts_us`/`rgb_ts_us` come from `rs.frame_metadata_value.frame_timestamp` —
the camera hardware's own per-frame timestamp, not a value computed from when
Python happened to read the frame off the USB bus. This is what makes it a
genuinely independent cross-check against §7's optical measurement: one is
"what the camera's internal clock says," the other is "what the LED panel
image itself shows," and they can disagree if there's a clock/driver-level
skew that doesn't show up in actual capture timing (or vice versa).

## 9. Frame drop detection: `_is_frame_drop`

`engine/metrics.py:122-132`, run independently per stream every frame pair:

```
delta = curr_ts - prev_ts
expected_delta = 1_000_000.0 / fps          # microseconds per frame at nominal fps
is_drop = delta < 0 or delta > expected_delta * threshold_factor
```

`threshold_factor` (`settings.yaml`: `test.frame_drop_threshold_factor`,
default `1.5`) — a gap more than 1.5× the expected inter-frame interval means
at least one frame was very likely skipped between the two that were actually
received. `delta < 0` catches a timestamp counter wraparound/reset case a pure
magnitude check would miss.

Worked example: `fps = 30` → `expected_delta ≈ 33333.3 µs`. A measured
`delta = 51000 µs` → `51000 / 33333.3 ≈ 1.53 > 1.5` → flagged as a drop.

This is also literally how a real, otherwise-mysterious **self-induced
frame-drop** bug in this project was root-caused (a separate issue from an
unrelated GUI-freeze bug caused by unthrottled plot updates — see
`CLAUDE.md`'s "Live Session pipeline" section for that one): consecutive
HW-timestamp intervals in a captured run were exact 2×/3× multiples of the
true frame interval — the unmistakable signature of the acquisition loop
being too slow to call `wait_for_frames()` again in time, not a
hardware/config issue. The actual cause was §2's per-LED brightness sampling
re-converting the same full-resolution frame to grayscale on every single LED
lookup; batching that conversion once per frame (see
`sample_all_neighborhood_brightness`) fixed it.

## 10. Running Stats — Welford's online algorithm

`domain/running_stats.py`, backing the Live Session "Stats" table's live
min/avg/std/max. Updates in **O(1) time and memory per sample** — no history
array is ever stored, which matters because this updates on every single frame
pair, unthrottled (same cadence as the frame-drop counters):

```
count += 1
delta  = value - mean
mean  += delta / count
delta2 = value - mean            # note: uses the UPDATED mean
M2    += delta * delta2
variance = M2 / count            # population variance (÷n, not n-1)
std = sqrt(variance)
min = value if min is None else min(min, value)
max = value if max is None else max(max, value)
```

This is the standard Welford formulation for numerically stable incremental
mean/variance (avoids the catastrophic cancellation of a naive
`Σx² / n − (Σx / n)²` running-sum approach over a long session). It reports
**population** variance/std (dividing by `n`), not the Bessel-corrected sample
variance (`n-1`) — appropriate here since this is a live monitoring statistic
over the run's own actual data, not an estimate of some larger population
being inferred from a sample.

## 11. LED blob detection (calibration image processing)

`domain/realsense_utils.py:85` `detect_led_centroids(image, threshold, min_area)`:

1. Grayscale conversion (if not already single-channel).
2. **Otsu auto-thresholding**: `cv2.threshold(gray, 0, 255, THRESH_BINARY + THRESH_OTSU)`
   — picks its own binary cutoff by minimizing intra-class pixel-intensity
   variance between the two classes it produces, rather than using a
   hand-picked constant. (The `threshold` parameter is accepted but unused —
   Otsu's own computed threshold, `chosen_threshold`, is what's actually
   applied and returned for debugging.)
3. Morphological **open** (erode then dilate, 3×3 kernel) — strips
   single-pixel noise blobs before contour detection.
4. `cv2.findContours` (external contours only) → one contour per surviving
   bright region.
5. Discard any contour with `cv2.contourArea < min_area` (`settings.yaml`:
   `calibration.min_blob_area`, default 20 px²).
6. Each survivor's centroid via `cv2.minEnclosingCircle`.

`merge_close_centroids` (`:59`) then collapses near-duplicates — one physical
LED occasionally splits into two adjacent blobs post-threshold:

```
nn_dist[i] = min over j≠i of ||point[i] - point[j]||     # nearest-neighbor distance
typical_spacing = median(nn_dist)                         # robust estimate of true LED pitch
merge_threshold = typical_spacing * distance_fraction     # distance_fraction defaults to 0.5
```

`distance_fraction` isn't a `settings.yaml` value — it's a Python default
(`0.5`) on the function itself, and `gui/pages/calibration_page.py` calls
`merge_close_centroids(ir_centroids)`/`(rgb_centroids)` without overriding it.

Any two centroids closer than `merge_threshold` are averaged into one point.
Using the **median** (not mean) nearest-neighbor distance makes this robust to
a few genuinely-close LED pairs at the panel's layout edges skewing the
estimate.

## 12. Grid ID assignment

`domain/calibration.py:14` `assign_grid_ids(centroids, row_gap_px)` — turns an
unordered list of detected `(x, y)` centroids into the panel's actual LED
numbering:

```
sort all points by y
start a new row whenever consecutive points' y-gap > row_gap_px
sort each row's points by x
number LEDs 0..N-1, row-major (row 0 left→right, then row 1, ...)
```

This assumes the panel is a rectangular grid scanned row-major — the
documented assumption behind `test.scan_direction: 1` in `settings.yaml`
("start top-left, scan left-to-right, wrapping row to row").

## 13. ROI: crop vs. mask (two different operations, same input)

`domain/realsense_utils.py` has two ROI functions that look similar but serve
different callers:

- `apply_roi_mask(image, roi)` (`:38`) — same full frame size, everything
  **outside** the ROI zeroed. Used where the surrounding frame context still
  matters.
- `crop_to_roi(image, roi)` (`:45`) — actually crops down to the ROI's own
  `(w, h)` dimensions. Used for the Live Session's video panels, where only the
  ROI region is worth displaying at all. Returns a `.copy()` of the slice, not
  a view — `QImage` construction requires a C-contiguous buffer, which a
  partial-row numpy slice isn't.

## Parameter quick-reference

| `settings.yaml` key | Used in | Effect of increasing it |
|---|---|---|
| `test.switch_time_ms` | §7 gap_ms scale, LED panel real scan speed | Coarser but more robust position-gap resolution; live-editable per run (toolbar) |
| `test.threshold_fraction` | §4 live on/off threshold | Higher = stricter "on" classification; must go lower as switch speed increases relative to exposure |
| `test.frame_drop_threshold_factor` | §9 drop detection | Higher = more tolerant of jitter, more likely to miss a real drop |
| `test.warmup_pairs_to_skip` | §7 exclusion | Higher = more auto-exposure settling time excluded from stats, less early data |
| `test.pairing_gap_outlier_threshold_us` | §8 exclusion | Higher = tolerates larger raw HW clock disagreement before flagging as unreliable |
| `calibration.min_blob_area` | §11 blob filtering | Higher = more resistant to noise, risks discarding real but dim/small LED detections |
| `calibration.row_gap_px` | §12 grid assignment | Must roughly match the panel's actual row pitch, or rows merge/split incorrectly |
| `calibration.neighborhood_size` | §2 brightness sampling | Larger = more noise-resistant, less resolution to fine ROI/position miscalibration |
