"""Standalone tool - NOT part of the shipped app, no automated tests for
this file itself (the analysis it calls into, domain/panel_drift_analysis.py,
IS unit-tested - see tests/domain/test_panel_drift_analysis.py - same "pure
logic is tested, the thin hardware/IO script around it isn't" split every
other tool in this project follows).

Offline analysis of a completed tools/panel_drift_measure.py run: reads the
CSVs it already wrote (output/panel_drift_raw.csv, output/
panel_drift_frame_drops.csv) - no camera, no LED panels, no Acroname hub
needed - and produces every plot plus a console summary answering "what is
the drift between the two LED panels":

- output/panel_drift_plot.png - the same pair-index-based chart
  domain.plot_export.export_session_plot already renders for a live
  session, reused unchanged.
- output/panel_drift_over_time.png - elapsed-seconds x-axis (from the
  camera's own HW frame timestamps), raw samples plotted faint, a
  binned/smoothed median plotted bold, transitions marked from the
  smoothed series (real hardware runs can oscillate for several seconds
  around a transition before settling - a raw per-sample "did it change"
  check would count every one of those as its own step; see domain/
  panel_drift_analysis.py's module docstring), and the overall linear-fit
  drift rate.
- output/panel_drift_local_rate.png - the practical "derivative": local
  drift rate (ms/s) between each pair of smoothed transitions, so you can
  see whether the rate itself is trending rather than assuming one fixed
  number for the whole run.
- A console summary: elapsed-time range covered, every clean transition's
  timestamp, the local rate at each one, and the headline number - the
  overall linear-fit drift rate in ms/s, ms/min, and ms/hour.

Run from the repo root: python tools/panel_drift_stats.py
Edit RAW_CSV_PATH/FRAME_DROPS_CSV_PATH/BIN_SECONDS below if your files or
desired smoothing window differ from the defaults.
"""

import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from settings import load_settings, ensure_output_dir
from domain.plot_export import export_session_plot
from domain.panel_drift_analysis import export_drift_over_time_plot, export_local_rate_plot

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_PATH = os.path.join(REPO_ROOT, "settings.yaml")

# Edit these if you moved the CSVs tools/panel_drift_measure.py wrote, or
# want to point at a different output directory's files. None (the
# default) resolves both against settings.yaml's paths.output_dir.
RAW_CSV_PATH = None
FRAME_DROPS_CSV_PATH = None

# Width of each time bucket used to smooth out real-hardware oscillation
# before looking for genuine step changes - see domain/
# panel_drift_analysis.py's module docstring. 30s was empirically checked
# against a real ~1000s dual-panel run: 10s still left spurious
# transitions (including a "phantom" non-integer value where a bin split
# close to evenly between 2 adjacent levels), 20s left one spurious
# back-and-forth pair, 30s cleanly resolved to just the genuine level
# changes. Raise it further if the smoothed (blue) line in
# panel_drift_over_time.png still shows spurious transitions for your
# run; lower it if genuine, closely-spaced transitions are getting merged
# into one.
BIN_SECONDS = 30.0

# Row fields that are booleans in the original data but come back as the
# strings "True"/"False" (or "") from a plain CSV read.
_BOOL_FIELDS = (
    "stream_a_frame_drop", "stream_b_frame_drop",
    "pairing_gap_us_excluded", "position_gap_ms_excluded",
)
# Row fields that are floats (or blank/None) in the original data.
_FLOAT_FIELDS = (
    "stream_a_ts_us", "stream_b_ts_us", "pairing_gap_us", "position_gap_ms",
)


def _parse_bool(raw_value):
    return raw_value.strip().lower() == "true"


def _parse_float_or_none(raw_value):
    raw_value = raw_value.strip()
    return float(raw_value) if raw_value else None


def _parse_row(raw_row):
    row = dict(raw_row)
    if "pair_index" in row and row["pair_index"] != "":
        row["pair_index"] = int(row["pair_index"])
    for field in _BOOL_FIELDS:
        if field in row:
            row[field] = _parse_bool(row[field])
    for field in _FLOAT_FIELDS:
        if field in row:
            row[field] = _parse_float_or_none(row[field])
    return row


