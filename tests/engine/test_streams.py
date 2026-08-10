import threading
import time

import pytest
import pyrealsense2 as rs
from engine.streams import (
    list_devices, capture_synced_frame_pair, ContinuousCapture,
    enable_auto_exposure,
    list_video_stream_options_from_device, resolve_and_group, group_for_pick,
    set_emitter_enabled, set_manual_exposure, stream_slug,
    parse_camera_tests_config, resolve_camera_tests,
)


class FakeOptionRange:
    """Stand-in for pyrealsense2's option_range - only .default is read."""

    def __init__(self, default):
        self.default = default


class FakeOptionSensor:
    """Fake sensor exposing just enough of the .supports()/.set_option()/
    .get_option_range() API for enable_auto_exposure/set_emitter_enabled/
    set_manual_exposure - real pyrealsense2 sensors aren't constructible
    without hardware."""

    def __init__(self, supported_options, option_defaults=None):
        self._supported_options = set(supported_options)
        self._option_defaults = option_defaults or {}
        self.set_options = {}
        # Ordered log of every write, so a test can assert on ORDER (e.g. that
        # exposure/gain are restored BEFORE auto-exposure is switched on),
        # which set_options alone can't show.
        self.writes = []

    def supports(self, option):
        return option in self._supported_options

    def set_option(self, option, value):
        self.set_options[option] = value
        self.writes.append((option, value))

    def get_option_range(self, option):
        return FakeOptionRange(self._option_defaults.get(option, 0))


def test_enable_auto_exposure_sets_option_on_when_supported():
    sensor = FakeOptionSensor(supported_options={rs.option.enable_auto_exposure})
    assert enable_auto_exposure(sensor) is True
    assert sensor.set_options[rs.option.enable_auto_exposure] == 1


def test_enable_auto_exposure_returns_false_when_unsupported():
    # Callers rely on this to warn the operator instead of silently
    # proceeding with auto-exposure left however it was.
    sensor = FakeOptionSensor(supported_options=set())
    assert enable_auto_exposure(sensor) is False
    assert sensor.set_options == {}


# --- Regression: switching Stream Config back to "Auto exposure" must undo
# what set_manual_exposure wrote, not just flip the auto flag. Leaving the
# manual gain behind (the UI defaults it to 16) left the camera dark enough
# that Calibration's Otsu blob detection stopped finding LEDs at all - and
# since the value lives in the CAMERA it survived app restarts. ---

def _full_exposure_sensor():
    return FakeOptionSensor(
        supported_options={rs.option.enable_auto_exposure, rs.option.exposure, rs.option.gain},
        option_defaults={rs.option.exposure: 7500, rs.option.gain: 64},
    )


def test_enable_auto_exposure_restores_exposure_and_gain_defaults():
    sensor = _full_exposure_sensor()
    set_manual_exposure(sensor, exposure=8500, gain=16)  # what the UI defaults to

    assert enable_auto_exposure(sensor) is True

    assert sensor.set_options[rs.option.exposure] == 7500  # factory default, not 8500
    assert sensor.set_options[rs.option.gain] == 64        # factory default, not 16
    assert sensor.set_options[rs.option.enable_auto_exposure] == 1


def test_enable_auto_exposure_restores_defaults_before_switching_auto_on():
    # Order is load-bearing: on some sensors writing exposure while auto is
    # ON implicitly turns auto back off, which would leave auto disabled.
    sensor = _full_exposure_sensor()

    enable_auto_exposure(sensor)

    written_options = [option for option, _ in sensor.writes]
    assert written_options[-1] == rs.option.enable_auto_exposure
    assert rs.option.exposure in written_options[:-1]
    assert rs.option.gain in written_options[:-1]


def test_enable_auto_exposure_skips_unsupported_exposure_gain_options():
    # A sensor supporting auto-exposure but not manual exposure/gain must
    # still get auto enabled, with no attempt to restore what it can't set.
    sensor = FakeOptionSensor(supported_options={rs.option.enable_auto_exposure})

    assert enable_auto_exposure(sensor) is True

    assert sensor.set_options == {rs.option.enable_auto_exposure: 1}


