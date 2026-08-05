"""Standalone tool - NOT part of the shipped app, no automated tests (same
"hardware-only, no tests by design" bucket as engine/session_engine.py,
engine/led_panel.py, tools/diag_*.py).

Measures drift between the two physical LED panels used in dual-panel
mode, using a SINGLE camera stream (see tools/panel_drift_calibrate.py's
module docstring for why - it eliminates the sensor-type confound of
comparing two different physical imagers).

Reuses engine.acquisition_loop.AcquisitionLoop + engine.test_session.
TestSession + engine.metrics.PositionGapMetric/PairingGapMetric completely
unchanged, by repurposing their "stream A vs stream B" shape to mean
"panel A vs panel B" instead - both metrics only ever look at the two
brightness arrays/timestamps a FramePairSample carries, never anything
that assumes they came from two different camera streams. Every captured
frame pair here is genuinely ONE camera frame (same image, same
timestamp), with two DIFFERENT brightness arrays sampled from it (panel
A's calibrated LED positions, panel B's) - PairingGapMetric will read ~0
gap throughout, which is itself a useful sanity check that this
single-frame assumption holds; PositionGapMetric's position_gap_ms *is*
the panel-to-panel drift measurement.

Deliberately does NOT use engine.streams.ContinuousCapture (it calls
config.enable_stream() once per pick in a for loop - feeding it the same
pick twice, since there's only one real stream here, is untested and
risks a duplicate-enable_stream error on real hardware) - instead opens a
small custom single-stream rs.pipeline() loop directly.

Run from the repo root: python tools/panel_drift_measure.py
Requires output/panel_drift_calibration.yaml (see
tools/panel_drift_calibrate.py). Runs for this script's own DURATION_S
constant (deliberately NOT settings.yaml's test.duration_s - this test is
run far longer than a normal live session, and the two must not be
coupled through one shared config value), or press Ctrl+C to stop early.
Writes output/panel_drift_raw.csv, output/panel_drift_frame_drops.csv,
output/panel_drift_plot.png (position_gap_ms/pairing_gap_us over pair
index, via the same domain.plot_export.export_session_plot every live
session already uses), and output/panel_drift_over_time.png - a SECOND,
drift-specific plot against actual elapsed seconds (from the camera's own
HW frame timestamps, not an assumed constant fps), with each step change
marked and a linear best-fit drift-rate line. A console summary lists
every step-change's elapsed time and an overall ms/s drift-rate estimate.

Pops up a live OpenCV preview window (reusing domain.realsense_utils.
draw_led_state_overlay unchanged - the same green/on-red/off circle
overlay Live Session's debug snapshots use) showing every captured frame
with both panels' calibrated LED positions circled - press 'q' in that
window (or Ctrl+C in the console) to stop early.
"""

import os
import sys
import time
import signal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import yaml
import numpy as np
import pyrealsense2 as rs

from settings import load_settings, ensure_output_dir
from state.gui_state import load_gui_state
import engine.dual_panel_control as dual_panel_control
from engine.metrics import PairingGapMetric, PositionGapMetric
from engine.test_session import TestSession, TestSessionConfig
from engine.acquisition_loop import AcquisitionLoop, AcquisitionCallbacks
from domain.calibration import compute_threshold
from domain.realsense_utils import decode_frame, sample_all_neighborhood_brightness, draw_led_state_overlay
from domain.csv_export import export_session_csvs
from domain.plot_export import export_session_plot

LIVE_VIEW_WINDOW = "Panel Drift - Live Capture (green=on, red=off, q=quit)"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_PATH = os.path.join(REPO_ROOT, "settings.yaml")

# Must match tools/panel_drift_calibrate.py's PICK exactly - see that
# script's module docstring for why these aren't shared/imported.
PICK = {
    "stream_type": rs.stream.infrared,
    "stream_index": 1,
    "format": rs.format.y8,
    "width": 1280,
    "height": 720,
    "fps": 30,
}

