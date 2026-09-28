# GMSL TSC Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On the Orin with 2x D585 over GMSL, put both cameras into kernel external-sync mode (`camera_sync_mode=2` via `v4l2-ctl`, read-back confirmed) and run the `/dev/cdi_tsc` trigger around a multi-camera live session, gated by auto-detect + an operator checkbox on Camera Hub.

**Architecture:** A new pure-core module `engine/gmsl_sync.py` (injectable `run_v4l2`/`tsc_io`/`sleep`/`glob_fn`) owns detection, V4L2 node resolution, mode write/restore, and a `GmslTscSync` engage/disengage object. `MultiCameraSessionController` gains an optional `gmsl_sync` collaborator engaged after the genlock step and disengaged after every thread finishes. `CameraHubPage` gets the checkbox; `MainWindow` runs detection on hub refresh and builds the config at Start. The user's `ext_sync_gen.py` is vendored unchanged under `tools/tsc_trigger/` and imported lazily.

**Tech Stack:** Python 3.10+, PySide6, pyrealsense2, pytest; `v4l2-ctl` (v4l-utils) and `/dev/cdi_tsc` on the Orin only.

**Spec:** `docs/superpowers/specs/2026-09-28-gmsl-tsc-sync-design.md`

## Global Constraints

- Nothing changes when the checkbox is unticked/hidden, when `panel_connection.mode` is `local`, or on the 1-camera path - existing tests must keep passing unmodified.
- `engine/gmsl_sync.py` must import on Windows: never import `fcntl` or `tools.tsc_trigger.ext_sync_gen` at module level.
- `tools/tsc_trigger/ext_sync_gen.py` is a byte-for-byte copy of `C:\Users\gbaruch\scripts\twe camera pair\ext_sync_gen.py`.
- Kernel control/value defaults: `control: camera_sync_mode`, `sync_mode_value: 2`, `duty_percent: 50`, `settle_s: 5.0`. Trigger fps = the streams' shared fps (not a setting).
- Detection = ALL of: `panel_connection.mode == "remote"`, exactly 2 cameras, both names contain `"D585"`, neither supports `rs.camera_info.usb_type_descriptor`, `/dev/cdi_tsc` exists. Any lookup exception -> False.
- `engage()` failures raise `RuntimeError` after undoing whatever was applied; `disengage()` never raises and is idempotent.
- Permission error on `/dev/cdi_tsc` message must include `KERNEL=="cdi_tsc", MODE="0666"`.
- Run tests with `.venv\Scripts\python.exe -m pytest ...` from the repo root (PowerShell). Widget tests use the `qapp` fixture.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. `v4l2-ctl` not installed on the Orin -> Start must fail with the "install v4l-utils" message, not a traceback or a silent unsynced run. (Task 1: `test_resolve_sync_nodes_reports_missing_v4l2_ctl`)
2. `-C` output in the menu-label form `camera_sync_mode: 2 (External Sync)` must parse as 2, or every run reports a false readback mismatch. (Task 1: `test_parse_control_value_handles_menu_label`)
3. As-found value unreadable on one node -> restore must skip that node, not write `None`. (Task 2: `test_restore_skips_unreadable_as_found`)
4. Operator unticks the checkbox, then edits a camera (hub refresh) -> must stay unticked. (Task 6: `test_gmsl_checkbox_untick_survives_refresh`)
5. A camera thread factory raising after the trigger was started must not leave the TSC running. (Task 4: `test_start_all_disengages_gmsl_if_thread_start_raises`)

---

## File Structure

- Create `tools/tsc_trigger/__init__.py`, `tools/tsc_trigger/ext_sync_gen.py` (vendored).
- Create `engine/gmsl_sync.py` - detection, V4L2 helpers, `KernelTscIO`, `GmslTscSync`, `stop_tsc_best_effort`, `DEFAULT_GMSL_TSC_SYNC`.
- Create `tests/engine/test_gmsl_sync.py`.
- Modify `engine/multi_camera_session.py` - `gmsl_sync` kwarg, engage/disengage.
- Modify `tests/engine/test_multi_camera_session.py`.
- Modify `gui/pages/multi_camera_live_session_page.py` - `gmsl_tsc_sync` in `set_cameras`, `gmsl_sync_factory`.
- Modify `tests/gui/pages/test_multi_camera_live_session_page.py`.
- Modify `gui/pages/camera_hub_page.py` - checkbox.
- Modify `tests/gui/pages/test_camera_hub_page.py`.
- Modify `gui/main_window.py` - availability on refresh, Start branch.
- Modify `tests/gui/test_main_window.py`.
- Modify `settings.yaml`, `main.py`, `CLAUDE.md`.

---

### Task 1: V4L2 parsing helpers and node resolution

**Files:**
- Create: `engine/gmsl_sync.py`
- Test: `tests/engine/test_gmsl_sync.py`

**Interfaces:**
- Produces:
  - `run_v4l2(node: str, *args: str, timeout: float = 15) -> tuple[int, str, str]` (never raises; 127 missing binary, 124 timeout)
  - `controls_in(text: str) -> list[str]`
  - `parse_control_value(text: str, name: str) -> int | None`
  - `control_range(text: str, name: str) -> tuple[int, int] | None`
  - `resolve_sync_nodes(control: str, run_v4l2=run_v4l2, glob_fn=glob.glob) -> list[str]` (exactly 2 or `RuntimeError`)

- [ ] **Step 1: Write the failing tests**

Create `tests/engine/test_gmsl_sync.py`:

```python
"""engine.gmsl_sync's pure logic, tested against fake v4l2-ctl / TSC /
device collaborators - never real /dev nodes or ioctls (hardware-only,
same convention as engine/led_panel.py)."""

from unittest.mock import MagicMock

import pytest

from engine import gmsl_sync


# Real `v4l2-ctl -L` shape on the D585 GMSL driver (from
# check_d585_sync_v4l2.py's own docstring).
L_WITH_SYNC = (
    "User Controls\n"
    "\n"
    "      laser_power_on_off 0x009a4001 (bool)   : default=1 value=1 flags=volatile, execute-on-write\n"
    "        camera_sync_mode 0x009a4010 (menu)   : min=0 max=2 default=0 value=0\n"
    "                                0: Default\n"
    "                                1: Master\n"
    "                                2: External Sync\n"
)
L_WITHOUT_SYNC = "      exposure_absolute 0x009a0902 (int)    : min=1 max=200000 step=1 default=33 value=33\n"


def test_controls_in_ignores_menu_value_lines():
    assert gmsl_sync.controls_in(L_WITH_SYNC) == ["laser_power_on_off", "camera_sync_mode"]


def test_parse_control_value_handles_menu_label():
    assert gmsl_sync.parse_control_value("camera_sync_mode: 2 (External Sync)", "camera_sync_mode") == 2


def test_parse_control_value_plain_and_unparseable():
    assert gmsl_sync.parse_control_value("camera_sync_mode: 0", "camera_sync_mode") == 0
    assert gmsl_sync.parse_control_value("garbage", "camera_sync_mode") is None


def test_control_range_reads_min_max():
    assert gmsl_sync.control_range(L_WITH_SYNC, "camera_sync_mode") == (0, 2)
    assert gmsl_sync.control_range(L_WITHOUT_SYNC, "camera_sync_mode") is None


def _fake_v4l2(listing_by_node):
    def run(node, *args, timeout=15):
        if args == ("-L",):
            if node not in listing_by_node:
                return 1, "", "no such device"
            return 0, listing_by_node[node], ""
        raise AssertionError("unexpected v4l2-ctl call {} {}".format(node, args))
    return run


def _fake_glob(mapping):
    return lambda pattern: list(mapping.get(pattern, []))


def test_resolve_sync_nodes_uses_udev_symlinks_and_skips_metadata_nodes():
    listing = {"/dev/video-rs-depth-0": L_WITH_SYNC, "/dev/video-rs-depth-1": L_WITH_SYNC}
    glob_fn = _fake_glob({"/dev/video-rs-*": [
        "/dev/video-rs-depth-0", "/dev/video-rs-depth-md-0",
        "/dev/video-rs-depth-1", "/dev/video-rs-depth-md-1",
    ]})
    run = MagicMock(side_effect=_fake_v4l2(listing))

    nodes = gmsl_sync.resolve_sync_nodes("camera_sync_mode", run_v4l2=run, glob_fn=glob_fn)

    assert nodes == ["/dev/video-rs-depth-0", "/dev/video-rs-depth-1"]
    probed = {call.args[0] for call in run.call_args_list}
    assert not any("-md-" in node for node in probed)


def test_resolve_sync_nodes_falls_back_to_full_video_scan():
    listing = {"/dev/video0": L_WITHOUT_SYNC, "/dev/video2": L_WITH_SYNC,
               "/dev/video10": L_WITH_SYNC, "/dev/video-rs-color-0": L_WITHOUT_SYNC}
    glob_fn = _fake_glob({
        "/dev/video-rs-*": ["/dev/video-rs-color-0"],
        "/dev/video[0-9]*": ["/dev/video10", "/dev/video0", "/dev/video2"],
    })

    nodes = gmsl_sync.resolve_sync_nodes("camera_sync_mode", run_v4l2=_fake_v4l2(listing), glob_fn=glob_fn)

    assert nodes == ["/dev/video2", "/dev/video10"]  # numeric /dev order, not string order


def test_resolve_sync_nodes_raises_when_not_exactly_two():
    listing = {"/dev/video0": L_WITH_SYNC}
    glob_fn = _fake_glob({"/dev/video-rs-*": [], "/dev/video[0-9]*": ["/dev/video0"]})

    with pytest.raises(RuntimeError, match="expected 2"):
        gmsl_sync.resolve_sync_nodes("camera_sync_mode", run_v4l2=_fake_v4l2(listing), glob_fn=glob_fn)


def test_resolve_sync_nodes_reports_missing_v4l2_ctl():
    run = lambda node, *args, timeout=15: (127, "", "v4l2-ctl not found on PATH. Install it with 'sudo apt install v4l-utils'.")
    glob_fn = _fake_glob({"/dev/video-rs-*": ["/dev/video-rs-depth-0"], "/dev/video[0-9]*": ["/dev/video0"]})

    with pytest.raises(RuntimeError, match="v4l-utils"):
        gmsl_sync.resolve_sync_nodes("camera_sync_mode", run_v4l2=run, glob_fn=glob_fn)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_gmsl_sync.py -v`
