"""Fully self-contained, standalone tool - NOT part of the shipped app, no
automated tests for this file itself (same "pure logic vs. thin script"
split every other tool in this project follows - see e.g. tools/
panel_drift/panel_drift_stats.py's own docstring) - the pure functions
below (combine_rows/_is_clean_row/compute_summary/format_summary) are
plain, easily-testable logic even though no test file exists for them yet.

Deliberately has ZERO imports from this repo (no `from settings import
...`, no `from domain... import ...`) - RunningStats below is an inlined
COPY of domain.running_stats.RunningStats, not an import of it. This is on
purpose: this file is meant to be copied out on its own, straight into a
completed run's own output folder (alongside its CSVs, with nothing else
from this project needed there), and run from that folder directly -
`from settings import load_settings` would break the moment this file
isn't sitting inside the actual repo checkout anymore, which defeats the
entire point of a portable single-file report generator.

Offline, one-shot report over an ALREADY-COMPLETED Live Session run's own
two CSVs (pipeline_sync_raw.csv / pipeline_sync_frame_drops.csv, whatever
domain.csv_export.export_session_csvs already wrote) - no camera, no GUI,
no re-run needed. Works against output from a run recorded BEFORE
domain.csv_export.export_clean_and_other_csvs existed (this script doesn't
call or depend on it at all - it recomputes the same "is this row clean"
logic independently, see _is_clean_row below), so it's usable on any
already-completed run's output folder, old or new.

What it does:
  1. Reads both CSVs and concatenates them back into ONE combined dataset -
     every pair_index appears in exactly one of the two files (never both,
     never neither - see docs/output_validation_report.md's real-data
     audit), so this recovers the complete original per-pair row set,
     sorted back into pair_index order.
  2. Writes that combined dataset - every column both files carry (HW TS
     timestamps, pairing_gap_us/position_gap_ms and each metric's own
     *_excluded/*_exclude_reason columns, frame-drop flags), plus one
     extra computed "is_clean" column - to a single new CSV.
  3. Computes final statistics via the RunningStats class below (the exact
     same algorithm domain.running_stats.RunningStats uses - Welford's
     online mean/variance - just inlined, see the class docstring), plus
     median (RunningStats itself can't produce one online without storing
     every value - not worth doing for the live app's per-frame updates,
     but this script already holds every row in memory anyway, so it just
     collects the same abs()'d values into a list and calls statistics.
     median() once at the end). All of it fed abs() values for BOTH
     metrics (see compute_summary) - every stat describes the magnitude of
     the gap, not a signed value, so this is deliberately NOT identical to
     the live app's own (signed) Stats panel - and prints/saves a
     plain-text summary alongside.

Run (from anywhere - this file has no dependency on where it physically
lives, repo or not):
    python full_session_report.py
    python full_session_report.py "path/to/some/other/output/folder"

With no argument, defaults to the directory THIS SCRIPT ITSELF is sitting
in - so the normal way to use this is: copy full_session_report.py
straight into a completed run's output folder (next to its
pipeline_sync_raw.csv/pipeline_sync_frame_drops.csv) and just run it there
with no arguments. Pass a directory as the one positional argument instead
to point at some OTHER folder without moving the script there (e.g. run it
once from tools/ against several different completed runs' folders).

Writes, into the resolved folder:
    pipeline_sync_full.csv         - every pair, one row each, every column
    pipeline_sync_full_summary.txt - the final-statistics summary (also
                                      printed to the console)
"""

import csv
import os
import statistics
import sys

# Override these if a given run's CSVs used different filenames than the
# app's own current defaults (e.g. an older run, or a hand-renamed copy).
RAW_CSV_FILENAME = "pipeline_sync_raw.csv"
DROPPED_CSV_FILENAME = "pipeline_sync_frame_drops.csv"


class RunningStats:
    """Inlined copy of domain.running_stats.RunningStats (Welford's online
    mean/variance) - duplicated here, not imported, so this file has zero
    dependency on the rest of the repo and can be copied out on its own -
    see the module docstring for why that matters. Keep this in sync with
    domain/running_stats.py if that ever changes."""

    def __init__(self):
        self.count = 0
        self.mean = 0.0
        self.min = None
        self.max = None
        self._m2 = 0.0

    def update(self, value):
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        delta2 = value - self.mean
        self._m2 += delta * delta2
        self.min = value if self.min is None else min(self.min, value)
        self.max = value if self.max is None else max(self.max, value)

    @property
    def std(self):
        if self.count < 2:
            return 0.0
        return (self._m2 / self.count) ** 0.5


