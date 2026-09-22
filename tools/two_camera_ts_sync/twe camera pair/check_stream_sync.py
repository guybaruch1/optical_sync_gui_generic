#!/usr/bin/env python3
"""Cross-camera AND inner-camera sync, two streams per camera - STANDALONE.

An extension of check_trigger_sync.py, which opens ONE stream per camera and
asks a single question: is the TSC trigger driving the two cameras? This one
opens TWO streams per camera and asks three:

    1. cross-camera, per stream   ir(CAM1) <-> ir(CAM2)
                                  color(CAM1) <-> color(CAM2)
       Are the two cameras aligned, measured on each stream separately?

    2. inner-camera               ir(CAM1) <-> color(CAM1)
                                  ir(CAM2) <-> color(CAM2)
       Within one camera, are its two sensors aligned with each other?

    3. frame cadence, per stream  what clock is each stream running on?

FOUR PHASES, and why the fourth exists
    Each is captured in turn, then compared column by column:

        mode0_trigger_off   sync mode 0, TSC off. The control.
        mode0_trigger_on    sync mode 0, TSC on. Does the trigger do anything
                            to a camera that was never told to follow it?
        mode2_trigger_on    sync mode 2 (slave), TSC on.
        mode3_trigger_on    sync mode 3 (genlock), TSC on.

    On D400, mode 3 is the value documented as pairing with an EXTERNAL
    trigger generator, while 1/2 (master/slave) is the D400-to-D400 peer-sync
    path over the sync cable. That fits what mode 2 actually measured on this
    rig: it did NOT gate the cameras - both kept streaming with the TSC
    disabled - which is what a peer-sync mode would do with no peer present.

    Mode 3 producing NO FRAMES is a legitimate result, not a failure. A sensor
    genuinely gated by an external trigger emits nothing when the trigger it
    waits for never arrives or does not match what the firmware expects. The
    report says so explicitly rather than leaving an empty phase to be read as
    a crash. And because mode 3 is the first mode here a firmware may simply
    not implement, each phase confirms the device REPORTS the mode it was
    given - a silently clamped write would otherwise produce a phase that
    reads like a genuine mode-3 result.

The pure helpers, the three capture modes, the TSC driving and the
single-threaded poll loop are all reused from check_trigger_sync.py rather
than reimplemented - imported from the sibling file so there is only ever one
copy of the pairing-free cross-camera maths.

TWO DIFFERENT METHODS, ON PURPOSE
    Cross-camera keeps the pairing-free phase method: two separate devices
    have unrelated hardware clocks and independent frame counters, so frames
    cannot be paired reliably and every frame is instead reduced to its
    position inside one trigger cycle.

    Inner-camera does NOT need any of that, and deliberately does not use it.
    Two streams on one device share that device's hardware clock and arrive
    in the SAME FRAMESET - the SDK has already grouped them - so the gap is a
    direct subtraction, exact in microseconds. Reducing it modulo the trigger
    period would actively hide faults: half a period is 16.7ms at 30fps, and
    this hardware's known inter-sensor offsets run 3.5-11.3ms, so a genuinely
    broken 20ms offset would fold to -13ms and read SMALLER than reality.

STREAMS
    --streams takes two specs, applied to BOTH cameras (they have to match,
    or there is no common stream to compare across cameras):

        --streams ir:1:1280x720@30 color:0:1280x720@30      IR + color
        --streams ir:1:1280x720@30 ir:2:1280x720@30         IR + IR
        --streams color:0:1280x720@30 color:1:1280x720@30   dual RGB

    Give both streams the SAME fps. Inner-camera measurement depends on the
    SDK delivering both in one frameset, which its syncer only does reliably
    when the two run at the same rate.

BANDWIDTH
    Two streams per camera on two cameras is roughly double what
    check_trigger_sync.py asks of the bus, so expect more dropped frames.
    That is inherent to measuring inner-camera sync, not a bug - the frame
    gap report tells you whether frames were never captured (missed trigger
    pulses) or captured and not delivered (transport, i.e. bandwidth).

OUTPUT
    frames.csv                every frame of every stream in every mode

    Two combined overviews, for comparing streams and cameras side by side:
        trigger_sync.png      a block per stream, plus the frame cadence
        inner_camera_sync.png a block per camera

    The same blocks again, one per file, for looking at a single question on
    a screen without scrolling past the others (named after the actual
    streams, so a run pairing different streams cannot overwrite another's):
        cross_sync_infrared1.png    cross-camera gap on that stream
        cross_sync_color.png        cross-camera gap on that stream
        frame_cadence.png           what clock each stream is on
        inner_sync_CAM1.png         ir vs color inside CAM1
        inner_sync_CAM2.png         ir vs color inside CAM2

RUN (on the Jetson; ext_sync_gen.py in the same directory is found for you)
    python check_stream_sync.py
    python check_stream_sync.py --streams ir:1:1280x720@30 ir:2:1280x720@30
    python check_stream_sync.py --help

Ctrl+C stops cleanly and still writes the CSV and every plot.
"""

import argparse
import csv
import os
import sys
import time
from dataclasses import dataclass

try:                                    # imported as a package (tests, -m)
    from . import check_trigger_sync as base
except ImportError:                     # run directly: sibling is on sys.path
    import check_trigger_sync as base

# Reused verbatim from the sibling script. Imported, not copied, so the
# pairing-free cross-camera maths has exactly one implementation and one set
# of tests - see tests/tools/two_camera_ts_sync/test_check_trigger_sync.py.
ROLE_CAM1 = base.ROLE_CAM1
ROLE_CAM2 = base.ROLE_CAM2
ROLES = base.ROLES
SYNC_MODE_DEFAULT = base.SYNC_MODE_DEFAULT
GLOBAL_TS_GRACE_S = base.GLOBAL_TS_GRACE_S
StreamSpec = base.StreamSpec
parse_stream_spec = base.parse_stream_spec
frame_gap_report = base.frame_gap_report
cross_gap_series = base.cross_gap_series
phase_series = base.phase_series
slide_rate_us_per_s = base.slide_rate_us_per_s
center_series = base.center_series
summarize = base.summarize
SURFACE = base.SURFACE
GRIDLINE = base.GRIDLINE
MUTED_TEXT = base.MUTED_TEXT
GLOBAL_GAP_COLOR = base.GLOBAL_GAP_COLOR
HW_GAP_COLOR = base.HW_GAP_COLOR
CAM1_COLOR = base.CAM1_COLOR
CAM2_COLOR = base.CAM2_COLOR

