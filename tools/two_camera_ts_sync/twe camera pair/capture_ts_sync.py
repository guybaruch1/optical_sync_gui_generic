#!/usr/bin/env python3
"""Two-camera timestamp-sync verification - a STANDALONE diagnostic.

Deliberately isolated: imports nothing from this project (no engine/,
domain/, gui/, settings.py), so it can be copied to a Jetson on its own.

WHAT IT ANSWERS
    Does the Jetson's TSC signal generator (ext_sync_gen.py, /dev/cdi_tsc)
    actually fire two GMSL-attached RealSense cameras in lockstep? It
    answers that by capturing the SAME rig twice and putting the two
    results side by side:

      Phase "untriggered": both cameras inter_cam_sync_mode=0 (free-running),
          TSC explicitly disabled. Two independent ~fps clocks - the
          cross-camera gap should wander across a whole frame period.
      Phase "triggered":   both cameras inter_cam_sync_mode=2 (slave),
          TSC enabled at the same fps the streams are configured for. The
          gap should collapse to a tight band.

    NOTE on why the baseline is free-running rather than "slave mode with
    the trigger off": a slaved camera with no trigger signal emits ZERO
    frames, so that configuration produces a wait_for_frames timeout and
    no data at all - nothing to compare against.

TIMESTAMPS
    HW TS     - frame.get_frame_metadata(frame_metadata_value.frame_timestamp),
                raw microseconds on each DEVICE'S OWN clock. Two cameras
                have unrelated epochs, so the raw difference is a large
                meaningless constant (hundreds of seconds), NOT latency.
                Reported both ways: hw_ts_diff_us_raw (as-is) and
                hw_ts_diff_us_corrected (minus a one-time offset learned
                from the first matched pair). Only the corrected one is
                physical, and watching it drift is the point of keeping both
                - a real run measured ~22us over 30s, about 0.7ppm.
    Global TS - frame.get_timestamp(), RealSense's GLOBAL_TIME domain,
                natively milliseconds since the Unix epoch, host-clock
                corrected and therefore directly comparable across two
                independent devices with no calibration. Stored internally
                in microseconds, PRINTED in milliseconds.
                ENABLED, then validated - never assumed. global_time_enabled
                is a per-sensor option this script turns on explicitly (see
                enable_global_time): it is NOT on by default everywhere.
                Confirmed on a Jetson with 2x D457 GMSL (FW 5.17.3.10),
                where frames start in the 'hardware_clock' domain and only
                reach GLOBAL_TIME once librealsense's device-to-host clock
                regression converges - hence GlobalTimeGate's grace window.

MATCHING
    Frames are paired on NEAREST TIMESTAMP within --max-match-gap-us, never
    on frame number: the two devices' frame counters are not in lockstep
    (a real capture showed #393 against #362), so a counter-based pairing
    would be silently wrong. Unmatched frames are counted and reported, so
    "matching never worked" cannot masquerade as a clean run.

    --match-on global (default) keys on Global TS - directly comparable
        across devices, no calibration needed. Frames captured before global
        time converges have no key and go unpaired, leaving a short gap at
        the start of a plot.
    --match-on hw     keys on HW TS instead, for a rig without global time.
        The two epochs are unrelated, so the FIRST pair matches unbounded to
        learn the offset and later pairs match within the window in
        offset-corrected space.

    Because matching minimises the difference in whichever timestamp is the
    KEY, the other one is the more trustworthy measurement: with the default
    --match-on global, the HW TS gap is the honest number.

RUN (on the Jetson; both files in the same directory)
    python capture_ts_sync.py
    python capture_ts_sync.py --match-on hw
    python capture_ts_sync.py --help

Ctrl+C during either phase stops cleanly and still writes every CSV and
plot for whatever was captured.
"""

import argparse
import csv
import math
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

ROLE_CAM1 = "CAM1"
ROLE_CAM2 = "CAM2"

PHASE_UNTRIGGERED = "untriggered"
PHASE_TRIGGERED = "triggered"

# Which rs.stream/index each --stream choice means, plus the label that
# identifies it in the CSVs. Mirrors engine/streams.py's stream_slug
# convention ("infrared1", "color") so a CSV from this script and one from
# the app read the same way.
STREAM_CHOICES = {
    "ir": {"label": "infrared1", "index": 1},
    "depth": {"label": "depth", "index": 0},
    "color": {"label": "color", "index": 0},
}

# D400-series rs.option.inter_cam_sync_mode values. NOT translated for
# D500-series, which uses a different scheme on the same option - hence
# --sync-mode, so a different generation can be driven without editing
# this file. Same reasoning as engine/streams.py's set_inter_cam_sync_mode.
SYNC_MODE_DEFAULT = 0
SYNC_MODE_SLAVE = 2

# Dark-theme chart colors, lifted from domain/plot_theme.py (itself the
# dataviz skill's validated dark palette) rather than imported, to keep
# this script standalone. Each chart panel carries a single series, so
# these two never sit adjacent and need no pairwise CVD separation.
SURFACE = "#1a1a19"
GRIDLINE = "#2c2c2a"
MUTED_TEXT = "#898781"
GLOBAL_GAP_COLOR = "#4a7fe0"
HW_GAP_COLOR = "#3fbf9e"


