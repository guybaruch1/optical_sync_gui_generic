from unittest.mock import MagicMock

import pyrealsense2 as rs

from gui.pages.stream_config_page import StreamConfigPage, _sensor_option_label


IR1 = {"sensor_index": 0, "stream_type": rs.stream.infrared, "stream_index": 1,
       "format": rs.format.y8, "width": 1280, "height": 720, "fps": 30}
IR2 = {"sensor_index": 0, "stream_type": rs.stream.infrared, "stream_index": 2,
       "format": rs.format.y8, "width": 1280, "height": 720, "fps": 30}
IR1_SMALL = {"sensor_index": 0, "stream_type": rs.stream.infrared, "stream_index": 1,
             "format": rs.format.y8, "width": 848, "height": 480, "fps": 60}
IR2_SMALL = {"sensor_index": 0, "stream_type": rs.stream.infrared, "stream_index": 2,
             "format": rs.format.y8, "width": 848, "height": 480, "fps": 60}
COLOR0 = {"sensor_index": 1, "stream_type": rs.stream.color, "stream_index": 0,
          "format": rs.format.bgr8, "width": 1280, "height": 720, "fps": 30}


def _tests(*test_specs):
    """test_specs: list of (test_name, [(pick_a, pick_b), ...]) tuples."""
    return [
        {"test_name": name, "options": [{"pick_a": a, "pick_b": b} for a, b in options]}
        for name, options in test_specs
    ]


# --- _sensor_option_label ---

def test_sensor_option_label_collapses_identical_format():
    label = _sensor_option_label({"pick_a": IR1, "pick_b": IR2})
    assert label == "1280x720 @ 30fps (y8)"


def test_sensor_option_label_shows_both_formats_when_they_differ():
    label = _sensor_option_label({"pick_a": IR1, "pick_b": COLOR0})
    assert label == "1280x720 @ 30fps (IR: y8, RGB: bgr8)"


def test_sensor_option_label_falls_back_to_full_description_when_resolution_differs():
    other = dict(COLOR0, width=640, height=480, fps=60)
    label = _sensor_option_label({"pick_a": IR1, "pick_b": other})
    assert label == "1280x720@30fps (IR, y8) vs 640x480@60fps (RGB, bgr8)"


# --- populate(): Test combo drives Sensor Options combo ---

def test_populate_lists_test_names(qapp):
    page = StreamConfigPage()
    page.populate(ctx=None, device_serial="123", tests=_tests(
        ("IR1 vs IR2 sync", [(IR1, IR2)]),
        ("IR vs RGB sync", [(IR1, COLOR0)]),
    ))
    assert page.combo_test.count() == 2
    assert page.combo_test.itemText(0) == "IR1 vs IR2 sync"
    assert page.combo_test.itemText(1) == "IR vs RGB sync"


def test_populate_populates_sensor_options_for_default_selected_test(qapp):
    page = StreamConfigPage()
    page.populate(ctx=None, device_serial="123", tests=_tests(
        ("IR1 vs IR2 sync", [(IR1, IR2), (IR1_SMALL, IR2_SMALL)]),
        ("IR vs RGB sync", [(IR1, COLOR0)]),
    ))
    assert page.combo_sensor_options.count() == 2  # first test's options, not the second's


def test_switching_test_repopulates_sensor_options(qapp):
    page = StreamConfigPage()
    page.populate(ctx=None, device_serial="123", tests=_tests(
        ("IR1 vs IR2 sync", [(IR1, IR2), (IR1_SMALL, IR2_SMALL)]),
        ("IR vs RGB sync", [(IR1, COLOR0)]),
    ))
    page.combo_test.setCurrentIndex(1)
    assert page.combo_sensor_options.count() == 1
    assert page.pick_a == IR1
    assert page.pick_b == COLOR0


