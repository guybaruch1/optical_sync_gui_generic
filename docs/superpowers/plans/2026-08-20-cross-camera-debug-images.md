# Cross-Camera Debug Images Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Save outlier-triggered and periodic debug images of the *actual* matched frames for cross-camera pairs, with the pair index, both raw HW timestamps, both global timestamps, and all three cross-camera metrics burned into the image.

**Architecture:** `SessionEngineThread` gains a small, lock-protected ring buffer of recent frames (keyed by that camera's own `pair_index`), populated from its existing unthrottled `on_frame_pair` callback. `MultiCameraLiveSessionPage`'s existing unthrottled `_on_cross_pair_ready` gains a cheap outlier/periodic check; on a genuine trigger it looks up both cameras' actual matched images via that ring buffer (reached through `MultiCameraSessionController`'s existing public `threads` property), draws a new overlay, and writes the file - reusing the master camera's own already-configured thresholds, no new settings.

**Tech Stack:** Python 3.10+/3.13, PySide6, opencv-python, pytest (`QT_QPA_PLATFORM=offscreen`, shared `qapp` fixture).

## Global Constraints

- No new settings.yaml keys - outlier threshold/cap and periodic interval/cap all reuse the MASTER camera's own already-configured `position_gap_outlier_threshold_ms`/`position_gap_outlier_max_snapshots`/`snapshot_every_n_pairs`/`max_snapshots`.
- Outlier trigger is Optical Sync (`position_gap_ms`) only - never HW TS Latency or Global TS Latency.
- Periodic/outlier counters are tracked per `(slave_camera_id, stream_identity)` spec independently, never one shared counter.
- The debug image must show the frames the reconciler ACTUALLY matched (looked up by `master_pair_index`/`slave_pair_index` from the cross-row), never an approximation from the throttled `frame_ready` path. If either side's frame has aged out of its ring buffer, skip the save entirely - no partial/misleading image.
- `CrossCameraReconciler` stays pure Python - no Qt, no images, no file I/O added to it.
- No live video panels added to the Cross-Camera Sync tab - this plan is saved debug images only.

---

### Task 1: `SessionEngineThread` gains a recent-frame ring buffer

**Files:**
- Modify: `engine/session_engine.py`
- Test: `tests/engine/test_session_engine.py` (new file - scoped ONLY to the new pure ring-buffer methods; this class's hardware/Qt-facing `run()` internals stay untested by design, per this project's existing convention)

**Interfaces:**
- Produces: `SessionEngineThread.get_recent_frame_pair(pair_index) -> (stream_a_image, stream_b_image) | None` - a new public method. Populated internally by a new `_record_recent_frame(pair_index, stream_a_image, stream_b_image)` method, called from the existing `on_frame_pair` callback inside `run()`.

- [ ] **Step 1: Write the failing tests**

Create `tests/engine/test_session_engine.py`:

```python
"""Pure-Python-testable slice of SessionEngineThread: the recent-frame
ring buffer (_record_recent_frame/get_recent_frame_pair) that backs
cross-camera debug images (gui/pages/multi_camera_live_session_page.py).
Everything else about this class is hardware/Qt-facing and untested by
design (see CLAUDE.md) - constructing a SessionEngineThread with dummy
args is safe (no hardware/Qt event loop is touched until .start()/.run()
actually runs), so this file is scoped ONLY to the ring buffer, never
calling those."""

from engine.session_engine import SessionEngineThread, _RECENT_FRAMES_MAXLEN


def _make_thread(qapp):
    return SessionEngineThread(
        ctx=None, device_serial="SN1", pick_a={}, pick_b={}, camera_controls={}, test_session=None,
    )


def test_get_recent_frame_pair_returns_none_when_never_recorded(qapp):
    thread = _make_thread(qapp)
    assert thread.get_recent_frame_pair(5) is None


def test_get_recent_frame_pair_returns_the_recorded_images(qapp):
    thread = _make_thread(qapp)
    thread._record_recent_frame(5, "image_a_5", "image_b_5")

    assert thread.get_recent_frame_pair(5) == ("image_a_5", "image_b_5")


def test_get_recent_frame_pair_distinguishes_between_pair_indices(qapp):
    thread = _make_thread(qapp)
    thread._record_recent_frame(1, "a1", "b1")
    thread._record_recent_frame(2, "a2", "b2")

    assert thread.get_recent_frame_pair(1) == ("a1", "b1")
    assert thread.get_recent_frame_pair(2) == ("a2", "b2")
    assert thread.get_recent_frame_pair(3) is None


def test_recent_frame_buffer_evicts_oldest_past_its_maxlen(qapp):
    thread = _make_thread(qapp)

    for i in range(_RECENT_FRAMES_MAXLEN + 5):
        thread._record_recent_frame(i, "a{}".format(i), "b{}".format(i))

    # The first 5 pair_indices (0-4) must have aged out - only the most
    # recent _RECENT_FRAMES_MAXLEN entries survive.
    assert thread.get_recent_frame_pair(0) is None
    assert thread.get_recent_frame_pair(4) is None
    assert thread.get_recent_frame_pair(5) == ("a5", "b5")
    last_index = _RECENT_FRAMES_MAXLEN + 4
    assert thread.get_recent_frame_pair(last_index) == ("a{}".format(last_index), "b{}".format(last_index))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_session_engine.py -v`
