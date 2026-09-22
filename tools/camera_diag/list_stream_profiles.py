"""Diagnostic script - NOT part of the shipped app, no automated tests.

Lists every video stream profile a connected RealSense device's sensors
actually report - the same raw data engine.streams.list_video_stream_
options_from_device works from - so you can compare it by eye against a
settings.yaml camera.stream_options test's sensor_options entries and see
exactly why a named test (e.g. "IR vs RGB sync") isn't showing up in Stream
Config's dropdown: resolve_camera_tests silently drops any test with zero
sensor_options entries matching what THIS specific connected device/
firmware reports (see that function's docstring in engine/streams.py), so
a device-name match with the wrong resolution/fps/format list produces no
error at all - just a missing entry in the picker.

Run from the repo root:
    python tools/camera_diag/list_stream_profiles.py
    python tools/camera_diag/list_stream_profiles.py --serial 123456789012
"""
import argparse
import sys

import pyrealsense2 as rs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", help="Device serial number (default: first device found)")
    args = parser.parse_args()

    ctx = rs.context()
    devices = ctx.query_devices()
    if len(devices) == 0:
        print("No RealSense device connected.")
        sys.exit(1)

    device = None
    for d in devices:
        if args.serial is None or d.get_info(rs.camera_info.serial_number) == args.serial:
            device = d
            break
    if device is None:
        print("No device found with serial {!r}.".format(args.serial))
        sys.exit(1)

    name = device.get_info(rs.camera_info.name)
    serial = device.get_info(rs.camera_info.serial_number)
    firmware = device.get_info(rs.camera_info.firmware_version) if device.supports(rs.camera_info.firmware_version) else "<unknown>"
    print("Device: {!r}  (serial {})".format(name, serial))
    print("Firmware version: {}".format(firmware))
    print("(this exact name string is the key settings.yaml's camera.stream_options must match)")
    print("(a firmware update can change which stream profiles a sensor advertises below - if")
    print(" settings.yaml's sensor_options used to match and now don't, compare this version")
    print(" against whatever firmware was on the device when those entries were written)")
    print()

    for sensor in device.query_sensors():
        sensor_name = sensor.get_info(rs.camera_info.name) if sensor.supports(rs.camera_info.name) else "<unnamed sensor>"
        print("Sensor: {}".format(sensor_name))
        seen = set()
        for profile in sensor.get_stream_profiles():
            if not profile.is_video_stream_profile():
                continue
            video = profile.as_video_stream_profile()
            key = (
                str(profile.stream_type()).replace("stream.", ""),
                profile.stream_index(),
                video.width(),
                video.height(),
                profile.fps(),
                str(profile.format()).replace("format.", ""),
            )
            seen.add(key)
        for stream_type, stream_index, width, height, fps, fmt in sorted(seen):
            print("    stream_type={:<10} stream_index={}  {}x{} @ {}fps  format={}".format(
                stream_type, stream_index, width, height, fps, fmt
            ))
        if not seen:
            print("    (no video stream profiles)")
        print()


if __name__ == "__main__":
    main()
