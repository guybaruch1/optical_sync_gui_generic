# Optical Sync GUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a PySide6 desktop app that guides a user through device selection, stream configuration, ROI selection, LED calibration, and a live IR/RGB sync test (dual live video, live scrolling pairing-gap/position-gap plot, live stats sidebar, Start/Stop with optional duration, CSV export), reusing the domain logic from the existing `optical_sync_poc_` scripts.

**Architecture:** A pure-Python, hardware-free `domain/` layer (image/math helpers, calibration math) and `engine/` layer (metrics, CSV export, test session, LED panel control, hardware streaming) sit underneath a PySide6 `gui/` layer. A `QThread` wraps a plain-Python `AcquisitionLoop` so the actual frame-processing logic is unit-testable without Qt or hardware; the thread itself only translates loop callbacks into Qt signals.

**Tech Stack:** Python 3.13, PySide6, pyqtgraph, pyrealsense2, OpenCV (`opencv-python`), numpy, PyYAML, pytest.

## Global Constraints

- Project root: `C:\Users\gbaruch\scripts\Optical Sync\optical_sync_gui` (currently empty, not yet a git repo).
- Source of reusable logic: `C:\Users\gbaruch\scripts\Optical Sync\optical_sync_poc_` (`led_calibration.py`, `led_panel_cli.py`, `pipeline_sync_test_diff.py`, `realsense_utils.py`, `roi_picker.py`, `settings.py`, `settings.yaml`, `config.yaml`) — the active, current scripts per that project's `CLAUDE.md` (not the older `optical_sync_poc` or `optical_sync_poc_/scratch/` directories, which are retired/superseded).
- `led_panel_cli.py`'s `from utils.Log.Logger import get_test_logger` import is unresolvable in this environment (`utils` package is not installed anywhere on this machine — confirmed via `python -c "from utils.Log.Logger import get_test_logger"` failing with `ModuleNotFoundError`). Every task that ports `LEDPanel` logic must replace that import with the stdlib `logging` module — do not attempt to install or vendor `utils.Log.Logger`.
- No pytest suite exists to inherit conventions from; this plan establishes the test layout from scratch under `tests/`, mirroring package structure (`tests/domain/...`, `tests/engine/...`, `tests/gui/...`, `tests/state/...`).
- `sync_test.py` and `led_calibration_at_speed.py`, referenced in `pipeline_sync_test_diff.py`'s comments, were confirmed **out of scope**: they exist only under `optical_sync_poc_/scratch/` and the older, superseded `optical_sync_poc/` directory, and `optical_sync_poc_/CLAUDE.md`'s "Repository map" explicitly lists both as retired/superseded by `pipeline_sync_test_diff.py`. Nothing in this plan needs to port them.
- Hardware-touching code (RealSense device/sensor calls, `LED-Panel.exe` subprocess calls) cannot be exercised by automated tests in this environment. Per `optical_sync_poc_/CLAUDE.md`'s testing convention, every non-trivial pure function (math, thresholding, wrap-around arithmetic) still gets a synthetic unit test; hardware-facing code gets a documented manual verification step instead of a unit test.
- `settings.yaml` must never be overwritten by the GUI — it is read-only from the app's perspective. `config.yaml` continues to be fully rewritten (per camera-model sub-block) by the calibration step, unchanged from today's behavior.

---

## File Structure

```
optical_sync_gui/
  pytest.ini
  requirements.txt
  .gitignore
  main.py

  domain/
    __init__.py
    realsense_utils.py      # pure image/math helpers, no pyrealsense2 import
    calibration.py          # LED grid assignment + per-LED threshold math
    csv_export.py           # generic session-row -> CSV writer

  engine/
    __init__.py
    metrics.py               # FramePairSample, MetricResult, Metric, PairingGapMetric, PositionGapMetric
    test_session.py          # TestSessionConfig, TestSession (start/stop/duration/buffer)
    acquisition_loop.py       # AcquisitionLoop: pure Python, dependency-injected, no Qt/hardware
    led_panel.py              # forked LEDPanel (stdlib logging instead of utils.Log.Logger)
    streams.py                 # DeviceInfo, profile enumeration/matching, ContinuousCapture (hardware)
    session_engine.py          # QThread wrapper: wires real hardware + AcquisitionLoop -> Qt signals

  state/
    __init__.py
    gui_state.py                # GuiState dataclass, load/save gui_state.json

  gui/
    __init__.py
    main_window.py               # QStackedWidget wizard shell, Back/Next navigation
    pages/
      __init__.py
      device_select_page.py       # pick a connected RealSense device
      stream_config_page.py       # pick FPS/resolution for IR + RGB
      roi_select_page.py           # embedded live preview, draggable ROI boxes
      calibration_page.py          # runs LED calibration, shows live progress + debug images
      live_session_page.py         # dual video panels, live plot, stats sidebar, Start/Stop/duration
    widgets/
      __init__.py
      video_panel.py                # numpy frame -> QImage display widget
      live_plot.py                   # pyqtgraph scrolling plot, generic over named series
      stats_panel.py                  # generic live key/value readout list

  tests/
    conftest.py                       # shared QApplication fixture for widget tests
    domain/
      test_realsense_utils.py
      test_calibration.py
      test_csv_export.py
    engine/
      test_metrics.py
      test_test_session.py
      test_acquisition_loop.py
      test_led_panel.py
      test_streams.py
    state/
      test_gui_state.py
    gui/
      widgets/
        test_video_panel.py
        test_live_plot.py
        test_stats_panel.py
```

---

### Task 1: Project scaffolding

**Files:**
- Create: `requirements.txt`
- Create: `pytest.ini`
- Create: `.gitignore`
- Create: `domain/__init__.py`, `engine/__init__.py`, `state/__init__.py`, `gui/__init__.py`, `gui/pages/__init__.py`, `gui/widgets/__init__.py`
- Create: `smoke_test_window.py` (temporary, deleted at the end of this task)

**Interfaces:**
- Produces: an importable package tree (`domain`, `engine`, `state`, `gui`) rooted at the project directory, with `pytest` able to discover `tests/` and import project packages via `pythonpath = .`.

- [ ] **Step 1: Create `requirements.txt`**

```
pyrealsense2
opencv-python
numpy
PyYAML
PySide6
pyqtgraph
pytest
```

- [ ] **Step 2: Create `pytest.ini`**

```ini
[pytest]
pythonpath = .
testpaths = tests
```

- [ ] **Step 3: Create `.gitignore`**

```
__pycache__/
*.pyc
.pytest_cache/
output/
gui_state.json
.venv/
```

- [ ] **Step 4: Create empty package `__init__.py` files**

```bash
mkdir -p domain engine state gui/pages gui/widgets tests/domain tests/engine tests/state tests/gui/widgets
touch domain/__init__.py engine/__init__.py state/__init__.py gui/__init__.py gui/pages/__init__.py gui/widgets/__init__.py
```

- [ ] **Step 5: Install dependencies**

Run: `pip install -r requirements.txt`
Expected: all packages install without error (PySide6 and pyqtgraph are new; `pyrealsense2`, `opencv-python`, `numpy`, `PyYAML` are already present on this machine per the sibling `optical_sync_poc_` project).

- [ ] **Step 6: Smoke-test that PySide6 actually opens a window on this machine**

Create `smoke_test_window.py`:

```python
import sys
from PySide6.QtWidgets import QApplication, QLabel

app = QApplication(sys.argv)
label = QLabel("PySide6 smoke test - close this window to finish")
label.resize(400, 100)
label.show()
sys.exit(app.exec())
```

Run: `python smoke_test_window.py`
Expected: a window titled with the label text appears; closing it exits cleanly with no traceback.

- [ ] **Step 7: Delete the smoke-test file**

```bash
rm smoke_test_window.py
```

- [ ] **Step 8: Initialize git and commit scaffolding**

```bash
git init
git add requirements.txt pytest.ini .gitignore domain engine state gui tests
git commit -m "chore: scaffold optical_sync_gui project structure"
```

---

### Task 2: `domain/realsense_utils.py` — pure image/math helpers

**Files:**
- Create: `domain/realsense_utils.py`
- Test: `tests/domain/test_realsense_utils.py`

**Interfaces:**
- Produces:
  - `sample_neighborhood_brightness(image: np.ndarray, x: float, y: float, size: int = 5) -> float`
  - `apply_roi_mask(image: np.ndarray, roi: tuple[int, int, int, int]) -> np.ndarray`
  - `merge_close_centroids(centroids: list[tuple[float, float]], distance_fraction: float = 0.5) -> list[tuple[float, float]]`
  - `detect_led_centroids(image: np.ndarray, threshold, min_area: int) -> tuple[list[tuple[float, float]], float]`
  - `ir_bytes_to_image(raw_bytes: bytes, width: int, height: int) -> np.ndarray`
  - `yuyv_to_bgr(raw_bytes: bytes, width: int, height: int) -> np.ndarray`

These are ported verbatim (same behavior) from `optical_sync_poc_/realsense_utils.py`, minus everything that imports `pyrealsense2` (that hardware-facing half moves to `engine/streams.py` in Task 10).

- [ ] **Step 1: Write the failing tests**

Create `tests/domain/test_realsense_utils.py`:

```python
import numpy as np
from domain.realsense_utils import (
    sample_neighborhood_brightness,
    apply_roi_mask,
    merge_close_centroids,
    detect_led_centroids,
    ir_bytes_to_image,
    yuyv_to_bgr,
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


def test_apply_roi_mask_zeroes_outside_box():
    image = np.full((10, 10, 3), 255, dtype=np.uint8)
    masked = apply_roi_mask(image, (2, 2, 3, 3))
    assert masked[0, 0].tolist() == [0, 0, 0]
    assert masked[3, 3].tolist() == [255, 255, 255]
    assert masked.shape == image.shape


def test_merge_close_centroids_merges_nearby_points():
    # nearest-neighbor distances here are [1.0, 1.0, ~55.9], so the median
    # (typical_spacing) is 1.0; distance_fraction must exceed 1.0 for the
    # merge_threshold to exceed the 1.0 gap between the first two points.
    centroids = [(10.0, 10.0), (11.0, 10.0), (50.0, 50.0)]
    merged = merge_close_centroids(centroids, distance_fraction=1.5)
    assert len(merged) == 2


def test_merge_close_centroids_passthrough_below_two_points():
    assert merge_close_centroids([(1.0, 1.0)]) == [(1.0, 1.0)]


def test_detect_led_centroids_finds_bright_blob():
    image = np.zeros((50, 50), dtype=np.uint8)
    image[20:30, 20:30] = 255
    centroids, chosen_threshold = detect_led_centroids(image, None, min_area=20)
    assert len(centroids) == 1
    cx, cy = centroids[0]
    assert 20 <= cx <= 30
    assert 20 <= cy <= 30


def test_ir_bytes_to_image_reshapes_correctly():
    raw = bytes(range(6))  # 2x3 image, 1 byte/pixel
    image = ir_bytes_to_image(raw, width=3, height=2)
    assert image.shape == (2, 3)
    assert image[0].tolist() == [0, 1, 2]
    assert image[1].tolist() == [3, 4, 5]


def test_yuyv_to_bgr_returns_correct_shape():
    width, height = 4, 2
    raw = bytes([128] * (width * height * 2))
    bgr = yuyv_to_bgr(raw, width, height)
    assert bgr.shape == (height, width, 3)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/domain/test_realsense_utils.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'domain.realsense_utils'` (or ImportError for each function).

- [ ] **Step 3: Write `domain/realsense_utils.py`**

```python
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


def apply_roi_mask(image, roi):
    x, y, w, h = roi
    mask = np.zeros_like(image)
    mask[y:y + h, x:x + w] = image[y:y + h, x:x + w]
    return mask


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/domain/test_realsense_utils.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add domain/realsense_utils.py tests/domain/test_realsense_utils.py
git commit -m "feat: port pure image/math helpers into domain/realsense_utils"
```

---

### Task 3: `domain/calibration.py` — LED grid assignment + threshold math

**Files:**
- Create: `domain/calibration.py`
- Test: `tests/domain/test_calibration.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (pure numpy/yaml logic).
- Produces:
  - `assign_grid_ids(centroids: list[tuple[float, float]], row_gap_px: int = 15) -> tuple[dict[str, list[float]], list[int]]`
  - `build_positions_with_thresholds(xy_positions: dict, on_frame: np.ndarray, off_frame: np.ndarray, neighborhood_size: int) -> dict[str, list[float]]` (depends on `domain.realsense_utils.sample_neighborhood_brightness`)
  - `update_config_leds(config_path: str, camera_name: str, ir_positions: dict, ir_res: tuple[int, int], rgb_positions: dict, rgb_res: tuple[int, int]) -> None`
  - `load_led_positions(config_path: str, camera_name: str) -> tuple[dict, dict]` — raises `KeyError` if the camera has no calibration yet. Consumed by `gui/main_window.py` (Task 19) to load the just-calibrated positions into the live session page.

- [ ] **Step 1: Write the failing tests**

Create `tests/domain/test_calibration.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/domain/test_calibration.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'domain.calibration'`.

- [ ] **Step 3: Write `domain/calibration.py`**

```python
"""LED grid assignment and per-LED on/off/threshold math.

Ported from optical_sync_poc_/led_calibration.py. See that file's module
docstring for the full rationale (per-LED thresholds instead of one
global constant, row-major grid numbering assumption, etc.) - this module
keeps only the pure computation, not the camera/LED-panel orchestration.
"""

import yaml

from domain.realsense_utils import sample_neighborhood_brightness


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
    result = {}
    for led_id, (x, y) in xy_positions.items():
        on_value = sample_neighborhood_brightness(on_frame, x, y, neighborhood_size)
        off_value = sample_neighborhood_brightness(off_frame, x, y, neighborhood_size)
        threshold = off_value + 0.5 * (on_value - off_value)
        result[led_id] = [x, y, round(on_value, 2), round(off_value, 2), round(threshold, 2)]
    return result