Expected: FAIL - `ImportError: cannot import name '_RECENT_FRAMES_MAXLEN'` (module doesn't have it yet), and `AttributeError: 'SessionEngineThread' object has no attribute 'get_recent_frame_pair'`.

- [ ] **Step 3: Implement**

In `engine/session_engine.py`, add these two imports right after the existing `import os` (currently line 38):

```python
import os
import collections
import threading
```

Add this module-level constant right after the imports, before `class SessionEngineThread(QThread):` (currently line 57):

```python
# Sized to match engine.cross_camera_reconciler.CrossCameraReconciler's own
# default row-buffer depth (fps_hint=30.0, buffer_seconds=1.0, neither
# currently overridden anywhere) - see SessionEngineThread.get_recent_frame_pair's
# own docstring for why: an image must stay available at least as long as
# the reconciler could still have a row buffered waiting to match against it.
_RECENT_FRAMES_MAXLEN = 30
```

In `SessionEngineThread.__init__`, add this right after `self._stop_requested = False` (currently line 131, just before `self._capture = None`):

```python
        self._stop_requested = False
        # Ring buffer of recent (pair_index, stream_a_image, stream_b_image)
        # tuples, populated from the existing unthrottled on_frame_pair
        # callback (not the throttled display_stride path) - lets
        # gui/pages/multi_camera_live_session_page.py's cross-camera debug
        # image feature find the ACTUAL matched frames later, from the GUI
        # thread, after engine.cross_camera_reconciler.CrossCameraReconciler
        # resolves a match asynchronously. Lock-protected since it's read
        # cross-thread.
        self._recent_frames = collections.deque(maxlen=_RECENT_FRAMES_MAXLEN)
        self._recent_frames_lock = threading.Lock()
        self._capture = None
```

Add these two new methods right after `_maybe_save_position_gap_outlier` (currently ends at line 195, right before `def run(self):`):

```python
    def _record_recent_frame(self, pair_index, stream_a_image, stream_b_image):
        with self._recent_frames_lock:
            self._recent_frames.append((pair_index, stream_a_image, stream_b_image))

    def get_recent_frame_pair(self, pair_index):
        """(stream_a_image, stream_b_image) for the given pair_index if
        still in the ring buffer, else None. Called from the GUI thread
        once engine.cross_camera_reconciler.CrossCameraReconciler resolves
        a cross-camera match, to look up the actual frames that produced
        it - not an approximation from the throttled frame_ready/display
        path. Thread-safe: this camera's own background thread keeps
        appending to the same deque concurrently via _record_recent_frame."""
        with self._recent_frames_lock:
            for stored_pair_index, stream_a_image, stream_b_image in self._recent_frames:
                if stored_pair_index == pair_index:
                    return stream_a_image, stream_b_image
        return None
```

Modify the `on_frame_pair` closure inside `run()` (currently lines 295-296):

```python
            def on_frame_pair(stream_a_image, stream_b_image, row):
                self._record_recent_frame(row["pair_index"], stream_a_image, stream_b_image)
                self._maybe_save_position_gap_outlier(stream_a_image, stream_b_image, row)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_session_engine.py -v`
Expected: PASS (every test in this new file).

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python.exe -m pytest -v`
Expected: PASS (every test in the project).

- [ ] **Step 6: Commit**

```bash
git add engine/session_engine.py tests/engine/test_session_engine.py
git commit -m "feat: SessionEngineThread gains a recent-frame ring buffer for cross-camera debug images"
```

---

### Task 2: New overlay function `draw_cross_camera_debug_overlay`

**Files:**
- Modify: `domain/realsense_utils.py`
- Test: `tests/domain/test_realsense_utils.py`

**Interfaces:**
- Produces: `draw_cross_camera_debug_overlay(image, cross_pair_index, master_pair_index, slave_pair_index, master_ts_us, slave_ts_us, master_global_ts_us, slave_global_ts_us, pairing_gap_us, global_ts_gap_us, position_gap_ms) -> np.ndarray` (BGR image, a drawn copy - never mutates `image`). `position_gap_ms` may be `None` (a "miss" pair) and must not raise.

- [ ] **Step 1: Write the failing tests**

Add this import to `tests/domain/test_realsense_utils.py`'s existing import block for `domain.realsense_utils` (find the exact current import line/statement and add `draw_cross_camera_debug_overlay` to it, alongside `draw_bundle_overlay`).

Add these tests right after `test_draw_bundle_overlay_does_not_mutate_bgr_input` (currently ending around line 420-421):

```python
def test_draw_cross_camera_debug_overlay_converts_grayscale_and_draws_text():
    image = np.zeros((100, 300), dtype=np.uint8)

    result = draw_cross_camera_debug_overlay(
        image, cross_pair_index=42, master_pair_index=100, slave_pair_index=98,
        master_ts_us=1_000_000.0, slave_ts_us=1_000_010.0,
        master_global_ts_us=2_000_000.0, slave_global_ts_us=2_000_012.0,
        pairing_gap_us=-5.0, global_ts_gap_us=-12.0, position_gap_ms=1.5,
    )

    assert result.shape == (100, 300, 3)  # grayscale input converted to BGR for drawing
    assert result is not image  # never mutates the caller's array
    assert (result > 0).any()  # some text pixels were actually drawn


def test_draw_cross_camera_debug_overlay_does_not_mutate_bgr_input():
    image = np.zeros((100, 300, 3), dtype=np.uint8)

    result = draw_cross_camera_debug_overlay(
        image, cross_pair_index=0, master_pair_index=0, slave_pair_index=0,
        master_ts_us=0.0, slave_ts_us=0.0, master_global_ts_us=0.0, slave_global_ts_us=0.0,
        pairing_gap_us=0.0, global_ts_gap_us=0.0, position_gap_ms=0.0,
    )

    assert (image == 0).all()  # original untouched
    assert (result > 0).any()  # the copy has the drawn text


def test_draw_cross_camera_debug_overlay_handles_none_position_gap():
    image = np.zeros((50, 200), dtype=np.uint8)

    # Must not raise when Optical Sync is a "miss" (position_gap_ms=None) -
    # a real, common case (no clear on-LED detected that frame).
    result = draw_cross_camera_debug_overlay(
        image, cross_pair_index=1, master_pair_index=1, slave_pair_index=1,
        master_ts_us=0.0, slave_ts_us=0.0, master_global_ts_us=0.0, slave_global_ts_us=0.0,
        pairing_gap_us=0.0, global_ts_gap_us=0.0, position_gap_ms=None,
    )

    assert result.shape == (50, 200, 3)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/domain/test_realsense_utils.py -k cross_camera_debug_overlay -v`
Expected: FAIL - `ImportError: cannot import name 'draw_cross_camera_debug_overlay'`.

- [ ] **Step 3: Implement**

In `domain/realsense_utils.py`, add this new function right after `draw_bundle_overlay` (currently ends at line 329):

```python
def draw_cross_camera_debug_overlay(image, cross_pair_index, master_pair_index, slave_pair_index,
                                     master_ts_us, slave_ts_us, master_global_ts_us, slave_global_ts_us,
                                     pairing_gap_us, global_ts_gap_us, position_gap_ms):
    """Burns a cross-camera debug diagnostic overlay (cross pair index,
    each camera's own pair_index, both raw HW timestamps, both global
    timestamps, and all three cross-camera metrics) onto a copy of the
    master's frame - used by gui/pages/multi_camera_live_session_page.py's
    outlier/periodic cross-camera debug images, mirroring
    draw_bundle_overlay's own cv2.putText convention exactly.
    position_gap_ms may be None (a "miss" pair - no clear on-LED detected
    by one or both cameras that frame)."""
    debug_img = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR) if len(image.shape) == 2 else image.copy()
    font = cv2.FONT_HERSHEY_SIMPLEX
    position_gap_text = "n/a" if position_gap_ms is None else "{:.2f} ms".format(position_gap_ms)
    lines = [
        ("Cross Pair: {}".format(cross_pair_index), (0, 255, 0)),
        ("Master Pair: {}  |  Slave Pair: {}".format(master_pair_index, slave_pair_index), (0, 255, 255)),
        ("Master HW TS: {:.0f}  |  Slave HW TS: {:.0f}".format(master_ts_us, slave_ts_us), (0, 255, 255)),
        ("Master Global TS: {:.0f}  |  Slave Global TS: {:.0f}".format(master_global_ts_us, slave_global_ts_us), (0, 255, 255)),
        ("HW TS Latency: {:.1f} us".format(pairing_gap_us), (255, 255, 0)),
        ("Global TS Latency: {:.1f} us".format(global_ts_gap_us), (255, 255, 0)),
        ("Optical Sync: {}".format(position_gap_text), (255, 255, 0)),
    ]
    y = 25
    for text, color in lines:
        cv2.putText(debug_img, text, (10, y), font, 0.6, color, 2)
        y += 25
    return debug_img
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/domain/test_realsense_utils.py -v`
Expected: PASS (every test in this file).

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python.exe -m pytest -v`
Expected: PASS (every test in the project).

