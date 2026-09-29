"""Entry point: creates the QApplication, a pyrealsense2 context, loads
settings.yaml (read-only defaults) and gui_state.json (the GUI's own
persisted choices), and shows the MainWindow wizard."""

import sys

import pyqtgraph as pg
import pyrealsense2 as rs
from PySide6.QtWidgets import QApplication, QMessageBox

from gui.main_window import MainWindow
from state.gui_state import load_gui_state
from settings import load_settings
from engine.led_panel import configure_panel_connection
from engine import panel_rpc_client
from engine.gmsl_sync import DEFAULT_GMSL_TSC_SYNC, clear_leftover_sync_at_startup, disengage_all_engaged


def _clear_leftover_gmsl_sync(settings):
    """On the Orin: undo what a killed earlier GMSL-synced run left behind
    before any page opens a stream (see clear_leftover_sync_at_startup). A
    failure warns but never blocks the app - a free-running page with no
    frames is then the visible symptom, and the pop-up says why."""
    gmsl_settings = {**DEFAULT_GMSL_TSC_SYNC,
                     **((settings.get("camera_sync") or {}).get("gmsl_tsc_sync") or {})}
    try:
        reset_nodes = clear_leftover_sync_at_startup(settings.get("panel_connection"), gmsl_settings["control"])
    except Exception as exc:
        QMessageBox.warning(
            None, "Could not clear leftover GMSL sync",
            "{}\n\nCameras may still be in external-sync mode from a previous run and deliver no "
            "frames. Recover on the Orin with 'python3 tools/tsc_trigger/ext_sync_gen.py --disable' "
            "and 'v4l2-ctl -d <node> -c camera_sync_mode=0'.".format(exc))
        return
    if reset_nodes:
        print("Reset camera sync mode left over from a previous run on {} and stopped the TSC "
              "trigger.".format(", ".join(reset_nodes)), file=sys.stderr)


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

    configure_panel_connection(settings["panel_connection"])
    if settings["panel_connection"]["mode"] == "remote":
        panel_rpc_client.configure(
            settings["panel_connection"]["ssh_user"],
            settings["panel_connection"]["ssh_host"],
            settings["panel_connection"]["remote_repo_path"],
            settings["panel_connection"].get("remote_python", "python"),
        )

    _clear_leftover_gmsl_sync(settings)

    window = MainWindow(ctx, gui_state, settings)
    # Maximized (not a fixed resize()) so the window - and everything in
    # it, now that VideoPanel/LivePlot have sane size policies - actually
    # uses the available screen space instead of a hardcoded pixel size
    # that may be too big or too small for a given monitor.
    window.showMaximized()

    exit_code = app.exec()
    if settings["panel_connection"]["mode"] == "remote":
        # Closing the window mid-run must not leave the Orin's TSC
        # generator running or the cameras stuck in external-sync mode.
        # Only undoes what this process engaged. MainWindow.closeEvent has
        # already waited for every camera thread, so no stream is open.
        # First, so a panel-connection close failure can't skip it.
        try:
            disengage_all_engaged()
        finally:
            panel_rpc_client.close()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
