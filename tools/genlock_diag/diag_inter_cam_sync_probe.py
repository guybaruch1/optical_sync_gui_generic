"""Diagnostic script - NOT part of the shipped app, no automated tests.

Probes whether real hardware genlock (rs.option.inter_cam_sync_mode) between
TWO connected RealSense devices actually works, before any raw master/slave
value is trusted enough to become a settings.yaml default. This exists
because a previous attempt hardcoded master=1/slave=2 (the publicly
documented D400-series scheme) straight into settings.yaml with no
real-hardware proof it did anything - this project's own established
practice (see CLAUDE.md's dual-panel arm-sequence history, the gain-restore
bug) is: always validate new hardware behavior with a dedicated diagnostic
script BEFORE it becomes a default.

What "working" means here is NOT "the SDK accepted the value without
raising" - engine.streams.set_inter_cam_sync_mode only returns False if NO
sensor supports the option at all; a WRONG-but-accepted value would still
report success with that check alone. The actual proof this script looks
for: two independently-clocked devices' own per-frame HW timestamps
(rs.frame_metadata_value.frame_timestamp) collapsing from a large,
essentially-arbitrary offset (this project has previously measured ~5.1
MINUTES between two unsynced D455s at the same wall-clock moment - each
device's clock starts at its own arbitrary epoch) down to something small
AND STABLE across the whole recording window once genlock is applied. Small
but drifting means the two clocks happened to be close by luck, not real
hardware sync - the stability check across --slices sub-windows is what
tells those two cases apart.

Measures the IR and the color/RGB channel SEPARATELY and reports a verdict
for each independently - a real, documented D400-series limitation is that
hardware genlock may synchronize only the stereo/depth module, not the
color sensor, which directly matters for whether this app's cross-camera
IR-vs-RGB pairing can ever be genlocked on this hardware or only IR-vs-IR
can.

Reuses engine.streams.list_devices/find_device_by_serial/
set_inter_cam_sync_mode/INTER_CAM_SYNC_DEFAULT/MASTER/SLAVE and
ContinuousCapture (one instance per device, pairing that device's own IR +
color streams through the SAME proven enable_depth_for_ir_sync fix a real
single-camera run uses - so any offset measured ACROSS the two devices here
is attributable to inter_cam_sync_mode, not the already-solved single-device
IR/RGB open-order bug). Deliberately does NOT reuse
engine.cross_camera_reconciler.CrossCameraReconciler - that's a streaming
matcher shaped for TestSession rows; this only needs a one-shot batch
nearest-time match over two recorded arrays, written locally below.

Run from the repo root (needs both devices connected, and genlock's physical
sync cable wired between them):
    python tools/genlock_diag/diag_inter_cam_sync_probe.py
    python tools/genlock_diag/diag_inter_cam_sync_probe.py --serial-a 123 --serial-b 456
    python tools/genlock_diag/diag_inter_cam_sync_probe.py --master-value 1 --slave-value 2 --duration-s 20

With no --serial-a/--serial-b and exactly 2 devices connected, auto-picks
both (reports which is which). Always resets both devices' genlock role
back to INTER_CAM_SYNC_DEFAULT before exiting, even on failure/Ctrl+C - this
script must not itself leave a real camera stuck in slave mode.
"""

import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pyrealsense2 as rs

from engine.streams import (
    list_devices, find_device_by_serial, ContinuousCapture,
    set_inter_cam_sync_mode, INTER_CAM_SYNC_DEFAULT, INTER_CAM_SYNC_MASTER, INTER_CAM_SYNC_SLAVE,
)

# Hardcoded, not exposed as CLI flags - this is a one-off probe, not a
# general tool. Matches settings.yaml's existing "RealSense D455" "IR vs RGB
# sync" sensor_options entry exactly, so any result here is directly
# comparable to what a real run would see.
IR_PICK = {"stream_type": rs.stream.infrared, "stream_index": 1, "width": 1280, "height": 720, "fps": 30, "format": rs.format.y8}
COLOR_PICK = {"stream_type": rs.stream.color, "stream_index": 0, "width": 1280, "height": 720, "fps": 30, "format": rs.format.bgr8}


