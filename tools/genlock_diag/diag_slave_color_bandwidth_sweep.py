"""Diagnostic script - NOT part of the shipped app, no automated tests.

Tests a real, not-yet-ruled-out hypothesis: every color-stream test earlier
in this investigation (diag_simple_master_slave_with_color_test.py,
diag_simple_slave_split_pipelines_test.py) used the full 1280x720@30 color
profile (~83MB/s) - if the slave's color sensor failing to deliver frames
is actually a BANDWIDTH problem (not a hard trigger-domain block), a lower-
resolution/fps color stream might work fine even combined with IR in one
pipeline, in the SAME shape the real app's intra-camera IR-vs-RGB test
actually needs.

For each (width, height, fps) candidate in COLOR_CANDIDATES: applies
master=1/slave=2 (the confirmed-working values), builds ONE combined
IR+color pipeline per device (IR fixed at 1280x720@30 - already proven to
work as slave on its own), tries grabbing several frames from each, records
whether master and slave each got IR and color frames, then resets both
devices to default before trying the next candidate.

Run from the repo root:
    python tools/genlock_diag/diag_slave_color_bandwidth_sweep.py
    python tools/genlock_diag/diag_slave_color_bandwidth_sweep.py --serial-master 123 --serial-slave 456
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pyrealsense2 as rs

from engine.streams import (
    list_devices, find_device_by_serial,
    set_inter_cam_sync_mode, INTER_CAM_SYNC_DEFAULT, INTER_CAM_SYNC_MASTER, INTER_CAM_SYNC_SLAVE,
)

# From full resolution down to something much lighter - if a lower point in
# this list works, that's a real bandwidth ceiling, not a hard block; if
# NONE work, that confirms the hard-block finding from earlier this
# investigation regardless of resolution.
COLOR_CANDIDATES = [
    (1280, 720, 30),
    (848, 480, 30),
    (640, 480, 30),
    (640, 480, 15),
    (424, 240, 30),
    (424, 240, 15),
    (424, 240, 6),
]

IR_WIDTH, IR_HEIGHT, IR_FPS = 1280, 720, 30  # already confirmed to work as slave on its own


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


def _build_config(serial, color_width, color_height, color_fps):
    config = rs.config()
    config.enable_device(serial)
    config.enable_stream(rs.stream.infrared, 1, IR_WIDTH, IR_HEIGHT, rs.format.y8, IR_FPS)
    config.enable_stream(rs.stream.color, 0, color_width, color_height, rs.format.bgr8, color_fps)
    return config


def _try_get_frames(pipeline, attempts, timeout_ms=5000):
    """Returns (got_ir, got_color) - True if ANY of `attempts` calls to
    wait_for_frames() produced that stream's frame at least once."""
    got_ir, got_color = False, False
    for _ in range(attempts):
        try:
            frameset = pipeline.wait_for_frames(timeout_ms)
        except Exception:
            continue
        if frameset.get_infrared_frame(1) is not None:
            got_ir = True
        if frameset.get_color_frame() is not None:
            got_color = True
    return got_ir, got_color


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--serial-master", default=None)
    parser.add_argument("--serial-slave", default=None)
    parser.add_argument("--attempts", type=int, default=5, help="Frame-grab attempts per candidate.")
    args = parser.parse_args()

    serial_master, serial_slave = _resolve_two_serials(args.serial_master, args.serial_slave)
    ctx = rs.context()
    device_master = find_device_by_serial(ctx, serial_master)
    device_slave = find_device_by_serial(ctx, serial_slave)
    print("Master: {} ({})".format(device_master.get_info(rs.camera_info.name), serial_master))
    print("Slave:  {} ({})".format(device_slave.get_info(rs.camera_info.name), serial_slave))

    results = []
    for color_width, color_height, color_fps in COLOR_CANDIDATES:
        label = "color {}x{}@{}".format(color_width, color_height, color_fps)
        print("\n=== Trying {} (IR fixed at {}x{}@{}) ===".format(label, IR_WIDTH, IR_HEIGHT, IR_FPS))

        master_applied = set_inter_cam_sync_mode(device_master, INTER_CAM_SYNC_MASTER)
        slave_applied = set_inter_cam_sync_mode(device_slave, INTER_CAM_SYNC_SLAVE)
        if not (master_applied and slave_applied):
            print("  Could not apply genlock roles - skipping this candidate.")
            results.append((label, "role not applied", "role not applied"))
            set_inter_cam_sync_mode(device_master, INTER_CAM_SYNC_DEFAULT)
            set_inter_cam_sync_mode(device_slave, INTER_CAM_SYNC_DEFAULT)
            continue

        pipeline_master = rs.pipeline()
        pipeline_slave = rs.pipeline()
        master_started = False
        slave_started = False
        try:
            try:
                pipeline_master.start(_build_config(serial_master, color_width, color_height, color_fps))
                master_started = True
            except Exception as exc:
                print("  Master pipeline failed to start: {}".format(exc))
            try:
                pipeline_slave.start(_build_config(serial_slave, color_width, color_height, color_fps))
                slave_started = True
            except Exception as exc:
                print("  Slave pipeline failed to start: {}".format(exc))

            master_ir = master_color = slave_ir = slave_color = False
            if master_started:
                master_ir, master_color = _try_get_frames(pipeline_master, args.attempts)
            if slave_started:
                slave_ir, slave_color = _try_get_frames(pipeline_slave, args.attempts)

            print("  Master: ir={} color={}".format(master_ir, master_color))
            print("  Slave:  ir={} color={}".format(slave_ir, slave_color))
            results.append((
                label,
                "ir={} color={}".format(master_ir, master_color),
                "ir={} color={}".format(slave_ir, slave_color),
            ))
        finally:
            if master_started:
                pipeline_master.stop()
            if slave_started:
                pipeline_slave.stop()
            set_inter_cam_sync_mode(device_master, INTER_CAM_SYNC_DEFAULT)
            set_inter_cam_sync_mode(device_slave, INTER_CAM_SYNC_DEFAULT)

    print("\n================ SUMMARY ================")
    print("Master fixed at inter_cam_sync_mode={}, slave at {}".format(
        INTER_CAM_SYNC_MASTER, INTER_CAM_SYNC_SLAVE,
    ))
    for label, master_result, slave_result in results:
        print("  {}: master[{}]  slave[{}]".format(label, master_result, slave_result))
    print("\nGenlock roles reset back to default on both devices after each candidate.")


if __name__ == "__main__":
    main()
