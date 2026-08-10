"""Generalized CSV export for a recorded TestSession.

Ported from optical_sync_poc_/pipeline_sync_test_diff.py's
write_raw_csvs, generalized so it no longer hardcodes exactly which
metric columns exist - engine.test_session.TestSession decides the row
shape (one column set per active Metric), this module just splits rows
into kept vs. frame-drop-excluded files and writes them, same convention
as the original script: only a frame-drop exclusion gets its own file,
every other exclusion reason (miss/warmup/outlier) stays in the kept
file, just flagged via its own column.

export_clean_and_other_csvs is a second, additive split of the same rows
(written to two more files alongside, not instead of, export_session_csvs'
kept/dropped pair) - "kept" above is not "clean": a warmup/miss/outlier
row stays in the kept file today, just flagged. clean/other instead routes
on whether ANY metric's own "<name>_excluded" column is True at all, so
the clean file is genuinely nothing-wrong-on-any-metric, and the other
file catches everything else (frame drops included) in one place.
"""

import csv


def export_series_csv(path, series_x, series_y_by_name):
    """Writes one chart's own plotted series - not a TestSession row export.
    series_y_by_name's series all share series_x - true here because every
    series on one LivePlot is appended together in the same
    LiveSessionPage._on_stats_ready tick, so they're always the same length
    and in the same order."""
    fieldnames = ["pair_index"] + list(series_y_by_name.keys())
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for i, x in enumerate(series_x):
            row = {"pair_index": x}
            for name, y_values in series_y_by_name.items():
                row[name] = y_values[i]
            writer.writerow(row)


def _is_clean_row(row):
    """A row is clean if NO metric's own "<name>_excluded" column is True -
    not frame_drop, not warmup, not miss, not no_led_data, not
    syncer_outlier, and not whatever exclusion any future metric adds.
    Generic over whatever metrics are configured (TestSession.process_pair
    writes one "<name>_excluded" key per registered Metric, see
    engine/test_session.py), not hardcoded to pairing_gap_us/
    position_gap_ms specifically - deliberately different from
    export_session_csvs' kept/dropped split above, which only ever routes
    on the frame-drop boolean and lets warmup/miss/outlier rows stay
    "kept"."""
    return not any(value is True for key, value in row.items() if key.endswith("_excluded"))


def export_clean_and_other_csvs(rows, clean_path, other_path):
    """Splits every row into "nothing at all excluded" (clean_path) vs.
    "excluded for some reason, on some metric" (other_path) - see
    _is_clean_row. This is additive: a separate split from the kept/dropped
    files export_session_csvs already writes (same rows, written again
    under a different partition), not a replacement for them."""
    fieldnames = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    if not fieldnames:
        fieldnames = ["pair_index"]

    n_clean = 0
    n_other = 0
    with open(clean_path, "w", newline="") as clean_f, open(other_path, "w", newline="") as other_f:
        clean_writer = csv.DictWriter(clean_f, fieldnames=fieldnames)
        other_writer = csv.DictWriter(other_f, fieldnames=fieldnames)
        clean_writer.writeheader()
        other_writer.writeheader()

        for row in rows:
            if _is_clean_row(row):
                clean_writer.writerow(row)
                n_clean += 1
            else:
                other_writer.writerow(row)
                n_other += 1

    return n_clean, n_other


def export_session_csvs(rows, kept_path, dropped_path):
    fieldnames = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    if not fieldnames:
        fieldnames = ["pair_index"]

    n_kept = 0
    n_dropped = 0
    with open(kept_path, "w", newline="") as kept_f, open(dropped_path, "w", newline="") as dropped_f:
        kept_writer = csv.DictWriter(kept_f, fieldnames=fieldnames)
        dropped_writer = csv.DictWriter(dropped_f, fieldnames=fieldnames)
        kept_writer.writeheader()
        dropped_writer.writeheader()

        for row in rows:
            is_frame_drop = bool(row.get("stream_a_frame_drop") or row.get("stream_b_frame_drop"))
            if is_frame_drop:
                dropped_writer.writerow(row)
                n_dropped += 1
            else:
                kept_writer.writerow(row)
                n_kept += 1

    return n_kept, n_dropped
