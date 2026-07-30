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

import pyrealsense2 as rs

from domain.realsense_utils import DECODERS, decode_frame


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
    fps) - the raw candidate set for the Stream Select picker. This is
    intentionally unfiltered beyond "is it decodable at all" - some devices
    advertise the SAME real stream at a given resolution/fps in many pixel
    formats (e.g. an infrared stream reported as y8 AND uyvy/bgr8/rgb8/
    bgra8/rgba8, all just software reinterpretations of the same monochrome
    data), which is real device behavior, not something to silently hide
    here. settings.yaml's camera.stream_options (see parse_camera_tests_
    config/resolve_camera_tests below) is where the operator curates which
    of these are actually worth offering in the picker -
    keeping that a hand-edited config decision rather than a hardcoded
    Python heuristic. Split out from list_video_stream_options so it's
    directly testable against a fake device without needing a fake
    rs.context too."""
    options = []
    for sensor_index, sensor in enumerate(device.query_sensors()):
        for p in sensor.profiles:
            if not p.is_video_stream_profile():
                continue
            if p.stream_type() not in (rs.stream.infrared, rs.stream.color):
                continue
            if p.format() not in DECODERS:
                # Advertised but undecodable format (e.g. y16 - see
                # domain/realsense_utils.py's DECODERS docstring/comment) -
                # never offer it as a pickable option, since nothing
                # downstream (LED-blob detection, VideoPanel's 8-bit
                # QImage assumption) can handle anything but what DECODERS
                # already covers.
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


_STREAM_TYPES_BY_NAME = {"infrared": rs.stream.infrared, "color": rs.stream.color}
_FORMATS_BY_NAME = {fmt.name: fmt for fmt in DECODERS}


def _parse_stream_type(stream_type_name):
    if stream_type_name not in _STREAM_TYPES_BY_NAME:
        raise ValueError(
            "settings.yaml camera.stream_options: unknown stream_type {!r} - must be "
            "'infrared' or 'color'.".format(stream_type_name)
        )
    return _STREAM_TYPES_BY_NAME[stream_type_name]


def _parse_format(format_name):
    if format_name not in _FORMATS_BY_NAME:
        raise ValueError(
            "settings.yaml camera.stream_options: unknown format {!r} - must be a format "
            "domain.realsense_utils.DECODERS can decode.".format(format_name)
        )
    return _FORMATS_BY_NAME[format_name]


def parse_camera_tests_config(raw_tests):
    """Converts one camera's settings.yaml camera.stream_options entry -
    a list of named tests, each fixing which two physical streams it
    compares (stream_a_identity/stream_b_identity: stream_type/stream_index,
    never changing across sensor options) and offering a list of
    resolution/fps/format pairs to run that test at (sensor_options) - into
    real-rs-enum form (plain stream_type/format name strings -> rs.stream/
    rs.format members). Raises ValueError on an unrecognized stream_type/
    format string, same convention the old flat-list config parsing used.
    Returns [{"test_name": str, "stream_a_identity": {"stream_type", "stream_index"},
    "stream_b_identity": {...}, "sensor_options": [{"stream_a": {width,
    height, fps, format}, "stream_b": {...}}, ...]}, ...] - still missing
    sensor_index, which only a live device query can resolve (see
    resolve_camera_tests)."""
    def parse_identity(raw_identity):
        return {
            "stream_type": _parse_stream_type(raw_identity["stream_type"]),
            "stream_index": raw_identity["stream_index"],
        }

    def parse_side(raw_side):
        return {
            "width": raw_side["width"],
            "height": raw_side["height"],
            "fps": raw_side["fps"],
            "format": _parse_format(raw_side["format"]),
        }

    parsed = []
    for test in raw_tests:
        parsed.append({
            "test_name": test["test_name"],
            "stream_a_identity": parse_identity(test["stream_a_identity"]),
            "stream_b_identity": parse_identity(test["stream_b_identity"]),
            "sensor_options": [
                {"stream_a": parse_side(entry["stream_a"]), "stream_b": parse_side(entry["stream_b"])}
                for entry in test["sensor_options"]
            ],
        })
    return parsed


_CURATED_MATCH_KEYS = ("stream_type", "stream_index", "width", "height", "fps", "format")


def _find_matching_option(options, wanted):
    """Returns the first entry in `options` (list_video_stream_options_
    from_device's output) matching `wanted` on stream_type/stream_index/
    width/height/fps/format - ignoring sensor_index, a live-device detail
    the curated config never specifies, since it can vary by rig even for
    the same logical stream. Returns None if nothing matches (this specific
    rig/firmware doesn't support what was asked for)."""
    for option in options:
        if all(option[key] == wanted[key] for key in _CURATED_MATCH_KEYS):
            return option
    return None


def resolve_camera_tests(device_options, parsed_tests):
    """Resolves each parsed test's sensor_options against a live device's
    raw options (list_video_stream_options_from_device's output), producing
    only the sensor-options entries where BOTH sides actually match
    something this specific connected device reports - combining each
    test's fixed stream_a_identity/stream_b_identity with each
    sensor_options entry's width/height/fps/format to search, same
    ignore-sensor_index matching _find_matching_option always used.
    Preserves both the tests' and their sensor_options' own configured
    order, not device-enumeration order.

    Returns [{"test_name": str, "options": [{"pick_a": <full device option
    incl. sensor_index>, "pick_b": ...}, ...]}, ...] - EVERY parsed test is
    included, even with an empty "options" list, so the caller can tell
    "test exists but nothing on this rig matches it" apart from "test
    doesn't exist at all" and decide how to handle that (this project's
    convention: omit it from the picker rather than show it disabled)."""
    resolved_tests = []
    for test in parsed_tests:
        resolved_options = []
        for entry in test["sensor_options"]:
            pick_a = _find_matching_option(device_options, {**test["stream_a_identity"], **entry["stream_a"]})
            pick_b = _find_matching_option(device_options, {**test["stream_b_identity"], **entry["stream_b"]})
            if pick_a is not None and pick_b is not None:
                resolved_options.append({"pick_a": pick_a, "pick_b": pick_b})
        resolved_tests.append({"test_name": test["test_name"], "options": resolved_options})
    return resolved_tests


def list_video_stream_options(ctx, serial):
    device = find_device_by_serial(ctx, serial)
    return list_video_stream_options_from_device(device)


def stream_slug(pick):
    """Slug key for a stream pick, e.g. "infrared1" / "color" - matches the
    slug scheme domain/calibration.py's update_config_leds/load_led_positions
    key config.yaml's per-camera LED blocks with (see tests/domain/
    test_calibration.py), and the filenames calibration_page.py's debug
    detection images use. stream_index 0 (the common case for a lone color
    sensor) is omitted rather than rendered as "color0", so a single-RGB
    camera's slug reads the same as it always has."""
    return "{}{}".format(pick["stream_type"].name, pick["stream_index"] or "")


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
    if pick_a["stream_type"] == pick_b["stream_type"] and pick_a["stream_index"] == pick_b["stream_index"]:
        raise RuntimeError(
            "resolve_and_group: pick_a and pick_b are the same stream ({!r} index {}) - Stream "
            "Select must choose two distinct streams.".format(pick_a["stream_type"], pick_a["stream_index"])
        )

    sensors = list(device.query_sensors())

    def sensor_and_profile_for(pick):
        sensor = sensors[pick["sensor_index"]]
        try:
            profile = next(p for p in sensor.profiles if _pick_matches(p, pick))
        except StopIteration:
            raise RuntimeError(
                "resolve_and_group: no matching profile found on sensor {} for pick {!r} - the "
                "device's available profiles may have changed since this pick was made (e.g. "
                "after a firmware mode switch or reconnect).".format(pick["sensor_index"], pick)
            )
        return sensor, profile

    sensor_a, profile_a = sensor_and_profile_for(pick_a)
    sensor_b, profile_b = sensor_and_profile_for(pick_b)

    if sensor_a is sensor_b:
        return [(sensor_a, [profile_a, profile_b])]
    return [(sensor_a, [profile_a]), (sensor_b, [profile_b])]


def _try_get_stream_key(profile):
    """Return (stream_type, stream_index) for a real profile object, or None
    if `profile` doesn't support those calls (e.g. a plain-string test-fake
    placeholder that's only ever handed opaquely to sensor.open()). Used by
    capture_synced_frame_pair to derive the *exact expected key set* when
    possible, not just a count, so that a phantom/unrequested stream
    delivering frames can't silently swap in for a genuinely missing
    expected one and still satisfy a count-only check."""
    stream_type = getattr(profile, "stream_type", None)
    stream_index = getattr(profile, "stream_index", None)
    if not callable(stream_type) or not callable(stream_index):
        return None
    return (stream_type(), stream_index())


def _try_derive_expected_keys(groups):
    """Best-effort exact (stream_type, stream_index) set for every profile
    across `groups`. Returns None (meaning "can't verify, fall back to a
    count-only check") if any profile isn't introspectable this way - this
    is only expected with opaque test-fake profiles; real rs profile objects
    always support stream_type()/stream_index()."""
    keys = set()
    for _, profiles in groups:
        for p in profiles:
            key = _try_get_stream_key(p)
            if key is None:
                return None
            keys.add(key)
    return keys


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
         frames each) AND, whenever the exact expected key set can be
         derived from `groups` (real rs profiles - see
         _try_derive_expected_keys), that the set of streams delivering
         frames is exactly the requested set - not just the right count.
         This closes a narrow gap where a phantom/unrequested stream
         delivering frames alongside a genuinely missing expected one could
         otherwise reach the right COUNT while capturing the wrong SET.
      4. Call on_both_streaming() if given (e.g. turn the LED panel on now).
      5. Reset counters and wait for `settle_frames` fresh frames per stream
         (so the captured frame reflects state AFTER on_both_streaming ran).
      6. Stop + close every sensor.

    Returns {(stream_type, stream_index): bytes} - raw frame bytes (safe
    regardless of pixel format/size; caller converts with
    domain.realsense_utils per stream's actual format).

    Note: the expected number of distinct streams is derived from
    len(profiles) per group, not (only) by calling stream_type()/
    stream_index() on the profile objects passed in. The actual
    (stream_type, stream_index) keys are discovered from the frames the
    callback receives, same as the original two-sensor version discovered
    rs.stream.infrared/rs.stream.color from incoming frames rather than from
    the profiles it opened with. Where the profiles ARE introspectable (real
    rs profiles), the exact expected key set is cross-checked as a defensive
    belt-and-braces measure (see _try_derive_expected_keys) - test fakes in
    this project's own test suite use opaque string placeholders for
    profiles (they're never called, only passed to sensor.open()), so that
    cross-check is a known no-op under test and only actually engages
    against real hardware/profiles.
    """
    state = {}
    expected_stream_count = sum(len(profiles) for _, profiles in groups)
    expected_keys = _try_derive_expected_keys(groups)

    def callback(frame):
        key = (frame.get_profile().stream_type(), frame.get_profile().stream_index())
        s = state.setdefault(key, {"count": 0, "frame": None})
        s["count"] += 1
        s["frame"] = bytes(frame.get_data())  # raw bytes - safe regardless of pixel format/size

    def keys_match_expected():
        return expected_keys is None or set(state.keys()) == expected_keys

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
                        "Timed out ({}) - {} of {} expected streams delivering frames "
                        "(keys seen: {}, expected: {})".format(
                            label, len(state), expected_stream_count,
                            sorted(state.keys(), key=repr),
                            sorted(expected_keys, key=repr) if expected_keys is not None else "<unverifiable>",
                        )
                    )
                time.sleep(0.05)

        # step 3: confirm every stream is actually streaming before doing anything else -
        # and, when verifiable, that it's exactly the requested set of streams (not a
        # phantom/unrequested one standing in for a missing expected one).
        wait_until(
            lambda: (
                len(state) >= expected_stream_count
                and all(s["count"] >= 1 for s in state.values())
                and keys_match_expected()
            ),
            "waiting for initial frames",
        )

        # step 4: trigger whatever should happen now that everything is live (e.g. LEDs on)
        if on_both_streaming is not None:
            on_both_streaming()

        # step 5: reset counters so we only accept frames captured AFTER the trigger
        for s in state.values():
            s["count"] = 0
        wait_until(
            lambda: all(s["count"] >= settle_frames for s in state.values()) and keys_match_expected(),
            "waiting for post-trigger settled frames",
        )

        # Final defensive check: guards against a phantom key sneaking into
        # `state` between the initial wait succeeding and here (e.g. during
        # the settle phase) - fail loudly rather than silently return a
        # mismatched stream set to the caller.
        if expected_keys is not None and set(state.keys()) != expected_keys:
            raise RuntimeError(
                "capture_synced_frame_pair: captured stream set does not match what was "
                "requested - expected {}, got {}".format(
                    sorted(expected_keys, key=repr), sorted(state.keys(), key=repr)
                )
            )

        return {key: s["frame"] for key, s in state.items()}
    finally:
        for sensor, _ in groups:
            sensor.stop()
            sensor.close()


def enable_auto_exposure(sensor):
    """Returns True/False so callers can warn the operator when the sensor
    doesn't support the option instead of silently proceeding with
    auto-exposure left however it was."""
    if sensor.supports(rs.option.enable_auto_exposure):
        sensor.set_option(rs.option.enable_auto_exposure, 1)
        return True
    return False


def set_emitter_enabled(sensor, enabled):
    if sensor.supports(rs.option.emitter_enabled):
        sensor.set_option(rs.option.emitter_enabled, 1 if enabled else 0)
        return True
    return False


def set_manual_exposure(sensor, exposure, gain):
    if not (sensor.supports(rs.option.enable_auto_exposure) and sensor.supports(rs.option.exposure) and sensor.supports(rs.option.gain)):
        return False
    sensor.set_option(rs.option.enable_auto_exposure, 0)
    sensor.set_option(rs.option.exposure, exposure)
    sensor.set_option(rs.option.gain, gain)
    return True


class ContinuousCapture:
    def __init__(self, device_serial, pick_a, pick_b):
        self.device_serial = device_serial
        self.pick_a = pick_a
        self.pick_b = pick_b
        self._pipeline = None

    def start(self):
        config = rs.config()
        # Bind the pipeline to the exact physical device chosen in Device
        # Select - without this, with more than one RealSense camera
        # attached, rs.pipeline() can silently pick a DIFFERENT device than
        # the one the operator selected (and than capture_synced_frame_pair,
        # which resolves via find_device_by_serial, correctly uses for
        # ROI/calibration), producing a wrong-camera bug with no error.
        config.enable_device(self.device_serial)
        for pick in (self.pick_a, self.pick_b):
            config.enable_stream(pick["stream_type"], pick["stream_index"], pick["width"], pick["height"], pick["format"], pick["fps"])
        self._pipeline = rs.pipeline()
        self._pipeline.start(config)

    def _get_frame(self, frameset, pick):
        if pick["stream_type"] == rs.stream.infrared:
            return frameset.get_infrared_frame(pick["stream_index"])
        return frameset.get_color_frame(pick["stream_index"])

    def frames(self):
        for stream_a_image, stream_b_image, stream_a_ts_us, stream_b_ts_us, _, _ in self.frames_with_diagnostics():
            yield stream_a_image, stream_b_image, stream_a_ts_us, stream_b_ts_us

    def frames_with_diagnostics(self):
        while True:
            frameset = self._pipeline.wait_for_frames()
            frame_a = self._get_frame(frameset, self.pick_a)
            frame_b = self._get_frame(frameset, self.pick_b)
            if not frame_a or not frame_b:
                continue

            metadata = rs.frame_metadata_value.frame_timestamp
            if not (frame_a.supports_frame_metadata(metadata) and frame_b.supports_frame_metadata(metadata)):
                raise RuntimeError(
                    "This camera/driver does not expose per-frame HW timestamp metadata "
                    "(frame_metadata_value.frame_timestamp), which the sync metrics require. "
                    "On Windows, RealSense per-frame metadata is often disabled by default at "
                    "the OS/driver level and needs a one-time enablement step (see Intel's "
                    "librealsense documentation on Windows metadata support) - reconnect the "
                    "camera after enabling it and retry."
                )

            image_a = decode_frame(bytes(frame_a.get_data()), self.pick_a["format"], self.pick_a["width"], self.pick_a["height"])
            image_b = decode_frame(bytes(frame_b.get_data()), self.pick_b["format"], self.pick_b["width"], self.pick_b["height"])
            ts_a = frame_a.get_frame_metadata(metadata)
            ts_b = frame_b.get_frame_metadata(metadata)
            num_a = frame_a.get_frame_number()
            num_b = frame_b.get_frame_number()

            yield image_a, image_b, ts_a, ts_b, num_a, num_b

    def stop(self):
        if self._pipeline is not None:
            self._pipeline.stop()
            self._pipeline = None
