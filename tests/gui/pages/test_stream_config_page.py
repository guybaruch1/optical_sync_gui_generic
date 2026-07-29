from gui.pages.stream_config_page import StreamConfigPage


def test_preselect_sets_current_index_when_preferred_combo_exists(qapp):
    page = StreamConfigPage()
    page.ir_combo.addItem("640x480@30fps", userData=(640, 480, 30))
    page.ir_combo.addItem("1280x720@30fps", userData=(1280, 720, 30))

    page._preselect(page.ir_combo, (1280, 720, 30))

    assert page.ir_combo.currentData() == (1280, 720, 30)


def test_preselect_leaves_default_selection_when_preferred_combo_unavailable(qapp):
    page = StreamConfigPage()
    page.ir_combo.addItem("640x480@30fps", userData=(640, 480, 30))
    page.ir_combo.addItem("320x240@60fps", userData=(320, 240, 60))

    page._preselect(page.ir_combo, (1280, 720, 30))  # not in the list

    assert page.ir_combo.currentData() == (640, 480, 30)  # unchanged, first item


def test_preselect_does_nothing_when_no_preferred_combo_given(qapp):
    page = StreamConfigPage()
    page.rgb_combo.addItem("640x480@30fps", userData=(640, 480, 30))

    page._preselect(page.rgb_combo, None)

    assert page.rgb_combo.currentData() == (640, 480, 30)
