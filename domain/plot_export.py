"""Static end-of-session plot export.

Matplotlib, not pyqtgraph - a live widget and a saved-file renderer are
different jobs. Restores the kind of after-the-fact plot
optical_sync_poc_/pipeline_sync_test_diff.py used to save before this
GUI's live view took over; this reads the same buffered rows
TestSession.stop() returns and domain.csv_export.export_session_csvs
writes, so it never recomputes a metric - it only re-renders numbers
that are already final.

Three separate, stacked subplots (sharing one x-axis, "Pair index"), not
two - Pairing gap (us), Position gap (ms), and Frame drop (per-stream) each
get their own y-axis. An earlier version put pairing_gap_us and
position_gap_ms on the SAME y-axis labeled just "Gap": since pairing gap's
outlier threshold is ~100,000us and position gap is typically single-digit
ms, one series would dwarf/flatten the other whenever both were legitimately
in-range - the exact "shared/dual axis is misleading" problem
gui/pages/live_session_page.py's own docstring already rejects for the live
charts. Frame drop is split by stream (stream_a_frame_drop/
stream_b_frame_drop), mirrored as +1/-1 the same way the live drop_plot
already does (gui/pages/live_session_page.py) so a simultaneous A+B drop
can't occlude one line behind the other - the old version collapsed both
streams into one combined 0/1 "did anything drop" line, losing which
stream actually dropped.

Colors and dark-mode chrome (background/gridlines/axis text) come from
domain/plot_theme.py, the same module gui/widgets/live_plot.py's LivePlot
sources its own theme from, so this static export and the live view never
visually drift apart.

Figure width scales with how many pairs the session ran (see
_figure_width) instead of a fixed size - a long run on a fixed-width figure
squeezes thousands of points into the same few inches and reads as a dense,
illegible smear. This deliberately does NOT decimate/drop any data point to
control size instead: every row is still plotted, so a very long run gets a
wider (but still complete and legible) image rather than a smaller,
lossy one - fidelity matters more than file size for this metrology tool.
"""

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from domain.plot_theme import (
    SURFACE, GRIDLINE, MUTED_TEXT,
    PAIRING_GAP_COLOR, POSITION_GAP_COLOR, STREAM_A_DROP_COLOR, STREAM_B_DROP_COLOR,
    CROSS_CAMERA_COLORS,
)

# Width grows with run length so a long session's line has room to breathe
# instead of flattening into a smear, but is capped so an extremely long
# run doesn't produce an unreasonably large image file. Tuned against a
# typical ~150 pairs/inch density; adjust these three constants together
# if that ever needs revisiting.
_MIN_FIGURE_WIDTH = 12.0
_MAX_FIGURE_WIDTH = 48.0
_PAIRS_PER_INCH = 150.0
_FIGURE_HEIGHT = 10.0


def _figure_width(num_rows):
    return min(max(_MIN_FIGURE_WIDTH, num_rows / _PAIRS_PER_INCH), _MAX_FIGURE_WIDTH)


def _style_axis(ax):
    ax.set_facecolor(SURFACE)
    ax.grid(True, color=GRIDLINE, alpha=0.6)
    ax.tick_params(colors=MUTED_TEXT)
    ax.xaxis.label.set_color(MUTED_TEXT)
    ax.yaxis.label.set_color(MUTED_TEXT)
    for spine in ax.spines.values():
        spine.set_color(GRIDLINE)
    # Guarded: an axis with zero labeled lines (e.g. export_cross_camera_
    # plot's empty-rows case, where no groups exist to plot() at all) would
    # otherwise raise a "No artists with labels found" UserWarning on every
    # such call - every existing caller always has labeled lines, so this
    # is a no-op for them.
    if ax.get_legend_handles_labels()[0]:
        ax.legend(facecolor=SURFACE, edgecolor=GRIDLINE, labelcolor=MUTED_TEXT)


