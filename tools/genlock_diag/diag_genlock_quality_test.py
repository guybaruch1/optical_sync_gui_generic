"""Diagnostic script - NOT part of the shipped app, no automated tests.

A properly redesigned genlock quality test, replacing diag_inter_cam_sync_
probe.py's wall-clock-nearest-match approach, which real hardware data
showed had two real flaws:

1. rs.frame_metadata_value.frame_timestamp appears to reset close to zero
   at each pipeline's own start() call - it is NOT a persistent absolute
   hardware clock. That earlier script's "is the mean offset near zero"
   verdict was comparing two independently-reset counters and was never a
   meaningful test: both an UNSYNCED run and a "SYNCED" run showed almost
   the identical ~2.6s offset, matching that script's own start_stagger_s
   delay almost exactly - not anything about genlock.
2. Cross-device frame matching by PYTHON-OBSERVED wall-clock arrival time
   (time.monotonic() when wait_for_frames() returns) bakes Python-level
   scheduling/GIL/decode jitter into the matching step itself, which can
   mask or mimic genuine (or missing) hardware sync.

This script uses two more direct, much harder-to-fool signals instead:

- FRAME COUNT PARITY (checked first, no offset math needed): if master and
  slave are genuinely triggered by the same external signal, they MUST
  produce very close to the SAME NUMBER of frames over the same wall-clock
  window - that's the entire physical meaning of "genlocked". A real
  mismatch (already observed once on this rig: master 125 frames vs. slave
  411 frames for the same window) is treated as sufficient evidence on its
  own that master and slave are NOT running off the same clock, regardless
  of what any offset statistic says.
- INDEX-LOCKSTEP OFFSET STABILITY (only meaningful if frame counts
  matched): pairs master's Nth captured frame directly with slave's Nth
  captured frame - no wall-clock re-matching - and reports how STABLE that
  per-index offset is across the whole recording. Genuine sync shows a
  near-CONSTANT offset (small jitter, no drift) regardless of what that
  constant absolute value is; drift or large noise means the two devices
  are not actually phase-locked.

Runs BOTH an unsynced baseline pass and a synced pass (master/slave roles
applied) so the two are directly comparable - same convention as the
earlier probe. IR-only by default (isolates genlock's core effect without
composite-frameset complexity); pass --with-color to extend both devices'
pipelines to also include color, once IR-only looks good.

Run from the repo root:
    python tools/genlock_diag/diag_genlock_quality_test.py
    python tools/genlock_diag/diag_genlock_quality_test.py --serial-master 123 --serial-slave 456
    python tools/genlock_diag/diag_genlock_quality_test.py --slave-value 4 --duration-s 30
    python tools/genlock_diag/diag_genlock_quality_test.py --with-color
"""

import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pyrealsense2 as rs

from engine.streams import (
    list_devices, find_device_by_serial,
    set_inter_cam_sync_mode, INTER_CAM_SYNC_DEFAULT, INTER_CAM_SYNC_MASTER,
)

# The only value confirmed (via tools/genlock_diag/diag_sweep_slave_value_test.py,
# on this exact rig) to let BOTH IR and color stream at all as slave - the
# publicly documented value 2 fully blocked color (and, combined with IR in
# one pipeline, blocked IR too); this is a real-hardware finding, not a
# public-docs assumption. Still needs the quality check this script runs -
# "frames flow" was never proof of "frames are genuinely synchronized".
DEFAULT_SLAVE_VALUE = 4


def _resolve_two_serials(serial_master, serial_slave):
    if serial_master is not None and serial_slave is not None:
        return serial_master, serial_slave
    ctx = rs.context()
    devices = list_devices(ctx)
    if len(devices) != 2:
        raise RuntimeError(
            "Need exactly 2 connected devices (found {}), or pass both serials explicitly via "
            "--serial-master/--serial-slave.".format(len(devices))
        )
    print("Auto-picked the 2 connected devices (pass --serial-master/--serial-slave to choose explicitly):")
    for d in devices:
        print("  {} ({})".format(d.name, d.serial))
    return devices[0].serial, devices[1].serial


def _build_config(serial, with_color):
    config = rs.config()
    config.enable_device(serial)
    config.enable_stream(rs.stream.infrared, 1, 1280, 720, rs.format.y8, 30)
    if with_color:
        config.enable_stream(rs.stream.color, 0, 1280, 720, rs.format.bgr8, 30)
    return config


