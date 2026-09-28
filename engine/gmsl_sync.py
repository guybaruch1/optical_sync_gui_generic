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
