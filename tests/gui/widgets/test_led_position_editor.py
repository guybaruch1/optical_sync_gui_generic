import numpy as np
import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialogButtonBox

from gui.widgets.led_position_editor import LedPositionEditorDialog, layout_problems, led_numbers


# A 3x3 grid, 20px pitch, given out of order.
GRID = [(50, 10), (10, 10), (30, 10), (10, 30), (50, 30), (30, 30), (30, 50), (10, 50), (50, 50)]


def test_led_numbers_follow_calibrations_row_major_order():
    numbers, rows = led_numbers(GRID, row_gap_px=15)

    assert rows == [3, 3, 3]
    assert [numbers[GRID.index(p)] for p in [(10, 10), (30, 10), (50, 10), (10, 30), (50, 50)]] == [0, 1, 2, 3, 8]


def test_led_added_into_a_gap_takes_its_place_in_scan_order():
    missing_middle = [p for p in GRID if p != (30, 30)]
    before, _ = led_numbers(missing_middle, 15)
    assert before[missing_middle.index((50, 30))] == 4

    after, rows = led_numbers(missing_middle + [(31, 29)], 15)

    assert after[-1] == 4  # the added LED is #4 ...
    assert after[missing_middle.index((50, 30))] == 5  # ... and the rest shift up
    assert rows == [3, 3, 3]


def test_led_numbers_empty():
    assert led_numbers([], 15) == ([], [])


def test_layout_problems():
    assert layout_problems(9, [3, 3, 3], 9) == []
    assert layout_problems(8, [3, 2, 3], 9) == ["8 of 9 LEDs", "rows have different lengths"]


def _dialog(qapp, points=GRID, num_leds=9):
    image = np.full((60, 60), 30, dtype=np.uint8)
    dialog = LedPositionEditorDialog(image, points, num_leds=num_leds, row_gap_px=15)
    dialog.show()
    QTest.qWaitForWindowExposed(dialog)
    return dialog


def _view_point(dialog, x, y):
    # Image point (cv2 convention) -> view widget coordinates.
    return dialog.view.mapFromScene(QPointF(x + 0.5, y + 0.5))


def _labels(dialog):
    return {tuple(round(v) for v in circle.point()): circle.label.text() for circle in dialog._circles}


def test_dialog_shows_every_point_numbered_and_status(qapp):
    dialog = _dialog(qapp)

    assert sorted(dialog.points()) == sorted((float(x), float(y)) for x, y in GRID)
    assert _labels(dialog)[(10, 10)] == "0" and _labels(dialog)[(50, 50)] == "8"
    assert "9 / 9 LEDs" in dialog.status_label.text()
    assert "check" not in dialog.status_label.text()
    dialog.close()


@pytest.mark.parametrize("zoom", [1.0, 3.0])
def test_left_click_on_empty_spot_adds_led_at_the_clicked_pixel(qapp, zoom):
    dialog = _dialog(qapp, points=[p for p in GRID if p != (30, 30)])
    dialog.view.resetTransform()
    dialog.view.scale(zoom, zoom)
    dialog.view.centerOn(QPointF(30.5, 30.5))
    assert "check" in dialog.status_label.text()

    QTest.mouseClick(dialog.view.viewport(), Qt.LeftButton, Qt.NoModifier, _view_point(dialog, 30, 30))

    added = dialog.points()[-1]
    assert added == pytest.approx((30, 30), abs=1.0 / zoom + 0.01)
    assert _labels(dialog)[(30, 30)] == "4"
    assert "9 / 9 LEDs" in dialog.status_label.text() and "check" not in dialog.status_label.text()
    dialog.close()


def test_dragging_a_circle_moves_it(qapp):
    dialog = _dialog(qapp)
    viewport = dialog.view.viewport()
    start, end = _view_point(dialog, 50, 50), _view_point(dialog, 54, 47)

    QTest.mousePress(viewport, Qt.LeftButton, Qt.NoModifier, start)
    QTest.mouseMove(viewport, start + QPoint(1, 1))
    QTest.mouseMove(viewport, end)
    QTest.mouseRelease(viewport, Qt.LeftButton, Qt.NoModifier, end)

    assert len(dialog.points()) == 9
    moved = [p for p in dialog.points() if p not in [(float(x), float(y)) for x, y in GRID]]
    assert len(moved) == 1
    assert moved[0] == pytest.approx((54, 47), abs=1.5)
    dialog.close()


def test_right_click_deletes_the_circle_and_ok_disables_when_empty(qapp):
    dialog = _dialog(qapp, points=[(10, 10)], num_leds=1)

    QTest.mouseClick(dialog.view.viewport(), Qt.RightButton, Qt.NoModifier, _view_point(dialog, 10, 10))

    assert dialog.points() == []
    assert not dialog.buttons.button(QDialogButtonBox.Ok).isEnabled()
    dialog.close()


def test_right_click_on_empty_spot_does_nothing(qapp):
    dialog = _dialog(qapp)

    QTest.mouseClick(dialog.view.viewport(), Qt.RightButton, Qt.NoModifier, _view_point(dialog, 20, 20))

    assert len(dialog.points()) == 9
    dialog.close()


def test_circles_are_kept_inside_the_image(qapp):
    dialog = _dialog(qapp, points=[(10, 10)], num_leds=1)

    dialog._circles[0].setPos(-50, 500)

    assert dialog.points()[0] == pytest.approx((-0.5, 59.5))
    dialog.close()


def test_color_image_is_supported(qapp):
    image = np.zeros((60, 60, 3), dtype=np.uint8)[:, 5:55]  # non-contiguous, like a crop
    dialog = LedPositionEditorDialog(image, [(10, 10)], num_leds=1, row_gap_px=15)

    assert dialog.scene.sceneRect().width() == 50
    dialog.close()