Expected: FAIL - `ImportError: cannot import name 'gmsl_sync'`

- [ ] **Step 3: Implement**

Create `engine/gmsl_sync.py`:

```python
"""Hardware sync for two D585 cameras on the Orin's GMSL deserializer:
kernel external-sync mode via v4l2-ctl plus the Orin's TSC signal
generator (/dev/cdi_tsc). See docs/superpowers/specs/2026-09-28-gmsl-tsc-
sync-design.md and CLAUDE.md's "GMSL TSC sync" section.

Pure core with injectable collaborators (run_v4l2, tsc_io, sleep,
glob_fn) - same testability convention as MultiCameraSessionController's
sync_setter/device_lookup. Must import on Windows: fcntl (via the vendored
tools/tsc_trigger/ext_sync_gen.py) is only ever imported lazily, inside
KernelTscIO.

Why V4L2 and not the SDK's inter_cam_sync_mode: on this D585 prototype
firmware the SDK write "succeeds" silently and its readback throws, so the
mode can never be confirmed. The kernel control has a working readback.
Its enum is NOT the SDK's: kernel 0=Default, 1=Master, 2=External Sync
(SDK d500_intercam_sync_mode numbers these differently)."""

import glob
import re
import subprocess

V4L2_CTL = "v4l2-ctl"

# A control line in `v4l2-ctl -L`:
#     camera_sync_mode 0x009a4010 (menu)   : min=0 max=2 default=0 value=0
# Anchored on the 'name 0xID' shape so a menu's indented value lines
# ('2: External Sync') are never read as controls.
_CONTROL_LINE_RE = re.compile(r"^\s*([a-zA-Z0-9_]+)\s+0x[0-9a-fA-F]+")
_RANGE_RE = re.compile(r"\bmin=(-?\d+)\s+max=(-?\d+)")
# librealsense udev creates a -md- METADATA node next to every stream node;
# it matches the same glob but exposes no controls.
_METADATA_MARKER = "-md-"


def run_v4l2(node, *args, timeout=15):
    """One v4l2-ctl call -> (returncode, stdout, stderr). Never raises."""
    command = [V4L2_CTL, "-d", node] + list(args)
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "", "v4l2-ctl not found on PATH. Install it with 'sudo apt install v4l-utils'."
    except subprocess.TimeoutExpired:
        return 124, "", "v4l2-ctl timed out after {}s".format(timeout)
    return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()


def controls_in(text):
    return [m.group(1) for m in (_CONTROL_LINE_RE.match(line) for line in text.splitlines()) if m]


def _control_line(text, name):
    for line in text.splitlines():
        match = _CONTROL_LINE_RE.match(line)
        if match and match.group(1) == name:
            return line.strip()
    return None


def parse_control_value(text, name):
    """`v4l2-ctl -C` on a menu control prints 'camera_sync_mode: 2 (External
    Sync)' - anchored on the control's name so the trailing label is
    tolerated."""
    match = re.search(r"^\s*" + re.escape(name) + r"\s*:\s*(-?\d+)", text, re.MULTILINE)
    return int(match.group(1)) if match else None


def control_range(text, name):
    line = _control_line(text, name)
    if line is None:
        return None
    match = _RANGE_RE.search(line)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _trailing_index(path):
    match = re.search(r"(\d+)$", path)
    return int(match.group(1)) if match else 1 << 30


def _nodes_with_control(candidates, control, run_v4l2, errors):
    found = []
    for node in candidates:
        code, out, err = run_v4l2(node, "-L")
        if code != 0:
            errors.append("{}: {}".format(node, err or "v4l2-ctl -L exited {}".format(code)))
            continue
        if control in controls_in(out):
            found.append(node)
    return found


def resolve_sync_nodes(control, run_v4l2=run_v4l2, glob_fn=glob.glob):
    """The two /dev nodes carrying `control`: librealsense udev symlinks
    first (metadata nodes excluded), then every /dev/videoN. Exactly 2 or
    RuntimeError. Assigned in /dev order - which node is which camera does
    not matter, since both cameras get the SAME value on D500."""
    errors = []
    symlinks = sorted((n for n in glob_fn("/dev/video-rs-*") if _METADATA_MARKER not in n),
                      key=_trailing_index)
    found = _nodes_with_control(symlinks, control, run_v4l2, errors)
    if len(found) != 2:
        scanned = sorted(glob_fn("/dev/video[0-9]*"), key=_trailing_index)
        found = _nodes_with_control(scanned, control, run_v4l2, errors)
    if len(found) != 2:
        raise RuntimeError(
            "Cannot place V4L2 control {!r}: {} node(s) expose it ({}), expected 2.{}".format(
                control, len(found), ", ".join(found) or "none",
                ("\n" + "\n".join(errors)) if errors else "",
            )
        )
    return found
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_gmsl_sync.py -v`
Expected: all 9 PASS

- [ ] **Step 5: Commit**