def _build_figure(rows):
    """Builds (but doesn't save/close) the 3-axis figure - split out from
    export_session_plot so tests can inspect the plotted line data directly
    without round-tripping through a saved PNG."""
    pair_indices = [row["pair_index"] for row in rows]
    # Excluded pairs (syncer_outlier / frame_drop / warmup / miss) can carry
    # wild values (e.g. a multi-hundred-thousand-us pairing gap during the
    # initial auto-exposure warmup) - plotting them would force the y-axis
    # to that scale and flatten every legitimate value into an invisible
    # line near zero, exactly like optical_sync_poc_/pipeline_sync_test_diff.py's
    # plot_position_gap_over_time avoids via `np.where(valid, gap_ms, nan)`.
    # NaN here does the same job: matplotlib breaks the line instead of
    # plotting or connecting through a known-bad point.
    pairing_gap = [_to_plot_value(row.get("pairing_gap_us"), row.get("pairing_gap_us_excluded")) for row in rows]
    position_gap = [_to_plot_value(row.get("position_gap_ms"), row.get("position_gap_ms_excluded")) for row in rows]

    # Mirrored +1/-1, not two 0/1 lines - a pair where BOTH streams drop at
    # once would otherwise draw one line directly on top of the other, same
    # convention gui/pages/live_session_page.py's live drop_plot already uses.
    stream_a_drop = [1 if row.get("stream_a_frame_drop") else 0 for row in rows]
    stream_b_drop = [-1 if row.get("stream_b_frame_drop") else 0 for row in rows]

    fig, (pairing_ax, position_ax, drop_ax) = plt.subplots(
        3, 1, figsize=(_figure_width(len(rows)), _FIGURE_HEIGHT), sharex=True,
    )
    fig.patch.set_facecolor(SURFACE)

    pairing_ax.plot(pair_indices, pairing_gap, label="HW TS Latency (us)", color=PAIRING_GAP_COLOR)
    pairing_ax.set_ylabel("Pairing gap (us)")
    _style_axis(pairing_ax)

    position_ax.plot(pair_indices, position_gap, label="Optical Sync (ms)", color=POSITION_GAP_COLOR)
    position_ax.set_ylabel("Position gap (ms)")
    _style_axis(position_ax)

    drop_ax.plot(pair_indices, stream_a_drop, label="Stream A frame drop", color=STREAM_A_DROP_COLOR)
    drop_ax.plot(pair_indices, stream_b_drop, label="Stream B frame drop", color=STREAM_B_DROP_COLOR)
    drop_ax.set_ylabel("Frame drop")
    drop_ax.set_xlabel("Pair index")
    _style_axis(drop_ax)

    fig.tight_layout()
    return fig


def export_session_plot(rows, path):
    fig = _build_figure(rows)
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def _to_plot_value(value, excluded):
    return value if (value is not None and not excluded) else float("nan")


def _build_cross_camera_figure(cross_rows):
    """Two stacked subplots (sharing one x-axis, "Pair index") - HW TS
    Latency and Optical Sync each get their own y-axis, same "wildly
    different scales" reasoning _build_figure's own 3-axis split already
    uses for the intra-camera plot. Same NaN-for-excluded convention, one
    line per (slave_camera_id, stream_identity) pair rather than a fixed
    axis-per-metric layout - engine.cross_camera_reconciler's own
    pair_index is a synthetic, shared-across-all-pairs counter (not
    comparable to any one camera's own pair_index), so it's used here only
    as this plot's own x-axis, not cross-referenced against per-camera
    CSVs. Split out from export_cross_camera_plot so tests can inspect the
    plotted line data directly, same reason _build_figure is split from
    export_session_plot."""
    groups = {}
    for row in cross_rows:
        key = (row["slave_camera_id"], row["stream_identity"])
        groups.setdefault(key, []).append(row)

    fig, (pairing_ax, position_ax) = plt.subplots(
        2, 1, figsize=(_figure_width(len(cross_rows)), _FIGURE_HEIGHT), sharex=True,
    )
    fig.patch.set_facecolor(SURFACE)

    for index, key in enumerate(sorted(groups.keys())):
        pair_rows = groups[key]
        pair_indices = [row["pair_index"] for row in pair_rows]
        color = CROSS_CAMERA_COLORS[index % len(CROSS_CAMERA_COLORS)]

        pairing_values = [_to_plot_value(row.get("pairing_gap_us"), row.get("pairing_gap_us_excluded"))
                           for row in pair_rows]
        pairing_ax.plot(pair_indices, pairing_values, label="{} {}".format(*key), color=color)

        position_values = [_to_plot_value(row.get("position_gap_ms"), row.get("position_gap_ms_excluded"))
                            for row in pair_rows]
        position_ax.plot(pair_indices, position_values, label="{} {}".format(*key), color=color)

    pairing_ax.set_ylabel("Cross-camera HW TS latency (us)")
    _style_axis(pairing_ax)

    position_ax.set_ylabel("Cross-camera Optical Sync (ms)")
    position_ax.set_xlabel("Pair index")
    _style_axis(position_ax)

    fig.tight_layout()
    return fig


def export_cross_camera_plot(cross_rows, path):
    fig = _build_cross_camera_figure(cross_rows)
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