def _collect(serial, with_color, stop_event, samples_out, samples_lock, errors_out):
    """Appends (wall_time, ir_ts_us, color_ts_us_or_None) per frameset. Note:
    wall_time is ONLY used here to discard the warmup window and to report
    achieved fps - it is NOT used to match frames across devices (that's the
    whole point of this redesign, see module docstring)."""
    pipeline = rs.pipeline()
    started = False
    metadata = rs.frame_metadata_value.frame_timestamp
    try:
        pipeline.start(_build_config(serial, with_color))
        started = True
        while not stop_event.is_set():
            frameset = pipeline.wait_for_frames(5000)
            ir_frame = frameset.get_infrared_frame(1)
            ir_ts = ir_frame.get_frame_metadata(metadata) if ir_frame else None
            color_ts = None
            if with_color:
                color_frame = frameset.get_color_frame()
                color_ts = color_frame.get_frame_metadata(metadata) if color_frame else None
            with samples_lock:
                samples_out.append((time.monotonic(), ir_ts, color_ts))
    except Exception as exc:
        errors_out.append(exc)
    finally:
        if started:
            pipeline.stop()


def measure(serial_master, serial_slave, duration_s, warmup_s, with_color, start_stagger_s):
    stop_master, stop_slave = threading.Event(), threading.Event()
    samples_master, samples_slave = [], []
    lock_master, lock_slave = threading.Lock(), threading.Lock()
    errors_master, errors_slave = [], []

    thread_master = threading.Thread(
        target=_collect, args=(serial_master, with_color, stop_master, samples_master, lock_master, errors_master),
    )
    thread_slave = threading.Thread(
        target=_collect, args=(serial_slave, with_color, stop_slave, samples_slave, lock_slave, errors_slave),
    )
    thread_master.start()
    if start_stagger_s > 0:
        time.sleep(start_stagger_s)
    thread_slave.start()

    time.sleep(warmup_s)
    with lock_master:
        samples_master.clear()
    with lock_slave:
        samples_slave.clear()

    time.sleep(duration_s)
    stop_master.set()
    stop_slave.set()
    thread_master.join(timeout=10.0)
    thread_slave.join(timeout=10.0)

    if errors_master or errors_slave:
        raise RuntimeError("Capture failed - master errors: {} - slave errors: {}".format(errors_master, errors_slave))

    with lock_master:
        result_master = list(samples_master)
    with lock_slave:
        result_slave = list(samples_slave)
    return result_master, result_slave


def report_frame_count_parity(samples_master, samples_slave, duration_s):
    count_master, count_slave = len(samples_master), len(samples_slave)
    print("\nFrame count over {:.1f}s: master={} ({:.1f}fps)  slave={} ({:.1f}fps)".format(
        duration_s, count_master, count_master / duration_s, count_slave, count_slave / duration_s,
    ))
    if count_master == 0 or count_slave == 0:
        print("  MISMATCH: one side produced zero frames.")
        return False
    ratio = max(count_master, count_slave) / min(count_master, count_slave)
    if ratio > 1.1:
        print("  MISMATCH: master and slave produced very different frame counts ({:.0%} apart). "
              "If these were genuinely hardware-triggered by the same clock, their counts would "
              "closely match - this alone strongly suggests they are NOT running off the same "
              "clock, regardless of any offset statistic below.".format(ratio - 1))
        return False
    print("  Frame counts closely match - necessary (not sufficient) for genuine hardware locking.")
    return True


def report_own_interval_stats(label, samples, ts_index):
    ts_values = [s[ts_index] for s in samples if s[ts_index] is not None]
    if len(ts_values) < 2:
        print("  {}: not enough samples to compute interval stats.".format(label))
        return
    deltas = [b - a for a, b in zip(ts_values, ts_values[1:])]
    mean = sum(deltas) / len(deltas)
    variance = sum((d - mean) ** 2 for d in deltas) / len(deltas)
    print("  {}: own frame interval mean={:.0f}us stdev={:.0f}us (n={})".format(
        label, mean, variance ** 0.5, len(deltas),
    ))


