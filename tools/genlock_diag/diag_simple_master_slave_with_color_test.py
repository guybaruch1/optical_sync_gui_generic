"""Diagnostic script - NOT part of the shipped app, no automated tests.

Same bare-bones genlock test as diag_simple_master_slave_test.py (which
confirmed IR-only genlock works: master and slave both got all 5 frames),
with exactly ONE variable changed: each device's pipeline now also enables
its color stream, matching what this app's real IR-vs-RGB pairing actually
needs. Isolates whether adding color to the SAME per-device pipeline as IR
is what breaks frame delivery under genlock - a real possibility since the
color sensor doesn't support inter_cam_sync_mode at all (confirmed earlier:
only the Stereo Module does), so once IR's timing is externally triggered
while color keeps free-running on its own clock, one rs.pipeline() trying
to compose both into a single synchronized frameset may never produce one.

Run from the repo root:
    python tools/genlock_diag/diag_simple_master_slave_with_color_test.py
    python tools/genlock_diag/diag_simple_master_slave_with_color_test.py <master_serial> <slave_serial>
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
config_master.enable_stream(rs.stream.color, 0, 1280, 720, rs.format.bgr8, 30)

config_slave = rs.config()
config_slave.enable_device(serial_slave)
config_slave.enable_stream(rs.stream.infrared, 1, 1280, 720, rs.format.y8, 30)
config_slave.enable_stream(rs.stream.color, 0, 1280, 720, rs.format.bgr8, 30)

pipeline_master = rs.pipeline()
pipeline_slave = rs.pipeline()
master_started = False
slave_started = False

try:
    print("\nStarting master pipeline (IR + color)...")
    pipeline_master.start(config_master)
    master_started = True
    print("Starting slave pipeline (IR + color)...")
    pipeline_slave.start(config_slave)
    slave_started = True

    print("\nWaiting for 5 composite framesets from each (5s timeout per attempt)...")
    for i in range(5):
        try:
            frames = pipeline_master.wait_for_frames(5000)
            has_ir = frames.get_infrared_frame(1) is not None
            has_color = frames.get_color_frame() is not None
            print("  Master frameset {}: OK (has_ir={} has_color={})".format(i, has_ir, has_color))
        except Exception as exc:
            print("  Master frameset {}: FAILED - {}".format(i, exc))
        try:
            frames = pipeline_slave.wait_for_frames(5000)
            has_ir = frames.get_infrared_frame(1) is not None
            has_color = frames.get_color_frame() is not None
            print("  Slave  frameset {}: OK (has_ir={} has_color={})".format(i, has_ir, has_color))
        except Exception as exc:
            print("  Slave  frameset {}: FAILED - {}".format(i, exc))
finally:
    print("\nCleaning up...")
    if master_started:
        pipeline_master.stop()
    if slave_started:
        pipeline_slave.stop()
    set_inter_cam_sync_mode(device_master, INTER_CAM_SYNC_DEFAULT)
    set_inter_cam_sync_mode(device_slave, INTER_CAM_SYNC_DEFAULT)
    print("Genlock roles reset back to default on both devices.")