# --------------------------------------------------------------------------
# Pure logic (no pyrealsense2, no matplotlib) - unit-tested in
# tests/tools/two_camera_ts_sync/test_capture_ts_sync.py
# --------------------------------------------------------------------------

@dataclass
class FrameRecord:
    camera_role: str
    serial: str
    stream: str
    frame_number: int
    hw_ts_us: float
    global_ts_us: float
    host_recv_s: float


class PendingBuffer:
    """Frames from one camera still waiting for a partner from the other.

    Bounded, oldest dropped first - an unmatched frame eventually stops
    being a plausible match for anything. Ported from
    engine/cross_camera_reconciler.py's _PendingBuffer.
    """

    def __init__(self, max_len):
        self._items = []  # [(ts_us, record), ...]
        self._max_len = max_len

    def push(self, ts_us, record):
        self._items.append((ts_us, record))
        if len(self._items) > self._max_len:
            self._items.pop(0)

    def pop_nearest(self, ts_us, max_gap_us):
        """Removes and returns the buffered record nearest ts_us if it is
        within max_gap_us - otherwise None, leaving the buffer untouched.
        An explicit no-match rather than a forced, misleading pairing."""
        if not self._items:
            return None
        best_index = min(
            range(len(self._items)),
            key=lambda index: abs(self._items[index][0] - ts_us),
        )
        if abs(self._items[best_index][0] - ts_us) > max_gap_us:
            return None
        return self._items.pop(best_index)[1]


class GlobalTimeGate:
    """Decides, per frame, whether its global timestamp is usable yet.

    Real-hardware finding (Jetson, 2x D457 GMSL, FW 5.17.3.10): frames arrive
    in the 'hardware_clock' domain at the start of a stream, not GLOBAL_TIME.
    librealsense's global-time reader maintains a running regression between
    the device clock and the host clock, and reports hardware_clock until that
    regression has enough samples to converge. An earlier version of this
    script aborted on the FIRST non-global frame, which could never reach
    convergence - it failed the run before the mechanism it was checking for
    had a chance to start working.

    So a non-global frame is tolerated (its global timestamp simply dropped)
    for the first grace_s seconds, and only becomes a hard failure if global
    time never shows up at all in that window. Once global HAS been seen, a
    later dropout is likewise skipped rather than fatal.

    Returns "record", "skip", or "fail".
    """

    def __init__(self, grace_s=5.0):
        self._grace_s = grace_s
        self.saw_global = False

    def check(self, is_global, elapsed_s):
        if is_global:
            self.saw_global = True
            return "record"
        # Strictly less-than, so --global-ts-grace-s 0 genuinely means "no
        # grace at all" rather than still tolerating the first frame.
        if self.saw_global or elapsed_s < self._grace_s:
            return "skip"
        return "fail"


class FrameMatcher:
    """Pairs one stream's frames across the two cameras on nearest timestamp.

    One instance per stream label - a rig capturing two streams per camera
    uses two matchers, so pairs are never crossed between streams.

    Sign convention throughout: every "_diff" is CAM2 minus CAM1.

    match_on="global" is the preferred key: two global-time-enabled devices
    produce directly comparable timestamps with no calibration, so a plain
    tight window works from the very first frame.

    match_on="hw" is the fallback for a rig where RealSense global time is
    unavailable. Raw HW timestamps come from each device's OWN clock with
    unrelated epochs - hundreds of seconds apart - so no fixed window can
    match them directly. Instead the FIRST pair is matched UNBOUNDED to learn
    the offset, and every later pair is matched within the window in
    offset-corrected space. Its weakness is that the unbounded first match
    can pair two unrelated frames, which then biases the learned offset - so
    prefer "global" wherever the hardware supports it.
    """

    def __init__(self, max_match_gap_us=50_000.0, buffer_len=30, match_on="global"):
        if match_on not in ("global", "hw"):
            raise ValueError("match_on must be 'global' or 'hw', got {!r}".format(match_on))
        self._max_match_gap_us = max_match_gap_us
        self._match_on = match_on
        self._buffers = {
            ROLE_CAM1: PendingBuffer(buffer_len),
            ROLE_CAM2: PendingBuffer(buffer_len),
        }
        self._pair_counter = 0
        self._hw_offset_us = None
        self._first_host_recv_s = None
        self.matched_count = 0
        self.unmatched_count = 0

    def ingest(self, record):
        """Feeds one frame in. Returns the completed pair dict if this frame
        found a partner, else None."""
        if self._first_host_recv_s is None:
            self._first_host_recv_s = record.host_recv_s

        own_ts = record.global_ts_us if self._match_on == "global" else record.hw_ts_us
        if own_ts is None:
            # Global mode, and global time has not converged yet.
            self.unmatched_count += 1
            return None

        other_role = ROLE_CAM2 if record.camera_role == ROLE_CAM1 else ROLE_CAM1
        search_ts, max_gap_us = self._search_target(record.camera_role, own_ts)
        partner = self._buffers[other_role].pop_nearest(search_ts, max_gap_us)
        if partner is None:
            self._buffers[record.camera_role].push(own_ts, record)
            self.unmatched_count += 1
            return None

        # The frame that was sitting in the buffer was counted as unmatched
        # when it went in - now that it found a partner, both count as matched.
        self.unmatched_count -= 1
        self.matched_count += 2

        if record.camera_role == ROLE_CAM1:
            cam1, cam2 = record, partner
        else:
            cam1, cam2 = partner, record
        return self._build_pair(cam1, cam2)

    def _search_target(self, role, own_ts):
        """Where to look in the OTHER camera's buffer, and how wide a window.

        Buffers hold each frame at its own raw timestamp, so an incoming
        frame's timestamp has to be translated into the other camera's clock
        before searching. In global mode the two clocks are already the same
        (no translation, fixed window); in HW mode the offset does the
        translating - and until it is known, the first search is unbounded.
        """
        if self._match_on == "global":
            return own_ts, self._max_match_gap_us
        if self._hw_offset_us is None:
            return own_ts, math.inf
        # offset is (cam2 - cam1), so cam1 space = cam2 - offset and
        # cam2 space = cam1 + offset.
        if role == ROLE_CAM1:
            return own_ts + self._hw_offset_us, self._max_match_gap_us
        return own_ts - self._hw_offset_us, self._max_match_gap_us

    def _build_pair(self, cam1, cam2):
        self._pair_counter += 1
        hw_diff_raw = cam2.hw_ts_us - cam1.hw_ts_us
        if self._hw_offset_us is None:
            self._hw_offset_us = hw_diff_raw
        if cam1.global_ts_us is None or cam2.global_ts_us is None:
            global_diff = None
        else:
            global_diff = cam2.global_ts_us - cam1.global_ts_us
        return {
            "pair_index": self._pair_counter,
            "stream": cam1.stream,
            "cam1_serial": cam1.serial,
            "cam2_serial": cam2.serial,
            "cam1_frame_number": cam1.frame_number,
            "cam2_frame_number": cam2.frame_number,
            "frame_number_diff": cam2.frame_number - cam1.frame_number,
            "cam1_hw_ts_us": cam1.hw_ts_us,
            "cam2_hw_ts_us": cam2.hw_ts_us,
            "hw_ts_diff_us_raw": hw_diff_raw,
            "hw_ts_diff_us_corrected": hw_diff_raw - self._hw_offset_us,
            "cam1_global_ts_us": cam1.global_ts_us,
            "cam2_global_ts_us": cam2.global_ts_us,
            "global_ts_diff_us": global_diff,
            "host_elapsed_s": max(cam1.host_recv_s, cam2.host_recv_s) - self._first_host_recv_s,
        }


