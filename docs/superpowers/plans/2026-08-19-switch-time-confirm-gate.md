# LED Switch Time Confirm Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the "Confirm" button next to LED Switch Time actually gate which value a run uses (on `LiveSessionPage`, changing existing behavior) and add the same gated per-test switch-time control to the multi-camera live session page (brand new), replacing every configured camera's own individually-tuned value for that run.

**Architecture:** `self._last_confirmed_switch_time_ms` (already tracked on `LiveSessionPage`, newly added to the multi-camera page) becomes the value `start_session()`/`start_all_sessions()` actually reads, not the spinbox's raw current value. `start_button`'s enabled state becomes the AND of "no session currently running" and "no pending unconfirmed switch-time edit," computed in one shared helper method per page. No engine-layer changes - every consumer of `switch_time_ms` already accepts it as a plain parameter; only which value the GUI passes in changes.

**Tech Stack:** Python 3.10+/3.13, PySide6, pytest (`QT_QPA_PLATFORM=offscreen`, shared `qapp` fixture).

## Global Constraints

- `ThresholdTuningPage`'s own Confirm button (a genuinely different mechanism - a real, live, mid-run hardware apply) is out of scope and must not be touched.
- Switch time is disabled/non-interactive on both pages for the entire span a session is running - unchanged existing behavior on `LiveSessionPage`, and the same lock pattern on the multi-camera page.
- On the multi-camera page, the confirmed value replaces every configured camera's own individually-tuned `switch_time_ms` for that run - it is a per-test parameter, not per-camera.
- No engine-layer changes anywhere (`engine/*.py` stays untouched) - this is entirely a GUI-layer change in which value gets passed to already-existing parameters.

---

### Task 1: `LiveSessionPage` - Confirm becomes a real gate