# Falls back to this if gui_state.json has no device_serial saved (i.e. the
# wizard's Device Select page has never been run on this machine). Leave as
# None to require gui_state.json; set to your camera's serial (a string,
# e.g. "123456789012") to bypass it entirely.
DEVICE_SERIAL = None

# How long to run the actual drift measurement, in seconds - deliberately
# a constant here, not settings.yaml's test.duration_s (that key is shared
# with the real wizard's Live Session, which should stay independently
# tunable from this occasional, much-longer-running niche test). None
# means "no auto-stop - run until Ctrl+C".
DURATION_S = 180.0

# How many frame-pairs between console prints/live-view window updates.
# Every pair's data is ALWAYS captured into the CSV/plots regardless of
# this value - it only throttles how often something gets printed/redrawn.
# 1 (print/redraw every single pair) is fine for a short run, but at 30fps
# a 10-15 minute run is 18,000-27,000 pairs - that many print() calls can
# make a slow console (PyCharm's GUI console especially) become the
# bottleneck, which risks *actually* slowing the capture loop down enough
# to induce real frame drops (see domain/realsense_utils.py's
# sample_all_neighborhood_brightness docstring for the same class of
# self-induced-slowness symptom). 30 (~once/second at 30fps) is a
# reasonable default for longer runs; drop to 1 for a short, closely-watched
# run.
DISPLAY_STRIDE = 30


def resolve_device_serial():
    if DEVICE_SERIAL:
        return DEVICE_SERIAL
    gui_state = load_gui_state(os.path.join(REPO_ROOT, "gui_state.json"))
    if gui_state.device_serial:
        return gui_state.device_serial
    raise RuntimeError(
        "No device_serial found in gui_state.json (run the wizard's Device Select page at least "
        "once) - or edit this script's DEVICE_SERIAL constant directly."
    )


def load_panel_calibration(output_dir):
    path = os.path.join(output_dir, "panel_drift_calibration.yaml")
    with open(path, "r") as f:
        calib = yaml.safe_load(f)

    stored_pick = calib["pick"]
    if (stored_pick["stream_type"] != PICK["stream_type"].name
            or stored_pick["stream_index"] != PICK["stream_index"]
            or stored_pick["format"] != PICK["format"].name
            or stored_pick["width"] != PICK["width"]
            or stored_pick["height"] != PICK["height"]
            or stored_pick["fps"] != PICK["fps"]):
        print(
            "WARNING: {} was calibrated against a different PICK ({!r}) than this script's own "
            "PICK ({!r}) - re-run tools/panel_drift_calibrate.py, or edit this script's PICK to "
            "match.".format(path, stored_pick, {
                "stream_type": PICK["stream_type"].name, "stream_index": PICK["stream_index"],
                "format": PICK["format"].name, "width": PICK["width"], "height": PICK["height"],
                "fps": PICK["fps"],
            })
        )

    return calib["panel_a"]["positions"], calib["panel_b"]["positions"]


def positions_to_arrays(positions):
    """Sorts by integer led_id to preserve assign_grid_ids' row-major grid
    order - PositionGapMetric's find_last_on_led/compute_position_gap
    assume array index == grid position along the scan."""
    ids = sorted(positions.keys(), key=int)
    xy = [(positions[i][0], positions[i][1]) for i in ids]
    on_values = np.array([positions[i][2] for i in ids])
    off_values = np.array([positions[i][3] for i in ids])
    return xy, on_values, off_values