# On D400, mode 3 is the value documented as pairing with an EXTERNAL trigger
# generator; 1/2 (master/slave) is the D400-to-D400 peer-sync path over the
# sync cable. That distinction matters here: mode 2 measurably does NOT gate
# these cameras - the control phase found both still streaming with the TSC
# disabled - which is exactly what a peer-sync mode would do when there is no
# peer. So mode 3 gets a phase of its own, appended after the other three.
SYNC_MODE_GENLOCK = 3

SYNC_MODE_NAMES = {
    0: "default (no external sync)",
    1: "master (drives other D400s over the sync cable)",
    2: "slave (follows a master D400 over the sync cable)",
    3: "genlock / full slave (meant for an external trigger generator)",
}

# base.MODES is the sibling's own tuple - concatenated, never appended to, so
# adding a phase here cannot change what check_trigger_sync.py itself runs.
MODES = base.MODES + (("mode3_trigger_on", SYNC_MODE_GENLOCK, True),)

# Solid for the first stream, dashed for the second - so the cadence panel
# stays readable in one colour per camera instead of needing four hues.
STREAM_LINESTYLES = ("-", "--")

# Two cameras with the same firmware often land on exactly the SAME value
# (both free-running at -17us, both with a 3.5ms inner offset), and then
# whichever is drawn second hides the first completely. A wide line underneath
# and a narrow one on top stay legible even when they coincide exactly.
CAM_STYLE = {ROLE_CAM1: {"color": CAM1_COLOR, "linewidth": 2.6, "alpha": 0.75},
             ROLE_CAM2: {"color": CAM2_COLOR, "linewidth": 1.1, "alpha": 1.0}}

# An inner-camera gap varying by more than this is called out as jitter
# rather than reported as a fixed offset.
INNER_JITTER_NOTE_US = 500.0

# Where the cadence panel switches from a linear to a logarithmic y-axis. A
# dropped frame is a whole 33333us period; the clock difference worth seeing
# is ~17us. Nothing linear shows both, so anything past this is compressed.
SYMLOG_LINTHRESH_US = 100.0

# Each question gets its own titled block, drawn as a card a shade lighter
# than the figure ground so the gaps between blocks read as separators. Five
# near-identical rows in one flat grid gave no clue which panels belonged
# together; a reader had to work it out from the row titles.
BLOCK_SURFACE = "#211f1e"
BLOCK_HEADER_TEXT = "#c9c7c1"     # brighter than MUTED_TEXT: these are headings
BLOCK_ROW_HEIGHT_IN = 3.6         # per panel row inside a block
BLOCK_HEADER_IN = 0.75            # room for a block's own heading
BLOCK_GAP = 0.055                 # visible ground between the cards
FIGURE_TITLE_IN = 0.55


# --------------------------------------------------------------------------
# Pure logic - unit-tested in tests/tools/two_camera_ts_sync/
# --------------------------------------------------------------------------

@dataclass
class StreamFrameRecord:
    """One frame. `frameset_id` is what makes inner-camera measurement work:
    frames sharing an id came out of ONE poll on ONE camera, i.e. the SDK
    grouped them, so they are already paired and need no matching."""
    camera_role: str
    serial: str
    stream: str
    frameset_id: int
    frame_number: int
    hw_ts_us: float
    global_ts_us: float
    host_recv_s: float


def describe_sync_mode(mode):
    """A mode number with what it actually means, for logs and warnings."""
    return "{} - {}".format(mode, SYNC_MODE_NAMES.get(int(mode), "unknown to this script"))


def describe_mode_range(mode_range):
    """"0..2" for a readable range, or that it could not be read."""
    if mode_range is None:
        return "unknown (the device would not report its range)"
    low, high = mode_range
    return "{:.0f}..{:.0f}".format(low, high)


def unsupported_mode_message(role, mode, mode_range):
    """None if the device accepts this mode; an explanation if it cannot.

    Checked BEFORE writing, because writing an out-of-range value raises
    'out of range value for argument "value"' straight out of set_option -
    confirmed on real hardware asking a D457 for mode 3. That exception says
    nothing about what the device CAN do, and the range is readable, so it
    gets read and reported instead.

    An unreadable range does NOT block the attempt: better a real error from
    the camera than a phase skipped on a guess.
    """
    if mode_range is None:
        return None
    low, high = mode_range
    if low <= mode <= high:
        return None
    return ("{}: this device does not support inter_cam_sync_mode {} ({}). It "
            "accepts {} only.".format(role, mode,
                                      SYNC_MODE_NAMES.get(int(mode), "unknown mode"),
                                      describe_mode_range(mode_range)))


def sync_mode_warning(role, requested, reported):
    """None when the device took the mode; a warning string when it did not.

    Mode 3 is why this exists. Firmware that does not implement genlock can
    silently CLAMP the write rather than refuse it - and then a whole phase
    looks like a real mode-3 result while the sensor never left the mode it
    was already in, which is the most misleading outcome available.
    """
    if reported is None:
        return ("{}: could not read inter_cam_sync_mode back, so there is no "
                "confirmation that mode {} was applied.".format(
                    role, describe_sync_mode(requested)))
    if int(reported) == int(requested):
        return None
    return ("{}: asked for inter_cam_sync_mode {}, but the device reports {}. "
            "This phase is NOT testing mode {} - the firmware likely does not "
            "support it.".format(role, describe_sync_mode(requested),
                                 describe_sync_mode(reported), int(requested)))


def validate_stream_pair(spec_a, spec_b):
    """Two picks of the same stream leave nothing to compare."""
    if (spec_a.type_name, spec_a.index) == (spec_b.type_name, spec_b.index):
        raise ValueError(
            "--streams asks for the same stream twice ({}), so there is no "
            "pair to compare. Use two different streams, e.g. "
            "'ir:1:1280x720@30 color:0:1280x720@30' or "
            "'ir:1:1280x720@30 ir:2:1280x720@30'.".format(spec_a.label))