def test_list_devices_lists_any_device_regardless_of_sensor_names():
    # A device whose sensors are named things other than "Stereo Module"/"RGB Camera"
    # (e.g. a D500-series device with different sensor naming) must still be listed.
    class FakeSensorWithCustomName:
        def get_info(self, info_type):
            if info_type == rs.camera_info.name:
                return "Custom Sensor Name"
            return None

    class FakeDeviceWithCustomSensors:
        def query_sensors(self):
            return [FakeSensorWithCustomName()]

        def get_info(self, info_type):
            if info_type == rs.camera_info.name:
                return "Custom Device"
            elif info_type == rs.camera_info.serial_number:
                return "ABC123"
            return None

    class FakeContext:
        def query_devices(self):
            return [FakeDeviceWithCustomSensors()]

    ctx = FakeContext()
    result = list_devices(ctx)
    assert len(result) == 1
    assert result[0].name == "Custom Device"
    assert result[0].serial == "ABC123"


class _FakeFrame:
    def __init__(self, stream_type, stream_index, data):
        self._stream_type = stream_type
        self._stream_index = stream_index
        self._data = data

    def get_profile(self):
        return self

    def stream_type(self):
        return self._stream_type

    def stream_index(self):
        return self._stream_index

    def get_data(self):
        return self._data