def update_config_leds(config_path, camera_name, ir_positions, ir_res, rgb_positions, rgb_res):
    with open(config_path, "r") as f:
        cfg = yaml.safe_load(f) or {}

    cfg.setdefault("leds", {})
    cfg["leds"][camera_name] = {
        "ir": {
            "frame_width": ir_res[0],
            "frame_height": ir_res[1],
            "positions": ir_positions,
        },
        "rgb": {
            "frame_width": rgb_res[0],
            "frame_height": rgb_res[1],
            "positions": rgb_positions,
        },
    }

    with open(config_path, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)


def load_led_positions(config_path, camera_name):
    with open(config_path, "r") as f:
        cfg = yaml.safe_load(f)
    leds_by_camera = cfg.get("leds", {})
    if camera_name not in leds_by_camera:
        raise KeyError(
            "No LED calibration yet for camera {!r} - run calibration with this "
            "camera connected first. Known cameras in {}: {}".format(
                camera_name, config_path, list(leds_by_camera.keys())
            )
        )
    return leds_by_camera[camera_name]["ir"]["positions"], leds_by_camera[camera_name]["rgb"]["positions"]
```

Note: `xy_positions` values are tuples `(x, y)` in the test but plain lists `[x, y]` when they come from `assign_grid_ids` in real use — both index the same way (`x, y = xy_positions[led_id]`), so no change needed; the destructuring in `build_positions_with_thresholds` works for either.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/domain/test_calibration.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add domain/calibration.py tests/domain/test_calibration.py
git commit -m "feat: port LED grid assignment and threshold math into domain/calibration"
```

---

### Task 4: `engine/metrics.py` — pairing-gap and position-gap metrics

**Files:**
- Create: `engine/metrics.py`
- Test: `tests/engine/test_metrics.py`

**Interfaces:**
- Produces:
  - `@dataclass FramePairSample(pair_index: int, ir_ts_us: float, rgb_ts_us: float, ir_bright: np.ndarray | None = None, rgb_bright: np.ndarray | None = None)`
  - `@dataclass MetricResult(name: str, value: float | None, excluded: bool, exclude_reason: str | None = None)`
  - `class Metric(ABC)` with `name: str` and `def update(self, sample: FramePairSample) -> MetricResult`
  - `class PairingGapMetric(Metric)` — constructor `(outlier_threshold_us: float)`
  - `class PositionGapMetric(Metric)` — constructor `(ir_threshold: np.ndarray, rgb_threshold: np.ndarray, num_leds: int, switch_time_ms: float, ir_fps: float, rgb_fps: float, frame_drop_threshold_factor: float, warmup_pairs_to_skip: int)`
  - `find_last_on_led(on: np.ndarray) -> tuple[int | None, int]`
  - `compute_position_gap(ir_last: int, rgb_last: int, n: int) -> float`
- These are consumed directly by `engine/test_session.py` (Task 7) and `engine/acquisition_loop.py` (Task 8).

- [ ] **Step 1: Write the failing tests**

Create `tests/engine/test_metrics.py`:

```python
import numpy as np
from engine.metrics import (
    FramePairSample,
    find_last_on_led,
    compute_position_gap,
    PairingGapMetric,
    PositionGapMetric,
)


def test_find_last_on_led_plain_block():
    on = np.zeros(10, dtype=bool)
    on[3:6] = True  # LEDs 3,4,5 on -> last is 5
    last, length = find_last_on_led(on)
    assert last == 5
    assert length == 3


def test_find_last_on_led_wrap_around():
    on = np.zeros(10, dtype=bool)
    on[[8, 9, 0, 1]] = True  # wraps 9->0, post-wrap highest is 1
    last, length = find_last_on_led(on)
    assert last == 1
    assert length == 4


def test_find_last_on_led_nothing_on():
    on = np.zeros(10, dtype=bool)
    last, length = find_last_on_led(on)
    assert last is None
    assert length == 0


def test_compute_position_gap_wraps_to_shortest_path():
    # n=100: ir=2, rgb=98 -> raw diff -96, wrapped should be +4 (2 is 4 steps past 98's wrap)
    diff = compute_position_gap(ir_last=2, rgb_last=98, n=100)
    assert diff == 4


def test_pairing_gap_metric_flags_outlier():
    metric = PairingGapMetric(outlier_threshold_us=100_000)
    sample = FramePairSample(pair_index=0, ir_ts_us=1_000_000.0, rgb_ts_us=1_500_000.0)
    result = metric.update(sample)
    assert result.name == "pairing_gap_us"
    assert result.value == -500_000.0
    assert result.excluded is True
    assert result.exclude_reason == "syncer_outlier"


def test_pairing_gap_metric_accepts_close_pair():
    metric = PairingGapMetric(outlier_threshold_us=100_000)
    sample = FramePairSample(pair_index=0, ir_ts_us=1_000_000.0, rgb_ts_us=1_000_050.0)
    result = metric.update(sample)
    assert result.excluded is False
    assert result.exclude_reason is None


def test_position_gap_metric_reports_miss_when_nothing_on():
    ir_threshold = np.full(10, 150.0)
    rgb_threshold = np.full(10, 150.0)
    metric = PositionGapMetric(
        ir_threshold=ir_threshold, rgb_threshold=rgb_threshold, num_leds=10,
        switch_time_ms=1.0, ir_fps=30, rgb_fps=30,
        frame_drop_threshold_factor=1.5, warmup_pairs_to_skip=0,
    )
    sample = FramePairSample(
        pair_index=0, ir_ts_us=0.0, rgb_ts_us=0.0,
        ir_bright=np.full(10, 50.0), rgb_bright=np.full(10, 50.0),
    )
    result = metric.update(sample)
    assert result.excluded is True
    assert result.exclude_reason == "miss"


def test_position_gap_metric_computes_gap_ms():
    ir_threshold = np.full(10, 150.0)
    rgb_threshold = np.full(10, 150.0)
    metric = PositionGapMetric(
        ir_threshold=ir_threshold, rgb_threshold=rgb_threshold, num_leds=10,
        switch_time_ms=2.0, ir_fps=30, rgb_fps=30,
        frame_drop_threshold_factor=1.5, warmup_pairs_to_skip=0,
    )
    ir_bright = np.full(10, 50.0); ir_bright[5] = 200.0
    rgb_bright = np.full(10, 50.0); rgb_bright[3] = 200.0
    sample = FramePairSample(pair_index=0, ir_ts_us=0.0, rgb_ts_us=0.0, ir_bright=ir_bright, rgb_bright=rgb_bright)
    result = metric.update(sample)
    assert result.excluded is False
    assert result.value == 4.0  # (5 - 3) LED steps * 2.0 ms


def test_position_gap_metric_flags_warmup_pairs():
    ir_threshold = np.full(10, 150.0)
    rgb_threshold = np.full(10, 150.0)
    metric = PositionGapMetric(
        ir_threshold=ir_threshold, rgb_threshold=rgb_threshold, num_leds=10,
        switch_time_ms=1.0, ir_fps=30, rgb_fps=30,
        frame_drop_threshold_factor=1.5, warmup_pairs_to_skip=2,
    )
    ir_bright = np.full(10, 200.0)
    rgb_bright = np.full(10, 200.0)
    first = metric.update(FramePairSample(0, 0.0, 0.0, ir_bright, rgb_bright))
    second = metric.update(FramePairSample(1, 33333.0, 33333.0, ir_bright, rgb_bright))
    third = metric.update(FramePairSample(2, 66666.0, 66666.0, ir_bright, rgb_bright))
    assert first.exclude_reason == "warmup"
    assert second.exclude_reason == "warmup"
    assert third.exclude_reason is None


def test_position_gap_metric_flags_frame_drop():
    ir_threshold = np.full(10, 150.0)
    rgb_threshold = np.full(10, 150.0)
    metric = PositionGapMetric(
        ir_threshold=ir_threshold, rgb_threshold=rgb_threshold, num_leds=10,
        switch_time_ms=1.0, ir_fps=30, rgb_fps=30,
        frame_drop_threshold_factor=1.5, warmup_pairs_to_skip=0,
    )
    ir_bright = np.full(10, 200.0)
    rgb_bright = np.full(10, 200.0)
    metric.update(FramePairSample(0, 0.0, 0.0, ir_bright, rgb_bright))
    # Expected delta at 30fps is ~33333us; jump to 500_000us should trip the drop check.
    result = metric.update(FramePairSample(1, 500_000.0, 33333.0, ir_bright, rgb_bright))
    assert result.exclude_reason == "frame_drop"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/engine/test_metrics.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'engine.metrics'`.

- [ ] **Step 3: Write `engine/metrics.py`**

```python
"""Live, per-frame-pair sync metrics.

Ported from optical_sync_poc_/pipeline_sync_test_diff.py, restructured
from "run once over the fully recorded arrays after capture finishes"
into incremental versions callable one frame-pair at a time, so the GUI
can plot them live instead of only after a run ends. find_last_on_led and
compute_position_gap already operated per-pair in the original script and
are ported unchanged; the frame-drop check is the one piece rewritten
from a batch np.diff over the whole array into a rolling
previous-timestamp comparison.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np


@dataclass
class FramePairSample:
    pair_index: int
    ir_ts_us: float
    rgb_ts_us: float
    ir_bright: "np.ndarray | None" = None
    rgb_bright: "np.ndarray | None" = None


@dataclass
class MetricResult:
    name: str
    value: "float | None"
    excluded: bool
    exclude_reason: "str | None" = None


class Metric(ABC):
    name: str

    @abstractmethod
    def update(self, sample: FramePairSample) -> MetricResult:
        raise NotImplementedError


def find_last_on_led(on):
    n = len(on)
    idx = np.where(on)[0]
    if len(idx) == 0:
        return None, 0

    runs = []
    start = idx[0]
    prev = idx[0]
    for i in idx[1:]:
        if i == prev + 1:
            prev = i
        else:
            runs.append((start, prev))
            start = i
            prev = i
    runs.append((start, prev))

    if len(runs) > 1 and runs[0][0] == 0 and runs[-1][1] == n - 1:
        first = runs[0]
        last = runs[-1]
        middle = runs[1:-1]
        wrap_len = (first[1] - first[0] + 1) + (last[1] - last[0] + 1)
        candidates = middle + [("wrap", last[0], first[1], wrap_len)]
    else:
        candidates = runs

    best = None
    best_len = -1
    for r in candidates:
        if r[0] == "wrap":
            _, last_start, first_end, length = r
            if length > best_len:
                best_len = length
                best = ("wrap", last_start, first_end)
        else:
            s, e = r
            length = e - s + 1
            if length > best_len:
                best_len = length
                best = ("plain", s, e)

    if best[0] == "wrap":
        _, _, first_end = best
        return int(first_end), best_len
    else:
        _, s, e = best
        return int(e), best_len


def compute_position_gap(ir_last, rgb_last, n):
    diff = ir_last - rgb_last
    half = n / 2.0
    if diff > half:
        diff -= n
    elif diff <= -half:
        diff += n
    return diff


class PairingGapMetric(Metric):
    name = "pairing_gap_us"

    def __init__(self, outlier_threshold_us):
        self.outlier_threshold_us = outlier_threshold_us

    def update(self, sample: FramePairSample) -> MetricResult:
        gap = sample.ir_ts_us - sample.rgb_ts_us
        excluded = abs(gap) > self.outlier_threshold_us
        return MetricResult(
            name=self.name,
            value=gap,
            excluded=excluded,
            exclude_reason="syncer_outlier" if excluded else None,
        )


def _is_frame_drop(prev_ts, curr_ts, fps, threshold_factor):
    if prev_ts is None:
        return False
    delta = curr_ts - prev_ts
    expected_delta = 1_000_000.0 / fps
    return delta < 0 or delta > expected_delta * threshold_factor


class PositionGapMetric(Metric):
    name = "position_gap_ms"

    def __init__(self, ir_threshold, rgb_threshold, num_leds, switch_time_ms,
                 ir_fps, rgb_fps, frame_drop_threshold_factor, warmup_pairs_to_skip):
        self.ir_threshold = ir_threshold
        self.rgb_threshold = rgb_threshold
        self.num_leds = num_leds
        self.switch_time_ms = switch_time_ms
        self.ir_fps = ir_fps
        self.rgb_fps = rgb_fps
        self.frame_drop_threshold_factor = frame_drop_threshold_factor
        self.warmup_pairs_to_skip = warmup_pairs_to_skip
        self._prev_ir_ts = None
        self._prev_rgb_ts = None
        self._pair_count = 0

    def update(self, sample: FramePairSample) -> MetricResult:
        ir_drop = _is_frame_drop(self._prev_ir_ts, sample.ir_ts_us, self.ir_fps, self.frame_drop_threshold_factor)
        rgb_drop = _is_frame_drop(self._prev_rgb_ts, sample.rgb_ts_us, self.rgb_fps, self.frame_drop_threshold_factor)
        self._prev_ir_ts = sample.ir_ts_us
        self._prev_rgb_ts = sample.rgb_ts_us
        self._pair_count += 1
        is_warmup = self._pair_count <= self.warmup_pairs_to_skip

        if sample.ir_bright is None or sample.rgb_bright is None:
            return MetricResult(name=self.name, value=None, excluded=True, exclude_reason="no_led_data")

        ir_on = sample.ir_bright > self.ir_threshold
        rgb_on = sample.rgb_bright > self.rgb_threshold
        ir_last, _ = find_last_on_led(ir_on)
        rgb_last, _ = find_last_on_led(rgb_on)

        if ir_last is None or rgb_last is None:
            return MetricResult(name=self.name, value=None, excluded=True, exclude_reason="miss")

        diff = compute_position_gap(ir_last, rgb_last, self.num_leds)
        gap_ms = diff * self.switch_time_ms

        if ir_drop or rgb_drop:
            return MetricResult(name=self.name, value=gap_ms, excluded=True, exclude_reason="frame_drop")
        if is_warmup:
            return MetricResult(name=self.name, value=gap_ms, excluded=True, exclude_reason="warmup")
        return MetricResult(name=self.name, value=gap_ms, excluded=False, exclude_reason=None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/engine/test_metrics.py -v`