def test_populate_preselects_preferred_test_name(qapp):
    page = StreamConfigPage()
    page.populate(
        ctx=None, device_serial="123",
        tests=_tests(("IR1 vs IR2 sync", [(IR1, IR2)]), ("IR vs RGB sync", [(IR1, COLOR0)])),
        preferred_test_name="IR vs RGB sync",
    )
    assert page.combo_test.currentIndex() == 1
    assert page.current_test_name == "IR vs RGB sync"


def test_populate_defaults_to_first_test_when_preferred_test_name_not_found(qapp):
    page = StreamConfigPage()
    page.populate(
        ctx=None, device_serial="123",
        tests=_tests(("IR1 vs IR2 sync", [(IR1, IR2)]), ("IR vs RGB sync", [(IR1, COLOR0)])),
        preferred_test_name="Some test that doesn't exist",
    )
    assert page.current_test_name == "IR1 vs IR2 sync"


def test_populate_preselects_sensor_option_matching_preferred_a(qapp):
    page = StreamConfigPage()
    page.populate(
        ctx=None, device_serial="123",
        tests=_tests(("IR1 vs IR2 sync", [(IR1, IR2), (IR1_SMALL, IR2_SMALL)])),
        preferred_a={"width": 848, "height": 480, "fps": 60},
    )
    assert page.pick_a == IR1_SMALL
    assert page.pick_b == IR2_SMALL


def test_populate_leaves_default_sensor_option_when_preferred_not_found(qapp):
    page = StreamConfigPage()
    page.populate(
        ctx=None, device_serial="123",
        tests=_tests(("IR1 vs IR2 sync", [(IR1, IR2), (IR1_SMALL, IR2_SMALL)])),
        preferred_a={"width": 9999},
    )
    assert page.pick_a == IR1  # unchanged, first item


# --- .pick_a / .pick_b / .current_test_name accessors (documented produced
# interface - gui/main_window.py reads these) ---

def test_pick_a_and_pick_b_return_current_sensor_option_selection(qapp):
    page = StreamConfigPage()
    page.populate(ctx=None, device_serial="123", tests=_tests(
        ("IR1 vs IR2 sync", [(IR1, IR2), (IR1_SMALL, IR2_SMALL)]),
    ))
    page.combo_sensor_options.setCurrentIndex(1)
    assert page.pick_a == IR1_SMALL
    assert page.pick_b == IR2_SMALL


def test_pick_a_and_pick_b_track_combo_changes_live(qapp):
    page = StreamConfigPage()
    page.populate(ctx=None, device_serial="123", tests=_tests(
        ("IR1 vs IR2 sync", [(IR1, IR2), (IR1_SMALL, IR2_SMALL)]),
    ))
    assert page.pick_a == IR1  # default: first item
    page.combo_sensor_options.setCurrentIndex(1)
    assert page.pick_a == IR1_SMALL  # updates without re-calling populate()


def test_pick_a_and_pick_b_are_none_before_any_test_populated(qapp):
    page = StreamConfigPage()
    assert page.pick_a is None
    assert page.pick_b is None
    assert page.current_test_name is None


# --- camera controls: ONE global block, always present regardless of the
# currently selected test/sensor options ---

def test_camera_controls_group_always_present_regardless_of_picks(qapp):
    page = StreamConfigPage()
    page.populate(ctx=None, device_serial="123", tests=_tests(("IR1 vs IR2 sync", [(IR1, IR2)])))
    assert page._camera_controls["emitter_checkbox"] is not None
    assert page._camera_controls["group_box"] is not None


# --- preferred_camera_controls: lets Edit continue with this camera's own
# previous camera-control choices instead of the hardcoded widget defaults -
# always applied explicitly (never left as-is), same reasoning as
# preferred_dual_panel, since this page's one instance is reused across
# every camera's own sub-flow visit. ---

def test_populate_defaults_camera_controls_when_none_given(qapp):
    page = StreamConfigPage()
    page.populate(ctx=None, device_serial="123", tests=_tests(("IR1 vs IR2 sync", [(IR1, IR2)])))
    assert page._camera_controls["emitter_checkbox"].isChecked()  # default: emitter disabled
    assert page._camera_controls["auto_radio"].isChecked()