```bash
git add engine/gmsl_sync.py tests/engine/test_gmsl_sync.py
git commit -m "feat: add V4L2 sync-control node resolution for GMSL cameras

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Apply and restore the kernel sync mode

**Files:**
- Modify: `engine/gmsl_sync.py`
- Test: `tests/engine/test_gmsl_sync.py`

**Interfaces:**
- Consumes: `run_v4l2`, `parse_control_value`, `control_range` (Task 1)
- Produces:
  - `apply_sync_mode(nodes: list[str], control: str, value: int, run_v4l2=run_v4l2) -> dict[str, int | None]` (as-found per node; `RuntimeError` on range/write/readback failure, after restoring already-written nodes)
  - `restore_sync_mode(as_found: dict[str, int | None], control: str, run_v4l2=run_v4l2) -> None` (never raises; skips `None`)

- [ ] **Step 1: Write the failing tests**

Append to `tests/engine/test_gmsl_sync.py`:

```python
class _FakeKernel:
    """Per-node control values, answering -L/-C/-c like v4l2-ctl."""

    def __init__(self, values, max_value=2, fail_write_on=(), ignore_write_on=(), unreadable=()):
        self.values = dict(values)
        self.max_value = max_value
        self.fail_write_on = set(fail_write_on)
        self.ignore_write_on = set(ignore_write_on)
        self.unreadable = set(unreadable)
        self.writes = []

    def __call__(self, node, *args, timeout=15):
        if args == ("-L",):
            return 0, "camera_sync_mode 0x009a4010 (menu) : min=0 max={} default=0 value={}".format(
                self.max_value, self.values[node]), ""
        if args == ("-C", "camera_sync_mode"):
            if node in self.unreadable:
                return 1, "", "read failed"
            return 0, "camera_sync_mode: {} (label)".format(self.values[node]), ""
        if len(args) == 2 and args[0] == "-c":
            value = int(args[1].split("=")[1])
            self.writes.append((node, value))
            if node in self.fail_write_on:
                return 1, "", "VIDIOC_S_EXT_CTRLS: failed: Invalid argument"
            if node not in self.ignore_write_on:
                self.values[node] = value
            return 0, "", ""
        raise AssertionError(args)


NODES = ["/dev/video2", "/dev/video10"]


def test_apply_sync_mode_writes_confirms_and_returns_as_found():
    kernel = _FakeKernel({"/dev/video2": 0, "/dev/video10": 1})

    as_found = gmsl_sync.apply_sync_mode(NODES, "camera_sync_mode", 2, run_v4l2=kernel)

    assert as_found == {"/dev/video2": 0, "/dev/video10": 1}
    assert kernel.values == {"/dev/video2": 2, "/dev/video10": 2}


def test_apply_sync_mode_out_of_range_writes_nothing():
    kernel = _FakeKernel({"/dev/video2": 0, "/dev/video10": 0}, max_value=2)

    with pytest.raises(RuntimeError, match="0..2"):
        gmsl_sync.apply_sync_mode(NODES, "camera_sync_mode", 3, run_v4l2=kernel)
    assert kernel.writes == []


def test_apply_sync_mode_write_failure_restores_earlier_node():
    kernel = _FakeKernel({"/dev/video2": 0, "/dev/video10": 0}, fail_write_on={"/dev/video10"})

    with pytest.raises(RuntimeError, match="/dev/video10"):
        gmsl_sync.apply_sync_mode(NODES, "camera_sync_mode", 2, run_v4l2=kernel)
    assert kernel.values["/dev/video2"] == 0  # restored


def test_apply_sync_mode_readback_mismatch_raises_and_restores():
    kernel = _FakeKernel({"/dev/video2": 0, "/dev/video10": 0}, ignore_write_on={"/dev/video10"})

    with pytest.raises(RuntimeError, match="readback"):
        gmsl_sync.apply_sync_mode(NODES, "camera_sync_mode", 2, run_v4l2=kernel)
    assert kernel.values == {"/dev/video2": 0, "/dev/video10": 0}


def test_restore_writes_as_found_values():
    kernel = _FakeKernel({"/dev/video2": 2, "/dev/video10": 2})

    gmsl_sync.restore_sync_mode({"/dev/video2": 0, "/dev/video10": 1}, "camera_sync_mode", run_v4l2=kernel)

    assert kernel.values == {"/dev/video2": 0, "/dev/video10": 1}


def test_restore_skips_unreadable_as_found():
    kernel = _FakeKernel({"/dev/video2": 2, "/dev/video10": 2})

    gmsl_sync.restore_sync_mode({"/dev/video2": None, "/dev/video10": 0}, "camera_sync_mode", run_v4l2=kernel)

    assert kernel.writes == [("/dev/video10", 0)]


def test_restore_never_raises():
    def exploding(node, *args, timeout=15):
        raise OSError("gone")

    gmsl_sync.restore_sync_mode({"/dev/video2": 0}, "camera_sync_mode", run_v4l2=exploding)  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_gmsl_sync.py -v -k "apply or restore"`
Expected: FAIL - `AttributeError: module 'engine.gmsl_sync' has no attribute 'apply_sync_mode'`

- [ ] **Step 3: Implement**

Append to `engine/gmsl_sync.py`:

```python
def _read_value(node, control, run_v4l2):
    code, out, err = run_v4l2(node, "-C", control)
    if code != 0:
        return None
    return parse_control_value(out, control)


def restore_sync_mode(as_found, control, run_v4l2=run_v4l2):
    """Best-effort: writes each node's as-found value back. A node whose
    as-found value was unreadable (None) is skipped - there is nothing
    known to restore it to. Never raises (same convention as
    MultiCameraSessionController._reset_genlock_roles)."""
    for node, value in as_found.items():
        if value is None:
            continue
        try:
            run_v4l2(node, "-c", "{}={}".format(control, value))
        except Exception:
            continue


def apply_sync_mode(nodes, control, value, run_v4l2=run_v4l2):
    """Reads each node's as-found value, range-checks `value` against the
    driver's own min/max BEFORE writing anything, then writes and reads
    back each node. Any write failure or readback mismatch restores every
    node already written and raises RuntimeError. Returns
    {node: as_found_value_or_None}."""
    as_found = {}
    for node in nodes:
        code, listing, err = run_v4l2(node, "-L")
        limits = control_range(listing, control) if code == 0 else None
        if limits is not None and not limits[0] <= value <= limits[1]:
            raise RuntimeError("{} on {} accepts {}..{}, so {} cannot be written".format(
                control, node, limits[0], limits[1], value))
        as_found[node] = _read_value(node, control, run_v4l2)

    written = {}
    for node in nodes:
        code, out, err = run_v4l2(node, "-c", "{}={}".format(control, value))
        if code != 0:
            restore_sync_mode(written, control, run_v4l2)
            raise RuntimeError("Writing {}={} on {} failed: {}".format(
                control, value, node, err or out or "exit {}".format(code)))
        written[node] = as_found[node]
        readback = _read_value(node, control, run_v4l2)
        if readback != value:
            restore_sync_mode(written, control, run_v4l2)
            raise RuntimeError("{} readback on {} is {}, not {} - the driver clamped or "
                               "ignored the write".format(control, node, readback, value))
    return as_found
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_gmsl_sync.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add engine/gmsl_sync.py tests/engine/test_gmsl_sync.py
git commit -m "feat: apply/restore kernel camera_sync_mode with readback confirmation

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Vendored TSC script, KernelTscIO, GmslTscSync, detection

**Files:**
- Create: `tools/tsc_trigger/__init__.py`, `tools/tsc_trigger/ext_sync_gen.py`
- Modify: `engine/gmsl_sync.py`
- Test: `tests/engine/test_gmsl_sync.py`

**Interfaces:**
- Consumes: `resolve_sync_nodes`, `apply_sync_mode`, `restore_sync_mode` (Tasks 1-2)
- Produces:
  - `DEFAULT_GMSL_TSC_SYNC = {"control": "camera_sync_mode", "sync_mode_value": 2, "duty_percent": 50, "settle_s": 5.0}`
  - `class KernelTscIO(ext_module=None, open_fn=os.open, close_fn=os.close)` with `start(fps: int, duty: int)`, `stop()`
  - `class GmslTscSync(control, sync_mode_value, fps, duty_percent, settle_s, run_v4l2=run_v4l2, tsc_io=None, sleep=time.sleep, glob_fn=glob.glob)` with `engage()`, `disengage()`
  - `stop_tsc_best_effort(tsc_io=None, path_exists=os.path.exists) -> None`
  - `detect_gmsl_tsc_rig(panel_connection: dict, serials: list[str], device_lookup: Callable[[str], device], path_exists=os.path.exists) -> bool`

- [ ] **Step 1: Vendor the script**

```bash
mkdir tools/tsc_trigger
cp "C:/Users/gbaruch/scripts/twe camera pair/ext_sync_gen.py" tools/tsc_trigger/ext_sync_gen.py
```

Create `tools/tsc_trigger/__init__.py` as an empty file. Verify the copy is identical:

```bash
cmp "C:/Users/gbaruch/scripts/twe camera pair/ext_sync_gen.py" tools/tsc_trigger/ext_sync_gen.py && echo identical
```

Expected: `identical`

- [ ] **Step 2: Write the failing tests**

