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


def group_for_pick(groups, pick):
    """Isolates the ONE (sensor, profiles) group (from resolve_and_group's
    output) that actually contains `pick`'s stream - for a caller that
    needs to capture/control just ONE of two picked streams independently,
    e.g. calibrating one stream's own dual-LED-panel-mode panel without
    touching the other stream's sensor at all (see
    gui/pages/calibration_page.py, gui/pages/roi_select_page.py).

    Assumes pick_a/pick_b resolve to two DISTINCT sensors - true whenever
    dual-panel mode is relevant (two physically separate panels imply two
    physically separate camera sensors). If they instead share one sensor,
    both picks already live in that same single group anyway (resolve_and_
    group merged them), so this just returns that shared group either way."""
    for sensor, profiles in groups:
        if any(_pick_matches(p, pick) for p in profiles):
            return [(sensor, profiles)]
    raise RuntimeError("No resolved sensor group contains pick {!r}".format(pick))


def exposure_for_group(profiles, pick_a, pick_b, exposure_a, exposure_b):
    """Which of exposure_a/exposure_b applies to a resolved sensor group,
    given that group's own `profiles` list and the two original picks -
    lets Stream Config's per-stream exposure values (different sensors have
    different brightness characteristics, same reasoning as Threshold
    Tuning's own independent per-stream threshold fraction) reach the right
    physical sensor when pick_a and pick_b resolve to two DISTINCT sensors
    (the common Stereo Module + RGB Camera shape).

    A group containing BOTH pick_a's and pick_b's streams (the Dual-RGB
    shape - two stream profiles sharing ONE physical sensor) can only ever
    have ONE real exposure value in hardware, regardless of what the UI
    offers per stream - exposure_a wins in that case, arbitrarily but
    deterministically (matches this project's other "Stream A takes
    precedence" spots, e.g. preselection logic only ever matching pick_a).
    Callers applying camera controls per group (gui/pages/roi_select_page.py's
    _apply_camera_controls and its duplicated inline copies in
    engine/session_engine.py/engine/threshold_preview_thread.py) call this
    once per group to resolve which value to actually write."""
    has_a = any(_pick_matches(p, pick_a) for p in profiles)
    has_b = any(_pick_matches(p, pick_b) for p in profiles)
    if has_a:
        return exposure_a
    if has_b:
        return exposure_b
    # Shouldn't happen - every group resolve_and_group produces contains at
    # least one of the two original picks by construction - but fall back
    # to exposure_a rather than raising, matching this function's other
    # "A takes precedence" tie-break.
    return exposure_a


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
    auto-exposure left however it was.

    Also restores EXPOSURE ONLY to the sensor's OWN factory default
    (get_option_range().default) before re-enabling auto, but ONLY when the
    sensor is actually coming FROM manual mode (enable_auto_exposure reads
    back as 0 right now) - set_manual_exposure writes exposure, so flipping
    only the auto flag back on would leave the manually-set exposure value
    stuck in the camera otherwise.

    GAIN IS DELIBERATELY NEVER TOUCHED HERE. An earlier version restored
    gain to its own factory default alongside exposure, added after a real
    observed failure: a manual->auto round trip in Stream Config left the
    sensor auto-exposing with the UI's default gain of 16 still stuck in
    it, dark enough that Calibration's Otsu blob detection stopped finding
    the LEDs at all - and because the value lives in the CAMERA, not the
    app, it survived app restarts and only a power-cycle/hardware_reset
    cleared it. That restore-gain-to-default fix was unit-tested and
    provably correct against a fake sensor, but on the real "RealSense D585
    Prototype" rig the SAME symptom came back anyway - gain still not
    returning to its original value after Manual->Auto, still needing a
    manual hardware reset. The SDK-reported "factory default" this function
    would restore to is firmware metadata, not a snapshot of the sensor's
    true pre-manual state - on this prototype board that reported value
    apparently isn't what a genuine hardware reset actually produces, and/or
    this sensor's own AE algorithm doesn't reliably re-take control of gain
    once it's been externally written, even after enable_auto_exposure is
    flipped back on. Software "restoring" gain to *a* value was fighting a
    firmware behavior that's proven unreliable here. The actual fix:
    set_manual_exposure no longer writes gain AT ALL (see below), so there
    is nothing of ours to restore and nothing for this class of bug to get
    stuck on - re-enabling auto here just hands gain back to the camera's
    own continuous AE algorithm, the same as a real power-cycle would.

    The was-manual GATE still matters for the exposure restore it does do.
    This function is called unconditionally on every apply point (ROI
    Select, Calibration, Threshold Tuning, Live Session) whenever the
    operator has "Auto exposure" selected - not just on an actual
    Manual->Auto transition. An earlier version restored the default
    unconditionally every time, which is a real regression on a sensor
    that's ALREADY auto-exposing correctly: forcibly resetting exposure
    back to a cold default and letting auto-exposure re-converge from
    scratch, on every single run, can leave it under-converged within
    calibration.settle_frames' short settle window even though it would
    have stayed correctly exposed if left alone - producing an
    intermittently underexposed image (LEDs near the detection threshold
    dropping out) rather than the original bug's total blackout. Restoring
    only on an actual mode transition leaves an already-auto sensor
    completely undisturbed, matching every apply point that doesn't
    actually need to fix anything."""
    if not sensor.supports(rs.option.enable_auto_exposure):
        return False
    was_manual = sensor.get_option(rs.option.enable_auto_exposure) == 0
    if was_manual and sensor.supports(rs.option.exposure):
        # Written BEFORE re-enabling auto, deliberately: on some sensors
        # writing exposure while auto-exposure is on implicitly turns auto
        # back off, so doing it in this order can't leave auto disabled
        # behind our back.
        sensor.set_option(rs.option.exposure, sensor.get_option_range(rs.option.exposure).default)
    sensor.set_option(rs.option.enable_auto_exposure, 1)
    return True


def set_emitter_enabled(sensor, enabled):
    if sensor.supports(rs.option.emitter_enabled):
        sensor.set_option(rs.option.emitter_enabled, 1 if enabled else 0)
        return True
    return False


# D400-series' own values for rs.option.inter_cam_sync_mode. D500-series
# devices (e.g. this project's "RealSense D585 Prototype") use the SAME
# option but a DIFFERENT value scheme entirely - rs.d500_intercam_sync_mode's
# own enum (none=0/rgb_master=1/pwm_master=2/external_master=3), confirmed
# via direct pyrealsense2 2.58.3 introspection, NOT the plain master/slave
# scheme below. Picking the right raw value per camera model/generation is
# the responsibility of whoever assigns master/slave roles (not this
# function - see set_inter_cam_sync_mode's own docstring), and needs real
# multi-camera hardware confirmation before it's load-bearing either way -
# see the multi-camera design doc's "Known risks" section.
INTER_CAM_SYNC_DEFAULT = 0
INTER_CAM_SYNC_MASTER = 1
INTER_CAM_SYNC_SLAVE = 2


def set_inter_cam_sync_mode(device, mode):
    """Applies rs.option.inter_cam_sync_mode to whichever sensor on `device`
    actually supports it - NOT assumed to be a fixed sensor/index, since a
    camera can have multiple sensors and (per public RealSense documentation,
    not yet confirmed on this project's own hardware) genlock is reportedly
    carried by the depth/stereo sensor, not the color sensor. `mode` is
    written as given - this function does NOT translate between D400-series'
    plain master/slave scheme and D500-series' differently-numbered
    rs.d500_intercam_sync_mode scheme (see the constants above); the caller
    must pass whichever raw value is correct for that specific device's
    generation. Returns True/False so callers can warn the operator instead
    of silently proceeding unsynced - same convention as
    set_emitter_enabled/enable_auto_exposure."""
    for sensor in device.query_sensors():
        if sensor.supports(rs.option.inter_cam_sync_mode):
            sensor.set_option(rs.option.inter_cam_sync_mode, mode)
            return True
    return False


def resolve_inter_cam_sync_value(inter_cam_sync_settings, camera_name, is_master):
    """Looks up the raw inter_cam_sync_mode value for THIS camera's role
    (master/slave) from settings.yaml's camera.inter_cam_sync section
    (keyed by exact device name, same convention as camera.stream_options) -
    NOT hardcoded here, because which raw value means what is a per-CAMERA-
    MODEL property (see set_inter_cam_sync_mode's own docstring: D400-series
    and D500-series use different value schemes on the same option - D500's
    own rs.d500_intercam_sync_mode enum doesn't even have a plain "slave"
    value, so blindly reusing D400's scheme for an unconfirmed model would
    silently misconfigure it). Returns None - skip genlock entirely for
    this camera - if camera_name has no entry, a safe default rather than
    guessing a possibly-wrong value for a model/firmware nobody has
    confirmed the right values for yet."""
    entry = inter_cam_sync_settings.get(camera_name)
    if entry is None:
        return None
    return entry["master"] if is_master else entry["slave"]


def resolve_max_slave_color_resolution(inter_cam_sync_settings, camera_name):
    """Returns (width, height) - the confirmed max color-stream resolution
    this camera MODEL can safely use while acting as a genlock SLAVE,
    without hitting the real USB-bandwidth ceiling found on real hardware
    (full 1280x720@30 color blocks BOTH streams entirely once genlocked;
    640x480@30 was rigorously confirmed - frame-count parity + tight
    index-lockstep offset stability, not just "frames flow" - see
    tools/genlock_diag/diag_genlock_quality_test.py). Returns None if this
    camera model has no confirmed cap at all - same "unconfirmed means
    don't guess" convention as resolve_inter_cam_sync_value; the caller
    must treat None as "block, not allow.\""""
    entry = inter_cam_sync_settings.get(camera_name)
    if entry is None or "max_slave_color_resolution" not in entry:
        return None
    resolution = entry["max_slave_color_resolution"]
    return resolution["width"], resolution["height"]


def set_manual_exposure(sensor, exposure):
    """Manual mode touches EXPOSURE ONLY - gain is deliberately never read
    or written here. See enable_auto_exposure's docstring for why: an
    earlier version also set gain (to the Stream Config UI's spinbox
    value) and tried to restore it on the way back to Auto, but that
    restore proved unreliable on real hardware (the "RealSense D585
    Prototype" rig), leaving gain stuck until a physical hardware reset.
    Never touching gain from software at all means there's nothing of ours
    to get stuck - the camera's own auto-exposure algorithm always owns
    gain, whether this sensor is currently in manual exposure mode or not.

    `exposure` is CLAMPED to this specific sensor's own currently-reported
    valid range (get_option_range().min/.max) before being written.
    Confirmed on real hardware: the Stream Config UI's exposure spinbox
    accepts any value from 1 to 1,000,000 with no way to know ahead of time
    what a given sensor's actual valid range is - that range isn't a fixed
    property of the sensor model, it can depend on the CURRENTLY configured
    resolution/fps (e.g. a color sensor's valid exposure range can differ
    at 640x480@5fps vs. other configurations). Passing an out-of-range
    value straight to sensor.set_option() raises a raw pyrealsense2
    exception ("out of range value for argument 'value'") instead of a
    sensible fallback - clamping to whatever this sensor/configuration
    actually supports right now means a value that's merely too extreme
    for this rig still applies (at its nearest valid bound) instead of
    hard-failing the whole capture."""
    if not (sensor.supports(rs.option.enable_auto_exposure) and sensor.supports(rs.option.exposure)):
        return False
    sensor.set_option(rs.option.enable_auto_exposure, 0)
    exposure_range = sensor.get_option_range(rs.option.exposure)
    clamped_exposure = max(exposure_range.min, min(exposure_range.max, exposure))
    sensor.set_option(rs.option.exposure, clamped_exposure)
    return True


def _read_global_ts_us(frame_a, frame_b):
    """Reads and validates both frames' RealSense global timestamp
    (frame.get_timestamp(), converted from its native ms to this project's
    _ts_us microsecond convention) - the join key
    engine.cross_camera_reconciler.CrossCameraReconciler's matching uses.
    Raises if either frame isn't actually reporting the GLOBAL_TIME domain:
    global_time_enabled may be disabled/unsupported on this device/driver,
    in which case frame.get_timestamp() silently falls back to a different
    domain (system_time/hardware_clock) that isn't comparable across two
    independent devices the way GLOBAL_TIME is meant to be - a silently-
    wrong value here would be worse than an obvious failure (same "fail
    loudly" convention as the frame_timestamp metadata check in
    ContinuousCapture.frames_with_diagnostics, the only caller of this
    function)."""
    domain = rs.timestamp_domain.global_time
    if frame_a.get_frame_timestamp_domain() != domain or frame_b.get_frame_timestamp_domain() != domain:
        raise RuntimeError(
            "This camera is not reporting frames in the RealSense GLOBAL_TIME "
            "timestamp domain (global_time_enabled may be disabled or unsupported "
            "on this device/driver), which the cross-camera Global TS Latency "
            "metric requires. Reconnect the camera or disable "
            "camera_sync.capture_global_ts and retry."
        )
    return frame_a.get_timestamp() * 1000.0, frame_b.get_timestamp() * 1000.0


class ContinuousCapture:
    def __init__(self, device_serial, pick_a, pick_b, enable_depth_for_ir_sync=True, capture_global_ts=False):
        self.device_serial = device_serial
        self.pick_a = pick_a
        self.pick_b = pick_b
        # See _depth_sync_stream/_build_config - whether to co-enable the
        # stereo module's depth stream to fix IR/RGB sync.
        self.enable_depth_for_ir_sync = enable_depth_for_ir_sync
        # Opt-in: reads+validates each frame's RealSense GLOBAL_TIME-domain
        # timestamp too (see _read_global_ts_us) - a cross-camera-only
        # concept (engine.cross_camera_reconciler's matching key and its
        # Global TS Latency metric), so single-camera runs never need or
        # request it.
        self.capture_global_ts = capture_global_ts
        # Set on start() to whether a depth stream was actually requested
        # (self._depth_sync_stream() is not None) - not a resolve/success
        # check, just what start() attempted, for callers that want to report.
        self.depth_sync_active = False
        self._pipeline = None

    def _depth_sync_stream(self):
        """Returns (width, height, fps) for the DEPTH stream to co-enable, or
        None if there's nothing to fix.

        Real-hardware finding (see CLAUDE.md's "IR/RGB sync depends on stream
        OPEN order" section): rs.pipeline() gives no control over the order it
        internally OPENS the two sensors, and that order decides whether IR
        and RGB come out synchronized - RGB-before-IR produces a fixed
        multi-ms offset (this app measured ~11.3ms) where IR-before-RGB (or a
        standalone reference script that happened to enable color first, but
        also always had depth+IR firmware-linked from an earlier prototype)
        measured ~3.5ms. Co-enabling depth alongside IR+RGB fixes this
        regardless of enable order, matching Intel's documented firmware
        requirement that depth and IR be configured together - the pipeline
        satisfies that requirement internally in an order we can't see when
        only IR (not depth) is requested; enabling depth explicitly removes
        the ambiguity.

        None when the setting is off, or when NEITHER pick is infrared (a
        color+color / Dual-RGB pairing has no stereo module in play at all -
        there's nothing for a depth stream to sync against).

        Only the FIRST infrared pick's own (width, height, fps) is ever
        returned, even for an IR+IR pairing - one depth stream, not two.
        Depth intentionally matches that pick's own resolution/fps rather
        than something smaller to save bandwidth: depth and IR come off one
        stereo readout and the firmware requires them configured together, so
        a mismatched-resolution depth stream would likely fail to resolve at
        all."""
        if not self.enable_depth_for_ir_sync:
            return None
        for pick in (self.pick_a, self.pick_b):
            if pick["stream_type"] == rs.stream.infrared:
                return pick["width"], pick["height"], pick["fps"]
        return None

    def _build_config(self):
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
        depth_stream = self._depth_sync_stream()
        if depth_stream is not None:
            width, height, fps = depth_stream
            config.enable_stream(rs.stream.depth, 0, width, height, rs.format.z16, fps)
        return config

    def start(self):
        # Deliberately as simple as the real-hardware-verified version this
        # matches: build one config (depth included whenever an infrared
        # pick is present and the setting is on) and start it directly - NO
        # can_resolve() pre-check/fallback. An earlier version added exactly
        # that speculative probe-then-fallback, and it silently undid this
        # fix: can_resolve() returning a false negative for a depth+IR+RGB
        # combination that pipeline.start() itself handles fine falls back to
        # the no-depth config with no error raised, which is indistinguishable
        # from the fix simply not being applied. If a config genuinely can't
        # start, let pipeline.start() raise - that reaches the operator as a
        # real error instead of a silent, wrong fallback.
        self.depth_sync_active = self._depth_sync_stream() is not None
        config = self._build_config()
        # Only assign self._pipeline AFTER pipeline.start(config) actually
        # succeeds - confirmed as a real bug via real hardware (two D455s
        # opened concurrently, one's pipeline.start() failed): assigning
        # self._pipeline = rs.pipeline() before the call that can fail left
        # a constructed-but-never-started pipeline behind for stop()'s own
        # "if self._pipeline is not None" guard to (wrongly) treat as
        # stoppable, and pyrealsense2 itself raises "stop() cannot be called
        # before start()" for that. If start(config) raises, self._pipeline
        # stays None (its __init__ default), so stop() correctly no-ops.
        pipeline = rs.pipeline()
        pipeline.start(config)
        self._pipeline = pipeline

    def _get_frame(self, frameset, pick):
        if pick["stream_type"] == rs.stream.infrared:
            return frameset.get_infrared_frame(pick["stream_index"])
        return frameset.get_color_frame(pick["stream_index"])

    def frames(self):
        for stream_a_image, stream_b_image, stream_a_ts_us, stream_b_ts_us, _, _, _, _ in self.frames_with_diagnostics():
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

            if self.capture_global_ts:
                global_ts_a, global_ts_b = _read_global_ts_us(frame_a, frame_b)
            else:
                global_ts_a, global_ts_b = None, None

            yield image_a, image_b, ts_a, ts_b, num_a, num_b, global_ts_a, global_ts_b

    def stop(self):
        if self._pipeline is not None:
            self._pipeline.stop()
            self._pipeline = None
