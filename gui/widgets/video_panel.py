"""Displays a live numpy frame (grayscale IR or BGR RGB) as a QLabel."""

import cv2
from PySide6.QtCore import QSize
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy


class VideoPanel(QLabel):
    def __init__(self, parent=None, force_square=False):
        super().__init__(parent)
        self.setScaledContents(True)
        self._force_square = force_square
        if force_square:
            # Matches the design mockup at its natural 320x320 size when
            # there's room (sizeHint() below), but - unlike the old
            # setFixedSize(320, 320) - allowed to shrink on a smaller/lower-
            # resolution screen instead of rigidly holding 320px and forcing
            # the rest of the page's content to overflow past the window's
            # visible area with no way to reach it. Expanding + a real
            # minimum, same shape as the non-square branch below, just with
            # a squarer floor.
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            self.setMinimumSize(160, 160)
            # Caps growth at the mockup's own natural size - without this,
            # on a page whose video row has nothing else competing for
            # leftover vertical space (e.g. Threshold Tuning, unlike Live
            # Session's busier graphs/stats layout), this panel would
            # stretch tall to fill it, rendering as a non-square rectangle.
            # Expanding (above) + a real minimum still lets it shrink on a
            # smaller/lower-resolution screen; this just stops it from
            # growing past 320x320 in the other direction.
            self.setMaximumSize(320, 320)
        else:
            # Without this, QLabel's sizeHint() defaults to the pixmap's
            # native resolution (e.g. 1280x720) once a frame is set, so two
            # side-by-side panels push the window/layout to grow to fit the
            # camera's actual resolution. Expanding (not Ignored - that
            # doesn't request any extra space, so this widget would just get
            # squeezed to its minimum whenever it shares a layout with
            # another Expanding widget, like the live plot) makes this
            # widget actively compete for available space; sizeHint() below
            # caps the "preferred" baseline at something modest instead of
            # the native resolution, and setScaledContents(True) stretches
            # whatever frame arrives to fill whatever size results.
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            self.setMinimumSize(160, 120)

    def sizeHint(self):
        return QSize(320, 320) if self._force_square else QSize(320, 240)

    def set_frame(self, image):
        if image.ndim == 2:
            height, width = image.shape
            qimage = QImage(image.data, width, height, width, QImage.Format_Grayscale8)
        else:
            rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            height, width, _ = rgb.shape
            qimage = QImage(rgb.data, width, height, 3 * width, QImage.Format_RGB888)
        self.setPixmap(QPixmap.fromImage(qimage.copy()))