def inner_gap_series(records, role, label_a, label_b, attr="hw_ts_us"):
    """The gap between two streams on ONE camera - a direct subtraction.

    Both streams come off the same device, on the same hardware clock, and
    are delivered in the same frameset. So this needs no pairing heuristic
    (the frameset id IS the pairing) and no modulo (the two timestamps share
    an epoch, so their difference is already the real number).

    Deliberately NOT wrapped to half a period, unlike cross_gap_series:
    wrapping would report a broken 20ms offset as -13ms, smaller than
    reality, hiding the very fault this is meant to catch.

    Returns [(elapsed_s, gap_us), ...], positive when label_b lags label_a.
    Framesets carrying only one of the two streams are skipped.
    """
    by_frameset = {}
    for record in records:
        if record.camera_role != role or getattr(record, attr) is None:
            continue
        by_frameset.setdefault(record.frameset_id, {})[record.stream] = record

    paired = []
    for frameset_id in sorted(by_frameset):
        bundle = by_frameset[frameset_id]
        first, second = bundle.get(label_a), bundle.get(label_b)
        if first is None or second is None:
            continue
        paired.append((min(first.host_recv_s, second.host_recv_s),
                       getattr(second, attr) - getattr(first, attr)))
    if not paired:
        return []
    origin = paired[0][0]
    return [(host - origin, gap) for host, gap in paired]


def interval_deviation_series(records, period_us):
    """Each frame interval minus the trigger period, for one stream.

    0 means the stream stepped exactly with the trigger. A constant non-zero
    means it is running on its own clock, and the value IS the difference
    (-17us on this rig: 33316us of its own against a 33333us trigger). A
    spike of one whole period is a missing frame.
    """
    ordered = sorted(records, key=lambda record: record.host_recv_s)
    if len(ordered) < 2:
        return []
    origin = ordered[0].host_recv_s
    return [(ordered[index + 1].host_recv_s - origin,
             ordered[index + 1].hw_ts_us - ordered[index].hw_ts_us - period_us)
            for index in range(len(ordered) - 1)]


def split_by_role_and_stream(records, specs):
    """{(role, stream_label): [record, ...]} for every combination."""
    buckets = {(role, spec.label): [] for role in ROLES for spec in specs}
    for record in records:
        key = (record.camera_role, record.stream)
        if key in buckets:
            buckets[key].append(record)
    return buckets


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def format_mode_report(mode_label, records, specs, period_us):
    """The human-facing block for one mode: cadence, cross-camera, inner."""
    lines = ["", "=" * 78, "MODE: {}".format(mode_label.upper()), "=" * 78]
    buckets = split_by_role_and_stream(records, specs)

    lines.append("  PER STREAM")
    for role in ROLES:
        for spec in specs:
            own = buckets[(role, spec.label)]
            report = frame_gap_report(own, period_us)
            lines.append("    {} {:<10} {} frames".format(role, spec.label, report["count"]))
            if report["median_interval_us"] is None:
                lines.append("        no cadence (too few frames)")
                continue
            lines.append("        interval  {:.0f} us  ({:.4f} Hz)".format(
                report["median_interval_us"], report["hz"]))
            for stall in report["stalls"]:
                lines.append("        FRAME GAP at {:.1f}s: {:.0f} us (~{:.1f} frames) - {}".format(
                    stall["at_s"], stall["gap_us"], stall["frames_missed"],
                    "never captured (missed trigger pulses)" if stall["not_captured"]
                    else "captured but not delivered ({} numbers skipped)".format(
                        stall["numbers_skipped"])))
            if not report["stalls"]:
                lines.append("        no frame gaps")
            slide = slide_rate_us_per_s(phase_series(own, period_us), period_us)
            if slide is not None:
                lines.append("        cycle position creeps {:+.1f} us/s  -> {}".format(
                    slide, "LOCKED to trigger" if abs(slide) < 2.0 else "sliding - own clock"))

    lines.append("  CROSS-CAMERA (pairing-free, wrapped to +/- half a period)")
    for spec in specs:
        for attr, name in (("global_ts_us", "Global TS gap"),
                           ("hw_ts_us", "HW TS gap (centred)")):
            series = cross_gap_series(buckets[(ROLE_CAM1, spec.label)],
                                      buckets[(ROLE_CAM2, spec.label)], period_us, attr)
            if attr == "hw_ts_us":
                series = center_series(series)
            lines.append("    {:<8} {}".format(spec.label, _summary_text(name, series)))

    label_a, label_b = specs[0].label, specs[1].label
    lines.append("  INNER-CAMERA {} -> {} (direct subtraction, never wrapped)".format(
        label_a, label_b))
    for role in ROLES:
        series = inner_gap_series(records, role, label_a, label_b)
        if not series:
            # Say which of the two reasons it actually is. Blaming the syncer
            # for a phase that captured nothing sends the reader after a bug
            # that is not there - and for mode 3, no frames IS the finding.
            if not any(buckets[(role, spec.label)] for spec in specs):
                lines.append("    {:<6} no frames at all from this camera in this "
                             "mode.".format(role))
            else:
                lines.append("    {:<6} no data - frames arrived, but the two streams "
                             "never shared a frameset.".format(role))
                lines.append("           The SDK's syncer did not group them; check "
                             "both streams run at the same fps.")
            continue
        stats = summarize([value for _, value in series])
        spread = stats["max"] - stats["min"]
        lines.append("    {:<6} n={:<5} mean={:+.1f} us  min={:+.1f}  max={:+.1f}  "
                     "spread={:.1f}".format(role, stats["count"], stats["mean"],
                                            stats["min"], stats["max"], spread))
        lines.append("           -> {}".format(
            "steady offset ({:+.2f} ms)".format(stats["mean"] / 1000.0)
            if spread < INNER_JITTER_NOTE_US
            else "VARYING by {:.2f} ms - not a fixed offset".format(spread / 1000.0)))
    return lines


def _summary_text(name, series):
    stats = summarize([value for _, value in series])
    if stats["count"] == 0:
        return "{:<20} no data".format(name)
    return "{:<20} n={:<5} mean={:+.1f} us  min={:+.1f}  max={:+.1f}".format(
        name, stats["count"], stats["mean"], stats["min"], stats["max"])


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------