def _resolve_two_serials(ctx, serial_a, serial_b):
    if serial_a is not None and serial_b is not None:
        return serial_a, serial_b
    devices = list_devices(ctx)
    if len(devices) != 2:
        raise RuntimeError(
            "Need exactly 2 connected devices to auto-pick (found {}) - pass "
            "--serial-a/--serial-b explicitly. Connected: {}".format(
                len(devices), ", ".join("{} ({})".format(d.name, d.serial) for d in devices)
            )
        )
    print("Auto-picked the 2 connected devices (pass --serial-a/--serial-b to choose explicitly):")
    for d in devices:
        print("  {} ({})".format(d.name, d.serial))
    return devices[0].serial, devices[1].serial


def report_sensor_support(label, device):
    print("\n{} - sensor support for rs.option.inter_cam_sync_mode:".format(label))
    any_supported = False
    for sensor_index, sensor in enumerate(device.query_sensors()):
        sensor_name = (
            sensor.get_info(rs.camera_info.name) if sensor.supports(rs.camera_info.name)
            else "sensor {}".format(sensor_index)
        )
        if sensor.supports(rs.option.inter_cam_sync_mode):
            any_supported = True
            option_range = sensor.get_option_range(rs.option.inter_cam_sync_mode)
            print("  [{}] {}: SUPPORTED (range min={} max={} default={} step={})".format(
                sensor_index, sensor_name, option_range.min, option_range.max,
                option_range.default, option_range.step,
            ))
        else:
            print("  [{}] {}: not supported".format(sensor_index, sensor_name))
    if not any_supported:
        print("  WARNING: no sensor on this device supports inter_cam_sync_mode at all.")


def _capture_samples_thread(serial, stop_event, samples_out, samples_lock, errors_out):
    capture = ContinuousCapture(serial, IR_PICK, COLOR_PICK)
    try:
        capture.start()
        for _image_a, _image_b, ts_a, ts_b, _num_a, _num_b in capture.frames_with_diagnostics():
            wall_time = time.monotonic()
            with samples_lock:
                samples_out.append((wall_time, ts_a, ts_b))
            if stop_event.is_set():
                break
    except Exception as exc:
        errors_out.append(exc)
    finally:
        capture.stop()


def measure_cross_device(serial_a, serial_b, duration_s, warmup_s, label):
    """Opens both devices' own ContinuousCapture concurrently on background
    threads, discards a warmup_s settle window, then records for duration_s.
    Returns the two devices' raw (wall_time, ir_ts_us, color_ts_us) sample
    lists, un-matched - nearest_match_offsets does the actual cross-device
    pairing."""
    print("\n{}: recording {:.1f}s (after {:.1f}s warmup)...".format(label, duration_s, warmup_s))
    stop_event_a, stop_event_b = threading.Event(), threading.Event()
    samples_a, samples_b = [], []
    lock_a, lock_b = threading.Lock(), threading.Lock()
    errors_a, errors_b = [], []

    thread_a = threading.Thread(
        target=_capture_samples_thread, args=(serial_a, stop_event_a, samples_a, lock_a, errors_a),
    )
    thread_b = threading.Thread(
        target=_capture_samples_thread, args=(serial_b, stop_event_b, samples_b, lock_b, errors_b),
    )
    thread_a.start()
    thread_b.start()

    time.sleep(warmup_s)
    with lock_a:
        samples_a.clear()
    with lock_b:
        samples_b.clear()

    time.sleep(duration_s)
    stop_event_a.set()
    stop_event_b.set()
    thread_a.join(timeout=10.0)
    thread_b.join(timeout=10.0)

    if errors_a or errors_b:
        raise RuntimeError("Capture failed - device A errors: {} - device B errors: {}".format(errors_a, errors_b))

    with lock_a:
        result_a = list(samples_a)
    with lock_b:
        result_b = list(samples_b)
    print("  Recorded {} samples from A, {} samples from B.".format(len(result_a), len(result_b)))
    return result_a, result_b


