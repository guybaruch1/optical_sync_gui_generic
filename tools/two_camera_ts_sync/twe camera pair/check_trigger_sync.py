#!/usr/bin/env python3
"""Does the TSC trigger actually sync the two cameras? - STANDALONE.

A companion to capture_ts_sync.py, not a replacement. That script pairs
frames across the cameras and reports the gap between the paired frames.
This one answers a narrower question - IS THE TRIGGER DRIVING THE CAMERAS -
and does it WITHOUT pairing any frames at all.

WHY NO PAIRING
    A "gap" between two cameras normally means: pick one frame from each and
    subtract. Picking is the hard part. On this rig it went wrong constantly -
    the cameras' frame counters differ by a constant that changes when one
    camera misses pulses, and a match window wide enough to cope pairs a
    frame with its NEIGHBOUR one period away, reporting a ~33ms gap while the
    physical relationship barely moved.

    So nothing here pairs. Instead every frame is reduced to its POSITION
    INSIDE ONE TRIGGER CYCLE - "how long after the tick was this taken?" -
    which is a per-frame, per-camera number needing no counterpart. Comparing
    the two cameras' positions gives the sync error directly, bounded to half
    a period by construction, immune to dropped frames, stalls, counter
    mismatches and off-by-one errors.

WHY NO THREADS
    One loop polls both pipelines with poll_for_frames() instead of blocking
    on wait_for_frames(). That is only safe because nothing is paired: a
    missed frame costs nothing here, since each frame is measured on its own.
    It also means one camera stalling cannot stop the other from recording.

THE THREE MODES (each captured in turn, then compared)
    mode0_trigger_off   inter_cam_sync_mode=0, TSC disabled. Baseline: what
                        the cameras do on their own.
    mode0_trigger_on    inter_cam_sync_mode=0, TSC enabled. Does the trigger
                        change anything even when the cameras are NOT slaved?
    mode2_trigger_on    inter_cam_sync_mode=2, TSC enabled. The intended
                        configuration.

    Comparing the first two isolates the trigger; comparing to the third
    isolates what slave mode adds. On this rig the cameras' own clock ticks
    ~33316us (30.0156 Hz) while the TSC ticks 33333us (30.0000 Hz), so a
    camera that starts following the trigger is visible in its frame rate
    alone - no cross-camera maths needed.

WHAT IT REPORTS, PER MODE
    Per camera:  frame count, median interval and Hz, and FRAME GAPS - holes
                 in the stream, split by whether the frame numbers skipped
                 (captured but not delivered) or not (never captured, i.e.
                 missed trigger pulses).
    Per camera:  position in the trigger cycle - steady means locked to the
                 trigger, creeping means running on its own clock, and the
                 creep rate says by how much.
    Cross:       Global TS gap and HW TS gap over time, both pairing-free.

RUN (on the Jetson; ext_sync_gen.py in the same directory is found for you)
    python check_trigger_sync.py
    python check_trigger_sync.py --duration 20 --stream color:0:1280x720@30
    python check_trigger_sync.py --help

Ctrl+C stops cleanly and still writes the CSV and the plot.
"""

import argparse
import csv
import os
import subprocess
import sys
import time
from dataclasses import dataclass

ROLE_CAM1 = "CAM1"
ROLE_CAM2 = "CAM2"
ROLES = (ROLE_CAM1, ROLE_CAM2)

STREAM_TYPE_ALIASES = {
    "ir": "infrared", "infrared": "infrared",
    "rgb": "color", "color": "color",
    "depth": "depth",
}

SYNC_MODE_DEFAULT = 0
SYNC_MODE_SLAVE = 2

# (label, inter_cam_sync_mode, trigger enabled)
MODES = (
    ("mode0_trigger_off", SYNC_MODE_DEFAULT, False),
    ("mode0_trigger_on", SYNC_MODE_DEFAULT, True),
    ("mode2_trigger_on", SYNC_MODE_SLAVE, True),
)

