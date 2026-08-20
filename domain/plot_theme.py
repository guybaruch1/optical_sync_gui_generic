"""Dark-theme colors shared by the live pyqtgraph charts
(gui/widgets/live_plot.py) and the static matplotlib session-end export
(domain/plot_export.py), so the two never visually drift apart. Values are
the dataviz skill's validated dark-mode reference palette
(references/palette.md) - lifted here (rather than duplicated) once a
second renderer needed the same chrome."""

SURFACE = "#1a1a19"
GRIDLINE = "#2c2c2a"
MUTED_TEXT = "#898781"

# Per-series colors, matching LiveSessionPage's own LivePlot.add_series
# calls exactly, so a chart looks the same whether it's the live pyqtgraph
# view or the static end-of-session matplotlib export.
PAIRING_GAP_COLOR = "#4a7fe0"     # LivePlot series "pairing_gap_us"
POSITION_GAP_COLOR = "#3fbf9e"    # LivePlot series "position_gap_ms"
STREAM_A_DROP_COLOR = "#e08a3f"   # LivePlot series "stream_a_frame_drops"
STREAM_B_DROP_COLOR = "#c0587a"   # LivePlot series "stream_b_frame_drops"

# One cross-camera (master-vs-slave) sync series per (slave_camera_id,
# stream_identity) pair - up to 4 in the current "up to 3 cameras, up to 2
# shared stream identities per slave" multi-camera design, cycled if there
# are ever more. Deliberately distinct from the 4 colors above so a
# cross-camera series is visually distinguishable from any one camera's own
# intra-camera chart. Shared by gui/pages/multi_camera_live_session_page.py's
# live LivePlot and domain/plot_export.py's static export_cross_camera_plot,
# so the two never visually drift apart - same reasoning as every other
# color in this file.
CROSS_CAMERA_COLORS = ("#9b59d0", "#d0a02f", "#2fb0b6", "#d0522f")
