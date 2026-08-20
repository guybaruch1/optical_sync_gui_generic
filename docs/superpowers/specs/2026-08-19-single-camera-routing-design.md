# Route a 1-Camera Run to LiveSessionPage — Design

## Context

`gui/main_window.py`'s current module docstring documents a deliberate,
pre-existing design decision from the original multi-camera feature: "a
Camera Hub page in front of every run (even a single camera... see
docs/superpowers's multi-camera design doc's 'Design detail' section 4)"
and "the original single-camera LiveSessionPage is untouched and no longer
constructed here at all." Concretely, `MainWindow` only constructs
`CameraHubPage` and `MultiCameraLiveSessionPage` - `LiveSessionPage` is
never instantiated, so it is unreachable from the running app regardless of
how many cameras are configured.

The operator's own experience of this: configuring exactly one camera and
clicking Start still lands on `MultiCameraLiveSessionPage` - a tab widget
with a "Cross-Camera Sync" tab (showing a placeholder for <2 cameras) and
the sole camera's own tab tagged "[MASTER]", plus toolbar wording ("Start
All"/"Stop All") and an output-folder layout (a shared run folder with a
per-camera subfolder underneath) that all exist to coordinate multiple
cameras and have nothing to do when there is only one.

`LiveSessionPage` itself is not dead weight - it is still fully
constructed, still tested, and was actively touched by this session's own
prior work (the LED Switch Time Confirm-gate fix). It is a genuinely
different, lighter single-camera experience: no tab wrapper, no
cross-camera concept at all, a flat `output/live_session_<timestamp>/`
folder, "Start"/"Stop" wording, and a single `SessionEngineThread` driven
directly rather than through `MultiCameraSessionController`'s
multi-camera orchestration (genlock role-assignment sequencing, camera-start
staggering, per-camera hardware-reset ordering) - none of which has
anything to meaningfully do for a solo camera.

This spec reverses that one specific piece of the original multi-camera
design decision: for a 1-camera run specifically, route to the real
`LiveSessionPage` instead of `MultiCameraLiveSessionPage`. The Camera Hub
itself is untouched - it still appears for every run, still lets the
operator add/remove cameras before starting; only the destination of the
Hub's own "Start" action changes, based on how many cameras are configured
at the moment Start is actually clicked.

## 1. `MainWindow` constructs `LiveSessionPage` again

`gui/main_window.py`'s `__init__` adds `self.live_session_page =
LiveSessionPage()` alongside the existing `self.camera_hub_page`/
`self.multi_camera_live_session_page` construction, and adds it to the
`QStackedWidget` the same way every other page already is.

## 2. `_on_start_multi_camera_session_requested` branches on camera count

A new check at the top of the method, before any genlock-resolution or
slave-color-resolution-conflict work runs (that machinery is meaningless
without a second camera to sync against):

```python
if len(self._cameras) == 1:
    only_camera = next(iter(self._cameras.values()))
    self.live_session_page.set_context(ctx=self.ctx, **only_camera["config"])
    self.stack.setCurrentWidget(self.live_session_page)
    return
```

This works with no field-by-field translation because `_on_tuning_done`'s
per-camera `config` dict already carries exactly the same keys
`LiveSessionPage.set_context()` takes as parameters - confirmed directly
in the existing code, whose own comment already states this: "same set of
values `LiveSessionPage.set_context()` used to receive directly; now
stored per-camera instead." The one key present in the multi-camera path's
own locally-built `cameras` list but absent from the raw per-camera
`config` - `inter_cam_sync_value`, added only in the existing genlock-
resolution loop below this new branch - is never added at all here, since
this branch returns before that loop runs; `set_context()` has no
parameter for it and never needs one.

The existing 2+-camera path (the genlock resolution loop, the conflict
check, `multi_camera_live_session_page.set_cameras(...)`) is entirely
unchanged, sitting below this new early-return branch.

## 3. What doesn't change

- **`CameraHubPage`** - untouched. It still appears after every camera's
  Threshold Tuning step completes, for every run regardless of camera
  count; the operator can still add a second camera there before starting,
  in which case Start behaves exactly as it does today. Its "Start
  Multi-Camera Live Session" button text and its
  `start_multi_camera_session_requested` signal name are unchanged - this
  is a cosmetic mismatch with the 1-camera case (the button says "Multi-
  Camera" even when it's about to launch a single-camera page) but
  renaming a page's public signal is out of scope for what this fix needs
  to accomplish.
- **`MultiCameraLiveSessionPage`** - untouched. The 2+-camera experience is
  exactly as it is today; nothing about its own tab-building, cross-camera
  section, or role-labeling logic is touched by this spec.
- **`LiveSessionPage`** itself - untouched beyond being constructed and
  reached again. No changes to its own behavior; it continues to work
  exactly as it already does (including this session's own recent LED
  Switch Time Confirm-gate fix, which becomes user-reachable again as a
  direct consequence of this spec, with no code change of its own needed).
- **Post-run navigation** - neither page currently navigates back to the
  Camera Hub when a session finishes (confirmed: neither
  `LiveSessionPage._on_engine_thread_finished` nor
  `MultiCameraLiveSessionPage._on_all_sessions_finished` does any
  `stack.setCurrentWidget` call) - this is pre-existing, unrelated
  behavior, unchanged either way.

## Critical files

- `gui/main_window.py` - constructs `LiveSessionPage`; adds the camera-count
  branch to `_on_start_multi_camera_session_requested`.
- No changes: `gui/pages/live_session_page.py`,
  `gui/pages/multi_camera_live_session_page.py`,
  `gui/pages/camera_hub_page.py`.

## Testing

- `tests/gui/test_main_window.py`: configuring exactly 1 camera and
  triggering `start_multi_camera_session_requested` lands on
  `live_session_page`, with its context correctly populated from that
  camera's own `config` dict (spot-check a few fields, e.g.
  `device_serial`, `pick_a`, `switch_time_ms`); configuring 2 cameras and
  triggering the same signal still lands on `multi_camera_live_session_page`
  exactly as today (a regression guard that this spec's new branch doesn't
  accidentally divert the existing multi-camera path); the 1-camera branch
  never calls `resolve_inter_cam_sync_value`/the slave-color-resolution
  conflict check (e.g. via a mock/spy, or by constructing a scenario that
  would fail that check if reached, and confirming it does not raise for
  the 1-camera path).