def export_drift_over_time_plot(rows, path):
    """A second, drift-specific plot on top of domain.plot_export.
    export_session_plot's existing pair-index one - that one shares an axis
    with pairing_gap_us (always ~0 here, not useful) and plots against
    pair_index rather than real time. This one uses the camera's own HW
    frame timestamps (stream_a_ts_us - accurate regardless of any assumed
    constant fps) for an actual elapsed-seconds x-axis, marks every point
    where the measured gap changes value (each one is exactly one more
    switch_time_ms step of accumulated clock skew between the 2 panels),
    and fits a straight line through the non-excluded samples to estimate
    an overall drift rate. Returns a summary dict (elapsed_s range, the
    list of step changes, and the fitted ms/s rate) for the caller to print
    - None if there's nothing to plot."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    timestamped = [r for r in rows if r.get("stream_a_ts_us") is not None]
    if not timestamped:
        return None

    # Accumulates consecutive per-row deltas rather than a single
    # `raw_ts_us - first_row_ts_us` subtraction - a real ~5000s dual-panel
    # run showed the camera's own HW frame timestamp can have a one-off
    # discontinuity (a downward jump/reset) partway through a long run,
    # which under plain subtraction corrupted every row after it into a
    # nonsensical NEGATIVE elapsed time. A backward step between two
    # consecutive rows is clamped to a 0-duration "glitch" instead - see
    # tools/panel_drift_analysis.py's parse_gap_series for the same fix,
    # applied there for tools/panel_drift_stats.py's offline analysis.
    elapsed_s = []
    elapsed_us = 0.0
    prev_raw = None
    n_discontinuities = 0
    for r in timestamped:
        raw = r["stream_a_ts_us"]
        if prev_raw is not None:
            delta = raw - prev_raw
            if delta < 0:
                delta = 0
                n_discontinuities += 1
            elapsed_us += delta
        prev_raw = raw
        elapsed_s.append(elapsed_us / 1_000_000.0)

    gap_ms = [
        r["position_gap_ms"] if (r.get("position_gap_ms") is not None and not r.get("position_gap_ms_excluded"))
        else float("nan")
        for r in timestamped
    ]

    valid = [(t, v) for t, v in zip(elapsed_s, gap_ms) if not np.isnan(v)]
    changes = []
    prev_v = None
    for t, v in valid:
        if prev_v is not None and v != prev_v:
            changes.append((t, prev_v, v))
        prev_v = v

    # Scales with the actual run length so a 10-15 minute run doesn't cram
    # its whole timeline into the same width as a 3-minute one - clamped so
    # it doesn't keep growing unreasonably for very long runs.
    total_elapsed_s = elapsed_s[-1] - elapsed_s[0] if len(elapsed_s) > 1 else 0
    fig_width = min(20, max(10, total_elapsed_s / 60.0 * 1.5))
    fig, ax = plt.subplots(figsize=(fig_width, 5))
    ax.plot(elapsed_s, gap_ms, color="tab:green", marker=".", markersize=3, label="Position gap (ms)")
    for t, _, _ in changes:
        ax.axvline(t, color="tab:red", linestyle="--", alpha=0.3)

    slope_ms_per_s = None
    if len(valid) >= 2:
        ts = np.array([t for t, _ in valid])
        vs = np.array([v for _, v in valid])
        if ts[-1] > ts[0]:
            slope_ms_per_s, intercept = np.polyfit(ts, vs, 1)
            ax.plot(
                ts, slope_ms_per_s * ts + intercept, color="tab:orange", linestyle=":",
                label="Linear fit: {:.4f} ms/s ({:.2f} ms/min)".format(slope_ms_per_s, slope_ms_per_s * 60),
            )

    ax.set_xlabel("Elapsed time (s)")
    ax.set_ylabel("Position gap (ms) - panel A vs panel B")
    ax.set_title("Panel-to-panel drift over time (dashed = step change)")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)

    return {
        "elapsed_s_range": (valid[0][0], valid[-1][0]) if valid else None,
        "changes": changes,
        "slope_ms_per_s": slope_ms_per_s,
        "n_timestamp_discontinuities": n_discontinuities,
    }


def frame_source(pipeline, panel_a_xy, panel_b_xy, neighborhood_size):
    metadata = rs.frame_metadata_value.frame_timestamp
    while True:
        frameset = pipeline.wait_for_frames()
        if PICK["stream_type"] == rs.stream.infrared:
            frame = frameset.get_infrared_frame(PICK["stream_index"])
        else:
            frame = frameset.get_color_frame(PICK["stream_index"])
        if not frame:
            continue
        if not frame.supports_frame_metadata(metadata):
            raise RuntimeError(
                "This camera/driver does not expose per-frame HW timestamp metadata - see "
                "engine/streams.py's ContinuousCapture for the same requirement/fix."
            )

        image = decode_frame(bytes(frame.get_data()), PICK["format"], PICK["width"], PICK["height"])
        ts_us = frame.get_frame_metadata(metadata)
        bright_a = sample_all_neighborhood_brightness(image, panel_a_xy, neighborhood_size)
        bright_b = sample_all_neighborhood_brightness(image, panel_b_xy, neighborhood_size)

        # Same image, same timestamp for both "sides" - it's genuinely one
        # frame; only the sampled brightness differs (panel A's LED
        # positions vs panel B's).
        yield image, image, ts_us, ts_us, bright_a, bright_b


def main():
    settings = load_settings(SETTINGS_PATH)
    output_dir = ensure_output_dir(settings)
    dual_panel_config = settings["dual_panel"]
    test_settings = settings["test"]

    panel_a_positions, panel_b_positions = load_panel_calibration(output_dir)
    panel_a_xy, panel_a_on, panel_a_off = positions_to_arrays(panel_a_positions)
    panel_b_xy, panel_b_on, panel_b_off = positions_to_arrays(panel_b_positions)

    if len(panel_a_xy) != len(panel_b_xy):
        print(
            "WARNING: panel A has {} calibrated LED(s) but panel B has {} - drift readings will "
            "likely be meaningless. Re-run tools/panel_drift_calibrate.py.".format(
                len(panel_a_xy), len(panel_b_xy)
            )
        )
    num_leds = len(panel_a_xy)

    # Reuses settings.yaml test.stream_a_threshold_fraction/
    # stream_b_threshold_fraction as "panel A's fraction"/"panel B's
    # fraction" here - no new settings.yaml keys for this niche test.
    panel_a_threshold = compute_threshold(panel_a_on, panel_a_off, test_settings["stream_a_threshold_fraction"])
    panel_b_threshold = compute_threshold(panel_b_on, panel_b_off, test_settings["stream_b_threshold_fraction"])

    device_serial = resolve_device_serial()

    position_gap_metric = PositionGapMetric(
        stream_a_threshold=panel_a_threshold,
        stream_b_threshold=panel_b_threshold,
        num_leds=num_leds,
        switch_time_ms=test_settings["switch_time_ms"],
        warmup_pairs_to_skip=test_settings["warmup_pairs_to_skip"],
    )
    metrics = [
        PairingGapMetric(outlier_threshold_us=test_settings["pairing_gap_outlier_threshold_us"]),
        position_gap_metric,
    ]
    session_config = TestSessionConfig(
        metrics=metrics,
        duration_s=DURATION_S,
        stream_a_fps=PICK["fps"],
        stream_b_fps=PICK["fps"],
        frame_drop_threshold_factor=test_settings["frame_drop_threshold_factor"],
    )
    test_session = TestSession(session_config)

    stop_requested = {"flag": False}

    def handle_sigint(signum, frame):
        print("\nCtrl+C received - stopping...")
        stop_requested["flag"] = True

    signal.signal(signal.SIGINT, handle_sigint)

    def on_frames(image_a, image_b, pair_index):
        # image_a/image_b are the SAME frame here (see frame_source's own
        # comment) - drawing panel A's overlay first, then panel B's on top
        # of the result, puts both panels' on/off circles on one window.
        overlay = draw_led_state_overlay(image_a, panel_a_xy, position_gap_metric.last_stream_a_on_mask)
        overlay = draw_led_state_overlay(overlay, panel_b_xy, position_gap_metric.last_stream_b_on_mask)
        cv2.imshow(LIVE_VIEW_WINDOW, overlay)
        if (cv2.waitKey(1) & 0xFF) == ord("q"):
            stop_requested["flag"] = True

    def on_row(row):
        pass

    def on_stats(row):
        print("pair {}: position_gap_ms={} pairing_gap_us={}".format(
            row.get("pair_index"), row.get("position_gap_ms"), row.get("pairing_gap_us")
        ))

    print("Arming dual-panel scanning (switch_time_ms={})...".format(test_settings["switch_time_ms"]))
    dual_panel_control.start_scanning(
        test_settings["switch_time_ms"], test_settings["scan_direction"], dual_panel_config,
    )

    config = rs.config()
    config.enable_device(device_serial)
    config.enable_stream(
        PICK["stream_type"], PICK["stream_index"], PICK["width"], PICK["height"], PICK["format"], PICK["fps"],
    )
    pipeline = rs.pipeline()
    pipeline.start(config)

    rows = []
    try:
        test_session.start()
        loop = AcquisitionLoop(
            frame_source(pipeline, panel_a_xy, panel_b_xy, test_settings["neighborhood_size"]),
            test_session,
            AcquisitionCallbacks(on_frames=on_frames, on_row=on_row, on_stats=on_stats),
            # See DISPLAY_STRIDE's own comment - every pair's data reaches
            # the CSV/plots regardless of this value; it only throttles
            # console prints/live-view redraws.
            display_stride=DISPLAY_STRIDE,
        )
        start_time = time.time()
        print("Measuring for {}s (Ctrl+C to stop early, or press 'q' in the live view window)...".format(DURATION_S))
        rows = loop.run_until_stopped(
            is_stop_requested=lambda: stop_requested["flag"],
            elapsed_s_fn=lambda: time.time() - start_time,
        )
    finally:
        pipeline.stop()
        cv2.destroyAllWindows()
        print("Disarming dual-panel scanning...")
        dual_panel_control.stop_scanning(dual_panel_config)

    kept_path = os.path.join(output_dir, "panel_drift_raw.csv")
    dropped_path = os.path.join(output_dir, "panel_drift_frame_drops.csv")
    n_kept, n_dropped = export_session_csvs(rows, kept_path, dropped_path)
    print("Wrote {} kept row(s) to {}, {} dropped-frame row(s) to {}".format(
        n_kept, kept_path, n_dropped, dropped_path
    ))

    if rows:
        plot_path = os.path.join(output_dir, "panel_drift_plot.png")
        export_session_plot(rows, plot_path)
        print("Saved drift plot to {}".format(plot_path))

        over_time_path = os.path.join(output_dir, "panel_drift_over_time.png")
        summary = export_drift_over_time_plot(rows, over_time_path)
        print("Saved time-based drift plot to {}".format(over_time_path))
        if summary is None:
            print("No timestamped samples to summarize.")
        else:
            if summary["elapsed_s_range"] is not None:
                start_s, end_s = summary["elapsed_s_range"]
                print("Covered {:.1f}s of non-excluded samples ({:.1f}s to {:.1f}s elapsed).".format(
                    end_s - start_s, start_s, end_s
                ))
            if summary["n_timestamp_discontinuities"]:
                print(
                    "NOTE: {} camera HW-timestamp discontinuity(ies) detected during this run - "
                    "elapsed time was held steady across each one rather than corrupted; the true "
                    "real-world duration of each glitch is unknown and not included above.".format(
                        summary["n_timestamp_discontinuities"]
                    )
                )
            if summary["changes"]:
                print("{} step change(s) - each is one more switch_time_ms of accumulated skew:".format(
                    len(summary["changes"])
                ))
                for t, old, new in summary["changes"]:
                    print("  at {:.2f}s: {} ms -> {} ms".format(t, old, new))
            else:
                print("No step changes detected - either no measurable drift in this run, or not enough elapsed time.")
            if summary["slope_ms_per_s"] is not None:
                print("Linear drift-rate estimate: {:.4f} ms/s ({:.2f} ms/min).".format(
                    summary["slope_ms_per_s"], summary["slope_ms_per_s"] * 60
                ))

    gaps = [row["position_gap_ms"] for row in rows
            if row.get("position_gap_ms") is not None and not row.get("position_gap_ms_excluded")]
    if gaps:
        print("position_gap_ms over {} sample(s): min={:.3f} max={:.3f} mean={:.3f}".format(
            len(gaps), min(gaps), max(gaps), sum(gaps) / len(gaps)
        ))
    else:
        print("No non-excluded position_gap_ms samples were recorded.")


if __name__ == "__main__":
    main()
