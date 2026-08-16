"""Diagnostic script - NOT part of the shipped app, no automated tests.

Real-hardware finding so far: with master fixed at inter_cam_sync_mode=1,
slave=2 (INTER_CAM_SYNC_SLAVE) gets its IR stream fine but its color stream
NEVER delivers a single frame - confirmed independent of pipeline structure
(diag_simple_master_slave_with_color_test.py, diag_simple_slave_split_
pipelines_test.py). Before concluding RGB simply can't work on a slaved
D455 at all, this sweeps the REST of the sensor's own reported valid range
for rs.option.inter_cam_sync_mode (confirmed min=0 max=4 step=1 - only 1 and
2 have been tried so far, out of 5 possible raw values) as the slave's
value, keeping master fixed at 1. Some D400-series firmware exposes
additional sync modes beyond plain master/slave (e.g. a "genlock" mode that
only aligns exposure START while leaving other sensors free-running) at the
higher values - untested here until now.

For EACH candidate slave value in [2, 3, 4]: applies master=1/slave=value,
starts one combined IR+color pipeline per device (mirrors the real app's
actual stream shape), tries 3 frames from each, records whether IR and
color each delivered ANY frame, then resets both devices to default before
trying the next value (never leaves a stale role behind between attempts,
same reasoning as the app's own MultiCameraSessionController._reset_
genlock_roles). Prints a summary table at the end - which values (if any)
let color through cleanly.

Run from the repo root:
    python tools/genlock_diag/diag_sweep_slave_value_test.py
    python tools/genlock_diag/diag_sweep_slave_value_test.py <master_serial> <slave_serial>
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pyrealsense2 as rs

from engine.streams import (
    list_devices, find_device_by_serial,
    set_inter_cam_sync_mode, INTER_CAM_SYNC_DEFAULT, INTER_CAM_SYNC_MASTER,
)

if len(sys.argv) == 3:
    serial_master, serial_slave = sys.argv[1], sys.argv[2]
else:
    ctx = rs.context()
    devices = list_devices(ctx)
    if len(devices) != 2:
        raise RuntimeError(
            "Need exactly 2 connected devices (found {}), or pass both serials explicitly: "
            "python {} <master_serial> <slave_serial>".format(len(devices), sys.argv[0])
        )
    serial_master, serial_slave = devices[0].serial, devices[1].serial

ctx = rs.context()
device_master = find_device_by_serial(ctx, serial_master)
device_slave = find_device_by_serial(ctx, serial_slave)
print("Master: {} ({})".format(device_master.get_info(rs.camera_info.name), serial_master))
print("Slave:  {} ({})".format(device_slave.get_info(rs.camera_info.name), serial_slave))

CANDIDATE_SLAVE_VALUES = [2, 3, 4]
results = []

for slave_value in CANDIDATE_SLAVE_VALUES:
    print("\n=== Trying slave value {} (master fixed at {}) ===".format(slave_value, INTER_CAM_SYNC_MASTER))
    master_applied = set_inter_cam_sync_mode(device_master, INTER_CAM_SYNC_MASTER)
    slave_applied = set_inter_cam_sync_mode(device_slave, slave_value)
    print("  Master role applied: {} - Slave role applied: {}".format(master_applied, slave_applied))
    if not (master_applied and slave_applied):
        results.append((slave_value, "role not applied", "role not applied"))
        set_inter_cam_sync_mode(device_master, INTER_CAM_SYNC_DEFAULT)
        set_inter_cam_sync_mode(device_slave, INTER_CAM_SYNC_DEFAULT)
        continue

    config_master = rs.config()
    config_master.enable_device(serial_master)
    config_master.enable_stream(rs.stream.infrared, 1, 1280, 720, rs.format.y8, 30)
    config_master.enable_stream(rs.stream.color, 0, 1280, 720, rs.format.bgr8, 30)

    config_slave = rs.config()
    config_slave.enable_device(serial_slave)
    config_slave.enable_stream(rs.stream.infrared, 1, 1280, 720, rs.format.y8, 30)
    config_slave.enable_stream(rs.stream.color, 0, 1280, 720, rs.format.bgr8, 30)

    pipeline_master = rs.pipeline()
    pipeline_slave = rs.pipeline()
    master_started = False
    slave_started = False
    slave_ir_ok = False
    slave_color_ok = False

    try:
        pipeline_master.start(config_master)
        master_started = True
        pipeline_slave.start(config_slave)
        slave_started = True

        for i in range(3):
            try:
                pipeline_master.wait_for_frames(5000)
            except Exception as exc:
                print("  Master frame {}: FAILED - {}".format(i, exc))
            try:
                frames = pipeline_slave.wait_for_frames(5000)
                has_ir = frames.get_infrared_frame(1) is not None
                has_color = frames.get_color_frame() is not None
                slave_ir_ok = slave_ir_ok or has_ir
                slave_color_ok = slave_color_ok or has_color
                print("  Slave frameset {}: OK (has_ir={} has_color={})".format(i, has_ir, has_color))
            except Exception as exc:
                print("  Slave frameset {}: FAILED - {}".format(i, exc))
    finally:
        if master_started:
            pipeline_master.stop()
        if slave_started:
            pipeline_slave.stop()
        set_inter_cam_sync_mode(device_master, INTER_CAM_SYNC_DEFAULT)
        set_inter_cam_sync_mode(device_slave, INTER_CAM_SYNC_DEFAULT)

    results.append((slave_value, "IR ok" if slave_ir_ok else "IR FAILED", "color ok" if slave_color_ok else "color FAILED"))

print("\n================ SUMMARY ================")
print("Master fixed at inter_cam_sync_mode={}".format(INTER_CAM_SYNC_MASTER))
for slave_value, ir_status, color_status in results:
    print("  slave={}: {} / {}".format(slave_value, ir_status, color_status))
print("\nGenlock roles reset back to default on both devices after each attempt.")