- [ ] **Step 6: Commit**

```bash
git add domain/realsense_utils.py tests/domain/test_realsense_utils.py
git commit -m "feat: add draw_cross_camera_debug_overlay for cross-camera debug images"
```

---

### Task 3: `_build_cross_row` returns the raw global timestamps too

**Files:**
- Modify: `engine/cross_camera_reconciler.py`
- Test: `tests/engine/test_cross_camera_reconciler.py`

**Interfaces:**
- Produces: every cross-row dict `CrossCameraReconciler.ingest_row()` returns now also carries `master_global_ts_us`/`slave_global_ts_us` (raw, unadjusted - same "transparency" convention as the existing `master_ts_us`/`slave_ts_us` fields), alongside every field it already returns.

- [ ] **Step 1: Write the failing test**

Add this test to `tests/engine/test_cross_camera_reconciler.py`, right after `test_master_row_then_slave_row_produces_a_matched_cross_row` (find its current exact location - it's the first test in the matching-tests section):

```python
def test_cross_row_carries_the_raw_global_timestamps_too():
    spec = _spec()
    reconciler = CrossCameraReconciler([spec])

    reconciler.ingest_row("cam1", _row(10, 1_000_000.0, global_ts_us=2_000_000.0))
    cross_rows = reconciler.ingest_row("cam2", _row(20, 1_000_050.0, global_ts_us=2_000_060.0))

    assert cross_rows[0]["master_global_ts_us"] == 2_000_000.0
    assert cross_rows[0]["slave_global_ts_us"] == 2_000_060.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_cross_camera_reconciler.py -k raw_global_timestamps -v`
Expected: FAIL with `KeyError: 'master_global_ts_us'`.

- [ ] **Step 3: Implement**

In `engine/cross_camera_reconciler.py`, modify `_build_cross_row`'s returned dict (currently lines 323-341) - add the two new keys right after the existing `slave_ts_us` line:

```python
        return {
            "pair_index": hw_sample.pair_index,
            "master_camera_id": spec.master_camera_id,
            "slave_camera_id": spec.slave_camera_id,
            "stream_identity": spec.stream_identity,
            "master_pair_index": master_row.get("pair_index"),
            "slave_pair_index": slave_row.get("pair_index"),
            "master_ts_us": master_hw_ts,  # RAW, unadjusted - for CSV/debugging transparency
            "slave_ts_us": slave_hw_ts,    # RAW, unadjusted
            "master_global_ts_us": master_global_ts,  # RAW, unadjusted
            "slave_global_ts_us": slave_global_ts,    # RAW, unadjusted
            "pairing_gap_us": hw_result.value,
            "pairing_gap_us_excluded": hw_result.excluded,
            "pairing_gap_us_exclude_reason": hw_result.exclude_reason,
            "global_ts_gap_us": global_result.value,
            "global_ts_gap_us_excluded": global_result.excluded,
            "global_ts_gap_us_exclude_reason": global_result.exclude_reason,
            "position_gap_ms": position_gap_ms,
            "position_gap_ms_excluded": position_gap_excluded,
            "position_gap_ms_exclude_reason": position_gap_exclude_reason,
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_cross_camera_reconciler.py -v`
Expected: PASS (every test in this file).

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python.exe -m pytest -v`
Expected: PASS (every test in the project).

- [ ] **Step 6: Commit**

```bash
git add engine/cross_camera_reconciler.py tests/engine/test_cross_camera_reconciler.py
git commit -m "feat: CrossCameraReconciler's cross-row also carries the raw global timestamps"
```

---

### Task 4: `MultiCameraLiveSessionPage` saves cross-camera debug images

**Files:**
- Modify: `gui/pages/multi_camera_live_session_page.py`
- Test: `tests/gui/pages/test_multi_camera_live_session_page.py`

**Interfaces:**
- Consumes: `SessionEngineThread.get_recent_frame_pair(pair_index)` (Task 1), `draw_cross_camera_debug_overlay(...)` (Task 2), cross-row dicts now carrying `master_global_ts_us`/`slave_global_ts_us` (Task 3), `MultiCameraSessionController`'s existing public `threads` property (unchanged, already exists).
- Produces: `MultiCameraLiveSessionPage._cross_debug_image_counts: dict` - `(slave_camera_id, stream_identity) -> {"periodic_count": int, "outlier_count": int}`, registered in `_build_slave_section`, reset in `_reset_cross_run_state`. No new public methods - the save logic is a new private method, `_maybe_save_cross_camera_debug_image`.

**Note on `_reset_cross_run_state`:** the design spec for this feature assumed stale debug-image files might need clearing at the start of each run (mirroring `_clear_periodic_snapshots`'s glob-and-delete pattern). That assumption doesn't hold here: `self._run_dir` is built fresh via `domain.run_output.create_run_dir` on every single `start_all_sessions()` call (a new timestamped folder every time, with automatic collision-numbering) - there is never anything stale to delete inside a folder that didn't exist a moment ago. Only the in-memory counters need resetting between repeated Start-All clicks in the same page visit (exactly like `_cross_running_stats` already needed its own reset fix in an earlier feature) - no file-clearing step is added.

- [ ] **Step 1: Write the failing tests**

In `tests/gui/pages/test_multi_camera_live_session_page.py`, extend `_FakeSessionEngineThread` (find its current exact definition, near the top of the file) to also fake the new `get_recent_frame_pair` method:

```python
class _FakeSessionEngineThread(QObject):
    """Same fake used by tests/engine/test_multi_camera_session.py - a real
    QObject exposing SessionEngineThread's exact signals, never a real
    QThread/camera."""
    frame_ready = Signal(str, object, int, object)
    row_ready = Signal(dict)
    stats_ready = Signal(dict)
    session_finished = Signal(list)
    error = Signal(str)
    finished = Signal()

    def __init__(self, **kwargs):
        super().__init__()
        self.kwargs = kwargs
        self.started = False
        self._recent_frames = {}  # pair_index -> (stream_a_image, stream_b_image)

    def start(self):
        self.started = True

    def request_stop(self):
        pass

    def set_recent_frame_pair(self, pair_index, stream_a_image, stream_b_image):
        """Test helper - mirrors the real SessionEngineThread's ring
        buffer, but without any eviction/threading (tests drive this
        directly with exactly the pair_indices they need)."""
        self._recent_frames[pair_index] = (stream_a_image, stream_b_image)

    def get_recent_frame_pair(self, pair_index):
        return self._recent_frames.get(pair_index)
```

Add this new import to the top of the test file, alongside the existing imports:

```python
import numpy as np
```

(Confirm it isn't already imported before adding - it likely already is, since `_camera_config` already uses `np.full`/`np.array`.)

Add these new tests anywhere in the file (e.g. right after `test_cross_running_stats_registered_per_slave_identity_and_metric`):

```python
def test_cross_debug_image_counts_registered_per_slave_identity(qapp, tmp_path):
    page, _ = _page_with_fake_threads()

    page.set_cameras(object(), _two_cameras(tmp_path))

    assert page._cross_debug_image_counts[("cam2", "infrared1")] == {"periodic_count": 0, "outlier_count": 0}
    assert page._cross_debug_image_counts[("cam2", "color")] == {"periodic_count": 0, "outlier_count": 0}


def test_outlier_cross_row_saves_a_debug_image_with_the_actual_matched_frames(qapp, tmp_path):
    page, fake_threads = _page_with_fake_threads()
    cameras = _two_cameras(tmp_path)
    # num_leds=2 (this fixture's default) means the only possible non-zero
    # Optical Sync gap is +-1.0ms (compute_position_gap(1, 0, 2) == 1,
    # * switch_time_ms 1.0) - a threshold of 0.5 guarantees that triggers.
    cameras[0]["config"]["position_gap_outlier_threshold_ms"] = 0.5
    page.set_cameras(object(), cameras)
    page.start_all_sessions()

    master_thread = fake_threads["SN1"]
    slave_thread = fake_threads["SN2"]
    master_thread.set_recent_frame_pair(1, np.full((4, 4), 10, dtype=np.uint8), np.full((4, 4, 3), 20, dtype=np.uint8))
    slave_thread.set_recent_frame_pair(1, np.full((4, 4), 30, dtype=np.uint8), np.full((4, 4, 3), 40, dtype=np.uint8))

    master_thread.row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_000.0, "stream_b_ts_us": 1_000_000.0,
        "stream_a_global_ts_us": 1_000_000.0, "stream_b_global_ts_us": 1_000_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
        "stream_a_last_led": 1, "position_gap_ms_excluded": False,
    })
    slave_thread.row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_010.0, "stream_b_ts_us": 1_000_010.0,
        "stream_a_global_ts_us": 1_000_010.0, "stream_b_global_ts_us": 1_000_010.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
        "stream_a_last_led": 0, "position_gap_ms_excluded": False,
    })

    saved = list(tmp_path.glob("**/cross_camera_optical_sync_outlier_*.png"))
    assert len(saved) == 1
    assert "slave1" in saved[0].name
    assert "infrared1" in saved[0].name