Expected: PASS (10 tests).

- [ ] **Step 5: Commit**

```bash
git add engine/metrics.py tests/engine/test_metrics.py
git commit -m "feat: add incremental pairing-gap and position-gap metrics"
```

---

### Task 5: `domain/csv_export.py` — generic session CSV writer

**Files:**
- Create: `domain/csv_export.py`
- Test: `tests/domain/test_csv_export.py`

**Interfaces:**
- Consumes: rows shaped like `{"pair_index": int, "ir_ts_us": float, "rgb_ts_us": float, "<metric_name>": float | None, "<metric_name>_excluded": bool, "<metric_name>_exclude_reason": str | None, ...}` — one such row is produced per frame-pair by `engine/test_session.py` (Task 7).
- Produces: `export_session_csvs(rows: list[dict], kept_path: str, dropped_path: str, drop_reason: str = "frame_drop") -> tuple[int, int]` (returns `(n_kept, n_dropped)`).

- [ ] **Step 1: Write the failing tests**

Create `tests/domain/test_csv_export.py`:

```python
import csv
from domain.csv_export import export_session_csvs


def _row(pair_index, exclude_reason=None):
    return {
        "pair_index": pair_index,
        "ir_ts_us": 1000.0 + pair_index,
        "rgb_ts_us": 1000.5 + pair_index,
        "pairing_gap_us": -0.5,
        "pairing_gap_us_excluded": False,
        "pairing_gap_us_exclude_reason": None,
        "position_gap_ms_excluded": exclude_reason is not None,
        "position_gap_ms_exclude_reason": exclude_reason,
    }


def test_export_session_csvs_splits_by_frame_drop(tmp_path):
    rows = [_row(0), _row(1, exclude_reason="frame_drop"), _row(2, exclude_reason="warmup")]
    kept_path = tmp_path / "kept.csv"
    dropped_path = tmp_path / "dropped.csv"

    n_kept, n_dropped = export_session_csvs(rows, str(kept_path), str(dropped_path))

    assert n_kept == 2  # pair 0 (clean) and pair 2 (warmup - still kept, just flagged)
    assert n_dropped == 1  # pair 1 (frame_drop) goes to the dropped file

    with open(kept_path, newline="") as f:
        kept_rows = list(csv.DictReader(f))
    with open(dropped_path, newline="") as f:
        dropped_rows = list(csv.DictReader(f))

    assert [r["pair_index"] for r in kept_rows] == ["0", "2"]
    assert [r["pair_index"] for r in dropped_rows] == ["1"]


def test_export_session_csvs_empty_rows(tmp_path):
    kept_path = tmp_path / "kept.csv"
    dropped_path = tmp_path / "dropped.csv"
    n_kept, n_dropped = export_session_csvs([], str(kept_path), str(dropped_path))
    assert (n_kept, n_dropped) == (0, 0)
    assert kept_path.exists()
    assert dropped_path.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/domain/test_csv_export.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'domain.csv_export'`.

- [ ] **Step 3: Write `domain/csv_export.py`**

```python
"""Generalized CSV export for a recorded TestSession.

Ported from optical_sync_poc_/pipeline_sync_test_diff.py's
write_raw_csvs, generalized so it no longer hardcodes exactly which
metric columns exist - engine.test_session.TestSession decides the row
shape (one column set per active Metric), this module just splits rows
into kept vs. frame-drop-excluded files and writes them, same convention
as the original script: only a frame-drop exclusion gets its own file,
every other exclusion reason (miss/warmup/outlier) stays in the kept
file, just flagged via its own column.
"""

import csv


def export_session_csvs(rows, kept_path, dropped_path, drop_reason="frame_drop"):
    fieldnames = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    if not fieldnames:
        fieldnames = ["pair_index"]

    n_kept = 0
    n_dropped = 0
    with open(kept_path, "w", newline="") as kept_f, open(dropped_path, "w", newline="") as dropped_f:
        kept_writer = csv.DictWriter(kept_f, fieldnames=fieldnames)
        dropped_writer = csv.DictWriter(dropped_f, fieldnames=fieldnames)
        kept_writer.writeheader()
        dropped_writer.writeheader()

        for row in rows:
            is_frame_drop = any(
                key.endswith("_exclude_reason") and value == drop_reason
                for key, value in row.items()
            )
            if is_frame_drop:
                dropped_writer.writerow(row)
                n_dropped += 1
            else:
                kept_writer.writerow(row)
                n_kept += 1

    return n_kept, n_dropped
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/domain/test_csv_export.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add domain/csv_export.py tests/domain/test_csv_export.py
git commit -m "feat: add generalized session CSV export"
```

---

### Task 6: `state/gui_state.py` — GUI's own persisted state

**Files:**
- Create: `state/gui_state.py`
- Test: `tests/state/test_gui_state.py`

**Interfaces:**
- Produces:
  - `@dataclass GuiState(device_serial: str | None = None, ir_fps: int | None = None, ir_width: int | None = None, ir_height: int | None = None, rgb_fps: int | None = None, rgb_width: int | None = None, rgb_height: int | None = None, ir_roi: list[int] | None = None, rgb_roi: list[int] | None = None, rig_setup_name: str | None = None)`
  - `load_gui_state(path: str = "gui_state.json") -> GuiState`
  - `save_gui_state(state: GuiState, path: str = "gui_state.json") -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/state/test_gui_state.py`:

```python
from state.gui_state import GuiState, load_gui_state, save_gui_state


def test_load_gui_state_missing_file_returns_defaults(tmp_path):
    path = tmp_path / "gui_state.json"
    state = load_gui_state(str(path))
    assert state == GuiState()


def test_save_then_load_round_trips(tmp_path):
    path = tmp_path / "gui_state.json"
    original = GuiState(
        device_serial="123456",
        ir_fps=30, ir_width=1280, ir_height=720,
        rgb_fps=30, rgb_width=1280, rgb_height=720,
        ir_roi=[10, 20, 100, 100], rgb_roi=[5, 15, 90, 90],
        rig_setup_name="dist2_height2",
    )
    save_gui_state(original, str(path))
    loaded = load_gui_state(str(path))
    assert loaded == original


def test_load_gui_state_ignores_corrupt_file(tmp_path):
    path = tmp_path / "gui_state.json"
    path.write_text("{not valid json")
    state = load_gui_state(str(path))
    assert state == GuiState()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/state/test_gui_state.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'state.gui_state'`.

- [ ] **Step 3: Write `state/gui_state.py`**

```python
"""The GUI's own persisted state - last device/stream/ROI choices.

Deliberately separate from settings.yaml (optical_sync_poc_'s hand-edited,
comment-preserving reference file, which the GUI must never overwrite -
see the design doc's "Settings persistence" decision). This file is
plain, disposable, machine-written JSON.
"""

import json
import dataclasses
from dataclasses import dataclass


@dataclass
class GuiState:
    device_serial: "str | None" = None
    ir_fps: "int | None" = None
    ir_width: "int | None" = None
    ir_height: "int | None" = None
    rgb_fps: "int | None" = None
    rgb_width: "int | None" = None
    rgb_height: "int | None" = None
    ir_roi: "list[int] | None" = None
    rgb_roi: "list[int] | None" = None
    rig_setup_name: "str | None" = None


def load_gui_state(path="gui_state.json"):
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return GuiState()

    known_fields = {f.name for f in dataclasses.fields(GuiState)}
    filtered = {k: v for k, v in data.items() if k in known_fields}
    return GuiState(**filtered)


def save_gui_state(state, path="gui_state.json"):
    with open(path, "w") as f:
        json.dump(dataclasses.asdict(state), f, indent=2)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/state/test_gui_state.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add state/gui_state.py tests/state/test_gui_state.py
git commit -m "feat: add GUI state persistence separate from settings.yaml"
```

---

### Task 7: `engine/test_session.py` — session lifecycle and row buffering

**Files:**
- Create: `engine/test_session.py`
- Test: `tests/engine/test_test_session.py`

**Interfaces:**
- Consumes: `engine.metrics.Metric`, `engine.metrics.FramePairSample`, `engine.metrics.MetricResult` (Task 4).
- Produces:
  - `@dataclass TestSessionConfig(metrics: list[Metric], duration_s: float | None = None)`
  - `class TestSession`:
    - `def __init__(self, config: TestSessionConfig)`
    - `def start(self) -> None`
    - `def process_pair(self, sample: FramePairSample) -> dict` — runs every metric, appends and returns a flat row dict (keys: `pair_index`, `ir_ts_us`, `rgb_ts_us`, then per metric `<name>`, `<name>_excluded`, `<name>_exclude_reason`)
    - `def should_auto_stop(self, elapsed_s: float) -> bool`
    - `def stop(self) -> list[dict]` — returns all buffered rows
    - `.is_running: bool` property
- Consumed by `engine/acquisition_loop.py` (Task 8) and `gui/pages/live_session_page.py` (Task 18).

- [ ] **Step 1: Write the failing tests**

Create `tests/engine/test_test_session.py`:

```python
from engine.metrics import FramePairSample, MetricResult, Metric
from engine.test_session import TestSession, TestSessionConfig


class FakeMetric(Metric):
    name = "fake_metric"

    def update(self, sample):
        return MetricResult(name=self.name, value=float(sample.pair_index), excluded=False, exclude_reason=None)


def test_start_sets_running_true():
    session = TestSession(TestSessionConfig(metrics=[FakeMetric()]))
    assert session.is_running is False
    session.start()
    assert session.is_running is True


def test_process_pair_returns_flat_row_and_buffers_it():
    session = TestSession(TestSessionConfig(metrics=[FakeMetric()]))
    session.start()
    row = session.process_pair(FramePairSample(pair_index=0, ir_ts_us=100.0, rgb_ts_us=100.0))
    assert row["pair_index"] == 0
    assert row["ir_ts_us"] == 100.0
    assert row["fake_metric"] == 0.0
    assert row["fake_metric_excluded"] is False
    assert row["fake_metric_exclude_reason"] is None


def test_stop_returns_all_buffered_rows_and_sets_running_false():
    session = TestSession(TestSessionConfig(metrics=[FakeMetric()]))
    session.start()
    session.process_pair(FramePairSample(0, 0.0, 0.0))
    session.process_pair(FramePairSample(1, 1.0, 1.0))
    rows = session.stop()
    assert len(rows) == 2
    assert session.is_running is False


def test_should_auto_stop_respects_configured_duration():
    session = TestSession(TestSessionConfig(metrics=[], duration_s=5.0))
    assert session.should_auto_stop(elapsed_s=4.9) is False
    assert session.should_auto_stop(elapsed_s=5.0) is True


def test_should_auto_stop_never_true_when_duration_is_none():
    session = TestSession(TestSessionConfig(metrics=[], duration_s=None))
    assert session.should_auto_stop(elapsed_s=1_000_000.0) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/engine/test_test_session.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'engine.test_session'`.

- [ ] **Step 3: Write `engine/test_session.py`**

```python
"""Owns start/stop/duration for one live sync-test run, and buffers the
rows that eventually become the CSV (domain.csv_export.export_session_csvs).

Deliberately has no idea about Qt or real hardware - engine.acquisition_loop
feeds it FramePairSample objects; engine.session_engine (the QThread
wrapper) is what actually drives that loop against real sensors.
"""

from dataclasses import dataclass, field

from engine.metrics import Metric, FramePairSample


@dataclass
class TestSessionConfig:
    metrics: "list[Metric]" = field(default_factory=list)
    duration_s: "float | None" = None


class TestSession:
    def __init__(self, config: TestSessionConfig):
        self.config = config
        self.is_running = False
        self._rows = []

    def start(self):
        self._rows = []
        self.is_running = True

    def process_pair(self, sample: FramePairSample) -> dict:
        row = {
            "pair_index": sample.pair_index,
            "ir_ts_us": sample.ir_ts_us,
            "rgb_ts_us": sample.rgb_ts_us,
        }
        for metric in self.config.metrics:
            result = metric.update(sample)
            row[result.name] = result.value
            row[f"{result.name}_excluded"] = result.excluded
            row[f"{result.name}_exclude_reason"] = result.exclude_reason
        self._rows.append(row)
        return row

    def should_auto_stop(self, elapsed_s: float) -> bool:
        if self.config.duration_s is None:
            return False
        return elapsed_s >= self.config.duration_s

    def stop(self) -> "list[dict]":
        self.is_running = False
        return self._rows
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/engine/test_test_session.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add engine/test_session.py tests/engine/test_test_session.py
git commit -m "feat: add TestSession lifecycle and row buffering"
```

---

### Task 8: `engine/acquisition_loop.py` — pure, dependency-injected frame loop

**Files:**
- Create: `engine/acquisition_loop.py`
- Test: `tests/engine/test_acquisition_loop.py`

**Interfaces:**
- Consumes: `engine.metrics.FramePairSample` (Task 4), `engine.test_session.TestSession` (Task 7).
- Produces:
  - `@dataclass AcquisitionCallbacks(on_frames: callable, on_row: callable, on_stats: callable)` — `on_frames(ir_image, rgb_image, pair_index)`, `on_row(row: dict)`, `on_stats(stats: dict)`
  - `class AcquisitionLoop`:
    - `def __init__(self, frame_source, test_session: TestSession, callbacks: AcquisitionCallbacks, display_stride: int = 10)`
    - `def run_until_stopped(self, is_stop_requested: callable, elapsed_s_fn: callable) -> list[dict]`
  - `frame_source` is any iterable/generator yielding `(ir_image: np.ndarray, rgb_image: np.ndarray, ir_ts_us: float, rgb_ts_us: float, ir_bright: np.ndarray | None, rgb_bright: np.ndarray | None)` tuples — this is the seam `engine/session_engine.py` (Task 11) fills with a real `ContinuousCapture` (Task 10), and tests fill with a fake generator.
- Consumed by `engine/session_engine.py` (Task 11).

- [ ] **Step 1: Write the failing tests**

Create `tests/engine/test_acquisition_loop.py`:

