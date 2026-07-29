import pyrealsense2 as rs

from gui.pages.stream_config_page import StreamConfigPage, group_camera_controls, _stream_option_label


IR1 = {"sensor_index": 0, "stream_type": rs.stream.infrared, "stream_index": 1,
       "format": rs.format.y8, "width": 1280, "height": 720, "fps": 30}
IR2 = {"sensor_index": 0, "stream_type": rs.stream.infrared, "stream_index": 2,
       "format": rs.format.y8, "width": 1280, "height": 720, "fps": 30}
COLOR0 = {"sensor_index": 1, "stream_type": rs.stream.color, "stream_index": 0,
          "format": rs.format.bgr8, "width": 1280, "height": 720, "fps": 30}
COLOR1 = {"sensor_index": 0, "stream_type": rs.stream.color, "stream_index": 1,
          "format": rs.format.bgr8, "width": 1280, "height": 720, "fps": 30}
COLOR2 = {"sensor_index": 0, "stream_type": rs.stream.color, "stream_index": 2,
          "format": rs.format.bgr8, "width": 1280, "height": 720, "fps": 30}


# --- _stream_option_label ---

def test_stream_option_label_formats_infrared_with_stream_index():
    assert _stream_option_label(IR1) == "Infrared 1 - 1280x720@30fps (y8)"


def test_stream_option_label_formats_color():
    assert _stream_option_label(COLOR0) == "Color - 1280x720@30fps (bgr8)"


# --- group_camera_controls (pure, no Qt/device needed) ---

def test_group_camera_controls_same_sensor_index_returns_one_group():
    groups = group_camera_controls(COLOR1, COLOR2)
    assert len(groups) == 1
    assert groups[0]["sensor_indices"] == [0]


def test_group_camera_controls_different_sensor_index_returns_two_groups():
    groups = group_camera_controls(IR1, COLOR0)
    assert len(groups) == 2
    assert groups[0]["sensor_indices"] == [0]
    assert groups[1]["sensor_indices"] == [1]


def test_group_camera_controls_flags_infrared_group_only():
    groups = group_camera_controls(IR1, COLOR0)
    assert groups[0]["has_infrared"] is True
    assert groups[1]["has_infrared"] is False


def test_group_camera_controls_shared_sensor_with_any_infrared_pick_flags_group():
    groups = group_camera_controls(IR1, IR2)
    assert len(groups) == 1
    assert groups[0]["has_infrared"] is True


def test_group_camera_controls_shared_sensor_color_only_not_flagged():
    groups = group_camera_controls(COLOR1, COLOR2)
    assert groups[0]["has_infrared"] is False


# --- populate() ---

def test_populate_lists_every_stream_option(qapp):
    page = StreamConfigPage()
    options = [IR1, IR2, COLOR0]
    page.populate(ctx=None, device_serial="123", stream_options=options)
    assert page.combo_a.count() == 3
    assert page.combo_b.count() == 3


def test_populate_preselects_preferred_a_and_b(qapp):
    page = StreamConfigPage()
    page.populate(
        ctx=None, device_serial="123", stream_options=[IR1, IR2, COLOR0],
        preferred_a={"stream_index": 2, "stream_type": rs.stream.infrared},
        preferred_b={"stream_type": rs.stream.color},
    )
    assert page.combo_a.currentData() == IR2
    assert page.combo_b.currentData() == COLOR0


def test_populate_leaves_default_selection_when_preferred_not_found(qapp):
    page = StreamConfigPage()
    page.populate(
        ctx=None, device_serial="123", stream_options=[IR1, COLOR0],
        preferred_a={"width": 9999},
    )
    assert page.combo_a.currentData() == IR1  # unchanged, first item


# --- camera-control-group widget count, driven by group_camera_controls ---

