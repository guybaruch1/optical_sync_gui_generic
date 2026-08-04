"""Standalone tool - NOT part of the shipped app, no automated tests for
this file itself (same "thin hardware/IO script, pure logic tested
elsewhere" split as every other tool in this family - the plotting/rate-
consistency logic it calls into, domain/panel_drift_analysis.py, IS
unit-tested).

Runs tools/panel_drift_measure.py many times back-to-back - as separate,
independent OS processes, matching exactly how it's actually run by hand,
since that's the execution mode the "alternating success/failure" bug was
observed under - to check whether the measured panel-to-panel drift RATE
is consistent run over run, not just within a single run. Meant to be
left running unattended overnight.

Does NOT modify tools/panel_drift_measure.py at all - it's invoked as an
opaque subprocess, unchanged, using whatever PICK/DURATION_S/DEVICE_SERIAL/
etc. are already configured there.

RESOLVED (the alternating-failure pattern): the dual-panel LED stepping
was observed to succeed only every other run (fail, succeed, fail,
succeed...) when run as separate fresh processes - root cause confirmed to
be engine/dual_panel_control.py's stop_scanning() sending LEDPanel.stop()
(--stop), which poisons the panel's internal state so the NEXT arm never
steps; a run that got interrupted before stop_scanning() ran (skipping the
--stop) always worked next time, which is exactly the "succeed" half of
the alternating pattern lining up with "the previous run never cleanly
finished." Fixed by switching stop_scanning()'s dual-panel path to
LEDPanel.reset() instead - see that function's own comment and CLAUDE.md's
dual-panel section for the full trail. Every run through this script
should now succeed, not alternate - if the pattern below still shows
failures, that's a NEW/different issue, not this one.

This script's own defenses against the old pattern are kept regardless -
still useful belt-and-suspenders, and still needed for genuinely different
failure modes (a hung/crashed subprocess, stale output files from an
earlier session): each run's own resulting CSV is inspected afterward (via
domain.panel_drift_analysis.summarize_drift) to classify PASS (at least
one real transition detected) vs FAIL (no transitions - the panels never
stepped), independently of whether the subprocess itself crashed or
completed silently. Failed runs are excluded from the rate-consistency
numbers/plots but still counted and reported, so the pass/fail pattern
itself is visible in the results too. An extra
dual_panel_control.stop_scanning() safety call still runs between
iterations regardless of the just-finished run's outcome, to maximize the
chance the panels are actually released before the next attempt
(belt-and-suspenders beyond whatever panel_drift_measure.py's own cleanup
already tried).

Run from the repo root: python tools/panel_drift_overnight.py
Writes output/overnight_runs/run_NNN/ (each run's own archived CSVs +
console log), output/overnight_rate_consistency.png (measured rate per
run, pass/fail marked), output/overnight_overlay.png (every successful
run's own drift curve overlaid on one chart), and prints a full summary.
"""

import csv
import os
import statistics
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from settings import load_settings, ensure_output_dir
import engine.dual_panel_control as dual_panel_control
from domain.panel_drift_analysis import summarize_drift, export_rate_consistency_plot, export_overlay_plot

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_PATH = os.path.join(REPO_ROOT, "settings.yaml")
PANEL_DRIFT_MEASURE_SCRIPT = os.path.join(REPO_ROOT, "tools", "panel_drift_measure.py")

# How many times to run tools/panel_drift_measure.py back-to-back. Each
# successful run takes as long as that script's own DURATION_S (edit it
# there, not here) - size this to fit your overnight window, and expect
# roughly half as many usable/successful runs given the known alternating
# issue, so budget accordingly (e.g. 100 runs x 3min DURATION_S is up to
# ~5 hours if every single one succeeded).
N_RUNS = 100

# Hard wall-clock cutoff so an overnight batch can't run into the next
# morning even if runs take longer than expected - stops STARTING new runs
# once this much total time has elapsed; the run already in progress still
# finishes.
MAX_TOTAL_RUNTIME_S = 8 * 3600.0

# Brief pause between runs - gives the hardware (Acroname hub/relay/USB)
# a moment to settle before the next attempt, same spirit as settings.yaml
# dual_panel.hub_switch_settle_s.
DELAY_BETWEEN_RUNS_S = 5.0

# Per-run subprocess timeout - generous headroom above panel_drift_measure.py's
# own DURATION_S so a genuinely-working run isn't killed early, while still
# bounding a truly hung one.
PER_RUN_TIMEOUT_S = 600.0

# Same tuned value tools/panel_drift_stats.py already uses (see its own
# comment) - real hardware showed 10s bins still left spurious transitions
# from measurement noise, which risks misclassifying a run here (a run
# could look "stepped" from noise alone at a finer bin size). Passed
# explicitly to summarize_drift() below rather than relying on its own
# generic default.
BIN_SECONDS = 30.0