def summarize(values):
    """count/mean/stddev(sample)/min/max, with None for anything undefined
    at that sample size, so a caller can print a summary without guarding
    every field itself. Missing values are ignored, not counted as zero."""
    clean = [value for value in values if value is not None]
    if not clean:
        return {"count": 0, "mean": None, "stddev": None, "min": None, "max": None}
    mean = sum(clean) / len(clean)
    if len(clean) < 2:
        stddev = None
    else:
        variance = sum((value - mean) ** 2 for value in clean) / (len(clean) - 1)
        stddev = math.sqrt(variance)
    return {
        "count": len(clean),
        "mean": mean,
        "stddev": stddev,
        "min": min(clean),
        "max": max(clean),
    }


def format_pair_lines(pair):
    """The operator's expected 3-line per-pair CLI block. Global TS prints in
    MILLISECONDS (its native get_timestamp() unit) while HW TS prints in raw
    microseconds. Prints "n/a" rather than a fabricated 0 when global time is
    unavailable on this rig."""
    def global_ts(value):
        return "n/a" if value is None else "{:.3f}".format(value / 1000.0)

    def global_diff(value):
        return "n/a" if value is None else "{:+.3f} ms".format(value / 1000.0)

    return [
        "{} | Frame #{:<7} | HW TS={:<15.3f} | Global TS={}".format(
            ROLE_CAM1, pair["cam1_frame_number"], pair["cam1_hw_ts_us"],
            global_ts(pair["cam1_global_ts_us"]),
        ),
        "{} | Frame #{:<7} | HW TS={:<15.3f} | Global TS={}".format(
            ROLE_CAM2, pair["cam2_frame_number"], pair["cam2_hw_ts_us"],
            global_ts(pair["cam2_global_ts_us"]),
        ),
        "     | Frame difference={:+d} | HW TS difference={:+.3f} us | "
        "Global TS difference={}".format(
            pair["frame_number_diff"], pair["hw_ts_diff_us_raw"],
            global_diff(pair["global_ts_diff_us"]),
        ),
    ]


def _format_stats(label, stats, unit):
    if stats["count"] == 0:
        return "  {:<34} no data".format(label)
    stddev = "n/a" if stats["stddev"] is None else "{:.3f}".format(stats["stddev"])
    return "  {:<34} n={:<6} mean={:+.3f} sd={} min={:+.3f} max={:+.3f} {}".format(
        label, stats["count"], stats["mean"], stddev, stats["min"], stats["max"], unit,
    )


