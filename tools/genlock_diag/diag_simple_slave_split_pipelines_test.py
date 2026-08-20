"""Diagnostic script - NOT part of the shipped app, no automated tests.

One more isolation on top of the last two findings:
- diag_simple_master_slave_test.py: IR-only, ONE pipeline per device - works,
  master and slave both get all 5 frames.
- diag_simple_master_slave_with_color_test.py: IR+color combined into ONE
  pipeline per device - master still works, slave gets ZERO framesets, every
  attempt, identically.

Open question this isolates: is the problem specifically composing IR+color
into a SINGLE pipeline's synchronized frameset on the slave, or does color
never arrive on a slaved device AT ALL regardless of pipeline structure?
Master keeps its already-proven-working single combined IR+color pipeline
unchanged; the SLAVE now gets TWO SEPARATE pipelines instead - one IR-only,
one color-only - so nothing ever asks the slave device to compose the two
into one synchronized frameset. If the slave's color pipeline now delivers
frames, the fix is architectural (split pipelines for a slaved camera). If
it still times out, color genuinely can't be delivered from a slaved device
at all, independent of pipeline structure.

Run from the repo root:
    python tools/genlock_diag/diag_simple_slave_split_pipelines_test.py
    python tools/genlock_diag/diag_simple_slave_split_pipelines_test.py <master_serial> <slave_serial>
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

# Master: ONE combined pipeline (IR + color) - already confirmed working.
config_master = rs.config()
config_master.enable_device(serial_master)
config_master.enable_stream(rs.stream.infrared, 1, 1280, 720, rs.format.y8, 30)
config_master.enable_stream(rs.stream.color, 0, 1280, 720, rs.format.bgr8, 30)
pipeline_master = rs.pipeline()

# Slave: TWO SEPARATE pipelines - one IR-only, one color-only.
config_slave_ir = rs.config()
config_slave_ir.enable_device(serial_slave)
config_slave_ir.enable_stream(rs.stream.infrared, 1, 1280, 720, rs.format.y8, 30)
pipeline_slave_ir = rs.pipeline()

config_slave_color = rs.config()
config_slave_color.enable_device(serial_slave)
config_slave_color.enable_stream(rs.stream.color, 0, 1280, 720, rs.format.bgr8, 30)
pipeline_slave_color = rs.pipeline()

master_started = False
slave_ir_started = False
slave_color_started = False

try:
    print("\nStarting master pipeline (IR + color, combined)...")
    pipeline_master.start(config_master)
    master_started = True
    print("Starting slave IR-only pipeline...")
    pipeline_slave_ir.start(config_slave_ir)
    slave_ir_started = True
    print("Starting slave color-only pipeline...")
    pipeline_slave_color.start(config_slave_color)
    slave_color_started = True

    print("\nWaiting for 5 frames from each (5s timeout per attempt)...")
    for i in range(5):
        try:
            frames = pipeline_master.wait_for_frames(5000)
            print("  Master frameset {}: OK (has_ir={} has_color={})".format(
                i, frames.get_infrared_frame(1) is not None, frames.get_color_frame() is not None,
            ))
        except Exception as exc:
            print("  Master frameset {}: FAILED - {}".format(i, exc))
        try:
            frames = pipeline_slave_ir.wait_for_frames(5000)
            print("  Slave IR    frame {}: OK (frame number {})".format(i, frames.get_frame_number()))
        except Exception as exc:
            print("  Slave IR    frame {}: FAILED - {}".format(i, exc))
        try:
            frames = pipeline_slave_color.wait_for_frames(5000)
            print("  Slave color frame {}: OK (frame number {})".format(i, frames.get_frame_number()))
        except Exception as exc:
            print("  Slave color frame {}: FAILED - {}".format(i, exc))
finally:
    print("\nCleaning up...")
    if master_started:
        pipeline_master.stop()
    if slave_ir_started:
        pipeline_slave_ir.stop()
    if slave_color_started:
        pipeline_slave_color.stop()
    set_inter_cam_sync_mode(device_master, INTER_CAM_SYNC_DEFAULT)
    set_inter_cam_sync_mode(device_slave, INTER_CAM_SYNC_DEFAULT)
    print("Genlock roles reset back to default on both devices.")
