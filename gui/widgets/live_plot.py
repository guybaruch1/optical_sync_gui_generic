"""Generic live scrolling plot, fed by named metric series (e.g. one
curve for PairingGapMetric, one for PositionGapMetric) so the GUI never
has to special-case which metrics exist - see engine.metrics.Metric.

Styling (background/grid/axis text/line joins) follows the dataviz
skill's validated dark-mode reference palette instead of pyqtgraph's
defaults (pure black background, alpha-blended white grid, mitered line
joins), which read as harsh/jagged on a live-updating chart."""

from collections import deque

import pyqtgraph as pg
from PySide6.QtCore import Qt, QSize
from PySide6.QtWidgets import QSizePolicy

# dataviz skill's dark-mode chart chrome (references/palette.md).
SURFACE = "#1a1a19"
GRIDLINE = "#2c2c2a"
MUTED_TEXT = "#898781"


class LivePlot(pg.PlotWidget):
    def __init__(self, parent=None, max_points=2000):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(200, 150)
        self.setBackground(SURFACE)
        # Explicit hairline pen instead of showGrid's alpha-blended default
        # white, which reads as busy/heavy at high tick density - a solid,
        # one-step-off-surface gray is the recessive-grid convention.
        for axis_name in ("left", "bottom"):
            axis = self.getAxis(axis_name)
            axis.setPen(GRIDLINE)
            axis.setTextPen(MUTED_TEXT)
        self.showGrid(x=True, y=True, alpha=0.3)
        self.addLegend()
        # Bounded per series (not the whole session) - add_point() used to
        # append forever and hand the ENTIRE history to setData() on every
        # single point, making one point's cost grow with how long the
        # session had been running (O(n) per call, O(n^2) over a session).
        # At 30fps unthrottled that compounds fast enough to look like the
        # app hanging on a long run. A deque caps memory AND keeps each
        # setData() call's cost constant, and incidentally makes this an
        # actual scrolling window instead of "whole history, rescaled".
        self._max_points = max_points
        self._curves = {}
        self._x_data = {}
        self._y_data = {}

    def sizeHint(self):
        # pg.PlotWidget's own sizeHint is 600x480 - harmless when this
        # widget's real geometry comes straight from the parent's actual
        # available space (Expanding just lets it grow into whatever's
        # there), but LiveSessionPage's content now lives inside a
        # QScrollArea, which sizes the content widget off each child's
        # sizeHint whenever that exceeds the viewport - so the inherited
        # 480px-tall default made every plot render at that oversized
        # native size (and pull in a scrollbar) even on a normal/maximized
        # screen. A modest sizeHint here is only a floor for "how much
        # room to ask for before there's real space to grow into" - on
        # any screen with actual room to spare, Expanding + the graphs
        # column's stretch factors still let this widget fill it, exactly
        # like before.
        return QSize(400, 200)

    def add_series(self, name, color, display_name=None):
        pen = pg.mkPen(color=color, width=2)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        # display_name is only for the legend text - name stays the lookup
        # key everywhere else (add_point/get_series_data/set_series_visible),
        # so a user-facing rename never has to touch those call sites.
        curve = self.plot([], [], pen=pen, name=display_name or name, connect="finite")
        self._curves[name] = curve
        self._x_data[name] = deque(maxlen=self._max_points)
        self._y_data[name] = deque(maxlen=self._max_points)

    def add_point(self, name, x, y):
        self._x_data[name].append(x)
        self._y_data[name].append(y)
        self._curves[name].setData(list(self._x_data[name]), list(self._y_data[name]))

    def clear_data(self):
        # Wipes every series back to empty and resets to auto-range, so a
        # new session starts on a genuinely blank graph - without this, a
        # new session's pair_index restarting at 0 would draw right on top
        # of/alongside whatever the previous session left behind, and any
        # manual zoom/pan from the previous session would carry over too.
        for name in self._curves:
            self._x_data[name].clear()
            self._y_data[name].clear()
            self._curves[name].setData([], [])
        self.enableAutoRange()

    def set_series_visible(self, name, visible):
        self._curves[name].setVisible(visible)

    def get_series_data(self, name):
        return list(self._x_data[name]), list(self._y_data[name])
