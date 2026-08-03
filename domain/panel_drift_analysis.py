"""Pure analysis/plotting for tools/panel_drift_measure.py's/tools/
panel_drift_stats.py's "how much do the two LED panels drift apart"
question. No Qt, no pyrealsense2, no hardware - takes the same row dicts
domain.csv_export.export_session_csvs writes/domain.plot_export.
export_session_plot reads, same convention as every other domain module.

Real hardware runs showed the raw position_gap_ms series is NOT simply a
clean staircase - a run can show several-second-long oscillation between
two adjacent values (e.g. right after a transition, before the gap
settles at its new level) on top of the genuine underlying drift trend.
A naive "did the value change since the last sample" check would count
every one of those oscillations as its own "step", drowning out the real,
sustained transitions in noise. Two techniques here are both robust to
that on purpose:

- linear_fit_rate: a single least-squares line through every sample - the
  most robust single "overall drift rate" number, since scattered noise
  anywhere in the run only nudges the fit slightly rather than being
  individually counted as if each fluctuation were a real event.
- bin_series/find_transitions: groups samples into fixed-width time bins
  and takes the MEDIAN per bin before looking for a value change between
  consecutive bins - a multi-second oscillation within one bin collapses
  to whichever value was more common, so only genuine, sustained level
  changes register as transitions.
"""

import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def parse_gap_series(rows, value_key="position_gap_ms", excluded_key="position_gap_ms_excluded",
                      ts_key="stream_a_ts_us"):
    """Extracts (elapsed_s, value) pairs for one metric column, in elapsed
    seconds from the first row's own HW frame timestamp - accurate
    regardless of any assumed constant fps. Skips excluded/missing samples
    entirely (not NaN-padded, unlike domain.plot_export.export_session_plot -
    this only ever plots one series, so there's no multi-series pair_index
    alignment to preserve). Returns ([elapsed_s, ...], [value, ...]), both
    empty if there's nothing usable."""
    timestamped = [r for r in rows if r.get(ts_key) is not None]
    if not timestamped:
        return [], []

    t0 = timestamped[0][ts_key]
    elapsed_s = []
    values = []
    for r in timestamped:
        if r.get(value_key) is None or r.get(excluded_key):
            continue
        elapsed_s.append((r[ts_key] - t0) / 1_000_000.0)
        values.append(r[value_key])
    return elapsed_s, values


def linear_fit_rate(elapsed_s, values):
    """Least-squares (slope, intercept) through every given point, slope in
    value-units per second. None if there are fewer than 2 distinct elapsed
    times to fit a line through."""
    if len(elapsed_s) < 2:
        return None
    ts = np.array(elapsed_s, dtype=float)
    vs = np.array(values, dtype=float)
    if ts.max() == ts.min():
        return None
    slope, intercept = np.polyfit(ts, vs, 1)
    return float(slope), float(intercept)


