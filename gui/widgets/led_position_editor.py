"""Manual LED position editor, opened from Threshold Tuning's "Edit LED
positions..." button when no detection threshold finds every LED.

Shows one stream's Calibration all-on frame, cropped to its ROI, with the
current LED circles on top. Left-click an empty spot adds a circle, left-
drag moves one, right-click deletes one; the wheel zooms toward the cursor,
middle-drag pans, "Fit" shows the whole ROI again. Every circle is labelled
with the led_id it will get - re-derived after every edit with
domain.calibration.grid_rows, the same row-major rule Calibration numbers
LEDs by - so an added LED lands in scan order without the operator ever
typing a number, and one dragged into the wrong row shows up at once.

Built on QGraphicsView on purpose: circles live in the scene in IMAGE
pixel coordinates and the view does the widget<->image mapping, so clicks
stay exact at any zoom. ROI Select avoids an embedded Qt editor because a
stretched QLabel (setScaledContents) got that mapping wrong - that is not
how this works.

Points are in the CROPPED image's coordinates (cv2 centroid convention:
pixel i's centre is at i), exactly what the page's
_{stream}_pending_centroids hold. Pixel i spans [i, i+1) in the scene, so a
circle for point x sits at scene x + 0.5."""

import numpy as np
import cv2
from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QGraphicsEllipseItem, QGraphicsItem, QGraphicsPixmapItem, QGraphicsScene,
    QGraphicsSimpleTextItem, QGraphicsView, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)

from domain.calibration import grid_rows
from domain.realsense_utils import _debug_circle_radius

_PIXEL_CENTER = 0.5
_ZOOM_STEP = 1.25


def led_numbers(points, row_gap_px):
    """led_id for each point (same order as `points`) plus the row layout,
    by Calibration's own grid rule. ([], []) for no points."""
    if not points:
        return [], []
    rows = grid_rows(list(points), row_gap_px)
    numbers = [0] * len(points)
    led_id = 0
    for row in rows:
        for index in row:
            numbers[index] = led_id
            led_id += 1
    return numbers, [len(row) for row in rows]


def layout_problems(count, row_layout, num_leds):
    """Operator-facing reasons the current layout looks wrong - empty when
    it looks right: the count must be num_leds and every row the same
    length (a shorter/longer row almost always means a circle sits in the
    wrong row, which would shift every later LED's number)."""
    problems = []
    if num_leds is not None and count != num_leds:
        problems.append("{} of {} LEDs".format(count, num_leds))
    if len(set(row_layout)) > 1:
        problems.append("rows have different lengths")
    return problems


def _to_pixmap(image):
    image = np.ascontiguousarray(image)
    if image.ndim == 2:
        height, width = image.shape
        qimage = QImage(image.data, width, height, width, QImage.Format_Grayscale8)
    else:
        rgb = np.ascontiguousarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
        height, width, _ = rgb.shape
        qimage = QImage(rgb.data, width, height, 3 * width, QImage.Format_RGB888)
    return QPixmap.fromImage(qimage.copy())


class _LedCircle(QGraphicsEllipseItem):
    """One LED: a movable circle centred on its own position, plus a
    zoom-independent number label. Tells the dialog on every move."""

    def __init__(self, x, y, radius, on_moved):
        super().__init__(-radius, -radius, 2 * radius, 2 * radius)
        self._on_moved = on_moved
        pen = QPen(QColor(0, 255, 0), 2)
        pen.setCosmetic(True)
        self.setPen(pen)
        # A transparent fill makes the whole disc grabbable, not just the ring.
        # Faint, so the LED under it stays visible for centring by eye.
        self.setBrush(QBrush(QColor(0, 255, 0, 18)))
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setCursor(Qt.OpenHandCursor)
        self.label = QGraphicsSimpleTextItem("", self)
        self.label.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        self.label.setBrush(QBrush(QColor(255, 255, 0)))
        font = QFont()
        font.setPointSize(8)
        self.label.setFont(font)
        self.label.setPos(radius, -radius)
        self.setPos(x + _PIXEL_CENTER, y + _PIXEL_CENTER)

    def point(self):
        return (self.pos().x() - _PIXEL_CENTER, self.pos().y() - _PIXEL_CENTER)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionChange and self.scene() is not None:
            # Keep the centre inside the image.
            bounds = self.scene().sceneRect()
            value = QPointF(min(max(value.x(), bounds.left()), bounds.right()),
                            min(max(value.y(), bounds.top()), bounds.bottom()))
            return value
        if change == QGraphicsItem.ItemPositionHasChanged:
            self._on_moved()
        return super().itemChange(change, value)