**Files:**
- Modify: `gui/pages/live_session_page.py:150-169` (`__init__`), `:319-354` (spinbox/button setup comments), `:547-560` (`start_session`'s switch_time_ms read), `:645-658` (the run-start lock block), `:672-681` (`_update_confirm_switch_time_button_state`, `_on_confirm_switch_time_clicked`), `:862-873` (`_on_engine_thread_finished`)
- Test: `tests/gui/pages/test_live_session_page.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `self._session_running: bool` (new instance attribute) - `True` for the entire span between `start_session()` and `_on_engine_thread_finished()`. `start_session()` now reads `self._last_confirmed_switch_time_ms` instead of `self.switch_time_spinbox.value()`. `_update_confirm_switch_time_button_state()` now also sets `self.start_button`'s enabled state whenever `not self._session_running`. Task 2 mirrors this exact same shape on the multi-camera page's own widgets/methods.

- [ ] **Step 1: Write the failing tests**

In `tests/gui/pages/test_live_session_page.py`, first REMOVE `test_confirm_switch_time_click_does_not_touch_hardware_or_change_start_session_value` (lines 345-361) and its section comment above it (lines 320-325) - its entire premise (Confirm has no effect on what `start_session()` uses) is the behavior this task removes. Replace that whole section with:

```python
# --- Confirm button next to LED Switch Time - now a REAL gate.
# start_session() reads self._last_confirmed_switch_time_ms, not the
# spinbox's raw value, so an edit sitting unconfirmed in the box is never
# silently used for a run - and start_button itself stays disabled while
# one is pending, so the operator can't even attempt to start with a
# value they haven't acknowledged. ---

def test_set_context_leaves_confirm_switch_time_disabled(qapp, tmp_path):
    page = LiveSessionPage()
    page.set_context(**_minimal_context(tmp_path, switch_time_ms=7))
    assert not page.confirm_switch_time_button.isEnabled()


def test_ticking_switch_time_spinbox_enables_confirm_then_disables_if_reverted(qapp, tmp_path):
    page = LiveSessionPage()
    page.set_context(**_minimal_context(tmp_path, switch_time_ms=1))
    assert not page.confirm_switch_time_button.isEnabled()

    page.switch_time_spinbox.setValue(5)
    assert page.confirm_switch_time_button.isEnabled()

    page.switch_time_spinbox.setValue(1)
    assert not page.confirm_switch_time_button.isEnabled()


def test_ticking_switch_time_spinbox_disables_start_until_confirmed(qapp, tmp_path):
    page = LiveSessionPage()
    page.set_context(**_minimal_context(tmp_path, switch_time_ms=1))
    assert page.start_button.isEnabled()

    page.switch_time_spinbox.setValue(5)
    assert not page.start_button.isEnabled()

    page._on_confirm_switch_time_clicked()
    assert page.start_button.isEnabled()


def test_confirm_switch_time_click_makes_the_value_usable_by_start_session(qapp, tmp_path):
    # No SessionEngineThread/LEDPanel call from Confirm itself - still
    # purely a UI acknowledgment - but start_session() now reads the
    # CONFIRMED value, so clicking Confirm is what makes a new value
    # actually usable for a run.
    page = LiveSessionPage()
    page.set_context(**_minimal_context(tmp_path, switch_time_ms=1))
    page.switch_time_spinbox.setValue(5)

    with patch("gui.pages.live_session_page.SessionEngineThread") as mock_thread_cls:
        page._on_confirm_switch_time_clicked()
        mock_thread_cls.assert_not_called()

    assert not page.confirm_switch_time_button.isEnabled()

    with patch("gui.pages.live_session_page.SessionEngineThread", _FakeEngineThread):
        page.start_session()
    assert _FakeEngineThread.last_kwargs["switch_time_ms"] == 5


def test_start_session_uses_last_confirmed_value_not_an_unconfirmed_edit(qapp, tmp_path):
    page = LiveSessionPage()
    page.set_context(**_minimal_context(tmp_path, switch_time_ms=1))
    page.switch_time_spinbox.setValue(5)  # never confirmed

    with patch("gui.pages.live_session_page.SessionEngineThread", _FakeEngineThread):
        page.start_session()

    assert _FakeEngineThread.last_kwargs["switch_time_ms"] == 1


def test_finishing_a_run_does_not_reenable_start_over_a_pending_unconfirmed_edit(qapp, tmp_path):
    page = LiveSessionPage()
    page.set_context(**_minimal_context(tmp_path, switch_time_ms=1))

    with patch("gui.pages.live_session_page.SessionEngineThread", _FakeEngineThread):
        page.start_session()

    # Simulates an edit landing while the run was in progress (defensive -
    # the spinbox is normally disabled during a run, but nothing prevents a
    # direct/programmatic value change, e.g. from a future call site).
    page.switch_time_spinbox.setValue(99)

    page._on_engine_thread_finished()

    assert not page.start_button.isEnabled()
    assert page.confirm_switch_time_button.isEnabled()
```

Then update the two existing tests that set the spinbox without confirming and expect `start_session()` to use the new value - replace `tests/gui/pages/test_live_session_page.py:216-226` with:

```python
def test_start_session_passes_toolbar_switch_time_and_frame_sample_interval(qapp, tmp_path):
    page = LiveSessionPage()
    page.set_context(**_minimal_context(tmp_path, switch_time_ms=1))
    page.switch_time_spinbox.setValue(42)
    page._on_confirm_switch_time_clicked()
    page.frame_sample_interval_spinbox.setValue(99)

    with patch("gui.pages.live_session_page.SessionEngineThread", _FakeEngineThread):
        page.start_session()

    assert _FakeEngineThread.last_kwargs["switch_time_ms"] == 42
    assert _FakeEngineThread.last_kwargs["display_stride"] == 99
```

and replace `tests/gui/pages/test_live_session_page.py:262-270` with:

```python
def test_start_session_passes_a_fractional_toolbar_switch_time(qapp, tmp_path):
    page = LiveSessionPage()
    page.set_context(**_minimal_context(tmp_path, switch_time_ms=1))
    page.switch_time_spinbox.setValue(2.5)
    page._on_confirm_switch_time_clicked()

    with patch("gui.pages.live_session_page.SessionEngineThread", _FakeEngineThread):
        page.start_session()

    assert _FakeEngineThread.last_kwargs["switch_time_ms"] == 2.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_live_session_page.py -k "switch_time or unconfirmed" -v`
Expected: FAIL on three tests specifically:
- `test_ticking_switch_time_spinbox_disables_start_until_confirmed` - `AssertionError` on `assert not page.start_button.isEnabled()` (today's spinbox `valueChanged` handler never touches `start_button`, so it stays enabled).
- `test_start_session_uses_last_confirmed_value_not_an_unconfirmed_edit` - `AssertionError: assert 5 == 1` (today's `start_session()` reads the spinbox's raw value, 5, not the confirmed value, 1).
- `test_finishing_a_run_does_not_reenable_start_over_a_pending_unconfirmed_edit` - `AssertionError` on `assert not page.start_button.isEnabled()` (today's `_on_engine_thread_finished` unconditionally sets `start_button.setEnabled(True)`).

The other tests in this run (`test_set_context_leaves_confirm_switch_time_disabled`, `test_ticking_switch_time_spinbox_enables_confirm_then_disables_if_reverted`,
`test_confirm_switch_time_click_makes_the_value_usable_by_start_session`, the two updated `test_start_session_passes_*` tests) already PASS against today's code too - they don't exercise the gap this task closes, they're kept here so the whole switch-time test group stays together and self-consistent after the edits in Step 1.

- [ ] **Step 3: Implement**

Update `__init__` (currently `gui/pages/live_session_page.py:166-169`):

```python
        # The switch-time value last acknowledged via Confirm - now a REAL
        # gate (see _update_confirm_switch_time_button_state): start_session()
        # reads THIS, not the spinbox directly, so an unconfirmed edit is
        # never silently used. None until set_context() first prefills the
        # spinbox (together with this).
        self._last_confirmed_switch_time_ms = None
        # True for the entire span between start_session() and
        # _on_engine_thread_finished() - start_button's own enabled state is
        # the AND of "not currently running" and "no pending unconfirmed
        # switch-time edit" (see _update_confirm_switch_time_button_state).
        self._session_running = False
```

Update the stale setup comment on the spinbox's `valueChanged` connection (currently `gui/pages/live_session_page.py:333-344`):

```python
        # valueChanged only toggles Confirm's (and, transitively, Start's)
        # enabled state here - no hardware call happens from typing into
        # the box. Unlike gui/pages/threshold_tuning_page.py's own Confirm
        # button (which applies a new speed live, to an already-running LED
        # panel), there is no live-apply step here: this page's
        # start_session() reads self._last_confirmed_switch_time_ms, and
        # Confirm is what advances that value - an edit sitting unconfirmed
        # in the box is never used for a run, and start_button itself stays
        # disabled while one is pending (see
        # _update_confirm_switch_time_button_state).
        self.switch_time_spinbox.valueChanged.connect(self._on_switch_time_spinbox_changed)
```

Update the stale tooltip text (currently `gui/pages/live_session_page.py:348-352`):

```python
        self.confirm_switch_time_button.setToolTip(
            "Confirm the LED Switch Time above. start_session() reads the last confirmed "
            "value, not the box's raw current value - an unconfirmed edit is never used for "
            "a run, and Start stays disabled until you confirm or revert it."
        )
```

Replace `start_session`'s switch_time_ms read (currently `gui/pages/live_session_page.py:550-555`):

```python
        # Read the last CONFIRMED value, not whatever the spinbox currently
        # shows - Confirm is a real gate now (see
        # _update_confirm_switch_time_button_state): an edit sitting
        # unconfirmed in the box must never silently be used for a run, and
        # Start itself stays disabled while one is pending anyway. Used for
        # BOTH the metric's math and the LED panel's actual scan speed
        # (below) - they must agree, or position_gap_ms would be computed
        # against a switch time the panel wasn't really using.
        switch_time_ms = self._last_confirmed_switch_time_ms
```

Add `self._session_running = True` to the run-start lock block (currently `gui/pages/live_session_page.py:648-658`):

```python
        self._session_running = True
        self.status_label.setText("")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        # Changing any of these mid-run wouldn't retroactively apply to the
        # thread that already started with the values read above, and would
        # misleadingly suggest it did - lock them for the same span Start
        # itself is locked (re-enabled together in _on_engine_thread_finished).
        self.duration_spinbox.setEnabled(False)
        self.switch_time_spinbox.setEnabled(False)
        self.confirm_switch_time_button.setEnabled(False)
        self.frame_sample_interval_spinbox.setEnabled(False)
```

Replace `_update_confirm_switch_time_button_state` (currently `gui/pages/live_session_page.py:672-674`):

```python
    def _update_confirm_switch_time_button_state(self):
        unconfirmed = self.switch_time_spinbox.value() != self._last_confirmed_switch_time_ms
        self.confirm_switch_time_button.setEnabled(unconfirmed)
        # Not gated while a session is running - start_session()'s own lock
        # (setEnabled(False)) already owns start_button for that span; this
        # only decides start_button's state for the "not currently running"
        # span, where it's otherwise always available.
        if not self._session_running:
            self.start_button.setEnabled(not unconfirmed)
```

Replace `_on_engine_thread_finished` (currently `gui/pages/live_session_page.py:862-873`):

```python
    def _on_engine_thread_finished(self):
        # QThread.finished - fires only once run() has fully returned,
        # finally block included, so it's safe to let the user start a new
        # session now (the camera/LED panel are actually free).
        self._session_running = False
        self.stop_button.setEnabled(False)
        self.duration_spinbox.setEnabled(True)
        self.switch_time_spinbox.setEnabled(True)
        # Not a blind setEnabled(True) on start_button - its availability
        # also reflects whether the spinbox actually holds an unconfirmed
        # value (see _update_confirm_switch_time_button_state).
        self._update_confirm_switch_time_button_state()
        self.frame_sample_interval_spinbox.setEnabled(True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_live_session_page.py -v`
Expected: PASS (all tests in the file).

- [ ] **Step 5: Commit**

```bash
git add gui/pages/live_session_page.py tests/gui/pages/test_live_session_page.py
git commit -m "fix: LED Switch Time Confirm button becomes a real gate on start_session"
```

---

### Task 2: Multi-camera page - new per-test LED Switch Time control

**Files:**
- Modify: `gui/pages/multi_camera_live_session_page.py:1-70` (module docstring, imports), `:126-190` (`__init__`), `:372-434` (`start_all_sessions`), `:579-583` (`_on_all_sessions_finished`'s lock/unlock lines)
- Test: `tests/gui/pages/test_multi_camera_live_session_page.py`

**Interfaces:**
- Consumes: nothing new from other tasks - this page is independent of Task 1's `LiveSessionPage` changes (separate files, no shared code beyond the identical pattern being mirrored).
- Produces: `self.switch_time_spinbox`, `self.confirm_switch_time_button`, `self._last_confirmed_switch_time_ms`, `self._session_running` - same names, same shapes as Task 1's `LiveSessionPage`, so both pages behave identically from an operator's perspective. `start_all_sessions()` now reads `self._last_confirmed_switch_time_ms` (not `config["switch_time_ms"]`) at all four places that previously read it per-camera.

- [ ] **Step 1: Write the failing tests**

Add to `tests/gui/pages/test_multi_camera_live_session_page.py`:

```python
def test_switch_time_spinbox_defaults_to_one_ms(qapp, tmp_path):
    page, _ = _page_with_fake_threads()
    assert page.switch_time_spinbox.value() == 1.0
    assert page.start_button.isEnabled()


def test_ticking_switch_time_spinbox_disables_start_all_until_confirmed(qapp, tmp_path):
    page, _ = _page_with_fake_threads()
    page.set_cameras(object(), _two_cameras(tmp_path))
    assert page.start_button.isEnabled()

    page.switch_time_spinbox.setValue(5.0)
    assert not page.start_button.isEnabled()

    page._on_confirm_switch_time_clicked()
    assert page.start_button.isEnabled()


def test_start_all_sessions_uses_confirmed_switch_time_for_every_camera(qapp, tmp_path):
    page, fake_threads = _page_with_fake_threads()
    cameras = _two_cameras(tmp_path)
    # Each camera's own individually-tuned value - both must be overridden.
    cameras[0]["config"]["switch_time_ms"] = 3.0
    cameras[1]["config"]["switch_time_ms"] = 7.0
    page.set_cameras(object(), cameras)
    page.switch_time_spinbox.setValue(5.0)
    page._on_confirm_switch_time_clicked()

    page.start_all_sessions()

    assert fake_threads["SN1"].kwargs["switch_time_ms"] == 5.0
    assert fake_threads["SN2"].kwargs["switch_time_ms"] == 5.0
    specs_by_camera_id = {spec.camera_id: spec for spec in page._controller._camera_specs}
    assert specs_by_camera_id["cam1"].switch_time_ms == 5.0
    assert specs_by_camera_id["cam2"].switch_time_ms == 5.0


def test_finishing_all_sessions_does_not_reenable_start_over_a_pending_unconfirmed_edit(qapp, tmp_path):
    page, fake_threads = _page_with_fake_threads()
    page.set_cameras(object(), _two_cameras(tmp_path))
    page.start_all_sessions()

    # Simulates an edit landing while sessions were running (defensive -
    # the spinbox is normally disabled during a run).
    page.switch_time_spinbox.setValue(99.0)

    fake_threads["SN1"].session_finished.emit([])
    fake_threads["SN1"].finished.emit()
    fake_threads["SN2"].session_finished.emit([])
    fake_threads["SN2"].finished.emit()

    assert not page.start_button.isEnabled()
    assert page.confirm_switch_time_button.isEnabled()
```

Then extend the two existing lock/unlock tests with two new assertions each. Update `test_start_all_sessions_locks_toolbar_and_starts_every_thread` (currently `tests/gui/pages/test_multi_camera_live_session_page.py:157-167`):

```python
def test_start_all_sessions_locks_toolbar_and_starts_every_thread(qapp, tmp_path):
    page, fake_threads = _page_with_fake_threads()
    page.set_cameras(object(), _two_cameras(tmp_path))

    page.start_all_sessions()

    assert not page.start_button.isEnabled()
    assert page.stop_button.isEnabled()
    assert not page.duration_spinbox.isEnabled()
    assert not page.switch_time_spinbox.isEnabled()
    assert not page.confirm_switch_time_button.isEnabled()
    assert fake_threads["SN1"].started
    assert fake_threads["SN2"].started
```

Update `test_all_sessions_finished_reenables_start` (currently `tests/gui/pages/test_multi_camera_live_session_page.py:381-393`):

```python
def test_all_sessions_finished_reenables_start(qapp, tmp_path):
    page, fake_threads = _page_with_fake_threads()
    page.set_cameras(object(), _two_cameras(tmp_path))
    page.start_all_sessions()

    fake_threads["SN1"].session_finished.emit([])
    fake_threads["SN1"].finished.emit()
    fake_threads["SN2"].session_finished.emit([])
    fake_threads["SN2"].finished.emit()

    assert page.start_button.isEnabled()
    assert not page.stop_button.isEnabled()
    assert page.duration_spinbox.isEnabled()
    assert page.switch_time_spinbox.isEnabled()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_multi_camera_live_session_page.py -k "switch_time or unconfirmed or locks_toolbar or reenables_start" -v`
Expected: FAIL - `test_switch_time_spinbox_defaults_to_one_ms` fails with `AttributeError: 'MultiCameraLiveSessionPage' object has no attribute 'switch_time_spinbox'` (the control doesn't exist yet); every other new/updated test in this run fails the same way, since none of `switch_time_spinbox`/`confirm_switch_time_button`/`_on_confirm_switch_time_clicked` exist on this page yet.

- [ ] **Step 3: Implement**

Add `QDoubleSpinBox` to the PySide6 import (currently `gui/pages/multi_camera_live_session_page.py:54-56`):

```python
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSpinBox, QDoubleSpinBox, QTabWidget,
)
```

Update the module docstring's stale "LED switch time is NOT a toolbar control" paragraph (currently lines 17-23):

```python
Toolbar is deliberately RUN-level, not per-camera: Duration, LED Switch
Time, and Frame Sample Interval all apply identically to every camera when
Start All is clicked. LED Switch Time replaces every configured camera's
own individually-tuned switch_time_ms (set on that camera's own Threshold
Tuning page) for the run - it's a per-test parameter, not per-camera,
since it configures the LED panel itself (one physical panel stepping at
one real rate, even in the shared-single-panel case with 2+ cameras), not
any one camera. Duration/Frame Sample Interval staying run-level rather
than per-camera-independent is the design doc's own "Explicitly deferred
to v2" simplification, unrelated to this.
```

Add the new instance attributes to `__init__` (currently `gui/pages/multi_camera_live_session_page.py:150-151`, right after `self._cross_rows = []`):

```python
        self._cross_rows = []
        # The switch-time value last acknowledged via Confirm - a real gate
        # (see _update_confirm_switch_time_button_state): start_all_sessions()
        # reads THIS, not the spinbox directly, for every configured camera.
        self._last_confirmed_switch_time_ms = 1.0
        # True for the entire span between start_all_sessions() and
        # _on_all_sessions_finished() - start_button's own enabled state is
        # the AND of "not currently running" and "no pending unconfirmed
        # switch-time edit" (see _update_confirm_switch_time_button_state).
        self._session_running = False
```

Insert the new toolbar widgets between `duration_spinbox` and `start_button` (currently `gui/pages/multi_camera_live_session_page.py:156-165`):

```python
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Duration (s, 0 = manual stop):"))
        self.duration_spinbox = QSpinBox()
        self.duration_spinbox.setRange(0, 3600)
        toolbar.addWidget(self.duration_spinbox)

        toolbar.addWidget(QLabel("LED Switch Time (ms):"))
        self.switch_time_spinbox = QDoubleSpinBox()
        self.switch_time_spinbox.setRange(0.1, 10000.0)
        self.switch_time_spinbox.setDecimals(1)
        self.switch_time_spinbox.setSingleStep(0.5)
        self.switch_time_spinbox.setValue(1.0)
        self.switch_time_spinbox.valueChanged.connect(self._on_switch_time_spinbox_changed)
        toolbar.addWidget(self.switch_time_spinbox)
        self.confirm_switch_time_button = QPushButton("Confirm")
        self.confirm_switch_time_button.setEnabled(False)
        self.confirm_switch_time_button.setToolTip(
            "Confirm the LED Switch Time above. This is a per-test parameter shared by "
            "every camera (it configures the LED panel, not any one camera) - "
            "start_all_sessions() reads the last confirmed value, and Start All stays "
            "disabled until any edit here is confirmed or reverted."
        )
        self.confirm_switch_time_button.clicked.connect(self._on_confirm_switch_time_clicked)
        toolbar.addWidget(self.confirm_switch_time_button)

        self.start_button = QPushButton("Start All")
        self.start_button.clicked.connect(self.start_all_sessions)
        self.stop_button = QPushButton("Stop All")
        self.stop_button.clicked.connect(self.stop_all_sessions)
        self.stop_button.setEnabled(False)
        toolbar.addWidget(self.start_button)
        toolbar.addWidget(self.stop_button)
```

Add the three new handler methods. Place them near `stop_all_sessions` (currently `gui/pages/multi_camera_live_session_page.py:477-479`):

```python
    def stop_all_sessions(self):
        if self._controller is not None:
            self._controller.stop_all()

    def _on_switch_time_spinbox_changed(self, value):
        self._update_confirm_switch_time_button_state()

    def _update_confirm_switch_time_button_state(self):
        unconfirmed = self.switch_time_spinbox.value() != self._last_confirmed_switch_time_ms
        self.confirm_switch_time_button.setEnabled(unconfirmed)
        # Not gated while a session is running - start_all_sessions()'s own
        # lock (setEnabled(False)) already owns start_button for that span.
        if not self._session_running:
            self.start_button.setEnabled(not unconfirmed)

    def _on_confirm_switch_time_clicked(self):
        self._last_confirmed_switch_time_ms = self.switch_time_spinbox.value()
        self._update_confirm_switch_time_button_state()
```

Update `start_all_sessions` (currently `gui/pages/multi_camera_live_session_page.py:372-434`) - add `self._session_running = True` near the top, replace all four `config["switch_time_ms"]` reads with `self._last_confirmed_switch_time_ms`, and lock the two new widgets in the existing lock block:

```python
    def start_all_sessions(self):
        if not self._cameras:
            return
        self._session_running = True
        duration_s = self.duration_spinbox.value() or None
        display_stride = self.frame_sample_interval_spinbox.value()

        # ONE shared run folder for the whole multi-camera run, one
        # subfolder per camera underneath it - every configured camera's
        # own files land together under one run instead of scattered
        # across independent top-level output/live_session_<timestamp>/
        # folders. output_root is read from the master's own config -
        # every camera's own settings.yaml-derived output_root should be
        # identical in practice (one app, one settings.yaml), same
        # assumption pairing_gap_outlier_threshold_us below already makes.
        master_config = next(c["config"] for c in self._cameras if c["is_master"])
        self._run_dir = create_run_dir(master_config["output_root"], "live_session")
        self._cross_rows = []
        self._reset_cross_run_state()

        camera_specs = []
        for camera in self._cameras:
            camera_id = camera["camera_id"]
            config = camera["config"]
            panel = self._panels[camera_id]

            position_gap_metric = PositionGapMetric(
                stream_a_threshold=config["stream_a_threshold"], stream_b_threshold=config["stream_b_threshold"],
                num_leds=config["num_leds"], switch_time_ms=self._last_confirmed_switch_time_ms,
                warmup_pairs_to_skip=config["warmup_pairs_to_skip"],
            )
            metrics = [
                PairingGapMetric(outlier_threshold_us=config["pairing_gap_outlier_threshold_us"]),
                position_gap_metric,
            ]
            test_session = TestSession(TestSessionConfig(
                metrics=metrics, duration_s=duration_s,
                stream_a_fps=config["pick_a"]["fps"], stream_b_fps=config["pick_b"]["fps"],
                frame_drop_threshold_factor=config["frame_drop_threshold_factor"],
            ))
            test_session.start()

            camera_output_dir = create_camera_subdir(self._run_dir, camera_id, camera["label"])
            output_dir = panel.prepare_for_run(
                output_dir=camera_output_dir, kept_csv_filename=config["kept_csv_filename"],
                dropped_csv_filename=config["dropped_csv_filename"],
                stream_a_xy=config["stream_a_xy"], stream_b_xy=config["stream_b_xy"],
                stream_a_roi=config["stream_a_roi"], stream_b_roi=config["stream_b_roi"],
                snapshot_every_n_pairs=config["snapshot_every_n_pairs"], max_snapshots=config["max_snapshots"],
                switch_time_ms=self._last_confirmed_switch_time_ms,
            )

            thread_kwargs = dict(
                pick_a=config["pick_a"], pick_b=config["pick_b"], camera_controls=config["camera_controls"],
                test_session=test_session,
                stream_a_xy=config["stream_a_xy"], stream_b_xy=config["stream_b_xy"],
                neighborhood_size=config["neighborhood_size"], scan_direction=config["scan_direction"],
                switch_time_ms=self._last_confirmed_switch_time_ms, display_stride=display_stride,
                position_gap_metric=position_gap_metric, dual_panel_config=config["dual_panel_config"],
                enable_depth_for_ir_sync=config["enable_depth_for_ir_sync"],
                output_dir=output_dir,
                position_gap_outlier_threshold_ms=config["position_gap_outlier_threshold_ms"],
                position_gap_outlier_max_snapshots=config["position_gap_outlier_max_snapshots"],
            )

            camera_specs.append(CameraSessionSpec(
                camera_id=camera_id, is_master=camera["is_master"],
                inter_cam_sync_value=config.get("inter_cam_sync_value"),
                stream_identities=_stream_identities(config),
                device_serial=config["device_serial"],
                num_leds=config["num_leds"], switch_time_ms=self._last_confirmed_switch_time_ms,
                hardware_reset_before_start=config["hardware_reset_before_start"],
                hardware_reset_settle_s=config["hardware_reset_settle_s"],
                thread_kwargs=thread_kwargs,
            ))

        controller_kwargs = dict(
            pairing_gap_outlier_threshold_us=master_config["pairing_gap_outlier_threshold_us"],
        )
        if self._thread_factory is not None:
            controller_kwargs["thread_factory"] = self._thread_factory
        if self._device_lookup is not None:
            controller_kwargs["device_lookup"] = self._device_lookup
        if self._sync_setter is not None:
            controller_kwargs["sync_setter"] = self._sync_setter
        if self._camera_start_stagger_s is not None:
            controller_kwargs["camera_start_stagger_s"] = self._camera_start_stagger_s

        self._controller = self._controller_factory(camera_specs, **controller_kwargs)
        self._controller.camera_frame_ready.connect(self._on_camera_frame_ready)
        self._controller.camera_row_ready.connect(self._on_camera_row_ready)
        self._controller.camera_stats_ready.connect(self._on_camera_stats_ready)
        self._controller.camera_session_finished.connect(self._on_camera_session_finished)
        self._controller.camera_error.connect(self._on_camera_error)
        self._controller.cross_pair_ready.connect(self._on_cross_pair_ready)
        self._controller.cross_stats_ready.connect(self._on_cross_stats_ready)
        self._controller.all_sessions_finished.connect(self._on_all_sessions_finished)

        self.status_label.setText("")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.duration_spinbox.setEnabled(False)
        self.switch_time_spinbox.setEnabled(False)
        self.confirm_switch_time_button.setEnabled(False)
        self.frame_sample_interval_spinbox.setEnabled(False)

        self._controller.start_all(self._ctx)
```

Update `_on_all_sessions_finished`'s lock/unlock lines (currently `gui/pages/multi_camera_live_session_page.py:579-583`):

```python
    def _on_all_sessions_finished(self, rows_by_camera):
        self._session_running = False
        self.stop_button.setEnabled(False)
        self.duration_spinbox.setEnabled(True)
        self.switch_time_spinbox.setEnabled(True)
        # Not a blind setEnabled(True) on start_button - its availability
        # also reflects whether the spinbox actually holds an unconfirmed
        # value (see _update_confirm_switch_time_button_state).
        self._update_confirm_switch_time_button_state()
        self.frame_sample_interval_spinbox.setEnabled(True)
```

(The rest of `_on_all_sessions_finished` - the CSV/per-slave-plot export block - is unchanged; only these five lines at the top of the method change.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_multi_camera_live_session_page.py -v`
Expected: PASS (all tests in the file).

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python.exe -m pytest -v`
Expected: PASS (every test in the project).

- [ ] **Step 6: Commit**

```bash
git add gui/pages/multi_camera_live_session_page.py tests/gui/pages/test_multi_camera_live_session_page.py
git commit -m "feat: multi-camera page gets a per-test LED Switch Time control"
```

---

## Self-Review

**Spec coverage:**
- Section 1 (`LiveSessionPage` Confirm becomes a real gate, `start_button` disabled while unconfirmed, locking during a run unchanged) - Task 1.
- Section 2 (multi-camera per-test control, `1.0` ms prefill with matching `_last_confirmed_switch_time_ms` initial value, replaces every camera's own tuned value at all read sites, locking during a run) - Task 2. Note: the spec named three read sites (`PositionGapMetric`, `thread_kwargs`, `CameraSessionSpec`); tracing the actual current code surfaced a fourth (`panel.prepare_for_run`'s own `switch_time_ms` param, which only feeds that camera's own per-camera stats-panel display field) - Task 2's implementation updates all four for consistency, since a displayed "LED Switch Time" number should show the same shared value everywhere, not a stale per-camera one.
- Section 3 (`ThresholdTuningPage` untouched, per-camera tuned values still exist and still prefill `LiveSessionPage`, CSV/plot exports unchanged, engine layer unchanged) - confirmed by scope: no task touches `gui/pages/threshold_tuning_page.py`, `engine/*.py`, `domain/csv_export.py`, or `domain/plot_export.py`.

**Placeholder scan:** No TBD/TODO/"add appropriate"/"similar to Task N" phrases - every step has real, complete code.

**Type consistency:** `self._last_confirmed_switch_time_ms`/`self._session_running`/`_update_confirm_switch_time_button_state`/`_on_switch_time_spinbox_changed`/`_on_confirm_switch_time_clicked` are named and shaped identically across both tasks' two independent files, matching the spec's explicit intent that both pages behave identically from an operator's perspective.