CSV_COLUMNS = ["mode", "camera_role", "serial", "stream", "frameset_id",
               "frame_number", "hw_ts_us", "global_ts_us", "host_recv_s"]


def write_frames_csv(path, records_by_mode):
    """Every frame from every stream in every mode, unpaired across cameras -
    the ground truth behind every number in the report. `frameset_id` is what
    lets a reader redo the inner-camera subtraction by hand."""
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
                    "frameset_id": record.frameset_id,
                    "frame_number": record.frame_number,
                    "hw_ts_us": "{:.3f}".format(record.hw_ts_us),
                    "global_ts_us": ("" if record.global_ts_us is None
                                     else "{:.3f}".format(record.global_ts_us)),
                    "host_recv_s": "{:.6f}".format(record.host_recv_s),
                })


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


def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def modes_present(records_by_mode):
    """Every phase that was ATTEMPTED, in phase order - empty ones included.

    Keyed on the key existing rather than on it holding frames. A phase that
    ran and captured nothing is a result: for mode 3 it is the headline one,
    since a sensor genuinely gated by an external trigger emits nothing when
    the trigger never arrives. Dropping it would delete the finding from every
    figure and leave three columns looking like the whole run. A phase that
    never ran at all - an interrupted run - has no key and is still skipped.
    """
    return [label for label, _, _ in MODES if label in records_by_mode]


def _no_data(ax):
    ax.text(0.5, 0.5, "no data", transform=ax.transAxes, ha="center",
            va="center", color=MUTED_TEXT, fontsize=11)


def _link_row_y(row_axes):
    """Give every panel in one row the same y-range.

    Done by hand rather than with subplots(sharey="row") so it can be applied
    to the gap rows only - see export_cross_plot's docstring for why the
    cadence row must be left alone. Panels with no data keep matplotlib's
    default 0..1 range, so they are excluded rather than allowed to drag a
    real row's scale down towards zero.
    """
    drawn = [ax for ax in row_axes if ax.has_data()]
    if not drawn:
        return
    low = min(ax.get_ylim()[0] for ax in drawn)
    high = max(ax.get_ylim()[1] for ax in drawn)
    for ax in row_axes:
        ax.set_ylim(low, high)


def _draw_gap_panel(ax, buckets, spec, attr, name, color, period_us):
    """One cross-camera gap panel. Returns the title text for it."""
    _style_axis(ax)
    series = cross_gap_series(buckets[(ROLE_CAM1, spec.label)],
                              buckets[(ROLE_CAM2, spec.label)], period_us, attr)
    if attr == "hw_ts_us":
        series = center_series(series)
    if series:
        ax.axhline(0.0, color=MUTED_TEXT, linewidth=0.8, alpha=0.5)
        ax.plot([t for t, _ in series], [value for _, value in series],
                color=color, linewidth=1.2)
    else:
        _no_data(ax)
    stats = summarize([value for _, value in series])
    if not stats["count"]:
        return name
    return "{}\nmean {:+.0f}  spread {:.0f} us".format(
        name, stats["mean"], stats["max"] - stats["min"])


def _draw_cadence_panel(ax, buckets, specs, period_us):
    """One cadence panel: every stream of both cameras, interval minus period."""
    _style_axis(ax)
    plotted = []
    for role in ROLES:
        style = dict(CAM_STYLE[role])
        for spec, dash in zip(specs, STREAM_LINESTYLES):
            series = interval_deviation_series(buckets[(role, spec.label)], period_us)
            if not series:
                continue
            plotted += [value for _, value in series]
            ax.plot([t for t, _ in series], [value for _, value in series],
                    linestyle=dash, label="{} {}".format(role, spec.label), **style)
    ax.axhline(0.0, color=MUTED_TEXT, linewidth=0.8, alpha=0.6)
    # Symlog ONLY when something in this panel actually needs it: a dropped
    # frame is a whole period, 2000x the ~17us clock difference, and no linear
    # axis shows both. But a panel whose every value already sits inside the
    # linear band - the common case of a steady -17us - gets junk tick labels
    # like "8.66667 x 10^-1" out of the symlog locator, so it keeps a plain
    # linear axis and reads properly.
    spiky = bool(plotted) and max(abs(value) for value in plotted) > SYMLOG_LINTHRESH_US
    if spiky:
        ax.set_yscale("symlog", linthresh=SYMLOG_LINTHRESH_US)
    if plotted:
        ax.legend(facecolor=SURFACE, edgecolor=GRIDLINE, labelcolor=MUTED_TEXT,
                  fontsize=8, ncol=2, framealpha=0.85)
    else:
        _no_data(ax)
        # Nothing links this panel's scale to the others (see the docstring on
        # why the cadence row is not y-linked), so an empty one would otherwise
        # advertise matplotlib's default 0..1 ticks as if they were a reading.
        ax.set_xticks([])
        ax.set_yticks([])
    return ("interval minus trigger period\n(0 = on the trigger; symlog beyond "
            "+/-{:.0f}us)".format(SYMLOG_LINTHRESH_US) if spiky
            else "interval minus trigger period\n(0 = on the trigger)")


def _open_block(target, header):
    """Turn a subfigure - or a whole standalone figure - into a titled card.

    Figure and SubFigure share the suptitle/subplots/patch API, which is what
    lets every block below be drawn either as one panel of a combined overview
    or as a standalone file, from the same code.
    """
    target.patch.set_facecolor(BLOCK_SURFACE)
    target.suptitle(header, color=BLOCK_HEADER_TEXT, fontsize=13,
                    fontweight="bold", x=0.012, ha="left")


GAP_ROWS = (("global_ts_us", "Global TS gap (us)", GLOBAL_GAP_COLOR),
            ("hw_ts_us", "HW TS gap, centred (us)", HW_GAP_COLOR))