Add `import pyrealsense2 as rs` to the imports at the top of `tests/engine/test_gmsl_sync.py`, then append:

```python
class _FakeExtModule:
    CDI_TSC_DEV = "/dev/cdi_tsc"

    def __init__(self, fail_first_stop=False, fail_start=False):
        self.calls = []
        self._fail_first_stop = fail_first_stop
        self._fail_start = fail_start

    def tsc_fsync(self, fd, on):
        self.calls.append(("fsync", fd, on))
        if on == 0 and self._fail_first_stop:
            self._fail_first_stop = False
            raise OSError("not running")
        if on == 1 and self._fail_start:
            raise OSError("ioctl failed")

    def tsc_set_rate(self, fd, fps, duty):
        self.calls.append(("set_rate", fd, fps, duty))


def _tsc_io(ext):
    closed = []
    io = gmsl_sync.KernelTscIO(ext_module=ext, open_fn=lambda path, flags: 7, close_fn=closed.append)
    return io, closed


def test_kernel_tsc_io_start_matches_ext_sync_gen_enable_sequence():
    ext = _FakeExtModule(fail_first_stop=True)  # a leading stop that fails is ignored, like the script
    io, closed = _tsc_io(ext)

    io.start(30, 50)

    assert ext.calls == [("fsync", 7, 0), ("set_rate", 7, 30, 50), ("fsync", 7, 1)]
    assert closed == [7]


def test_kernel_tsc_io_stop_and_fd_closed_on_failure():
    ext = _FakeExtModule(fail_start=True)
    io, closed = _tsc_io(ext)

    with pytest.raises(RuntimeError, match="ioctl"):
        io.start(30, 50)
    assert closed == [7]

    io.stop()
    assert ext.calls[-1] == ("fsync", 7, 0)


def test_kernel_tsc_io_permission_error_mentions_udev_rule():
    io = gmsl_sync.KernelTscIO(ext_module=_FakeExtModule(),
                               open_fn=MagicMock(side_effect=PermissionError()), close_fn=MagicMock())
    with pytest.raises(RuntimeError, match='KERNEL=="cdi_tsc", MODE="0666"'):
        io.start(30, 50)


def _gmsl_sync(order, kernel=None, tsc_start_error=None):
    kernel = kernel or _FakeKernel({"/dev/video2": 0, "/dev/video10": 0})

    def run(node, *args, timeout=15):
        if args and args[0] == "-c":
            order.append(("write", node, args[1]))
        return kernel(node, *args, timeout=timeout)

    def tsc_start(fps, duty):
        order.append(("tsc_start", fps, duty))
        if tsc_start_error is not None:
            raise tsc_start_error

    tsc = MagicMock()
    tsc.start.side_effect = tsc_start
    tsc.stop.side_effect = lambda: order.append(("tsc_stop",))
    glob_fn = _fake_glob({"/dev/video-rs-*": NODES})
    sync = gmsl_sync.GmslTscSync(
        control="camera_sync_mode", sync_mode_value=2, fps=30, duty_percent=50, settle_s=5.0,
        run_v4l2=run, tsc_io=tsc, sleep=lambda s: order.append(("sleep", s)), glob_fn=glob_fn,
    )
    return sync, kernel, tsc


def test_engage_order_is_mode_then_tsc_then_settle():
    order = []
    sync, kernel, _ = _gmsl_sync(order)

    sync.engage()

    assert order == [("write", "/dev/video2", "camera_sync_mode=2"),
                     ("write", "/dev/video10", "camera_sync_mode=2"),
                     ("tsc_start", 30, 50), ("sleep", 5.0)]


def test_engage_restores_mode_when_tsc_start_fails():
    order = []
    sync, kernel, _ = _gmsl_sync(order, tsc_start_error=RuntimeError("ioctl failed"))

    with pytest.raises(RuntimeError, match="ioctl"):
        sync.engage()

    assert kernel.values == {"/dev/video2": 0, "/dev/video10": 0}
    assert ("sleep", 5.0) not in order


def test_disengage_stops_tsc_then_restores_and_is_idempotent():
    order = []
    sync, kernel, tsc = _gmsl_sync(order, kernel=_FakeKernel({"/dev/video2": 1, "/dev/video10": 0}))
    sync.engage()
    order.clear()

    sync.disengage()
    sync.disengage()

    assert order[0] == ("tsc_stop",)
    assert kernel.values == {"/dev/video2": 1, "/dev/video10": 0}
    assert tsc.stop.call_count == 1


def test_disengage_after_failed_engage_is_noop():
    order = []
    kernel = _FakeKernel({"/dev/video2": 0, "/dev/video10": 0}, fail_write_on={"/dev/video2"})
    sync, _, tsc = _gmsl_sync(order, kernel=kernel)
    with pytest.raises(RuntimeError):
        sync.engage()

    sync.disengage()  # must not raise

    tsc.stop.assert_not_called()


def test_disengage_never_raises_when_tsc_stop_fails():
    order = []
    sync, kernel, tsc = _gmsl_sync(order)
    sync.engage()
    tsc.stop.side_effect = OSError("gone")

    sync.disengage()  # must not raise

    assert kernel.values == {"/dev/video2": 0, "/dev/video10": 0}  # restore still ran


def test_stop_tsc_best_effort():
    tsc = MagicMock()
    gmsl_sync.stop_tsc_best_effort(tsc_io=tsc, path_exists=lambda p: False)
    tsc.stop.assert_not_called()

    tsc.stop.side_effect = OSError("x")
    gmsl_sync.stop_tsc_best_effort(tsc_io=tsc, path_exists=lambda p: True)  # must not raise
    tsc.stop.assert_called_once()


def _device(name="Intel RealSense D585", usb=False):
    device = MagicMock()
    device.get_info.side_effect = lambda info: name if info == rs.camera_info.name else "x"
    device.supports.side_effect = lambda info: usb if info == rs.camera_info.usb_type_descriptor else True
    return device


REMOTE = {"mode": "remote"}


def _detect(panel_connection=REMOTE, devices=None, tsc_exists=True):
    devices = devices if devices is not None else {"s1": _device(), "s2": _device()}
    return gmsl_sync.detect_gmsl_tsc_rig(
        panel_connection, list(devices), lambda serial: devices[serial],
        path_exists=lambda path: tsc_exists,
    )


def test_detect_true_for_two_gmsl_d585_on_remote():
    assert _detect() is True


@pytest.mark.parametrize("kwargs", [
    {"panel_connection": {"mode": "local"}},
    {"panel_connection": {}},
    {"devices": {"s1": _device()}},
    {"devices": {"s1": _device(), "s2": _device(), "s3": _device()}},
    {"devices": {"s1": _device(), "s2": _device(name="Intel RealSense D455")}},
    {"devices": {"s1": _device(), "s2": _device(usb=True)}},
    {"tsc_exists": False},
])
def test_detect_false_when_any_condition_fails(kwargs):
    assert _detect(**kwargs) is False


def test_detect_false_when_lookup_raises():
    def lookup(serial):
        raise RuntimeError("No connected device")

    assert gmsl_sync.detect_gmsl_tsc_rig(REMOTE, ["s1", "s2"], lookup, path_exists=lambda p: True) is False


def test_detect_never_looks_up_devices_in_local_mode():
    lookup = MagicMock()
    gmsl_sync.detect_gmsl_tsc_rig({"mode": "local"}, ["s1", "s2"], lookup, path_exists=lambda p: True)
    lookup.assert_not_called()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_gmsl_sync.py -v`
Expected: FAIL - `AttributeError: ... 'KernelTscIO'`

- [ ] **Step 4: Implement**

At the top of `engine/gmsl_sync.py`, extend the imports:

```python
import glob
import importlib
import os
import re
import subprocess
import time

import pyrealsense2 as rs
```

Append to `engine/gmsl_sync.py`:

