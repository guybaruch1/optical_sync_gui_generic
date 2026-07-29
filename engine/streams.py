"""Hardware-facing RealSense device/sensor helpers.

Ported from optical_sync_poc_/realsense_utils.py's pyrealsense2-dependent
half (the pure-numpy half lives in domain/realsense_utils.py instead),
plus new device-listing and continuous-capture pieces the GUI needs that
the original one-shot scripts didn't: find_camera_sensors only ever
returned the FIRST matching device, and none of the original scripts
streamed continuously - they all captured one settled frame (calibration,
ROI picker) or ran a fixed-duration batch loop (pipeline_sync_test_diff).
The GUI's live preview and live session both need an open-ended stream,
hence ContinuousCapture.
"""

import time
from dataclasses import dataclass

import numpy as np
import pyrealsense2 as rs


@dataclass
class DeviceInfo:
    name: str
    serial: str


def list_devices(ctx):
    devices = []
    for d in ctx.query_devices():
        devices.append(DeviceInfo(
            name=d.get_info(rs.camera_info.name),
            serial=d.get_info(rs.camera_info.serial_number),
        ))
    return devices


def find_device_by_serial(ctx, serial):
    for d in ctx.query_devices():
        if d.get_info(rs.camera_info.serial_number) == serial:
            return d
    raise RuntimeError("No connected device with serial {!r}".format(serial))


def list_video_stream_options_from_device(device):
    """List every infrared/color video-stream profile a device offers, as
    plain dicts (sensor_index/stream_type/stream_index/format/width/height/
    fps) - the picker data the Stream Config page needs to let the operator
    choose ANY two streams instead of a hardcoded IR+RGB pair. Split out from
    list_video_stream_options so it's directly testable against a fake
    device without needing a fake rs.context too."""
    options = []
    for sensor_index, sensor in enumerate(device.query_sensors()):
        for p in sensor.profiles:
            if not p.is_video_stream_profile():
                continue
            if p.stream_type() not in (rs.stream.infrared, rs.stream.color):
                continue
            vp = p.as_video_stream_profile()
            options.append({
                "sensor_index": sensor_index,
                "stream_type": p.stream_type(),
                "stream_index": p.stream_index(),
                "format": p.format(),
                "width": vp.width(),
                "height": vp.height(),
                "fps": p.fps(),
            })
    return options


def list_video_stream_options(ctx, serial):
    device = find_device_by_serial(ctx, serial)
    return list_video_stream_options_from_device(device)


def _pick_matches(profile, pick):
    if profile.stream_type() != pick["stream_type"] or profile.stream_index() != pick["stream_index"]:
        return False
    if profile.format() != pick["format"] or profile.fps() != pick["fps"]:
        return False
    vp = profile.as_video_stream_profile()
    return vp.width() == pick["width"] and vp.height() == pick["height"]


def resolve_and_group(device, pick_a, pick_b):
    """Group two picked stream profiles by which physical sensor object they
    live on. This is the key insight that unifies two different camera
    topologies: some devices have IR and RGB on two separate sensor objects
    (two groups, one profile each), others have two color streams sharing
    ONE sensor object at different stream indices (one group, two profiles) -
    which matters because sensor.open()/.start() must be called once per
    distinct sensor object, with all of that sensor's wanted profiles passed
    together, not once per stream."""
    sensors = list(device.query_sensors())

    def sensor_and_profile_for(pick):
        sensor = sensors[pick["sensor_index"]]
        profile = next(p for p in sensor.profiles if _pick_matches(p, pick))
        return sensor, profile

    sensor_a, profile_a = sensor_and_profile_for(pick_a)
    sensor_b, profile_b = sensor_and_profile_for(pick_b)

    if sensor_a is sensor_b:
        return [(sensor_a, [profile_a, profile_b])]
    return [(sensor_a, [profile_a]), (sensor_b, [profile_b])]


def get_sensors_for_device(ctx, serial):
    for d in ctx.query_devices():
        if d.get_info(rs.camera_info.serial_number) != serial:
            continue
        sensors = d.query_sensors()
        stereo = next(s for s in sensors if s.get_info(rs.camera_info.name) == "Stereo Module")
        rgb = next(s for s in sensors if s.get_info(rs.camera_info.name) == "RGB Camera")
        return stereo, rgb
    raise RuntimeError("No connected device with serial {!r}".format(serial))