def nearest_match_offsets(samples_a, samples_b, max_gap_s):
    """Batch nearest-wall-clock-time match: for each entry in samples_a,
    finds the closest-in-time entry in samples_b BY THE WALL-CLOCK TIME each
    sample was recorded at (not the HW timestamp - that's exactly what's
    being measured). Drops any pair further apart than max_gap_s - an
    explicit exclusion, never a forced/misleading match, matching this
    project's own frame-drop/outlier conventions elsewhere. Returns two
    separate (wall_time, offset_us) lists: IR channel and color channel,
    where offset_us = B's timestamp minus A's for that matched pair (HW
    frame_timestamp is already in microseconds, matching this project's own
    *_ts_us naming convention - no unit conversion needed)."""
    ir_offsets, color_offsets = [], []
    if not samples_a or not samples_b:
        return ir_offsets, color_offsets

    b_index = 0
    for wall_a, ir_a, color_a in samples_a:
        best = None
        j = b_index
        while j < len(samples_b):
            wall_b, ir_b, color_b = samples_b[j]
            gap = abs(wall_b - wall_a)
            if best is None or gap < best[0]:
                best = (gap, j, ir_b, color_b)
            elif wall_b - wall_a > max_gap_s:
                break
            j += 1
        if best is None:
            continue
        gap, matched_index, ir_b, color_b = best
        if gap > max_gap_s:
            continue
        ir_offsets.append((wall_a, ir_b - ir_a))
        color_offsets.append((wall_a, color_b - color_a))
        b_index = matched_index
    return ir_offsets, color_offsets


def summarize_offsets(offsets):
    if not offsets:
        return {"n": 0, "mean_us": None, "stdev_us": None, "min_us": None, "max_us": None}
    values = [v for _, v in offsets]
    n = len(values)
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n if n > 1 else 0.0
    return {"n": n, "mean_us": mean, "stdev_us": variance ** 0.5, "min_us": min(values), "max_us": max(values)}