def test_populate_applies_preferred_camera_controls(qapp):
    page = StreamConfigPage()
    page.populate(
        ctx=None, device_serial="123", tests=_tests(("IR1 vs IR2 sync", [(IR1, IR2)])),
        preferred_camera_controls={
            "emitter_enabled": True, "auto_exposure": False, "exposure_a": 1234, "exposure_b": 5678,
        },
    )
    # emitter_checkbox is labeled "Disable IR emitter" - emitter_enabled
    # True means the emitter is ON, i.e. the checkbox is UNCHECKED.
    assert not page._camera_controls["emitter_checkbox"].isChecked()
    assert page._camera_controls["manual_radio"].isChecked()
    assert page._camera_controls["exposure_a_spin"].value() == 1234
    assert page._camera_controls["exposure_b_spin"].value() == 5678


def test_populate_resets_camera_controls_when_not_preselected(qapp):
    # Guards against stale carryover, same reasoning as the equivalent
    # dual-panel test above.
    page = StreamConfigPage()
    page.populate(
        ctx=None, device_serial="123", tests=_tests(("IR1 vs IR2 sync", [(IR1, IR2)])),
        preferred_camera_controls={
            "emitter_enabled": True, "auto_exposure": False, "exposure_a": 1234, "exposure_b": 5678,
        },
    )
    assert page._camera_controls["manual_radio"].isChecked()

    page.populate(ctx=None, device_serial="456", tests=_tests(("IR1 vs IR2 sync", [(IR1, IR2)])))

    assert page._camera_controls["auto_radio"].isChecked()


# --- config_chosen payload: (pick_a, pick_b, camera_controls) - no "test"
# concept in the emitted payload at all ---

def test_next_emits_picks_and_default_auto_exposure_camera_controls(qapp):
    page = StreamConfigPage()
    page.populate(ctx=None, device_serial="123", tests=_tests(("IR vs RGB sync", [(IR1, COLOR0)])))

    received = []
    page.config_chosen.connect(lambda payload: received.append(payload))
    page._on_next_clicked()

    assert len(received) == 1
    pick_a, pick_b, camera_controls = received[0]
    assert pick_a == IR1
    assert pick_b == COLOR0
    assert camera_controls["auto_exposure"] is True
    assert camera_controls["exposure_a"] is None
    assert camera_controls["exposure_b"] is None
    assert "gain" not in camera_controls  # manual exposure never touches gain - see engine.streams
    assert camera_controls["emitter_enabled"] is False  # checkbox checked by default -> emitter disabled


def test_manual_exposure_selection_reports_independent_spinbox_values(qapp):
    # Different sensors (IR vs RGB, or two different IR sensors) have
    # different brightness characteristics - exposure_a/exposure_b must be
    # independently settable, not one shared value applied to both.
    page = StreamConfigPage()
    page.populate(ctx=None, device_serial="123", tests=_tests(("IR vs RGB sync", [(IR1, COLOR0)])))

    page._camera_controls["manual_radio"].setChecked(True)
    page._camera_controls["exposure_a_spin"].setValue(5000)
    page._camera_controls["exposure_b_spin"].setValue(9000)

    received = []
    page.config_chosen.connect(lambda payload: received.append(payload))
    page._on_next_clicked()

    _, _, camera_controls = received[0]
    assert camera_controls["auto_exposure"] is False
    assert camera_controls["exposure_a"] == 5000
    assert camera_controls["exposure_b"] == 9000


def test_exposure_labels_show_actual_stream_for_ir_vs_rgb(qapp):
    # Exposure A/B's labels must name the actual physical stream each
    # spinbox controls (not a generic "Exposure A"/"Exposure B") - a fixed
    # "IR"/"RGB" label would be flat wrong for the IR1-vs-IR2 test below.
    page = StreamConfigPage()
    page.populate(ctx=None, device_serial="123", tests=_tests(("IR vs RGB sync", [(IR1, COLOR0)])))

    assert page._camera_controls["exposure_a_label"].text() == "Exposure (Infrared 1):"
    assert page._camera_controls["exposure_b_label"].text() == "Exposure (Color 0):"


