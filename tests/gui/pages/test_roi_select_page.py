import pyrealsense2 as rs

from gui.pages.roi_select_page import _apply_camera_controls, stream_label


class FakeOptionRange:
    # .min/.max unbounded by default - set_manual_exposure clamps to these,
    # and none of this file's tests care about clamping specifically (see
    # tests/engine/test_streams.py for that coverage).
    def __init__(self, default, min_value=float("-inf"), max_value=float("inf")):
        self.default = default
        self.min = min_value
        self.max = max_value


class FakeSensor:
    """Same fake-sensor shape as tests/engine/test_streams.py's
    FakeOptionSensor - real pyrealsense2 sensors aren't constructible
    without hardware, and _apply_camera_controls calls the real
    engine.streams.set_emitter_enabled/enable_auto_exposure/
    set_manual_exposure, which need .supports()/.set_option()/.get_option()/
    .get_option_range(). get_option() defaults to 1 ("already auto") since
    none of this file's tests exercise enable_auto_exposure's was-manual
    restore gate - see test_streams.py's FakeOptionSensor for that."""

    def __init__(self, supported_options):
        self._supported = set(supported_options)
        self.set_options = {}

    def supports(self, option):
        return option in self._supported

    def set_option(self, option, value):
        self.set_options[option] = value

    def get_option(self, option):
        return self.set_options.get(option, 1)

    def get_option_range(self, option):
        return FakeOptionRange(0)


class FakeProfile:
    def __init__(self, stream_type):
        self._stream_type = stream_type

    def stream_type(self):
        return self._stream_type


class FakeVideoProfile:
    """Full profile fake matching engine.streams._pick_matches' exact
    interface (stream_type/stream_index/format/fps/
    as_video_stream_profile().width()/.height()) - needed only for
    exposure_for_group's per-stream routing test below. FakeProfile above
    (stream_type() only) still covers every emitter-gating test that
    doesn't exercise Manual exposure at all."""

    def __init__(self, pick):
        self._pick = pick

    def stream_type(self):
        return self._pick["stream_type"]

    def stream_index(self):
        return self._pick["stream_index"]

    def format(self):
        return self._pick["format"]

    def fps(self):
        return self._pick["fps"]

    def as_video_stream_profile(self):
        return self

    def width(self):
        return self._pick["width"]

    def height(self):
        return self._pick["height"]


def _global_controls(emitter_enabled=False, auto_exposure=True, exposure_a=None, exposure_b=None):
    return {
        "emitter_enabled": emitter_enabled, "auto_exposure": auto_exposure,
        "exposure_a": exposure_a, "exposure_b": exposure_b,
    }


# --- stream_label ---

def test_stream_label_formats_infrared_with_index():
    assert stream_label({"stream_type": rs.stream.infrared, "stream_index": 1}) == "Infrared 1"


def test_stream_label_formats_color():
    assert stream_label({"stream_type": rs.stream.color, "stream_index": 0}) == "Color 0"


# --- _apply_camera_controls (regression tests for Issue 6a: the global
# "Disable IR emitter" checkbox must not be attempted on a group with no
# infrared stream at all, or every pure color+color (Dual RGB) run gets a
# spurious "not supported" warning) ---

def test_apply_camera_controls_skips_emitter_for_color_only_group_no_warning():
    sensor = FakeSensor(supported_options={rs.option.enable_auto_exposure})
    groups = [(sensor, [FakeProfile(rs.stream.color), FakeProfile(rs.stream.color)])]

    warnings = _apply_camera_controls(groups, _global_controls(emitter_enabled=False, auto_exposure=True), None, None)

    assert warnings == []
    assert rs.option.emitter_enabled not in sensor.set_options


def test_apply_camera_controls_applies_emitter_for_infrared_group():
    sensor = FakeSensor(supported_options={rs.option.emitter_enabled, rs.option.enable_auto_exposure})
    groups = [(sensor, [FakeProfile(rs.stream.infrared)])]

    warnings = _apply_camera_controls(groups, _global_controls(emitter_enabled=False, auto_exposure=True), None, None)

    assert warnings == []
    assert sensor.set_options[rs.option.emitter_enabled] == 0  # emitter_enabled=False -> disabled -> 0


