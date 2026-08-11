"""Diagnostic script - NOT part of the shipped app, no automated tests.

Software-only alternative to unplugging the camera when a sensor is stuck
with a bad manual exposure/gain (see CLAUDE.md's "Camera controls
(emitter/exposure)" section). Restores exposure AND gain to their
SDK-reported factory defaults, then re-enables auto-exposure - exactly what
a physical power-cycle/hardware_reset does for exposure/gain specifically,
without dropping the device off USB or requiring a several-second
re-enumeration wait.

Note this script does its own EXPLICIT gain reset (direct set_option, not
via engine.streams.enable_auto_exposure) - the app's own normal runtime no
longer touches gain at all (set_manual_exposure was narrowed to
exposure-only after that restore-on-switch-back-to-auto approach proved
unreliable on real hardware and could leave gain stuck regardless). This
script still resets gain directly because its whole purpose is a one-off
manual recovery for a sensor that's ALREADY stuck - including one left over
from before that narrowing shipped - not something the app itself should
ever need to do again going forward.

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
        restored_exposure = enable_auto_exposure(sensor)
        # Explicit, independent of enable_auto_exposure - the app's own
        # runtime code deliberately never touches gain anymore (see
        # engine.streams.set_manual_exposure's docstring), but this script's
        # whole job is clearing out whatever a real sensor is CURRENTLY
        # stuck with, including gain left over from before that changed.
        restored_gain = False
        if sensor.supports(rs.option.gain):
            sensor.set_option(rs.option.gain, sensor.get_option_range(rs.option.gain).default)
            restored_gain = True

        if restored_exposure or restored_gain:
            any_restored = True
            restored_what = " and ".join(
                filter(None, ["exposure" if restored_exposure else None, "gain" if restored_gain else None])
            )
            print("  [{}] {}: restored {} to factory defaults, auto-exposure ON".format(
                sensor_index, sensor_name, restored_what
            ))
        else:
            print("  [{}] {}: no auto-exposure/gain option - skipped".format(sensor_index, sensor_name))

    if not any_restored:
        print("\nNo sensor on this device supports auto-exposure - nothing to reset.")
    else:
        print("\nDone. Re-run Calibration/ROI Select with Auto exposure selected in Stream Config.")


if __name__ == "__main__":
    main()