def test_periodic_cross_row_saves_a_debug_image_every_nth_pair_per_spec(qapp, tmp_path):
    page, fake_threads = _page_with_fake_threads()
    cameras = _two_cameras(tmp_path)
    cameras[0]["config"]["snapshot_every_n_pairs"] = 1  # every pair, deterministic
    cameras[0]["config"]["position_gap_outlier_threshold_ms"] = 999  # disable outlier triggering here
    page.set_cameras(object(), cameras)
    page.start_all_sessions()

    master_thread = fake_threads["SN1"]
    slave_thread = fake_threads["SN2"]
    master_thread.set_recent_frame_pair(1, np.zeros((4, 4), dtype=np.uint8), np.zeros((4, 4, 3), dtype=np.uint8))
    slave_thread.set_recent_frame_pair(1, np.zeros((4, 4), dtype=np.uint8), np.zeros((4, 4, 3), dtype=np.uint8))

    master_thread.row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_000.0, "stream_b_ts_us": 1_000_000.0,
        "stream_a_global_ts_us": 1_000_000.0, "stream_b_global_ts_us": 1_000_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    slave_thread.row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_010.0, "stream_b_ts_us": 1_000_010.0,
        "stream_a_global_ts_us": 1_000_010.0, "stream_b_global_ts_us": 1_000_010.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })

    # _camera_config's two cameras share BOTH "infrared1" and "color" -
    # each identity's own cross-row independently triggers the periodic
    # save, using the reconciler's own synthetic pair_index (1 and 2).
    saved = list(tmp_path.glob("**/cross_camera_periodic_*.png"))
    assert len(saved) == 2