def test_selecting_streams_on_the_same_sensor_shows_one_camera_control_group(qapp):
    page = StreamConfigPage()
    shared = [COLOR1, COLOR2]
    page.populate(ctx=None, device_serial="123", stream_options=shared)
    page.combo_a.setCurrentIndex(0)
    page.combo_b.setCurrentIndex(1)
    page._refresh_camera_control_groups()  # recomputes resolve_and_group-based grouping for UI layout
    assert page.camera_control_group_count() == 1  # same sensor -> one control group, not two


def test_selecting_streams_on_different_sensors_shows_two_camera_control_groups(qapp):
    page = StreamConfigPage()
    page.populate(ctx=None, device_serial="123", stream_options=[IR1, COLOR0])
    page.combo_a.setCurrentIndex(0)  # IR1, sensor 0
    page.combo_b.setCurrentIndex(1)  # COLOR0, sensor 1
    page._refresh_camera_control_groups()
    assert page.camera_control_group_count() == 2


def test_emitter_checkbox_only_shown_for_infrared_group(qapp):
    page = StreamConfigPage()
    page.populate(ctx=None, device_serial="123", stream_options=[IR1, COLOR0])
    page.combo_a.setCurrentIndex(0)
    page.combo_b.setCurrentIndex(1)
    page._refresh_camera_control_groups()
    ir_group = page._camera_control_widgets[0]
    color_group = page._camera_control_widgets[1]
    assert ir_group["emitter_checkbox"] is not None
    assert color_group["emitter_checkbox"] is None


# --- config_chosen payload: (pick_a, pick_b, camera_controls) ---

def test_next_emits_picks_and_default_auto_exposure_camera_controls(qapp):
    page = StreamConfigPage()
    page.populate(ctx=None, device_serial="123", stream_options=[IR1, COLOR0])
    page.combo_a.setCurrentIndex(0)
    page.combo_b.setCurrentIndex(1)

    received = []
    page.config_chosen.connect(lambda payload: received.append(payload))
    page._on_next_clicked()

    assert len(received) == 1
    pick_a, pick_b, camera_controls = received[0]
    assert pick_a == IR1
    assert pick_b == COLOR0
    assert len(camera_controls) == 2
    assert camera_controls[0]["sensor_indices"] == [0]
    assert camera_controls[0]["auto_exposure"] is True
    assert camera_controls[0]["exposure"] is None
    assert camera_controls[0]["gain"] is None
    assert camera_controls[0]["emitter_enabled"] is True  # checkbox unchecked -> emitter left enabled
    assert camera_controls[1]["emitter_enabled"] is None  # no infrared in this group -> N/A


def test_manual_exposure_selection_reports_spinbox_values(qapp):
    page = StreamConfigPage()
    page.populate(ctx=None, device_serial="123", stream_options=[COLOR1, COLOR2])
    page.combo_a.setCurrentIndex(0)
    page.combo_b.setCurrentIndex(1)

    group = page._camera_control_widgets[0]
    group["manual_radio"].setChecked(True)
    group["exposure_spin"].setValue(5000)
    group["gain_spin"].setValue(32)

    received = []
    page.config_chosen.connect(lambda payload: received.append(payload))
    page._on_next_clicked()

    _, _, camera_controls = received[0]
    assert camera_controls[0]["auto_exposure"] is False
    assert camera_controls[0]["exposure"] == 5000
    assert camera_controls[0]["gain"] == 32


def test_checked_disable_emitter_checkbox_reports_emitter_disabled(qapp):
    page = StreamConfigPage()
    page.populate(ctx=None, device_serial="123", stream_options=[IR1, COLOR0])
    page.combo_a.setCurrentIndex(0)
    page.combo_b.setCurrentIndex(1)

    ir_group = page._camera_control_widgets[0]
    ir_group["emitter_checkbox"].setChecked(True)

    received = []
    page.config_chosen.connect(lambda payload: received.append(payload))
    page._on_next_clicked()

    _, _, camera_controls = received[0]
    assert camera_controls[0]["emitter_enabled"] is False