```python
DEFAULT_GMSL_TSC_SYNC = {
    "control": "camera_sync_mode",
    "sync_mode_value": 2,
    "duty_percent": 50,
    "settle_s": 5.0,
}

CDI_TSC_DEV = "/dev/cdi_tsc"


class KernelTscIO:
    """Drives /dev/cdi_tsc through the vendored tools/tsc_trigger/
    ext_sync_gen.py's own ioctl helpers - the same sequence as its --enable
    (stop, ignore failure; set rate; start) and --disable. That module
    imports fcntl at top level (Linux-only), so it is imported lazily here,
    never at engine.gmsl_sync import time."""

    def __init__(self, ext_module=None, open_fn=os.open, close_fn=os.close):
        self._ext_module = ext_module
        self._open = open_fn
        self._close = close_fn

    def _ext(self):
        if self._ext_module is None:
            self._ext_module = importlib.import_module("tools.tsc_trigger.ext_sync_gen")
        return self._ext_module

    def _with_fd(self, action):
        ext = self._ext()
        try:
            fd = self._open(ext.CDI_TSC_DEV, os.O_RDWR)
        except PermissionError:
            raise RuntimeError(
                'Permission denied on {}. Add udev rule: KERNEL=="cdi_tsc", MODE="0666"'.format(
                    ext.CDI_TSC_DEV))
        try:
            action(ext, fd)
        except OSError as exc:
            raise RuntimeError("TSC ioctl failed: {}".format(exc))
        finally:
            self._close(fd)

    def start(self, fps, duty):
        def action(ext, fd):
            try:
                ext.tsc_fsync(fd, 0)
            except OSError:
                pass
            ext.tsc_set_rate(fd, fps, duty)
            ext.tsc_fsync(fd, 1)
        self._with_fd(action)

    def stop(self):
        self._with_fd(lambda ext, fd: ext.tsc_fsync(fd, 0))


class GmslTscSync:
    """engage(): resolve the two nodes -> external-sync mode on both
    (read-back confirmed) -> start the TSC -> wait settle_s so the sensors
    lock to a stable signal before any stream opens. Any failure undoes
    what was applied and raises. disengage(): stop TSC, restore as-found
    mode; best-effort, idempotent, never raises."""

    def __init__(self, control, sync_mode_value, fps, duty_percent, settle_s,
                 run_v4l2=run_v4l2, tsc_io=None, sleep=time.sleep, glob_fn=glob.glob):
        self._control = control
        self._value = sync_mode_value
        self._fps = fps
        self._duty = duty_percent
        self._settle_s = settle_s
        self._run_v4l2 = run_v4l2
        self._tsc_io = tsc_io or KernelTscIO()
        self._sleep = sleep
        self._glob_fn = glob_fn
        self._as_found = None
        self._tsc_running = False

    def engage(self):
        nodes = resolve_sync_nodes(self._control, run_v4l2=self._run_v4l2, glob_fn=self._glob_fn)
        self._as_found = apply_sync_mode(nodes, self._control, self._value, run_v4l2=self._run_v4l2)
        try:
            self._tsc_io.start(self._fps, self._duty)
        except Exception:
            restore_sync_mode(self._as_found, self._control, run_v4l2=self._run_v4l2)
            self._as_found = None
            raise
        self._tsc_running = True
        if self._settle_s > 0:
            self._sleep(self._settle_s)

    def disengage(self):
        if self._tsc_running:
            self._tsc_running = False
            try:
                self._tsc_io.stop()
            except Exception:
                pass
        if self._as_found is not None:
            as_found, self._as_found = self._as_found, None
            restore_sync_mode(as_found, self._control, run_v4l2=self._run_v4l2)


def stop_tsc_best_effort(tsc_io=None, path_exists=os.path.exists):
    """App-exit safety net: stop the generator if the device exists. Never
    raises."""
    if not path_exists(CDI_TSC_DEV):
        return
    try:
        (tsc_io or KernelTscIO()).stop()
    except Exception:
        pass


def detect_gmsl_tsc_rig(panel_connection, serials, device_lookup, path_exists=os.path.exists):
    """True only for: remote panel mode, exactly 2 cameras, both D585,
    neither reporting a USB descriptor (GMSL), and /dev/cdi_tsc present.
    Checks the cheap conditions before touching any device; any lookup
    failure means False - the hub must stay usable with no hardware."""
    if (panel_connection or {}).get("mode") != "remote":
        return False
    if len(serials) != 2 or not path_exists(CDI_TSC_DEV):
        return False
    try:
        for serial in serials:
            device = device_lookup(serial)
            if "D585" not in device.get_info(rs.camera_info.name):
                return False
            if device.supports(rs.camera_info.usb_type_descriptor):
                return False
    except Exception:
        return False
    return True
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_gmsl_sync.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add tools/tsc_trigger engine/gmsl_sync.py tests/engine/test_gmsl_sync.py
git commit -m "feat: vendor ext_sync_gen.py and add GmslTscSync engage/disengage and rig detection

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Controller integration

**Files:**
- Modify: `engine/multi_camera_session.py` (`__init__` ~line 98, `start_all` ~line 148, `_on_thread_finished` ~line 260)
- Test: `tests/engine/test_multi_camera_session.py`

**Interfaces:**
- Consumes: any object with `engage()`/`disengage()` (`GmslTscSync`, Task 3)
- Produces: `MultiCameraSessionController(..., gmsl_sync=None)`

- [ ] **Step 1: Write the failing tests**

In `tests/engine/test_multi_camera_session.py`, change the `_controller` helper signature and constructor call to pass `gmsl_sync` through:

```python
def _controller(camera_specs, sync_setter=None, device_lookup=None, camera_start_stagger_s=0,
                gmsl_sync=None, thread_factory=None):
    fake_threads = {}

    def default_thread_factory(**kwargs):
        thread = _FakeSessionEngineThread(**kwargs)
        fake_threads[kwargs["device_serial"]] = thread
        return thread

    controller = MultiCameraSessionController(
        camera_specs=camera_specs,
        thread_factory=thread_factory or default_thread_factory,
        device_lookup=device_lookup or (lambda ctx, serial: MagicMock(name=serial)),
        sync_setter=sync_setter or MagicMock(return_value=True),
        camera_start_stagger_s=camera_start_stagger_s,
        gmsl_sync=gmsl_sync,
    )
    return controller, fake_threads
```

(Keep the helper's existing comment about the stagger default.) Append:

```python
# --- GMSL TSC sync: engaged after genlock, before any thread; disengaged
# only after every thread finished. ---

def _gmsl_specs():
    return [_spec("cam1", True, inter_cam_sync_value=None, device_serial="s1"),
            _spec("cam2", False, inter_cam_sync_value=None, device_serial="s2")]


def test_start_all_engages_gmsl_before_any_thread_starts():
    events = []
    gmsl = MagicMock()
    gmsl.engage.side_effect = lambda: events.append("engage")

    def thread_factory(**kwargs):
        events.append("thread")
        return _FakeSessionEngineThread(**kwargs)

    controller, _ = _controller(_gmsl_specs(), gmsl_sync=gmsl, thread_factory=thread_factory)
    controller.start_all(ctx=object())

    assert events == ["engage", "thread", "thread"]


def test_start_all_starts_no_thread_when_gmsl_engage_fails():
    gmsl = MagicMock()
    gmsl.engage.side_effect = RuntimeError("readback mismatch")
    controller, fake_threads = _controller(_gmsl_specs(), gmsl_sync=gmsl)

    with pytest.raises(RuntimeError, match="readback"):
        controller.start_all(ctx=object())
    assert fake_threads == {}


def test_start_all_disengages_gmsl_if_thread_start_raises():
    gmsl = MagicMock()

    def thread_factory(**kwargs):
        raise RuntimeError("thread construction failed")

    controller, _ = _controller(_gmsl_specs(), gmsl_sync=gmsl, thread_factory=thread_factory)

    with pytest.raises(RuntimeError, match="thread construction"):
        controller.start_all(ctx=object())
    gmsl.disengage.assert_called_once()


def test_gmsl_disengaged_only_after_every_thread_finished():
    gmsl = MagicMock()
    controller, fake_threads = _controller(_gmsl_specs(), gmsl_sync=gmsl)
    controller.start_all(ctx=object())

    fake_threads["s1"].finished.emit()
    gmsl.disengage.assert_not_called()
    fake_threads["s2"].finished.emit()
    gmsl.disengage.assert_called_once()