def test_no_debug_image_saved_when_the_matched_frame_has_aged_out_of_the_buffer(qapp, tmp_path):
    page, fake_threads = _page_with_fake_threads()
    cameras = _two_cameras(tmp_path)
    cameras[0]["config"]["snapshot_every_n_pairs"] = 1
    page.set_cameras(object(), cameras)
    page.start_all_sessions()

    # Deliberately never call set_recent_frame_pair - simulates the image
    # having already aged out of (or never having reached) the ring buffer.
    fake_threads["SN1"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_000.0, "stream_b_ts_us": 1_000_000.0,
        "stream_a_global_ts_us": 1_000_000.0, "stream_b_global_ts_us": 1_000_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_010.0, "stream_b_ts_us": 1_000_010.0,
        "stream_a_global_ts_us": 1_000_010.0, "stream_b_global_ts_us": 1_000_010.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })

    saved = list(tmp_path.glob("**/cross_camera_periodic_*.png"))
    assert saved == []


def test_periodic_debug_images_stop_once_max_snapshots_reached_per_spec(qapp, tmp_path):
    page, fake_threads = _page_with_fake_threads()
    cameras = _two_cameras(tmp_path)
    cameras[0]["config"]["snapshot_every_n_pairs"] = 1
    cameras[0]["config"]["max_snapshots"] = 1
    page.set_cameras(object(), cameras)
    page.start_all_sessions()

    master_thread = fake_threads["SN1"]
    slave_thread = fake_threads["SN2"]
    for pair in range(1, 3):
        master_thread.set_recent_frame_pair(
            pair, np.zeros((4, 4), dtype=np.uint8), np.zeros((4, 4, 3), dtype=np.uint8)
        )
        slave_thread.set_recent_frame_pair(
            pair, np.zeros((4, 4), dtype=np.uint8), np.zeros((4, 4, 3), dtype=np.uint8)
        )
        master_thread.row_ready.emit({
            "pair_index": pair, "stream_a_ts_us": 1_000_000.0 + pair, "stream_b_ts_us": 1_000_000.0 + pair,
            "stream_a_global_ts_us": 1_000_000.0 + pair, "stream_b_global_ts_us": 1_000_000.0 + pair,
            "stream_a_frame_drop": False, "stream_b_frame_drop": False,
        })
        slave_thread.row_ready.emit({
            "pair_index": pair, "stream_a_ts_us": 1_000_010.0 + pair, "stream_b_ts_us": 1_000_010.0 + pair,
            "stream_a_global_ts_us": 1_000_010.0 + pair, "stream_b_global_ts_us": 1_000_010.0 + pair,
            "stream_a_frame_drop": False, "stream_b_frame_drop": False,
        })

    # max_snapshots=1 caps the infrared1 spec's own periodic count
    # independently of the color spec's own count - across 2 rounds
    # (2 cross-rows each), exactly 1 infrared1 image is ever saved.
    saved = list(tmp_path.glob("**/cross_camera_periodic_slave1_infrared1_*.png"))
    assert len(saved) == 1