def test_exposure_labels_update_on_test_change(qapp):
    page = StreamConfigPage()
    page.populate(
        ctx=None, device_serial="123",
        tests=_tests(("IR1 vs IR2 sync", [(IR1, IR2)]), ("IR vs RGB sync", [(IR1, COLOR0)])),
    )
    assert page._camera_controls["exposure_a_label"].text() == "Exposure (Infrared 1):"
    assert page._camera_controls["exposure_b_label"].text() == "Exposure (Infrared 2):"

    page.combo_test.setCurrentIndex(1)

    assert page._camera_controls["exposure_a_label"].text() == "Exposure (Infrared 1):"
    assert page._camera_controls["exposure_b_label"].text() == "Exposure (Color 0):"


def test_unchecking_disable_emitter_checkbox_reports_emitter_enabled(qapp):
    # The checkbox now defaults to checked (emitter disabled - see
    # test_next_emits_picks_and_default_auto_exposure_camera_controls above
    # for coverage of the default itself). This test covers the OTHER
    # state: explicitly opting OUT of the default by unchecking it.
    page = StreamConfigPage()
    page.populate(ctx=None, device_serial="123", tests=_tests(("IR vs RGB sync", [(IR1, COLOR0)])))

    page._camera_controls["emitter_checkbox"].setChecked(False)

    received = []
    page.config_chosen.connect(lambda payload: received.append(payload))
    page._on_next_clicked()

    _, _, camera_controls = received[0]
    assert camera_controls["emitter_enabled"] is True


# --- Same-stream guard: defense-in-depth only now (a well-formed test's two
# streams differ by construction), but still fires for a misconfigured
# settings.yaml test that accidentally defines the same stream twice ---

def test_on_next_clicked_does_not_emit_when_test_picks_are_the_same_stream(qapp):
    page = StreamConfigPage()
    page.populate(ctx=None, device_serial="123", tests=_tests(("Misconfigured test", [(IR1, IR1)])))

    received = []
    page.config_chosen.connect(lambda payload: received.append(payload))
    page._on_next_clicked()

    assert received == []
    assert "same physical stream" in page.status_label.text()


def test_on_start_preview_clicked_does_not_start_preview_when_test_picks_are_the_same_stream(qapp, monkeypatch):
    page = StreamConfigPage()
    page.populate(ctx=None, device_serial="123", tests=_tests(("Misconfigured test", [(IR1, IR1)])))

    constructed = []
    import gui.pages.stream_config_page as stream_config_page_module
    monkeypatch.setattr(
        stream_config_page_module, "StreamPreviewThread",
        lambda *a, **k: constructed.append((a, k)),
    )

    page._on_start_preview_clicked()

    assert constructed == []
    assert page.preview_thread is None
    assert "same physical stream" in page.status_label.text()


# --- RGB Mode: moved here from Device Select since a mode switch changes
# which streams the device exposes, invalidating whatever Test/Sensor
# Options this page already resolved - the switch has to happen (and
# refresh those lists) on the SAME page that shows them, not one page
# earlier where nothing downstream exists to invalidate yet. ---

def test_mode_box_hidden_for_a_device_with_no_recognized_rgb_mode(qapp, monkeypatch):
    import gui.pages.stream_config_page as stream_config_page_module
    monkeypatch.setattr(stream_config_page_module, "get_mode", lambda device: None)
    monkeypatch.setattr(stream_config_page_module, "find_device_by_serial", lambda ctx, serial: object())
    page = StreamConfigPage()

    page.populate(ctx=None, device_serial="SN_D435", tests=_tests(("IR1 vs IR2 sync", [(IR1, IR2)])))

    assert not page.mode_group_box.isVisibleTo(page)