class _FakeStreamingSensor:
    """Delivers frames continuously on a background thread once started,
    cycling through all of this sensor's (stream_type, stream_index) keys -
    like a real sensor's callback delivering interleaved frames for however
    many streams it was opened/started with (one sensor can carry more than
    one stream, e.g. two color stream indices). Unlike a synchronous fake,
    this doesn't deliver everything before capture_synced_frame_pair's
    counter reset happens, so it actually exercises the
    reset-then-wait-for-fresh-frames control flow instead of trivially
    satisfying it."""

    def __init__(self, keys):
        self.keys = keys
        self._running = False
        self._thread = None

    def open(self, profiles):
        pass

    def start(self, callback):
        self._running = True

        def deliver_loop():
            counters = {key: 0 for key in self.keys}
            while self._running:
                for stream_type, stream_index in self.keys:
                    counters[(stream_type, stream_index)] += 1
                    data = "frame-{}-{}-{}".format(
                        stream_type, stream_index, counters[(stream_type, stream_index)]
                    ).encode()
                    callback(_FakeFrame(stream_type, stream_index, data))
                time.sleep(0.001)

        self._thread = threading.Thread(target=deliver_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def close(self):
        pass


class _FakeNonDeliveringSensor:
    def open(self, profiles):
        pass

    def start(self, callback):
        pass

    def stop(self):
        pass

    def close(self):
        pass


def test_capture_synced_frame_pair_with_two_distinct_sensors():
    ir_sensor = _FakeStreamingSensor(keys=[(rs.stream.infrared, 1)])
    color_sensor = _FakeStreamingSensor(keys=[(rs.stream.color, 0)])
    groups = [(ir_sensor, ["ir_profile"]), (color_sensor, ["color_profile"])]
    triggered = {"count": 0}

    frames = capture_synced_frame_pair(
        groups,
        on_both_streaming=lambda: triggered.__setitem__("count", triggered["count"] + 1),
        settle_frames=5, timeout_s=5.0,
    )

    assert triggered["count"] == 1
    assert (rs.stream.infrared, 1) in frames
    assert (rs.stream.color, 0) in frames


def test_capture_synced_frame_pair_with_one_shared_sensor_two_stream_indices():
    shared_sensor = _FakeStreamingSensor(keys=[(rs.stream.color, 1), (rs.stream.color, 2)])
    groups = [(shared_sensor, ["left_profile", "right_profile"])]

    frames = capture_synced_frame_pair(groups, settle_frames=5, timeout_s=5.0)

    assert (rs.stream.color, 1) in frames
    assert (rs.stream.color, 2) in frames
    assert frames[(rs.stream.color, 1)] != frames[(rs.stream.color, 2)]  # distinguishable, not accidentally aliased


def test_capture_synced_frame_pair_raises_on_timeout_when_no_frames_arrive():
    sensor = _FakeNonDeliveringSensor()
    groups = [(sensor, ["profile"])]
    with pytest.raises(RuntimeError):
        capture_synced_frame_pair(groups, settle_frames=5, timeout_s=0.2)


# --- list_video_stream_options / resolve_and_group ---
#
# Named FakeVideoProfile2/FakeProfile2/FakeSensor2 (with a FakeDevice added)
# to avoid clashing with the differently-shaped _FakeFrame/_FakeStreamingSensor/
# _FakeNonDeliveringSensor fakes defined above in this file for
# capture_synced_frame_pair.

class FakeVideoProfile2:
    def __init__(self, width, height):
        self._width, self._height = width, height
    def width(self): return self._width
    def height(self): return self._height


class FakeProfile2:
    def __init__(self, stream_type, stream_index, fmt, width=None, height=None, fps=30, is_video=True):
        self._stream_type = stream_type
        self._stream_index = stream_index
        self._fmt = fmt
        self._fps = fps
        self._is_video = is_video
        self._video = FakeVideoProfile2(width, height) if is_video else None

    def stream_type(self): return self._stream_type
    def stream_index(self): return self._stream_index
    def format(self): return self._fmt
    def fps(self): return self._fps
    def is_video_stream_profile(self): return self._is_video
    def as_video_stream_profile(self): return self._video


class FakeSensor2:
    def __init__(self, profiles):
        self.profiles = profiles


class FakeDevice:
    def __init__(self, sensors):
        self._sensors = sensors
    def query_sensors(self):
        return self._sensors


def test_list_video_stream_options_includes_infrared_and_color_only():
    ir_sensor = FakeSensor2(profiles=[
        FakeProfile2(rs.stream.infrared, 1, rs.format.y8, 1280, 720, 30),
        FakeProfile2(rs.stream.gyro, 0, rs.format.motion_xyz32f, is_video=False),  # excluded: not IR/color
    ])
    color_sensor = FakeSensor2(profiles=[
        FakeProfile2(rs.stream.color, 0, rs.format.bgr8, 1280, 720, 30),
    ])
    device = FakeDevice([ir_sensor, color_sensor])

    options = list_video_stream_options_from_device(device)  # test the pure-device-arg variant directly

    assert len(options) == 2
    assert {"sensor_index": 0, "stream_type": rs.stream.infrared, "stream_index": 1,
            "format": rs.format.y8, "width": 1280, "height": 720, "fps": 30} in options
    assert {"sensor_index": 1, "stream_type": rs.stream.color, "stream_index": 0,
            "format": rs.format.bgr8, "width": 1280, "height": 720, "fps": 30} in options


def test_list_video_stream_options_excludes_non_video_profiles_without_crashing():
    sensor = FakeSensor2(profiles=[
        FakeProfile2(rs.stream.pose, 0, rs.format.six_dof, is_video=False),
    ])
    device = FakeDevice([sensor])

    options = list_video_stream_options_from_device(device)

    assert options == []  # no crash from calling width()/height() on a non-video profile


def test_list_video_stream_options_excludes_formats_not_in_decoders():
    # y16 (or any other advertised-but-undecodable format) must never reach
    # the Stream Select picker - only formats domain.realsense_utils.DECODERS
    # can actually decode should be choosable.
    sensor = FakeSensor2(profiles=[
        FakeProfile2(rs.stream.infrared, 1, rs.format.y16, 1280, 720, 30),
        FakeProfile2(rs.stream.infrared, 1, rs.format.y8, 1280, 720, 30),
    ])
    device = FakeDevice([sensor])

    options = list_video_stream_options_from_device(device)

    assert len(options) == 1
    assert options[0]["format"] == rs.format.y8


def test_resolve_and_group_two_distinct_sensors():
    ir_profile = FakeProfile2(rs.stream.infrared, 1, rs.format.y8, 1280, 720, 30)
    color_profile = FakeProfile2(rs.stream.color, 0, rs.format.bgr8, 1280, 720, 30)
    ir_sensor = FakeSensor2(profiles=[ir_profile])
    color_sensor = FakeSensor2(profiles=[color_profile])
    device = FakeDevice([ir_sensor, color_sensor])
    pick_a = {"sensor_index": 0, "stream_type": rs.stream.infrared, "stream_index": 1,
              "format": rs.format.y8, "width": 1280, "height": 720, "fps": 30}
    pick_b = {"sensor_index": 1, "stream_type": rs.stream.color, "stream_index": 0,
              "format": rs.format.bgr8, "width": 1280, "height": 720, "fps": 30}

    groups = resolve_and_group(device, pick_a, pick_b)

    assert len(groups) == 2  # two distinct sensors -> two groups
    sensors_in_groups = [g[0] for g in groups]
    assert ir_sensor in sensors_in_groups and color_sensor in sensors_in_groups
    for sensor, profiles in groups:
        assert len(profiles) == 1


def test_resolve_and_group_one_shared_sensor():
    left_profile = FakeProfile2(rs.stream.color, 1, rs.format.bgr8, 1280, 720, 30)
    right_profile = FakeProfile2(rs.stream.color, 2, rs.format.bgr8, 1280, 720, 30)
    shared_sensor = FakeSensor2(profiles=[left_profile, right_profile])
    device = FakeDevice([shared_sensor])
    pick_a = {"sensor_index": 0, "stream_type": rs.stream.color, "stream_index": 1,
              "format": rs.format.bgr8, "width": 1280, "height": 720, "fps": 30}
    pick_b = {"sensor_index": 0, "stream_type": rs.stream.color, "stream_index": 2,
              "format": rs.format.bgr8, "width": 1280, "height": 720, "fps": 30}

    groups = resolve_and_group(device, pick_a, pick_b)

    assert len(groups) == 1  # one shared sensor -> one group
    sensor, profiles = groups[0]
    assert sensor is shared_sensor
    assert len(profiles) == 2


# --- group_for_pick: isolates ONE resolved group for a caller (Calibration,
# ROI Select) that needs to capture/control just one of the two picked
# streams independently, e.g. for dual-LED-panel mode's sequential
# per-stream calibration (see gui/pages/calibration_page.py). ---

def test_group_for_pick_isolates_the_matching_sensor_group():
    ir_profile = FakeProfile2(rs.stream.infrared, 1, rs.format.y8, 1280, 720, 30)
    color_profile = FakeProfile2(rs.stream.color, 0, rs.format.bgr8, 1280, 720, 30)
    ir_sensor = FakeSensor2(profiles=[ir_profile])
    color_sensor = FakeSensor2(profiles=[color_profile])
    groups = [(ir_sensor, [ir_profile]), (color_sensor, [color_profile])]
    pick_a = {"sensor_index": 0, "stream_type": rs.stream.infrared, "stream_index": 1,
              "format": rs.format.y8, "width": 1280, "height": 720, "fps": 30}
    pick_b = {"sensor_index": 1, "stream_type": rs.stream.color, "stream_index": 0,
              "format": rs.format.bgr8, "width": 1280, "height": 720, "fps": 30}

    group_a = group_for_pick(groups, pick_a)
    group_b = group_for_pick(groups, pick_b)

    assert group_a == [(ir_sensor, [ir_profile])]
    assert group_b == [(color_sensor, [color_profile])]


def test_group_for_pick_returns_shared_group_for_both_picks_on_one_sensor():
    left_profile = FakeProfile2(rs.stream.color, 1, rs.format.bgr8, 1280, 720, 30)
    right_profile = FakeProfile2(rs.stream.color, 2, rs.format.bgr8, 1280, 720, 30)
    shared_sensor = FakeSensor2(profiles=[left_profile, right_profile])
    groups = [(shared_sensor, [left_profile, right_profile])]
    pick_a = {"sensor_index": 0, "stream_type": rs.stream.color, "stream_index": 1,
              "format": rs.format.bgr8, "width": 1280, "height": 720, "fps": 30}

    group = group_for_pick(groups, pick_a)

    assert group == [(shared_sensor, [left_profile, right_profile])]


def test_group_for_pick_raises_when_no_group_matches():
    ir_profile = FakeProfile2(rs.stream.infrared, 1, rs.format.y8, 1280, 720, 30)
    ir_sensor = FakeSensor2(profiles=[ir_profile])
    groups = [(ir_sensor, [ir_profile])]
    unrelated_pick = {"sensor_index": 5, "stream_type": rs.stream.color, "stream_index": 0,
                       "format": rs.format.bgr8, "width": 1280, "height": 720, "fps": 30}

    with pytest.raises(RuntimeError):
        group_for_pick(groups, unrelated_pick)


def test_resolve_and_group_raises_when_picks_are_the_same_stream():
    # Nothing anywhere else guards against Stream A and Stream B being
    # picked as the identical (stream_type, stream_index) - this is the
    # engine-layer backstop in case a future caller reaches resolve_and_group
    # without going through Stream Select's own GUI-level guard.
    profile = FakeProfile2(rs.stream.infrared, 1, rs.format.y8, 1280, 720, 30)
    sensor = FakeSensor2(profiles=[profile])
    device = FakeDevice([sensor])
    pick_a = {"sensor_index": 0, "stream_type": rs.stream.infrared, "stream_index": 1,
              "format": rs.format.y8, "width": 1280, "height": 720, "fps": 30}
    pick_b = dict(pick_a)

    with pytest.raises(RuntimeError):
        resolve_and_group(device, pick_a, pick_b)


def test_resolve_and_group_raises_runtime_error_not_stop_iteration_for_missing_profile():
    # A pick whose (stream_type, stream_index, format, width, height, fps)
    # doesn't match anything on its sensor (e.g. the device's profiles
    # changed since the pick was made - a re-plug or firmware mode switch)
    # must raise an informative RuntimeError, not a bare StopIteration whose
    # str() is '' - callers stringify this directly into a user-facing
    # message.
    sensor = FakeSensor2(profiles=[FakeProfile2(rs.stream.infrared, 1, rs.format.y8, 1280, 720, 30)])
    color_sensor = FakeSensor2(profiles=[FakeProfile2(rs.stream.color, 0, rs.format.bgr8, 1280, 720, 30)])
    device = FakeDevice([sensor, color_sensor])
    pick_a = {"sensor_index": 0, "stream_type": rs.stream.infrared, "stream_index": 1,
              "format": rs.format.y8, "width": 1920, "height": 1080, "fps": 60}  # no matching profile
    pick_b = {"sensor_index": 1, "stream_type": rs.stream.color, "stream_index": 0,
              "format": rs.format.bgr8, "width": 1280, "height": 720, "fps": 30}

    with pytest.raises(RuntimeError) as excinfo:
        resolve_and_group(device, pick_a, pick_b)
    assert str(excinfo.value)  # non-empty, informative message (not a bare StopIteration)


def test_set_emitter_enabled_true_when_supported():
    sensor = FakeOptionSensor(supported_options={rs.option.emitter_enabled})
    assert set_emitter_enabled(sensor, True) is True
    assert sensor.set_options[rs.option.emitter_enabled] == 1


def test_set_emitter_enabled_false_when_supported():
    sensor = FakeOptionSensor(supported_options={rs.option.emitter_enabled})
    assert set_emitter_enabled(sensor, False) is True
    assert sensor.set_options[rs.option.emitter_enabled] == 0


def test_set_emitter_enabled_returns_false_when_unsupported():
    sensor = FakeOptionSensor(supported_options=set())
    assert set_emitter_enabled(sensor, False) is False


def test_set_manual_exposure_sets_exposure_and_gain_and_disables_auto():
    sensor = FakeOptionSensor(supported_options={rs.option.enable_auto_exposure, rs.option.exposure, rs.option.gain})
    assert set_manual_exposure(sensor, exposure=150, gain=16) is True
    assert sensor.set_options[rs.option.enable_auto_exposure] == 0
    assert sensor.set_options[rs.option.exposure] == 150
    assert sensor.set_options[rs.option.gain] == 16


def test_set_manual_exposure_returns_false_when_fully_unsupported():
    sensor = FakeOptionSensor(supported_options=set())
    assert set_manual_exposure(sensor, exposure=150, gain=16) is False
    assert sensor.set_options == {}


def test_set_manual_exposure_returns_false_when_partially_unsupported():
    # exposure is supported but enable_auto_exposure and gain are not
    sensor = FakeOptionSensor(supported_options={rs.option.exposure})
    assert set_manual_exposure(sensor, exposure=150, gain=16) is False
    assert sensor.set_options == {}  # nothing should be set if guard fails


# --- stream_slug ---
#
# Only the two fields stream_slug actually reads (stream_type/stream_index)
# are needed - a plain pick dict, same shape list_video_stream_options_from_device
# produces, is enough; no fake profile/sensor/device required.

def test_stream_slug_appends_index_for_infrared():
    pick = {"stream_type": rs.stream.infrared, "stream_index": 1}
    assert stream_slug(pick) == "infrared1"


def test_stream_slug_appends_index_for_second_infrared():
    pick = {"stream_type": rs.stream.infrared, "stream_index": 2}
    assert stream_slug(pick) == "infrared2"


def test_stream_slug_omits_index_when_zero_for_color():
    # A single-RGB camera's color stream is stream_index 0 - this must slug to
    # "color", not "color0", to match domain/calibration.py's/config.yaml's
    # established slug scheme (tests/domain/test_calibration.py).
    pick = {"stream_type": rs.stream.color, "stream_index": 0}
    assert stream_slug(pick) == "color"


def test_stream_slug_appends_index_for_color_when_nonzero():
    pick = {"stream_type": rs.stream.color, "stream_index": 2}
    assert stream_slug(pick) == "color2"


# --- parse_camera_tests_config / resolve_camera_tests (settings.yaml's
# per-camera camera.stream_options named-test curation - see
# engine/streams.py docstrings) ---

def _raw_test(test_name, stream_a_identity, stream_b_identity, sensor_options):
    return {
        "test_name": test_name,
        "stream_a_identity": stream_a_identity,
        "stream_b_identity": stream_b_identity,
        "sensor_options": sensor_options,
    }


def test_parse_camera_tests_config_converts_names_to_real_enums():
    raw = [_raw_test(
        "IR1 vs IR2 sync",
        {"stream_type": "infrared", "stream_index": 1},
        {"stream_type": "infrared", "stream_index": 2},
        [{"stream_a": {"width": 1280, "height": 720, "fps": 30, "format": "y8"},
          "stream_b": {"width": 1280, "height": 720, "fps": 30, "format": "y8"}}],
    )]

    parsed = parse_camera_tests_config(raw)

    assert parsed == [{
        "test_name": "IR1 vs IR2 sync",
        "stream_a_identity": {"stream_type": rs.stream.infrared, "stream_index": 1},
        "stream_b_identity": {"stream_type": rs.stream.infrared, "stream_index": 2},
        "sensor_options": [{
            "stream_a": {"width": 1280, "height": 720, "fps": 30, "format": rs.format.y8},
            "stream_b": {"width": 1280, "height": 720, "fps": 30, "format": rs.format.y8},
        }],
    }]


def test_parse_camera_tests_config_rejects_unknown_stream_type():
    raw = [_raw_test(
        "bad test", {"stream_type": "depth", "stream_index": 0}, {"stream_type": "color", "stream_index": 0},
        [{"stream_a": {"width": 1280, "height": 720, "fps": 30, "format": "y8"},
          "stream_b": {"width": 1280, "height": 720, "fps": 30, "format": "bgr8"}}],
    )]
    with pytest.raises(ValueError):
        parse_camera_tests_config(raw)


def test_parse_camera_tests_config_rejects_unknown_format():
    raw = [_raw_test(
        "bad test", {"stream_type": "infrared", "stream_index": 1}, {"stream_type": "color", "stream_index": 0},
        [{"stream_a": {"width": 1280, "height": 720, "fps": 30, "format": "not_a_real_format"},
          "stream_b": {"width": 1280, "height": 720, "fps": 30, "format": "bgr8"}}],
    )]
    with pytest.raises(ValueError):
        parse_camera_tests_config(raw)


def _device_option(stream_type, stream_index, width, height, fps, format_, sensor_index=0):
    return {"sensor_index": sensor_index, "stream_type": stream_type, "stream_index": stream_index,
            "width": width, "height": height, "fps": fps, "format": format_}


def test_resolve_camera_tests_keeps_only_matching_sensor_options():
    # Simulates the real-world bug report: a device advertises the same
    # infrared resolution/fps in several redundant pixel formats
    # (y8/uyvy/bgr8) - the test only wants y8.
    device_options = [
        _device_option(rs.stream.infrared, 1, 1280, 720, 30, rs.format.y8),
        _device_option(rs.stream.infrared, 1, 1280, 720, 30, rs.format.uyvy),
        _device_option(rs.stream.color, 0, 1280, 720, 30, rs.format.bgr8),
    ]
    parsed_tests = [{
        "test_name": "IR vs RGB sync",
        "stream_a_identity": {"stream_type": rs.stream.infrared, "stream_index": 1},
        "stream_b_identity": {"stream_type": rs.stream.color, "stream_index": 0},
        "sensor_options": [{
            "stream_a": {"width": 1280, "height": 720, "fps": 30, "format": rs.format.y8},
            "stream_b": {"width": 1280, "height": 720, "fps": 30, "format": rs.format.bgr8},
        }],
    }]

    resolved = resolve_camera_tests(device_options, parsed_tests)

    assert len(resolved) == 1
    assert resolved[0]["test_name"] == "IR vs RGB sync"
    assert len(resolved[0]["options"]) == 1
    assert resolved[0]["options"][0]["pick_a"]["format"] == rs.format.y8
    assert resolved[0]["options"][0]["pick_b"]["format"] == rs.format.bgr8


def test_resolve_camera_tests_ignores_sensor_index_when_matching():
    # sensor_index is a live/device detail the config never specifies - a
    # sensor_options entry must still match regardless of which
    # sensor_index the device happens to report each side under.
    device_options = [
        _device_option(rs.stream.infrared, 1, 1280, 720, 30, rs.format.y8, sensor_index=3),
        _device_option(rs.stream.infrared, 2, 1280, 720, 30, rs.format.y8, sensor_index=3),
    ]
    parsed_tests = [{
        "test_name": "IR1 vs IR2 sync",
        "stream_a_identity": {"stream_type": rs.stream.infrared, "stream_index": 1},
        "stream_b_identity": {"stream_type": rs.stream.infrared, "stream_index": 2},
        "sensor_options": [{
            "stream_a": {"width": 1280, "height": 720, "fps": 30, "format": rs.format.y8},
            "stream_b": {"width": 1280, "height": 720, "fps": 30, "format": rs.format.y8},
        }],
    }]

    resolved = resolve_camera_tests(device_options, parsed_tests)

    assert len(resolved[0]["options"]) == 1
    assert resolved[0]["options"][0]["pick_a"]["sensor_index"] == 3


def test_resolve_camera_tests_drops_sensor_options_entry_when_either_side_unmatched():
    # A sensor_options entry with no matching device option on EITHER side
    # (e.g. this specific rig doesn't support that resolution) is dropped
    # from that test's options, not an error.
    device_options = [
        _device_option(rs.stream.infrared, 1, 1280, 720, 30, rs.format.y8),
        _device_option(rs.stream.color, 0, 1280, 720, 30, rs.format.bgr8),
    ]
    parsed_tests = [{
        "test_name": "IR vs RGB sync",
        "stream_a_identity": {"stream_type": rs.stream.infrared, "stream_index": 1},
        "stream_b_identity": {"stream_type": rs.stream.color, "stream_index": 0},
        "sensor_options": [
            {"stream_a": {"width": 1280, "height": 720, "fps": 30, "format": rs.format.y8},
             "stream_b": {"width": 1280, "height": 720, "fps": 30, "format": rs.format.bgr8}},
            {"stream_a": {"width": 1920, "height": 1080, "fps": 60, "format": rs.format.y8},  # doesn't match this device
             "stream_b": {"width": 1920, "height": 1080, "fps": 60, "format": rs.format.bgr8}},
        ],
    }]

    resolved = resolve_camera_tests(device_options, parsed_tests)

    assert len(resolved[0]["options"]) == 1


def test_resolve_camera_tests_includes_test_with_zero_matching_options():
    # A test that exists in config but has NOTHING matching this device
    # still appears in the returned list (with an empty "options"), so the
    # caller can tell "test exists, nothing usable on this rig" apart from
    # "test doesn't exist at all" - gui/main_window.py uses this to decide
    # whether to show an error.
    device_options = [_device_option(rs.stream.color, 0, 1280, 720, 30, rs.format.bgr8)]
    parsed_tests = [{
        "test_name": "IR1 vs IR2 sync",
        "stream_a_identity": {"stream_type": rs.stream.infrared, "stream_index": 1},
        "stream_b_identity": {"stream_type": rs.stream.infrared, "stream_index": 2},
        "sensor_options": [{
            "stream_a": {"width": 1280, "height": 720, "fps": 30, "format": rs.format.y8},
            "stream_b": {"width": 1280, "height": 720, "fps": 30, "format": rs.format.y8},
        }],
    }]

    resolved = resolve_camera_tests(device_options, parsed_tests)

    assert resolved == [{"test_name": "IR1 vs IR2 sync", "options": []}]


def test_resolve_camera_tests_preserves_config_order_for_tests_and_options():
    device_options = [
        _device_option(rs.stream.infrared, 1, 1280, 720, 30, rs.format.y8),
        _device_option(rs.stream.infrared, 2, 1280, 720, 30, rs.format.y8),
        _device_option(rs.stream.infrared, 1, 848, 480, 60, rs.format.y8),
        _device_option(rs.stream.infrared, 2, 848, 480, 60, rs.format.y8),
    ]
    parsed_tests = [{
        "test_name": "IR1 vs IR2 sync",
        "stream_a_identity": {"stream_type": rs.stream.infrared, "stream_index": 1},
        "stream_b_identity": {"stream_type": rs.stream.infrared, "stream_index": 2},
        "sensor_options": [
            {"stream_a": {"width": 848, "height": 480, "fps": 60, "format": rs.format.y8},
             "stream_b": {"width": 848, "height": 480, "fps": 60, "format": rs.format.y8}},
            {"stream_a": {"width": 1280, "height": 720, "fps": 30, "format": rs.format.y8},
             "stream_b": {"width": 1280, "height": 720, "fps": 30, "format": rs.format.y8}},
        ],
    }]

    resolved = resolve_camera_tests(device_options, parsed_tests)

    assert [o["pick_a"]["fps"] for o in resolved[0]["options"]] == [60, 30]


# --- ContinuousCapture._ordered_picks: which order the two streams are handed
# to config.enable_stream(). Pure/testable even though ContinuousCapture's
# real rs.pipeline() internals are hardware-only by design. Motivated by a
# real measured discrepancy - a reference script enabling COLOR first saw a
# ~3.5ms inter-sensor timestamp gap where this app, enabling stream_a (IR)
# first, consistently saw ~11.3ms on the same rig. ---

def _ir_pick(stream_index=1):
    return {"stream_type": rs.stream.infrared, "stream_index": stream_index,
            "width": 1280, "height": 720, "fps": 30, "format": rs.format.y8}


def _color_pick(stream_index=0):
    return {"stream_type": rs.stream.color, "stream_index": stream_index,
            "width": 1280, "height": 720, "fps": 30, "format": rs.format.bgr8}


def test_ordered_picks_puts_color_before_infrared():
    capture = ContinuousCapture("SN1", _ir_pick(), _color_pick(), color_stream_first=True)
    ordered = capture._ordered_picks()
    assert [p["stream_type"] for p in ordered] == [rs.stream.color, rs.stream.infrared]


def test_ordered_picks_leaves_color_first_pairing_untouched():
    capture = ContinuousCapture("SN1", _color_pick(), _ir_pick(), color_stream_first=True)
    ordered = capture._ordered_picks()
    assert [p["stream_type"] for p in ordered] == [rs.stream.color, rs.stream.infrared]


def test_ordered_picks_is_a_noop_for_two_infrared_streams():
    # Stable sort - a same-type pairing must keep pick_a/pick_b's own order,
    # since there's no color stream to promote.
    pick_a, pick_b = _ir_pick(1), _ir_pick(2)
    capture = ContinuousCapture("SN1", pick_a, pick_b, color_stream_first=True)
    assert capture._ordered_picks() == [pick_a, pick_b]


def test_ordered_picks_is_a_noop_for_two_color_streams():
    pick_a, pick_b = _color_pick(0), _color_pick(1)
    capture = ContinuousCapture("SN1", pick_a, pick_b, color_stream_first=True)
    assert capture._ordered_picks() == [pick_a, pick_b]


def test_ordered_picks_preserves_original_order_when_disabled():
    # The A/B "off" arm of the experiment - reproduces the pre-change
    # behavior exactly (stream_a enabled first, whatever type it is).
    pick_a, pick_b = _ir_pick(), _color_pick()
    capture = ContinuousCapture("SN1", pick_a, pick_b, color_stream_first=False)
    assert capture._ordered_picks() == [pick_a, pick_b]


def test_ordered_picks_never_changes_which_pick_is_stream_a():
    # Load-bearing safety property: reordering is ONLY about enable order.
    # _get_frame still resolves stream_a/stream_b from pick_a/pick_b, so the
    # data mapping must be completely unaffected by this setting.
    pick_a, pick_b = _ir_pick(), _color_pick()
    capture = ContinuousCapture("SN1", pick_a, pick_b, color_stream_first=True)
    capture._ordered_picks()
    assert capture.pick_a is pick_a
    assert capture.pick_b is pick_b