def list_supported_profiles(sensor, stream_type, fmt, stream_index):
    results = set()
    for p in sensor.profiles:
        if p.stream_type() != stream_type or p.format() != fmt:
            continue
        if p.stream_index() != stream_index:
            continue
        vp = p.as_video_stream_profile()
        results.add((vp.width(), vp.height(), p.fps()))
    return sorted(results)


def match_profile(sensor, stream_type, fmt, width, height, fps, stream_index):
    for p in sensor.profiles:
        vp = p.as_video_stream_profile()
        if (
            p.stream_type() == stream_type
            and p.format() == fmt
            and vp.width() == width
            and vp.height() == height
            and p.fps() == fps
            and p.stream_index() == stream_index
        ):
            return p
    raise RuntimeError(
        "No matching profile for {} {}x{}@{}fps ({})".format(stream_type, width, height, fps, fmt)
    )


def capture_synced_frame_pair(groups, on_both_streaming=None, settle_frames=15, timeout_s=10.0):
    """
    Generalized from the original two-sensor (stereo IR + RGB) version -
    ported verbatim from optical_sync_poc_/realsense_utils.py - to an
    arbitrary list of (sensor, profiles) `groups`, as produced by
    resolve_and_group. This is the same proven capture mechanism
    led_calibration.py/roi_picker.py actually used (NOT the rs.pipeline()-
    based ContinuousCapture below), now covering any two picked streams:
    two distinct sensors (e.g. IR + RGB), or one shared sensor exposing two
    stream indices (e.g. two color picks, or two infrared picks). State is
    keyed by (stream_type, stream_index) instead of stream_type() alone,
    since two picks can share a stream_type.

    Open + start every sensor concurrently (matches the pattern used in the
    working IMU test script - open everything, then start everything, rather
    than sequential open/close per sensor, which can stall on some devices).

    Flow:
      1. Open every sensor's wanted profiles.
      2. Start every sensor with a shared callback tracking counts/latest
         frame per (stream_type, stream_index).
      3. Wait until every stream is confirmed actually streaming (a few
         frames each).
      4. Call on_both_streaming() if given (e.g. turn the LED panel on now).
      5. Reset counters and wait for `settle_frames` fresh frames per stream
         (so the captured frame reflects state AFTER on_both_streaming ran).
      6. Stop + close every sensor.

    Returns {(stream_type, stream_index): bytes} - raw frame bytes (safe
    regardless of pixel format/size; caller converts with
    domain.realsense_utils per stream's actual format).

    Note: the expected number of distinct streams is derived from
    len(profiles) per group, not by calling stream_type()/stream_index() on
    the profile objects passed in - those are only ever handed to
    sensor.open() here. The actual (stream_type, stream_index) keys are
    discovered from the frames the callback receives, same as the original
    two-sensor version discovered rs.stream.infrared/rs.stream.color from
    incoming frames rather than from the profiles it opened with.
    """
    state = {}
    expected_stream_count = sum(len(profiles) for _, profiles in groups)

    def callback(frame):
        key = (frame.get_profile().stream_type(), frame.get_profile().stream_index())
        s = state.setdefault(key, {"count": 0, "frame": None})
        s["count"] += 1
        s["frame"] = bytes(frame.get_data())  # raw bytes - safe regardless of pixel format/size

    for sensor, profiles in groups:
        sensor.open(profiles)
    try:
        for sensor, _ in groups:
            sensor.start(callback)

        def wait_until(predicate, label):
            start = time.time()
            while not predicate():
                if time.time() - start > timeout_s:
                    raise RuntimeError(
                        "Timed out ({}) - {} of {} expected streams delivering frames".format(
                            label, len(state), expected_stream_count,
                        )
                    )
                time.sleep(0.05)

        # step 3: confirm every stream is actually streaming before doing anything else
        wait_until(
            lambda: len(state) >= expected_stream_count and all(s["count"] >= 1 for s in state.values()),
            "waiting for initial frames",
        )

        # step 4: trigger whatever should happen now that everything is live (e.g. LEDs on)
        if on_both_streaming is not None:
            on_both_streaming()

        # step 5: reset counters so we only accept frames captured AFTER the trigger
        for s in state.values():
            s["count"] = 0
        wait_until(
            lambda: all(s["count"] >= settle_frames for s in state.values()),
            "waiting for post-trigger settled frames",
        )

        return {key: s["frame"] for key, s in state.items()}
    finally:
        for sensor, _ in groups:
            sensor.stop()
            sensor.close()