# Seconds allowed for librealsense's global-time regression to converge.
GLOBAL_TS_GRACE_S = 5.0

# A hole in the frame stream: an interval this many times the median.
STALL_FACTOR = 2.0

# Dark-theme chart colors, lifted from domain/plot_theme.py (itself the
# dataviz skill's validated dark palette) rather than imported, to keep this
# file standalone.
SURFACE = "#1a1a19"
GRIDLINE = "#2c2c2a"
MUTED_TEXT = "#898781"
GLOBAL_GAP_COLOR = "#4a7fe0"
HW_GAP_COLOR = "#3fbf9e"
CAM1_COLOR = "#e08a3f"
CAM2_COLOR = "#9b59d0"


# --------------------------------------------------------------------------
# Pure logic - unit-tested in tests/tools/two_camera_ts_sync/
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class StreamSpec:
    type_name: str
    index: int
    width: int
    height: int
    fps: int

    @property
    def label(self):
        return "{}{}".format(self.type_name, self.index or "")

    def describe(self):
        return "{} {}x{}@{}".format(self.label, self.width, self.height, self.fps)


def parse_stream_spec(text):
    parts = text.split(":")
    if len(parts) != 3:
        raise ValueError("bad --stream {!r}: expected TYPE:INDEX:WIDTHxHEIGHT@FPS, "
                         "e.g. ir:1:1280x720@30".format(text))
    type_text, index_text, geometry = parts
    canonical = STREAM_TYPE_ALIASES.get(type_text.strip().lower())
    if canonical is None:
        raise ValueError("bad --stream {!r}: unknown stream type {!r}".format(text, type_text))
    try:
        resolution, fps_text = geometry.split("@")
        width_text, height_text = resolution.lower().split("x")
        return StreamSpec(canonical, int(index_text), int(width_text),
                          int(height_text), int(fps_text))
    except ValueError:
        raise ValueError("bad --stream {!r}: expected TYPE:INDEX:WIDTHxHEIGHT@FPS, "
                         "e.g. ir:1:1280x720@30".format(text))


@dataclass
class FrameRecord:
    camera_role: str
    serial: str
    stream: str
    frame_number: int
    hw_ts_us: float
    global_ts_us: float
    host_recv_s: float


def wrap_half(value, period_us):
    """Folds a difference into +/- half a period.

    This is what makes every cross-camera number here trustworthy: a camera a
    whole pulse behind reads ~0 rather than 33333, because on a shared pulse
    train both cameras fire on every pulse and "one pulse apart" is not a
    real timing difference. It also makes a nonsense value impossible - the
    result cannot exceed half a period.
    """
    half = period_us / 2.0
    folded = value % period_us
    if folded > half:
        folded -= period_us
    return folded


def phase_of(ts_us, period_us):
    """Where inside one trigger cycle this timestamp falls - "how long after
    the tick". A per-frame number needing no counterpart on the other
    camera."""
    return ts_us % period_us


def _median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def frame_gap_report(records, period_us, stall_factor=STALL_FACTOR):
    """Cadence and holes for one camera.

    The cadence alone answers "is this camera following the trigger", because
    the two clocks are distinguishable: ~33316us is the camera's own
    (30.0156 Hz), 33333us is the TSC's (30.0000 Hz).

    A hole is classified by whether the frame NUMBERS skipped across it,
    which matters a lot:
      numbers skipped  -> the camera captured those frames and the transport
                          dropped them. Harmless to timing.
      numbers did NOT  -> the camera never captured them, i.e. it missed
                          trigger pulses. This is what shifts two cameras
                          out of step relative to each other.
    """
    ordered = sorted(records, key=lambda record: record.host_recv_s)
    if len(ordered) < 2:
        return {"count": len(ordered), "median_interval_us": None, "hz": None,
                "distinct_us": [], "stalls": []}

    origin = ordered[0].host_recv_s
    deltas = [(index,
               ordered[index + 1].hw_ts_us - ordered[index].hw_ts_us,
               ordered[index + 1].frame_number - ordered[index].frame_number)
              for index in range(len(ordered) - 1)]
    median = _median([delta for _, delta, _ in deltas])
    stalls, cadence = [], []
    for index, delta, number_step in deltas:
        if delta > stall_factor * median:
            stalls.append({
                "index": index,
                "at_s": ordered[index].host_recv_s - origin,
                "gap_us": delta,
                "frames_missed": delta / period_us,
                "numbers_skipped": number_step - 1,
                "not_captured": number_step == 1,
            })
        else:
            cadence.append(delta)
    return {
        "count": len(ordered),
        "median_interval_us": median,
        "hz": 1_000_000.0 / median if median else None,
        "distinct_us": sorted({round(delta) for delta in cadence}),
        "stalls": stalls,
    }