class _EditorView(QGraphicsView):
    def __init__(self, scene, on_add, on_delete):
        super().__init__(scene)
        self._on_add = on_add
        self._on_delete = on_delete
        self._pan_origin = None
        self.setRenderHint(QPainter.Antialiasing)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setBackgroundBrush(QBrush(QColor(58, 58, 58)))

    def _circle_at(self, pos):
        for item in self.items(pos):
            if isinstance(item, _LedCircle):
                return item
            if isinstance(item.parentItem(), _LedCircle):
                return item.parentItem()
        return None

    def mousePressEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self._pan_origin = event.position().toPoint()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        circle = self._circle_at(event.position().toPoint())
        if event.button() == Qt.RightButton:
            if circle is not None:
                self._on_delete(circle)
            event.accept()
            return
        if event.button() == Qt.LeftButton and circle is None:
            scene_pos = self.mapToScene(event.position().toPoint())
            if self.sceneRect().contains(scene_pos):
                self._on_add(scene_pos.x() - _PIXEL_CENTER, scene_pos.y() - _PIXEL_CENTER)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pan_origin is not None:
            pos = event.position().toPoint()
            delta = pos - self._pan_origin
            self._pan_origin = pos
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MiddleButton and self._pan_origin is not None:
            self._pan_origin = None
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        steps = event.angleDelta().y() / 120.0
        if steps:
            factor = _ZOOM_STEP ** steps
            self.scale(factor, factor)
        event.accept()

    def fit(self):
        self.fitInView(self.sceneRect(), Qt.KeepAspectRatio)


class LedPositionEditorDialog(QDialog):
    """exec() -> Accepted/Rejected; points() is the edited list, in the
    cropped image's coordinates."""

    def __init__(self, image, points, num_leds, row_gap_px, title="Edit LED positions", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(900, 750)
        self._num_leds = num_leds
        self._row_gap_px = row_gap_px
        self._circles = []
        points = [(float(x), float(y)) for x, y in points]
        self._radius = float(_debug_circle_radius(points))

        self.scene = QGraphicsScene(self)
        pixmap = QGraphicsPixmapItem(_to_pixmap(image))
        pixmap.setZValue(-1)
        self.scene.addItem(pixmap)
        height, width = image.shape[:2]
        self.scene.setSceneRect(QRectF(0, 0, width, height))
        self.view = _EditorView(self.scene, self.add_point, self.delete_circle)

        help_label = QLabel(
            "Left-click an empty spot to add an LED - drag a circle to move it - right-click a "
            "circle to delete it. Mouse wheel zooms, middle-drag pans. Numbers follow the "
            "panel's scan order (rows top to bottom, left to right) and update as you edit.")
        help_label.setWordWrap(True)
        self.status_label = QLabel("")
        fit_button = QPushButton("Fit")
        fit_button.clicked.connect(self.view.fit)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        bottom_row = QHBoxLayout()
        bottom_row.addWidget(self.status_label, 1)
        bottom_row.addWidget(fit_button)
        bottom_row.addWidget(self.buttons)
        layout = QVBoxLayout(self)
        layout.addWidget(help_label)
        layout.addWidget(self.view, 1)
        layout.addLayout(bottom_row)

        for x, y in points:
            self._add_circle(x, y)
        self._renumber()

    def showEvent(self, event):
        super().showEvent(event)
        self.view.fit()

    def points(self):
        return [circle.point() for circle in self._circles]

    def add_point(self, x, y):
        self._add_circle(x, y)
        self._renumber()

    def delete_circle(self, circle):
        self._circles.remove(circle)
        self.scene.removeItem(circle)
        self._renumber()

    def _add_circle(self, x, y):
        circle = _LedCircle(x, y, self._radius, self._renumber)
        self.scene.addItem(circle)
        self._circles.append(circle)

    def _renumber(self):
        points = self.points()
        numbers, row_layout = led_numbers(points, self._row_gap_px)
        for circle, number in zip(self._circles, numbers):
            circle.label.setText(str(number))
        problems = layout_problems(len(points), row_layout, self._num_leds)
        self.status_label.setText("{} / {} LEDs - rows: {}{}".format(
            len(points), self._num_leds, ",".join(str(n) for n in row_layout) or "-",
            " - check: " + "; ".join(problems) if problems else ""))
        self.status_label.setStyleSheet("color: #b00020; font-weight: 600;" if problems else "color: #1b5e20;")
        # Nothing to commit with no LEDs at all.
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(bool(points))