def test_mode_box_shown_and_preselected_for_a_dual_rgb_device(qapp, monkeypatch):
    import gui.pages.stream_config_page as stream_config_page_module
    monkeypatch.setattr(stream_config_page_module, "get_mode", lambda device: "dual")
    monkeypatch.setattr(stream_config_page_module, "find_device_by_serial", lambda ctx, serial: object())
    page = StreamConfigPage()

    page.populate(ctx=None, device_serial="SN_DUAL", tests=_tests(("IR1 vs IR2 sync", [(IR1, IR2)])))

    assert page.mode_group_box.isVisibleTo(page)
    assert page.dual_radio.isChecked()
    assert not page.dedicated_radio.isChecked()


def test_mode_box_preselected_for_a_dedicated_rgb_device(qapp, monkeypatch):
    import gui.pages.stream_config_page as stream_config_page_module
    monkeypatch.setattr(stream_config_page_module, "get_mode", lambda device: "dedicated")
    monkeypatch.setattr(stream_config_page_module, "find_device_by_serial", lambda ctx, serial: object())
    page = StreamConfigPage()

    page.populate(ctx=None, device_serial="SN_DEDICATED", tests=_tests(("IR1 vs IR2 sync", [(IR1, IR2)])))

    assert page.dedicated_radio.isChecked()
    assert not page.dual_radio.isChecked()


def test_next_does_not_intercept_when_mode_radio_matches_current_mode(qapp, monkeypatch):
    import gui.pages.stream_config_page as stream_config_page_module
    monkeypatch.setattr(stream_config_page_module, "get_mode", lambda device: "dual")
    monkeypatch.setattr(stream_config_page_module, "find_device_by_serial", lambda ctx, serial: object())
    page = StreamConfigPage()
    page.populate(ctx=None, device_serial="SN_DUAL", tests=_tests(("IR1 vs IR2 sync", [(IR1, IR2)])))
    assert page.dual_radio.isChecked()  # already matches - untouched

    mode_switches = []
    page.mode_switch_requested.connect(mode_switches.append)
    configs_chosen = []
    page.config_chosen.connect(configs_chosen.append)
    page._on_next_clicked()

    assert mode_switches == []
    assert len(configs_chosen) == 1


def test_next_intercepts_and_emits_mode_switch_when_radio_differs(qapp, monkeypatch):
    import gui.pages.stream_config_page as stream_config_page_module
    monkeypatch.setattr(stream_config_page_module, "get_mode", lambda device: "dual")
    monkeypatch.setattr(stream_config_page_module, "find_device_by_serial", lambda ctx, serial: object())
    page = StreamConfigPage()
    page.populate(ctx=None, device_serial="SN_DUAL", tests=_tests(("IR1 vs IR2 sync", [(IR1, IR2)])))
    page.dedicated_radio.setChecked(True)  # operator picked the OTHER mode

    mode_switches = []
    page.mode_switch_requested.connect(mode_switches.append)
    configs_chosen = []
    page.config_chosen.connect(configs_chosen.append)
    page._on_next_clicked()

    assert mode_switches == ["dedicated"]
    assert configs_chosen == []  # does not proceed - the switch may invalidate the current picks


def test_next_does_not_check_mode_for_a_device_with_no_recognized_rgb_mode(qapp, monkeypatch):
    import gui.pages.stream_config_page as stream_config_page_module
    monkeypatch.setattr(stream_config_page_module, "get_mode", lambda device: None)
    monkeypatch.setattr(stream_config_page_module, "find_device_by_serial", lambda ctx, serial: object())
    page = StreamConfigPage()
    page.populate(ctx=None, device_serial="SN_D435", tests=_tests(("IR1 vs IR2 sync", [(IR1, IR2)])))

    mode_switches = []
    page.mode_switch_requested.connect(mode_switches.append)
    configs_chosen = []
    page.config_chosen.connect(configs_chosen.append)
    page._on_next_clicked()

    assert mode_switches == []
    assert len(configs_chosen) == 1


