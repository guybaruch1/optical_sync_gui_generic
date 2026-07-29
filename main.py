"""Entry point: creates the QApplication, a pyrealsense2 context, loads
settings.yaml (read-only defaults) and gui_state.json (the GUI's own
persisted choices), and shows the MainWindow wizard."""

import sys

import pyqtgraph as pg
import pyrealsense2 as rs
from PySide6.QtWidgets import QApplication

from gui.main_window import MainWindow
from state.gui_state import load_gui_state
from settings import load_settings


def main():
    # Smooths the live plots' lines - pyqtgraph defaults to off, which reads
    # as jagged on the fast, densely-sampled LED-scan sawtooth patterns the
    # live session graphs show. Must be set before any PlotWidget is
    # constructed.
    pg.setConfigOptions(antialias=True)

    app = QApplication(sys.argv)
    ctx = rs.context()
    gui_state = load_gui_state()
    settings = load_settings()

    window = MainWindow(ctx, gui_state, settings)
    # Maximized (not a fixed resize()) so the window - and everything in
    # it, now that VideoPanel/LivePlot have sane size policies - actually
    # uses the available screen space instead of a hardcoded pixel size
    # that may be too big or too small for a given monitor.
    window.showMaximized()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