def disable_ir_emitter(stereo_sensor):
    if stereo_sensor.supports(rs.option.emitter_enabled):
        stereo_sensor.set_option(rs.option.emitter_enabled, 0)
        return True
    return False


def enable_auto_exposure(sensor):
    """Returns True/False like disable_ir_emitter, so callers can warn the
    operator the same way when the sensor doesn't support the option instead
    of silently proceeding with auto-exposure left however it was."""
    if sensor.supports(rs.option.enable_auto_exposure):
        sensor.set_option(rs.option.enable_auto_exposure, 1)
        return True
    return False


class ContinuousCapture:
    """Open-ended IR+RGB capture via rs.pipeline(), same mechanism as
    optical_sync_poc_/pipeline_sync_test_diff.py's run_pipeline_capture,
    restructured as start/frames()/stop() so it can back both the live
    ROI-selection preview and the live sync-test session."""

    def __init__(self, ir_resolution, ir_fps, color_resolution, color_fps):
        self.ir_resolution = ir_resolution
        self.ir_fps = ir_fps
        self.color_resolution = color_resolution
        self.color_fps = color_fps
        self._pipeline = None

    def start(self):
        config = rs.config()
        config.enable_stream(rs.stream.infrared, 1, *self.ir_resolution, rs.format.y8, self.ir_fps)
        config.enable_stream(rs.stream.color, *self.color_resolution, rs.format.yuyv, self.color_fps)
        self._pipeline = rs.pipeline()
        self._pipeline.start(config)

    def frames(self):
        for ir_image, rgb_image, ir_ts_us, rgb_ts_us, _, _ in self.frames_with_diagnostics():
            yield ir_image, rgb_image, ir_ts_us, rgb_ts_us

    def frames_with_diagnostics(self):
        """Like frames(), but also yields each stream's own HW frame-number
        counter (frame.get_frame_number()) - not needed by the metrics
        pipeline frames() serves, but useful for the Stream Config page's
        live pairing-quality preview, which shows these numbers directly to
        the operator to sanity-check pairing before committing to a
        resolution/fps."""
        from domain.realsense_utils import ir_bytes_to_image, yuyv_to_bgr

        while True:
            frameset = self._pipeline.wait_for_frames()
            ir_frame = frameset.get_infrared_frame()
            color_frame = frameset.get_color_frame()
            if not ir_frame or not color_frame:
                continue

            metadata = rs.frame_metadata_value.frame_timestamp
            if not (ir_frame.supports_frame_metadata(metadata) and color_frame.supports_frame_metadata(metadata)):
                raise RuntimeError(
                    "This camera/driver does not expose per-frame HW timestamp metadata "
                    "(frame_metadata_value.frame_timestamp), which the sync metrics require. "
                    "On Windows, RealSense per-frame metadata is often disabled by default at "
                    "the OS/driver level and needs a one-time enablement step (see Intel's "
                    "librealsense documentation on Windows metadata support) - reconnect the "
                    "camera after enabling it and retry."
                )

            ir_image = ir_bytes_to_image(bytes(ir_frame.get_data()), *self.ir_resolution)
            rgb_image = yuyv_to_bgr(bytes(color_frame.get_data()), *self.color_resolution)
            ir_ts_us = ir_frame.get_frame_metadata(metadata)
            rgb_ts_us = color_frame.get_frame_metadata(metadata)
            ir_frame_number = ir_frame.get_frame_number()
            color_frame_number = color_frame.get_frame_number()

            yield ir_image, rgb_image, ir_ts_us, rgb_ts_us, ir_frame_number, color_frame_number

    def stop(self):
        if self._pipeline is not None:
            self._pipeline.stop()
            self._pipeline = None
