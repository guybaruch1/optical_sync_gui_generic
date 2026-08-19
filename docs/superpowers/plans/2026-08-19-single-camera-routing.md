# Route a 1-Camera Run to LiveSessionPage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When exactly one camera is configured, clicking Start on the Camera Hub routes to the real single-camera `LiveSessionPage` instead of `MultiCameraLiveSessionPage`.

**Architecture:** `MainWindow` constructs `LiveSessionPage` (currently not constructed at all) and adds an early-return branch to `_on_start_multi_camera_session_requested`: with exactly 1 configured camera, call `LiveSessionPage.set_context()` directly using that camera's own already-complete `config` dict (whose keys already match `set_context()`'s parameters exactly) and switch to it, skipping the genlock-resolution/slave-color-conflict machinery entirely. The existing 2+-camera path is unchanged below this branch.

**Tech Stack:** Python 3.10+/3.13, PySide6, pytest (`QT_QPA_PLATFORM=offscreen`, shared `qapp` fixture).

## Global Constraints

- `CameraHubPage` is untouched - it still appears for every run regardless of camera count, and the operator can still add a second camera there before Start.
- `MultiCameraLiveSessionPage`'s own behavior (2+ cameras) is unchanged.
- `LiveSessionPage`'s own internals are unchanged - only reached again, not modified.
- The 1-camera branch must never call `resolve_inter_cam_sync_value` or the slave-color-resolution conflict check - both are meaningless without a second camera.

---

### Task 1: `MainWindow` routes a 1-camera Start to `LiveSessionPage`

**Files:**
- Modify: `gui/main_window.py:34-40` (imports), `:107-163` (`__init__`'s page construction/stack registration), `:526-562` (`_on_start_multi_camera_session_requested`)
- Test: `tests/gui/test_main_window.py`

**Interfaces:**
- Consumes: `LiveSessionPage.set_context(ctx, device_serial, pick_a, pick_b, camera_controls, switch_time_ms, scan_direction, stream_a_threshold, stream_b_threshold, stream_a_xy, stream_b_xy, num_leds, neighborhood_size, frame_drop_threshold_factor, warmup_pairs_to_skip, pairing_gap_outlier_threshold_us, position_gap_outlier_threshold_ms, position_gap_outlier_max_snapshots, output_root, kept_csv_filename, dropped_csv_filename, snapshot_every_n_pairs, max_snapshots, stream_a_roi, stream_b_roi, camera_name, stream_a_label, stream_b_label, dual_panel_config=None, enable_depth_for_ir_sync=True, hardware_reset_before_start=False, hardware_reset_settle_s=8.0)` (existing, unchanged) - `self._cameras[camera_id]["config"]`'s keys already match every one of these parameter names exactly (confirmed in `_on_tuning_done`'s own existing code and comment).
- Produces: `self.live_session_page: LiveSessionPage` - a new `MainWindow` attribute, added to `self.stack`. No new public methods.

- [ ] **Step 1: Write the failing tests**

In `tests/gui/test_main_window.py`, replace `test_start_multi_camera_session_requested_switches_to_the_new_page_with_cameras` (currently lines 627-637) - its whole premise (1 camera routes to the multi-camera page) is exactly what this task changes:

```python
def test_start_multi_camera_session_requested_switches_to_live_session_page_with_exactly_one_camera(qapp, monkeypatch, tmp_path):
    window = _window_after_config_chosen(qapp, monkeypatch, tmp_path)
    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        window._on_calibration_done()
        window._on_tuning_done()

    window._on_start_multi_camera_session_requested()

    assert window.stack.currentWidget() is window.live_session_page
    assert window.live_session_page._context["device_serial"] == "SN123"


def test_start_multi_camera_session_requested_skips_genlock_resolution_for_one_camera(qapp, monkeypatch, tmp_path):
    window = _window_after_config_chosen(qapp, monkeypatch, tmp_path)
    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        window._on_calibration_done()
        window._on_tuning_done()

    calls = []
    monkeypatch.setattr(
        main_window_module, "resolve_inter_cam_sync_value",
        lambda *a, **k: calls.append((a, k)) or 1,
    )

    window._on_start_multi_camera_session_requested()

    assert calls == []
```

Then replace `test_start_multi_camera_session_requested_leaves_inter_cam_sync_value_none_for_unconfigured_camera_model` (currently lines 693-706) - it drives only 1 camera through `_window_after_config_chosen`, but genlock resolution (the exact thing this test exercises) is skipped entirely for 1 camera after this task lands, so it needs a second camera to keep testing something meaningful:

