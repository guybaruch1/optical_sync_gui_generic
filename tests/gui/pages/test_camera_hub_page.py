from gui.pages.camera_hub_page import CameraHubPage, CameraSummary


def _summary(camera_id, is_master=False, configured=True, label=None):
    return CameraSummary(camera_id=camera_id, label=label or camera_id, is_master=is_master, configured=configured)


# --- Start button eligibility: exactly one master, every camera configured ---

def test_no_cameras_start_disabled_add_enabled(qapp):
    page = CameraHubPage()
    page.set_cameras([])
    assert not page.start_button.isEnabled()
    assert page.add_camera_button.isEnabled()


def test_one_unconfigured_master_camera_disables_start(qapp):
    page = CameraHubPage()
    page.set_cameras([_summary("cam1", is_master=True, configured=False)])
    assert not page.start_button.isEnabled()


def test_one_configured_master_camera_enables_start(qapp):
    page = CameraHubPage()
    page.set_cameras([_summary("cam1", is_master=True, configured=True)])
    assert page.start_button.isEnabled()


def test_multiple_configured_cameras_with_no_master_disables_start(qapp):
    page = CameraHubPage()
    page.set_cameras([
        _summary("cam1", is_master=False, configured=True),
        _summary("cam2", is_master=False, configured=True),
    ])
    assert not page.start_button.isEnabled()


def test_multiple_configured_cameras_with_two_masters_disables_start(qapp):
    # Defensive - the hub page never enables Start on an invalid rig state,
    # even though MainWindow is the one actually enforcing "exactly one
    # master" when the operator clicks "Set as Master".
    page = CameraHubPage()
    page.set_cameras([
        _summary("cam1", is_master=True, configured=True),
        _summary("cam2", is_master=True, configured=True),
    ])
    assert not page.start_button.isEnabled()


def test_one_configured_and_one_unconfigured_camera_disables_start(qapp):
    page = CameraHubPage()
    page.set_cameras([
        _summary("cam1", is_master=True, configured=True),
        _summary("cam2", is_master=False, configured=False),
    ])
    assert not page.start_button.isEnabled()


def test_clicking_start_when_eligible_emits_signal(qapp):
    page = CameraHubPage()
    page.set_cameras([_summary("cam1", is_master=True, configured=True)])
    emitted = []
    page.start_multi_camera_session_requested.connect(lambda: emitted.append(True))

    page.start_button.click()

    assert emitted == [True]


# --- Add Camera: always available up to CameraHubPage.MAX_CAMERAS ---

def test_clicking_add_camera_emits_signal(qapp):
    page = CameraHubPage()
    page.set_cameras([])
    emitted = []
    page.add_camera_requested.connect(lambda: emitted.append(True))

    page.add_camera_button.click()

    assert emitted == [True]


def test_add_camera_disabled_at_max_cameras(qapp):
    page = CameraHubPage()
    page.set_cameras([_summary("cam1", is_master=True), _summary("cam2"), _summary("cam3")])
    assert len(page._summaries) == CameraHubPage.MAX_CAMERAS
    assert not page.add_camera_button.isEnabled()


def test_add_camera_enabled_below_max_cameras(qapp):
    page = CameraHubPage()
    page.set_cameras([_summary("cam1", is_master=True), _summary("cam2")])
    assert page.add_camera_button.isEnabled()


# --- Per-card actions: Edit / Set as Master / Remove, each tagged with the
# card's own camera_id. ---

def test_edit_button_emits_that_cards_camera_id(qapp):
    page = CameraHubPage()
    page.set_cameras([_summary("cam1", is_master=True), _summary("cam2")])
    emitted = []
    page.edit_camera_requested.connect(emitted.append)

    page._cards["cam2"].edit_button.click()

    assert emitted == ["cam2"]


def test_set_as_master_button_emits_that_cards_camera_id(qapp):
    page = CameraHubPage()
    page.set_cameras([_summary("cam1", is_master=True), _summary("cam2", is_master=False)])
    emitted = []
    page.master_change_requested.connect(emitted.append)

    page._cards["cam2"].master_button.click()

    assert emitted == ["cam2"]