def report_lockstep_offset(samples_master, samples_slave, ts_index, label, num_slices):
    ts_master = [s[ts_index] for s in samples_master if s[ts_index] is not None]
    ts_slave = [s[ts_index] for s in samples_slave if s[ts_index] is not None]
    n = min(len(ts_master), len(ts_slave))
    if n < 2:
        print("  {}: not enough samples for lockstep comparison.".format(label))
        return
    offsets = [ts_slave[i] - ts_master[i] for i in range(n)]
    mean = sum(offsets) / n
    variance = sum((o - mean) ** 2 for o in offsets) / n
    stdev = variance ** 0.5
    chunk_size = max(1, n // num_slices)
    slice_means = [
        sum(chunk) / len(chunk) for chunk in
        (offsets[i:i + chunk_size] for i in range(0, n, chunk_size)) if chunk
    ]
    spread = (max(slice_means) - min(slice_means)) if len(slice_means) > 1 else 0.0
    print("  {}: index-lockstep offset mean={:.0f}us stdev={:.0f}us (n={})".format(label, mean, stdev, n))
    print("    per-slice means (us): {}".format(["{:.0f}".format(m) for m in slice_means]))
    print("    slice spread: {:.0f}us".format(spread))
    if stdev < 2000 and spread < 2000:
        print("    -> STABLE (tight, no drift) - consistent with genuine hardware lock")
    elif spread > stdev * 3:
        print("    -> DRIFTING - offset changes progressively over the recording, not a stable lock")
    else:
        print("    -> NOISY but not clearly drifting - inconclusive on this window alone")


def run_pass(label, serial_master, serial_slave, duration_s, warmup_s, with_color, start_stagger_s, num_slices):
    print("\n=== {} ({:.1f}s, after {:.1f}s warmup) ===".format(label, duration_s, warmup_s))
    samples_master, samples_slave = measure(
        serial_master, serial_slave, duration_s, warmup_s, with_color, start_stagger_s,
    )
    parity_ok = report_frame_count_parity(samples_master, samples_slave, duration_s)
    report_own_interval_stats("Master IR", samples_master, 1)
    report_own_interval_stats("Slave  IR", samples_slave, 1)
    report_lockstep_offset(samples_master, samples_slave, 1, "IR", num_slices)
    if with_color:
        report_own_interval_stats("Master color", samples_master, 2)
        report_own_interval_stats("Slave  color", samples_slave, 2)
        report_lockstep_offset(samples_master, samples_slave, 2, "Color", num_slices)
    return parity_ok


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--serial-master", default=None)
    parser.add_argument("--serial-slave", default=None)
    parser.add_argument("--master-value", type=int, default=INTER_CAM_SYNC_MASTER)
    parser.add_argument("--slave-value", type=int, default=DEFAULT_SLAVE_VALUE)
    parser.add_argument("--duration-s", type=float, default=20.0)
    parser.add_argument("--warmup-s", type=float, default=2.0)
    parser.add_argument("--start-stagger-s", type=float, default=2.0)
    parser.add_argument("--slices", type=int, default=8)
    parser.add_argument("--with-color", action="store_true", default=False,
                         help="Also stream color on each device, in the same pipeline as IR.")
    args = parser.parse_args()

    serial_master, serial_slave = _resolve_two_serials(args.serial_master, args.serial_slave)
    ctx = rs.context()
    device_master = find_device_by_serial(ctx, serial_master)
    device_slave = find_device_by_serial(ctx, serial_slave)
    print("Master: {} ({})".format(device_master.get_info(rs.camera_info.name), serial_master))
    print("Slave:  {} ({})".format(device_slave.get_info(rs.camera_info.name), serial_slave))

    unsynced_parity = run_pass(
        "UNSYNCED baseline", serial_master, serial_slave,
        args.duration_s, args.warmup_s, args.with_color, args.start_stagger_s, args.slices,
    )

    print("\nApplying genlock roles: master={} (device master) / slave={} (device slave)".format(
        args.master_value, args.slave_value,
    ))
    master_applied = set_inter_cam_sync_mode(device_master, args.master_value)
    slave_applied = set_inter_cam_sync_mode(device_slave, args.slave_value)
    print("  Master role applied: {} - Slave role applied: {}".format(master_applied, slave_applied))
    if not (master_applied and slave_applied):
        print("Aborting - could not apply genlock roles to both devices.")
        set_inter_cam_sync_mode(device_master, INTER_CAM_SYNC_DEFAULT)
        set_inter_cam_sync_mode(device_slave, INTER_CAM_SYNC_DEFAULT)
        return

    try:
        synced_parity = run_pass(
            "SYNCED", serial_master, serial_slave,
            args.duration_s, args.warmup_s, args.with_color, args.start_stagger_s, args.slices,
        )
    finally:
        set_inter_cam_sync_mode(device_master, INTER_CAM_SYNC_DEFAULT)
        set_inter_cam_sync_mode(device_slave, INTER_CAM_SYNC_DEFAULT)
        print("\nGenlock roles reset back to default on both devices.")

    print("\n================ SUMMARY ================")
    print("Unsynced frame-count parity: {}".format("OK" if unsynced_parity else "MISMATCH"))
    print("Synced   frame-count parity: {}".format("OK" if synced_parity else "MISMATCH"))
    if not synced_parity:
        print("\nFrame counts did not match under 'sync' - master and slave are very likely NOT "
              "genuinely locked to the same clock with these values, regardless of the offset "
              "stability lines above. Re-check the physical sync cable, or try a different "
              "--slave-value.")
    elif unsynced_parity:
        print("\nBoth passes show matching frame counts - frame-count parity alone can't "
              "distinguish sync from coincidence here. Judge by the offset STABILITY lines above "
              "instead (STABLE in the synced pass, especially if NOISY/DRIFTING in the unsynced "
              "pass, is the real signal).")
    else:
        print("\nFrame counts matched only in the SYNCED pass, not the unsynced baseline - that "
              "contrast, combined with a STABLE (not drifting) offset above, is a strong positive "
              "signal for genuine hardware lock.")


if __name__ == "__main__":
    main()
