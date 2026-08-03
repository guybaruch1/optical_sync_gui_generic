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
tools/panel_drift_calibrate.py). Runs for settings.yaml's test.duration_s,
or press Ctrl+C to stop early. Writes
output/panel_drift_raw.csv/output/panel_drift_frame_drops.csv.
"""

import os
import sys
import time
import signal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
from domain.realsense_utils import decode_frame, sample_all_neighborhood_brightness
from domain.csv_export import export_session_csvs

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

    metrics = [
        PairingGapMetric(outlier_threshold_us=test_settings["pairing_gap_outlier_threshold_us"]),
        PositionGapMetric(
            stream_a_threshold=panel_a_threshold,
            stream_b_threshold=panel_b_threshold,
            num_leds=num_leds,
            switch_time_ms=test_settings["switch_time_ms"],
            warmup_pairs_to_skip=test_settings["warmup_pairs_to_skip"],
        ),
    ]
    session_config = TestSessionConfig(
        metrics=metrics,
        duration_s=test_settings["duration_s"],
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
        pass

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
            display_stride=10,
        )
        start_time = time.time()
        print("Measuring for {}s (Ctrl+C to stop early)...".format(test_settings["duration_s"]))
        rows = loop.run_until_stopped(
            is_stop_requested=lambda: stop_requested["flag"],
            elapsed_s_fn=lambda: time.time() - start_time,
        )
    finally:
        pipeline.stop()
        print("Disarming dual-panel scanning...")
        dual_panel_control.stop_scanning(dual_panel_config)

    kept_path = os.path.join(output_dir, "panel_drift_raw.csv")
    dropped_path = os.path.join(output_dir, "panel_drift_frame_drops.csv")
    n_kept, n_dropped = export_session_csvs(rows, kept_path, dropped_path)
    print("Wrote {} kept row(s) to {}, {} dropped-frame row(s) to {}".format(
        n_kept, kept_path, n_dropped, dropped_path
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