def load_rows(raw_csv_path, frame_drops_csv_path):
    """Reads both CSVs domain.csv_export.export_session_csvs wrote and
    merges them back into one list, sorted by pair_index, so the full
    original row timeline (kept + frame-drop-excluded) is reconstructed for
    the plotting functions - they already know how to skip/NaN-out
    excluded rows via each row's own *_excluded flag, same as a live run."""
    rows = []
    for path in (raw_csv_path, frame_drops_csv_path):
        if not os.path.exists(path):
            print("WARNING: {} does not exist - skipping.".format(path))
            continue
        with open(path, "r", newline="") as f:
            reader = csv.DictReader(f)
            rows.extend(_parse_row(raw_row) for raw_row in reader)

    rows.sort(key=lambda r: r.get("pair_index", 0))
    return rows


def main():
    settings = load_settings(SETTINGS_PATH)
    output_dir = ensure_output_dir(settings)

    raw_csv_path = RAW_CSV_PATH or os.path.join(output_dir, "panel_drift_raw.csv")
    frame_drops_csv_path = FRAME_DROPS_CSV_PATH or os.path.join(output_dir, "panel_drift_frame_drops.csv")

    print("Reading {} and {}...".format(raw_csv_path, frame_drops_csv_path))
    rows = load_rows(raw_csv_path, frame_drops_csv_path)
    print("Loaded {} row(s).".format(len(rows)))
    if not rows:
        print("Nothing to analyze - check the CSV paths above.")
        return

    plot_path = os.path.join(output_dir, "panel_drift_plot.png")
    export_session_plot(rows, plot_path)
    print("Saved {}".format(plot_path))

    over_time_path = os.path.join(output_dir, "panel_drift_over_time.png")
    summary = export_drift_over_time_plot(rows, over_time_path, bin_seconds=BIN_SECONDS)
    if summary is None:
        print("No timestamped, non-excluded position_gap_ms samples to analyze.")
        return
    print("Saved {}".format(over_time_path))

    local_rate_path = os.path.join(output_dir, "panel_drift_local_rate.png")
    if export_local_rate_plot(summary, local_rate_path):
        print("Saved {}".format(local_rate_path))
    else:
        print("Fewer than 2 clean transitions - skipped {} (nothing to show a local rate between).".format(
            local_rate_path
        ))

    start_s, end_s = summary["elapsed_s_range"]
    print()
    print("=== Summary ===")
    print("Covered {:.1f}s of non-excluded samples ({:.1f}s to {:.1f}s elapsed), {} sample(s), "
          "smoothed into {}s bins.".format(end_s - start_s, start_s, end_s, summary["n_samples"], BIN_SECONDS))

    if summary["transitions"]:
        print("{} clean transition(s) (smoothed - see BIN_SECONDS):".format(len(summary["transitions"])))
        for t, old, new in summary["transitions"]:
            print("  at {:.1f}s: {} ms -> {} ms".format(t, old, new))
    else:
        print("No clean transitions detected - either no measurable drift, or not enough elapsed time/bins.")

    if summary["local_rates"]:
        print("Local drift rate between consecutive transitions (the practical 'derivative'):")
        for t, rate in summary["local_rates"]:
            print("  at {:.1f}s: {:.4f} ms/s ({:.2f} ms/min)".format(t, rate, rate * 60))

    print()
    if summary["overall_slope_per_s"] is not None:
        slope = summary["overall_slope_per_s"]
        print("HEADLINE - estimated panel-to-panel drift rate (overall linear fit):")
        print("  {:.4f} ms/s  =  {:.2f} ms/min  =  {:.1f} ms/hour".format(slope, slope * 60, slope * 3600))
    else:
        print("Not enough data to fit an overall drift rate.")


if __name__ == "__main__":
    main()
