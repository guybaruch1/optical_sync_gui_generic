"""Standalone script - NOT part of the shipped app, no automated tests (same
"hardware-only, no tests by design" bucket as engine/session_engine.py,
tools/panel_drift/panel_drift_measure.py, tools/*_diag/diag_*.py).

Reruns the main live sync test - the same capture/metrics pipeline
engine/session_engine.py's SessionEngineThread drives for the GUI's Live
Session page - as a single flat script with no Qt, no QThread, no wizard:
run it, watch it print, set a breakpoint anywhere in this file or step
into any of the engine/domain functions it calls directly. Every
hardware-facing and math piece is reused completely UNCHANGED
(engine.streams' ContinuousCapture/resolve_and_group/camera-control
setters, engine.dual_panel_control's start_scanning/stop_scanning,
engine.acquisition_loop.AcquisitionLoop, engine.test_session.TestSession,
engine.metrics' PairingGapMetric/PositionGapMetric, domain.csv_export/
domain.plot_export) - nothing about the real app's code changed to build
this, and nothing here is a second copy of that logic. Only the
orchestration around those calls (this file) and the config-file plumbing
(config_io.py) are new.

Needs a run config to work from - everything Stream Config/Calibration/
Threshold Tuning resolve interactively (which streams, camera controls,
calibrated LED positions/thresholds, switch time, dual-panel wiring).
Rather than re-deriving all of that from CLI flags, this reads it straight
from gui_run_config.json (config_io.py), which the real GUI now writes
automatically every time a real Live Session run starts (see
gui/pages/live_session_page.py's start_session()). So the actual flow is:
run the GUI wizard through to a real Live Session Start at least once (to
produce that file), then rerun THIS script as many times as you want with
no GUI involved at all, for as long as that config stays valid (same
camera plugged in, same calibration still on disk).

Run from the repo root:
    python tools/standalone_sync_test/run_sync_test.py

Options (all default to whatever the GUI run that produced the config
actually used):
    --config PATH          path to gui_run_config.json
                            (default: <settings.yaml paths.output_dir>/gui_run_config.json)
    --duration SECONDS      0 or omitted = unlimited (Ctrl+C to stop)
    --switch-time-ms MS
    --display-stride N      how many frame-pairs between console prints

Press Ctrl+C at any time to stop early and still get CSVs/a plot for
whatever was captured so far.
"""

import argparse
import os
import signal
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

import pyrealsense2 as rs  # noqa: E402

from settings import load_settings  # noqa: E402
from engine.streams import (  # noqa: E402
    ContinuousCapture, find_device_by_serial, resolve_and_group,
    set_emitter_enabled, enable_auto_exposure, set_manual_exposure, exposure_for_group,
)
from engine.dual_panel_control import start_scanning, stop_scanning  # noqa: E402
from engine.metrics import PairingGapMetric, PositionGapMetric  # noqa: E402
from engine.test_session import TestSession, TestSessionConfig  # noqa: E402
from engine.acquisition_loop import AcquisitionLoop, AcquisitionCallbacks  # noqa: E402
from domain.realsense_utils import sample_all_neighborhood_brightness, safe_neighborhood_size  # noqa: E402
from domain.csv_export import export_session_csvs  # noqa: E402
from domain.plot_export import export_session_plot  # noqa: E402
from domain.run_output import create_run_dir  # noqa: E402
from tools.standalone_sync_test.config_io import load_gui_run_config, CONFIG_FILENAME  # noqa: E402


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=None,
                         help="Path to gui_run_config.json (default: under settings.yaml's paths.output_dir)")
    parser.add_argument("--duration", type=float, default=None, help="Override run duration in seconds (0 = unlimited)")
    parser.add_argument("--switch-time-ms", type=float, default=None, help="Override the LED panel's switch time")
    parser.add_argument("--display-stride", type=int, default=None,
                         help="Override how many frame-pairs between console prints")
    return parser.parse_args()