# --- Minimal, standalone CSV loading - duplicated from tools/
# panel_drift_stats.py on purpose, matching this project's existing
# convention of keeping each hardware-adjacent tool script independently
# runnable rather than sharing small helpers across them (see PICK/
# DEVICE_SERIAL in tools/panel_drift_calibrate.py/panel_drift_measure.py). ---

_BOOL_FIELDS = (
    "stream_a_frame_drop", "stream_b_frame_drop",
    "pairing_gap_us_excluded", "position_gap_ms_excluded",
)
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
    rows = []
    for path in (raw_csv_path, frame_drops_csv_path):
        if not os.path.exists(path):
            continue
        with open(path, "r", newline="") as f:
            reader = csv.DictReader(f)
            rows.extend(_parse_row(raw_row) for raw_row in reader)
    rows.sort(key=lambda r: r.get("pair_index", 0))
    return rows


def _archive_run_output(output_dir, run_dir):
    """Moves (not copies) tools/panel_drift_measure.py's fixed-name output
    files into this run's own subdirectory immediately after it finishes -
    that script always writes to the SAME output/panel_drift_raw.csv etc.,
    so without this the next run would silently overwrite this one's
    results before they're read. Returns {filename: archived_path} for
    whichever of the two CSVs actually existed."""
    os.makedirs(run_dir, exist_ok=True)
    archived = {}
    for name in ("panel_drift_raw.csv", "panel_drift_frame_drops.csv"):
        src = os.path.join(output_dir, name)
        if os.path.exists(src):
            dst = os.path.join(run_dir, name)
            os.replace(src, dst)
            archived[name] = dst
    return archived


def _clear_stale_output(output_dir):
    """Deletes any pre-existing panel_drift_raw.csv/panel_drift_frame_drops.csv
    in output_dir BEFORE launching the next run. Without this, a run that
    crashes before ever writing fresh CSVs would leave _archive_run_output
    silently picking up STALE files left over from an earlier, unrelated
    session - which is exactly what happened the first time this script
    ran: every run crashed immediately (output/panel_drift_calibration.yaml
    was missing), but leftover CSVs from hours-old manual testing were
    still sitting in output_dir and got archived/reported as if they were
    that run's own fresh, successful result."""
    for name in ("panel_drift_raw.csv", "panel_drift_frame_drops.csv"):
        path = os.path.join(output_dir, name)
        if os.path.exists(path):
            os.remove(path)


def run_once(run_index, output_dir, archive_dir):
    print("--- Run {} starting at {} ---".format(run_index, time.strftime("%Y-%m-%d %H:%M:%S")))
    run_dir = os.path.join(archive_dir, "run_{:03d}".format(run_index))
    os.makedirs(run_dir, exist_ok=True)
    log_path = os.path.join(run_dir, "console.log")

    _clear_stale_output(output_dir)

    exit_code = None
    try:
        with open(log_path, "w") as log_file:
            result = subprocess.run(
                [sys.executable, PANEL_DRIFT_MEASURE_SCRIPT],
                cwd=REPO_ROOT, stdout=log_file, stderr=subprocess.STDOUT,
                timeout=PER_RUN_TIMEOUT_S,
            )
            exit_code = result.returncode
    except subprocess.TimeoutExpired:
        print("Run {} timed out after {}s.".format(run_index, PER_RUN_TIMEOUT_S))
        exit_code = "timeout"
    except Exception as exc:
        print("Run {} failed to even start: {}".format(run_index, exc))
        exit_code = "launch_error"

    archived = _archive_run_output(output_dir, run_dir)
    rows = []
    if "panel_drift_raw.csv" in archived:
        rows = load_rows(
            archived["panel_drift_raw.csv"],
            archived.get("panel_drift_frame_drops.csv", os.path.join(run_dir, "panel_drift_frame_drops.csv")),
        )

    summary = summarize_drift(rows, bin_seconds=BIN_SECONDS) if rows else None
    stepped = bool(summary and summary["transitions"])
    # Distinguishes WHY a run failed - a crash (exit_code != 0, e.g. the
    # calibration file was missing, before any hardware was even touched)
    # is a completely different problem from "it ran to completion but the
    # panels never actually stepped" (rows exist, no transitions) - both
    # show up as stepped=False, but need different fixes, so keep both
    # signals in the result rather than collapsing them into one flag.
    crashed = exit_code not in (0, None)

    print("Run {}: exit_code={}, {} row(s), stepped={}{}".format(
        run_index, exit_code, len(rows), stepped,
        " (CRASHED - see {})".format(log_path) if crashed else "",
    ))

    return {"run_index": run_index, "exit_code": exit_code, "n_rows": len(rows),
            "stepped": stepped, "crashed": crashed, "summary": summary}