def test_reset_cross_run_state_resets_debug_image_counters_on_a_second_run(qapp, tmp_path):
    page, fake_threads = _page_with_fake_threads()
    cameras = _two_cameras(tmp_path)
    page.set_cameras(object(), cameras)
    page.start_all_sessions()

    key = ("cam2", "infrared1")
    page._cross_debug_image_counts[key]["periodic_count"] = 5
    page._cross_debug_image_counts[key]["outlier_count"] = 3

    fake_threads["SN1"].session_finished.emit([])
    fake_threads["SN1"].finished.emit()
    fake_threads["SN2"].session_finished.emit([])
    fake_threads["SN2"].finished.emit()

    page.start_all_sessions()

    assert page._cross_debug_image_counts[key] == {"periodic_count": 0, "outlier_count": 0}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_multi_camera_live_session_page.py -k "debug_image" -v`
Expected: FAIL - `AttributeError: 'MultiCameraLiveSessionPage' object has no attribute '_cross_debug_image_counts'` for the registration test; the outlier/periodic/aged-out/cap/reset tests either fail the same way, or (once that attribute exists) fail because no debug images are actually saved yet (`assert len(saved) == 1` failing against `0`).

- [ ] **Step 3: Implement**

In `gui/pages/multi_camera_live_session_page.py`, add these imports to the existing import block (currently lines 60-78) - `cv2` is new, and two new names come from already-imported-from modules:

```python
import os

import cv2
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSpinBox, QDoubleSpinBox, QTabWidget,
)

from gui.widgets.camera_live_session_panel import CameraLiveSessionPanel
from gui.widgets.live_plot import LivePlot
from gui.widgets.stats_panel import StatsPanel
from engine.multi_camera_session import CameraSessionSpec, MultiCameraSessionController
from engine.cross_camera_reconciler import build_cross_camera_pair_specs
from engine.metrics import PairingGapMetric, PositionGapMetric, is_position_gap_debug_outlier
from engine.test_session import TestSession, TestSessionConfig
from engine.streams import stream_slug
from domain.run_output import create_run_dir, create_camera_subdir
from domain.csv_export import export_cross_camera_csv
from domain.plot_export import export_cross_camera_plot
from domain.plot_theme import CROSS_CAMERA_COLORS
from domain.running_stats import RunningStats
from domain.realsense_utils import draw_cross_camera_debug_overlay, combine_side_by_side
```

Add this new module-level helper function right after `_stream_identities` (currently lines 97-98):

```python
def _row_role_for_identity(config, identity):
    """Which of a camera's own two picks (stream_a/stream_b) a given
    shared stream identity actually is - mirrors the exact same lookup
    engine.cross_camera_reconciler.build_cross_camera_pair_specs already
    does when it computes master_row_role/slave_row_role, needed again
    here since the cross-row dict itself doesn't carry those roles."""
    stream_identities = _stream_identities(config)
    return next(role for role, ident in stream_identities.items() if ident == identity)
