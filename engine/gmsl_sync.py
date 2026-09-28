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
import importlib
import os
import re
import subprocess
import time

import pyrealsense2 as rs

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