def _fill_stream_gap_block(target, buckets_by_mode, labels, spec, period_us):
    """One stream's cross-camera gap: two y-linked rows, one column per mode."""
    axes = target.subplots(len(GAP_ROWS), len(labels), squeeze=False)
    for row, (attr, name, color) in enumerate(GAP_ROWS):
        for column, mode_label in enumerate(labels):
            ax = axes[row][column]
            heading = _draw_gap_panel(ax, buckets_by_mode[mode_label], spec,
                                      attr, name, color, period_us)
            ax.set_title("{}\n{}".format(mode_label, heading) if row == 0
                         else heading, fontsize=9)
            if row == len(GAP_ROWS) - 1:
                ax.set_xlabel("Elapsed time (s)")
            if column == 0:
                ax.set_ylabel(name)
            else:
                # The row is y-linked, so every other column would just
                # repeat these same numbers - noise, and it costs width.
                ax.tick_params(labelleft=False)
        _link_row_y([axes[row][column] for column in range(len(labels))])


def _fill_cadence_block(target, buckets_by_mode, labels, specs, period_us):
    """Every stream of both cameras: interval minus the trigger period."""
    axes = target.subplots(1, len(labels), squeeze=False)
    for column, mode_label in enumerate(labels):
        ax = axes[0][column]
        heading = _draw_cadence_panel(ax, buckets_by_mode[mode_label], specs, period_us)
        ax.set_title("{}\n{}".format(mode_label, heading), fontsize=9)
        ax.set_xlabel("Elapsed time (s)")
        if column == 0:
            ax.set_ylabel("frame interval (us)")


def _fill_inner_block(target, by_panel, labels, role, label_a, label_b, limits):
    """One camera's inner gap, one column per mode, on a caller-set y-range."""
    axes = target.subplots(1, len(labels), squeeze=False)
    for column, mode_label in enumerate(labels):
        ax = axes[0][column]
        _style_axis(ax)
        series = by_panel[(mode_label, role)]
        if series:
            stats = summarize([value for _, value in series])
            ax.plot([t for t, _ in series], [value for _, value in series],
                    label="mean {:+.0f} us   spread {:.0f} us".format(
                        stats["mean"], stats["max"] - stats["min"]),
                    **CAM_STYLE[role])
            ax.legend(facecolor=SURFACE, edgecolor=GRIDLINE, labelcolor=MUTED_TEXT,
                      fontsize=8, framealpha=0.85)
        else:
            _no_data(ax)
        ax.set_title(mode_label, fontsize=10)
        ax.set_xlabel("Elapsed time (s)")
        if column == 0:
            ax.set_ylabel("{} -> {} gap (us)".format(label_a, label_b))
        else:
            ax.tick_params(labelleft=False)
        if limits is not None:
            low, high = limits
            ax.set_ylim(low, high)
            if low <= 0.0 <= high:
                ax.axhline(0.0, color=MUTED_TEXT, linewidth=0.8, alpha=0.6)


def _inner_panels(records_by_mode, labels, label_a, label_b):
    """Every camera's inner series plus ONE shared y-range for all of them.

    The range spans both cameras and every mode, so a per-camera file is
    directly comparable against its sibling instead of each being
    autoscaled to its own data and only looking alike.

    Fitted to the data rather than anchored at zero. Anchoring there sounds
    right - zero is perfect alignment - but a real 3.5ms offset then puts
    every line in the top 2% of the panel and flattens the shape, which is the
    one thing the numbers in the report cannot show. Zero still gets a
    reference line whenever it falls inside the fitted range.
    """
    by_panel = {(mode_label, role): inner_gap_series(records_by_mode[mode_label],
                                                    role, label_a, label_b)
                for mode_label in labels for role in ROLES}
    values = [value for series in by_panel.values() for _, value in series]
    if not values:
        return by_panel, None
    low, high = min(values), max(values)
    padding = 0.15 * (high - low) or 100.0
    return by_panel, (low - padding, high + padding)


def _stream_gap_header(spec):
    return "STREAM  {}   -   cross-camera gap, CAM1 vs CAM2".format(spec.label)


CADENCE_HEADER = "FRAME CADENCE   -   what clock is each stream on?"


def _inner_header(role, label_a, label_b):
    return "{}   -   {} vs {} inside this one camera".format(role, label_a, label_b)


def _standalone(plt, header, rows, columns):
    """A single-block figure, sized and titled like one block of an overview."""
    figure = plt.figure(figsize=(6 * columns, BLOCK_ROW_HEIGHT_IN * rows + BLOCK_HEADER_IN),
                        layout="constrained")
    _open_block(figure, header)
    return figure


def _save(figure, path, plt, facecolor):
    figure.savefig(path, facecolor=facecolor, dpi=110)
    plt.close(figure)


# --- the combined overviews -----------------------------------------------

def export_cross_plot(path, records_by_mode, specs, period_us):
    """Every cross-camera question in one figure: a titled block per stream,
    plus a cadence block. One column per mode throughout.

    Blocks are separate subfigures on a lighter ground, so which panels belong
    together is visible at a glance rather than having to be read off several
    near-identical row titles, and every block repeats the mode names across
    its top so the column being looked at is never ambiguous however far down
    the figure the eye has travelled.

    The gap rows share a y-axis across modes, so a tight mode and a wandering
    one are directly comparable. The cadence row deliberately does NOT: one
    mode's 85ms interruption is 5000x the ~17us clock difference that row
    exists to show, and sharing the scale with it squashes that signal flat.
    Frame gaps are already called out by name in the text report, so nothing
    is lost by letting each cadence panel scale to its own data.
    """
    plt = _plt()
    labels = modes_present(records_by_mode)
    if not labels:
        return
    buckets_by_mode = {mode_label: split_by_role_and_stream(
        records_by_mode[mode_label], specs) for mode_label in labels}

    block_rows = [len(GAP_ROWS)] * len(specs) + [1]
    block_heights = [BLOCK_ROW_HEIGHT_IN * rows + BLOCK_HEADER_IN for rows in block_rows]
    figure = plt.figure(figsize=(6 * len(labels), sum(block_heights) + FIGURE_TITLE_IN),
                        layout="constrained")
    figure.patch.set_facecolor(SURFACE)
    figure.suptitle("Cross-camera sync per stream - no frame pairing",
                    color=MUTED_TEXT, fontsize=15)
    blocks = figure.subfigures(len(block_rows), 1, height_ratios=block_heights,
                               hspace=BLOCK_GAP)

    for spec, block in zip(specs, blocks):
        _open_block(block, _stream_gap_header(spec))
        _fill_stream_gap_block(block, buckets_by_mode, labels, spec, period_us)
    _open_block(blocks[-1], CADENCE_HEADER)
    _fill_cadence_block(blocks[-1], buckets_by_mode, labels, specs, period_us)
    _save(figure, path, plt, SURFACE)