```python
def test_start_multi_camera_session_requested_leaves_inter_cam_sync_value_none_for_unconfigured_camera_model(qapp, monkeypatch, tmp_path):
    # No camera.inter_cam_sync entry at all for this device name - genlock is
    # skipped entirely rather than guessing a possibly-wrong raw value. Needs
    # 2 cameras: with only 1 configured, MainWindow now routes to
    # LiveSessionPage instead (see test_start_multi_camera_session_requested_
    # switches_to_live_session_page_with_exactly_one_camera), which never
    # attempts genlock resolution for a solo camera at all.
    window = _window_after_config_chosen(qapp, monkeypatch, tmp_path)
    first_camera_id = window._editing_camera_id
    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        window._on_calibration_done()
        window._on_tuning_done()

    window._on_add_camera_requested()
    window._on_device_chosen("SN456", "Intel RealSense D455")
    window._on_config_chosen((IR1, COLOR0, {
        "emitter_enabled": False, "auto_exposure": True, "exposure_a": None, "exposure_b": None,
    }))
    window.gui_state.stream_a_roi = [0, 0, 50, 50]
    window.gui_state.stream_b_roi = [0, 0, 50, 50]
    window.calibration_page.last_calibration_result = dict(
        image_a_on=np.full((50, 50), 50, dtype=np.uint8), image_a_off=np.full((50, 50), 50, dtype=np.uint8),
        image_b_on=np.full((50, 50), 50, dtype=np.uint8), image_b_off=np.full((50, 50), 50, dtype=np.uint8),
        stream_a_otsu_threshold=127, stream_b_otsu_threshold=127,
        min_blob_area=5, row_gap_px=15, neighborhood_size=5,
    )
    with patch("gui.pages.threshold_tuning_page.ThresholdPreviewThread", _FakePreviewThread):
        window._on_calibration_done()
        window._on_tuning_done()

    window._on_start_multi_camera_session_requested()

    page = window.multi_camera_live_session_page
    configs_by_id = {c["camera_id"]: c["config"] for c in page._cameras}
    assert configs_by_id[first_camera_id]["inter_cam_sync_value"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/test_main_window.py -k "exactly_one_camera or skips_genlock_resolution_for_one_camera or leaves_inter_cam_sync_value_none" -v`