def test_stop_all_never_disengages_gmsl_by_itself():
    gmsl = MagicMock()
    controller, _ = _controller(_gmsl_specs(), gmsl_sync=gmsl)
    controller.start_all(ctx=object())

    controller.stop_all()

    gmsl.disengage.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_multi_camera_session.py -v`
Expected: new tests FAIL with `TypeError: ... unexpected keyword argument 'gmsl_sync'`; existing tests also error until the kwarg exists.

- [ ] **Step 3: Implement**

In `engine/multi_camera_session.py` `__init__`, add the parameter and store it:

```python
    def __init__(self, camera_specs, pairing_gap_outlier_threshold_us=100_000,
                 thread_factory=None, device_lookup=None, sync_setter=None,
                 camera_start_stagger_s=2.0, gmsl_sync=None, parent=None):
```

and, after `self._sync_setter = ...`:

```python
        # Optional GMSL external-sync collaborator (engine.gmsl_sync.
        # GmslTscSync): kernel camera_sync_mode + the Orin's TSC trigger,
        # engaged after the genlock step and before any thread, disengaged
        # only once every thread has finished. None = today's behavior.
        self._gmsl_sync = gmsl_sync
```

In `start_all`, directly after the genlock role loop (after `self._applied_genlock_specs.append(spec)` loop ends) and before `self._finished_rows_by_camera = {}`:

```python
        if self._gmsl_sync is not None:
            try:
                self._gmsl_sync.engage()
            except Exception:
                self._reset_genlock_roles()
                raise
```

Wrap the existing thread-start `for index, spec in enumerate(self._camera_specs):` loop:

```python
        try:
            for index, spec in enumerate(self._camera_specs):
                ...existing body unchanged...
        except Exception:
            # The trigger must not keep running for a run that never got
            # its threads up.
            if self._gmsl_sync is not None:
                self._gmsl_sync.disengage()
            raise
```

In `_on_thread_finished`, right after `self._reset_genlock_roles()`:

```python
            if self._gmsl_sync is not None:
                self._gmsl_sync.disengage()
```

Add one sentence to `start_all`'s docstring step list: "2b. If a gmsl_sync collaborator was given, engage it (kernel external-sync mode + TSC trigger + settle) - all-or-nothing, same as genlock."

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_multi_camera_session.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add engine/multi_camera_session.py tests/engine/test_multi_camera_session.py
git commit -m "feat: engage/disengage GMSL TSC sync around multi-camera sessions

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Multi-camera page passes the config to the controller

**Files:**
- Modify: `gui/pages/multi_camera_live_session_page.py` (`__init__` ~line 150, `set_cameras` ~line 262, `start_all_sessions` controller_kwargs ~line 577)
- Test: `tests/gui/pages/test_multi_camera_live_session_page.py`

**Interfaces:**
- Consumes: `MultiCameraSessionController(..., gmsl_sync=...)` (Task 4), `GmslTscSync(**config)` (Task 3)
- Produces: `MultiCameraLiveSessionPage(..., gmsl_sync_factory=None)`; `set_cameras(ctx, cameras, gmsl_tsc_sync=None)` where `gmsl_tsc_sync` is `{"control", "sync_mode_value", "duty_percent", "settle_s", "fps"}` or `None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/gui/pages/test_multi_camera_live_session_page.py`:

```python
GMSL_CONFIG = {"control": "camera_sync_mode", "sync_mode_value": 2, "duty_percent": 50,
               "settle_s": 0.0, "fps": 30}


def _page_with_fake_gmsl():
    page, fake_threads = _page_with_fake_threads()
    created = []

    def factory(**kwargs):
        sync = MagicMock()
        sync.kwargs = kwargs
        created.append(sync)
        return sync

    page._gmsl_sync_factory = factory
    return page, fake_threads, created


def test_start_all_sessions_builds_gmsl_sync_from_config(qapp, tmp_path):
    page, _, created = _page_with_fake_gmsl()
    page.set_cameras(object(), _two_cameras(tmp_path), gmsl_tsc_sync=GMSL_CONFIG)

    page.start_all_sessions()

    assert len(created) == 1
    assert created[0].kwargs == GMSL_CONFIG
    assert page._controller._gmsl_sync is created[0]
    created[0].engage.assert_called_once()


def test_start_all_sessions_without_gmsl_config_passes_none(qapp, tmp_path):
    page, _, created = _page_with_fake_gmsl()
    page.set_cameras(object(), _two_cameras(tmp_path))

    page.start_all_sessions()

    assert created == []
    assert page._controller._gmsl_sync is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_multi_camera_live_session_page.py -v -k gmsl`
Expected: FAIL - `TypeError: set_cameras() got an unexpected keyword argument 'gmsl_tsc_sync'`

- [ ] **Step 3: Implement**

Add the import near the other engine imports:

```python
from engine.gmsl_sync import GmslTscSync
```

`__init__` signature and body:

```python
    def __init__(self, thread_factory=None, device_lookup=None, sync_setter=None,
                 camera_start_stagger_s=None, controller_factory=None, gmsl_sync_factory=None,
                 parent=None):
```

after `self._controller_factory = ...`:

```python
        self._gmsl_sync_factory = gmsl_sync_factory or GmslTscSync
        # engine.gmsl_sync config dict for this run, or None (the normal
        # case) - set by MainWindow only when Camera Hub's "GMSL TSC sync"
        # checkbox is ticked.
        self._gmsl_tsc_sync = None
```

`set_cameras`:

```python
    def set_cameras(self, ctx, cameras, gmsl_tsc_sync=None):
```

and at the top of its body, next to `self._cameras = cameras`:

```python
        self._gmsl_tsc_sync = gmsl_tsc_sync
```

In `start_all_sessions`, after the `camera_start_stagger_s` controller_kwargs block:

```python
        if self._gmsl_tsc_sync is not None:
            controller_kwargs["gmsl_sync"] = self._gmsl_sync_factory(**self._gmsl_tsc_sync)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_multi_camera_live_session_page.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add gui/pages/multi_camera_live_session_page.py tests/gui/pages/test_multi_camera_live_session_page.py
git commit -m "feat: thread GMSL TSC sync config from multi-camera page to controller

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Camera Hub checkbox

**Files:**
- Modify: `gui/pages/camera_hub_page.py` (imports, `__init__` ~line 70-95)
- Test: `tests/gui/pages/test_camera_hub_page.py`

**Interfaces:**
- Produces: `CameraHubPage.set_gmsl_tsc_available(available: bool)`, `CameraHubPage.gmsl_tsc_checked -> bool` (property), widget `CameraHubPage.gmsl_tsc_checkbox`

- [ ] **Step 1: Write the failing tests**

Append to `tests/gui/pages/test_camera_hub_page.py`:

```python
def test_gmsl_checkbox_hidden_and_unchecked_by_default(qapp):
    page = CameraHubPage()
    assert page.gmsl_tsc_checkbox.isHidden()
    assert page.gmsl_tsc_checked is False


def test_gmsl_checkbox_pre_ticked_when_it_becomes_available(qapp):
    page = CameraHubPage()
    page.set_gmsl_tsc_available(True)
    assert not page.gmsl_tsc_checkbox.isHidden()
    assert page.gmsl_tsc_checked is True


def test_gmsl_checkbox_untick_survives_refresh(qapp):
    page = CameraHubPage()
    page.set_gmsl_tsc_available(True)
    page.gmsl_tsc_checkbox.setChecked(False)

    page.set_gmsl_tsc_available(True)  # e.g. hub refresh after an Edit

    assert page.gmsl_tsc_checked is False


def test_gmsl_checkbox_unavailable_hides_and_reports_false(qapp):
    page = CameraHubPage()
    page.set_gmsl_tsc_available(True)
    page.set_gmsl_tsc_available(False)
    assert page.gmsl_tsc_checkbox.isHidden()
    assert page.gmsl_tsc_checked is False

    page.set_gmsl_tsc_available(True)  # becomes available again -> re-ticked
    assert page.gmsl_tsc_checked is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_camera_hub_page.py -v -k gmsl`
Expected: FAIL - `AttributeError: 'CameraHubPage' object has no attribute 'gmsl_tsc_checkbox'`

- [ ] **Step 3: Implement**

Add `QCheckBox` to the existing `PySide6.QtWidgets` import in `gui/pages/camera_hub_page.py`.

