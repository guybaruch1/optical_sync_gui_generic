"""JSON persistence for the resolved run-config a completed GUI wizard flow
produces, so tools/standalone_sync_test/run_sync_test.py can rerun the main
live sync test without going through the wizard again.

Deliberately lives in this tool's own folder, not engine/state, so the real
app only gains one import + one call (see gui/pages/live_session_page.py's
start_session()) - all the actual serialization logic (numpy arrays <->
plain lists, rs.stream/rs.format enum members <-> their .name strings)
stays out of app code.

CONFIG_FILENAME is written under the app's own settings.yaml paths.
output_dir every time a real Live Session run starts (always overwritten,
so it reflects the MOST RECENT real GUI run, not a specific historical
one). The real GUI resolves that output_dir relative to its own working
directory (always the project root, in practice), so run_sync_test.py
must NOT do the same relative-to-cwd join itself - a script can
reasonably be invoked from anywhere, including from inside this very
tools/standalone_sync_test/ folder, which would otherwise silently look
in (or create) a second, different "output" folder there instead of the
project's real one. run_sync_test.py anchors this filename to REPO_ROOT
itself rather than a helper here, the same way it already anchors
settings.yaml's own path - see that script's own main().
"""

import json

import numpy as np
import pyrealsense2 as rs

CONFIG_FILENAME = "gui_run_config.json"


def _pick_to_json(pick):
    return {
        "sensor_index": pick["sensor_index"],
        "stream_type": pick["stream_type"].name,
        "stream_index": pick["stream_index"],
        "format": pick["format"].name,
        "width": pick["width"],
        "height": pick["height"],
        "fps": pick["fps"],
    }


def _pick_from_json(raw):
    return {
        "sensor_index": raw["sensor_index"],
        "stream_type": getattr(rs.stream, raw["stream_type"]),
        "stream_index": raw["stream_index"],
        "format": getattr(rs.format, raw["format"]),
        "width": raw["width"],
        "height": raw["height"],
        "fps": raw["fps"],
    }


def write_gui_run_config(path, ctx, switch_time_ms, display_stride, duration_s):
    """Snapshots everything run_sync_test.py needs to rerun THIS exact live
    sync test headless, straight from a real GUI run's already-resolved
    self._context (gui/pages/live_session_page.py's LiveSessionPage) plus
    the few values only known at Start (the CONFIRMED switch time - not
    ctx["switch_time_ms"], settings.yaml's stale starting default - and
    the toolbar's own display_stride/duration_s, same "read what THIS run
    actually uses, not a default" reasoning start_session() itself already
    applies to the metric/thread construction). Always overwrites `path`,
    so it always reflects the most recently STARTED real GUI run, not a
    specific historical one."""
    payload = {
        "device_serial": ctx["device_serial"],
        "camera_name": ctx["camera_name"],
        "pick_a": _pick_to_json(ctx["pick_a"]),
        "pick_b": _pick_to_json(ctx["pick_b"]),
        "camera_controls": ctx["camera_controls"],
        "stream_a_xy": np.asarray(ctx["stream_a_xy"]).tolist(),
        "stream_b_xy": np.asarray(ctx["stream_b_xy"]).tolist(),
        "stream_a_threshold": np.asarray(ctx["stream_a_threshold"]).tolist(),
        "stream_b_threshold": np.asarray(ctx["stream_b_threshold"]).tolist(),
        "num_leds": ctx["num_leds"],
        "neighborhood_size": ctx["neighborhood_size"],
        "switch_time_ms": switch_time_ms,
        "scan_direction": ctx["scan_direction"],
        "frame_drop_threshold_factor": ctx["frame_drop_threshold_factor"],
        "warmup_pairs_to_skip": ctx["warmup_pairs_to_skip"],
        "pairing_gap_outlier_threshold_us": ctx["pairing_gap_outlier_threshold_us"],
        "position_gap_outlier_threshold_ms": ctx["position_gap_outlier_threshold_ms"],
        "position_gap_outlier_max_snapshots": ctx["position_gap_outlier_max_snapshots"],
        "stream_a_label": ctx["stream_a_label"],
        "stream_b_label": ctx["stream_b_label"],
        "dual_panel_config": ctx["dual_panel_config"],
        "enable_depth_for_ir_sync": ctx["enable_depth_for_ir_sync"],
        "hardware_reset_before_start": ctx["hardware_reset_before_start"],
        "hardware_reset_settle_s": ctx["hardware_reset_settle_s"],
        "duration_s": duration_s,
        "display_stride": display_stride,
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)


def load_gui_run_config(path):
    """Inverse of write_gui_run_config - restores pick_a/pick_b's
    stream_type/format to real rs enum members and the xy/threshold lists
    back to numpy arrays (engine.metrics.PositionGapMetric and
    domain.realsense_utils.sample_all_neighborhood_brightness both do
    numpy ops on these, not plain Python lists)."""
    with open(path, "r") as f:
        payload = json.load(f)
    payload["pick_a"] = _pick_from_json(payload["pick_a"])
    payload["pick_b"] = _pick_from_json(payload["pick_b"])
    payload["stream_a_xy"] = np.array(payload["stream_a_xy"])
    payload["stream_b_xy"] = np.array(payload["stream_b_xy"])
    payload["stream_a_threshold"] = np.array(payload["stream_a_threshold"])
    payload["stream_b_threshold"] = np.array(payload["stream_b_threshold"])
    return payload