def frame_pairs_with_brightness(capture, stream_a_xy, stream_b_xy, stream_a_safe_size, stream_b_safe_size):
    """Direct, unchanged port of engine/session_engine.py's
    SessionEngineThread._frame_pairs_with_brightness body - not imported,
    since that's a private method on a QThread subclass, not a standalone
    function this script could call."""
    for (stream_a_image, stream_b_image, stream_a_ts_us, stream_b_ts_us, _, _,
         stream_a_global_ts_us, stream_b_global_ts_us) in capture.frames_with_diagnostics():
        stream_a_bright = sample_all_neighborhood_brightness(stream_a_image, stream_a_xy, stream_a_safe_size)
        stream_b_bright = sample_all_neighborhood_brightness(stream_b_image, stream_b_xy, stream_b_safe_size)
        yield (stream_a_image, stream_b_image, stream_a_ts_us, stream_b_ts_us,
               stream_a_bright, stream_b_bright, stream_a_global_ts_us, stream_b_global_ts_us)


def apply_camera_controls(groups, pick_a, pick_b, camera_controls):
    """Same sequence engine/session_engine.py's run() applies per resolved
    sensor group - duplicated inline rather than imported, matching that
    file's own reasoning (every real call site in the app does the same,
    since this has to run before/without any shared device handle)."""
    for sensor, profiles in groups:
        group_has_infrared = any(p.stream_type() == rs.stream.infrared for p in profiles)
        if camera_controls["emitter_enabled"] is not None and group_has_infrared:
            if not set_emitter_enabled(sensor, camera_controls["emitter_enabled"]):
                print("WARNING: emitter_enabled not supported on this sensor - confirm manually.")
        if camera_controls["auto_exposure"]:
            if not enable_auto_exposure(sensor):
                print("WARNING: enable_auto_exposure not supported on this sensor - confirm manually.")
        else:
            exposure = exposure_for_group(
                profiles, pick_a, pick_b, camera_controls["exposure_a"], camera_controls["exposure_b"],
            )
            if not set_manual_exposure(sensor, exposure):
                print("WARNING: manual exposure not supported on this sensor - confirm manually.")