def format_phase_summary(phase_name, pairs, frame_counts, unmatched):
    lines = ["", "=" * 78, "PHASE: {}".format(phase_name.upper()), "=" * 78]
    for role in (ROLE_CAM1, ROLE_CAM2):
        lines.append("  {:<34} {}".format(role + " frames captured", frame_counts.get(role, 0)))
    lines.append("  {:<34} {}".format("matched pairs", len(pairs)))
    lines.append("  {:<34} {}".format("frames left unmatched", unmatched))
    lines.append(_format_stats(
        "Global TS gap", summarize([p["global_ts_diff_us"] for p in pairs]), "us"))
    lines.append(_format_stats(
        "HW TS gap (offset-corrected)",
        summarize([p["hw_ts_diff_us_corrected"] for p in pairs]), "us"))
    lines.append(_format_stats(
        "frame number difference",
        summarize([float(p["frame_number_diff"]) for p in pairs]), "frames"))
    return lines


# --------------------------------------------------------------------------
# CSV output
# --------------------------------------------------------------------------

FRAME_CSV_COLUMNS = [
    "phase", "camera_role", "serial", "stream", "frame_number",
    "hw_ts_us", "global_ts_us", "host_recv_s",
]

PAIR_CSV_COLUMNS = [
    "phase", "pair_index", "stream", "cam1_serial", "cam2_serial",
    "cam1_frame_number", "cam2_frame_number", "frame_number_diff",
    "cam1_hw_ts_us", "cam2_hw_ts_us", "hw_ts_diff_us_raw", "hw_ts_diff_us_corrected",
    "cam1_global_ts_us", "cam2_global_ts_us", "global_ts_diff_us", "host_elapsed_s",
]


def write_frames_csv(path, phase_name, records):
    """Every frame from each camera, unmatched and unfiltered - the ground
    truth any other bundling/pairing scheme can be checked against."""
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FRAME_CSV_COLUMNS)
        writer.writeheader()
        for record in records:
            writer.writerow({
                "phase": phase_name,
                "camera_role": record.camera_role,
                "serial": record.serial,
                "stream": record.stream,
                "frame_number": record.frame_number,
                "hw_ts_us": "{:.3f}".format(record.hw_ts_us),
                "global_ts_us": ("" if record.global_ts_us is None
                                 else "{:.3f}".format(record.global_ts_us)),
                "host_recv_s": "{:.6f}".format(record.host_recv_s),
            })


def write_pairs_csv(path, phase_name, pairs):
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PAIR_CSV_COLUMNS)
        writer.writeheader()
        for pair in pairs:
            row = dict(pair)
            row["phase"] = phase_name
            writer.writerow({column: row[column] for column in PAIR_CSV_COLUMNS})


# --------------------------------------------------------------------------
# Plots
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


def _panel_title(metric_label, stats):
    if stats["count"] == 0:
        return "{} - no data".format(metric_label)
    spread = stats["max"] - stats["min"]
    stddev = "n/a" if stats["stddev"] is None else "{:.1f}".format(stats["stddev"])
    return "{}\nspread {:.1f} us   sd {} us   n={}".format(
        metric_label, spread, stddev, stats["count"])


def export_comparison_plot(path, phase_pairs):
    """One 2x2 figure: columns are the two phases, rows the two metrics.

    Each ROW shares one y-axis across both columns on purpose. Left to
    autoscale independently, a +/-16000us free-running spread and a +/-50us
    triggered spread both render as equally-tall noise, which would hide the
    single result this whole script exists to show. Sharing the row scale
    makes the triggered panel visibly flat. The numeric spread is in every
    panel title too, so the conclusion never rests on eyeballing alone.

    Each panel carries exactly one series, so no legend is needed - the
    panel title names it (the dataviz skill's single-series rule).
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    phases = [PHASE_UNTRIGGERED, PHASE_TRIGGERED]
    metrics = [
        ("global_ts_diff_us", "Global TS gap (us)", GLOBAL_GAP_COLOR),
        ("hw_ts_diff_us_corrected", "HW TS gap, offset-corrected (us)", HW_GAP_COLOR),
    ]

    figure, axes = plt.subplots(2, 2, figsize=(16, 9), sharey="row")
    figure.patch.set_facecolor(SURFACE)

    for row, (key, metric_label, color) in enumerate(metrics):
        for column, phase_name in enumerate(phases):
            ax = axes[row][column]
            _style_axis(ax)
            points = [(pair["host_elapsed_s"], pair[key])
                      for pair in (phase_pairs.get(phase_name) or [])
                      if pair[key] is not None]
            elapsed = [point[0] for point in points]
            values = [point[1] for point in points]
            if values:
                ax.axhline(0.0, color=MUTED_TEXT, linewidth=0.8, alpha=0.5)
                ax.plot(elapsed, values, color=color, linewidth=1.5)
            else:
                ax.text(0.5, 0.5, "no data captured", transform=ax.transAxes,
                        ha="center", va="center", color=MUTED_TEXT, fontsize=11)
            ax.set_title("{} - {}".format(
                phase_name.upper(), _panel_title(metric_label, summarize(values))),
                fontsize=9)
            if row == 1:
                ax.set_xlabel("Elapsed time (s)")
            if column == 0:
                ax.set_ylabel(metric_label)

    figure.suptitle(
        "Two-camera timestamp sync: free-running vs TSC-triggered "
        "(each row shares one y-axis)",
        color=MUTED_TEXT, fontsize=12)
    figure.tight_layout(rect=(0, 0, 1, 0.96))
    figure.savefig(path, facecolor=SURFACE, dpi=110)
    plt.close(figure)


def export_phase_detail_plot(path, phase_name, pairs):
    """One phase on its OWN autoscaled y-axes - the companion to
    export_comparison_plot, not a replacement for it.

    The comparison figure deliberately shares each row's y-axis so the
    triggered phase reads as flat against the free-running spread; the cost
    is that the triggered phase's own residual jitter (tens of us against a
    ~16000us row scale) is then invisible. This renders that same data
    autoscaled, so residual jitter and any slow drift can actually be
    inspected.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    metrics = [
        ("global_ts_diff_us", "Global TS gap (us)", GLOBAL_GAP_COLOR),
        ("hw_ts_diff_us_corrected", "HW TS gap, offset-corrected (us)", HW_GAP_COLOR),
    ]
    figure, axes = plt.subplots(2, 1, figsize=(14, 8), sharex=True)
    figure.patch.set_facecolor(SURFACE)

    for row, (key, metric_label, color) in enumerate(metrics):
        ax = axes[row]
        _style_axis(ax)
        points = [(pair["host_elapsed_s"], pair[key])
                  for pair in pairs if pair[key] is not None]
        values = [point[1] for point in points]
        ax.axhline(0.0, color=MUTED_TEXT, linewidth=0.8, alpha=0.5)
        if values:
            ax.plot([point[0] for point in points], values, color=color, linewidth=1.5)
        else:
            ax.text(0.5, 0.5, "no data captured", transform=ax.transAxes,
                    ha="center", va="center", color=MUTED_TEXT, fontsize=11)
        ax.set_title(_panel_title(metric_label, summarize(values)), fontsize=9)
        ax.set_ylabel(metric_label)
    axes[1].set_xlabel("Elapsed time (s)")

    figure.suptitle(
        "Phase '{}' in detail (autoscaled - NOT the shared scale of the "
        "comparison figure)".format(phase_name),
        color=MUTED_TEXT, fontsize=12)
    figure.tight_layout(rect=(0, 0, 1, 0.95))
    figure.savefig(path, facecolor=SURFACE, dpi=110)
    plt.close(figure)


