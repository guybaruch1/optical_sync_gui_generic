import pyrealsense2 as rs

from gui.pages.roi_select_page import _apply_camera_controls, stream_label


class FakeSensor:
    """Same fake-sensor shape as tests/engine/test_streams.py's
    FakeOptionSensor - real pyrealsense2 sensors aren't constructible
    without hardware, and _apply_camera_controls calls the real
    engine.streams.set_emitter_enabled/enable_auto_exposure/
    set_manual_exposure, which only need .supports()/.set_option()."""

    def __init__(self, supported_options):
        self._supported = set(supported_options)
        self.set_options = {}

    def supports(self, option):
        return option in self._supported

    def set_option(self, option, value):
        self.set_options[option] = value


class FakeProfile:
    def __init__(self, stream_type):
        self._stream_type = stream_type

    def stream_type(self):
        return self._stream_type


def _global_controls(emitter_enabled=False, auto_exposure=True, exposure=None, gain=None):
    return {"emitter_enabled": emitter_enabled, "auto_exposure": auto_exposure, "exposure": exposure, "gain": gain}


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

    warnings = _apply_camera_controls(groups, _global_controls(emitter_enabled=False, auto_exposure=True))

    assert warnings == []
    assert rs.option.emitter_enabled not in sensor.set_options


def test_apply_camera_controls_applies_emitter_for_infrared_group():
    sensor = FakeSensor(supported_options={rs.option.emitter_enabled, rs.option.enable_auto_exposure})
    groups = [(sensor, [FakeProfile(rs.stream.infrared)])]

    warnings = _apply_camera_controls(groups, _global_controls(emitter_enabled=False, auto_exposure=True))

    assert warnings == []
    assert sensor.set_options[rs.option.emitter_enabled] == 0  # emitter_enabled=False -> disabled -> 0


def test_apply_camera_controls_warns_when_infrared_group_lacks_emitter_support():
    sensor = FakeSensor(supported_options={rs.option.enable_auto_exposure})  # no emitter support despite being IR
    groups = [(sensor, [FakeProfile(rs.stream.infrared)])]

    warnings = _apply_camera_controls(groups, _global_controls(emitter_enabled=False, auto_exposure=True))

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

    warnings = _apply_camera_controls(groups, _global_controls(emitter_enabled=False, auto_exposure=True))

    assert warnings == []
    assert rs.option.emitter_enabled not in color_sensor.set_options
    assert ir_sensor.set_options[rs.option.emitter_enabled] == 0


def test_apply_camera_controls_applies_exposure_to_every_group_regardless_of_stream_type():
    # auto_exposure/gain are NOT gated by stream type, unlike emitter -
    # exposure control is meaningful for color sensors too.
    color_sensor = FakeSensor(supported_options={rs.option.enable_auto_exposure})
    warnings = _apply_camera_controls(
        [(color_sensor, [FakeProfile(rs.stream.color)])],
        _global_controls(emitter_enabled=None, auto_exposure=True),
    )
    assert warnings == []
    assert color_sensor.set_options[rs.option.enable_auto_exposure] == 1
