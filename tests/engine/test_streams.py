import threading
import time

import pytest
import pyrealsense2 as rs
from engine.streams import (
    list_supported_profiles, match_profile, capture_synced_frame_pair,
    disable_ir_emitter, enable_auto_exposure,
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
    def __init__(self, stream_type, fmt, width, height, fps):
        self._stream_type = stream_type
        self._fmt = fmt
        self._fps = fps
        self._video = FakeVideoProfile(width, height)

    def stream_type(self):
        return self._stream_type

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
        FakeProfile("infrared", "y8", 1280, 720, 30),
        FakeProfile("infrared", "y8", 640, 480, 60),
        FakeProfile("color", "yuyv", 1280, 720, 30),
    ])
    result = list_supported_profiles(sensor, "infrared", "y8")
    assert set(result) == {(1280, 720, 30), (640, 480, 60)}


def test_match_profile_finds_exact_match():
    target = FakeProfile("infrared", "y8", 1280, 720, 30)
    sensor = FakeSensor(profiles=[FakeProfile("infrared", "y8", 640, 480, 60), target])
    matched = match_profile(sensor, "infrared", "y8", 1280, 720, 30)
    assert matched is target


def test_match_profile_raises_when_nothing_matches():
    sensor = FakeSensor(profiles=[FakeProfile("infrared", "y8", 640, 480, 60)])
    with pytest.raises(RuntimeError):
        match_profile(sensor, "infrared", "y8", 1280, 720, 30)


class _FakeFrame:
    def __init__(self, stream_type, data):
        self._stream_type = stream_type
        self._data = data

    def get_profile(self):
        return self

    def stream_type(self):
        return self._stream_type

    def get_data(self):
        return self._data


class _FakeStreamingSensor:
    """Delivers frames continuously on a background thread once started,
    like a real sensor's callback - unlike a synchronous fake, this doesn't
    deliver everything before capture_synced_frame_pair's counter reset
    happens, so it actually exercises the reset-then-wait-for-fresh-frames
    control flow instead of trivially satisfying it."""

    def __init__(self, stream_type):
        self.stream_type = stream_type
        self._running = False
        self._thread = None

    def open(self, profiles):
        pass

    def start(self, callback):
        self._running = True

        def deliver_loop():
            counter = 0
            while self._running:
                counter += 1
                callback(_FakeFrame(self.stream_type, "frame-{}".format(counter).encode()))
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


def test_capture_synced_frame_pair_calls_trigger_once_and_returns_frames():
    ir_sensor = _FakeStreamingSensor(rs.stream.infrared)
    color_sensor = _FakeStreamingSensor(rs.stream.color)
    triggered = {"count": 0}

    def on_both_streaming():
        triggered["count"] += 1

    ir_frame, rgb_frame = capture_synced_frame_pair(
        ir_sensor, None, color_sensor, None,
        on_both_streaming=on_both_streaming, settle_frames=5, timeout_s=5.0,
    )

    assert triggered["count"] == 1
    assert ir_frame is not None
    assert rgb_frame is not None


def test_capture_synced_frame_pair_works_without_a_trigger_callback():
    ir_sensor = _FakeStreamingSensor(rs.stream.infrared)
    color_sensor = _FakeStreamingSensor(rs.stream.color)

    ir_frame, rgb_frame = capture_synced_frame_pair(
        ir_sensor, None, color_sensor, None,
        on_both_streaming=None, settle_frames=5, timeout_s=5.0,
    )

    assert ir_frame is not None
    assert rgb_frame is not None


def test_capture_synced_frame_pair_raises_on_timeout_when_no_frames_arrive():
    ir_sensor = _FakeNonDeliveringSensor()
    color_sensor = _FakeNonDeliveringSensor()

    with pytest.raises(RuntimeError):
        capture_synced_frame_pair(
            ir_sensor, None, color_sensor, None, settle_frames=5, timeout_s=0.2,
        )