```

In `MultiCameraLiveSessionPage.__init__`, add this right after `self._cross_running_stats = {}` (currently line 226):

```python
        # (slave_camera_id, stream_identity) -> {"periodic_count": int,
        # "outlier_count": int} - independent per-spec caps for
        # _maybe_save_cross_camera_debug_image, mirroring
        # self._cross_running_stats' own per-spec independence.
        self._cross_debug_image_counts = {}
```

In `_build_slave_section`, add this inside the existing `for index, spec in enumerate(specs):` loop (currently lines 398-407), right after the `self._cross_running_stats[(slave_camera_id, identity, "position_gap_ms")] = RunningStats()` line:

```python
            self._cross_debug_image_counts[(slave_camera_id, identity)] = {
                "periodic_count": 0, "outlier_count": 0,
            }
```

In `_reset_cross_run_state`, add this right after the existing `for key in self._cross_running_stats:` loop (currently lines 444-445):

```python
        for key in self._cross_debug_image_counts:
            self._cross_debug_image_counts[key] = {"periodic_count": 0, "outlier_count": 0}
```

Modify `_on_cross_pair_ready` (currently lines 621-642) - add the new call at the end of the existing method body:

```python
    def _on_cross_pair_ready(self, cross_row):
        # O(1) bookkeeping only - no add_point here. Fires unthrottled, once
        # per cross-camera match; plotting on this cadence caused a real GUI
        # freeze for the analogous intra-camera case (see CLAUDE.md's
        # row_ready/stats_ready cadence split). Both graphs' add_point calls,
        # and the actual stats-panel pushes, happen only in
        # _on_cross_stats_ready, below - RunningStats.update() here is the
        # one exception, matching CameraLiveSessionPanel.on_row_ready's own
        # unthrottled accumulation (cheap, no plotting).
        self._cross_rows.append(cross_row)

        key = (cross_row["slave_camera_id"], cross_row["stream_identity"])
        pairing_stats = self._cross_running_stats.get(key + ("pairing_gap_us",))
        if pairing_stats is not None and not cross_row.get("pairing_gap_us_excluded"):
            pairing_stats.update(cross_row["pairing_gap_us"])
        global_ts_stats = self._cross_running_stats.get(key + ("global_ts_gap_us",))
        if global_ts_stats is not None and not cross_row.get("global_ts_gap_us_excluded"):
            global_ts_stats.update(cross_row["global_ts_gap_us"])
        position_stats = self._cross_running_stats.get(key + ("position_gap_ms",))
        if (position_stats is not None and cross_row.get("position_gap_ms") is not None
                and not cross_row.get("position_gap_ms_excluded")):
            position_stats.update(cross_row["position_gap_ms"])

        self._maybe_save_cross_camera_debug_image(cross_row)
