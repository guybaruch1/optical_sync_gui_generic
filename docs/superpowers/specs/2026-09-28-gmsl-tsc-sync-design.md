# GMSL TSC Sync for 2x D585 on the Orin — Design

## Context

With the Orin/Windows split (`2026-09-22-orin-panel-rpc-split-design.md`),
the camera + GUI + measurement run on an NVIDIA Orin while the LED panel
stays on Windows (`panel_connection.mode: remote`). On that rig, two D585
cameras are connected through the Orin's **GMSL** deserializer, not USB.
GMSL cameras on this carrier board can be hardware-synced by the Orin's own
**TSC signal generator** (`/dev/cdi_tsc`), which drives every GMSL camera's
sync line at once.

The existing multi-camera genlock path (`MultiCameraSessionController`'s
SDK `inter_cam_sync_mode` master/slave assignment) does not cover this:

- D585 has no `camera.inter_cam_sync` entry in `settings.yaml`, so today a
  2x D585 run skips sync entirely.
- On this D585 prototype firmware, the SDK `inter_cam_sync_mode` write
  "succeeds" silently and its readback throws, so the mode can never be
  confirmed through the SDK.
- There is no master/slave on D500 under an external trigger - both cameras
  get the SAME value.

The reference method that works on this rig (the external
`check_d585_sync_v4l2.py`) writes the **kernel** control
`camera_sync_mode` via `v4l2-ctl` (which has a working readback), then
starts the TSC generator, waits ~5 s for it to stabilize, and only then
opens any stream.

**Scope: only the sync method** - putting both cameras into external-sync
mode and running the trigger around a session. None of the reference
script's analysis, reports, link snapshots, dmesg capture, or laser
handling is ported.

## 1. When it is used: auto-detect + operator checkbox

A pure function `engine/gmsl_sync.py: detect_gmsl_tsc_rig(...)` returns
True only when ALL of:

1. `panel_connection.mode == "remote"`
2. exactly 2 cameras are configured
3. both configured devices' names contain `"D585"`
4. neither device supports/reports `rs.camera_info.usb_type_descriptor`
   (GMSL devices have no USB descriptor)
5. `/dev/cdi_tsc` exists

Device-info lookups and the path-exists check are injected, so this is
unit-testable with fakes.

**Camera Hub** gets a `"GMSL TSC sync"` checkbox (`CameraHubPage`,
below the camera cards). `MainWindow._refresh_camera_hub` runs detection
and calls a new `CameraHubPage.set_gmsl_tsc_available(bool)`:

- detection False -> checkbox hidden and unchecked
- detection True -> checkbox visible, enabled, and **pre-ticked** on the
  transition from unavailable to available (a later re-refresh while
  already available does not re-tick it, so an operator's untick survives
  e.g. an Edit round trip)

Unticking gives a deliberate free-running baseline run on the same rig.
Detection failures (no device reachable, lookup raises) are treated as
False, never as an error - the hub must stay usable with no hardware.

The checkbox is read once, at Start, in
`MainWindow._on_start_multi_camera_session_requested`, **2+-camera branch
only** (the 1-camera `LiveSessionPage` branch is untouched - a solo camera
has nothing to sync against). When ticked:

- every camera's `inter_cam_sync_value` is forced to `None`, so the SDK
  genlock path never runs alongside V4L2/TSC sync
- `_slave_genlock_color_resolution_conflicts` is skipped (it is an SDK
  genlock/USB-bandwidth rule, not applicable to GMSL)
- a `gmsl_tsc_sync` config dict (section 3) is passed through to
  `MultiCameraLiveSessionPage`, which passes it to the controller

## 2. Components

### 2.1 `tools/tsc_trigger/ext_sync_gen.py` (vendored, unchanged)

The user's `ext_sync_gen.py` copied byte-for-byte into
`tools/tsc_trigger/`, plus an empty `__init__.py`. It stays runnable as a
standalone CLI on the Orin (`python3 tools/tsc_trigger/ext_sync_gen.py
--disable` is the manual recovery if the app dies mid-run). The engine
imports `tsc_fsync`, `tsc_set_rate`, `CDI_TSC_DEV` from it.

Its module-level `import fcntl` does not exist on Windows, so the engine
imports it **lazily**, only inside the TSC functions - importing
`engine/gmsl_sync.py` works on Windows and in CI.