def cross_gap_series(records_a, records_b, period_us, attr):
    """The cross-camera gap over time, WITHOUT pairing frames.

    Each camera's frames are reduced to their position in the trigger cycle
    first, then compared. Because both sides are already reduced modulo the
    period, which frame of A a given frame of B is looked up against cannot
    change the answer - a neighbour has essentially the same position. That
    is the whole difference from timestamp matching, where an off-by-one
    changed the result by a full frame period.

    Returns [(elapsed_s, gap_us), ...] with gap_us wrapped to +/- half a
    period. Frames missing the requested timestamp (global time before it
    converges) are skipped.
    """
    usable_a = sorted((record for record in records_a if getattr(record, attr) is not None),
                      key=lambda record: record.host_recv_s)
    usable_b = sorted((record for record in records_b if getattr(record, attr) is not None),
                      key=lambda record: record.host_recv_s)
    if not usable_a or not usable_b:
        return []

    origin = min(usable_a[0].host_recv_s, usable_b[0].host_recv_s)
    series, cursor = [], 0
    for record in usable_b:
        # Walk forward to A's frame closest in host time. Coarse on purpose -
        # it only picks WHICH cycle to compare within, never the value.
        while (cursor + 1 < len(usable_a)
               and abs(usable_a[cursor + 1].host_recv_s - record.host_recv_s)
               <= abs(usable_a[cursor].host_recv_s - record.host_recv_s)):
            cursor += 1
        gap = wrap_half(phase_of(getattr(record, attr), period_us)
                        - phase_of(getattr(usable_a[cursor], attr), period_us),
                        period_us)
        series.append((record.host_recv_s - origin, gap))
    return series


def phase_series(records, period_us, attr="global_ts_us"):
    """One camera's position in the trigger cycle over time."""
    usable = sorted((record for record in records if getattr(record, attr) is not None),
                    key=lambda record: record.host_recv_s)
    if not usable:
        return []
    origin = usable[0].host_recv_s
    return [(record.host_recv_s - origin, phase_of(getattr(record, attr), period_us))
            for record in usable]


def slide_rate_us_per_s(series, period_us=None):
    """How fast a camera's position in the cycle is creeping, in us/s.

    Zero means the camera is locked to the trigger. Non-zero means it is
    running on its own clock, and the value IS the rate difference: a camera
    at 30.0156 Hz against a 30.0000 Hz trigger creeps ~520us per second.

    The phase has to be UNWRAPPED before fitting. A creeping camera runs off
    the end of a cycle and reappears at the other side, so the raw series is
    a sawtooth - a line fitted straight through it measures the sawtooth, not
    the creep. Each jump larger than half a period is treated as a roll-over
    and compensated.
    """
    if len(series) < 2:
        return None
    if period_us is None:
        # Infer it: the phase never exceeds one period, so the largest value
        # seen is a lower bound close enough to detect roll-overs.
        period_us = max(value for _, value in series) or 1.0
    half = period_us / 2.0

    times = [point[0] for point in series]
    unwrapped, roll = [series[0][1]], 0.0
    for index in range(1, len(series)):
        step = series[index][1] - series[index - 1][1]
        if step > half:
            roll -= period_us
        elif step < -half:
            roll += period_us
        unwrapped.append(series[index][1] + roll)

    mean_t = sum(times) / len(times)
    mean_v = sum(unwrapped) / len(unwrapped)
    denominator = sum((t - mean_t) ** 2 for t in times)
    if denominator == 0:
        return None
    return sum((t - mean_t) * (v - mean_v)
               for t, v in zip(times, unwrapped)) / denominator