Expected: two of the three FAIL, one already PASSES:
- `test_start_multi_camera_session_requested_switches_to_live_session_page_with_exactly_one_camera` — FAILS with `AttributeError: 'MainWindow' object has no attribute 'live_session_page'` (not constructed yet).
- `test_start_multi_camera_session_requested_skips_genlock_resolution_for_one_camera` — FAILS with `assert [(...)] == []` (today's code always calls `resolve_inter_cam_sync_value`, even for 1 camera).
- The rewritten `leaves_inter_cam_sync_value_none_for_unconfigured_camera_model` — already PASSES against current code: it now drives 2 cameras through the existing, unmodified 2-camera path, which already resolves `inter_cam_sync_value` to `None` for a model with no settings entry. This one is a consistency-preserving rewrite (its 1-camera premise is invalidated by this task, not a new behavior needing new code to pass) rather than a red test - confirm it stays green through Step 4, don't expect it to flip.

- [ ] **Step 3: Implement**

Add the import (currently `gui/main_window.py:40`, right after the `multi_camera_live_session_page` import):

```python
from gui.pages.multi_camera_live_session_page import MultiCameraLiveSessionPage
from gui.pages.live_session_page import LiveSessionPage
```

Add the page construction (currently `gui/main_window.py:113`, right after `self.multi_camera_live_session_page = MultiCameraLiveSessionPage()`):

```python
        self.multi_camera_live_session_page = MultiCameraLiveSessionPage()
        self.live_session_page = LiveSessionPage()
```

Add it to the stack registration tuple (currently `gui/main_window.py:160-162`):

```python
        for page in (self.device_page, self.stream_config_page, self.roi_page,
                     self.calibration_page, self.threshold_tuning_page, self.camera_hub_page,
                     self.multi_camera_live_session_page, self.live_session_page):
            self.stack.addWidget(page)
```

Add the branch to `_on_start_multi_camera_session_requested` (currently `gui/main_window.py:526-562`) - insert right after the existing `if not self._cameras: return` guard, before the genlock-resolution block:

```python
    def _on_start_multi_camera_session_requested(self):
        # Not reachable via the real hub with zero cameras (Start is
        # disabled - see CameraHubPage._can_start), but guard defensively
        # rather than switch to an empty page if something else calls this.
        if not self._cameras:
            return
        if len(self._cameras) == 1:
            # A solo camera has no genlock partner and no cross-camera
            # concept at all - route to the lighter, purpose-built
            # single-camera page instead of the multi-camera one, skipping
            # genlock/slave-color-resolution resolution entirely (it's
            # meaningless without a second camera). The per-camera config
            # dict's own keys already match set_context()'s parameters
            # exactly - see _on_tuning_done's own comment.
            only_camera = next(iter(self._cameras.values()))
            self.live_session_page.set_context(ctx=self.ctx, **only_camera["config"])
            self.stack.setCurrentWidget(self.live_session_page)
            return
        # Genlock role resolution happens fresh HERE, at Start-time, not
        # earlier - the master assignment can change at any point in the hub
        # (Set as Master, remove-the-master promotion) up until the operator
        # actually starts the run, so re-resolving off self._master_camera_id
        # every Start is what keeps this correct rather than stale.
        inter_cam_sync_settings = self.settings["camera"].get("inter_cam_sync", {})
        cameras = [
            {"camera_id": camera_id, "label": camera["label"],
             "is_master": (camera_id == self._master_camera_id),
             "config": {
                 **camera["config"],
                 "inter_cam_sync_value": resolve_inter_cam_sync_value(
                     inter_cam_sync_settings, camera["label"],
                     is_master=(camera_id == self._master_camera_id),
                 ),
             }}
            for camera_id, camera in self._cameras.items()
        ]
        conflicts = _slave_genlock_color_resolution_conflicts(cameras, inter_cam_sync_settings)
        if conflicts:
            QMessageBox.critical(
                self, "Slave camera color resolution too high for genlock",
                "The following camera(s) can't safely run their configured color stream "
                "while acting as a genlock slave:\n\n{}\n\nLower that stream's resolution "
                "in Stream Config, or make this camera the master instead.".format(
                    "\n".join(conflicts)
                ),
            )
            return
        self.multi_camera_live_session_page.set_cameras(self.ctx, cameras)
        self.stack.setCurrentWidget(self.multi_camera_live_session_page)
```

Update the module docstring's stale claim (currently `gui/main_window.py:22-27`):

```python
CameraHubPage's "Start Multi-Camera Live Session" switches to
gui/pages/multi_camera_live_session_page.py's MultiCameraLiveSessionPage
when 2+ cameras are configured. With exactly 1 configured camera, it
instead routes to the original single-camera gui/pages/live_session_page.py's
LiveSessionPage directly - a solo camera has no genlock partner and no
cross-camera concept, so the lighter, purpose-built single-camera page is
used instead of the multi-camera one. See
_on_start_multi_camera_session_requested for the branch.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/test_main_window.py -v`
Expected: PASS (all tests in the file).

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python.exe -m pytest -v`
Expected: PASS (every test in the project).

- [ ] **Step 6: Commit**

```bash
git add gui/main_window.py tests/gui/test_main_window.py
git commit -m "feat: route a 1-camera run to LiveSessionPage instead of the multi-camera page"
```

---

## Self-Review

**Spec coverage:**
- Section 1 (`MainWindow` constructs `LiveSessionPage` again) - Task 1's import + construction + stack-registration steps.
- Section 2 (`_on_start_multi_camera_session_requested` branches on camera count, before genlock/conflict work) - Task 1's method replacement, plus the dedicated test confirming `resolve_inter_cam_sync_value` is never called for 1 camera.
- Section 3 (what doesn't change: `CameraHubPage`, `MultiCameraLiveSessionPage`, `LiveSessionPage`'s own internals, post-run navigation) - confirmed by scope: Task 1 touches only `gui/main_window.py` and its test file; no other page file is modified.

**Placeholder scan:** No TBD/TODO/"add appropriate"/"similar to Task N" phrases - every step has real, complete code.

**Type consistency:** `self.live_session_page` is the same attribute name used consistently in the construction step, the stack-registration step, the branch's own code, and both new/updated tests. `only_camera["config"]` matches the exact dict shape `_on_tuning_done` already builds (confirmed against that method's own current code before writing this plan, not assumed).