```

Add this new method right after `_on_cross_pair_ready`:

```python
    def _maybe_save_cross_camera_debug_image(self, cross_row):
        """Saves a side-by-side debug image of the two ACTUAL matched
        frames for a cross-camera pair - outlier-triggered (Optical Sync
        only, mirroring engine.session_engine.py's own intra-camera
        _maybe_save_position_gap_outlier) or periodic (every Nth
        cross-camera pair, per (slave, identity) spec independently) -
        both reusing the MASTER camera's own already-configured
        thresholds/cadence (same "master's config wins" precedent this
        feature already uses for num_leds/switch_time_ms). Runs on every
        unthrottled cross-camera match (this method's own cheap checks),
        but the expensive part (image lookup, drawing, disk write) only
        actually happens on a genuine trigger - the same shape
        _maybe_save_position_gap_outlier already uses on its own
        unthrottled per-pair callback, not a new risk to the documented
        row_ready/stats_ready cadence discipline (which is specifically
        about never calling GUI-widget updates like add_point here)."""
        if self._controller is None or self._run_dir is None:
            return
        key = (cross_row["slave_camera_id"], cross_row["stream_identity"])
        counts = self._cross_debug_image_counts.get(key)
        if counts is None:
            return

        master_config = next((c["config"] for c in self._cameras if c["is_master"]), None)
        if master_config is None:
            return

        is_outlier = (
            is_position_gap_debug_outlier(cross_row, master_config["position_gap_outlier_threshold_ms"])
            and counts["outlier_count"] < master_config["position_gap_outlier_max_snapshots"]
        )
        every_n = master_config["snapshot_every_n_pairs"]
        is_periodic = (
            every_n > 0 and cross_row["pair_index"] % every_n == 0
            and counts["periodic_count"] < master_config["max_snapshots"]
        )
        if not is_outlier and not is_periodic:
            return

        threads = self._controller.threads
        master_thread = threads.get(cross_row["master_camera_id"])
        slave_thread = threads.get(cross_row["slave_camera_id"])
        if master_thread is None or slave_thread is None:
            return
        master_frames = master_thread.get_recent_frame_pair(cross_row["master_pair_index"])
        slave_frames = slave_thread.get_recent_frame_pair(cross_row["slave_pair_index"])
        if master_frames is None or slave_frames is None:
            return

        identity = cross_row["stream_identity"]
        slave_config = next(c["config"] for c in self._cameras if c["camera_id"] == cross_row["slave_camera_id"])
        master_role = _row_role_for_identity(master_config, identity)
        slave_role = _row_role_for_identity(slave_config, identity)
        master_image = master_frames[0] if master_role == "stream_a" else master_frames[1]
        slave_image = slave_frames[0] if slave_role == "stream_a" else slave_frames[1]

        overlay_image = draw_cross_camera_debug_overlay(
            master_image,
            cross_pair_index=cross_row["pair_index"],
            master_pair_index=cross_row["master_pair_index"], slave_pair_index=cross_row["slave_pair_index"],
            master_ts_us=cross_row["master_ts_us"], slave_ts_us=cross_row["slave_ts_us"],
            master_global_ts_us=cross_row["master_global_ts_us"], slave_global_ts_us=cross_row["slave_global_ts_us"],
            pairing_gap_us=cross_row["pairing_gap_us"], global_ts_gap_us=cross_row["global_ts_gap_us"],
            position_gap_ms=cross_row["position_gap_ms"],
        )
        combined = combine_side_by_side(overlay_image, slave_image)
        roles = _camera_roles(self._cameras)
        slave_slug = roles[cross_row["slave_camera_id"]]["slug"]

        if is_outlier:
            path = os.path.join(
                self._run_dir,
                "cross_camera_optical_sync_outlier_{}_{}_pair{:05d}.png".format(
                    slave_slug, identity, cross_row["pair_index"]
                ),
            )
            cv2.imwrite(path, combined)
            counts["outlier_count"] += 1
        if is_periodic:
            path = os.path.join(
                self._run_dir,
                "cross_camera_periodic_{}_{}_pair{:05d}.png".format(
                    slave_slug, identity, cross_row["pair_index"]
                ),
            )
            cv2.imwrite(path, combined)
            counts["periodic_count"] += 1
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_multi_camera_live_session_page.py -v`
Expected: PASS (every test in this file).

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python.exe -m pytest -v`
Expected: PASS (every test in the project).

- [ ] **Step 6: Commit**

```bash
git add gui/pages/multi_camera_live_session_page.py tests/gui/pages/test_multi_camera_live_session_page.py
git commit -m "feat: save cross-camera outlier/periodic debug images of the actual matched frames"
```

---

## Self-Review

**Spec coverage:**
- Section 1 (per-camera unthrottled frame ring buffer) - Task 1.
- Section 2 (outlier/periodic decision + save, reusing master's config, per-spec counters, output naming) - Task 4.
- Section 3 (new overlay function, "everything available" content) - Task 2, consumed by Task 4.
- Section 4 (no live video panels; `CrossCameraReconciler` stays pure; no new settings; existing drawing helpers reused unmodified; intra-camera untouched) - confirmed by scope: no task modifies `engine/cross_camera_reconciler.py`'s matching logic (only adds 2 return-dict fields in Task 3), no task adds video panels, no task touches `settings.yaml`, no task modifies `draw_led_state_overlay`/`combine_side_by_side`'s own bodies (Task 4 only calls the existing `combine_side_by_side`), no task modifies `engine/session_engine.py`'s existing intra-camera outlier/periodic mechanisms (only adds alongside them in Task 1).
- Critical files' note about `_build_cross_row` needing the two extra raw global-ts fields - Task 3, consumed by Task 4's overlay call.
- Testing section's coverage list - matches Tasks 1-4's own test steps: ring buffer append/evict/lookup (Task 1), overlay text/no-mutation/None-handling (Task 2), raw global ts fields present (Task 3), outlier/periodic/aged-out-skip/caps/reset (Task 4).

**Placeholder scan:** No TBD/TODO/"add appropriate"/"similar to Task N" phrases - every step has complete, real code, including the design-vs-plan correction note about `_reset_cross_run_state` (explained, not left vague).

**Type consistency:** `get_recent_frame_pair`/`_record_recent_frame` are the exact same method names in Task 1's implementation, Task 1's own tests, and Task 4's real usage (`master_thread.get_recent_frame_pair(...)`) and its fake (`_FakeSessionEngineThread.get_recent_frame_pair`/`set_recent_frame_pair`). `draw_cross_camera_debug_overlay`'s exact 11-parameter signature (Task 2) matches its call site in Task 4's `_maybe_save_cross_camera_debug_image` exactly, parameter-for-parameter. `master_global_ts_us`/`slave_global_ts_us` are the same two new cross-row keys in Task 3's implementation/test and Task 4's consumption. `_cross_debug_image_counts`'s `{"periodic_count": int, "outlier_count": int}` shape is consistent across `__init__`'s registration comment, `_build_slave_section`'s initialization, `_reset_cross_run_state`'s reset, and `_maybe_save_cross_camera_debug_image`'s read/increment.