Its known `--fps`-without-`--duty` wart does not apply: the engine always
passes both values.

### 2.2 `engine/gmsl_sync.py` (new, pure core)

Hardware touches go through two injectable callables, defaulting to real
implementations:

- `run_v4l2(node, *args) -> (returncode, stdout, stderr)` - one `v4l2-ctl`
  subprocess call, never raises (missing binary -> 127, timeout -> 124),
  same shape as the reference script's `run_v4l2`
- `tsc_io` - an object with `start(fps, duty)` / `stop()`; the default
  opens `CDI_TSC_DEV`, and for start does `fsync(0)` (ignore OSError),
  `set_rate(fps, duty)`, `fsync(1)`; for stop `fsync(0)`; always closes
  the fd. This matches `ext_sync_gen.py --enable/--disable` exactly.

Functions:

- **`detect_gmsl_tsc_rig(...)`** - section 1.
- **`resolve_sync_nodes(control, run_v4l2, glob_fn) -> [node, node]`**
  1. candidates = `/dev/video-rs-*-N` symlinks, excluding names containing
     `-md-` (metadata nodes expose no controls)
  2. keep candidates whose `v4l2-ctl -L` lists `control`
  3. if that does not yield exactly 2 nodes, scan every `/dev/videoN`
  4. exactly 2 nodes required, else `RuntimeError` naming what was found.
     Nodes are assigned in `/dev` order; which node maps to which camera
     does not matter because both cameras get the same value.
- **`apply_sync_mode(nodes, control, value, run_v4l2) -> as_found`**
  - reads each node's current value (`-C`) - the as-found values
  - checks `value` against the control's `min=`/`max=` from `-L`; out of
    range -> `RuntimeError` before writing anything
  - writes (`-c control=value`) and reads back on each node; write failure
    or readback != value -> restore any node already written, then
    `RuntimeError`
  - returns `{node: as_found_value}`
- **`restore_sync_mode(nodes_to_values, control, run_v4l2)`** -
  best-effort, swallows errors per node (same convention as
  `_reset_genlock_roles`). A node whose as-found value was unreadable is
  skipped.
