# Cross-Camera Graphs Per-Slave Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure the multi-camera live session page's cross-camera graphs to group by slave camera first (one graph-pair + one combined stats panel per slave, mirroring the per-camera tab's own layout) instead of today's single graph-per-metric with every slave/identity flattened onto it, add serial-number + master/slave-role labeling throughout, and bring the cross-camera stats panels to full live-data parity with the per-camera tabs.

**Architecture:** A new pure helper (`_camera_roles`) computes master/slave-N role tags, filename-safe slugs, and label+serial display strings once, reused by per-camera tab labels, cross-camera section headers, and static-export titles/filenames. The cross-camera section becomes one section (header + 2 stacked graphs + 1 combined stats panel) per slave — shown directly when there's exactly 1 slave, or inside an inner tab widget when there are 2. `RunningStats` instances (one per slave/identity/metric triple) accumulate unthrottled in `_on_cross_pair_ready` and get pushed to the stats panel only on the throttled `_on_cross_stats_ready` tick, mirroring `CameraLiveSessionPanel`'s own cadence discipline exactly. The static PNG export becomes one file per slave. No engine-layer changes.

**Tech Stack:** Python 3.10+/3.13, PySide6, pyqtgraph (`gui/widgets/live_plot.py`'s `LivePlot`), matplotlib `Agg` backend (`domain/plot_export.py`), pytest (`QT_QPA_PLATFORM=offscreen`, shared `qapp` fixture).

## Global Constraints

- No engine-layer changes anywhere in this feature (`engine/cross_camera_reconciler.py`, `engine/multi_camera_session.py` stay untouched) — this is entirely a GUI/plot_export presentation change over already-available `slave_camera_id`/`stream_identity` data.
- Slave numbering ("Slave 1", "Slave 2", ...) is assigned in the order cameras appear in the `cameras` list, excluding master — the same order the per-camera tabs already iterate in.
- With exactly 1 slave, no inner tab widget appears — the slave's section (header + graphs + stats) is shown directly. Inner tabs only appear at 2+ slaves.
- Within a slave's own graphs/stats fields, lines/fields are named by stream identity alone (`"infrared1"`, `"color"`) — never prefixed with the camera name, since the slave is already established by the section/tab.
- `row_ready`/unthrottled callbacks (`_on_cross_pair_ready`) must stay O(1) — no `add_point`/plotting calls there; only cheap bookkeeping and `RunningStats.update()` calls. All `add_point`/`set_value` calls happen in the throttled `_on_cross_stats_ready`.
- `export_cross_camera_csv` (`domain/csv_export.py`) is unchanged — stays one combined CSV with every slave's rows together.
- Debug images for cross-camera Optical Sync outliers are explicitly OUT OF SCOPE for this plan (deferred to a future spec).

---

### Task 1: `_camera_roles` helper

**Files:**
- Modify: `gui/pages/multi_camera_live_session_page.py:86-87` (insert after `_stream_identities`)
- Test: `tests/gui/pages/test_multi_camera_live_session_page.py`

**Interfaces:**
- Consumes: `cameras` — the same `list of {"camera_id", "label", "is_master", "config"}` shape `set_cameras()` already takes; reads `camera["config"]["device_serial"]` (already populated in every real and test config dict).
- Produces: `_camera_roles(cameras) -> dict[str, dict]` — `{camera_id: {"tag": str, "slug": str, "display": str}}`. `tag` is `"MASTER"`, `"SLAVE 1"`, `"SLAVE 2"`, ...; `slug` is `"master"`, `"slave1"`, `"slave2"`, ... (filename-safe); `display` is `"{label} (SN {device_serial})"`. Tasks 2, 3, 6 all call this function.

- [ ] **Step 1: Write the failing test**

Add to `tests/gui/pages/test_multi_camera_live_session_page.py`, near the top after the existing helpers (e.g. after `_two_cameras`):

```python
def test_camera_roles_tags_master_and_numbers_slaves_in_order():
    from gui.pages.multi_camera_live_session_page import _camera_roles

    cameras = [
        {"camera_id": "cam1", "label": "D455 A", "is_master": True, "config": {"device_serial": "SN1"}},
        {"camera_id": "cam2", "label": "D455 B", "is_master": False, "config": {"device_serial": "SN2"}},
        {"camera_id": "cam3", "label": "D455 C", "is_master": False, "config": {"device_serial": "SN3"}},
    ]

    roles = _camera_roles(cameras)

    assert roles["cam1"] == {"tag": "MASTER", "slug": "master", "display": "D455 A (SN SN1)"}
    assert roles["cam2"] == {"tag": "SLAVE 1", "slug": "slave1", "display": "D455 B (SN SN2)"}
    assert roles["cam3"] == {"tag": "SLAVE 2", "slug": "slave2", "display": "D455 C (SN SN3)"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_multi_camera_live_session_page.py -k camera_roles -v`
Expected: FAIL with `ImportError: cannot import name '_camera_roles'`.

- [ ] **Step 3: Implement**

In `gui/pages/multi_camera_live_session_page.py`, insert this new function right after `_stream_identities` (currently lines 86-87):

```python
def _camera_roles(cameras):
    """Computes each camera's master/slave-N role once, reused everywhere a
    role/label/serial needs displaying (per-camera tabs, cross-camera
    section headers, static-export titles/filenames) - so the numbering is
    never computed two different ways. Slave numbering is assigned in the
    order cameras appear in `cameras` (excluding master), the same order
    the per-camera tabs already iterate in."""
    roles = {}
    slave_number = 0
    for camera in cameras:
        camera_id = camera["camera_id"]
        display = "{} (SN {})".format(camera["label"], camera["config"]["device_serial"])
        if camera["is_master"]:
            roles[camera_id] = {"tag": "MASTER", "slug": "master", "display": display}
        else:
            slave_number += 1
            roles[camera_id] = {
                "tag": "SLAVE {}".format(slave_number),
                "slug": "slave{}".format(slave_number),
                "display": display,
            }
    return roles
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_multi_camera_live_session_page.py -k camera_roles -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add gui/pages/multi_camera_live_session_page.py tests/gui/pages/test_multi_camera_live_session_page.py
git commit -m "feat: add _camera_roles helper for master/slave-N labeling"
```

---

### Task 2: Per-camera tab labels get their role tag

**Files:**
- Modify: `gui/pages/multi_camera_live_session_page.py:152-173` (`set_cameras`)
- Test: `tests/gui/pages/test_multi_camera_live_session_page.py`

**Interfaces:**
- Consumes: `_camera_roles(cameras)` (Task 1).
- Produces: every per-camera tab's label now includes its role tag, not just the master's.

- [ ] **Step 1: Write the failing test**

Add to `tests/gui/pages/test_multi_camera_live_session_page.py`:

```python
def test_set_cameras_tags_every_tab_with_its_role(qapp, tmp_path):
    page, _ = _page_with_fake_threads()

    page.set_cameras(object(), _two_cameras(tmp_path))

    # Tab 0 is Cross-Camera Sync, tab 1 is cam1 (master), tab 2 is cam2 (slave 1).
    assert page.tabs.tabText(1) == "D455 A [MASTER]"
    assert page.tabs.tabText(2) == "D455 B [SLAVE 1]"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_multi_camera_live_session_page.py -k tags_every_tab -v`
Expected: FAIL with `AssertionError: 'D455 B' != 'D455 B [SLAVE 1]'` (today's code only tags the master).

- [ ] **Step 3: Implement**

Replace `set_cameras` (`gui/pages/multi_camera_live_session_page.py:152-173`):

```python
    def set_cameras(self, ctx, cameras):
        """cameras: list of {"camera_id", "label", "is_master", "config"} -
        exactly what MainWindow's self._cameras/self._master_camera_id
        already hold, built fresh by MainWindow's own _refresh_camera_hub-
        style helper right before switching to this page."""
        self._ctx = ctx
        self._cameras = cameras

        self.tabs.clear()
        self._panels = {}

        # Cross-camera tab first - it's the operator's primary test.
        self._rebuild_cross_camera_section(cameras)
        self.tabs.addTab(self._cross_tab_widget, "Cross-Camera Sync")

        roles = _camera_roles(cameras)
        for camera in cameras:
            panel = CameraLiveSessionPanel(camera["camera_id"])
            config = camera["config"]
            panel.set_camera_labels(camera["label"], config["stream_a_label"], config["stream_b_label"])
            tab_label = "{} [{}]".format(camera["label"], roles[camera["camera_id"]]["tag"])
            self.tabs.addTab(panel, tab_label)
            self._panels[camera["camera_id"]] = panel
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_multi_camera_live_session_page.py -v`
Expected: PASS (all tests in the file).

- [ ] **Step 5: Commit**

```bash
git add gui/pages/multi_camera_live_session_page.py tests/gui/pages/test_multi_camera_live_session_page.py
git commit -m "feat: tag every per-camera tab with its master/slave-N role"
```

---

### Task 3: Cross-camera section rebuilt per slave

**Files:**
- Modify: `gui/pages/multi_camera_live_session_page.py:1-67` (imports, module docstring), `:139-150` (`__init__`), `:175-244` (`_rebuild_cross_camera_section`, new `_build_slave_section`)
- Test: `tests/gui/pages/test_multi_camera_live_session_page.py`

**Interfaces:**
- Consumes: `_camera_roles` (Task 1); `build_cross_camera_pair_specs`/`_IdentitySpec`/`_stream_identities` (unchanged, existing); `domain.running_stats.RunningStats` (new import).
- Produces: `self._slave_sections: dict[str, dict]` — `{slave_camera_id: {"pairing_plot": LivePlot, "position_plot": LivePlot, "stats_panel": StatsPanel}}`. `self._cross_pair_series_keys: dict[tuple, str]` — stays keyed by `(slave_camera_id, stream_identity)`, but the series key value is now the bare identity string (e.g. `"infrared1"`), not `"{slave}::{identity}"`. `self._cross_running_stats: dict[tuple, RunningStats]` — keyed by `(slave_camera_id, stream_identity, metric_name)` where `metric_name` is `"pairing_gap_us"` or `"position_gap_ms"`, each a freshly-constructed empty `RunningStats()`. `self.cross_plot`/`self.cross_stats_panel`/`self.cross_position_plot`/`self.cross_position_stats_panel` (the old single shared instances) are REMOVED — Task 4 and any remaining consumers must use `self._slave_sections` instead. Stats-panel field keys per slave: `"pair_index"`, `"switch_time_ms"`, `"{identity}_pairing_gap_us"`, `"{identity}_position_gap_ms"`, and (via `add_stats_table`) `"{identity}_hw_ts_latency_min/avg/std/max"`, `"{identity}_optical_sync_min/avg/std/max"` — Task 4 pushes values into these exact keys.

- [ ] **Step 1: Write the failing tests**

Replace the two existing `page.cross_plot`/`page.cross_position_plot`-based tests and add new structural tests. In `tests/gui/pages/test_multi_camera_live_session_page.py`, remove `test_cross_pair_ready_does_not_plot_directly`, `test_matching_rows_plot_a_cross_camera_hw_ts_point_on_stats_ready`, and `test_matching_rows_plot_a_cross_camera_optical_sync_point_on_stats_ready` (lines 250-350) — Task 4 rewrites these against the new per-slave widgets. For Task 3 alone, add these structural tests (they don't yet require `_on_cross_pair_ready`/`_on_cross_stats_ready` to route correctly — that's Task 4):

```python
def test_one_slave_shows_section_directly_with_no_inner_tabs(qapp, tmp_path):
    page, _ = _page_with_fake_threads()

    page.set_cameras(object(), _two_cameras(tmp_path))

    assert "cam2" in page._slave_sections
    section = page._slave_sections["cam2"]
    assert section["pairing_plot"] is not None
    assert section["position_plot"] is not None
    assert section["stats_panel"] is not None
    # No inner QTabWidget anywhere under the cross-camera tab for exactly 1 slave.
    from PySide6.QtWidgets import QTabWidget
    inner_tabs = page._cross_tab_widget.findChildren(QTabWidget)
    assert inner_tabs == []


def test_two_slaves_get_an_inner_tab_each(qapp, tmp_path):
    page, _ = _page_with_fake_threads()
    cameras = _two_cameras(tmp_path)
    cameras.append({"camera_id": "cam3", "label": "D455 C", "is_master": False,
                     "config": _camera_config(tmp_path, device_serial="SN3")})

    page.set_cameras(object(), cameras)

    assert set(page._slave_sections.keys()) == {"cam2", "cam3"}
    from PySide6.QtWidgets import QTabWidget
    inner_tabs = page._cross_tab_widget.findChildren(QTabWidget)
    assert len(inner_tabs) == 1
    inner = inner_tabs[0]
    assert inner.count() == 2
    assert inner.tabText(0) == "Slave 1: D455 B"
    assert inner.tabText(1) == "Slave 2: D455 C"


def test_cross_pair_series_keys_are_bare_identity_strings(qapp, tmp_path):
    page, _ = _page_with_fake_threads()

    page.set_cameras(object(), _two_cameras(tmp_path))

    assert page._cross_pair_series_keys[("cam2", "infrared1")] == "infrared1"
    assert page._cross_pair_series_keys[("cam2", "color")] == "color"


def test_cross_running_stats_registered_per_slave_identity_and_metric(qapp, tmp_path):
    page, _ = _page_with_fake_threads()

    page.set_cameras(object(), _two_cameras(tmp_path))

    assert ("cam2", "infrared1", "pairing_gap_us") in page._cross_running_stats
    assert ("cam2", "infrared1", "position_gap_ms") in page._cross_running_stats
    assert ("cam2", "color", "pairing_gap_us") in page._cross_running_stats
    assert ("cam2", "color", "position_gap_ms") in page._cross_running_stats
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_multi_camera_live_session_page.py -k "one_slave_shows_section or two_slaves_get_an_inner_tab or bare_identity_strings or registered_per_slave" -v`
Expected: FAIL - `AttributeError: 'MultiCameraLiveSessionPage' object has no attribute '_slave_sections'`.

- [ ] **Step 3: Implement**

Add the import in `gui/pages/multi_camera_live_session_page.py` (near the top, with the other `domain.*` imports at line 64-67):

```python
from domain.run_output import create_run_dir, create_camera_subdir
from domain.csv_export import export_cross_camera_csv
from domain.plot_export import export_cross_camera_plot
from domain.plot_theme import CROSS_CAMERA_COLORS
from domain.running_stats import RunningStats
```

Update the module docstring's opening paragraph (currently lines 1-6):

```python
"""Wizard's actual multi-camera live-run page - a tab widget with one
always-first "Cross-Camera Sync" tab (one section per slave camera, each
with its own HW TS Latency + Optical Sync graphs and stats panel - shown
directly for a single slave, or inside an inner tab widget once there are
2) followed by one CameraLiveSessionPanel tab per configured camera (each
camera's own intra-camera view, unchanged from today's single-camera
experience), all driven by ONE
engine.multi_camera_session.MultiCameraSessionController.
```

Replace `__init__`'s cross-tab-related attributes (currently lines 142-147):

```python
        self._cross_tab_widget = QWidget()
        self._cross_tab_layout = QVBoxLayout(self._cross_tab_widget)
        # slave_camera_id -> {"pairing_plot": LivePlot, "position_plot": LivePlot,
        # "stats_panel": StatsPanel} - one full section's worth of widgets per slave.
        self._slave_sections = {}
        # (slave_camera_id, stream_identity, metric_name) -> RunningStats,
        # metric_name is "pairing_gap_us" or "position_gap_ms" - accumulated
        # unthrottled in _on_cross_pair_ready, pushed to the stats panel only
        # on the throttled _on_cross_stats_ready tick.
        self._cross_running_stats = {}
```

Replace `_rebuild_cross_camera_section` (currently lines 175-244) with this and a new `_build_slave_section` method:

```python
    def _rebuild_cross_camera_section(self, cameras):
        while self._cross_tab_layout.count():
            item = self._cross_tab_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self._cross_pair_series_keys = {}
        self._slave_sections = {}
        self._cross_running_stats = {}

        self._cross_tab_layout.addWidget(QLabel("Cross-Camera Sync (master vs. each slave)"))

        if len(cameras) < 2:
            self._cross_tab_layout.addWidget(
                QLabel("Add a second camera to see cross-camera sync.")
            )
            return

        roles = _camera_roles(cameras)
        master_camera = next(c for c in cameras if c["is_master"])
        master_display = roles[master_camera["camera_id"]]["display"]

        identity_specs = [
            _IdentitySpec(
                camera["camera_id"], camera["is_master"], _stream_identities(camera["config"]),
                num_leds=camera["config"]["num_leds"], switch_time_ms=camera["config"]["switch_time_ms"],
            )
            for camera in cameras
        ]
        try:
            # outlier_threshold_us here only shapes the PairingGapMetric
            # instances this call constructs for its own throwaway use
            # (deciding which series to show) - start_all_sessions builds
            # the REAL ones the controller actually uses, with each run's
            # own configured threshold.
            pair_specs = build_cross_camera_pair_specs(identity_specs, outlier_threshold_us=100_000)
        except ValueError:
            # No master designated - shouldn't be reachable once Start is
            # actually clickable (CameraHubPage._can_start already requires
            # exactly one), but guard defensively rather than crash the page.
            pair_specs = []

        # Grouped by slave, preserving each slave's own identity order -
        # build_cross_camera_pair_specs already returns identities sorted
        # per slave, so no re-sorting needed here.
        specs_by_slave = {}
        for spec in pair_specs:
            specs_by_slave.setdefault(spec.slave_camera_id, []).append(spec)

        slave_cameras = [camera for camera in cameras if not camera["is_master"]]

        if len(slave_cameras) == 1:
            slave = slave_cameras[0]
            section_widget = self._build_slave_section(
                slave, roles, master_display, specs_by_slave.get(slave["camera_id"], [])
            )
            self._cross_tab_layout.addWidget(section_widget)
        else:
            inner_tabs = QTabWidget()
            for slave in slave_cameras:
                section_widget = self._build_slave_section(
                    slave, roles, master_display, specs_by_slave.get(slave["camera_id"], [])
                )
                tab_label = "{}: {}".format(roles[slave["camera_id"]]["tag"].title(), slave["label"])
                inner_tabs.addTab(section_widget, tab_label)
            self._cross_tab_layout.addWidget(inner_tabs)

    def _build_slave_section(self, slave, roles, master_display, specs):
        """One slave's worth of cross-camera UI: a header line, two stacked
        graphs (HW TS Latency, Optical Sync), and one combined stats panel -
        mirrors CameraLiveSessionPanel's own graphs_column + single
        stats_panel layout exactly, scoped to this one slave's shared
        identities. Registers this slave's series keys and RunningStats
        instances into self._cross_pair_series_keys/self._cross_running_stats
        as a side effect - _on_cross_pair_ready/_on_cross_stats_ready
        (Task 4) read those to route incoming cross-rows here."""
        slave_camera_id = slave["camera_id"]
        slave_role = roles[slave_camera_id]

        section_widget = QWidget()
        section_layout = QVBoxLayout(section_widget)

        header_text = "{}: {}  vs.  Master: {}".format(
            slave_role["tag"].title(), slave_role["display"], master_display
        )
        section_layout.addWidget(QLabel(header_text))

        pairing_plot = LivePlot()
        pairing_plot.setLabel("left", "HW TS Latency (us)")
        pairing_plot.setLabel("bottom", "Pair Index")

        position_plot = LivePlot()
        position_plot.setLabel("left", "Optical Sync (ms)")
        position_plot.setLabel("bottom", "Pair Index")

        stats_panel = StatsPanel()
        stats_panel.setFixedWidth(220)
        stats_panel.add_section_header("Live Data")
        stats_panel.add_field("pair_index", "Pair Index")
        for spec in specs:
            identity = spec.stream_identity
            stats_panel.add_field("{}_pairing_gap_us".format(identity), "{} HW TS Latency (us)".format(identity))
            stats_panel.add_field("{}_position_gap_ms".format(identity), "{} Optical Sync (ms)".format(identity))
        stats_panel.add_field("switch_time_ms", "LED Switch Time (ms)")
        stats_panel.add_section_header("Stats")
        stats_rows = []
        for spec in specs:
            identity = spec.stream_identity
            stats_rows.append(("{}_hw_ts_latency".format(identity), "{} HW TS Latency".format(identity)))
            stats_rows.append(("{}_optical_sync".format(identity), "{} Optical Sync".format(identity)))
        stats_panel.add_stats_table(stats_rows)
        if specs:
            stats_panel.set_value("switch_time_ms", specs[0].switch_time_ms)

        for index, spec in enumerate(specs):
            identity = spec.stream_identity
            color = CROSS_CAMERA_COLORS[index % len(CROSS_CAMERA_COLORS)]
            pairing_plot.add_series(identity, color=color, display_name=identity)
            position_plot.add_series(identity, color=color, display_name=identity)
            self._cross_pair_series_keys[(slave_camera_id, identity)] = identity
            self._cross_running_stats[(slave_camera_id, identity, "pairing_gap_us")] = RunningStats()
            self._cross_running_stats[(slave_camera_id, identity, "position_gap_ms")] = RunningStats()

        self._slave_sections[slave_camera_id] = {
            "pairing_plot": pairing_plot, "position_plot": position_plot, "stats_panel": stats_panel,
        }

        graphs_column = QVBoxLayout()
        graphs_column.addWidget(pairing_plot, stretch=1)
        graphs_column.addWidget(position_plot, stretch=1)

        middle_row = QHBoxLayout()
        middle_row.addLayout(graphs_column, stretch=1)
        middle_row.addWidget(stats_panel)
        section_layout.addLayout(middle_row)

        return section_widget
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_multi_camera_live_session_page.py -v`
Expected: FAIL only on tests Task 4 owns (`test_all_sessions_finished_writes_cross_camera_csv_and_plot` will fail on the old `cross_camera_sync_plot.png` filename - that's Task 6's fix; `_on_cross_pair_ready`/`_on_cross_stats_ready` still reference the removed `self.cross_plot` etc. and will raise `AttributeError` the moment a row is emitted - any test emitting `row_ready` will fail until Task 4 lands). All NEW tests from this task's Step 1, plus `test_set_cameras_with_two_cameras_builds_one_cross_series_per_shared_identity`, `test_set_cameras_with_one_camera_has_no_cross_series`, `test_cross_camera_tab_is_first`, `test_set_cameras_builds_one_tab_per_camera`, `test_set_cameras_tags_every_tab_with_its_role` must PASS now. This partial-red state is expected and resolved by Task 4.

- [ ] **Step 5: Commit**

```bash
git add gui/pages/multi_camera_live_session_page.py tests/gui/pages/test_multi_camera_live_session_page.py
git commit -m "feat: cross-camera section rebuilt as one graph-pair+stats-panel per slave"
```

---

### Task 4: `_on_cross_pair_ready`/`_on_cross_stats_ready` route per slave and accumulate stats

**Files:**
- Modify: `gui/pages/multi_camera_live_session_page.py:379-404` (`_on_cross_pair_ready`, `_on_cross_stats_ready`), new `_push_running_stats` method
- Test: `tests/gui/pages/test_multi_camera_live_session_page.py`

**Interfaces:**
- Consumes: `self._slave_sections`, `self._cross_pair_series_keys`, `self._cross_running_stats` (all from Task 3).
- Produces: `_on_cross_pair_ready` stays O(1) (bookkeeping + `RunningStats.update()` only); `_on_cross_stats_ready` pushes `add_point`/`set_value` to the correct slave's `pairing_plot`/`position_plot`/`stats_panel`, including the new `"pair_index"`, per-identity min/avg/std/max fields.

- [ ] **Step 1: Write the failing tests**

Add to `tests/gui/pages/test_multi_camera_live_session_page.py` (these replace the three removed in Task 3):

```python
def test_cross_pair_ready_does_not_plot_directly(qapp, tmp_path):
    # Efficiency fix: row_ready-cadence callbacks must stay O(1) (CLAUDE.md's
    # documented row_ready/stats_ready split) - add_point only happens on the
    # throttled stats_ready cadence, in _on_cross_stats_ready.
    page, fake_threads = _page_with_fake_threads()
    page.set_cameras(object(), _two_cameras(tmp_path))
    page.start_all_sessions()
    pairing_plot = page._slave_sections["cam2"]["pairing_plot"]

    fake_threads["SN1"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_000.0, "stream_b_ts_us": 1_000_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_010.0, "stream_b_ts_us": 1_000_010.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })

    assert pairing_plot.get_series_data("infrared1")[1] == []
    # 2, not 1: _camera_config's two cameras share BOTH "infrared1" and
    # "color" identities, so one row_ready from each camera legitimately
    # produces one cross_pair_ready per shared identity.
    assert len(page._cross_rows) == 2


def test_matching_rows_plot_a_cross_camera_hw_ts_point_on_stats_ready(qapp, tmp_path):
    page, fake_threads = _page_with_fake_threads()
    page.set_cameras(object(), _two_cameras(tmp_path))
    page.start_all_sessions()

    # First pair is the reconciler's own calibration pair - always 0.0.
    fake_threads["SN1"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_000.0, "stream_b_ts_us": 1_000_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_010.0, "stream_b_ts_us": 1_000_010.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    # Second pair, after calibration (offset learned: 10) - reports the
    # genuine residual (-5), not the raw absolute difference.
    fake_threads["SN1"].row_ready.emit({
        "pair_index": 2, "stream_a_ts_us": 1_100_000.0, "stream_b_ts_us": 1_100_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 2, "stream_a_ts_us": 1_100_015.0, "stream_b_ts_us": 1_100_015.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })

    fake_threads["SN1"].stats_ready.emit({"pair_index": 2})

    pairing_plot = page._slave_sections["cam2"]["pairing_plot"]
    _, ys = pairing_plot.get_series_data("infrared1")
    assert ys == [-5.0]


def test_matching_rows_plot_a_cross_camera_optical_sync_point_on_stats_ready(qapp, tmp_path):
    page, fake_threads = _page_with_fake_threads()
    page.set_cameras(object(), _two_cameras(tmp_path))
    page.start_all_sessions()

    fake_threads["SN1"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_000.0, "stream_b_ts_us": 1_000_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
        "stream_a_last_led": 0, "position_gap_ms_excluded": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_010.0, "stream_b_ts_us": 1_000_010.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
        "stream_a_last_led": 0, "position_gap_ms_excluded": False,
    })
    # Second pair: master detects LED 1, slave detects LED 0. _camera_config's
    # default num_leds=2, switch_time_ms=1.0 ->
    # compute_position_gap(1, 0, 2) == 1, * 1.0 == 1.0ms.
    fake_threads["SN1"].row_ready.emit({
        "pair_index": 2, "stream_a_ts_us": 1_100_000.0, "stream_b_ts_us": 1_100_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
        "stream_a_last_led": 1, "position_gap_ms_excluded": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 2, "stream_a_ts_us": 1_100_015.0, "stream_b_ts_us": 1_100_015.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
        "stream_a_last_led": 0, "position_gap_ms_excluded": False,
    })

    fake_threads["SN1"].stats_ready.emit({"pair_index": 2})

    position_plot = page._slave_sections["cam2"]["position_plot"]
    _, ys = position_plot.get_series_data("infrared1")
    assert ys == [1.0]


def test_cross_stats_panel_shows_latest_pair_index_and_running_stats(qapp, tmp_path):
    page, fake_threads = _page_with_fake_threads()
    page.set_cameras(object(), _two_cameras(tmp_path))
    page.start_all_sessions()

    fake_threads["SN1"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_000.0, "stream_b_ts_us": 1_000_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_010.0, "stream_b_ts_us": 1_000_010.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN1"].row_ready.emit({
        "pair_index": 2, "stream_a_ts_us": 1_100_000.0, "stream_b_ts_us": 1_100_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 2, "stream_a_ts_us": 1_100_015.0, "stream_b_ts_us": 1_100_015.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })

    fake_threads["SN1"].stats_ready.emit({"pair_index": 2})

    stats_panel = page._slave_sections["cam2"]["stats_panel"]
    # "pair_index" is the reconciler's own synthetic counter - by the second
    # stats_ready tick, both "infrared1" and "color" identities have each
    # produced 2 cross-rows (4 total across both identities), so the max
    # pair_index seen is 4 (the reconciler's _pair_counter increments once
    # per cross-row it builds, across every pair-spec it owns).
    assert stats_panel._value_labels["pair_index"].text() == "4"
    assert stats_panel._value_labels["infrared1_hw_ts_latency_min"].text() != "-"
    assert stats_panel._value_labels["infrared1_hw_ts_latency_avg"].text() != "-"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_multi_camera_live_session_page.py -k "cross_pair_ready_does_not_plot_directly or plot_a_cross_camera or shows_latest_pair_index" -v`
Expected: FAIL with `AttributeError: 'MultiCameraLiveSessionPage' object has no attribute 'cross_plot'` (current `_on_cross_pair_ready`/`_on_cross_stats_ready` still reference the removed single shared widgets).

- [ ] **Step 3: Implement**

Replace `_on_cross_pair_ready` and `_on_cross_stats_ready` (currently lines 379-404) with:

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
        position_stats = self._cross_running_stats.get(key + ("position_gap_ms",))
        if (position_stats is not None and cross_row.get("position_gap_ms") is not None
                and not cross_row.get("position_gap_ms_excluded")):
            position_stats.update(cross_row["position_gap_ms"])

    def _on_cross_stats_ready(self, latest_by_pair):
        rows_by_slave = {}
        for (slave_camera_id, identity), row in latest_by_pair.items():
            rows_by_slave.setdefault(slave_camera_id, []).append((identity, row))

        for slave_camera_id, identity_rows in rows_by_slave.items():
            section = self._slave_sections.get(slave_camera_id)
            if section is None:
                continue
            stats_panel = section["stats_panel"]
            pairing_plot = section["pairing_plot"]
            position_plot = section["position_plot"]

            # A slave sharing multiple identities can have each identity's
            # own match complete independently, landing different
            # pair_index values in the same tick - show the most recently
            # completed match across all of this slave's identities as the
            # single "is this still updating" heartbeat.
            stats_panel.set_value("pair_index", max(row["pair_index"] for _, row in identity_rows))

            for identity, row in identity_rows:
                series_key = self._cross_pair_series_keys.get((slave_camera_id, identity))
                if series_key is None:
                    continue

                stats_panel.set_value("{}_pairing_gap_us".format(identity), row["pairing_gap_us"])
                pairing_value = row["pairing_gap_us"]
                if row.get("pairing_gap_us_excluded"):
                    pairing_value = float("nan")
                pairing_plot.add_point(series_key, row["pair_index"], pairing_value)

                if row.get("position_gap_ms") is not None:
                    stats_panel.set_value("{}_position_gap_ms".format(identity), row["position_gap_ms"])
                    position_value = row["position_gap_ms"]
                    if row.get("position_gap_ms_excluded"):
                        position_value = float("nan")
                    position_plot.add_point(series_key, row["pair_index"], position_value)

                pairing_stats = self._cross_running_stats.get((slave_camera_id, identity, "pairing_gap_us"))
                if pairing_stats is not None:
                    self._push_running_stats(stats_panel, "{}_hw_ts_latency".format(identity), pairing_stats)
                position_stats = self._cross_running_stats.get((slave_camera_id, identity, "position_gap_ms"))
                if position_stats is not None:
                    self._push_running_stats(stats_panel, "{}_optical_sync".format(identity), position_stats)

    def _push_running_stats(self, stats_panel, key, stats):
        if stats.count == 0:
            return
        stats_panel.set_value("{}_min".format(key), round(stats.min, 1))
        stats_panel.set_value("{}_avg".format(key), round(stats.mean, 1))
        stats_panel.set_value("{}_std".format(key), round(stats.std, 1))
        stats_panel.set_value("{}_max".format(key), round(stats.max, 1))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_multi_camera_live_session_page.py -v`
Expected: PASS on every test except `test_all_sessions_finished_writes_cross_camera_csv_and_plot` (still fails on the old `cross_camera_sync_plot.png` filename - fixed in Task 6).

- [ ] **Step 5: Commit**

```bash
git add gui/pages/multi_camera_live_session_page.py tests/gui/pages/test_multi_camera_live_session_page.py
git commit -m "feat: cross-camera stats/plots route per slave with full live-data parity"
```

---

### Task 5: Static plot export becomes one figure per slave

**Files:**
- Modify: `domain/plot_export.py:137-187` (`_build_cross_camera_figure`, `export_cross_camera_plot`)
- Test: `tests/domain/test_plot_export.py`

**Interfaces:**
- Consumes: `_to_plot_value`, `_style_axis`, `_figure_width`, `_FIGURE_HEIGHT`, `SURFACE`, `MUTED_TEXT`, `CROSS_CAMERA_COLORS` (all pre-existing, unchanged).
- Produces: `_build_cross_camera_figure(cross_rows, title)` — now takes a required `title` param, groups internally by `stream_identity` alone (caller must pre-filter `cross_rows` to one slave). `export_cross_camera_plot(cross_rows, path, title)` — same new required param. Task 6 is the only caller and supplies pre-filtered rows + a title per slave.

- [ ] **Step 1: Write the failing tests**

Update `tests/domain/test_plot_export.py`'s existing cross-camera tests (lines 91-134) to pass `title` and to filter rows to one slave before calling, matching the new per-slave contract. Replace lines 91-134 with:

```python
def test_export_cross_camera_plot_writes_a_file(tmp_path):
    rows = [_cross_row(0), _cross_row(1, pairing_gap_us=-12.0)]
    path = str(tmp_path / "cross_camera_sync_plot_slave1.png")

    export_cross_camera_plot(rows, path, title="Slave 1: D455 B (SN 1)  vs.  Master: D455 A (SN 0)")

    assert os.path.exists(path)
    assert os.path.getsize(path) > 0


def test_export_cross_camera_plot_handles_empty_rows(tmp_path):
    path = str(tmp_path / "cross_camera_sync_plot_slave1.png")

    export_cross_camera_plot([], path, title="Slave 1")

    assert os.path.exists(path)


def test_export_cross_camera_plot_draws_one_line_per_identity():
    # Rows are pre-filtered to ONE slave by the caller (gui/pages/
    # multi_camera_live_session_page.py) - a single figure can still have
    # multiple lines if that one slave shares multiple stream identities
    # with master.
    rows = [
        _cross_row(0, stream_identity="infrared1", pairing_gap_us=-10.0),
        _cross_row(1, stream_identity="infrared1", pairing_gap_us=-11.0),
        _cross_row(0, stream_identity="color", pairing_gap_us=5.0),
    ]

    import matplotlib.pyplot as plt
    from domain.plot_export import _build_cross_camera_figure
    fig = _build_cross_camera_figure(rows, title="Slave 1")
    lines = fig.axes[0].get_lines()

    assert len(lines) == 2
    plt.close(fig)


def test_export_cross_camera_plot_nans_out_excluded_values():
    rows = [_cross_row(0, pairing_gap_us=99999.0, excluded=True)]

    import matplotlib.pyplot as plt
    from domain.plot_export import _build_cross_camera_figure
    fig = _build_cross_camera_figure(rows, title="Slave 1")
    line = fig.axes[0].get_lines()[0]

    assert math.isnan(line.get_ydata()[0])
    plt.close(fig)


def test_export_cross_camera_plot_draws_position_gap_on_second_axis():
    rows = [
        _cross_row(0, stream_identity="infrared1"),
        _cross_row(1, stream_identity="infrared1"),
        _cross_row(0, stream_identity="color"),
    ]

    import matplotlib.pyplot as plt
    from domain.plot_export import _build_cross_camera_figure
    fig = _build_cross_camera_figure(rows, title="Slave 1")
    lines = fig.axes[1].get_lines()

    assert len(lines) == 2
    plt.close(fig)


def test_export_cross_camera_plot_nans_out_excluded_position_gap_values():
    rows = [_cross_row(0, position_gap_ms=99.0, position_gap_ms_excluded=True)]

    import matplotlib.pyplot as plt
    from domain.plot_export import _build_cross_camera_figure
    fig = _build_cross_camera_figure(rows, title="Slave 1")
    line = fig.axes[1].get_lines()[0]

    assert math.isnan(line.get_ydata()[0])
    plt.close(fig)


def test_export_cross_camera_plot_sets_the_given_title():
    rows = [_cross_row(0)]

    import matplotlib.pyplot as plt
    from domain.plot_export import _build_cross_camera_figure
    fig = _build_cross_camera_figure(rows, title="Slave 1: D455 B  vs.  Master: D455 A")

    assert fig._suptitle.get_text() == "Slave 1: D455 B  vs.  Master: D455 A"
    plt.close(fig)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/domain/test_plot_export.py -k cross_camera -v`
Expected: FAIL with `TypeError: export_cross_camera_plot() missing 1 required positional argument: 'title'` / `TypeError: _build_cross_camera_figure() missing 1 required positional argument: 'title'`.

- [ ] **Step 3: Implement**

Replace `_build_cross_camera_figure`/`export_cross_camera_plot` in `domain/plot_export.py` (currently lines 137-187):

```python
def _build_cross_camera_figure(cross_rows, title):
    """Two stacked subplots (sharing one x-axis, "Pair index") - HW TS
    Latency and Optical Sync each get their own y-axis, same "wildly
    different scales" reasoning _build_figure's own 3-axis split already
    uses for the intra-camera plot. One line per stream identity - the
    caller (gui/pages/multi_camera_live_session_page.py) pre-filters
    cross_rows to a single slave camera before calling, since this export
    is now one figure per slave (see that page's own per-slave cross-camera
    section); a single slave can still produce multiple lines here if it
    shares more than one stream identity with master. engine.
    cross_camera_reconciler's own pair_index is a synthetic, shared-
    across-all-pairs counter (not comparable to any one camera's own
    pair_index), so it's used here only as this plot's own x-axis, not
    cross-referenced against per-camera CSVs. Split out from
    export_cross_camera_plot so tests can inspect the plotted line data
    directly, same reason _build_figure is split from export_session_plot."""
    groups = {}
    for row in cross_rows:
        key = row["stream_identity"]
        groups.setdefault(key, []).append(row)

    fig, (pairing_ax, position_ax) = plt.subplots(
        2, 1, figsize=(_figure_width(len(cross_rows)), _FIGURE_HEIGHT), sharex=True,
    )
    fig.patch.set_facecolor(SURFACE)
    fig.suptitle(title, color=MUTED_TEXT)

    for index, identity in enumerate(sorted(groups.keys())):
        pair_rows = groups[identity]
        pair_indices = [row["pair_index"] for row in pair_rows]
        color = CROSS_CAMERA_COLORS[index % len(CROSS_CAMERA_COLORS)]

        pairing_values = [_to_plot_value(row.get("pairing_gap_us"), row.get("pairing_gap_us_excluded"))
                           for row in pair_rows]
        pairing_ax.plot(pair_indices, pairing_values, label=identity, color=color)

        position_values = [_to_plot_value(row.get("position_gap_ms"), row.get("position_gap_ms_excluded"))
                            for row in pair_rows]
        position_ax.plot(pair_indices, position_values, label=identity, color=color)

    pairing_ax.set_ylabel("HW TS Latency (us)")
    _style_axis(pairing_ax)

    position_ax.set_ylabel("Optical Sync (ms)")
    position_ax.set_xlabel("Pair index")
    _style_axis(position_ax)

    fig.tight_layout()
    return fig


def export_cross_camera_plot(cross_rows, path, title):
    fig = _build_cross_camera_figure(cross_rows, title)
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/domain/test_plot_export.py -v`
Expected: PASS (all tests in the file). Do NOT run the full suite as this task's verification step: `export_cross_camera_plot`'s new required `title` parameter means `gui/pages/multi_camera_live_session_page.py`'s `_on_all_sessions_finished` (still calling it with the old 2-argument form until Task 6 lands) will raise `TypeError: export_cross_camera_plot() missing 1 required positional argument: 'title'` the moment any test drives a 2-camera run to completion - e.g. `tests/gui/pages/test_multi_camera_live_session_page.py::test_all_sessions_finished_writes_cross_camera_csv_and_plot`. This is expected, interim breakage, not a regression in this task's own code - Task 6 updates that call site.

- [ ] **Step 5: Commit**

```bash
git add domain/plot_export.py tests/domain/test_plot_export.py
git commit -m "feat: cross-camera plot export becomes one figure per slave"
```

---

### Task 6: `_on_all_sessions_finished` writes one plot per slave

**Files:**
- Modify: `gui/pages/multi_camera_live_session_page.py:406-418` (`_on_all_sessions_finished`)
- Test: `tests/gui/pages/test_multi_camera_live_session_page.py`

**Interfaces:**
- Consumes: `_camera_roles` (Task 1); `export_cross_camera_plot(cross_rows, path, title)` (Task 5, new required `title` param).
- Produces: one `cross_camera_sync_plot_slave1.png`, `cross_camera_sync_plot_slave2.png`, ... per slave, instead of one combined `cross_camera_sync_plot.png`. `cross_camera_sync.csv` stays a single combined file, unchanged.

- [ ] **Step 1: Write the failing test**

Update `test_all_sessions_finished_writes_cross_camera_csv_and_plot` in `tests/gui/pages/test_multi_camera_live_session_page.py` (currently lines 200-223):

```python
def test_all_sessions_finished_writes_cross_camera_csv_and_one_plot_per_slave(qapp, tmp_path):
    import os
    page, fake_threads = _page_with_fake_threads()
    page.set_cameras(object(), _two_cameras(tmp_path))
    page.start_all_sessions()

    fake_threads["SN1"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_000.0, "stream_b_ts_us": 1_000_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_010.0, "stream_b_ts_us": 1_000_010.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN1"].session_finished.emit([])
    fake_threads["SN1"].finished.emit()
    fake_threads["SN2"].session_finished.emit([])
    fake_threads["SN2"].finished.emit()

    csv_path = os.path.join(page._run_dir, "cross_camera_sync.csv")
    plot_path = os.path.join(page._run_dir, "cross_camera_sync_plot_slave1.png")
    assert os.path.exists(csv_path)
    assert os.path.exists(plot_path)
    assert os.path.getsize(csv_path) > 0
    assert os.path.getsize(plot_path) > 0


def test_all_sessions_finished_writes_a_separate_plot_per_slave_with_three_cameras(qapp, tmp_path):
    import os
    page, fake_threads = _page_with_fake_threads()
    cameras = _two_cameras(tmp_path)
    cameras.append({"camera_id": "cam3", "label": "D455 C", "is_master": False,
                     "config": _camera_config(tmp_path, device_serial="SN3")})
    page.set_cameras(object(), cameras)
    page.start_all_sessions()

    fake_threads["SN1"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_000.0, "stream_b_ts_us": 1_000_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_010.0, "stream_b_ts_us": 1_000_010.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN3"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_020.0, "stream_b_ts_us": 1_000_020.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    for serial in ("SN1", "SN2", "SN3"):
        fake_threads[serial].session_finished.emit([])
        fake_threads[serial].finished.emit()

    assert os.path.exists(os.path.join(page._run_dir, "cross_camera_sync_plot_slave1.png"))
    assert os.path.exists(os.path.join(page._run_dir, "cross_camera_sync_plot_slave2.png"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_multi_camera_live_session_page.py -k "one_plot_per_slave or separate_plot_per_slave" -v`
Expected: FAIL on both new tests with `TypeError: export_cross_camera_plot() missing 1 required positional argument: 'title'` - Task 5 already landed and changed that function's signature, but `_on_all_sessions_finished` still calls it with the old 2-argument form until this task's own Step 3 fixes the call site.

- [ ] **Step 3: Implement**

Replace `_on_all_sessions_finished` (currently lines 406-418):

```python
    def _on_all_sessions_finished(self, rows_by_camera):
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.duration_spinbox.setEnabled(True)
        self.frame_sample_interval_spinbox.setEnabled(True)

        # Only when a cross-camera comparison actually exists (>=2 cameras,
        # >=1 shared stream identity) - with a single camera there's no
        # cross-camera concept at all, and writing an empty-but-valid
        # cross_camera_sync.csv would just be confusing clutter.
        if self._cross_pair_series_keys:
            export_cross_camera_csv(self._cross_rows, os.path.join(self._run_dir, "cross_camera_sync.csv"))

            roles = _camera_roles(self._cameras)
            master_camera = next(c for c in self._cameras if c["is_master"])
            master_display = roles[master_camera["camera_id"]]["display"]

            slave_ids = sorted({row["slave_camera_id"] for row in self._cross_rows})
            for slave_camera_id in slave_ids:
                slave_role = roles[slave_camera_id]
                rows_for_slave = [row for row in self._cross_rows if row["slave_camera_id"] == slave_camera_id]
                title = "{}: {}  vs.  Master: {}".format(
                    slave_role["tag"].title(), slave_role["display"], master_display
                )
                path = os.path.join(
                    self._run_dir, "cross_camera_sync_plot_{}.png".format(slave_role["slug"])
                )
                export_cross_camera_plot(rows_for_slave, path, title)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_multi_camera_live_session_page.py -v`
Expected: PASS (all tests in the file).

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python.exe -m pytest -v`
Expected: PASS (every test in the project).

- [ ] **Step 6: Commit**

```bash
git add gui/pages/multi_camera_live_session_page.py tests/gui/pages/test_multi_camera_live_session_page.py
git commit -m "feat: static cross-camera export writes one plot per slave"
```

---

## Self-Review

**Spec coverage:**
- Section 1 (role & label computation) - Task 1.
- Section 2 (per-camera tab labeling) - Task 2.
- Section 3 (cross-camera section: one graph pair + one stats panel per slave, inner tabs at 2+ slaves, identity-only line naming) - Task 3.
- Section 4 (static plot export: one PNG per slave) - Task 5 (the export function itself) + Task 6 (the per-slave calling loop).
- Section 5 (no changes: CSV export, engine layer, run architecture) - confirmed unchanged throughout; no task touches `engine/cross_camera_reconciler.py`, `engine/multi_camera_session.py`, or `domain/csv_export.py`.
- Section 6 (live-data parity: pair index, LED switch time, per-identity value fields, per-identity min/avg/std/max) - Task 3 (field registration) + Task 4 (accumulation and pushing).
- Debug-image snapshots explicitly out of scope per the spec - no task attempts them.

**Placeholder scan:** No TBD/TODO/"add appropriate"/"similar to Task N" phrases - every step has real, complete code.

**Type consistency:** `_camera_roles` return shape (`{"tag", "slug", "display"}`) is used identically in Tasks 2, 3, and 6. `self._slave_sections`/`self._cross_pair_series_keys`/`self._cross_running_stats` names and shapes introduced in Task 3 are consumed with matching names/shapes in Task 4. `_build_cross_camera_figure`/`export_cross_camera_plot`'s new `title` parameter name and position match between Task 5's definition and Task 6's call site. Stats-panel field keys (`"pair_index"`, `"switch_time_ms"`, `"{identity}_pairing_gap_us"`, `"{identity}_position_gap_ms"`, `"{identity}_hw_ts_latency"`/`"{identity}_optical_sync"` stats-table keys) are registered in Task 3 and pushed to with the identical keys in Task 4.