def test_set_as_master_button_disabled_for_the_current_master(qapp):
    page = CameraHubPage()
    page.set_cameras([_summary("cam1", is_master=True), _summary("cam2", is_master=False)])

    assert not page._cards["cam1"].master_button.isEnabled()
    assert page._cards["cam2"].master_button.isEnabled()


def test_remove_button_emits_that_cards_camera_id(qapp):
    page = CameraHubPage()
    page.set_cameras([_summary("cam1", is_master=True), _summary("cam2")])
    emitted = []
    page.remove_camera_requested.connect(emitted.append)

    page._cards["cam2"].remove_button.click()

    assert emitted == ["cam2"]


def test_set_cameras_rebuilds_cards_cleanly_on_second_call(qapp):
    # A camera removed from the summaries list must not leave a stale card
    # behind, and a re-added one under the same id must get a FRESH card
    # (not accidentally reuse stale widget state/signal connections from a
    # previous call).
    page = CameraHubPage()
    page.set_cameras([_summary("cam1", is_master=True), _summary("cam2")])
    assert set(page._cards.keys()) == {"cam1", "cam2"}

    page.set_cameras([_summary("cam1", is_master=True)])

    assert set(page._cards.keys()) == {"cam1"}


def test_card_label_reflects_master_and_configured_state(qapp):
    page = CameraHubPage()
    page.set_cameras([_summary("cam1", label="D455 (SN123)", is_master=True, configured=False)])

    text = page._cards["cam1"].label_widget.text()
    assert "D455 (SN123)" in text
    assert "MASTER" in text
    assert "needs setup" in text.lower()


def test_gmsl_checkbox_hidden_and_unchecked_by_default(qapp):
    page = CameraHubPage()
    assert page.gmsl_tsc_checkbox.isHidden()
    assert page.gmsl_tsc_checked is False


def test_gmsl_checkbox_pre_ticked_when_it_becomes_available(qapp):
    page = CameraHubPage()
    page.set_gmsl_tsc_available(True)
    assert not page.gmsl_tsc_checkbox.isHidden()
    assert page.gmsl_tsc_checked is True


def test_gmsl_checkbox_untick_survives_refresh(qapp):
    page = CameraHubPage()
    page.set_gmsl_tsc_available(True)
    page.gmsl_tsc_checkbox.setChecked(False)

    page.set_gmsl_tsc_available(True)  # e.g. hub refresh after an Edit

    assert page.gmsl_tsc_checked is False


def test_gmsl_checkbox_unavailable_hides_and_reports_false(qapp):
    page = CameraHubPage()
    page.set_gmsl_tsc_available(True)
    page.set_gmsl_tsc_available(False)
    assert page.gmsl_tsc_checkbox.isHidden()
    assert page.gmsl_tsc_checked is False

    page.set_gmsl_tsc_available(True)  # becomes available again -> re-ticked
    assert page.gmsl_tsc_checked is True


def test_gmsl_checkbox_operator_untick_survives_a_transient_unavailable_refresh(qapp):
    # M3: a lookup blip (available -> unavailable -> available) must not
    # override the operator's own untick.
    page = CameraHubPage()
    page.set_gmsl_tsc_available(True)
    page.gmsl_tsc_checkbox.click()  # operator unticks
    assert page.gmsl_tsc_checked is False

    page.set_gmsl_tsc_available(False)
    page.set_gmsl_tsc_available(True)

    assert page.gmsl_tsc_checked is False


# --- Single-stream cameras: a solo one has nothing to compare against ---

def test_start_disabled_for_a_solo_single_stream_camera(qapp):
    page = CameraHubPage()
    page.set_cameras([CameraSummary("cam1", "D455", is_master=True, configured=True, single_stream=True)])
    assert not page.start_button.isEnabled()
    assert page.start_button.toolTip() == CameraHubPage.SOLO_SINGLE_STREAM_MESSAGE
    assert CameraHubPage.SOLO_SINGLE_STREAM_MESSAGE == (
        "A single-stream camera needs at least one other camera to compare against."
    )


def test_start_enabled_for_two_single_stream_cameras(qapp):
    page = CameraHubPage()
    page.set_cameras([
        CameraSummary("cam1", "D455", is_master=True, configured=True, single_stream=True),
        CameraSummary("cam2", "D455", is_master=False, configured=True, single_stream=True),
    ])
    assert page.start_button.isEnabled()
    assert page.start_button.toolTip() == ""