def main():
    args = parse_args()
    settings = load_settings(os.path.join(REPO_ROOT, "settings.yaml"))
    # Anchored to REPO_ROOT, not left as settings.yaml's own plain relative
    # "output" string (what ensure_output_dir alone would give) - the real
    # GUI (gui/pages/live_session_page.py's start_session()) always writes
    # gui_run_config.json under the PROJECT's output/, since it's launched
    # with the project root as its working directory. This script has no
    # such guarantee - it can reasonably be run from anywhere, including
    # from inside this very tools/standalone_sync_test/ folder - so its own
    # notion of "output" must resolve to the same physical folder the GUI
    # used regardless of the CURRENT WORKING DIRECTORY it happens to be
    # invoked from, the same reasoning settings.yaml's own path above
    # already gets.
    output_dir = os.path.join(REPO_ROOT, settings["paths"]["output_dir"])
    os.makedirs(output_dir, exist_ok=True)

    config_path = args.config or os.path.join(output_dir, CONFIG_FILENAME)
    if not os.path.exists(config_path):
        raise SystemExit(
            "No GUI run config found at {!r}. Run the app's wizard through to a real Live "
            "Session Start at least once first - that's what writes this file.".format(config_path)
        )
    cfg = load_gui_run_config(config_path)
    print("Loaded GUI run config from {} (camera {!r}, {} vs {})".format(
        config_path, cfg["camera_name"], cfg["stream_a_label"], cfg["stream_b_label"]
    ))

    duration_s = args.duration if args.duration is not None else cfg["duration_s"]
    if duration_s == 0:
        duration_s = None
    switch_time_ms = args.switch_time_ms if args.switch_time_ms is not None else cfg["switch_time_ms"]
    display_stride = args.display_stride if args.display_stride is not None else cfg["display_stride"]

    position_gap_metric = PositionGapMetric(
        stream_a_threshold=cfg["stream_a_threshold"], stream_b_threshold=cfg["stream_b_threshold"],
        num_leds=cfg["num_leds"], switch_time_ms=switch_time_ms,
        warmup_pairs_to_skip=cfg["warmup_pairs_to_skip"],
    )
    metrics = [
        PairingGapMetric(outlier_threshold_us=cfg["pairing_gap_outlier_threshold_us"]),
        position_gap_metric,
    ]
    test_session = TestSession(TestSessionConfig(
        metrics=metrics, duration_s=duration_s,
        stream_a_fps=cfg["pick_a"]["fps"], stream_b_fps=cfg["pick_b"]["fps"],
        frame_drop_threshold_factor=cfg["frame_drop_threshold_factor"],
    ))

    stop_requested = {"flag": False}

    def handle_sigint(signum, frame):
        print("\nCtrl+C received - stopping...")
        stop_requested["flag"] = True

    signal.signal(signal.SIGINT, handle_sigint)

    def on_frames(stream_a_image, stream_b_image, pair_index):
        pass

    def on_row(row):
        pass

    def on_stats(row):
        print("pair {}: pairing_gap_us={} position_gap_ms={} stream_a_drop={} stream_b_drop={}".format(
            row.get("pair_index"), row.get("pairing_gap_us"), row.get("position_gap_ms"),
            row.get("stream_a_frame_drop"), row.get("stream_b_frame_drop"),
        ))

    ctx = rs.context()
    device = find_device_by_serial(ctx, cfg["device_serial"])
    groups = resolve_and_group(device, cfg["pick_a"], cfg["pick_b"])
    apply_camera_controls(groups, cfg["pick_a"], cfg["pick_b"], cfg["camera_controls"])

    print("Arming LED panel scanning (switch_time_ms={})...".format(switch_time_ms))
    start_scanning(switch_time_ms, cfg["scan_direction"], cfg["dual_panel_config"])

    capture = ContinuousCapture(
        cfg["device_serial"], cfg["pick_a"], cfg["pick_b"],
        enable_depth_for_ir_sync=cfg["enable_depth_for_ir_sync"],
    )
    capture.start()

    stream_a_safe_size = safe_neighborhood_size(cfg["stream_a_xy"], cfg["neighborhood_size"])
    stream_b_safe_size = safe_neighborhood_size(cfg["stream_b_xy"], cfg["neighborhood_size"])

    rows = []
    try:
        test_session.start()
        loop = AcquisitionLoop(
            frame_pairs_with_brightness(
                capture, cfg["stream_a_xy"], cfg["stream_b_xy"], stream_a_safe_size, stream_b_safe_size,
            ),
            test_session,
            AcquisitionCallbacks(on_frames=on_frames, on_row=on_row, on_stats=on_stats),
            display_stride=display_stride,
        )
        start_time = time.time()
        print("Running{} (Ctrl+C to stop early)...".format(
            "" if duration_s is None else " for {}s".format(duration_s)
        ))
        rows = loop.run_until_stopped(
            is_stop_requested=lambda: stop_requested["flag"],
            elapsed_s_fn=lambda: time.time() - start_time,
        )
    finally:
        capture.stop()
        print("Disarming LED panel scanning...")
        stop_scanning(cfg["dual_panel_config"])

    run_dir = create_run_dir(output_dir, "standalone_sync_test")
    kept_path = os.path.join(run_dir, settings["paths"]["raw_csv_path"])
    dropped_path = os.path.join(run_dir, settings["paths"]["frame_drop_csv_path"])
    n_kept, n_dropped = export_session_csvs(rows, kept_path, dropped_path)
    print("Wrote {} kept row(s) to {}, {} dropped-frame row(s) to {}".format(
        n_kept, kept_path, n_dropped, dropped_path
    ))

    if rows:
        plot_path = os.path.join(run_dir, "sync_test_plot.png")
        export_session_plot(rows, plot_path)
        print("Saved plot to {}".format(plot_path))

    gaps = [
        r["position_gap_ms"] for r in rows
        if r.get("position_gap_ms") is not None and not r.get("position_gap_ms_excluded")
    ]
    if gaps:
        print("position_gap_ms over {} sample(s): min={:.3f} max={:.3f} mean={:.3f}".format(
            len(gaps), min(gaps), max(gaps), sum(gaps) / len(gaps)
        ))
    else:
        print("No non-excluded position_gap_ms samples were recorded.")


if __name__ == "__main__":
    main()
