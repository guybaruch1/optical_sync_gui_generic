import threading
import time

import pytest
import pyrealsense2 as rs
from engine.streams import (
    list_devices, list_supported_profiles, match_profile, capture_synced_frame_pair,
    disable_ir_emitter, enable_auto_exposure,
    list_video_stream_options_from_device, resolve_and_group,
    set_emitter_enabled, set_manual_exposure,
)


class FakeOptionSensor:
    """Fake sensor exposing just enough of the .supports()/.set_option() API
    for disable_ir_emitter/enable_auto_exposure - real pyrealsense2 sensors
    aren't constructible without hardware."""

    def __init__(self, supported_options):
        self._supported_options = set(supported_options)
        self.set_options = {}

    def supports(self, option):
        return option in self._supported_options

    def set_option(self, option, value):
        self.set_options[option] = value


def test_disable_ir_emitter_sets_option_off_when_supported():
    sensor = FakeOptionSensor(supported_options={rs.option.emitter_enabled})
    assert disable_ir_emitter(sensor) is True
    assert sensor.set_options[rs.option.emitter_enabled] == 0


def test_disable_ir_emitter_returns_false_when_unsupported():
    sensor = FakeOptionSensor(supported_options=set())
    assert disable_ir_emitter(sensor) is False
    assert sensor.set_options == {}


def test_enable_auto_exposure_sets_option_on_when_supported():
    sensor = FakeOptionSensor(supported_options={rs.option.enable_auto_exposure})
    assert enable_auto_exposure(sensor) is True
    assert sensor.set_options[rs.option.enable_auto_exposure] == 1


def test_enable_auto_exposure_returns_false_when_unsupported():
    # Callers rely on this to warn the operator (the same way they already do
    # for disable_ir_emitter) instead of silently leaving auto-exposure
    # however it was.
    sensor = FakeOptionSensor(supported_options=set())
    assert enable_auto_exposure(sensor) is False
    assert sensor.set_options == {}


class FakeVideoProfile:
    def __init__(self, width, height):
        self._width = width
        self._height = height

    def width(self):
        return self._width

    def height(self):
        return self._height


class FakeProfile:
    def __init__(self, stream_type, stream_index, fmt, width, height, fps):
        self._stream_type = stream_type
        self._stream_index = stream_index
        self._fmt = fmt
        self._fps = fps
        self._video = FakeVideoProfile(width, height)

    def stream_type(self):
        return self._stream_type

    def stream_index(self):
        return self._stream_index

    def format(self):
        return self._fmt

    def fps(self):
        return self._fps

    def as_video_stream_profile(self):
        return self._video


class FakeSensor:
    def __init__(self, profiles):
        self.profiles = profiles


def test_list_supported_profiles_filters_by_stream_and_format():
    sensor = FakeSensor(profiles=[
        FakeProfile("infrared", 0, "y8", 1280, 720, 30),
        FakeProfile("infrared", 0, "y8", 640, 480, 60),
        FakeProfile("color", 0, "yuyv", 1280, 720, 30),
    ])
    result = list_supported_profiles(sensor, "infrared", "y8", 0)
    assert set(result) == {(1280, 720, 30), (640, 480, 60)}


def test_match_profile_finds_exact_match():
    target = FakeProfile("infrared", 0, "y8", 1280, 720, 30)
    sensor = FakeSensor(profiles=[FakeProfile("infrared", 0, "y8", 640, 480, 60), target])
    matched = match_profile(sensor, "infrared", "y8", 1280, 720, 30, 0)
    assert matched is target


def test_match_profile_raises_when_nothing_matches():
    sensor = FakeSensor(profiles=[FakeProfile("infrared", 0, "y8", 640, 480, 60)])
    with pytest.raises(RuntimeError):
        match_profile(sensor, "infrared", "y8", 1280, 720, 30, 0)


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


def test_list_supported_profiles_filters_by_stream_index():
    sensor = FakeSensor(profiles=[
        FakeProfile(rs.stream.color, 1, rs.format.bgr8, 1280, 720, 30),
        FakeProfile(rs.stream.color, 2, rs.format.bgr8, 1280, 720, 30),
    ])
    result = list_supported_profiles(sensor, rs.stream.color, rs.format.bgr8, stream_index=1)
    assert result == [(1280, 720, 30)]


def test_match_profile_finds_exact_match_for_given_stream_index():
    target = FakeProfile(rs.stream.color, 2, rs.format.bgr8, 1280, 720, 30)
    sensor = FakeSensor(profiles=[FakeProfile(rs.stream.color, 1, rs.format.bgr8, 1280, 720, 30), target])
    matched = match_profile(sensor, rs.stream.color, rs.format.bgr8, 1280, 720, 30, stream_index=2)
    assert matched is target


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
# Renamed from the brief's FakeVideoProfile/FakeProfile/FakeSensor to
# FakeVideoProfile2/FakeProfile2/FakeSensor2 (with a FakeDevice added) to
# avoid clashing with the same-named, differently-shaped fakes already
# defined above in this file for list_supported_profiles/match_profile/
# capture_synced_frame_pair - those are module-level class names, and a
# later class statement redefining them would silently break the earlier
# tests (which look up the name at call time, after the whole module has
# finished importing).

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