# --------------------------------------------------------------------------
# Hardware / trigger (Jetson-only; no automated tests, same convention as
# engine/led_panel.py and engine/session_engine.py)
# --------------------------------------------------------------------------

def _rs():
    import pyrealsense2 as rs
    return rs


def run_trigger_script(script_path, action, fps=None, duty=None):
    """Shells out to ext_sync_gen.py. --fps and --duty are passed together or
    not at all: that script has no GET_RATE ioctl, so passing either one
    overwrites BOTH (its own hardcoded default fills the other in), which
    would silently change the frame rate underneath the measurement."""
    command = [sys.executable, script_path, "--" + action]
    if action == "enable" and fps is not None and duty is not None:
        command += ["--fps", str(fps), "--duty", str(duty)]
    print("  $ {}".format(" ".join(command)))
    result = subprocess.run(command, capture_output=True, text=True)
    for line in (result.stdout or "").splitlines() + (result.stderr or "").splitlines():
        print("    {}".format(line))
    if result.returncode != 0:
        raise RuntimeError(
            "ext_sync_gen.py --{} failed with exit code {}. The TSC generator is "
            "required for the triggered phase; refusing to report a 'triggered' "
            "result that was never actually triggered.".format(action, result.returncode)
        )


def apply_sync_mode(device, mode, role_label):
    """Writes rs.option.inter_cam_sync_mode to whichever sensor on the device
    actually supports it - not a fixed sensor index, since genlock is carried
    by the stereo sensor on some models and the option is absent on others.
    Ported from engine/streams.py's set_inter_cam_sync_mode, plus a readback
    so the operator sees what the device actually accepted."""
    rs = _rs()
    for sensor in device.query_sensors():
        if sensor.supports(rs.option.inter_cam_sync_mode):
            sensor.set_option(rs.option.inter_cam_sync_mode, mode)
            readback = sensor.get_option(rs.option.inter_cam_sync_mode)
            print("  {}: inter_cam_sync_mode set to {} (device reports {:.0f})".format(
                role_label, mode, readback))
            return True
    print("  {}: WARNING - no sensor supports inter_cam_sync_mode; "
          "this camera cannot be synced.".format(role_label))
    return False


def enable_global_time(device, role_label, quiet=False):
    """Turns rs.option.global_time_enabled ON for every sensor that supports it.

    Real-hardware finding (Jetson + 2x D457 GMSL): frames came back in a
    NON-global timestamp domain. This project's own app never hits that
    because Windows defaults the option on - engine/streams.py's
    _read_global_ts_us only ever VALIDATES the domain and never sets it.
    Validating without first enabling is not enough on a platform where the
    default differs.
    """
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
            print("  {} [{}]: global_time_enabled -> {:.0f}".format(
                role_label, name, readback))
        enabled_any = enabled_any or readback != 0
    if not enabled_any and not quiet:
        print("  {}: WARNING - no sensor reports global_time_enabled. The Global "
              "TS metric will be unavailable; --match-on hw is the fallback."
              .format(role_label))
    return enabled_any


def _domain_name(domain):
    return str(domain).rsplit(".", 1)[-1]