def export_inner_plot(path, records_by_mode, specs, period_us):
    """Both cameras' inner-camera sync in one figure, a titled block each.

    A block per camera rather than both overlaid on one set of axes: two
    cameras of the same model on the same firmware routinely land on the same
    offset, and overlaid they simply hide each other. Given a block each both
    are always fully visible, and the shared y-range still makes them directly
    comparable.

    Plotted in raw microseconds with no wrapping and no centring - unlike
    every cross-camera panel, this number is absolute and its actual value IS
    the finding, so shifting it would throw the answer away.
    """
    plt = _plt()
    labels = modes_present(records_by_mode)
    if not labels:
        return
    label_a, label_b = specs[0].label, specs[1].label
    by_panel, limits = _inner_panels(records_by_mode, labels, label_a, label_b)

    block_height = BLOCK_ROW_HEIGHT_IN + BLOCK_HEADER_IN
    figure = plt.figure(figsize=(6 * len(labels),
                                 block_height * len(ROLES) + FIGURE_TITLE_IN),
                        layout="constrained")
    figure.patch.set_facecolor(SURFACE)
    figure.suptitle("Inner-camera sync: {} vs {} on the same device "
                    "(same clock, same frameset - exact, never wrapped)".format(
                        label_a, label_b), color=MUTED_TEXT, fontsize=15)
    for role, block in zip(ROLES, figure.subfigures(len(ROLES), 1, hspace=BLOCK_GAP)):
        _open_block(block, _inner_header(role, label_a, label_b))
        _fill_inner_block(block, by_panel, labels, role, label_a, label_b, limits)
    _save(figure, path, plt, SURFACE)


# --- one file per block ---------------------------------------------------
# The same blocks again, each on its own page. The overviews above are for
# comparing streams and cameras side by side; these are for looking at one
# question on a screen without scrolling past the others.

def export_stream_gap_plot(path, records_by_mode, specs, spec, period_us):
    """One stream's cross-camera gap, alone."""
    plt = _plt()
    labels = modes_present(records_by_mode)
    if not labels:
        return
    buckets_by_mode = {mode_label: split_by_role_and_stream(
        records_by_mode[mode_label], specs) for mode_label in labels}
    figure = _standalone(plt, _stream_gap_header(spec), len(GAP_ROWS), len(labels))
    _fill_stream_gap_block(figure, buckets_by_mode, labels, spec, period_us)
    _save(figure, path, plt, BLOCK_SURFACE)


def export_cadence_plot(path, records_by_mode, specs, period_us):
    """The frame cadence of every stream, alone."""
    plt = _plt()
    labels = modes_present(records_by_mode)
    if not labels:
        return
    buckets_by_mode = {mode_label: split_by_role_and_stream(
        records_by_mode[mode_label], specs) for mode_label in labels}
    figure = _standalone(plt, CADENCE_HEADER, 1, len(labels))
    _fill_cadence_block(figure, buckets_by_mode, labels, specs, period_us)
    _save(figure, path, plt, BLOCK_SURFACE)


def export_inner_camera_plot(path, records_by_mode, specs, role, period_us):
    """One camera's inner-camera sync, alone - still on the y-range shared
    with the other camera's file, so the two can be compared directly."""
    plt = _plt()
    labels = modes_present(records_by_mode)
    if not labels:
        return
    label_a, label_b = specs[0].label, specs[1].label
    by_panel, limits = _inner_panels(records_by_mode, labels, label_a, label_b)
    figure = _standalone(plt, _inner_header(role, label_a, label_b), 1, len(labels))
    _fill_inner_block(figure, by_panel, labels, role, label_a, label_b, limits)
    _save(figure, path, plt, BLOCK_SURFACE)


def plot_jobs(specs):
    """Every figure to write, as (filename, callable(path)) pairs.

    Two combined overviews plus one file per block. Named so the per-block
    files sort together under their own prefixes, and so a stream's file is
    named after the stream itself rather than "stream A" - two runs pairing
    different streams then produce differently-named files instead of
    overwriting each other's.
    """
    jobs = [("trigger_sync.png", export_cross_plot),
            ("inner_camera_sync.png", export_inner_plot)]
    for spec in specs:
        jobs.append(("cross_sync_{}.png".format(spec.label),
                     lambda path, r, s, p, spec=spec: export_stream_gap_plot(
                         path, r, s, spec, p)))
    jobs.append(("frame_cadence.png", export_cadence_plot))
    for role in ROLES:
        jobs.append(("inner_sync_{}.png".format(role),
                     lambda path, r, s, p, role=role: export_inner_camera_plot(
                         path, r, s, role, p)))
    return jobs


# --------------------------------------------------------------------------
# Hardware (Jetson-only; no automated tests)
# --------------------------------------------------------------------------

def sync_mode_range(device):
    """(min, max) the device accepts for inter_cam_sync_mode, or None.

    This is what distinguishes "genlock is not implemented in this firmware"
    from "genlock is there but did not work" - the two need completely
    different next steps, and only the first is visible from the range.
    """
    rs = base._rs()
    for sensor in device.query_sensors():
        if sensor.supports(rs.option.inter_cam_sync_mode):
            try:
                option_range = sensor.get_option_range(rs.option.inter_cam_sync_mode)
                return (option_range.min, option_range.max)
            except Exception:
                return None
    return None


def read_sync_mode(device):
    """The device's own reported inter_cam_sync_mode, or None if unreadable."""
    rs = base._rs()
    for sensor in device.query_sensors():
        if sensor.supports(rs.option.inter_cam_sync_mode):
            try:
                return sensor.get_option(rs.option.inter_cam_sync_mode)
            except Exception:
                return None
    return None


def apply_sync_mode_to_all(devices, sync_mode):
    """Set the mode on both cameras and CONFIRM each one took it.

    base.apply_sync_mode prints whatever the device reports but never compares
    it to what was asked for. That was fine while every mode used was 0 or 2,
    which any D400 accepts; mode 3 is the first one a given firmware may not
    implement, and a silently clamped write would produce a phase that reads
    like a genuine mode-3 result. Returns the warnings to put in the report.
    """
    warnings = []
    for role in ROLES:
        base.apply_sync_mode(devices[role], sync_mode, role)
        warning = sync_mode_warning(role, sync_mode, read_sync_mode(devices[role]))
        if warning:
            print("  WARNING: {}".format(warning))
            warnings.append("WARNING: " + warning)
    return warnings


