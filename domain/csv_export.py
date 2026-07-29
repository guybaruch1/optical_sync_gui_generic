"""Generalized CSV export for a recorded TestSession.

Ported from optical_sync_poc_/pipeline_sync_test_diff.py's
write_raw_csvs, generalized so it no longer hardcodes exactly which
metric columns exist - engine.test_session.TestSession decides the row
shape (one column set per active Metric), this module just splits rows
into kept vs. frame-drop-excluded files and writes them, same convention
as the original script: only a frame-drop exclusion gets its own file,
every other exclusion reason (miss/warmup/outlier) stays in the kept
file, just flagged via its own column.
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


def export_session_csvs(rows, kept_path, dropped_path, drop_reason="frame_drop"):
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
            is_frame_drop = any(
                key.endswith("_exclude_reason") and value == drop_reason
                for key, value in row.items()
            )
            if is_frame_drop:
                dropped_writer.writerow(row)
                n_dropped += 1
            else:
                kept_writer.writerow(row)
                n_kept += 1

    return n_kept, n_dropped