- **`class GmslTscSync`** - bundles the above for the controller:
  - `__init__(control, sync_mode_value, fps, duty_percent, settle_s,
    run_v4l2=..., tsc_io=..., sleep=time.sleep, glob_fn=glob.glob)`
  - `engage()` - resolve nodes -> `apply_sync_mode` -> `tsc_io.start(fps,
    duty)` -> `sleep(settle_s)`. If TSC start fails, restore the mode
    before raising. Records what it applied.
  - `disengage()` - `tsc_io.stop()` then `restore_sync_mode(...)`;
    best-effort, idempotent (a second call, or a call after a failed
    `engage()`, is a no-op for whatever wasn't applied).

Parsing helpers (control names out of `-L`, value out of `-C` including
the `camera_sync_mode: 2 (External Sync)` menu-label form, `min=/max=`)
are ported from the reference script, since they have already been shaken
out against this driver's real output.

### 2.3 `settings.yaml`

Under the existing `camera_sync` section:

```yaml
  gmsl_tsc_sync:
    # Kernel V4L2 control and value - NOT the SDK enum. On this D585
    # driver: 0 = Default, 1 = Master, 2 = External Sync (SDK's own
    # d500_intercam_sync_mode numbers these differently; kernel 2 matches
    # SDK 3). Confirmed via `v4l2-ctl -L` on the rig.
    control: camera_sync_mode
    sync_mode_value: 2
    duty_percent: 50
    # Trigger runs this long before any stream opens, so the sensors lock
    # to a stable signal (matches the reference method that passes).
    settle_s: 5.0
```

Trigger fps is **not** a setting: it is the streams' fps. At Start, every
pick (`pick_a`/`pick_b` of both cameras) must share one fps; otherwise
Start is blocked with a `QMessageBox.critical` naming the mismatch
(nothing is written to hardware).

## 3. Data flow

`_on_start_multi_camera_session_requested` (checkbox ticked) builds
`gmsl_tsc_sync = {**settings["camera_sync"]["gmsl_tsc_sync"], "fps": <shared fps>}`
and passes it to `MultiCameraLiveSessionPage.set_context(...)` (default
`None`). `start_all_sessions` constructs `GmslTscSync(**gmsl_tsc_sync)`
when not `None` and passes it to the controller as a new
`gmsl_sync=` kwarg (default `None`; injectable factory for tests, same
pattern as `sync_setter`/`device_lookup`).

`MultiCameraSessionController.start_all()`:

1. dual-panel count check (existing)
2. hardware resets (existing)
3. SDK genlock role loop (existing; a no-op here since every
   `inter_cam_sync_value` is `None`)
4. **new:** `if self._gmsl_sync: self._gmsl_sync.engage()` - on exception,
   `_reset_genlock_roles()` and re-raise; no thread is started
5. start camera threads (existing staggered loop)

`MultiCameraSessionController._on_thread_finished`, once all threads are
done: `_reset_genlock_roles()` (existing) then **new**
`self._gmsl_sync.disengage()` - only after every thread's own `finished`,
never from `stop_all()`, for the same reason as `_reset_genlock_roles`
(the pipelines may still be mid-stop).

`engage()` runs on the GUI thread and blocks for `settle_s` - the same
place and style as the existing `hardware_reset_settle_s` and
`camera_start_stagger_s` sleeps in `start_all()`.

**App exit safety:** `main.py`, after `app.exec()` returns (next to the
existing `panel_rpc_client.close()`), calls a best-effort
`engine.gmsl_sync.stop_tsc_best_effort()` when
`panel_connection.mode == "remote"` and `/dev/cdi_tsc` exists, so closing
the window mid-run does not leave the generator running. The kernel mode
is not restored there (no live device state is known at that point); the
next run's `apply_sync_mode` reads and handles whatever is found.

## 4. Error handling

- Any failure in `engage()` (node resolution, out-of-range value, write,
  readback mismatch, TSC ioctl, `v4l2-ctl` missing) -> clear
  `RuntimeError`, everything already applied is undone, no thread starts.
  This matches the controller's existing all-or-nothing genlock rule and
  the project's "fail loudly, never a silent partial run" convention.
- `disengage()` never raises - it cannot be allowed to suppress
  `all_sessions_finished`.
- A permission error on `/dev/cdi_tsc` surfaces the same hint the script
  prints (udev rule `KERNEL=="cdi_tsc", MODE="0666"`).

## 5. Out of scope

- Laser/projector control (the existing emitter camera control covers it)
- Link/device-tree snapshots, dmesg windows, reports, plots
- Re-applying the (volatile) mode between repeated starts within one run -
  each Start engages fresh
- Any Windows-side / RPC change - the trigger is local to the Orin

## 6. Testing

Unit (no hardware, runs on Windows CI):

- `detect_gmsl_tsc_rig` truth table: each of the 5 conditions false in
  turn; a lookup that raises -> False
- `resolve_sync_nodes`: `-md-` filtering; udev-symlink hit; fallback to
  full scan; != 2 nodes -> RuntimeError
- `-L`/`-C` parsing on real captured driver output (menu-label form)
- `apply_sync_mode`: success returns as-found; out of range -> no write;
  write failure / readback mismatch -> earlier node restored + raise
- `GmslTscSync.engage` order: resolve -> mode -> TSC start -> sleep; TSC
  failure restores the mode; `disengage` order and idempotence
- default `tsc_io` against a fake ioctl/fd: exact ioctl sequence for
  start/stop, fd always closed
- controller: `engage` before any thread starts; failure -> no threads,
  genlock reset; `disengage` only after every thread finished; `None` ->
  behavior byte-for-byte unchanged
- `MainWindow`: ticked checkbox forces `inter_cam_sync_value=None`, skips
  the slave-color check, blocks on fps mismatch; unticked/hidden -> no
  `gmsl_tsc_sync` passed
- `CameraHubPage`: hidden when unavailable, pre-ticked on becoming
  available, operator untick survives a re-refresh

Real hardware (Orin, 2x D585 GMSL) - first thing to verify:

1. a GMSL D585 really reports no `usb_type_descriptor` (if wrong, only
   detection rule 4 changes)
2. both nodes resolve and read back `camera_sync_mode = 2`
3. with the checkbox ticked, both cameras deliver frames at the trigger
   rate; unticked, the run behaves as today
4. after the run, `camera_sync_mode` is back to its as-found value and
   `ext_sync_gen.py` reports nothing running