def _depth_sync_stream(specs):
    """The depth geometry to co-enable, or None.

    Intel's firmware wants depth and IR configured together. Left to itself
    the pipeline satisfies that internally, in an open order we cannot see -
    and on this project's own hardware, RGB-before-IR produced a FIXED ~11.3ms
    inter-sensor offset where IR-before-RGB measured the true ~3.5ms.
    Co-enabling depth removes the ambiguity. Off by default because it costs
    real bus bandwidth (~55 MB/s at 1280x720@30) and raises frame drops.
    """
    for spec in specs:
        if spec.type_name == "infrared":
            return (spec.width, spec.height, spec.fps)
    return None


def _build_config(serial, specs, enable_depth_sync):
    rs = base._rs()
    formats = {"infrared": (rs.stream.infrared, rs.format.y8),
               "color": (rs.stream.color, rs.format.bgr8),
               "depth": (rs.stream.depth, rs.format.z16)}
    config = rs.config()
    config.enable_device(serial)
    for spec in specs:
        stream_type, stream_format = formats[spec.type_name]
        config.enable_stream(stream_type, spec.index, spec.width, spec.height,
                             stream_format, spec.fps)
    if enable_depth_sync:
        geometry = _depth_sync_stream(specs)
        if geometry is not None:
            width, height, fps = geometry
            config.enable_stream(rs.stream.depth, 0, width, height, rs.format.z16, fps)
    return config


def capture_mode(mode_label, serials, devices, specs, duration_s, enable_depth_sync):
    """Single-threaded capture of BOTH streams on BOTH cameras: one loop
    polling both pipelines with poll_for_frames(), so neither camera can
    block the other. Safe because nothing is paired ACROSS cameras - and the
    within-camera pairing comes free from the frameset, not from matching.

    Every frame that a single poll returned gets the same frameset_id, which
    is what inner_gap_series uses instead of a matching heuristic.
    """
    rs = base._rs()
    metadata = rs.frame_metadata_value.frame_timestamp
    global_domain = rs.timestamp_domain.global_time

    pipelines, records = {}, []
    last_number = {(role, spec.label): None for role in ROLES for spec in specs}
    frameset_counter = {role: 0 for role in ROLES}
    warned = {role: False for role in ROLES}
    started_s = time.perf_counter()

    try:
        for role, serial in zip(ROLES, serials):
            base.enable_global_time(devices[role], role, quiet=True)
            pipeline = rs.pipeline()
            profile = pipeline.start(_build_config(serial, specs, enable_depth_sync))
            # Re-asserted on the device the STARTED pipeline resolved: the
            # handle above came from a separate rs.context() and setting an
            # option through it may not reach the streaming one.
            base.enable_global_time(profile.get_device(), role, quiet=True)
            pipelines[role] = pipeline
        print("  polling both cameras for {:.1f}s...".format(duration_s))

        deadline = time.perf_counter() + duration_s
        while time.perf_counter() < deadline:
            got_any = False
            for role, serial in zip(ROLES, serials):
                frameset = pipelines[role].poll_for_frames()
                if not frameset:
                    continue
                fresh = []
                for spec in specs:
                    frame = base._frame_for_stream(frameset, spec)
                    if not frame:
                        continue
                    number = frame.get_frame_number()
                    if last_number[(role, spec.label)] == number:
                        continue  # poll returned the same frame again
                    if not frame.supports_frame_metadata(metadata):
                        raise RuntimeError("{} ({}) {}: no HW timestamp metadata.".format(
                            role, serial, spec.label))
                    fresh.append((spec, frame, number))
                if not fresh:
                    continue
                # One id per poll that produced anything new: frames sharing
                # it were grouped by the SDK, which IS the inner-camera pair.
                frameset_counter[role] += 1
                got_any = True
                host_recv_s = time.perf_counter()
                for spec, frame, number in fresh:
                    last_number[(role, spec.label)] = number
                    in_global = frame.get_frame_timestamp_domain() == global_domain
                    if not in_global and not warned[role]:
                        warned[role] = True
                        print("    {}: waiting for global time to converge...".format(role))
                    if not in_global and time.perf_counter() - started_s > GLOBAL_TS_GRACE_S:
                        # Keep going: HW timestamps still work, and the Global
                        # panels simply stay empty rather than failing the run.
                        pass
                    records.append(StreamFrameRecord(
                        camera_role=role, serial=serial, stream=spec.label,
                        frameset_id=frameset_counter[role], frame_number=number,
                        hw_ts_us=float(frame.get_frame_metadata(metadata)),
                        global_ts_us=frame.get_timestamp() * 1000.0 if in_global else None,
                        host_recv_s=host_recv_s))
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

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Check cross-camera AND inner-camera stream sync on two "
                    "RealSense cameras, with two streams open per camera.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--serials", nargs=2, metavar=("CAM1", "CAM2"))
    parser.add_argument("--streams", nargs=2, metavar="SPEC",
                        default=["ir:1:1280x720@30", "color:0:1280x720@30"],
                        help="Two TYPE:INDEX:WIDTHxHEIGHT@FPS specs, opened on BOTH cameras")
    parser.add_argument("--duration", type=float, default=20.0,
                        help="Capture seconds PER MODE")
    parser.add_argument("--fps", type=int, default=30, help="TSC generator rate")
    parser.add_argument("--duty", type=int, default=50, help="TSC duty cycle percent")
    parser.add_argument("--enable-depth-sync", action="store_true",
                        help="Co-enable depth alongside IR. Costs bandwidth; try this if "
                             "the inner-camera gap reads a suspiciously fixed ~11.3ms")
    parser.add_argument("--trigger-script", default=base._default_trigger_script(),
                        help="Path to ext_sync_gen.py; defaults to one next to this script")
    parser.add_argument("--output-dir", default=None,
                        help="Default: output/check_stream_sync/<timestamp>")
    args = parser.parse_args(argv)
    args.stream_specs = [parse_stream_spec(text) for text in args.streams]
    validate_stream_pair(*args.stream_specs)
    return args