def bin_series(elapsed_s, values, bin_seconds):
    """Groups (elapsed_s, value) points into fixed-width time bins and takes
    the MEDIAN value per bin - see module docstring for why median-per-bin
    rather than a raw per-sample check. Returns [(bin_center_s, median_value),
    ...] for bins with at least one sample, in time order."""
    if not elapsed_s:
        return []
    buckets = {}
    for t, v in zip(elapsed_s, values):
        bucket_index = int(t // bin_seconds)
        buckets.setdefault(bucket_index, []).append(v)
    result = []
    for bucket_index in sorted(buckets):
        bucket_values = buckets[bucket_index]
        center = bucket_index * bin_seconds + bin_seconds / 2.0
        result.append((center, float(np.median(bucket_values))))
    return result


def find_transitions(binned_series):
    """Where the BINNED (already median-smoothed) series' value actually
    changes from one bin to the next - each one is a genuine, sustained
    level change, not a momentary blip within a single bin (already
    absorbed by the median). Returns [(time_s, old_value, new_value), ...]."""
    transitions = []
    prev_value = None
    for t, v in binned_series:
        if prev_value is not None and v != prev_value:
            transitions.append((t, prev_value, v))
        prev_value = v
    return transitions


def compute_local_rates(transitions):
    """Local drift-rate estimate (value-units per second) leading up to each
    transition after the first - the practical stand-in for "the derivative"
    of a staircase-shaped signal: literal per-sample differencing would be
    zero almost everywhere with a spike at each jump, not something you can
    read a trend from. Returns [(time_s, rate_per_s), ...], one entry per
    transition after the first (needs a previous transition to measure an
    interval against)."""
    rates = []
    prev_t = None
    for t, old, new in transitions:
        if prev_t is not None:
            dt = t - prev_t
            if dt > 0:
                rates.append((t, (new - old) / dt))
        prev_t = t
    return rates


def summarize_drift(rows, bin_seconds=10.0, value_key="position_gap_ms",
                     excluded_key="position_gap_ms_excluded"):
    """One-stop analysis: parses the metric series, fits an overall linear
    drift rate, bins+smooths it, and derives clean transitions/local rates
    from the smoothed series. Returns a dict consumed by both
    export_drift_over_time_plot/export_local_rate_plot and by callers that
    just want the numbers (e.g. tools/panel_drift_stats.py's console
    summary) - elapsed_s_range is None if there's nothing usable at all."""
    elapsed_s, values = parse_gap_series(rows, value_key, excluded_key)
    fit = linear_fit_rate(elapsed_s, values)
    binned = bin_series(elapsed_s, values, bin_seconds)
    transitions = find_transitions(binned)
    local_rates = compute_local_rates(transitions)

    return {
        "elapsed_s_range": (elapsed_s[0], elapsed_s[-1]) if elapsed_s else None,
        "n_samples": len(elapsed_s),
        "elapsed_s": elapsed_s,
        "values": values,
        "overall_slope_per_s": fit[0] if fit else None,
        "overall_intercept": fit[1] if fit else None,
        "bin_seconds": bin_seconds,
        "binned": binned,
        "transitions": transitions,
        "local_rates": local_rates,
    }


def export_drift_over_time_plot(rows, path, bin_seconds=10.0):
    """Elapsed-seconds x-axis (from real HW frame timestamps, not an assumed
    constant fps) - the raw series plotted faint, the binned/smoothed median
    plotted bold, transitions marked from the SMOOTHED series (not the raw
    one - see module docstring), and the overall linear-fit line. Returns
    summarize_drift's own summary dict, or None if there's nothing to plot."""
    summary = summarize_drift(rows, bin_seconds)
    if summary["elapsed_s_range"] is None:
        return None

    elapsed_s, values = summary["elapsed_s"], summary["values"]
    total_elapsed = summary["elapsed_s_range"][1] - summary["elapsed_s_range"][0]
    # Scales with the actual run length so a 10-15 minute run doesn't cram
    # its whole timeline into the same width as a 3-minute one - clamped so
    # it doesn't keep growing unreasonably for very long runs.
    fig_width = min(20, max(10, total_elapsed / 60.0 * 1.5))
    fig, ax = plt.subplots(figsize=(fig_width, 5))

    ax.plot(elapsed_s, values, color="tab:green", alpha=0.3, linewidth=0.8, label="Raw position gap (ms)")
    if summary["binned"]:
        bt = [t for t, _ in summary["binned"]]
        bv = [v for _, v in summary["binned"]]
        ax.plot(bt, bv, color="tab:blue", linewidth=2, marker="o", markersize=3,
                 label="Binned median ({:.0f}s bins)".format(bin_seconds))
    for t, _, _ in summary["transitions"]:
        ax.axvline(t, color="tab:red", linestyle="--", alpha=0.4)

    if summary["overall_slope_per_s"] is not None:
        slope, intercept = summary["overall_slope_per_s"], summary["overall_intercept"]
        ts = np.array(elapsed_s)
        ax.plot(ts, slope * ts + intercept, color="tab:orange", linestyle=":",
                 label="Linear fit: {:.4f} ms/s ({:.2f} ms/min)".format(slope, slope * 60))

    ax.set_xlabel("Elapsed time (s)")
    ax.set_ylabel("Position gap (ms) - panel A vs panel B")
    ax.set_title("Panel-to-panel drift over time (dashed = binned/smoothed step change)")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)

    return summary