```python
import numpy as np
from engine.acquisition_loop import AcquisitionLoop, AcquisitionCallbacks
from engine.metrics import Metric, MetricResult
from engine.test_session import TestSession, TestSessionConfig


class CountingMetric(Metric):
    name = "count"

    def update(self, sample):
        return MetricResult(name=self.name, value=float(sample.pair_index), excluded=False, exclude_reason=None)


def fake_frame_source(n_pairs):
    for i in range(n_pairs):
        ir_image = np.full((4, 4), i, dtype=np.uint8)
        rgb_image = np.full((4, 4, 3), i, dtype=np.uint8)
        yield ir_image, rgb_image, float(i), float(i), None, None


def test_run_until_stopped_processes_every_frame_and_calls_on_row():
    session = TestSession(TestSessionConfig(metrics=[CountingMetric()]))
    session.start()
    rows_seen = []
    frames_seen = []
    stats_seen = []
    callbacks = AcquisitionCallbacks(
        on_frames=lambda ir, rgb, idx: frames_seen.append(idx),
        on_row=lambda row: rows_seen.append(row),
        on_stats=lambda stats: stats_seen.append(stats),
    )
    loop = AcquisitionLoop(fake_frame_source(5), session, callbacks, display_stride=2)

    stop_after = {"count": 0}

    def is_stop_requested():
        stop_after["count"] += 1
        return stop_after["count"] > 5  # never true before the generator is exhausted

    rows = loop.run_until_stopped(is_stop_requested, elapsed_s_fn=lambda: 0.0)

    assert len(rows) == 5
    assert [row["pair_index"] for row in rows] == [0, 1, 2, 3, 4]
    assert len(rows_seen) == 5


def test_run_until_stopped_throttles_frame_display_by_stride():
    session = TestSession(TestSessionConfig(metrics=[CountingMetric()]))
    session.start()
    frames_seen = []
    callbacks = AcquisitionCallbacks(
        on_frames=lambda ir, rgb, idx: frames_seen.append(idx),
        on_row=lambda row: None,
        on_stats=lambda stats: None,
    )
    loop = AcquisitionLoop(fake_frame_source(10), session, callbacks, display_stride=3)
    loop.run_until_stopped(is_stop_requested=lambda: False, elapsed_s_fn=lambda: 0.0)

    # Every metric row is still processed for all 10 pairs, but the video
    # callback should only fire every 3rd pair (0, 3, 6, 9).
    assert frames_seen == [0, 3, 6, 9]


def test_run_until_stopped_honors_stop_request_mid_stream():
    session = TestSession(TestSessionConfig(metrics=[CountingMetric()]))
    session.start()
    callbacks = AcquisitionCallbacks(on_frames=lambda *a: None, on_row=lambda r: None, on_stats=lambda s: None)
    loop = AcquisitionLoop(fake_frame_source(100), session, callbacks, display_stride=10)

    seen = {"n": 0}

    def is_stop_requested():
        seen["n"] += 1
        return seen["n"] > 3  # first true on the 4th check, i.e. before processing the 4th frame

    rows = loop.run_until_stopped(is_stop_requested, elapsed_s_fn=lambda: 0.0)
    # is_stop_requested is checked before each frame is processed, so the 4th
    # check (which returns True) stops the loop having processed exactly 3 frames.
    assert len(rows) == 3


def test_run_until_stopped_honors_session_auto_stop_duration():
    session = TestSession(TestSessionConfig(metrics=[CountingMetric()], duration_s=2.0))
    session.start()
    callbacks = AcquisitionCallbacks(on_frames=lambda *a: None, on_row=lambda r: None, on_stats=lambda s: None)
    loop = AcquisitionLoop(fake_frame_source(100), session, callbacks, display_stride=10)

    elapsed = {"t": 0.0}

    def elapsed_s_fn():
        elapsed["t"] += 1.0
        return elapsed["t"]

    rows = loop.run_until_stopped(is_stop_requested=lambda: False, elapsed_s_fn=elapsed_s_fn)
    # elapsed_s_fn is checked before each frame is processed: call 1 returns
    # 1.0 (< duration_s, so frame 0 is processed), call 2 returns 2.0
    # (>= duration_s, so the loop stops before processing a second frame).
    assert len(rows) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/engine/test_acquisition_loop.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'engine.acquisition_loop'`.

- [ ] **Step 3: Write `engine/acquisition_loop.py`**

```python
"""Pure-Python frame-pair processing loop.

No Qt, no pyrealsense2 - this is the piece that used to be
pipeline_sync_test_diff.py's run_pipeline_capture, restructured so it can
be unit-tested with a fake frame_source and so the real hardware/Qt
wiring (engine.session_engine) stays a thin adapter around it.
"""

from dataclasses import dataclass

from engine.metrics import FramePairSample
from engine.test_session import TestSession


@dataclass
class AcquisitionCallbacks:
    on_frames: callable
    on_row: callable
    on_stats: callable


class AcquisitionLoop:
    def __init__(self, frame_source, test_session: TestSession, callbacks: AcquisitionCallbacks, display_stride: int = 10):
        self.frame_source = frame_source
        self.test_session = test_session
        self.callbacks = callbacks
        self.display_stride = display_stride

    def run_until_stopped(self, is_stop_requested, elapsed_s_fn) -> "list[dict]":
        pair_index = 0
        for ir_image, rgb_image, ir_ts_us, rgb_ts_us, ir_bright, rgb_bright in self.frame_source:
            if is_stop_requested():
                break
            if self.test_session.should_auto_stop(elapsed_s_fn()):
                break

            sample = FramePairSample(
                pair_index=pair_index,
                ir_ts_us=ir_ts_us,
                rgb_ts_us=rgb_ts_us,
                ir_bright=ir_bright,
                rgb_bright=rgb_bright,
            )
            row = self.test_session.process_pair(sample)
            self.callbacks.on_row(row)

            if pair_index % self.display_stride == 0:
                self.callbacks.on_frames(ir_image, rgb_image, pair_index)
                self.callbacks.on_stats(row)

            pair_index += 1

        return self.test_session.stop()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/engine/test_acquisition_loop.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add engine/acquisition_loop.py tests/engine/test_acquisition_loop.py
git commit -m "feat: add dependency-injected acquisition loop"
```

---

### Task 9: `engine/led_panel.py` — forked LEDPanel without the missing logger dependency

**Files:**
- Create: `engine/led_panel.py`
- Test: `tests/engine/test_led_panel.py`

**Interfaces:**
- Produces: `class LEDPanel` — same static-method API as `optical_sync_poc_/led_panel_cli.py` (`all_leds_on`, `all_leds_off`, `stop`, `start`, `reset`, `response_time_measurement_mode`, `rolling_shutter_mode`, `set_display_brightness`, `set_speed_ms`, `set_direction_single`).
- Consumed by `engine/acquisition_loop.py` callers in `engine/session_engine.py` (Task 11) and `gui/pages/calibration_page.py` / `gui/pages/live_session_page.py`.

- [ ] **Step 1: Write the failing tests**

Create `tests/engine/test_led_panel.py`:

```python
from unittest.mock import patch, call
from subprocess import CalledProcessError

from engine.led_panel import LEDPanel


def test_all_leds_on_calls_stop_then_set_mode_5():
    with patch("engine.led_panel.check_call") as mock_check_call, patch("time.sleep"):
        LEDPanel.all_leds_on()
        commands = [c.args[0] for c in mock_check_call.call_args_list]
        assert commands[0] == ["LED-Panel.exe", "--stop"]
        assert commands[1] == ["LED-Panel.exe", "--setMode", "5"]


def test_set_speed_ms_converts_to_seconds_string():
    with patch("engine.led_panel.check_call") as mock_check_call, patch("time.sleep"):
        LEDPanel.set_speed_ms(1)
        mock_check_call.assert_called_once_with(["LED-Panel.exe", "--setTime", "0.0010"])


def test_run_retries_on_called_process_error_then_gives_up():
    with patch("engine.led_panel.check_call", side_effect=CalledProcessError(1, "cmd")) as mock_check_call, \
         patch("time.sleep"):
        LEDPanel._run("--stop")
        assert mock_check_call.call_count == 3  # 3 retries, per the original script's convention
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/engine/test_led_panel.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'engine.led_panel'`.

- [ ] **Step 3: Write `engine/led_panel.py`**

```python
"""LEDPanel control, forked from optical_sync_poc_/led_panel_cli.py.

The only change from the original is the logger: that file imports
`from utils.Log.Logger import get_test_logger`, a package that is not
installed anywhere on this machine (confirmed:
`python -c "from utils.Log.Logger import get_test_logger"` raises
ModuleNotFoundError). Swapped for the stdlib logging module - behavior
and the LED-Panel.exe CLI reference are otherwise unchanged. See
optical_sync_poc_/CLAUDE.md's "LEDPanel CLI reference" section for the
mode numbers and the all_leds_off-vs-stop distinction.
"""

import logging
import time
from subprocess import check_call, CalledProcessError

_logger = logging.getLogger(__name__)


class LEDPanel:
    cmd_delay = 0.1
    exe_name = "LED-Panel.exe"

    @staticmethod
    def _run(args):
        cmd = [LEDPanel.exe_name] + args.split()
        retries = 3
        _logger.info("Running cmd: %s", " ".join(cmd))
        while retries > 0:
            try:
                check_call(cmd)
                retries = 0
            except CalledProcessError as e:
                _logger.error("Command returned with an error: %s", e)
                _logger.info("Retries left: %d", retries - 1)
                retries -= 1
                time.sleep(0.5)
        time.sleep(LEDPanel.cmd_delay)

    @staticmethod
    def all_leds_on():
        LEDPanel.stop()
        LEDPanel._run("--setMode 5")

    @staticmethod
    def rolling_shutter_mode():
        LEDPanel.stop()
        LEDPanel._run("--setMode 4")

    @staticmethod
    def response_time_measurement_mode():
        LEDPanel.stop()
        LEDPanel._run("--setMode 1")

    @staticmethod
    def set_display_brightness(brightness):
        LEDPanel._run("--setDisplayBrightness {}".format(str(brightness)))

    @staticmethod
    def set_speed_ms(ms):
        secs = float(ms) / 1000
        LEDPanel._run("--setTime {:.4f}".format(secs))

    @staticmethod
    def start():
        LEDPanel._run("--start")

    @staticmethod
    def stop():
        LEDPanel._run("--stop")

    @staticmethod
    def reset():
        LEDPanel._run("--reset")

    @staticmethod
    def set_direction_single(mode):
        LEDPanel._run("--setDirectionSingle {}".format(mode))

    @staticmethod
    def all_leds_off():
        LEDPanel.stop()
        LEDPanel._run("--setMode 3")
```

Note: `_run` splits `args` on whitespace (matching the original script), so `mock_check_call.assert_called_once_with(["LED-Panel.exe", "--setTime", "0.0010"])` works because `"--setTime 0.0010".split()` yields `["--setTime", "0.0010"]`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/engine/test_led_panel.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add engine/led_panel.py tests/engine/test_led_panel.py
git commit -m "feat: fork LEDPanel without the unresolvable utils.Log.Logger dependency"
```

---

### Task 10: `engine/streams.py` — device enumeration, profile matching, continuous capture

**Files:**
- Create: `engine/streams.py`
- Test: `tests/engine/test_streams.py`

**Interfaces:**
- Produces:
  - `@dataclass DeviceInfo(name: str, serial: str)`
  - `list_devices(ctx) -> list[DeviceInfo]` — every connected device exposing both a "Stereo Module" and an "RGB Camera" sensor (generalizes `find_camera_sensors`, which only returned the first match, into a full list for the device picker).
  - `get_sensors_for_device(ctx, serial: str) -> tuple[stereo_sensor, rgb_sensor]`
  - `list_supported_profiles(sensor, stream_type, fmt) -> list[tuple[int, int, int]]` — `(width, height, fps)` tuples, for populating the stream-config combo boxes.
  - `match_profile(sensor, stream_type, fmt, width, height, fps)` (ported unchanged)
  - `disable_ir_emitter(stereo_sensor) -> bool`, `enable_auto_exposure(sensor) -> None` (ported unchanged)
  - `class ContinuousCapture` — `__init__(self, ir_resolution, ir_fps, color_resolution, color_fps)`, `def start(self) -> None`, `def frames(self) -> Iterator[tuple[ir_image, rgb_image, ir_ts_us, rgb_ts_us]]`, `def stop(self) -> None` (wraps `rs.pipeline()`, the same mechanism `pipeline_sync_test_diff.py`'s `run_pipeline_capture` already uses).
- Consumed by `gui/pages/device_select_page.py`, `gui/pages/stream_config_page.py`, `gui/pages/roi_select_page.py`, and `engine/session_engine.py` (Task 11).

**Testing note:** `pyrealsense2` device/sensor/pipeline objects cannot be constructed or safely mocked in this environment without real hardware attached (the SDK's Python bindings are thin wrappers over native objects). Per the project's established testing convention (`optical_sync_poc_/CLAUDE.md`: verify pure logic synthetically, verify hardware-facing code manually against the real rig), this task unit-tests only `list_supported_profiles`' and `match_profile`'s pure filtering logic against hand-built fake profile objects, and defers `list_devices`, `get_sensors_for_device`, and `ContinuousCapture` to the manual hardware verification in Task 20.

- [ ] **Step 1: Write the failing tests**

Create `tests/engine/test_streams.py`:

```python
import pytest
from engine.streams import list_supported_profiles, match_profile


class FakeVideoProfile:
    def __init__(self, width, height):
        self._width = width
        self._height = height

    def width(self):
        return self._width

    def height(self):
        return self._height


class FakeProfile:
    def __init__(self, stream_type, fmt, width, height, fps):
        self._stream_type = stream_type
        self._fmt = fmt
        self._fps = fps
        self._video = FakeVideoProfile(width, height)

    def stream_type(self):
        return self._stream_type

    def format(self):
        return self._fmt

    def fps(self):
        return self._fps

    def as_video_stream_profile(self):
        return self._video