def _get_stream_frame(frameset, kind, index):
    if kind == "ir":
        return frameset.get_infrared_frame(index)
    if kind == "depth":
        return frameset.get_depth_frame()
    return frameset.get_color_frame()


def _capture_worker(pipeline, role, serial, stream_kinds, stop_event, out_queue,
                    errors, require_global_ts, global_ts_grace_s):
    """One blocking wait_for_frames loop per camera, on its own thread.

    Threads rather than polling both pipelines from one loop: a blocking wait
    on camera 1 would let camera 2's frame queue overrun and drop frames,
    which would look exactly like a sync problem.
    """
    rs = _rs()
    metadata = rs.frame_metadata_value.frame_timestamp
    global_domain = rs.timestamp_domain.global_time
    gate = GlobalTimeGate(grace_s=global_ts_grace_s)
    started_s = time.perf_counter()
    warned_waiting = False
    try:
        while not stop_event.is_set():
            try:
                frameset = pipeline.wait_for_frames(2000)
            except Exception:
                if stop_event.is_set():
                    return
                raise RuntimeError(
                    "{} ({}): no frames for 2s. A camera at inter_cam_sync_mode=2 "
                    "with no external trigger present emits nothing at all - check "
                    "that the TSC generator is enabled and wired to this "
                    "camera.".format(role, serial)
                )
            host_recv_s = time.perf_counter()
            for kind in stream_kinds:
                frame = _get_stream_frame(frameset, kind, STREAM_CHOICES[kind]["index"])
                if not frame:
                    continue
                if not frame.supports_frame_metadata(metadata):
                    raise RuntimeError(
                        "{} ({}): no per-frame HW timestamp metadata "
                        "(frame_metadata_value.frame_timestamp), which this test "
                        "requires.".format(role, serial)
                    )
                # Global time is captured only when the frame really is in the
                # GLOBAL_TIME domain. Outside it, get_timestamp() silently
                # returns a different clock that is NOT comparable across two
                # independent devices, so this records None rather than a
                # plausible-looking wrong number.
                domain = frame.get_frame_timestamp_domain()
                verdict = gate.check(domain == global_domain,
                                     time.perf_counter() - started_s)
                if verdict == "record":
                    global_ts_us = frame.get_timestamp() * 1000.0
                elif verdict == "fail" and require_global_ts:
                    raise RuntimeError(
                        "{} ({}): frames stayed in the '{}' timestamp domain for "
                        "{:.1f}s and never reached GLOBAL_TIME, so a cross-device "
                        "Global TS gap would be meaningless. Try a longer "
                        "--global-ts-grace-s, or re-run with --match-on hw to "
                        "match on the hardware timestamp instead.".format(
                            role, serial, _domain_name(domain), global_ts_grace_s)
                    )
                else:
                    global_ts_us = None
                    if not warned_waiting and require_global_ts:
                        warned_waiting = True
                        print("  {}: waiting for global time to converge "
                              "(currently '{}')...".format(role, _domain_name(domain)))
                out_queue.put(FrameRecord(
                    camera_role=role,
                    serial=serial,
                    stream=STREAM_CHOICES[kind]["label"],
                    frame_number=frame.get_frame_number(),
                    hw_ts_us=float(frame.get_frame_metadata(metadata)),
                    global_ts_us=global_ts_us,
                    host_recv_s=host_recv_s,
                ))
    except BaseException as exc:  # surfaced to the main thread, never swallowed
        errors.append(exc)
        stop_event.set()


def _build_config(serial, stream_kinds, width, height, fps):
    rs = _rs()
    config = rs.config()
    config.enable_device(serial)
    formats = {
        "ir": (rs.stream.infrared, rs.format.y8),
        "depth": (rs.stream.depth, rs.format.z16),
        "color": (rs.stream.color, rs.format.bgr8),
    }
    for kind in stream_kinds:
        stream_type, stream_format = formats[kind]
        config.enable_stream(
            stream_type, STREAM_CHOICES[kind]["index"], width, height, stream_format, fps)
    return config


