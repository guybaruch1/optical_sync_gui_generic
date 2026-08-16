"""Diagnostic script - NOT part of the shipped app, no automated tests.

The simplest possible genlock test: no threads, no bandwidth measurement, no
offset computation - just "does the slave ever receive a single frame at
all". One IR stream per device, nothing else. Reuses engine.streams'
find_device_by_serial/set_inter_cam_sync_mode/INTER_CAM_SYNC_* - no hardware
logic reimplemented.

Run from the repo root:
    python tools/genlock_diag/diag_simple_master_slave_test.py
    python tools/genlock_diag/diag_simple_master_slave_test.py <master_serial> <slave_serial>

With no arguments, auto-picks the 2 connected devices in whatever order
list_devices() returns them - pass the two serials explicitly (in either
order) to control which one becomes master. Always resets both devices'
genlock role back to default before exiting, even on failure.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pyrealsense2 as rs

from engine.streams import (
    list_devices, find_device_by_serial,
    set_inter_cam_sync_mode, INTER_CAM_SYNC_DEFAULT, INTER_CAM_SYNC_MASTER, INTER_CAM_SYNC_SLAVE,
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

print("\nApplying genlock roles...")
print("  Master role applied: {}".format(set_inter_cam_sync_mode(device_master, INTER_CAM_SYNC_MASTER)))
print("  Slave role applied:  {}".format(set_inter_cam_sync_mode(device_slave, INTER_CAM_SYNC_SLAVE)))

config_master = rs.config()
config_master.enable_device(serial_master)
config_master.enable_stream(rs.stream.infrared, 1, 1280, 720, rs.format.y8, 30)

config_slave = rs.config()
config_slave.enable_device(serial_slave)
config_slave.enable_stream(rs.stream.infrared, 1, 1280, 720, rs.format.y8, 30)

pipeline_master = rs.pipeline()
pipeline_slave = rs.pipeline()
master_started = False
slave_started = False

try:
    print("\nStarting master pipeline...")
    pipeline_master.start(config_master)
    master_started = True
    print("Starting slave pipeline...")
    pipeline_slave.start(config_slave)
    slave_started = True

    print("\nWaiting for 5 frames from each (5s timeout per attempt)...")
    for i in range(5):
        try:
            frames = pipeline_master.wait_for_frames(5000)
            print("  Master frame {}: OK (frame number {})".format(i, frames.get_frame_number()))
        except Exception as exc:
            print("  Master frame {}: FAILED - {}".format(i, exc))
        try:
            frames = pipeline_slave.wait_for_frames(5000)
            print("  Slave  frame {}: OK (frame number {})".format(i, frames.get_frame_number()))
        except Exception as exc:
            print("  Slave  frame {}: FAILED - {}".format(i, exc))
finally:
    print("\nCleaning up...")
    if master_started:
        pipeline_master.stop()
    if slave_started:
        pipeline_slave.stop()
    set_inter_cam_sync_mode(device_master, INTER_CAM_SYNC_DEFAULT)
    set_inter_cam_sync_mode(device_slave, INTER_CAM_SYNC_DEFAULT)
    print("Genlock roles reset back to default on both devices.")