In `__init__`, between `layout.addWidget(self.status_label)` and the Start button:

```python
        # Only shown on the Orin with 2x D585 over GMSL (MainWindow runs
        # engine.gmsl_sync.detect_gmsl_tsc_rig and calls
        # set_gmsl_tsc_available). Pre-ticked on becoming available; the
        # operator can untick it for a free-running baseline run.
        self.gmsl_tsc_checkbox = QCheckBox("GMSL TSC sync (external trigger)")
        self.gmsl_tsc_checkbox.setHidden(True)
        self._gmsl_tsc_available = False
        layout.addWidget(self.gmsl_tsc_checkbox)
```

Add methods:

```python
    def set_gmsl_tsc_available(self, available):
        """Pre-ticks only on the unavailable->available transition, so an
        operator's untick survives a later hub refresh (e.g. after Edit)."""
        if available and not self._gmsl_tsc_available:
            self.gmsl_tsc_checkbox.setChecked(True)
        if not available:
            self.gmsl_tsc_checkbox.setChecked(False)
        self._gmsl_tsc_available = available
        self.gmsl_tsc_checkbox.setHidden(not available)

    @property
    def gmsl_tsc_checked(self):
        return self._gmsl_tsc_available and self.gmsl_tsc_checkbox.isChecked()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/pages/test_camera_hub_page.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add gui/pages/camera_hub_page.py tests/gui/pages/test_camera_hub_page.py
git commit -m "feat: add GMSL TSC sync checkbox to Camera Hub

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: MainWindow wiring, settings.yaml, main.py exit, CLAUDE.md

**Files:**
- Modify: `gui/main_window.py` (imports ~line 60, `_refresh_camera_hub` ~line 624, `_on_start_multi_camera_session_requested` ~lines 730-763)
- Modify: `settings.yaml` (`camera_sync` section ~line 219)
- Modify: `main.py` (after `app.exec()`)
- Modify: `CLAUDE.md`
- Test: `tests/gui/test_main_window.py`

**Interfaces:**
- Consumes: `detect_gmsl_tsc_rig`, `DEFAULT_GMSL_TSC_SYNC`, `stop_tsc_best_effort` (Task 3); `CameraHubPage.set_gmsl_tsc_available`/`gmsl_tsc_checked` (Task 6); `MultiCameraLiveSessionPage.set_cameras(ctx, cameras, gmsl_tsc_sync=...)` (Task 5)

- [ ] **Step 1: Write the failing tests**

Append to `tests/gui/test_main_window.py`:

```python
# --- GMSL TSC sync: detection on hub refresh, and the Start branch. ---

def _two_camera_window(qapp, monkeypatch, tmp_path, slave_pick=COLOR0):
    settings = _full_settings({"Intel RealSense D455": [_ir_vs_rgb_test()]})
    settings["camera"]["inter_cam_sync"] = {
        "Intel RealSense D455": {"master": 1, "slave": 2, "max_slave_color_resolution": {"width": 640, "height": 480}},
    }
    window = _make_window(qapp, settings)
    monkeypatch.setattr(main_window_module, "list_video_stream_options", lambda ctx, serial: [IR1, COLOR0])
    monkeypatch.setattr(main_window_module, "save_gui_state", lambda state: None)
    monkeypatch.setattr(window.roi_page, "set_context", lambda *a, **k: None)
    monkeypatch.setattr(main_window_module, "ensure_output_dir", lambda settings: str(tmp_path))
    monkeypatch.setattr(
        main_window_module, "load_led_positions",
        lambda *a, **k: ({"0": [1.0, 1.0, 300.0, 100.0, 200.0]}, {"0": [2.0, 2.0, 600.0, 200.0, 400.0]}),
    )
    master_id = _configure_one_camera(window, "SN123")
    window._on_add_camera_requested()
    slave_id = _configure_one_camera(window, "SN456", color_pick=slave_pick)
    return window, master_id, slave_id


