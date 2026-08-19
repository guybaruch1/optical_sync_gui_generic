# LED Switch Time Confirm Gate + Multi-Camera Per-Test Control — Design

## Context

`LiveSessionPage` (single-camera live test) has a "LED Switch Time (ms)"
toolbar spinbox plus a "Confirm" button. Today, Confirm does nothing
functional: `start_session()` reads `self.switch_time_spinbox.value()`
directly, so a run always uses whatever the box currently shows, whether
or not Confirm was ever clicked. Confirm only toggles its own
enabled/disabled visual state (`_last_confirmed_switch_time_ms` is tracked
but never actually read by `start_session()`). This was a deliberate,
documented choice when it was built — pure UI parity with
`ThresholdTuningPage`'s own Confirm button, which genuinely does apply a
new speed live to an already-running LED panel, a mechanism this page has
no equivalent need for since nothing is running yet at Start time.

The operator wants Confirm to actually mean something here: a run should
use the last value the operator explicitly confirmed, not an edit still
sitting unconfirmed in the box. This is a real behavior change to existing
code, not just new functionality.

Separately, `gui/pages/multi_camera_live_session_page.py` has no switch-time
control at all today - `start_all_sessions()` silently reads each
camera's own individually-tuned `config["switch_time_ms"]` (set during that
camera's own Threshold Tuning step) and uses it for that camera's metric and
its own real LED-panel hardware call. Switch time is physically a property
of the LED panel, not the camera - in the normal shared-single-panel setup,
every camera is looking at one panel stepping at one real rate, so letting
each camera carry its own independently-tuned number risks the app
representing one physical fact as several silently-possibly-different
numbers. This spec adds an explicit, single, per-test switch-time control to
the multi-camera page, replacing every configured camera's own tuned value
for that run.

## 1. `LiveSessionPage`: Confirm becomes a real gate

`_on_confirm_switch_time_clicked`'s existing bookkeeping
(`self._last_confirmed_switch_time_ms = self.switch_time_spinbox.value()`)
is unchanged. What changes:

- `start_session()` reads `self._last_confirmed_switch_time_ms` instead of
  `self.switch_time_spinbox.value()` - a run always uses the last
  explicitly confirmed number.
- `_update_confirm_switch_time_button_state()` gains a second
  responsibility: alongside toggling `confirm_switch_time_button`'s enabled
  state, it also disables `start_button` whenever there is a pending,
  unconfirmed edit (`switch_time_spinbox.value() != _last_confirmed_switch_time_ms`).
  `start_button`'s enabled state becomes the AND of two conditions: no
  session currently running, AND no pending unconfirmed switch-time edit.
- Any place that currently re-enables `start_button` unconditionally when a
  run finishes (`_on_engine_thread_finished`) must route through this same
  combined check instead of a bare `setEnabled(True)`, so finishing a run
  doesn't silently re-enable Start out from under a still-unconfirmed edit.
- Locking during a run is unchanged: `start_session()` already disables both
  `switch_time_spinbox` and `confirm_switch_time_button`;
  `_on_engine_thread_finished` already re-enables them. Nothing here alters
  that - a session in progress leaves both fully non-interactive exactly as
  today.

`ThresholdTuningPage`'s own Confirm button (the one that already does a
real, live, mid-run hardware apply) is untouched - different page,
different mechanism, unrelated to this change.

## 2. Multi-camera page: new per-test LED Switch Time control