def main(argv=None):
    args = parse_args(argv)
    if not args.trigger_script:
        print("No ext_sync_gen.py found next to this script; pass --trigger-script.",
              file=sys.stderr)
        return 1

    specs = args.stream_specs
    period_us = 1_000_000.0 / args.fps
    output_dir = args.output_dir or os.path.join(
        "output", "check_stream_sync", time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(output_dir, exist_ok=True)

    serials = base._resolve_serials(args)
    devices = {role: base._find_device(serial) for role, serial in zip(ROLES, serials)}
    rs = base._rs()
    print("Output directory: {}".format(os.path.abspath(output_dir)))
    mode_ranges = {role: sync_mode_range(devices[role]) for role in ROLES}
    for role in ROLES:
        device = devices[role]
        print("{}: {} (serial {}, firmware {})".format(
            role, device.get_info(rs.camera_info.name),
            device.get_info(rs.camera_info.serial_number),
            device.get_info(rs.camera_info.firmware_version)))
        # Printed up front so it is clear which phases can even run on this
        # firmware, instead of a phase failing several minutes into the run.
        print("      inter_cam_sync_mode supported: {}   ({})".format(
            describe_mode_range(mode_ranges[role]),
            ", ".join("{}={}".format(mode, SYNC_MODE_NAMES[mode])
                      for mode in sorted(SYNC_MODE_NAMES)
                      if mode_ranges[role] is None
                      or mode_ranges[role][0] <= mode <= mode_ranges[role][1])))
    print("Streams per camera: {}   and   {}".format(specs[0].describe(), specs[1].describe()))
    print("Trigger period: {:.0f} us ({} Hz)".format(period_us, args.fps))
    print("Depth co-enabled for IR sync: {}".format(
        "yes" if args.enable_depth_sync else "no"))
    print("Trigger script: {}".format(os.path.abspath(args.trigger_script)))
    print("No pairing ACROSS cameras, no threads. Inner-camera pairing comes "
          "from the frameset.")
    if specs[0].fps != specs[1].fps:
        print("WARNING: the two streams run at different fps ({} vs {}). The SDK's "
              "syncer may not group them into one frameset, in which case the "
              "inner-camera panels stay empty.".format(specs[0].fps, specs[1].fps))

    print("\nEnabling RealSense global time...")
    for role in ROLES:
        base.enable_global_time(devices[role], role)

    records_by_mode = {}
    report_lines = []
    try:
        for mode_label, sync_mode, trigger_on in MODES:
            print("\n" + "=" * 78)
            print("MODE '{}': inter_cam_sync_mode {}, TSC {}".format(
                mode_label, describe_sync_mode(sync_mode),
                "ENABLED" if trigger_on else "DISABLED"))
            print("=" * 78)
            blocked = [message for message in
                       (unsupported_mode_message(role, sync_mode, mode_ranges[role])
                        for role in ROLES) if message]
            if blocked:
                # Skipped rather than attempted: writing an out-of-range value
                # raises a bare 'out of range value for argument "value"',
                # which says nothing about what the device can actually do.
                print("  PHASE SKIPPED - mode not supported by this hardware:")
                for message in blocked:
                    print("    {}".format(message))
                report_lines += ["", "=" * 78,
                                 "MODE: {} - PHASE SKIPPED".format(mode_label.upper()),
                                 "=" * 78] + ["  " + m for m in blocked] + [
                    "  Nothing was written to the camera, and the other phases are "
                    "unaffected.",
                    "  inter_cam_sync_mode {} is a firmware capability - if this "
                    "device should".format(sync_mode),
                    "  have it, that is a firmware-version question, not a setting "
                    "this script can",
                    "  reach."]
                continue
            try:
                warnings = apply_sync_mode_to_all(devices, sync_mode)
                base.run_trigger_script(args.trigger_script,
                                        "enable" if trigger_on else "disable",
                                        args.fps, args.duty)
                records = capture_mode(mode_label, serials, devices, specs,
                                       args.duration, args.enable_depth_sync)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                # One phase failing must not cost the phases already captured,
                # and mode 3 is exactly the phase a device may reject outright.
                # Recorded as a report line so it appears in the final summary
                # rather than only in the scrollback.
                print("  PHASE FAILED: {}".format(exc))
                report_lines += ["", "=" * 78,
                                 "MODE: {} - PHASE FAILED".format(mode_label.upper()),
                                 "=" * 78, "  {}".format(exc)]
                continue
            records_by_mode[mode_label] = records
            block = format_mode_report(mode_label, records, specs, period_us)
            if warnings:
                # Prepended, not appended: without it a reader takes the whole
                # block at face value as a result for the mode they asked for.
                block = block[:4] + ["  " + line for line in warnings] + block[4:]
            if not records:
                block += ["  NO FRAMES AT ALL in this mode.",
                          "         For mode {} that may be the REAL answer, not a "
                          "failure: a camera".format(sync_mode),
                          "         genuinely gated by an external trigger produces "
                          "nothing when the",
                          "         trigger it is waiting for never arrives, or does "
                          "not match what the",
                          "         firmware expects. Compare against the mode0 "
                          "phases above."]
            report_lines += block
            print("\n".join(block))
    except KeyboardInterrupt:
        print("\nInterrupted between modes - keeping what was captured.")
    finally:
        print("\nCleaning up...")
        try:
            base.run_trigger_script(args.trigger_script, "disable")
        except Exception as exc:
            print("  WARNING: could not disable the TSC: {}".format(exc))
        for role in ROLES:
            try:
                base.apply_sync_mode(devices[role], SYNC_MODE_DEFAULT, role)
            except Exception as exc:
                print("  WARNING: could not reset {} sync mode: {}".format(role, exc))

        write_frames_csv(os.path.join(output_dir, "frames.csv"), records_by_mode)
        print("  Wrote frames.csv")
        if any(records_by_mode.values()):
            for name, exporter in plot_jobs(specs):
                exporter(os.path.join(output_dir, name), records_by_mode,
                         specs, period_us)
                print("  Wrote {}".format(name))

    print("\n".join(report_lines))
    print("\nAll output in: {}".format(os.path.abspath(output_dir)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