def run_phase(phase_name, serials, devices, stream_kinds, width, height, fps,
              duration_s, max_match_gap_us, quiet, match_on, global_ts_grace_s):
    """Starts one pipeline per camera, captures for duration_s, and returns
    (records, pairs, unmatched_count, error).

    A capture-thread failure is RETURNED, not raised: the triggered phase is
    exactly the one that fails when the TSC generator isn't actually wired
    (a slaved camera with no trigger yields nothing), and raising here would
    discard the untriggered phase's already-captured data along with it. The
    caller reports the error and still writes everything it has. Ctrl+C
    likewise returns what it has so far."""
    rs = _rs()
    print("\n--- Phase '{}': starting pipelines ---".format(phase_name))

    pipelines = []
    records = []
    pairs = []
    matchers = {}
    stop_event = threading.Event()
    out_queue = queue.Queue()
    errors = []
    threads = []

    try:
        for role, serial in ((ROLE_CAM1, serials[0]), (ROLE_CAM2, serials[1])):
            # Enabled again right before streaming: the option lives on the
            # sensor, and re-asserting it after the earlier startup pass costs
            # nothing while covering a handle that was re-created in between.
            enable_global_time(devices[role], role, quiet=True)
            pipeline = rs.pipeline()
            profile = pipeline.start(_build_config(serial, stream_kinds, width, height, fps))
            pipelines.append(pipeline)
            # Once more on the device the STARTED pipeline actually resolved.
            # The handle from rs.context() above is a different object, and
            # setting a sensor option through it is not guaranteed to reach
            # the one now streaming.
            enable_global_time(profile.get_device(), role + " (active)", quiet=True)
            thread = threading.Thread(
                target=_capture_worker,
                args=(pipeline, role, serial, stream_kinds, stop_event, out_queue,
                      errors, match_on == "global", global_ts_grace_s),
                daemon=True,
            )
            thread.start()
            threads.append(thread)

        print("Capturing for {:.1f}s (Ctrl+C stops early and still writes "
              "output)...".format(duration_s))
        deadline = time.perf_counter() + duration_s
        while time.perf_counter() < deadline and not errors:
            try:
                record = out_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            records.append(record)
            matcher = matchers.setdefault(
                record.stream,
                FrameMatcher(max_match_gap_us=max_match_gap_us, match_on=match_on))
            pair = matcher.ingest(record)
            if pair is not None:
                pairs.append(pair)
                if not quiet:
                    print("\n".join(format_pair_lines(pair)))
    except KeyboardInterrupt:
        print("\nInterrupted - stopping this phase early.")
    finally:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=3.0)
        for pipeline in pipelines:
            try:
                pipeline.stop()
            except Exception as exc:
                print("  WARNING: pipeline.stop() failed: {}".format(exc))

    unmatched = sum(matcher.unmatched_count for matcher in matchers.values())
    return records, pairs, unmatched, (errors[0] if errors else None)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def _default_trigger_script():
    """ext_sync_gen.py sitting next to this script (how it is deployed on the
    Jetson: both files in the same directory). Returns None if absent, which
    falls back to prompting the operator to run it by hand."""
    candidate = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "ext_sync_gen.py")
    return candidate if os.path.exists(candidate) else None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Verify whether the Jetson TSC generator triggers two "
                    "RealSense cameras in lockstep, by comparing a free-running "
                    "capture against a triggered one.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--serials", nargs=2, metavar=("CAM1", "CAM2"),
                        help="Device serials; default is the 2 connected devices")
    parser.add_argument("--stream", choices=sorted(STREAM_CHOICES), default="ir",
                        help="Which stream to harvest timestamps from")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=30,
                        help="Used for BOTH the camera streams and the TSC "
                             "generator, so the two cannot disagree")
    parser.add_argument("--duty", type=int, default=50,
                        help="TSC duty cycle percent")
    parser.add_argument("--duration", type=float, default=30.0,
                        help="Capture seconds PER PHASE")
    parser.add_argument("--trigger-script", default=_default_trigger_script(),
                        help="Path to ext_sync_gen.py; defaults to one sitting "
                             "next to this script. Without it, the script never "
                             "touches the TSC and waits for you to press Enter")
    parser.add_argument("--global-ts-grace-s", type=float, default=5.0,
                        help="Seconds to let librealsense's global-time clock "
                             "regression converge before treating a non-GLOBAL_TIME "
                             "domain as fatal")
    parser.add_argument("--match-on", choices=("global", "hw"), default="global",
                        help="Cross-camera pairing key. 'global' needs RealSense "
                             "global time; 'hw' falls back to the hardware "
                             "timestamp with a learned offset")
    parser.add_argument("--sync-mode", type=int, default=SYNC_MODE_SLAVE,
                        help="Raw inter_cam_sync_mode value for the triggered "
                             "phase (D400-series slave=2; D500-series differs)")
    parser.add_argument("--max-match-gap-us", type=float, default=50_000.0,
                        help="Max timestamp distance for a cross-camera pairing")
    parser.add_argument("--output-dir", default=None,
                        help="Default: output/two_camera_ts_sync/<timestamp>")
    parser.add_argument("--skip-untriggered", action="store_true")
    parser.add_argument("--skip-triggered", action="store_true")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress the per-pair CLI blocks")
    return parser.parse_args(argv)


def _resolve_serials(args):
    rs = _rs()
    if args.serials:
        return list(args.serials)
    devices = list(rs.context().query_devices())
    if len(devices) != 2:
        raise RuntimeError(
            "Need exactly 2 connected RealSense devices (found {}), or pass both "
            "explicitly: --serials <CAM1> <CAM2>".format(len(devices)))
    return [device.get_info(rs.camera_info.serial_number) for device in devices]


def _find_device(serial):
    rs = _rs()
    for device in rs.context().query_devices():
        if device.get_info(rs.camera_info.serial_number) == serial:
            return device
    raise RuntimeError("No connected RealSense device with serial {}".format(serial))