def test_apply_camera_controls_warns_when_infrared_group_lacks_emitter_support():
    sensor = FakeSensor(supported_options={rs.option.enable_auto_exposure})  # no emitter support despite being IR
    groups = [(sensor, [FakeProfile(rs.stream.infrared)])]

    warnings = _apply_camera_controls(groups, _global_controls(emitter_enabled=False, auto_exposure=True), None, None)

    assert len(warnings) == 1
    assert "emitter" in warnings[0].lower()


def test_apply_camera_controls_gates_per_group_not_globally():
    # Mixed scenario in the SAME call: one all-color group (skip emitter,
    # no warning) and one infrared group (apply emitter there) - proves the
    # gate is per-group, not an all-or-nothing decision for the whole run.
    color_sensor = FakeSensor(supported_options={rs.option.enable_auto_exposure})
    ir_sensor = FakeSensor(supported_options={rs.option.emitter_enabled, rs.option.enable_auto_exposure})
    groups = [
        (color_sensor, [FakeProfile(rs.stream.color)]),
        (ir_sensor, [FakeProfile(rs.stream.infrared)]),
    ]

    warnings = _apply_camera_controls(groups, _global_controls(emitter_enabled=False, auto_exposure=True), None, None)

    assert warnings == []
    assert rs.option.emitter_enabled not in color_sensor.set_options
    assert ir_sensor.set_options[rs.option.emitter_enabled] == 0


def test_apply_camera_controls_applies_exposure_to_every_group_regardless_of_stream_type():
    # auto_exposure is NOT gated by stream type, unlike emitter - exposure
    # control is meaningful for color sensors too.
    color_sensor = FakeSensor(supported_options={rs.option.enable_auto_exposure})
    warnings = _apply_camera_controls(
        [(color_sensor, [FakeProfile(rs.stream.color)])],
        _global_controls(emitter_enabled=None, auto_exposure=True),
        None, None,
    )
    assert warnings == []
    assert color_sensor.set_options[rs.option.enable_auto_exposure] == 1


# --- Manual exposure routes each stream's OWN value (exposure_a/exposure_b)
# to whichever resolved sensor group actually contains that stream, instead
# of applying one shared value to both - different sensors (IR vs RGB, or
# two different IR sensors) have different brightness characteristics. ---

def test_apply_camera_controls_routes_separate_exposure_per_stream_on_distinct_sensors():
    pick_a = {"stream_type": rs.stream.infrared, "stream_index": 1,
              "width": 1280, "height": 720, "fps": 30, "format": "y8"}
    pick_b = {"stream_type": rs.stream.color, "stream_index": 0,
              "width": 1280, "height": 720, "fps": 30, "format": "bgr8"}
    sensor_a = FakeSensor(supported_options={rs.option.enable_auto_exposure, rs.option.exposure})
    sensor_b = FakeSensor(supported_options={rs.option.enable_auto_exposure, rs.option.exposure})
    groups = [
        (sensor_a, [FakeVideoProfile(pick_a)]),
        (sensor_b, [FakeVideoProfile(pick_b)]),
    ]

    warnings = _apply_camera_controls(
        groups,
        _global_controls(emitter_enabled=None, auto_exposure=False, exposure_a=1111, exposure_b=2222),
        pick_a, pick_b,
    )

    assert warnings == []
    assert sensor_a.set_options[rs.option.exposure] == 1111
    assert sensor_b.set_options[rs.option.exposure] == 2222


def test_apply_camera_controls_shared_sensor_gets_stream_as_exposure():
    # Dual-RGB shape - pick_a and pick_b share ONE physical sensor. Only
    # one real exposure value can apply in hardware regardless of what the
    # UI offers per stream - Stream A's value wins (see
    # engine.streams.exposure_for_group's own docstring for why).
    pick_a = {"stream_type": rs.stream.color, "stream_index": 1,
              "width": 1280, "height": 720, "fps": 30, "format": "bgr8"}
    pick_b = {"stream_type": rs.stream.color, "stream_index": 2,
              "width": 1280, "height": 720, "fps": 30, "format": "bgr8"}
    shared_sensor = FakeSensor(supported_options={rs.option.enable_auto_exposure, rs.option.exposure})
    groups = [(shared_sensor, [FakeVideoProfile(pick_a), FakeVideoProfile(pick_b)])]

    _apply_camera_controls(
        groups, _global_controls(auto_exposure=False, exposure_a=1111, exposure_b=2222), pick_a, pick_b,
    )

    assert shared_sensor.set_options[rs.option.exposure] == 1111