# --- note_mode_switch_applied: MainWindow calls this after a real
# ensure_mode() succeeds even when the subsequent Test/Sensor Options
# re-populate then fails (e.g. no configured test matches this camera's new
# mode) - without it, _current_rgb_mode would stay stale at the PRE-switch
# value, making the next Next click think another switch is still needed
# and re-trigger ensure_mode() for a switch that already succeeded, in an
# unrecoverable retry loop. ---

def test_note_mode_switch_applied_updates_current_mode_and_radios(qapp, monkeypatch):
    import gui.pages.stream_config_page as stream_config_page_module
    monkeypatch.setattr(stream_config_page_module, "get_mode", lambda device: "dual")
    monkeypatch.setattr(stream_config_page_module, "find_device_by_serial", lambda ctx, serial: object())
    page = StreamConfigPage()
    page.populate(ctx=None, device_serial="SN_DUAL", tests=_tests(("IR1 vs IR2 sync", [(IR1, IR2)])))

    page.note_mode_switch_applied("dedicated")

    assert page.dedicated_radio.isChecked()
    assert not page.dual_radio.isChecked()
    # A subsequent Next click must not think another switch is still
    # needed, now that the radio and _current_rgb_mode agree.
    mode_switches = []
    page.mode_switch_requested.connect(mode_switches.append)
    configs_chosen = []
    page.config_chosen.connect(configs_chosen.append)
    page._on_next_clicked()
    assert mode_switches == []
    assert len(configs_chosen) == 1


# --- Use dual LED panel: moved here from Device Select since it depends on
# which Test/pairing is picked (IR vs RGB needs it, IR vs IR doesn't) -
# manual, per-camera-flow, not inferred from the test automatically (yet). ---

def test_dual_panel_checkbox_defaults_unchecked(qapp):
    page = StreamConfigPage()
    page.populate(ctx=None, device_serial="123", tests=_tests(("IR1 vs IR2 sync", [(IR1, IR2)])))
    assert not page.dual_panel_checkbox.isChecked()


def test_populate_can_preselect_dual_panel_checked(qapp):
    page = StreamConfigPage()
    page.populate(
        ctx=None, device_serial="123", tests=_tests(("IR vs RGB sync", [(IR1, COLOR0)])),
        preferred_dual_panel=True,
    )
    assert page.dual_panel_checkbox.isChecked()


def test_populate_resets_dual_panel_checkbox_when_not_preselected(qapp):
    # Guards against stale carryover: this page's SAME instance is reused
    # across every camera's own sub-flow visit (see main_window.py's module
    # docstring) - a previous camera's checked state must not silently leak
    # into the next camera's default.
    page = StreamConfigPage()
    page.populate(
        ctx=None, device_serial="123", tests=_tests(("IR vs RGB sync", [(IR1, COLOR0)])),
        preferred_dual_panel=True,
    )
    assert page.dual_panel_checkbox.isChecked()

    page.populate(ctx=None, device_serial="456", tests=_tests(("IR1 vs IR2 sync", [(IR1, IR2)])))

    assert not page.dual_panel_checkbox.isChecked()


# --- Back button: mirrors Next's own silent auto-stop-preview precedent -
# no confirmation, since a pairing-quality preview has no work to lose ---

def test_back_button_emits_back_requested(qapp):
    page = StreamConfigPage()
    page.populate(ctx=None, device_serial="123", tests=_tests(("IR1 vs IR2 sync", [(IR1, IR2)])))
    emitted = []
    page.back_requested.connect(lambda: emitted.append(True))

    page.back_button.click()

    assert emitted == [True]


def test_back_button_stops_a_running_preview_first(qapp, monkeypatch):
    page = StreamConfigPage()
    page.populate(ctx=None, device_serial="123", tests=_tests(("IR1 vs IR2 sync", [(IR1, IR2)])))
    fake_thread = MagicMock()
    page.preview_thread = fake_thread

    emitted = []
    page.back_requested.connect(lambda: emitted.append(True))
    page.back_button.click()

    fake_thread.request_stop.assert_called_once()
    fake_thread.wait.assert_called_once()
    assert page.preview_thread is None
    assert emitted == [True]  # still navigates away, same as Next does