A new toolbar control on `gui/pages/multi_camera_live_session_page.py`,
placed between Duration and Start All: a `switch_time_spinbox` +
`confirm_switch_time_button` pair, built with the gated semantics from
Section 1 from the start (there is no prior behavior to migrate away from
here, since this control doesn't exist yet):

- **Prefill:** a fixed `1.0` ms default when the page loads - not derived
  from any configured camera's own tuned value. `self._last_confirmed_switch_time_ms`
  must be initialized to this same `1.0` at construction (not `None` or
  unset), matching how `LiveSessionPage.set_context()` sets the confirmed
  baseline together with the spinbox's own prefill - otherwise Start All
  would appear disabled the instant the page opens, before the operator has
  touched anything.
- **Confirm gate:** identical mechanics to Section 1.
  `self._last_confirmed_switch_time_ms` tracked the same way;
  `start_button` ("Start All") disabled whenever there is a pending
  unconfirmed edit, re-enabled only once confirmed (and once no session is
  running).
- **What it replaces:** `start_all_sessions()` currently reads each
  camera's own `config["switch_time_ms"]` independently, three times per
  camera - building that camera's `PositionGapMetric(switch_time_ms=...)`,
  its `thread_kwargs["switch_time_ms"]` (the real per-camera LED-panel
  hardware call inside `SessionEngineThread`), and its
  `CameraSessionSpec(switch_time_ms=...)`. All three call sites switch to
  `self._last_confirmed_switch_time_ms` instead, for every configured
  camera - master and every slave alike.
- **Knock-on effect, no code change needed:** the cross-camera section's
  "LED Switch Time (ms)" stats-panel field (`_build_slave_section`, reading
  `specs[0].switch_time_ms`, itself sourced from the master's own
  `CameraSessionSpec.switch_time_ms`) automatically shows the new shared
  value once every `CameraSessionSpec` carries it - nothing else in the
  cross-camera Optical Sync feature needs touching.
- **Locking during a run:** same pattern as `duration_spinbox`/
  `frame_sample_interval_spinbox` already use on this page - both new
  widgets disable the moment "Start All" is clicked, re-enable together in
  `_on_all_sessions_finished`. A session in progress leaves the switch-time
  control fully non-interactive, matching Section 1's page.

## 3. What doesn't change

- **`ThresholdTuningPage`** - completely untouched.
- **Each camera's own tuned `switch_time_ms`** (set during that camera's
  Threshold Tuning step, stored in its own `config` dict) - still exists,
  still discovered by tuning against the live LED overlay, still prefills
  `LiveSessionPage`'s spinbox on arrival (`set_context()` is unchanged -
  only what `start_session()` reads at Start time changes, per Section 1).
  It's simply no longer silently threaded through as *the* value on the
  multi-camera page - the operator confirms one explicit shared number
  there instead.
- **CSV/plot exports** - no changes. Each row already records whatever
  `switch_time_ms` was actually used, via `PositionGapMetric`'s own
  existing constructor parameter.
- **Engine layer** (`SessionEngineThread`, `PositionGapMetric`,
  `CameraSessionSpec`, `CrossCameraPairSpec`, `CrossCameraReconciler`) -
  zero interface changes. Every one of these already accepts
  `switch_time_ms` as a plain parameter; only the *value* the GUI layer
  passes in changes.

## Critical files

- `gui/pages/live_session_page.py` - `start_session()`,
  `_update_confirm_switch_time_button_state()`, `_on_engine_thread_finished`
  (Section 1).
- `gui/pages/multi_camera_live_session_page.py` - new toolbar widgets,
  `start_all_sessions()`'s three `switch_time_ms` read sites, locking in
  `start_all_sessions()`/`_on_all_sessions_finished` (Section 2).
- No changes: `gui/pages/threshold_tuning_page.py`, `engine/*.py`,
  `domain/csv_export.py`, `domain/plot_export.py`.

## Testing

- `tests/gui/pages/test_live_session_page.py`: `start_session()` uses the
  last-confirmed value, not an unconfirmed spinbox edit; `start_button` is
  disabled while an edit is unconfirmed and re-enabled once confirmed;
  finishing a run while an edit is unconfirmed does not re-enable Start.
- `tests/gui/pages/test_multi_camera_live_session_page.py`: the new control
  exists with the `1.0` ms prefill; `start_all_sessions()` uses the
  confirmed value for every camera's `PositionGapMetric`/`thread_kwargs`/
  `CameraSessionSpec`, overriding each camera's own individually-tuned
  value; `start_button` ("Start All") gating mirrors Section 1's tests;
  locking during a run matches `duration_spinbox`'s existing behavior.