class FakeSensor:
    def __init__(self, profiles):
        self.profiles = profiles


def test_list_supported_profiles_filters_by_stream_and_format():
    sensor = FakeSensor(profiles=[
        FakeProfile("infrared", "y8", 1280, 720, 30),
        FakeProfile("infrared", "y8", 640, 480, 60),
        FakeProfile("color", "yuyv", 1280, 720, 30),
    ])
    result = list_supported_profiles(sensor, "infrared", "y8")
    assert set(result) == {(1280, 720, 30), (640, 480, 60)}


def test_match_profile_finds_exact_match():
    target = FakeProfile("infrared", "y8", 1280, 720, 30)
    sensor = FakeSensor(profiles=[FakeProfile("infrared", "y8", 640, 480, 60), target])
    matched = match_profile(sensor, "infrared", "y8", 1280, 720, 30)
    assert matched is target


def test_match_profile_raises_when_nothing_matches():
    sensor = FakeSensor(profiles=[FakeProfile("infrared", "y8", 640, 480, 60)])
    with pytest.raises(RuntimeError):
        match_profile(sensor, "infrared", "y8", 1280, 720, 30)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/engine/test_streams.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'engine.streams'`.

- [ ] **Step 3: Write `engine/streams.py`**

```python
"""Hardware-facing RealSense device/sensor helpers.

Ported from optical_sync_poc_/realsense_utils.py's pyrealsense2-dependent
half (the pure-numpy half lives in domain/realsense_utils.py instead),
plus new device-listing and continuous-capture pieces the GUI needs that
the original one-shot scripts didn't: find_camera_sensors only ever
returned the FIRST matching device, and none of the original scripts
streamed continuously - they all captured one settled frame (calibration,
ROI picker) or ran a fixed-duration batch loop (pipeline_sync_test_diff).
The GUI's live preview and live session both need an open-ended stream,
hence ContinuousCapture.
"""

import time
from dataclasses import dataclass

import numpy as np
import pyrealsense2 as rs


@dataclass
class DeviceInfo:
    name: str
    serial: str


def list_devices(ctx):
    devices = []
    for d in ctx.query_devices():
        sensors = d.query_sensors()
        names = [s.get_info(rs.camera_info.name) for s in sensors]
        if "Stereo Module" in names and "RGB Camera" in names:
            devices.append(DeviceInfo(
                name=d.get_info(rs.camera_info.name),
                serial=d.get_info(rs.camera_info.serial_number),
            ))
    return devices


def get_sensors_for_device(ctx, serial):
    for d in ctx.query_devices():
        if d.get_info(rs.camera_info.serial_number) != serial:
            continue
        sensors = d.query_sensors()
        stereo = next(s for s in sensors if s.get_info(rs.camera_info.name) == "Stereo Module")
        rgb = next(s for s in sensors if s.get_info(rs.camera_info.name) == "RGB Camera")
        return stereo, rgb
    raise RuntimeError("No connected device with serial {!r}".format(serial))


def list_supported_profiles(sensor, stream_type, fmt):
    results = set()
    for p in sensor.profiles:
        if p.stream_type() != stream_type or p.format() != fmt:
            continue
        vp = p.as_video_stream_profile()
        results.add((vp.width(), vp.height(), p.fps()))
    return sorted(results)


def match_profile(sensor, stream_type, fmt, width, height, fps):
    for p in sensor.profiles:
        vp = p.as_video_stream_profile()
        if (
            p.stream_type() == stream_type
            and p.format() == fmt
            and vp.width() == width
            and vp.height() == height
            and p.fps() == fps
        ):
            return p
    raise RuntimeError(
        "No matching profile for {} {}x{}@{}fps ({})".format(stream_type, width, height, fps, fmt)
    )


def disable_ir_emitter(stereo_sensor):
    if stereo_sensor.supports(rs.option.emitter_enabled):
        stereo_sensor.set_option(rs.option.emitter_enabled, 0)
        return True
    return False


def enable_auto_exposure(sensor):
    if sensor.supports(rs.option.enable_auto_exposure):
        sensor.set_option(rs.option.enable_auto_exposure, 1)


class ContinuousCapture:
    """Open-ended IR+RGB capture via rs.pipeline(), same mechanism as
    optical_sync_poc_/pipeline_sync_test_diff.py's run_pipeline_capture,
    restructured as start/frames()/stop() so it can back both the live
    ROI-selection preview and the live sync-test session."""

    def __init__(self, ir_resolution, ir_fps, color_resolution, color_fps):
        self.ir_resolution = ir_resolution
        self.ir_fps = ir_fps
        self.color_resolution = color_resolution
        self.color_fps = color_fps
        self._pipeline = None

    def start(self):
        config = rs.config()
        config.enable_stream(rs.stream.infrared, 1, *self.ir_resolution, rs.format.y8, self.ir_fps)
        config.enable_stream(rs.stream.color, *self.color_resolution, rs.format.yuyv, self.color_fps)
        self._pipeline = rs.pipeline()
        self._pipeline.start(config)

    def frames(self):
        from domain.realsense_utils import ir_bytes_to_image, yuyv_to_bgr

        while True:
            frameset = self._pipeline.wait_for_frames()
            ir_frame = frameset.get_infrared_frame()
            color_frame = frameset.get_color_frame()
            if not ir_frame or not color_frame:
                continue

            ir_image = ir_bytes_to_image(bytes(ir_frame.get_data()), *self.ir_resolution)
            rgb_image = yuyv_to_bgr(bytes(color_frame.get_data()), *self.color_resolution)
            ir_ts_us = ir_frame.get_frame_metadata(getattr(rs.frame_metadata_value, "frame_timestamp"))
            rgb_ts_us = color_frame.get_frame_metadata(getattr(rs.frame_metadata_value, "frame_timestamp"))

            yield ir_image, rgb_image, ir_ts_us, rgb_ts_us

    def stop(self):
        if self._pipeline is not None:
            self._pipeline.stop()
            self._pipeline = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/engine/test_streams.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add engine/streams.py tests/engine/test_streams.py
git commit -m "feat: add device enumeration, profile matching, and continuous capture"
```

---

### Task 11: `engine/session_engine.py` — QThread adapter (manual verification only)

**Files:**
- Create: `engine/session_engine.py`

**Interfaces:**
- Consumes: `engine.streams.ContinuousCapture`, `engine.streams.disable_ir_emitter`, `engine.streams.enable_auto_exposure` (Task 10); `engine.acquisition_loop.AcquisitionLoop`, `AcquisitionCallbacks` (Task 8); `engine.test_session.TestSession` (Task 7); `engine.led_panel.LEDPanel` (Task 9); `domain.realsense_utils.sample_neighborhood_brightness` (Task 2).
- Produces: `class SessionEngineThread(QThread)` with Qt signals `frame_ready = Signal(str, object)` (stream name `"ir"`/`"rgb"`, numpy array), `row_ready = Signal(dict)`, `stats_ready = Signal(dict)`, `session_finished = Signal(list)`, `error = Signal(str)`; constructor takes `(ctx, device_serial, ir_resolution, ir_fps, color_resolution, color_fps, test_session, ir_xy=None, rgb_xy=None, neighborhood_size=5, display_stride=10, parent=None)`; `def request_stop(self)` sets an internal flag `AcquisitionLoop.run_until_stopped` polls via `is_stop_requested`.
- `ir_xy`/`rgb_xy` are the calibrated per-LED `(x, y)` position arrays (same arrays whose on/off values produced `ir_threshold`/`rgb_threshold` in Task 3's `load_led_positions`) — **critical**: `engine.streams.ContinuousCapture.frames()` (Task 10) only yields raw frames + timestamps (a 4-tuple), it has no notion of LED positions or metrics. `SessionEngineThread` is the one piece that bridges "raw frames" to "the 6-tuple `AcquisitionLoop`/`FramePairSample` need" (frames + timestamps + per-LED brightness), by sampling `sample_neighborhood_brightness` at each `ir_xy`/`rgb_xy` position on every frame. When `ir_xy`/`rgb_xy` are `None` (the ROI-preview use case, Task 16, where no metrics run at all), brightness sampling is skipped and `None` is passed through instead — `PositionGapMetric` already treats that as `exclude_reason="no_led_data"`, and `RoiSelectPage` never constructs a `PositionGapMetric` in the first place, so this is inert there.
- Consumed by `gui/pages/roi_select_page.py` (Task 16, frames only, `ir_xy`/`rgb_xy` left at their `None` default, no `test_session` metrics) and `gui/pages/live_session_page.py` (Task 18, full session, passes real `ir_xy`/`rgb_xy` derived from the calibrated LED positions).

This task has no automated test — it is a thin adapter with no branching logic of its own beyond the brightness-sampling wrapper below (everything else it calls was already unit-tested in Tasks 7, 8, 9, and 10); its correctness can only be observed by actually running it against a connected RealSense camera and LED panel, which is why it's verified manually in Task 20 instead.

- [ ] **Step 1: Write `engine/session_engine.py`**

```python
"""Thin QThread adapter: wires real hardware (engine.streams,
engine.led_panel) into engine.acquisition_loop.AcquisitionLoop and
translates its plain-Python callbacks into Qt signals.

Deliberately as small as possible - all the actual logic (frame-pair
processing, metric computation, session buffering) already lives in
AcquisitionLoop/TestSession/Metric, which are unit-tested without Qt or
hardware. This class exists only so that logic can run on a background
thread and reach the UI safely.
"""

from PySide6.QtCore import QThread, Signal

from engine.acquisition_loop import AcquisitionLoop, AcquisitionCallbacks
from engine.streams import ContinuousCapture, disable_ir_emitter, enable_auto_exposure, get_sensors_for_device
from engine.led_panel import LEDPanel
from domain.realsense_utils import sample_neighborhood_brightness


def _sample_all_positions(image, xy_array, neighborhood_size):
    import numpy as np
    return np.array([
        sample_neighborhood_brightness(image, x, y, neighborhood_size)
        for (x, y) in xy_array
    ])


