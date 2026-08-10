"""Diagnostic script - NOT part of the shipped app, no automated tests.

Software-only alternative to unplugging the camera when a sensor is stuck
with a bad manual exposure/gain from BEFORE engine.streams.enable_auto_exposure
was fixed to restore both to factory defaults on switch-back to Auto (see
CLAUDE.md's "Camera controls (emitter/exposure)" section). That fix only
takes effect the NEXT time something calls enable_auto_exposure on the
sensor - which happens automatically once you re-run ROI Select/Calibration/
Threshold Tuning/Live Session with "Auto exposure" selected in Stream
Config - but if you just want to clear a stuck sensor RIGHT NOW without
navigating the wizard, this calls it directly.

This does exactly what a physical power-cycle/hardware_reset does for
exposure/gain specifically (restores the SDK-reported factory default),
without dropping the device off USB or requiring a several-second
re-enumeration wait.

Run from the repo root:
    python tools/camera_diag/reset_camera_exposure.py
    python tools/camera_diag/reset_camera_exposure.py --serial 123456789012

With no --serial, defaults to gui_state.json's last-used device (whatever
Device Select last connected to); falls back to the first device found if
gui_state.json has none or doesn't match anything currently connected.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pyrealsense2 as rs

from engine.streams import list_devices, find_device_by_serial, enable_auto_exposure
from state.gui_state import load_gui_state


def _resolve_serial(ctx, requested_serial):
    devices = list_devices(ctx)
    if not devices:
        raise RuntimeError("No RealSense devices connected.")

    if requested_serial is not None:
        if not any(d.serial == requested_serial for d in devices):
            raise RuntimeError(
                "No connected device with serial {!r}. Connected: {}".format(
                    requested_serial, ", ".join("{} ({})".format(d.name, d.serial) for d in devices)
                )
            )
        return requested_serial

    last_used = load_gui_state().device_serial
    if last_used is not None and any(d.serial == last_used for d in devices):
        print("Using gui_state.json's last-used device: {}".format(last_used))
        return last_used

    if len(devices) > 1:
        print("Multiple devices connected and no --serial given/matched - using the first one:")
        for d in devices:
            print("  {} ({})".format(d.name, d.serial))
    return devices[0].serial


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--serial", default=None, help="Device serial number. Defaults to gui_state.json's last-used device, or the first device found.")
    args = parser.parse_args()

    ctx = rs.context()
    serial = _resolve_serial(ctx, args.serial)
    device = find_device_by_serial(ctx, serial)
    device_name = device.get_info(rs.camera_info.name)
    print("Device: {} ({})".format(device_name, serial))

    any_restored = False
    for sensor_index, sensor in enumerate(device.query_sensors()):
        sensor_name = sensor.get_info(rs.camera_info.name) if sensor.supports(rs.camera_info.name) else "sensor {}".format(sensor_index)
        if enable_auto_exposure(sensor):
            any_restored = True
            print("  [{}] {}: restored exposure/gain to factory defaults, auto-exposure ON".format(sensor_index, sensor_name))
        else:
            print("  [{}] {}: no auto-exposure option - skipped".format(sensor_index, sensor_name))

    if not any_restored:
        print("\nNo sensor on this device supports auto-exposure - nothing to reset.")
    else:
        print("\nDone. Re-run Calibration/ROI Select with Auto exposure selected in Stream Config.")


if __name__ == "__main__":
    main()