def main():
    settings = load_settings(SETTINGS_PATH)
    output_dir = ensure_output_dir(settings)
    dual_panel_config = settings["dual_panel"]
    archive_dir = os.path.join(output_dir, "overnight_runs")
    os.makedirs(archive_dir, exist_ok=True)

    # Fails fast, before burning the whole batch on the same crash
    # repeated N_RUNS times overnight - tools/panel_drift_measure.py can't
    # do anything at all without this (it crashes immediately, before
    # touching the camera/relay/panels), so there's no point starting.
    calibration_path = os.path.join(output_dir, "panel_drift_calibration.yaml")
    if not os.path.exists(calibration_path):
        print(
            "ERROR: {} does not exist - tools/panel_drift_measure.py cannot run at all without "
            "it. Run tools/panel_drift_calibrate.py first, then re-run this script.".format(calibration_path)
        )
        return

    print("Starting overnight batch: up to {} run(s), max {:.1f}h total.".format(
        N_RUNS, MAX_TOTAL_RUNTIME_S / 3600.0
    ))

    results = []
    batch_start = time.time()
    for i in range(1, N_RUNS + 1):
        if time.time() - batch_start > MAX_TOTAL_RUNTIME_S:
            print("Hit MAX_TOTAL_RUNTIME_S ({:.1f}h) - stopping before run {}.".format(
                MAX_TOTAL_RUNTIME_S / 3600.0, i
            ))
            break

        results.append(run_once(i, output_dir, archive_dir))

        # Extra safety net beyond whatever panel_drift_measure.py's own
        # cleanup already tried - if THAT failed too (e.g. the same crash
        # behind the alternating success/failure pattern), this gives the
        # next run a better chance of starting from a clean, released state.
        try:
            dual_panel_control.stop_scanning(dual_panel_config)
        except Exception as exc:
            print("WARNING: extra safety stop_scanning() failed: {}".format(exc))

        time.sleep(DELAY_BETWEEN_RUNS_S)

    n_stepped = sum(1 for r in results if r["stepped"])
    n_crashed = sum(1 for r in results if r["crashed"])
    n_total = len(results)
    print()
    print("=== Overnight batch summary ===")
    print("{}/{} run(s) stepped (LEDs actually moved). {} crashed before/without producing usable data.".format(
        n_stepped, n_total, n_crashed
    ))
    # P = stepped, C = crashed (see that run's own console.log - e.g. a
    # missing output/panel_drift_calibration.yaml crashes before any
    # hardware is even touched, a completely different problem from the
    # panels just not stepping), N = completed but never stepped.
    pattern = "".join("P" if r["stepped"] else ("C" if r["crashed"] else "N") for r in results)
    print("Pass/fail pattern: {}  (P=stepped, C=crashed, N=completed but never stepped)".format(pattern))

    rates = []
    for r in results:
        rate = r["summary"]["overall_slope_per_s"] if (r["stepped"] and r["summary"]) else None
        rates.append((r["run_index"], rate))
        if rate is not None:
            status = "{:.4f} ms/s".format(rate)
        elif r["crashed"]:
            status = "CRASHED - see output/overnight_runs/run_{:03d}/console.log".format(r["run_index"])
        else:
            status = "completed but panels never stepped"
        print("  run {}: {}".format(r["run_index"], status))

    valid_rates = [r for _, r in rates if r is not None]
    if valid_rates:
        mean_rate = statistics.mean(valid_rates)
        stdev_rate = statistics.stdev(valid_rates) if len(valid_rates) > 1 else 0.0
        print()
        print("Across {} successful run(s): mean={:.4f} ms/s, stdev={:.4f} ms/s".format(
            len(valid_rates), mean_rate, stdev_rate
        ))
    else:
        print("No successful runs to compute rate consistency from.")

    rate_plot_path = os.path.join(output_dir, "overnight_rate_consistency.png")
    if export_rate_consistency_plot(rates, rate_plot_path):
        print("Saved {}".format(rate_plot_path))

    overlay_series = [
        (r["run_index"], r["summary"]["elapsed_s"], r["summary"]["values"])
        for r in results if r["stepped"] and r["summary"]
    ]
    overlay_path = os.path.join(output_dir, "overnight_overlay.png")
    if export_overlay_plot(overlay_series, overlay_path):
        print("Saved {}".format(overlay_path))
    else:
        print("No successful runs to overlay.")


if __name__ == "__main__":
    main()