def stability_report(offsets, num_slices):
    """Splits offsets (already ordered by wall time) into num_slices
    contiguous chunks and reports each chunk's mean plus the spread
    (max-min) across chunk means - small aggregate offset with small spread
    is real, stable sync; small aggregate but large spread means the two
    clocks were coincidentally close, not actually synced."""
    if not offsets or num_slices < 1:
        return {"slice_means_us": [], "spread_us": None}
    chunk_size = max(1, len(offsets) // num_slices)
    slice_means = []
    for i in range(0, len(offsets), chunk_size):
        chunk_values = [v for _, v in offsets[i:i + chunk_size]]
        if chunk_values:
            slice_means.append(sum(chunk_values) / len(chunk_values))
    spread = (max(slice_means) - min(slice_means)) if len(slice_means) > 1 else 0.0
    return {"slice_means_us": slice_means, "spread_us": spread}


def apply_roles(device_a, device_b, master_value, slave_value):
    if not set_inter_cam_sync_mode(device_a, master_value):
        print("FAILED: device A has no sensor supporting inter_cam_sync_mode at all - cannot genlock.")
        return False
    if not set_inter_cam_sync_mode(device_b, slave_value):
        print("FAILED: device B has no sensor supporting inter_cam_sync_mode at all - cannot genlock.")
        # Roll back A - already touched it, never leave a partially-applied
        # role lingering on a real device (same reasoning as
        # MultiCameraSessionController._reset_genlock_roles).
        set_inter_cam_sync_mode(device_a, INTER_CAM_SYNC_DEFAULT)
        return False
    return True


def reset_roles(device_a, device_b):
    set_inter_cam_sync_mode(device_a, INTER_CAM_SYNC_DEFAULT)
    set_inter_cam_sync_mode(device_b, INTER_CAM_SYNC_DEFAULT)


def verdict(synced_summary, stability, working_threshold_us=1000.0, stable_threshold_us=1000.0):
    if synced_summary["n"] == 0:
        return "INCONCLUSIVE - no matched frame pairs recorded"
    if abs(synced_summary["mean_us"]) >= working_threshold_us:
        return "NOT SYNCED - offset still large ({:.0f}us mean)".format(synced_summary["mean_us"])
    spread = stability["spread_us"] or 0.0
    if spread >= stable_threshold_us:
        return "UNSTABLE / INCONCLUSIVE - offset small but drifts across the recording ({:.0f}us spread)".format(spread)
    return "WORKING - offset small and stable ({:.0f}us mean, {:.0f}us spread)".format(
        synced_summary["mean_us"], spread
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--serial-a", default=None, help="Device A serial. With neither serial given, auto-picks if exactly 2 devices are connected.")
    parser.add_argument("--serial-b", default=None, help="Device B serial.")
    parser.add_argument("--master-value", type=int, default=INTER_CAM_SYNC_MASTER, help="Raw inter_cam_sync_mode value applied to device A. Default matches the publicly documented D400-series 'master' value - unconfirmed on this hardware, hence this script.")
    parser.add_argument("--slave-value", type=int, default=INTER_CAM_SYNC_SLAVE, help="Raw inter_cam_sync_mode value applied to device B.")
    parser.add_argument("--duration-s", type=float, default=15.0, help="Recording window per measurement, in seconds.")
    parser.add_argument("--warmup-s", type=float, default=2.0, help="Settle time discarded before each recording starts.")
    parser.add_argument("--slices", type=int, default=5, help="Number of contiguous sub-windows the synced measurement is split into for the stability check.")
    parser.add_argument("--max-gap-s", type=float, default=0.05, help="Max wall-clock gap (seconds) allowed for a cross-device frame match - unmatched samples are dropped, never forced.")
    args = parser.parse_args()

    ctx = rs.context()
    serial_a, serial_b = _resolve_two_serials(ctx, args.serial_a, args.serial_b)
    device_a = find_device_by_serial(ctx, serial_a)
    device_b = find_device_by_serial(ctx, serial_b)
    name_a = device_a.get_info(rs.camera_info.name)
    name_b = device_b.get_info(rs.camera_info.name)
    print("Device A: {} ({})".format(name_a, serial_a))
    print("Device B: {} ({})".format(name_b, serial_b))

    report_sensor_support("Device A", device_a)
    report_sensor_support("Device B", device_b)

    print("\n=== Step 1: UNSYNCED baseline (inter_cam_sync_mode untouched) ===")
    samples_a, samples_b = measure_cross_device(serial_a, serial_b, args.duration_s, args.warmup_s, "Unsynced")
    unsynced_ir_offsets, unsynced_color_offsets = nearest_match_offsets(samples_a, samples_b, args.max_gap_s)
    unsynced_ir = summarize_offsets(unsynced_ir_offsets)
    unsynced_color = summarize_offsets(unsynced_color_offsets)
    print("  IR offset:    {}".format(unsynced_ir))
    print("  Color offset: {}".format(unsynced_color))

    print("\n=== Step 2: applying master={} (device A) / slave={} (device B) ===".format(args.master_value, args.slave_value))
    if not apply_roles(device_a, device_b, args.master_value, args.slave_value):
        print("\nAborting - could not apply genlock roles to both devices. Nothing left applied (rolled back).")
        return

    try:
        print("\n=== Step 3: SYNCED measurement ===")
        samples_a, samples_b = measure_cross_device(serial_a, serial_b, args.duration_s, args.warmup_s, "Synced")
        synced_ir_offsets, synced_color_offsets = nearest_match_offsets(samples_a, samples_b, args.max_gap_s)
        synced_ir = summarize_offsets(synced_ir_offsets)
        synced_color = summarize_offsets(synced_color_offsets)
        ir_stability = stability_report(synced_ir_offsets, args.slices)
        color_stability = stability_report(synced_color_offsets, args.slices)
    finally:
        reset_roles(device_a, device_b)
        print("\nGenlock roles reset back to default (INTER_CAM_SYNC_DEFAULT) on both devices.")

    print("\n================ FINAL REPORT ================")
    print("Device A: {} ({})".format(name_a, serial_a))
    print("Device B: {} ({})".format(name_b, serial_b))
    print("Applied values: master={} (A) / slave={} (B)".format(args.master_value, args.slave_value))

    print("\n-- IR channel --")
    print("  Unsynced: {}".format(unsynced_ir))
    print("  Synced:   {}".format(synced_ir))
    print("  Per-slice means (us): {}".format(ir_stability["slice_means_us"]))
    print("  VERDICT: {}".format(verdict(synced_ir, ir_stability)))

    print("\n-- Color/RGB channel --")
    print("  Unsynced: {}".format(unsynced_color))
    print("  Synced:   {}".format(synced_color))
    print("  Per-slice means (us): {}".format(color_stability["slice_means_us"]))
    print("  VERDICT: {}".format(verdict(synced_color, color_stability)))


if __name__ == "__main__":
    main()
