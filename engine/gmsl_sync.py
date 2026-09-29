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
_DEFAULT_RE = re.compile(r"\bdefault=(-?\d+)")
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


def control_default(text, name):
    line = _control_line(text, name)
    if line is None:
        return None
    match = _DEFAULT_RE.search(line)
    return int(match.group(1)) if match else None


def _trailing_index(path):
    match = re.search(r"(\d+)$", path)
    return int(match.group(1)) if match else 1 << 30


def _nodes_with_control(candidates, control, run_v4l2, errors):
    found = []
    for node in candidates:
        code, out, err = run_v4l2(node, "-L")
        if code == 127:
            # The binary itself is missing - every other node would fail
            # identically, so stop here with the one actionable message.
            raise RuntimeError(err)
        if code != 0:
            errors.append("{}: {}".format(node, err or "v4l2-ctl -L exited {}".format(code)))
            continue
        if control in controls_in(out):
            found.append(node)
    return found


def resolve_sync_nodes(control, run_v4l2=run_v4l2, glob_fn=glob.glob, expected=2, allow_none=False):
    """The /dev nodes carrying `control`: librealsense udev symlinks first
    (metadata nodes excluded), then every /dev/videoN. With expected=2 (a
    synced run) exactly 2 or RuntimeError - assigned in /dev order, which
    node is which camera does not matter since both cameras get the SAME
    value on D500. With expected=None (the free-run cleanup) every node
    that carries it, at least 1 - a leftover mode must be cleared on EVERY
    GMSL camera, however many are attached. allow_none (with
    expected=None) returns [] instead of raising when no node carries it -
    the app-launch cleanup on an Orin with no GMSL camera attached."""
    def enough(found):
        return len(found) == expected if expected is not None else len(found) >= 1

    errors = []
    symlinks = sorted((n for n in glob_fn("/dev/video-rs-*") if _METADATA_MARKER not in n),
                      key=_trailing_index)
    found = _nodes_with_control(symlinks, control, run_v4l2, errors)
    if not enough(found):
        scanned = sorted(glob_fn("/dev/video[0-9]*"), key=_trailing_index)
        found = _nodes_with_control(scanned, control, run_v4l2, errors)
    if not found and expected is None and allow_none:
        return []
    if not enough(found):
        raise RuntimeError(
            "Cannot place V4L2 control {!r}: {} node(s) expose it ({}), expected {}.{}".format(
                control, len(found), ", ".join(found) or "none",
                expected if expected is not None else "at least 1",
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
    """Best-effort: writes each node's as-found value back and reads it
    back. A node whose as-found value is None is skipped - there is nothing
    known to restore it to. Never raises (same convention as
    MultiCameraSessionController._reset_genlock_roles); returns one message
    per node that could not be restored, so the caller can tell the
    operator the next run may start still externally synced."""
    problems = []
    for node, value in as_found.items():
        if value is None:
            continue
        try:
            code, out, err = run_v4l2(node, "-c", "{}={}".format(control, value))
            readback = _read_value(node, control, run_v4l2) if code == 0 else None
        except Exception as exc:
            problems.append("{}: {}".format(node, exc))
            continue
        if code != 0:
            problems.append("{}: writing {}={} failed: {}".format(
                node, control, value, err or out or "exit {}".format(code)))
        elif readback != value:
            problems.append("{}: {} reads back {}, not {}".format(node, control, readback, value))
    return problems


def apply_sync_mode(nodes, control, value, run_v4l2=run_v4l2):
    """Reads each node's as-found value, range-checks `value` against the
    driver's own min/max BEFORE writing anything, then writes and reads
    back each node. Any write failure or readback mismatch restores every
    node already written and raises RuntimeError. Returns
    {node: value_to_restore_or_None} - normally the as-found value, but the
    driver's own default (0 if unknown) when a node was ALREADY in `value`
    (almost always a killed earlier run's leftover - restoring it as-found
    would leave the camera stuck in external sync forever) or when its
    value could not be read at all (restoring nothing would leave it at
    `value` after the run)."""
    as_found = {}
    for node in nodes:
        code, listing, err = run_v4l2(node, "-L")
        limits = control_range(listing, control) if code == 0 else None
        if limits is not None and not limits[0] <= value <= limits[1]:
            raise RuntimeError("{} on {} accepts {}..{}, so {} cannot be written".format(
                control, node, limits[0], limits[1], value))
        found = _read_value(node, control, run_v4l2)
        if found is None or found == value:
            default = control_default(listing, control) if code == 0 else None
            found = default if default is not None else 0
        as_found[node] = found

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
    # Both from the reference check_d585_sync_v4l2.py: projector off via
    # V4L2 for the run (its default), and the first seconds of global
    # timestamps left out of Global TS Latency while librealsense's
    # device-to-host clock fit converges (measured there: -2.1 ms in the
    # first second, +61 us after ten).
    "laser_off": True,
    "global_ts_skip_s": 10.0,
    # Real-hardware finding (2026-09-29, 24 app runs vs the reference on the
    # same rig): with the app's old sequence the cameras never locked - each
    # Start left them a random, fixed offset apart (0.3-23.5 ms, like the
    # reference's no-TSC phase), while the reference locked to ~0.1 ms on
    # every start. These three follow the reference's sequence; each can be
    # switched back to find which one matters.
    # tsc_before_mode: start the TSC and wait settle_s BEFORE switching the
    # cameras to external sync (the reference order); False = the old order
    # (mode first, then TSC, then settle).
    "tsc_before_mode": True,
    # enable_depth: co-enable depth with IR in ticked runs (the reference
    # runs IR + color only). Overrides camera_sync.enable_depth_for_ir_sync
    # for ticked runs only.
    "enable_depth": False,
    # start_cameras_back_to_back: open each camera's stream right after the
    # previous camera's stream is open (the reference opens them back to
    # back) instead of the fixed 2 s USB stagger.
    "start_cameras_back_to_back": True,
}

LASER_CONTROL = "laser_power_on_off"

CDI_TSC_DEV = "/dev/cdi_tsc"

# Every GmslTscSync currently engaged in this process, so app exit can
# undo exactly what this app applied (disengage_all_engaged) - and never
# touch a trigger someone started by hand with ext_sync_gen.py.
_ENGAGED = []


def unknown_gmsl_tsc_settings_keys(section):
    """Keys under settings.yaml's camera_sync.gmsl_tsc_sync that
    GmslTscSync doesn't take, in order - a typo there would otherwise
    surface as a raw TypeError at Start."""
    return [key for key in (section or {}) if key not in DEFAULT_GMSL_TSC_SYNC]


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
        except FileNotFoundError:
            raise RuntimeError("{} not found (TSC driver not loaded?)".format(ext.CDI_TSC_DEV))
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
    """engage(), reference order (tsc_before_mode, the default): resolve the
    two nodes -> start the TSC -> wait settle_s -> external-sync mode on both
    (read-back confirmed) -> projector off; the streams open right after.
    Old order (tsc_before_mode=False): mode -> TSC -> settle. Any failure
    undoes what was applied and raises. disengage(): stop TSC, restore
    as-found mode; best-effort, idempotent, never raises."""

    def __init__(self, control, sync_mode_value, fps, duty_percent, settle_s, laser_off=True,
                 global_ts_skip_s=10.0, tsc_before_mode=True, enable_depth=False,
                 start_cameras_back_to_back=True, run_v4l2=run_v4l2, tsc_io=None, sleep=time.sleep,
                 glob_fn=glob.glob):
        self._control = control
        self._laser_off = laser_off
        self._tsc_before_mode = tsc_before_mode
        # Not used here - read by the page (enable_depth -> each camera
        # thread's enable_depth_for_ir_sync) and the controller
        # (start_cameras_back_to_back) for this run.
        self.enable_depth = enable_depth
        self.start_cameras_back_to_back = start_cameras_back_to_back
        # Not used here - the controller hands it to CrossCameraReconciler
        # (Global TS Latency warm-up exclusion) for this run.
        self.global_ts_skip_s = global_ts_skip_s
        self._laser_as_found = None
        # Non-fatal notes (laser could not be switched off/confirmed) - a
        # projector left on changes the IR image, not the sync, so it never
        # blocks a run; the page shows these with the verified status.
        self.warnings = []
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
        self._nodes = []

    @property
    def nodes(self):
        return list(self._nodes)

    def engage(self):
        nodes = resolve_sync_nodes(self._control, run_v4l2=self._run_v4l2, glob_fn=self._glob_fn)
        self._nodes = list(nodes)
        self.warnings = []
        if self._tsc_before_mode:
            # The reference check_d585_sync_v4l2.py's order: the trigger is
            # already running and settled when the cameras are switched to
            # external sync, and the streams open right after that.
            self._start_tsc()
            self._settle()
            try:
                self._apply_modes()
            except Exception:
                self._stop_tsc_quietly()
                raise
        else:
            self._apply_modes()
            try:
                self._start_tsc()
            except Exception:
                self._undo_modes()
                raise
            self._settle()

    def _apply_modes(self):
        self._as_found = apply_sync_mode(self._nodes, self._control, self._value, run_v4l2=self._run_v4l2)
        if self._laser_off:
            self._switch_laser_off()

    def _undo_modes(self):
        if self._as_found is not None:
            restore_sync_mode(self._as_found, self._control, run_v4l2=self._run_v4l2)
            self._as_found = None
        if self._laser_as_found:
            restore_sync_mode(self._laser_as_found, LASER_CONTROL, run_v4l2=self._run_v4l2)
        self._laser_as_found = None

    def _start_tsc(self):
        self._tsc_io.start(self._fps, self._duty)
        self._tsc_running = True
        if self not in _ENGAGED:
            _ENGAGED.append(self)

    def _stop_tsc_quietly(self):
        if self in _ENGAGED:
            _ENGAGED.remove(self)
        if self._tsc_running:
            self._tsc_running = False
            try:
                self._tsc_io.stop()
            except Exception:
                pass

    def _settle(self):
        if self._settle_s > 0:
            self._sleep(self._settle_s)

    def _switch_laser_off(self):
        """laser_power_on_off=0 on every sync node (same node and driver, per
        the reference), remembering as-found values for disengage."""
        self._laser_as_found = {}
        for node in self._nodes:
            found = _read_value(node, LASER_CONTROL, self._run_v4l2)
            if found is None:
                self.warnings.append("{}: no readable {} - projector state unknown".format(
                    node, LASER_CONTROL))
                continue
            self._laser_as_found[node] = found
            if found == 0:
                continue
            code, out, err = self._run_v4l2(node, "-c", "{}=0".format(LASER_CONTROL))
            if code != 0 or _read_value(node, LASER_CONTROL, self._run_v4l2) != 0:
                self.warnings.append("{}: could not switch the projector off ({})".format(
                    node, err or out or "readback mismatch"))

    def verify(self):
        """Called once every camera is actually streaming: engage()'s own
        readback happens BEFORE any stream opens, so it can't catch a
        stream start (librealsense opening the device) knocking the mode
        back. Re-reads every engaged node and returns one message per
        problem - empty means both cameras are still in the sync mode with
        the trigger this process started. The TSC has no GET ioctl, so
        "running" is what this process knows it started, not a hardware
        readback; frames arriving at all under external sync (the
        controller's own check) is the hardware evidence for the trigger."""
        problems = []
        if not self._nodes:
            problems.append("GMSL sync was never engaged")
        if not self._tsc_running:
            problems.append("the TSC trigger is not running")
        for node in self._nodes:
            value = _read_value(node, self._control, self._run_v4l2)
            if value != self._value:
                problems.append("{} on {} reads {} after the streams opened, expected {}".format(
                    self._control, node, "nothing" if value is None else value, self._value))
        if self._laser_off:
            # The driver can turn the projector back on at stream start
            # (the reference re-applies it before every start for this) -
            # switch it off again now that the streams are open.
            for node in self._laser_as_found or {}:
                if _read_value(node, LASER_CONTROL, self._run_v4l2) == 0:
                    continue
                code, out, err = self._run_v4l2(node, "-c", "{}=0".format(LASER_CONTROL))
                if code != 0 or _read_value(node, LASER_CONTROL, self._run_v4l2) != 0:
                    self.warnings.append("{}: projector came back on after the stream started and "
                                         "could not be switched off".format(node))
        return problems

    def disengage(self):
        """Returns one message per thing that could not be undone (empty
        when everything was) - never raises."""
        problems = []
        if self in _ENGAGED:
            _ENGAGED.remove(self)
        if self._tsc_running:
            self._tsc_running = False
            try:
                self._tsc_io.stop()
            except Exception as exc:
                problems.append("TSC trigger could not be stopped: {}".format(exc))
        if self._as_found is not None:
            as_found, self._as_found = self._as_found, None
            problems.extend(restore_sync_mode(as_found, self._control, run_v4l2=self._run_v4l2))
        if self._laser_as_found:
            laser_as_found, self._laser_as_found = self._laser_as_found, None
            problems.extend(restore_sync_mode(laser_as_found, LASER_CONTROL, run_v4l2=self._run_v4l2))
        return problems


def reset_leftover_sync(control, run_v4l2=run_v4l2, tsc_io=None, glob_fn=glob.glob, allow_no_nodes=False):
    """Makes the rig genuinely free-running before an UNTICKED run: any
    node not at the driver's default is written back to it (read-back
    confirmed), and the TSC is always stopped - it has no GET ioctl, so a
    trigger still pulsing from a killed ticked run cannot be detected, only
    stopped. Raises RuntimeError if either step fails: a baseline that is
    secretly still synced is worse than no run. Covers EVERY node carrying
    the control (not just two), so it also works for a single-camera run or
    a rig with a third GMSL camera attached. Returns the nodes reset.
    allow_no_nodes: no node carrying the control is not an error (the TSC
    is still stopped) - for the app-launch cleanup."""
    nodes = resolve_sync_nodes(control, run_v4l2=run_v4l2, glob_fn=glob_fn, expected=None,
                               allow_none=allow_no_nodes)
    reset_nodes = []
    for node in nodes:
        code, listing, err = run_v4l2(node, "-L")
        default = control_default(listing, control) if code == 0 else None
        default = 0 if default is None else default
        if _read_value(node, control, run_v4l2) == default:
            continue
        code, out, err = run_v4l2(node, "-c", "{}={}".format(control, default))
        if code != 0 or _read_value(node, control, run_v4l2) != default:
            raise RuntimeError(
                "{} on {} is left over from a previous run and could not be reset to {} ({}) - "
                "a free-running run would still be externally synced".format(
                    control, node, default, err or out or "readback mismatch"))
        reset_nodes.append(node)
    try:
        (tsc_io or KernelTscIO()).stop()
    except Exception as exc:
        raise RuntimeError("Could not stop the TSC trigger before a free-running run: {}".format(exc))
    return reset_nodes


def clear_leftover_sync_at_startup(panel_connection, control, run_v4l2=run_v4l2, tsc_io=None,
                                   glob_fn=glob.glob, path_exists=os.path.exists):
    """App launch on the Orin (remote panel mode, /dev/cdi_tsc present):
    clears what a killed earlier GMSL-synced run left behind - cameras still
    in external-sync mode and a trigger possibly still pulsing - BEFORE
    Stream Config's preview, ROI Select, Calibration or Threshold Tuning
    open any stream. None of those free-running pages checks the sync mode,
    and a camera left in external sync with no trigger delivers no frames
    there. Same reset as the unticked-run guard (reset_leftover_sync), so it
    also stops a trigger started by hand with ext_sync_gen.py: on this rig
    the app owns the TSC. No GMSL camera attached is not an error. Returns
    the nodes reset ([] when there was nothing to do, or off the Orin);
    raises RuntimeError when the cleanup itself fails."""
    if (panel_connection or {}).get("mode") != "remote" or not path_exists(CDI_TSC_DEV):
        return []
    return reset_leftover_sync(control, run_v4l2=run_v4l2, tsc_io=tsc_io, glob_fn=glob_fn,
                               allow_no_nodes=True)


class GmslFreeRunGuard:
    """Controller-compatible (engage/disengage) wrapper around
    reset_leftover_sync for an unticked run on the detected GMSL rig - rides
    MultiCameraSessionController's existing gmsl_sync slot, so it runs
    before any camera thread, all-or-nothing. disengage() is a no-op:
    nothing was applied that needs undoing."""

    def __init__(self, control, run_v4l2=run_v4l2, tsc_io=None, glob_fn=glob.glob):
        self._control = control
        self._run_v4l2 = run_v4l2
        self._tsc_io = tsc_io
        self._glob_fn = glob_fn
        self.reset_nodes = []

    def engage(self):
        self.reset_nodes = reset_leftover_sync(self._control, run_v4l2=self._run_v4l2,
                                               tsc_io=self._tsc_io, glob_fn=self._glob_fn)

    def disengage(self):
        return []


def disengage_all_engaged():
    """App-exit safety net: disengage (TSC off, as-found mode restored)
    every GmslTscSync this process engaged and never disengaged - e.g. the
    window closed mid-run (MainWindow.closeEvent first waits for every
    camera thread, so this never rewrites the mode under a live stream). A camera left in external-sync mode with no
    trigger delivers no frames on the next free-running run. Never raises
    (disengage itself never does)."""
    for sync in list(_ENGAGED):
        sync.disengage()


def _is_gmsl_d585(device):
    return ("D585" in device.get_info(rs.camera_info.name)
            and not device.supports(rs.camera_info.usb_type_descriptor))


def detect_gmsl_camera(panel_connection, serial, device_lookup, path_exists=os.path.exists):
    """True for one D585 on the Orin's GMSL deserializer: remote panel
    mode, /dev/cdi_tsc present, D585, no USB descriptor. Used to self-heal
    a leftover external-sync mode before a SINGLE-camera run, which
    detect_gmsl_tsc_rig (exactly 2 cameras) never covers. Any lookup
    failure means False."""
    if (panel_connection or {}).get("mode") != "remote" or not path_exists(CDI_TSC_DEV):
        return False
    try:
        return _is_gmsl_d585(device_lookup(serial))
    except Exception:
        return False


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
        return all(_is_gmsl_d585(device_lookup(serial)) for serial in serials)
    except Exception:
        return False