def center_series(series):
    """Shifts a series so its median sits at zero.

    Used for the HW TS gap: the two devices' clock epochs are unrelated, so
    modulo the period their difference carries an arbitrary constant. Only
    the SHAPE is meaningful, and centring makes that readable instead of
    hiding it behind an offset like -14956us.
    """
    if not series:
        return []
    middle = _median([value for _, value in series])
    return [(t, value - middle) for t, value in series]


def summarize(values):
    clean = [value for value in values if value is not None]
    if not clean:
        return {"count": 0, "mean": None, "min": None, "max": None}
    return {"count": len(clean), "mean": sum(clean) / len(clean),
            "min": min(clean), "max": max(clean)}


def format_mode_report(mode_label, records, period_us):
    """The human-facing block for one mode."""
    lines = ["", "=" * 78, "MODE: {}".format(mode_label.upper()), "=" * 78]
    per_role = {role: [r for r in records if r.camera_role == role] for role in ROLES}

    for role in ROLES:
        report = frame_gap_report(per_role[role], period_us)
        lines.append("  {} - {} frames".format(role, report["count"]))
        if report["median_interval_us"] is None:
            lines.append("      no cadence (too few frames)")
            continue
        lines.append("      interval  {:.0f} us  ({:.4f} Hz)   seen: {}".format(
            report["median_interval_us"], report["hz"],
            ", ".join(str(value) for value in report["distinct_us"][:6])))
        if report["stalls"]:
            for stall in report["stalls"]:
                lines.append("      FRAME GAP at {:.1f}s: {:.0f} us (~{:.1f} frames) - {}".format(
                    stall["at_s"], stall["gap_us"], stall["frames_missed"],
                    "never captured (missed trigger pulses)" if stall["not_captured"]
                    else "captured but not delivered ({} numbers skipped)".format(
                        stall["numbers_skipped"])))
        else:
            lines.append("      no frame gaps")
        phases = phase_series(per_role[role], period_us)
        slide = slide_rate_us_per_s(phases, period_us)
        if slide is not None:
            verdict = "LOCKED to trigger" if abs(slide) < 2.0 else "sliding - own clock"
            lines.append("      cycle position creeps {:+.1f} us/s  -> {}".format(slide, verdict))

    count_a, count_b = len(per_role[ROLE_CAM1]), len(per_role[ROLE_CAM2])
    lines.append("  frame count difference: {:+d}".format(count_b - count_a))

    for attr, name in (("global_ts_us", "Global TS gap"),
                       ("hw_ts_us", "HW TS gap (centred)")):
        series = cross_gap_series(per_role[ROLE_CAM1], per_role[ROLE_CAM2], period_us, attr)
        if attr == "hw_ts_us":
            # Unrelated clock epochs make the absolute value arbitrary; only
            # the shape means anything, so centre it.
            series = center_series(series)
        stats = summarize([value for _, value in series])
        if stats["count"] == 0:
            lines.append("  {:<16} no data".format(name))
        else:
            lines.append("  {:<16} n={:<5} mean={:+.1f} us  min={:+.1f}  max={:+.1f}".format(
                name, stats["count"], stats["mean"], stats["min"], stats["max"]))
    return lines


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------

CSV_COLUMNS = ["mode", "camera_role", "serial", "stream", "frame_number",
               "hw_ts_us", "global_ts_us", "host_recv_s"]