def main(argv=None):
    args = parse_args(argv)
    if args.skip_untriggered and args.skip_triggered:
        print("Both phases skipped - nothing to do.", file=sys.stderr)
        return 1

    stream_kinds = [args.stream]
    output_dir = args.output_dir or os.path.join(
        "output", "two_camera_ts_sync", time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(output_dir, exist_ok=True)

    serials = _resolve_serials(args)
    devices = {role: _find_device(serial)
               for role, serial in ((ROLE_CAM1, serials[0]), (ROLE_CAM2, serials[1]))}
    rs = _rs()
    print("Output directory: {}".format(os.path.abspath(output_dir)))
    for role in (ROLE_CAM1, ROLE_CAM2):
        device = devices[role]
        print("{}: {} (serial {}, firmware {})".format(
            role,
            device.get_info(rs.camera_info.name),
            device.get_info(rs.camera_info.serial_number),
            device.get_info(rs.camera_info.firmware_version)))
    print("Stream: {} {}x{}@{}  |  match window: {:.0f}us  |  match on: {}".format(
        args.stream, args.width, args.height, args.fps, args.max_match_gap_us,
        args.match_on))
    if args.trigger_script:
        print("Trigger script: {}".format(os.path.abspath(args.trigger_script)))
    else:
        print("Trigger script: none given - you will be prompted to run it by hand")

    print("Enabling RealSense global time...")
    for role in (ROLE_CAM1, ROLE_CAM2):
        enable_global_time(devices[role], role)

    phase_pairs = {}
    summary_lines = []
    phase_errors = []
    trigger_enabled = False

    plan = []
    if not args.skip_untriggered:
        plan.append((PHASE_UNTRIGGERED, SYNC_MODE_DEFAULT, False))
    if not args.skip_triggered:
        plan.append((PHASE_TRIGGERED, args.sync_mode, True))

    try:
        for phase_name, sync_mode, wants_trigger in plan:
            print("\n" + "=" * 78)
            print("PHASE '{}': inter_cam_sync_mode={}, TSC {}".format(
                phase_name, sync_mode, "ENABLED" if wants_trigger else "DISABLED"))
            print("=" * 78)

            # Sync mode is written with nothing streaming - the ordering
            # engine/multi_camera_session.py establishes (roles first, then
            # start the pipelines).
            for role in (ROLE_CAM1, ROLE_CAM2):
                apply_sync_mode(devices[role], sync_mode, role)

            # Trigger BEFORE the pipelines: a slaved camera with no trigger
            # never delivers a frame, and that timeout is indistinguishable
            # from a genuinely broken configuration.
            if args.trigger_script:
                if wants_trigger:
                    run_trigger_script(args.trigger_script, "enable", args.fps, args.duty)
                    trigger_enabled = True
                else:
                    run_trigger_script(args.trigger_script, "disable")
                    trigger_enabled = False
            else:
                action = "ENABLE" if wants_trigger else "DISABLE"
                print("  No --trigger-script given. {} the TSC generator now, e.g.:"
                      .format(action))
                print("    python ext_sync_gen.py --{}{}".format(
                    action.lower(),
                    " --fps {} --duty {}".format(args.fps, args.duty) if wants_trigger else ""))
                input("  Press Enter when ready...")

            records, pairs, unmatched, phase_error = run_phase(
                phase_name, serials, devices, stream_kinds, args.width, args.height,
                args.fps, args.duration, args.max_match_gap_us, args.quiet,
                args.match_on, args.global_ts_grace_s)

            phase_pairs[phase_name] = pairs
            if phase_error is not None:
                print("\n  PHASE '{}' FAILED: {}".format(phase_name, phase_error))
                print("  Writing whatever this phase captured and continuing.")
                phase_errors.append((phase_name, phase_error))

            write_frames_csv(
                os.path.join(output_dir, "frames_raw_{}.csv".format(phase_name)),
                phase_name, records)
            write_pairs_csv(
                os.path.join(output_dir, "pairs_{}.csv".format(phase_name)),
                phase_name, pairs)

            frame_counts = {}
            for record in records:
                frame_counts[record.camera_role] = frame_counts.get(record.camera_role, 0) + 1
            summary_lines += format_phase_summary(
                phase_name, pairs, frame_counts, unmatched)
    except KeyboardInterrupt:
        print("\nInterrupted between phases - writing what was captured.")
    finally:
        print("\nCleaning up...")
        if args.trigger_script and trigger_enabled:
            try:
                run_trigger_script(args.trigger_script, "disable")
            except Exception as exc:
                print("  WARNING: could not disable the TSC generator: {}".format(exc))
        for role in (ROLE_CAM1, ROLE_CAM2):
            try:
                apply_sync_mode(devices[role], SYNC_MODE_DEFAULT, role)
            except Exception as exc:
                print("  WARNING: could not reset {} sync mode: {}".format(role, exc))

        plot_path = os.path.join(output_dir, "ts_sync_comparison.png")
        if any(phase_pairs.values()):
            export_comparison_plot(plot_path, phase_pairs)
            print("  Wrote {}".format(plot_path))
            for phase_name, pairs in phase_pairs.items():
                if not pairs:
                    continue
                detail_path = os.path.join(
                    output_dir, "ts_sync_detail_{}.png".format(phase_name))
                export_phase_detail_plot(detail_path, phase_name, pairs)
                print("  Wrote {}".format(detail_path))
        else:
            print("  No matched pairs in any phase - no plot written.")

    print("\n".join(summary_lines))
    if phase_errors:
        print("\nPHASES THAT FAILED:")
        for phase_name, error in phase_errors:
            print("  {}: {}".format(phase_name, error))
    print("\nAll output in: {}".format(os.path.abspath(output_dir)))
    return 1 if phase_errors else 0


if __name__ == "__main__":
    sys.exit(main())