def export_local_rate_plot(summary, path):
    """The practical "derivative" chart: local drift rate (ms/s) between
    each consecutive pair of smoothed transitions, over elapsed time - lets
    you see whether the drift rate itself is trending (e.g. faster during
    an initial thermal warm-up, settling to a steadier rate later) rather
    than the one fixed number the overall linear fit gives. False if there
    weren't at least 2 transitions to compute a local rate between."""
    local_rates = summary["local_rates"]
    if not local_rates:
        return False

    ts = [t for t, _ in local_rates]
    rs = [r for _, r in local_rates]

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.step(ts, rs, where="post", color="tab:purple", marker="o", markersize=4, label="Local drift rate (ms/s)")
    if summary["overall_slope_per_s"] is not None:
        ax.axhline(summary["overall_slope_per_s"], color="tab:orange", linestyle=":",
                    label="Overall linear fit: {:.4f} ms/s".format(summary["overall_slope_per_s"]))
    ax.set_xlabel("Elapsed time (s)")
    ax.set_ylabel("Local drift rate (ms/s)")
    ax.set_title("Local drift rate between transitions (the practical 'derivative')")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return True


def export_rate_consistency_plot(rates, path):
    """rates: [(run_index, slope_ms_per_s_or_None), ...] - one entry per
    attempted run of a repeated (e.g. overnight) batch, None for a run
    where no stepping was detected at all (some rigs show the panels
    stepping only every other run - a hardware issue this function doesn't
    try to explain, just surface). Plots successful runs' rates as points
    with a mean +/-1 stdev band, and marks failed runs distinctly along
    the same chart so the pass/fail pattern stays visible too, not just
    the rate of the runs that worked. Returns True if anything was
    plotted, False for an empty rates list."""
    if not rates:
        return False

    successful = [(i, r) for i, r in rates if r is not None]
    failed_indices = [i for i, r in rates if r is None]

    fig, ax = plt.subplots(figsize=(max(8, len(rates) * 0.3), 5))

    if successful:
        xs = [i for i, _ in successful]
        ys = [r for _, r in successful]
        ax.plot(xs, ys, "o-", color="tab:blue", label="Measured drift rate (ms/s)")
        mean_rate = float(np.mean(ys))
        ax.axhline(mean_rate, color="tab:orange", linestyle=":", label="Mean: {:.4f} ms/s".format(mean_rate))
        if len(ys) > 1:
            std_rate = float(np.std(ys, ddof=1))
            ax.axhspan(mean_rate - std_rate, mean_rate + std_rate, color="tab:orange", alpha=0.15,
                        label="+/-1 stdev: {:.4f} ms/s".format(std_rate))

    if failed_indices:
        # Marked at the bottom of the successful runs' own range (or at 0
        # if nothing succeeded at all) - visible without distorting the
        # y-axis scale that matters for the successful rates.
        y_for_fail = min((r for _, r in successful), default=0.0)
        ax.plot(failed_indices, [y_for_fail] * len(failed_indices), "rx", markersize=10,
                 label="Failed (no stepping detected)")

    ax.set_xlabel("Run number")
    ax.set_ylabel("Measured drift rate (ms/s)")
    ax.set_title("Drift-rate consistency across {} run(s)".format(len(rates)))
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return True


def export_overlay_plot(run_series, path):
    """run_series: [(label, elapsed_s, values), ...] - overlays every
    successful run's own raw position_gap_ms(t) series on one chart (each
    already starts at its own t=0, from parse_gap_series/summarize_drift's
    own elapsed_s), to visually compare whether independent runs follow a
    similar trend/rate. Returns True if anything was plotted, False for an
    empty run_series."""
    if not run_series:
        return False

    fig, ax = plt.subplots(figsize=(12, 6))
    cmap = plt.get_cmap("tab20")
    for idx, (label, elapsed_s, values) in enumerate(run_series):
        ax.plot(elapsed_s, values, alpha=0.6, linewidth=1, color=cmap(idx % 20), label="Run {}".format(label))

    ax.set_xlabel("Elapsed time (s)")
    ax.set_ylabel("Position gap (ms)")
    ax.set_title("All successful runs overlaid ({} run(s))".format(len(run_series)))
    ax.grid(True, alpha=0.3)
    if len(run_series) <= 15:
        ax.legend(fontsize=8, ncol=2)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return True