def write_frames_csv(path, records_by_mode):
    """Every frame from every camera in every mode, unpaired - the ground
    truth behind every number in the report."""
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for mode_label, records in records_by_mode.items():
            for record in records:
                writer.writerow({
                    "mode": mode_label,
                    "camera_role": record.camera_role,
                    "serial": record.serial,
                    "stream": record.stream,
                    "frame_number": record.frame_number,
                    "hw_ts_us": "{:.3f}".format(record.hw_ts_us),
                    "global_ts_us": ("" if record.global_ts_us is None
                                     else "{:.3f}".format(record.global_ts_us)),
                    "host_recv_s": "{:.6f}".format(record.host_recv_s),
                })


# --------------------------------------------------------------------------
# Plot
# --------------------------------------------------------------------------

def _style_axis(ax):
    ax.set_facecolor(SURFACE)
    ax.grid(True, color=GRIDLINE, alpha=0.6)
    ax.tick_params(colors=MUTED_TEXT, labelsize=8)
    ax.xaxis.label.set_color(MUTED_TEXT)
    ax.yaxis.label.set_color(MUTED_TEXT)
    ax.title.set_color(MUTED_TEXT)
    for spine in ax.spines.values():
        spine.set_color(GRIDLINE)


def export_plot(path, records_by_mode, period_us):
    """Three rows x one column per mode, each row sharing a y-axis:

      row 1  Global TS gap  - cross-camera, pairing-free
      row 2  HW TS gap      - cross-camera, pairing-free
      row 3  frame interval - per camera, the "gap of the frames"

    Rows share their y-axis across modes so the modes are directly
    comparable; without that, a tight mode and a wandering one would both
    render as equally-tall noise.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    labels = [label for label, _, _ in MODES if records_by_mode.get(label)]
    if not labels:
        return
    figure, axes = plt.subplots(3, len(labels), figsize=(6 * len(labels), 11),
                               sharey="row", squeeze=False)
    figure.patch.set_facecolor(SURFACE)

    for column, mode_label in enumerate(labels):
        records = records_by_mode[mode_label]
        per_role = {role: [r for r in records if r.camera_role == role] for role in ROLES}

        for row, (attr, name, color) in enumerate(
                (("global_ts_us", "Global TS gap (us)", GLOBAL_GAP_COLOR),
                 ("hw_ts_us", "HW TS gap, centred (us)", HW_GAP_COLOR))):
            ax = axes[row][column]
            _style_axis(ax)
            series = cross_gap_series(per_role[ROLE_CAM1], per_role[ROLE_CAM2],
                                      period_us, attr)
            if attr == "hw_ts_us":
                series = center_series(series)
            if series:
                ax.axhline(0.0, color=MUTED_TEXT, linewidth=0.8, alpha=0.5)
                ax.plot([t for t, _ in series], [v for _, v in series],
                        color=color, linewidth=1.2)
            else:
                ax.text(0.5, 0.5, "no data", transform=ax.transAxes, ha="center",
                        va="center", color=MUTED_TEXT, fontsize=11)
            stats = summarize([v for _, v in series])
            title = name if stats["count"] == 0 else "{}\nmean {:+.0f}  spread {:.0f} us".format(
                name, stats["mean"], stats["max"] - stats["min"])
            ax.set_title("{}\n{}".format(mode_label, title) if row == 0 else title, fontsize=9)
            if column == 0:
                ax.set_ylabel(name)

        ax = axes[2][column]
        _style_axis(ax)
        for role, color in ((ROLE_CAM1, CAM1_COLOR), (ROLE_CAM2, CAM2_COLOR)):
            ordered = sorted(per_role[role], key=lambda record: record.host_recv_s)
            if len(ordered) < 2:
                continue
            origin = ordered[0].host_recv_s
            points = [(ordered[i + 1].host_recv_s - origin,
                       ordered[i + 1].hw_ts_us - ordered[i].hw_ts_us - period_us)
                      for i in range(len(ordered) - 1)]
            ax.plot([t for t, _ in points], [v for _, v in points],
                    color=color, linewidth=1.0, label=role)
        ax.axhline(0.0, color=MUTED_TEXT, linewidth=0.8, alpha=0.6)
        # Deviation from the trigger period, on a symlog scale. Plotted raw
        # and linearly, a single 600ms startup stall flattens the whole
        # series - hiding the ~17us difference between the camera's own clock
        # and the TSC, which is the thing worth seeing. Linear within
        # +/-100us, logarithmic beyond, so both scales are legible at once.
        ax.set_yscale("symlog", linthresh=100.0)
        ax.set_title("frame interval minus trigger period\n"
                     "(0 = exactly on the trigger; symlog beyond +/-100us)", fontsize=9)
        ax.set_xlabel("Elapsed time (s)")
        ax.legend(facecolor=SURFACE, edgecolor=GRIDLINE, labelcolor=MUTED_TEXT, fontsize=8)
        if column == 0:
            ax.set_ylabel("frame interval (us)")

    figure.suptitle("Is the TSC trigger syncing the cameras? (no frame pairing)",
                    color=MUTED_TEXT, fontsize=13)
    figure.tight_layout(rect=(0, 0, 1, 0.95))
    figure.savefig(path, facecolor=SURFACE, dpi=110)
    plt.close(figure)


# --------------------------------------------------------------------------
# Hardware (Jetson-only; no automated tests)
# --------------------------------------------------------------------------

def _rs():
    import pyrealsense2 as rs
    return rs


def run_trigger_script(script_path, action, fps=None, duty=None):
    """Shells out to ext_sync_gen.py. --fps and --duty go together or not at
    all: that script has no GET_RATE ioctl, so passing either overwrites BOTH
    (its own default fills the other in), silently changing the rate."""
    command = [sys.executable, script_path, "--" + action]
    if action == "enable" and fps is not None and duty is not None:
        command += ["--fps", str(fps), "--duty", str(duty)]
    print("  $ {}".format(" ".join(command)))
    result = subprocess.run(command, capture_output=True, text=True)
    for line in (result.stdout or "").splitlines() + (result.stderr or "").splitlines():
        print("    {}".format(line))
    if result.returncode != 0:
        raise RuntimeError("ext_sync_gen.py --{} failed with exit code {}".format(
            action, result.returncode))


def apply_sync_mode(device, mode, role_label):
    rs = _rs()
    for sensor in device.query_sensors():
        if sensor.supports(rs.option.inter_cam_sync_mode):
            sensor.set_option(rs.option.inter_cam_sync_mode, mode)
            print("  {}: inter_cam_sync_mode set to {} (device reports {:.0f})".format(
                role_label, mode, sensor.get_option(rs.option.inter_cam_sync_mode)))
            return True
    print("  {}: WARNING - no sensor supports inter_cam_sync_mode.".format(role_label))
    return False


def enable_global_time(device, role_label, quiet=False):
    """Turns rs.option.global_time_enabled ON for every sensor supporting it.
    Not on by default everywhere: on a Jetson with D457 GMSL, frames come
    back in a non-global domain until this is set."""
    rs = _rs()
    enabled_any = False
    for sensor in device.query_sensors():
        if not sensor.supports(rs.option.global_time_enabled):
            continue
        name = sensor.get_info(rs.camera_info.name)
        try:
            sensor.set_option(rs.option.global_time_enabled, 1)
        except Exception as exc:
            print("  {} [{}]: could not enable global_time_enabled: {}".format(
                role_label, name, exc))
            continue
        readback = sensor.get_option(rs.option.global_time_enabled)
        if not quiet:
            print("  {} [{}]: global_time_enabled -> {:.0f}".format(role_label, name, readback))
        enabled_any = enabled_any or readback != 0
    return enabled_any


def _frame_for_stream(frameset, stream_spec):
    if stream_spec.type_name == "infrared":
        return frameset.get_infrared_frame(stream_spec.index)
    if stream_spec.type_name == "depth":
        return frameset.get_depth_frame()
    return frameset.get_color_frame()


def _build_config(serial, stream_spec):
    rs = _rs()
    formats = {"infrared": (rs.stream.infrared, rs.format.y8),
               "color": (rs.stream.color, rs.format.bgr8),
               "depth": (rs.stream.depth, rs.format.z16)}
    stream_type, stream_format = formats[stream_spec.type_name]
    config = rs.config()
    config.enable_device(serial)
    config.enable_stream(stream_type, stream_spec.index, stream_spec.width,
                         stream_spec.height, stream_format, stream_spec.fps)
    return config


def capture_mode(mode_label, serials, devices, stream_spec, duration_s):
    """Single-threaded capture: one loop polling both cameras.

    poll_for_frames() rather than wait_for_frames() so neither camera can
    block the other. Safe here precisely because nothing is paired - a missed
    frame costs nothing, since every measurement is per-frame.
    """
    rs = _rs()
    metadata = rs.frame_metadata_value.frame_timestamp
    global_domain = rs.timestamp_domain.global_time

    pipelines, records = {}, []
    last_number = {role: None for role in ROLES}
    warned = {role: False for role in ROLES}
    started_s = time.perf_counter()

    try:
        for role, serial in zip(ROLES, serials):
            enable_global_time(devices[role], role, quiet=True)
            pipeline = rs.pipeline()
            profile = pipeline.start(_build_config(serial, stream_spec))
            # Re-asserted on the device the STARTED pipeline resolved: the
            # handle above came from a separate rs.context() and setting an
            # option through it may not reach the streaming one.
            enable_global_time(profile.get_device(), role, quiet=True)
            pipelines[role] = pipeline
        print("  polling both cameras for {:.1f}s...".format(duration_s))

        deadline = time.perf_counter() + duration_s
        while time.perf_counter() < deadline:
            got_any = False
            for role, serial in zip(ROLES, serials):
                frameset = pipelines[role].poll_for_frames()
                if not frameset:
                    continue
                frame = _frame_for_stream(frameset, stream_spec)
                if not frame:
                    continue
                number = frame.get_frame_number()
                if last_number[role] == number:
                    continue  # poll returned the same frame again
                last_number[role] = number
                got_any = True
                if not frame.supports_frame_metadata(metadata):
                    raise RuntimeError("{} ({}): no HW timestamp metadata.".format(role, serial))
                in_global = frame.get_frame_timestamp_domain() == global_domain
                if not in_global and not warned[role]:
                    warned[role] = True
                    print("    {}: waiting for global time to converge...".format(role))
                if not in_global and time.perf_counter() - started_s > GLOBAL_TS_GRACE_S:
                    # Keep going: HW timestamps still work, and the Global
                    # panel simply stays empty rather than failing the run.
                    pass
                records.append(FrameRecord(
                    camera_role=role, serial=serial, stream=stream_spec.label,
                    frame_number=number,
                    hw_ts_us=float(frame.get_frame_metadata(metadata)),
                    global_ts_us=frame.get_timestamp() * 1000.0 if in_global else None,
                    host_recv_s=time.perf_counter()))
            if not got_any:
                time.sleep(0.001)
    except KeyboardInterrupt:
        print("\n  Interrupted - keeping what was captured.")
    finally:
        for pipeline in pipelines.values():
            try:
                pipeline.stop()
            except Exception as exc:
                print("  WARNING: pipeline.stop() failed: {}".format(exc))
    return records


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def _default_trigger_script():
    candidate = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ext_sync_gen.py")
    return candidate if os.path.exists(candidate) else None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Check whether the Jetson TSC trigger actually syncs two "
                    "RealSense cameras, without pairing any frames.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--serials", nargs=2, metavar=("CAM1", "CAM2"))
    parser.add_argument("--stream", default="ir:1:1280x720@30", metavar="SPEC",
                        help="TYPE:INDEX:WIDTHxHEIGHT@FPS")
    parser.add_argument("--duration", type=float, default=20.0,
                        help="Capture seconds PER MODE")
    parser.add_argument("--fps", type=int, default=30, help="TSC generator rate")
    parser.add_argument("--duty", type=int, default=50, help="TSC duty cycle percent")
    parser.add_argument("--trigger-script", default=_default_trigger_script(),
                        help="Path to ext_sync_gen.py; defaults to one next to this script")
    parser.add_argument("--output-dir", default=None,
                        help="Default: output/check_trigger_sync/<timestamp>")
    args = parser.parse_args(argv)
    args.stream_spec = parse_stream_spec(args.stream)
    return args


def _resolve_serials(args):
    rs = _rs()
    if args.serials:
        return list(args.serials)
    devices = list(rs.context().query_devices())
    if len(devices) != 2:
        raise RuntimeError("Need exactly 2 connected RealSense devices (found {}), or "
                           "pass --serials <CAM1> <CAM2>".format(len(devices)))
    return [device.get_info(rs.camera_info.serial_number) for device in devices]


def _find_device(serial):
    rs = _rs()
    for device in rs.context().query_devices():
        if device.get_info(rs.camera_info.serial_number) == serial:
            return device
    raise RuntimeError("No connected RealSense device with serial {}".format(serial))


def main(argv=None):
    args = parse_args(argv)
    if not args.trigger_script:
        print("No ext_sync_gen.py found next to this script; pass --trigger-script.",
              file=sys.stderr)
        return 1

    period_us = 1_000_000.0 / args.fps
    output_dir = args.output_dir or os.path.join(
        "output", "check_trigger_sync", time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(output_dir, exist_ok=True)

    serials = _resolve_serials(args)
    devices = {role: _find_device(serial) for role, serial in zip(ROLES, serials)}
    rs = _rs()
    print("Output directory: {}".format(os.path.abspath(output_dir)))
    for role in ROLES:
        device = devices[role]
        print("{}: {} (serial {}, firmware {})".format(
            role, device.get_info(rs.camera_info.name),
            device.get_info(rs.camera_info.serial_number),
            device.get_info(rs.camera_info.firmware_version)))
    print("Stream: {}   trigger period: {:.0f} us ({} Hz)".format(
        args.stream_spec.describe(), period_us, args.fps))
    print("Trigger script: {}".format(os.path.abspath(args.trigger_script)))
    print("No frame pairing, no threads.")

    print("\nEnabling RealSense global time...")
    for role in ROLES:
        enable_global_time(devices[role], role)

    records_by_mode = {}
    report_lines = []
    try:
        for mode_label, sync_mode, trigger_on in MODES:
            print("\n" + "=" * 78)
            print("MODE '{}': inter_cam_sync_mode={}, TSC {}".format(
                mode_label, sync_mode, "ENABLED" if trigger_on else "DISABLED"))
            print("=" * 78)
            for role in ROLES:
                apply_sync_mode(devices[role], sync_mode, role)
            run_trigger_script(args.trigger_script,
                               "enable" if trigger_on else "disable", args.fps, args.duty)
            records = capture_mode(mode_label, serials, devices,
                                   args.stream_spec, args.duration)
            records_by_mode[mode_label] = records
            block = format_mode_report(mode_label, records, period_us)
            report_lines += block
            print("\n".join(block))
    except KeyboardInterrupt:
        print("\nInterrupted between modes - keeping what was captured.")
    finally:
        print("\nCleaning up...")
        try:
            run_trigger_script(args.trigger_script, "disable")
        except Exception as exc:
            print("  WARNING: could not disable the TSC: {}".format(exc))
        for role in ROLES:
            try:
                apply_sync_mode(devices[role], SYNC_MODE_DEFAULT, role)
            except Exception as exc:
                print("  WARNING: could not reset {} sync mode: {}".format(role, exc))

        write_frames_csv(os.path.join(output_dir, "frames.csv"), records_by_mode)
        print("  Wrote frames.csv")
        if any(records_by_mode.values()):
            plot_path = os.path.join(output_dir, "trigger_sync.png")
            export_plot(plot_path, records_by_mode, period_us)
            print("  Wrote {}".format(plot_path))

    print("\n".join(report_lines))
    print("\nAll output in: {}".format(os.path.abspath(output_dir)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