class SessionEngineThread(QThread):
    frame_ready = Signal(str, object)
    row_ready = Signal(dict)
    stats_ready = Signal(dict)
    session_finished = Signal(list)
    error = Signal(str)

    def __init__(self, ctx, device_serial, ir_resolution, ir_fps, color_resolution, color_fps,
                 test_session, ir_xy=None, rgb_xy=None, neighborhood_size=5,
                 display_stride=10, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.device_serial = device_serial
        self.ir_resolution = ir_resolution
        self.ir_fps = ir_fps
        self.color_resolution = color_resolution
        self.color_fps = color_fps
        self.test_session = test_session
        self.ir_xy = ir_xy
        self.rgb_xy = rgb_xy
        self.neighborhood_size = neighborhood_size
        self.display_stride = display_stride
        self._stop_requested = False
        self._capture = None
        self._start_time = None

    def request_stop(self):
        self._stop_requested = True

    def _frame_pairs_with_brightness(self):
        """Adapts ContinuousCapture.frames()'s 4-tuple (image, image, ts, ts)
        into the 6-tuple AcquisitionLoop/FramePairSample need, by sampling
        brightness at each calibrated LED position. This is deliberately done
        here, not inside ContinuousCapture itself: ContinuousCapture is a
        generic hardware-capture primitive with no notion of LED positions or
        metrics (gui/pages/calibration_page.py, a later task, consumes its raw
        4-tuple directly for exactly that reason)."""
        for ir_image, rgb_image, ir_ts_us, rgb_ts_us in self._capture.frames():
            ir_bright = (
                _sample_all_positions(ir_image, self.ir_xy, self.neighborhood_size)
                if self.ir_xy is not None else None
            )
            rgb_bright = (
                _sample_all_positions(rgb_image, self.rgb_xy, self.neighborhood_size)
                if self.rgb_xy is not None else None
            )
            yield ir_image, rgb_image, ir_ts_us, rgb_ts_us, ir_bright, rgb_bright

    def run(self):
        import time

        try:
            stereo_sensor, rgb_sensor = get_sensors_for_device(self.ctx, self.device_serial)
            if not disable_ir_emitter(stereo_sensor):
                self.error.emit("This sensor/firmware does not expose emitter_enabled - confirm the IR projector is off manually.")
            enable_auto_exposure(rgb_sensor)

            self._capture = ContinuousCapture(self.ir_resolution, self.ir_fps, self.color_resolution, self.color_fps)
            self._capture.start()
            self._start_time = time.time()

            def on_frames(ir_image, rgb_image, pair_index):
                self.frame_ready.emit("ir", ir_image)
                self.frame_ready.emit("rgb", rgb_image)

            def on_row(row):
                self.row_ready.emit(row)

            def on_stats(stats):
                self.stats_ready.emit(stats)

            callbacks = AcquisitionCallbacks(on_frames=on_frames, on_row=on_row, on_stats=on_stats)
            loop = AcquisitionLoop(
                self._frame_pairs_with_brightness(), self.test_session, callbacks,
                display_stride=self.display_stride,
            )
            rows = loop.run_until_stopped(
                is_stop_requested=lambda: self._stop_requested,
                elapsed_s_fn=lambda: time.time() - self._start_time,
            )
            self.session_finished.emit(rows)
        except Exception as exc:  # surfaced to the UI rather than crashing the worker thread silently
            self.error.emit(str(exc))
        finally:
            if self._capture is not None:
                self._capture.stop()
            LEDPanel.stop()
```

- [ ] **Step 2: Commit**

```bash
git add engine/session_engine.py
git commit -m "feat: add QThread adapter wiring hardware into the acquisition loop"
```

---

### Task 12: `gui/widgets/video_panel.py` — numpy frame display widget

**Files:**
- Create: `gui/widgets/video_panel.py`
- Create: `tests/conftest.py`
- Test: `tests/gui/widgets/test_video_panel.py`

**Interfaces:**
- Produces: `class VideoPanel(QLabel)` — `def set_frame(self, image: np.ndarray) -> None` (accepts grayscale `(H,W)` or BGR `(H,W,3)` numpy arrays, converts and displays as a `QPixmap`).

- [ ] **Step 1: Create the shared `QApplication` fixture**

Create `tests/conftest.py`:

```python
import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app
```

- [ ] **Step 2: Write the failing test**

Create `tests/gui/widgets/test_video_panel.py`:

```python
import numpy as np
from gui.widgets.video_panel import VideoPanel


def test_set_frame_grayscale_sets_nonnull_pixmap(qapp):
    panel = VideoPanel()
    image = np.full((20, 30), 128, dtype=np.uint8)
    panel.set_frame(image)
    pixmap = panel.pixmap()
    assert pixmap is not None
    assert pixmap.width() == 30
    assert pixmap.height() == 20


def test_set_frame_bgr_sets_correct_size(qapp):
    panel = VideoPanel()
    image = np.zeros((15, 25, 3), dtype=np.uint8)
    panel.set_frame(image)
    pixmap = panel.pixmap()
    assert pixmap.width() == 25
    assert pixmap.height() == 15
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/gui/widgets/test_video_panel.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'gui.widgets.video_panel'`.

- [ ] **Step 4: Write `gui/widgets/video_panel.py`**

```python
"""Displays a live numpy frame (grayscale IR or BGR RGB) as a QLabel."""

import cv2
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QLabel


class VideoPanel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScaledContents(True)

    def set_frame(self, image):
        if image.ndim == 2:
            height, width = image.shape
            qimage = QImage(image.data, width, height, width, QImage.Format_Grayscale8)
        else:
            rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            height, width, _ = rgb.shape
            qimage = QImage(rgb.data, width, height, 3 * width, QImage.Format_RGB888)
        self.setPixmap(QPixmap.fromImage(qimage.copy()))
```

Note: `qimage.copy()` is required because `image.data`/`rgb.data` point at a numpy buffer that may be reused or garbage-collected once `set_frame` returns; copying detaches the `QImage` from that buffer before wrapping it in a `QPixmap`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/gui/widgets/test_video_panel.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add gui/widgets/video_panel.py tests/conftest.py tests/gui/widgets/test_video_panel.py
git commit -m "feat: add VideoPanel widget for displaying live numpy frames"
```

---

### Task 13: `gui/widgets/live_plot.py` — generic scrolling metric plot

**Files:**
- Create: `gui/widgets/live_plot.py`
- Test: `tests/gui/widgets/test_live_plot.py`

**Interfaces:**
- Produces: `class LivePlot(pyqtgraph.PlotWidget)`:
  - `def add_series(self, name: str, color: str) -> None`
  - `def add_point(self, name: str, x: float, y: float) -> None`
  - `def set_series_visible(self, name: str, visible: bool) -> None`
  - `def get_series_data(self, name: str) -> tuple[list[float], list[float]]` (for testing/inspection — returns the `(x_values, y_values)` accumulated so far)

- [ ] **Step 1: Write the failing test**

Create `tests/gui/widgets/test_live_plot.py`:

```python
from gui.widgets.live_plot import LivePlot


def test_add_point_accumulates_series_data(qapp):
    plot = LivePlot()
    plot.add_series("pairing_gap_us", color="r")
    plot.add_point("pairing_gap_us", x=0, y=10.0)
    plot.add_point("pairing_gap_us", x=1, y=-5.0)

    xs, ys = plot.get_series_data("pairing_gap_us")
    assert xs == [0, 1]
    assert ys == [10.0, -5.0]


def test_set_series_visible_toggles_curve_visibility(qapp):
    plot = LivePlot()
    plot.add_series("position_gap_ms", color="g")
    plot.set_series_visible("position_gap_ms", False)
    assert plot._curves["position_gap_ms"].isVisible() is False
    plot.set_series_visible("position_gap_ms", True)
    assert plot._curves["position_gap_ms"].isVisible() is True


def test_two_independent_series_do_not_interfere(qapp):
    plot = LivePlot()
    plot.add_series("a", color="r")
    plot.add_series("b", color="b")
    plot.add_point("a", 0, 1.0)
    plot.add_point("b", 0, 99.0)
    assert plot.get_series_data("a") == ([0], [1.0])
    assert plot.get_series_data("b") == ([0], [99.0])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/gui/widgets/test_live_plot.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'gui.widgets.live_plot'`.

- [ ] **Step 3: Write `gui/widgets/live_plot.py`**

```python
"""Generic live scrolling plot, fed by named metric series (e.g. one
curve for PairingGapMetric, one for PositionGapMetric) so the GUI never
has to special-case which metrics exist - see engine.metrics.Metric."""

import pyqtgraph as pg


class LivePlot(pg.PlotWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.showGrid(x=True, y=True, alpha=0.3)
        self._curves = {}
        self._x_data = {}
        self._y_data = {}

    def add_series(self, name, color):
        curve = self.plot([], [], pen=pg.mkPen(color=color, width=2), name=name)
        self._curves[name] = curve
        self._x_data[name] = []
        self._y_data[name] = []

    def add_point(self, name, x, y):
        self._x_data[name].append(x)
        self._y_data[name].append(y)
        self._curves[name].setData(self._x_data[name], self._y_data[name])

    def set_series_visible(self, name, visible):
        self._curves[name].setVisible(visible)

    def get_series_data(self, name):
        return self._x_data[name], self._y_data[name]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/gui/widgets/test_live_plot.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add gui/widgets/live_plot.py tests/gui/widgets/test_live_plot.py
git commit -m "feat: add generic live scrolling plot widget"
```

---

### Task 14: `gui/widgets/stats_panel.py` — generic live stats sidebar

**Files:**
- Create: `gui/widgets/stats_panel.py`
- Test: `tests/gui/widgets/test_stats_panel.py`

**Interfaces:**
- Produces: `class StatsPanel(QWidget)`:
  - `def add_field(self, key: str, label: str) -> None`
  - `def set_value(self, key: str, value) -> None` (formats and updates that field's `QLabel` text; unregistered keys are silently ignored so future callers can add stats fields without every emitter needing to know the full set)

- [ ] **Step 1: Write the failing test**

Create `tests/gui/widgets/test_stats_panel.py`:

```python
from gui.widgets.stats_panel import StatsPanel


def test_add_field_and_set_value_updates_label_text(qapp):
    panel = StatsPanel()
    panel.add_field("frame_index", "Frame Index")
    panel.set_value("frame_index", 42)
    assert "42" in panel._value_labels["frame_index"].text()


def test_set_value_on_unregistered_key_is_ignored(qapp):
    panel = StatsPanel()
    panel.set_value("nonexistent", 123)  # must not raise


def test_multiple_fields_are_independent(qapp):
    panel = StatsPanel()
    panel.add_field("pairing_gap_us", "Pairing Gap (us)")
    panel.add_field("switch_time_ms", "Switch Time (ms)")
    panel.set_value("pairing_gap_us", -12.5)
    panel.set_value("switch_time_ms", 1.0)
    assert "-12.5" in panel._value_labels["pairing_gap_us"].text()
    assert "1.0" in panel._value_labels["switch_time_ms"].text()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/gui/widgets/test_stats_panel.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'gui.widgets.stats_panel'`.

- [ ] **Step 3: Write `gui/widgets/stats_panel.py`**

```python
"""Generic live key/value readout list - frame index, HW timestamp gap,
LED switch_time_ms today; add_field lets future callers register more
stats fields without changing this widget."""

from PySide6.QtWidgets import QWidget, QFormLayout, QLabel


class StatsPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout = QFormLayout(self)
        self._value_labels = {}

    def add_field(self, key, label):
        value_label = QLabel("-")
        self._value_labels[key] = value_label
        self._layout.addRow(QLabel(label), value_label)

    def set_value(self, key, value):
        if key not in self._value_labels:
            return
        self._value_labels[key].setText(str(value))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/gui/widgets/test_stats_panel.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add gui/widgets/stats_panel.py tests/gui/widgets/test_stats_panel.py
git commit -m "feat: add generic live stats sidebar widget"
```

---

### Task 15: `gui/pages/device_select_page.py` and `gui/pages/stream_config_page.py`

**Files:**
- Create: `gui/pages/device_select_page.py`
- Create: `gui/pages/stream_config_page.py`

**Interfaces:**
- Consumes: `engine.streams.list_devices`, `engine.streams.get_sensors_for_device`, `engine.streams.list_supported_profiles` (Task 10); `state.gui_state.GuiState` (Task 6).
- Produces:
  - `class DeviceSelectPage(QWidget)` — `device_chosen = Signal(str)` (emits the chosen serial); `def refresh_devices(self, ctx) -> None` populates a `QComboBox` from `list_devices`.
  - `class StreamConfigPage(QWidget)` — `config_chosen = Signal(tuple)` (emits `(ir_width, ir_height, ir_fps, rgb_width, rgb_height, rgb_fps)`); `def populate(self, stereo_sensor, rgb_sensor) -> None` fills FPS/resolution combo boxes from `list_supported_profiles`.
- Consumed by `gui/main_window.py` (Task 18).

No automated test for these two pages: both are thin forms whose only logic is "read `list_devices`/`list_supported_profiles` into combo boxes, emit a signal on Next" — the enumeration logic they call is already tested in Task 10, and populating a `QComboBox` from a list has no branching worth a synthetic test. Verified manually in Task 20 alongside the rest of the wizard flow.

- [ ] **Step 1: Write `gui/pages/device_select_page.py`**

```python
"""Wizard step 1: pick which connected RealSense device to use."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QComboBox, QPushButton

from engine.streams import list_devices


class DeviceSelectPage(QWidget):
    device_chosen = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._devices = []

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Select a connected RealSense device:"))
        self.combo = QComboBox()
        layout.addWidget(self.combo)
        self.next_button = QPushButton("Next")
        self.next_button.clicked.connect(self._on_next_clicked)
        layout.addWidget(self.next_button)

    def refresh_devices(self, ctx):
        self._devices = list_devices(ctx)
        self.combo.clear()
        for device in self._devices:
            self.combo.addItem("{} ({})".format(device.name, device.serial), userData=device.serial)

    def _on_next_clicked(self):
        serial = self.combo.currentData()
        if serial is not None:
            self.device_chosen.emit(serial)
```

- [ ] **Step 2: Write `gui/pages/stream_config_page.py`**

```python
"""Wizard step 2: pick FPS/resolution for the IR and RGB streams."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QFormLayout, QLabel, QComboBox, QPushButton

from engine.streams import list_supported_profiles


class StreamConfigPage(QWidget):
    config_chosen = Signal(tuple)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.ir_combo = QComboBox()
        self.rgb_combo = QComboBox()
        form.addRow(QLabel("IR resolution/fps:"), self.ir_combo)
        form.addRow(QLabel("RGB resolution/fps:"), self.rgb_combo)
        layout.addLayout(form)
        self.next_button = QPushButton("Next")
        self.next_button.clicked.connect(self._on_next_clicked)
        layout.addWidget(self.next_button)

    def populate(self, stereo_sensor, rgb_sensor):
        ir_profiles = list_supported_profiles(stereo_sensor, "infrared", "y8")
        rgb_profiles = list_supported_profiles(rgb_sensor, "color", "yuyv")

        self.ir_combo.clear()
        for width, height, fps in ir_profiles:
            self.ir_combo.addItem("{}x{}@{}fps".format(width, height, fps), userData=(width, height, fps))

        self.rgb_combo.clear()
        for width, height, fps in rgb_profiles:
            self.rgb_combo.addItem("{}x{}@{}fps".format(width, height, fps), userData=(width, height, fps))

    def _on_next_clicked(self):
        ir_choice = self.ir_combo.currentData()
        rgb_choice = self.rgb_combo.currentData()
        if ir_choice is not None and rgb_choice is not None:
            ir_width, ir_height, ir_fps = ir_choice
            rgb_width, rgb_height, rgb_fps = rgb_choice
            self.config_chosen.emit((ir_width, ir_height, ir_fps, rgb_width, rgb_height, rgb_fps))
```

- [ ] **Step 3: Commit**

```bash
git add gui/pages/device_select_page.py gui/pages/stream_config_page.py
git commit -m "feat: add device-select and stream-config wizard pages"
```

---

### Task 16: `gui/pages/roi_select_page.py` — embedded live preview with draggable ROI

**Files:**
- Create: `gui/pages/roi_select_page.py`

**Interfaces:**
- Consumes: `engine.session_engine.SessionEngineThread` (Task 11, used here with `test_session=None`-equivalent — frames only, see note below), `gui.widgets.video_panel.VideoPanel` (Task 12).
- Produces: `class RoiSelectPage(QWidget)` — `roi_chosen = Signal(tuple)` (emits `(ir_roi, rgb_roi)`, each a 4-tuple `(x, y, w, h)`); draws a draggable `QRubberBand` over each `VideoPanel` as live frames arrive.

Since `SessionEngineThread` always requires a `TestSession` (it drives `AcquisitionLoop`, which requires one), this page constructs a `TestSession` with an empty metrics list (`TestSessionConfig(metrics=[])`) — no metrics run, but the same frame-delivery path is reused rather than duplicating a second hardware-streaming mechanism.

No automated test for this page: `QRubberBand` mouse-drag interaction and live-frame wiring are both thin glue over already-tested pieces (`SessionEngineThread`, `VideoPanel`) with no independent branching logic; verified manually in Task 20.

- [ ] **Step 1: Write `gui/pages/roi_select_page.py`**

```python
"""Wizard step 3: live preview of both sensors, drag a box on each to
pick its ROI - replaces optical_sync_poc_/roi_picker.py's cv2.selectROI
popup with an in-app rubber-band selection over a live feed."""

from PySide6.QtCore import Signal, Qt, QRect
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QRubberBand

from gui.widgets.video_panel import VideoPanel
from engine.session_engine import SessionEngineThread
from engine.test_session import TestSession, TestSessionConfig


class _DraggableVideoPanel(VideoPanel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._rubber_band = QRubberBand(QRubberBand.Rectangle, self)
        self._origin = None
        self.roi = None

    def mousePressEvent(self, event):
        self._origin = event.pos()
        self._rubber_band.setGeometry(QRect(self._origin, event.pos()))
        self._rubber_band.show()

    def mouseMoveEvent(self, event):
        if self._origin is not None:
            self._rubber_band.setGeometry(QRect(self._origin, event.pos()).normalized())

    def mouseReleaseEvent(self, event):
        if self._origin is not None:
            rect = QRect(self._origin, event.pos()).normalized()
            self.roi = (rect.x(), rect.y(), rect.width(), rect.height())
            self._origin = None


class RoiSelectPage(QWidget):
    roi_chosen = Signal(tuple)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.engine_thread = None

        layout = QVBoxLayout(self)
        video_row = QHBoxLayout()
        self.ir_panel = _DraggableVideoPanel()
        self.rgb_panel = _DraggableVideoPanel()
        video_row.addWidget(self.ir_panel)
        video_row.addWidget(self.rgb_panel)
        layout.addLayout(video_row)
        layout.addWidget(QLabel("Drag a box on each preview to set its ROI, then click Next."))
        self.next_button = QPushButton("Next")
        self.next_button.clicked.connect(self._on_next_clicked)
        layout.addWidget(self.next_button)

    def start_preview(self, ctx, device_serial, ir_resolution, ir_fps, color_resolution, color_fps):
        test_session = TestSession(TestSessionConfig(metrics=[]))
        test_session.start()
        self.engine_thread = SessionEngineThread(
            ctx, device_serial, ir_resolution, ir_fps, color_resolution, color_fps, test_session,
        )
        self.engine_thread.frame_ready.connect(self._on_frame_ready)
        self.engine_thread.start()

    def stop_preview(self):
        if self.engine_thread is not None:
            self.engine_thread.request_stop()
            self.engine_thread.wait()
            self.engine_thread = None

    def _on_frame_ready(self, stream_name, image):
        if stream_name == "ir":
            self.ir_panel.set_frame(image)
        else:
            self.rgb_panel.set_frame(image)

    def _on_next_clicked(self):
        if self.ir_panel.roi is not None and self.rgb_panel.roi is not None:
            self.stop_preview()
            self.roi_chosen.emit((self.ir_panel.roi, self.rgb_panel.roi))
```

- [ ] **Step 2: Commit**

```bash
git add gui/pages/roi_select_page.py
git commit -m "feat: add embedded live-preview ROI selection page"
```

---

### Task 17: `gui/pages/calibration_page.py` — in-app LED calibration

**Files:**
- Create: `gui/pages/calibration_page.py`

**Interfaces:**
- Consumes: `domain.calibration.assign_grid_ids`, `build_positions_with_thresholds`, `update_config_leds` (Task 3); `domain.realsense_utils.detect_led_centroids`, `merge_close_centroids`, `apply_roi_mask` (Task 2); `engine.streams.ContinuousCapture`, `disable_ir_emitter`, `enable_auto_exposure`, `get_sensors_for_device` (Task 10); `engine.led_panel.LEDPanel` (Task 9).
- Produces: `class CalibrationPage(QWidget)` — `calibration_done = Signal()`; `def run_calibration(self, ctx, device_serial, ir_resolution, ir_fps, color_resolution, color_fps, ir_roi, rgb_roi, config_path) -> None` runs the same steps as `optical_sync_poc_/led_calibration.py`'s `main()`, appending progress lines to an in-page log widget instead of `print()`.

No automated test for this page: it is orchestration of already-tested domain functions (`assign_grid_ids`, `build_positions_with_thresholds`, `update_config_leds`) plus direct hardware calls (`ContinuousCapture`, `LEDPanel`) that cannot run without the physical rig. Verified manually in Task 20.

- [ ] **Step 1: Write `gui/pages/calibration_page.py`**

```python
"""Wizard step 4: runs LED calibration in-app (same steps as
optical_sync_poc_/led_calibration.py's main()), logging progress into a
QPlainTextEdit instead of print()."""

import time

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QPlainTextEdit, QPushButton

from domain.calibration import assign_grid_ids, build_positions_with_thresholds, update_config_leds
from domain.realsense_utils import detect_led_centroids, merge_close_centroids, apply_roi_mask
from engine.streams import ContinuousCapture, disable_ir_emitter, enable_auto_exposure, get_sensors_for_device
from engine.led_panel import LEDPanel


class CalibrationPage(QWidget):
    calibration_done = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view)
        self.run_button = QPushButton("Run Calibration")
        self.run_button.clicked.connect(self._on_run_clicked)
        layout.addWidget(self.run_button)
        self._pending_args = None

    def _log(self, message):
        self.log_view.appendPlainText(message)

    def set_context(self, ctx, device_serial, ir_resolution, ir_fps, color_resolution, color_fps,
                    ir_roi, rgb_roi, config_path, camera_name,
                    min_blob_area=20, neighborhood_size=5, row_gap_px=15, min_acceptable_contrast=20):
        self._pending_args = dict(
            ctx=ctx, device_serial=device_serial, ir_resolution=ir_resolution, ir_fps=ir_fps,
            color_resolution=color_resolution, color_fps=color_fps, ir_roi=ir_roi, rgb_roi=rgb_roi,
            config_path=config_path, camera_name=camera_name, min_blob_area=min_blob_area,
            neighborhood_size=neighborhood_size, row_gap_px=row_gap_px,
            min_acceptable_contrast=min_acceptable_contrast,
        )

    def _on_run_clicked(self):
        if self._pending_args is not None:
            self._run_calibration(**self._pending_args)

    def _run_calibration(self, ctx, device_serial, ir_resolution, ir_fps, color_resolution, color_fps,
                          ir_roi, rgb_roi, config_path, camera_name, min_blob_area, neighborhood_size,
                          row_gap_px, min_acceptable_contrast):
        stereo_sensor, rgb_sensor = get_sensors_for_device(ctx, device_serial)
        if not disable_ir_emitter(stereo_sensor):
            self._log("WARNING: emitter_enabled not supported - confirm the IR projector is off manually.")
        enable_auto_exposure(rgb_sensor)

        capture = ContinuousCapture(ir_resolution, ir_fps, color_resolution, color_fps)
        capture.start()
        frame_iter = capture.frames()

        self._log("Turning on all LEDs...")
        LEDPanel.stop()
        LEDPanel.all_leds_on()
        time.sleep(0.5)
        ir_on_image, rgb_on_image, _, _ = next(frame_iter)

        self._log("Turning LED panel off, capturing OFF-state frames...")
        LEDPanel.all_leds_off()
        time.sleep(0.5)
        ir_off_image, rgb_off_image, _, _ = next(frame_iter)
        capture.stop()

        ir_masked = apply_roi_mask(ir_on_image, ir_roi)
        rgb_masked = apply_roi_mask(rgb_on_image, rgb_roi)

        self._log("Detecting LEDs in IR frame...")
        ir_centroids, ir_otsu = detect_led_centroids(ir_masked, None, min_blob_area)
        ir_centroids = merge_close_centroids(ir_centroids)
        self._log("Detected {} LED(s) in IR (Otsu threshold {}).".format(len(ir_centroids), ir_otsu))
        ir_positions, ir_row_layout = assign_grid_ids(ir_centroids, row_gap_px)

        self._log("Detecting LEDs in RGB frame...")
        rgb_centroids, rgb_otsu = detect_led_centroids(rgb_masked, None, min_blob_area)
        rgb_centroids = merge_close_centroids(rgb_centroids)
        self._log("Detected {} LED(s) in RGB (Otsu threshold {}).".format(len(rgb_centroids), rgb_otsu))
        rgb_positions, rgb_row_layout = assign_grid_ids(rgb_centroids, row_gap_px)

        if ir_row_layout != rgb_row_layout:
            self._log(
                "WARNING: IR row layout {} != RGB row layout {} - led_id may not match the same "
                "physical LED in both dicts.".format(ir_row_layout, rgb_row_layout)
            )

        self._log("Computing per-LED on/off/threshold values...")
        ir_positions = build_positions_with_thresholds(ir_positions, ir_on_image, ir_off_image, neighborhood_size)
        rgb_positions = build_positions_with_thresholds(rgb_positions, rgb_on_image, rgb_off_image, neighborhood_size)

        for label, positions in (("IR", ir_positions), ("RGB", rgb_positions)):
            weakest_id, weakest_contrast = min(
                ((led_id, vals[2] - vals[3]) for led_id, vals in positions.items()),
                key=lambda pair: pair[1],
            )
            self._log("{} weakest LED contrast: led_id={} on-off={:.2f}".format(label, weakest_id, weakest_contrast))
            if weakest_contrast < min_acceptable_contrast:
                self._log("  WARNING: this LED's on/off gap is small - its threshold may be unreliable.")

        update_config_leds(config_path, camera_name, ir_positions, ir_resolution, rgb_positions, color_resolution)
        self._log("Saved {} LED positions per sensor to {}".format(len(ir_positions), config_path))
        self.calibration_done.emit()
```

- [ ] **Step 2: Commit**

```bash
git add gui/pages/calibration_page.py
git commit -m "feat: add in-app LED calibration wizard page"
```

---

### Task 18: `gui/pages/live_session_page.py` — the live sync-test view

**Files:**
- Create: `gui/pages/live_session_page.py`

**Interfaces:**
- Consumes: `engine.session_engine.SessionEngineThread` (Task 11); `engine.test_session.TestSession`, `TestSessionConfig` (Task 7); `engine.metrics.PairingGapMetric`, `PositionGapMetric` (Task 4); `domain.csv_export.export_session_csvs` (Task 5); `gui.widgets.video_panel.VideoPanel` (Task 12); `gui.widgets.live_plot.LivePlot` (Task 13); `gui.widgets.stats_panel.StatsPanel` (Task 14).
- Produces: `class LiveSessionPage(QWidget)` — the core deliverable: two `VideoPanel`s side by side, a `LivePlot` with `pairing_gap_us` and `position_gap_ms` series (each with its own visibility checkbox), a `StatsPanel` with `frame_index`, `pairing_gap_us`, `switch_time_ms` fields, an optional duration `QSpinBox` (0 = manual stop only), Start/Stop buttons, and a status `QLabel` that surfaces `SessionEngineThread.error` (e.g. camera unplugged mid-session) and resets the Start/Stop button state so the operator can retry — this is required for Task 20's manual verification step, which explicitly checks that an unplugged camera surfaces an error and re-enables Start.

No automated test for this page: it wires already-tested pieces (`SessionEngineThread`, `TestSession`, the three widgets, `export_session_csvs`) together with no independent computation of its own. Verified manually in Task 20, which is where this page's actual behavior (does the plot really scroll live, do both metrics toggle, does Stop produce a correct CSV) gets checked against real hardware.

- [ ] **Step 1: Write `gui/pages/live_session_page.py`**

```python
"""Wizard step 5 - the live sync-test view: dual video panels, a
togglable/stacked live plot of both metrics, a live stats sidebar, and
Start/Stop with an optional fixed duration. Produces a CSV at Stop via
domain.csv_export.export_session_csvs, same spirit as
optical_sync_poc_/pipeline_sync_test_diff.py's write_raw_csvs."""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QSpinBox, QLabel, QCheckBox,
)

from gui.widgets.video_panel import VideoPanel
from gui.widgets.live_plot import LivePlot
from gui.widgets.stats_panel import StatsPanel
from engine.session_engine import SessionEngineThread
from engine.test_session import TestSession, TestSessionConfig
from engine.metrics import PairingGapMetric, PositionGapMetric
from domain.csv_export import export_session_csvs


class LiveSessionPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.engine_thread = None
        self._context = None

        layout = QVBoxLayout(self)

        video_row = QHBoxLayout()
        self.ir_panel = VideoPanel()
        self.rgb_panel = VideoPanel()
        video_row.addWidget(self.ir_panel)
        video_row.addWidget(self.rgb_panel)
        layout.addLayout(video_row)

        toggle_row = QHBoxLayout()
        self.pairing_gap_checkbox = QCheckBox("Pairing gap (us)")
        self.pairing_gap_checkbox.setChecked(True)
        self.pairing_gap_checkbox.toggled.connect(
            lambda checked: self.live_plot.set_series_visible("pairing_gap_us", checked)
        )
        self.position_gap_checkbox = QCheckBox("Position gap (ms)")
        self.position_gap_checkbox.setChecked(True)
        self.position_gap_checkbox.toggled.connect(
            lambda checked: self.live_plot.set_series_visible("position_gap_ms", checked)
        )
        toggle_row.addWidget(self.pairing_gap_checkbox)
        toggle_row.addWidget(self.position_gap_checkbox)
        layout.addLayout(toggle_row)

        bottom_row = QHBoxLayout()
        self.live_plot = LivePlot()
        self.live_plot.add_series("pairing_gap_us", color="r")
        self.live_plot.add_series("position_gap_ms", color="g")
        bottom_row.addWidget(self.live_plot, stretch=2)

        self.stats_panel = StatsPanel()
        self.stats_panel.add_field("frame_index", "Frame Index")
        self.stats_panel.add_field("pairing_gap_us", "HW Timestamp Gap (us)")
        self.stats_panel.add_field("switch_time_ms", "LED Switch Time (ms)")
        bottom_row.addWidget(self.stats_panel, stretch=1)
        layout.addLayout(bottom_row)

        control_row = QHBoxLayout()
        control_row.addWidget(QLabel("Duration (s, 0 = manual stop):"))
        self.duration_spinbox = QSpinBox()
        self.duration_spinbox.setRange(0, 3600)
        control_row.addWidget(self.duration_spinbox)
        self.start_button = QPushButton("Start")
        self.start_button.clicked.connect(self.start_session)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop_session)
        self.stop_button.setEnabled(False)
        control_row.addWidget(self.start_button)
        control_row.addWidget(self.stop_button)
        layout.addLayout(control_row)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

    def set_context(self, ctx, device_serial, ir_resolution, ir_fps, color_resolution, color_fps,
                    switch_time_ms, ir_threshold, rgb_threshold, ir_xy, rgb_xy, num_leds,
                    frame_drop_threshold_factor, warmup_pairs_to_skip, pairing_gap_outlier_threshold_us,
                    kept_csv_path, dropped_csv_path):
        self._context = dict(
            ctx=ctx, device_serial=device_serial, ir_resolution=ir_resolution, ir_fps=ir_fps,
            color_resolution=color_resolution, color_fps=color_fps, switch_time_ms=switch_time_ms,
            ir_threshold=ir_threshold, rgb_threshold=rgb_threshold, ir_xy=ir_xy, rgb_xy=rgb_xy,
            num_leds=num_leds,
            frame_drop_threshold_factor=frame_drop_threshold_factor,
            warmup_pairs_to_skip=warmup_pairs_to_skip,
            pairing_gap_outlier_threshold_us=pairing_gap_outlier_threshold_us,
            kept_csv_path=kept_csv_path, dropped_csv_path=dropped_csv_path,
        )
        self.stats_panel.set_value("switch_time_ms", switch_time_ms)

    def start_session(self):
        ctx = self._context
        duration_s = self.duration_spinbox.value() or None
        metrics = [
            PairingGapMetric(outlier_threshold_us=ctx["pairing_gap_outlier_threshold_us"]),
            PositionGapMetric(
                ir_threshold=ctx["ir_threshold"], rgb_threshold=ctx["rgb_threshold"], num_leds=ctx["num_leds"],
                switch_time_ms=ctx["switch_time_ms"], ir_fps=ctx["ir_fps"], rgb_fps=ctx["color_fps"],
                frame_drop_threshold_factor=ctx["frame_drop_threshold_factor"],
                warmup_pairs_to_skip=ctx["warmup_pairs_to_skip"],
            ),
        ]
        test_session = TestSession(TestSessionConfig(metrics=metrics, duration_s=duration_s))
        test_session.start()

        self.engine_thread = SessionEngineThread(
            ctx["ctx"], ctx["device_serial"], ctx["ir_resolution"], ctx["ir_fps"],
            ctx["color_resolution"], ctx["color_fps"], test_session,
            ir_xy=ctx["ir_xy"], rgb_xy=ctx["rgb_xy"],
        )
        self.engine_thread.frame_ready.connect(self._on_frame_ready)
        self.engine_thread.row_ready.connect(self._on_row_ready)
        self.engine_thread.stats_ready.connect(self._on_stats_ready)
        self.engine_thread.session_finished.connect(self._on_session_finished)
        self.engine_thread.error.connect(self._on_error)
        self.engine_thread.start()

        self.status_label.setText("")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)

    def stop_session(self):
        if self.engine_thread is not None:
            self.engine_thread.request_stop()

    def _on_frame_ready(self, stream_name, image):
        if stream_name == "ir":
            self.ir_panel.set_frame(image)
        else:
            self.rgb_panel.set_frame(image)

    def _on_row_ready(self, row):
        # Fired every frame-pair (not throttled) - the plot gets every point
        # so the graph itself isn't affected by the video-display stride.
        pair_index = row["pair_index"]
        if row.get("pairing_gap_us") is not None:
            self.live_plot.add_point("pairing_gap_us", pair_index, row["pairing_gap_us"])
        if row.get("position_gap_ms") is not None:
            self.live_plot.add_point("position_gap_ms", pair_index, row["position_gap_ms"])

    def _on_stats_ready(self, stats):
        # Fired only at the throttled display_stride cadence (same frames
        # the video panels update on), so the shown frame index always
        # matches what's visually on screen right now.
        self.stats_panel.set_value("frame_index", stats["pair_index"])
        if stats.get("pairing_gap_us") is not None:
            self.stats_panel.set_value("pairing_gap_us", stats["pairing_gap_us"])

    def _on_session_finished(self, rows):
        export_session_csvs(rows, self._context["kept_csv_path"], self._context["dropped_csv_path"])
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    def _on_error(self, message):
        # Surfaces a hardware failure (e.g. camera unplugged mid-session) to
        # the operator and resets controls so Start can be retried, rather
        # than leaving Stop enabled against a worker thread that already
        # exited its run() loop.
        self.status_label.setText("Error: {}".format(message))
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
```

- [ ] **Step 2: Commit**

```bash
git add gui/pages/live_session_page.py
git commit -m "feat: add live sync-test session page"
```

---

### Task 19: `gui/main_window.py` and `main.py` — wizard shell and entry point

**Files:**
- Create: `gui/main_window.py`
- Create: `main.py`

**Interfaces:**
- Consumes: all five page classes (Tasks 15-18), `state.gui_state.load_gui_state`/`save_gui_state` (Task 6), `domain.calibration.load_led_positions` (Task 3), `settings.py`'s existing `load_settings`/`ensure_output_dir` (read-only defaults source, per the design's "settings.yaml stays hand-edited/read-only" decision).
- Produces: `class MainWindow(QMainWindow)` — a `QStackedWidget` holding the five pages in order, connecting each page's "chosen" signal to advance to the next page and to `state.gui_state.save_gui_state`; after calibration finishes, loads the just-calibrated LED positions and computes threshold arrays before handing control to the live session page. `main.py`'s `if __name__ == "__main__":` block creates the `QApplication`, an `rs.context()`, and the `MainWindow`.

No automated test: this is pure UI wiring (advance `QStackedWidget.setCurrentIndex` on each signal) with no independent logic. Verified manually in Task 20's end-to-end walkthrough.

- [ ] **Step 1: Write `gui/main_window.py`**

```python
"""Wizard shell: Device select -> Stream config -> ROI select ->
Calibration -> Live session, in a QStackedWidget, persisting choices to
state.gui_state as the user moves through the wizard."""

import os

import numpy as np
from PySide6.QtWidgets import QMainWindow, QStackedWidget

from gui.pages.device_select_page import DeviceSelectPage
from gui.pages.stream_config_page import StreamConfigPage
from gui.pages.roi_select_page import RoiSelectPage
from gui.pages.calibration_page import CalibrationPage
from gui.pages.live_session_page import LiveSessionPage
from state.gui_state import GuiState, save_gui_state
from engine.streams import get_sensors_for_device
from domain.calibration import load_led_positions
from settings import ensure_output_dir


class MainWindow(QMainWindow):
    def __init__(self, ctx, gui_state: GuiState, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Optical Sync GUI")
        self.ctx = ctx
        self.gui_state = gui_state
        self.settings = settings

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.device_page = DeviceSelectPage()
        self.stream_config_page = StreamConfigPage()
        self.roi_page = RoiSelectPage()
        self.calibration_page = CalibrationPage()
        self.live_session_page = LiveSessionPage()

        for page in (self.device_page, self.stream_config_page, self.roi_page,
                     self.calibration_page, self.live_session_page):
            self.stack.addWidget(page)

        self.device_page.device_chosen.connect(self._on_device_chosen)
        self.stream_config_page.config_chosen.connect(self._on_config_chosen)
        self.roi_page.roi_chosen.connect(self._on_roi_chosen)
        self.calibration_page.calibration_done.connect(self._on_calibration_done)

        self.device_page.refresh_devices(self.ctx)
        self.stack.setCurrentWidget(self.device_page)

    def _on_device_chosen(self, serial):
        self.gui_state.device_serial = serial
        save_gui_state(self.gui_state)
        stereo_sensor, rgb_sensor = get_sensors_for_device(self.ctx, serial)
        self.stream_config_page.populate(stereo_sensor, rgb_sensor)
        self.stack.setCurrentWidget(self.stream_config_page)

    def _on_config_chosen(self, config):
        ir_width, ir_height, ir_fps, rgb_width, rgb_height, rgb_fps = config
        self.gui_state.ir_width, self.gui_state.ir_height, self.gui_state.ir_fps = ir_width, ir_height, ir_fps
        self.gui_state.rgb_width, self.gui_state.rgb_height, self.gui_state.rgb_fps = rgb_width, rgb_height, rgb_fps
        save_gui_state(self.gui_state)
        self.roi_page.start_preview(
            self.ctx, self.gui_state.device_serial,
            (ir_width, ir_height), ir_fps, (rgb_width, rgb_height), rgb_fps,
        )
        self.stack.setCurrentWidget(self.roi_page)

    def _on_roi_chosen(self, rois):
        ir_roi, rgb_roi = rois
        self.gui_state.ir_roi = list(ir_roi)
        self.gui_state.rgb_roi = list(rgb_roi)
        save_gui_state(self.gui_state)
        self.calibration_page.set_context(
            self.ctx, self.gui_state.device_serial,
            (self.gui_state.ir_width, self.gui_state.ir_height), self.gui_state.ir_fps,
            (self.gui_state.rgb_width, self.gui_state.rgb_height), self.gui_state.rgb_fps,
            ir_roi, rgb_roi,
            config_path=self.settings["paths"]["config_path"],
            camera_name=self._current_device_name(),
        )
        self.stack.setCurrentWidget(self.calibration_page)

    def _on_calibration_done(self):
        camera_name = self._current_device_name()
        config_path = self.settings["paths"]["config_path"]
        ir_positions, rgb_positions = load_led_positions(config_path, camera_name)

        ir_ids = list(ir_positions.keys())
        rgb_ids = list(rgb_positions.keys())
        ir_xy = np.array([ir_positions[i][:2] for i in ir_ids])
        rgb_xy = np.array([rgb_positions[i][:2] for i in rgb_ids])
        ir_on = np.array([ir_positions[i][2] for i in ir_ids])
        ir_off = np.array([ir_positions[i][3] for i in ir_ids])
        rgb_on = np.array([rgb_positions[i][2] for i in rgb_ids])
        rgb_off = np.array([rgb_positions[i][3] for i in rgb_ids])

        threshold_fraction = self.settings["test"]["threshold_fraction"]
        ir_threshold = ir_off + threshold_fraction * (ir_on - ir_off)
        rgb_threshold = rgb_off + threshold_fraction * (rgb_on - rgb_off)

        output_dir = ensure_output_dir(self.settings)
        self.live_session_page.set_context(
            self.ctx, self.gui_state.device_serial,
            (self.gui_state.ir_width, self.gui_state.ir_height), self.gui_state.ir_fps,
            (self.gui_state.rgb_width, self.gui_state.rgb_height), self.gui_state.rgb_fps,
            switch_time_ms=self.settings["test"]["switch_time_ms"],
            ir_threshold=ir_threshold, rgb_threshold=rgb_threshold, ir_xy=ir_xy, rgb_xy=rgb_xy,
            num_leds=self.settings["test"]["num_leds"],
            frame_drop_threshold_factor=self.settings["test"]["frame_drop_threshold_factor"],
            warmup_pairs_to_skip=self.settings["test"]["warmup_pairs_to_skip"],
            pairing_gap_outlier_threshold_us=self.settings["test"]["pairing_gap_outlier_threshold_us"],
            kept_csv_path=os.path.join(output_dir, self.settings["paths"]["raw_csv_path"]),
            dropped_csv_path=os.path.join(output_dir, self.settings["paths"]["frame_drop_csv_path"]),
        )
        self.stack.setCurrentWidget(self.live_session_page)

    def _current_device_name(self):
        for device in self.device_page._devices:
            if device.serial == self.gui_state.device_serial:
                return device.name
        raise RuntimeError("Selected device serial no longer connected")
```

- [ ] **Step 2: Write `main.py`**

```python
"""Entry point: creates the QApplication, a pyrealsense2 context, loads
settings.yaml (read-only defaults) and gui_state.json (the GUI's own
persisted choices), and shows the MainWindow wizard."""

import sys

import pyrealsense2 as rs
from PySide6.QtWidgets import QApplication

from gui.main_window import MainWindow
from state.gui_state import load_gui_state
from settings import load_settings


def main():
    app = QApplication(sys.argv)
    ctx = rs.context()
    gui_state = load_gui_state()
    settings = load_settings()

    window = MainWindow(ctx, gui_state, settings)
    window.resize(1200, 800)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Copy `settings.py` and `settings.yaml` from the POC project as the read-only defaults source**

```bash
cp "../optical_sync_poc_/settings.py" .
cp "../optical_sync_poc_/settings.yaml" .
cp "../optical_sync_poc_/config.yaml" .
```

- [ ] **Step 4: Commit**

```bash
git add gui/main_window.py main.py settings.py settings.yaml config.yaml
git commit -m "feat: add wizard shell and application entry point"
```

---

### Task 20: End-to-end manual hardware verification

**Files:** none created — this task documents the manual verification pass required before considering the feature done, per the plan's constraint that hardware-touching code cannot be exercised by automated tests in this environment.

- [ ] **Step 1: Run the full automated test suite one more time**

Run: `pytest -v`
Expected: every test from Tasks 2-14 passes (domain, engine's pure-logic pieces, state, and the three GUI widgets).

- [ ] **Step 2: Manual walkthrough with real hardware connected**

With a RealSense device (Stereo Module + RGB Camera), the LED panel, and `LED-Panel.exe` all connected/available, run:

```bash
python main.py
```

Walk through and confirm:
- Device select page lists the connected device(s); choosing one advances to stream config.
- Stream config page's combo boxes show real supported resolutions/fps for both sensors; choosing values advances to ROI select.
- ROI select page shows two live-updating video feeds; dragging a box on each and clicking Next advances to calibration.
- Calibration page's log fills in as calibration runs (mirroring `led_calibration.py`'s console output) and `config.yaml` gets a fresh sub-block for the connected camera's model name.
- Live session page shows both video feeds updating live; clicking Start begins the LED panel scan and both plot series scroll live; unchecking either checkbox hides that series without stopping the other; the stats sidebar's frame index, HW timestamp gap, and switch-time values update live; clicking Stop (or letting a configured duration elapse) stops the LED panel and writes two CSVs whose columns match `domain/csv_export.py`'s schema.

- [ ] **Step 3: Confirm graceful handling of a disconnected/unplugged camera mid-session**

Unplug the RealSense device while a live session is running.
Expected: `SessionEngineThread.error` fires with a message rather than the app hanging or crashing; the UI re-enables the Start button so the user can retry after reconnecting.

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "docs: complete end-to-end manual hardware verification"
```