def test_refresh_camera_hub_runs_gmsl_detection(qapp, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(main_window_module, "detect_gmsl_tsc_rig",
                        lambda panel_connection, serials, lookup: calls.append(sorted(serials)) or True)
    window, _, _ = _two_camera_window(qapp, monkeypatch, tmp_path)

    assert calls[-1] == ["SN123", "SN456"]
    assert window.camera_hub_page.gmsl_tsc_checked is True


def test_start_with_gmsl_ticked_skips_genlock_and_passes_config(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(main_window_module, "detect_gmsl_tsc_rig", lambda *a: True)
    # COLOR0 (1280x720) on the slave would normally fail the slave-color check.
    window, master_id, slave_id = _two_camera_window(qapp, monkeypatch, tmp_path, slave_pick=COLOR0)
    critical = _capture_critical(monkeypatch)
    captured = {}
    monkeypatch.setattr(window.multi_camera_live_session_page, "set_cameras",
                        lambda ctx, cameras, gmsl_tsc_sync=None: captured.update(
                            cameras=cameras, gmsl_tsc_sync=gmsl_tsc_sync))

    window._on_start_multi_camera_session_requested()

    assert critical == []
    assert all(c["config"]["inter_cam_sync_value"] is None for c in captured["cameras"])
    assert captured["gmsl_tsc_sync"] == {"control": "camera_sync_mode", "sync_mode_value": 2,
                                         "duty_percent": 50, "settle_s": 5.0, "fps": 30}


def test_start_with_gmsl_ticked_blocks_on_fps_mismatch(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(main_window_module, "detect_gmsl_tsc_rig", lambda *a: True)
    color_15fps = dict(COLOR0, fps=15)
    window, _, _ = _two_camera_window(qapp, monkeypatch, tmp_path, slave_pick=color_15fps)
    critical = _capture_critical(monkeypatch)
    set_cameras = MagicMock()
    monkeypatch.setattr(window.multi_camera_live_session_page, "set_cameras", set_cameras)

    window._on_start_multi_camera_session_requested()

    assert len(critical) == 1
    set_cameras.assert_not_called()


def test_start_with_gmsl_unticked_behaves_as_before(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(main_window_module, "detect_gmsl_tsc_rig", lambda *a: True)
    window, master_id, slave_id = _two_camera_window(qapp, monkeypatch, tmp_path, slave_pick=COLOR0_SAFE)
    window.camera_hub_page.gmsl_tsc_checkbox.setChecked(False)
    captured = {}
    monkeypatch.setattr(window.multi_camera_live_session_page, "set_cameras",
                        lambda ctx, cameras, gmsl_tsc_sync=None: captured.update(
                            cameras=cameras, gmsl_tsc_sync=gmsl_tsc_sync))

    window._on_start_multi_camera_session_requested()

    configs = {c["camera_id"]: c["config"] for c in captured["cameras"]}
    assert configs[master_id]["inter_cam_sync_value"] == 1
    assert configs[slave_id]["inter_cam_sync_value"] == 2
    assert captured["gmsl_tsc_sync"] is None


def test_gmsl_settings_section_overrides_defaults(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(main_window_module, "detect_gmsl_tsc_rig", lambda *a: True)
    window, _, _ = _two_camera_window(qapp, monkeypatch, tmp_path)
    window.settings["camera_sync"] = {"gmsl_tsc_sync": {"duty_percent": 25, "settle_s": 2.0}}
    captured = {}
    monkeypatch.setattr(window.multi_camera_live_session_page, "set_cameras",
                        lambda ctx, cameras, gmsl_tsc_sync=None: captured.update(gmsl_tsc_sync=gmsl_tsc_sync))

    window._on_start_multi_camera_session_requested()

    assert captured["gmsl_tsc_sync"]["duty_percent"] == 25
    assert captured["gmsl_tsc_sync"]["settle_s"] == 2.0
    assert captured["gmsl_tsc_sync"]["sync_mode_value"] == 2
```

Note: `_full_settings` has no `panel_connection` key, so existing tests exercise the local/default path; these tests patch `detect_gmsl_tsc_rig` directly.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/test_main_window.py -v -k gmsl`
Expected: FAIL - `AttributeError: <module 'gui.main_window'> has no attribute 'detect_gmsl_tsc_rig'`

- [ ] **Step 3: Implement MainWindow**

Add import in `gui/main_window.py` next to the other engine imports:

```python
from engine.gmsl_sync import detect_gmsl_tsc_rig, DEFAULT_GMSL_TSC_SYNC
```

At the end of `_refresh_camera_hub`, after `self.camera_hub_page.set_cameras(summaries)`:

```python
        # GMSL TSC sync is only offered on the Orin with 2x D585 over GMSL -
        # see engine.gmsl_sync.detect_gmsl_tsc_rig. Local panel mode returns
        # False before any device lookup, so Windows/tests are unaffected.
        self.camera_hub_page.set_gmsl_tsc_available(detect_gmsl_tsc_rig(
            self.settings.get("panel_connection"),
            [camera["config"]["device_serial"] for camera in self._cameras.values()],
            lambda serial: find_device_by_serial(self.ctx, serial),
        ))
```

In `_on_start_multi_camera_session_requested`, directly after `camera_sync_settings = self.settings.get("camera_sync") or {}`:

```python
        gmsl_tsc_on = self.camera_hub_page.gmsl_tsc_checked
        gmsl_tsc_sync = None
        if gmsl_tsc_on:
            fps_values = sorted({camera["config"][pick]["fps"]
                                 for camera in self._cameras.values() for pick in ("pick_a", "pick_b")})
            if len(fps_values) != 1:
                QMessageBox.critical(
                    self, "GMSL TSC sync needs one frame rate",
                    "The TSC trigger drives every GMSL camera at one rate, but the configured "
                    "streams use {} fps. Set every stream to the same fps in Stream Config, or "
                    "untick \"GMSL TSC sync\" on the Camera Hub.".format(
                        " / ".join(str(fps) for fps in fps_values)),
                )
                return
            gmsl_tsc_sync = {**DEFAULT_GMSL_TSC_SYNC,
                             **(camera_sync_settings.get("gmsl_tsc_sync") or {}),
                             "fps": fps_values[0]}
```

In the `cameras = [...]` comprehension, replace the `"inter_cam_sync_value": resolve_inter_cam_sync_value(...)` entry with:

```python
                 # GMSL TSC sync replaces SDK genlock entirely - the two are
                 # never applied together.
                 "inter_cam_sync_value": None if gmsl_tsc_on else resolve_inter_cam_sync_value(
                     inter_cam_sync_settings, camera["label"],
                     is_master=(camera_id == self._master_camera_id),
                 ),
```

Replace `conflicts = _slave_genlock_color_resolution_conflicts(cameras, inter_cam_sync_settings)` with:

```python
        # A USB-bandwidth rule for SDK genlock slaves - not applicable to GMSL.
        conflicts = [] if gmsl_tsc_on else _slave_genlock_color_resolution_conflicts(
            cameras, inter_cam_sync_settings)
```

Replace `self.multi_camera_live_session_page.set_cameras(self.ctx, cameras)` with:

```python
        self.multi_camera_live_session_page.set_cameras(self.ctx, cameras, gmsl_tsc_sync=gmsl_tsc_sync)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/gui/test_main_window.py -v`
Expected: all PASS (new and existing)

- [ ] **Step 5: settings.yaml**

At the end of the `camera_sync:` section in `settings.yaml` (keep 2-space indentation under `camera_sync`):

```yaml
  # GMSL external sync for 2x D585 on the Orin (Camera Hub's "GMSL TSC
  # sync" checkbox - only offered when panel_connection.mode is remote,
  # both cameras are D585 with no USB descriptor, and /dev/cdi_tsc exists;
  # see engine/gmsl_sync.py). The trigger rate is the streams' own fps,
  # not a setting - every stream must share one fps.
  gmsl_tsc_sync:
    # Kernel V4L2 control and value - NOT the SDK enum. On this D585
    # driver: 0 = Default, 1 = Master, 2 = External Sync (the SDK's
    # d500_intercam_sync_mode numbers these differently; kernel 2 matches
    # SDK 3). Confirmed via `v4l2-ctl -L` on the rig.
    control: camera_sync_mode
    sync_mode_value: 2
    duty_percent: 50
    # Trigger runs this long before any stream opens, so the sensors lock
    # to a stable signal (matches the reference method that passes).
    settle_s: 5.0
```

- [ ] **Step 6: main.py exit safety**

In `main.py`, add the import next to `from engine import panel_rpc_client`:

```python
from engine.gmsl_sync import stop_tsc_best_effort
```

Change the post-`app.exec()` block to:

```python
    exit_code = app.exec()
    if settings["panel_connection"]["mode"] == "remote":
        panel_rpc_client.close()
        # Closing the window mid-run must not leave the Orin's TSC
        # generator running. No-op when /dev/cdi_tsc doesn't exist.
        stop_tsc_best_effort()
    sys.exit(exit_code)
```

- [ ] **Step 7: CLAUDE.md**

Add a new section after "Cross-camera matching: HW timestamp vs. RealSense GLOBAL_TIME, and two parallel latency metrics":

```markdown
### GMSL TSC sync (2x D585 on the Orin)

On the Orin (`panel_connection.mode: remote`) with two D585s on the GMSL
deserializer, `engine/gmsl_sync.py` hardware-syncs both cameras instead of
SDK genlock: kernel `camera_sync_mode=2` ("External Sync") written via
`v4l2-ctl` with a read-back check, then the Orin's TSC signal generator
(`/dev/cdi_tsc`) started at the streams' shared fps, then `settle_s` of
wait before any stream opens. Why V4L2, not the SDK's
`inter_cam_sync_mode`: on this D585 prototype firmware the SDK write
succeeds silently and its readback throws, so the mode can never be
confirmed. The kernel enum is NOT the SDK enum (kernel 2 = SDK 3).

Gating is auto-detect + operator checkbox: `detect_gmsl_tsc_rig` (remote
mode, exactly 2 cameras, both D585, neither reports
`usb_type_descriptor`, `/dev/cdi_tsc` exists) decides whether Camera Hub
shows its "GMSL TSC sync" checkbox, pre-ticked on becoming available.
When ticked, every camera's `inter_cam_sync_value` is forced to `None` and
the slave-color-resolution check is skipped - SDK genlock and GMSL sync
are never applied together. `MultiCameraSessionController` engages
`GmslTscSync` after the genlock step and before any thread
(all-or-nothing), and disengages (TSC off, as-found mode restored) only
once every thread's own `finished` has fired. `main.py` also stops the TSC
on exit in remote mode.

`tools/tsc_trigger/ext_sync_gen.py` is the user's script vendored
unchanged; `engine/gmsl_sync.KernelTscIO` imports its ioctl helpers
lazily (it imports `fcntl`, Linux-only). Manual recovery if the app dies
mid-run: `python3 tools/tsc_trigger/ext_sync_gen.py --disable` on the
Orin. Unconfirmed on real hardware: that a GMSL D585 reports no
`usb_type_descriptor` - if wrong, only that detection rule changes.
```

- [ ] **Step 8: Run the full suite**

Run: `.venv\Scripts\python.exe -m pytest -v`
Expected: all PASS

- [ ] **Step 9: Commit**

```bash
git add gui/main_window.py tests/gui/test_main_window.py settings.yaml main.py CLAUDE.md
git commit -m "feat: wire GMSL TSC sync detection and Start branch into MainWindow

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Real-hardware verification (Orin, 2x D585 GMSL) - manual

Not automatable; run by the operator after merge to the Orin checkout.

- [ ] **Step 1:** Confirm detection's USB assumption:

```bash
python3 -c "import pyrealsense2 as rs; [print(d.get_info(rs.camera_info.name), d.supports(rs.camera_info.usb_type_descriptor)) for d in rs.context().query_devices()]"
```

Expected: both D585 lines print `False`. If `True`, detection rule 4 must change before the checkbox will appear.

- [ ] **Step 2:** Configure both cameras (same fps on all streams), confirm the Camera Hub checkbox is visible and ticked, Start, and check both cameras deliver frames at the trigger rate.
- [ ] **Step 3:** After the run, `v4l2-ctl -d <node> -C camera_sync_mode` on both nodes shows the as-found value, and the TSC is off.
- [ ] **Step 4:** Untick the checkbox and Start again - the run behaves as today (free-running).