def _resolve_output_dir(argv):
    if len(argv) > 1:
        return argv[1]
    # Default: the directory this script file itself is sitting in - the
    # normal usage is copying this one file straight into a completed
    # run's output folder and running it from there with no arguments.
    return os.path.dirname(os.path.abspath(__file__))


def _load_rows(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _is_clean_row(row):
    """A row is clean if NO metric's own "<name>_excluded" column reads
    the string "True" - not frame_drop, not warmup, not miss, not
    no_led_data, not syncer_outlier. Deliberately reimplemented here (not
    imported from domain.csv_export._is_clean_row) - this script must keep
    working against a run recorded before that function ever existed, and
    against CSV values that always arrive as strings (csv.DictReader), not
    the live booleans that function was written against."""
    return not any(value == "True" for key, value in row.items() if key.endswith("_excluded"))


def _parse_float_or_none(value):
    if value is None or value == "" or value.lower() == "none":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def combine_rows(raw_rows, dropped_rows):
    """Reconstructs the full, single per-pair dataset export_session_csvs
    originally split apart - every pair_index appears in exactly one of
    the two input files, so concatenating and sorting by pair_index
    recovers the complete original row set, in original order."""
    all_rows = raw_rows + dropped_rows
    all_rows.sort(key=lambda r: int(r["pair_index"]))
    return all_rows


def _median_or_none(values):
    return statistics.median(values) if values else None


def compute_summary(rows):
    """Follows gui/pages/live_session_page.py's own _on_row_ready/
    _push_running_stats logic for WHICH rows count - skip a metric's
    RunningStats update whenever THAT metric's own *_excluded flag is set -
    via the same RunningStats algorithm the live app uses, but fed abs()
    values for both metrics (a signed +5 and -5 are the same magnitude of
    "how far off"), so min/avg/std/max describe magnitude, not sign -
    deliberately different from the live app's own (signed) Stats panel.
    Also collects the same abs()'d values into a plain list per metric so
    median can be computed once at the end (see _median_or_none) - not
    something RunningStats' online algorithm can produce by itself."""
    pairing_stats = RunningStats()
    position_stats = RunningStats()
    pairing_values = []
    position_values = []
    stream_a_drops = 0
    stream_b_drops = 0
    n_clean = 0
    pairing_reasons = {}
    position_reasons = {}

    for row in rows:
        if row.get("stream_a_frame_drop") == "True":
            stream_a_drops += 1
        if row.get("stream_b_frame_drop") == "True":
            stream_b_drops += 1
        if _is_clean_row(row):
            n_clean += 1

        pairing_reason = row.get("pairing_gap_us_exclude_reason") or "none"
        pairing_reasons[pairing_reason] = pairing_reasons.get(pairing_reason, 0) + 1
        position_reason = row.get("position_gap_ms_exclude_reason") or "none"
        position_reasons[position_reason] = position_reasons.get(position_reason, 0) + 1

        # abs() BEFORE feeding RunningStats/the median list, not after - a
        # signed +5 and -5 are the same magnitude of "how far off" for both
        # metrics, and every stat should describe that magnitude, not get
        # dragged around by sign. This means "min" here is the smallest
        # deviation seen, not the most-negative value - deliberately
        # different from the live app's own (signed) Stats panel, see the
        # module docstring.
        pairing_value = _parse_float_or_none(row.get("pairing_gap_us"))
        if pairing_value is not None and row.get("pairing_gap_us_excluded") != "True":
            abs_pairing_value = abs(pairing_value)
            pairing_stats.update(abs_pairing_value)
            pairing_values.append(abs_pairing_value)

        position_value = _parse_float_or_none(row.get("position_gap_ms"))
        if position_value is not None and row.get("position_gap_ms_excluded") != "True":
            abs_position_value = abs(position_value)
            position_stats.update(abs_position_value)
            position_values.append(abs_position_value)

    return {
        "total_pairs": len(rows),
        "stream_a_frame_drops": stream_a_drops,
        "stream_b_frame_drops": stream_b_drops,
        "n_clean": n_clean,
        "n_other": len(rows) - n_clean,
        "pairing_gap_us_exclude_reasons": pairing_reasons,
        "position_gap_ms_exclude_reasons": position_reasons,
        "pairing_gap_us_stats": pairing_stats,
        "position_gap_ms_stats": position_stats,
        "pairing_gap_us_median": _median_or_none(pairing_values),
        "position_gap_ms_median": _median_or_none(position_values),
    }


def _format_stats_block(name, stats, median):
    if stats.count == 0:
        return "  {}: no valid (non-excluded) samples".format(name)
    # min/avg/median/std/max are all computed on abs(value) - see
    # compute_summary - so "min" is the smallest deviation magnitude seen,
    # never negative.
    return "  {} [abs]: count={} min={:.2f} avg={:.2f} median={:.2f} std={:.2f} max={:.2f}".format(
        name, stats.count, stats.min, stats.mean, median, stats.std, stats.max
    )


def _pct(count, total):
    return 100.0 * count / total if total else 0.0


def format_summary(summary):
    total = summary["total_pairs"]
    lines = [
        "=== Live Session full report ===",
        "Total pairs: {}".format(total),
        "Frame drops: stream_a={} ({:.1f}%), stream_b={} ({:.1f}%)".format(
            summary["stream_a_frame_drops"], _pct(summary["stream_a_frame_drops"], total),
            summary["stream_b_frame_drops"], _pct(summary["stream_b_frame_drops"], total),
        ),
        "Genuinely clean pairs (no exclusion on either metric): {} ({:.1f}%)".format(
            summary["n_clean"], _pct(summary["n_clean"], total)
        ),
        "Everything else (frame_drop/warmup/miss/no_led_data/syncer_outlier): {}".format(summary["n_other"]),
        "",
        "HW TS Latency (pairing_gap_us) exclude reasons: {}".format(summary["pairing_gap_us_exclude_reasons"]),
        "Optical Sync (position_gap_ms) exclude reasons: {}".format(summary["position_gap_ms_exclude_reasons"]),
        "",
        "Final statistics (non-excluded rows only, values converted to abs() before "
        "min/avg/median/std/max - see compute_summary):",
        _format_stats_block(
            "HW TS Latency (pairing_gap_us, us)", summary["pairing_gap_us_stats"], summary["pairing_gap_us_median"]
        ),
        _format_stats_block(
            "Optical Sync (position_gap_ms, ms)", summary["position_gap_ms_stats"], summary["position_gap_ms_median"]
        ),
    ]
    return "\n".join(lines)


def write_full_csv(rows, path):
    fieldnames = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    fieldnames.append("is_clean")
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            out_row = dict(row)
            out_row["is_clean"] = _is_clean_row(row)
            writer.writerow(out_row)


def main():
    output_dir = _resolve_output_dir(sys.argv)
    raw_path = os.path.join(output_dir, RAW_CSV_FILENAME)
    dropped_path = os.path.join(output_dir, DROPPED_CSV_FILENAME)

    if not os.path.exists(raw_path):
        sys.exit(
            "Could not find {} - either copy this script into the run's own "
            "output folder and run it from there with no arguments, or pass "
            "that folder as an argument, e.g.\n"
            '  python full_session_report.py "path\\to\\output\\folder"'.format(raw_path)
        )
    if not os.path.exists(dropped_path):
        sys.exit("Could not find {} - pass the correct output folder as an argument.".format(dropped_path))

    raw_rows = _load_rows(raw_path)
    dropped_rows = _load_rows(dropped_path)
    rows = combine_rows(raw_rows, dropped_rows)

    full_csv_path = os.path.join(output_dir, "pipeline_sync_full.csv")
    write_full_csv(rows, full_csv_path)

    summary = compute_summary(rows)
    summary_text = format_summary(summary)
    summary_path = os.path.join(output_dir, "pipeline_sync_full_summary.txt")
    with open(summary_path, "w") as f:
        f.write(summary_text + "\n")

    print(summary_text)
    print()
    print("Wrote {} rows to {}".format(len(rows), full_csv_path))
    print("Wrote summary to {}".format(summary_path))


if __name__ == "__main__":
    main()
