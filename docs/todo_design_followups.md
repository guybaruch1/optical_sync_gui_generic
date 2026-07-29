# Design follow-ups: functionality behind the new UI

The Live Session page's layout/styling was updated to match the
`claude.ai/design` mockup "Optical Sync GUI.dc.html" (project "GUI layout
redesign options", imported via the DesignSync MCP tool). All four
follow-up items below are now implemented.

## Wired up

- **Frame drops checkbox** — toggles both `ir_frame_drops` and
  `rgb_frame_drops` visibility on `drop_plot` via
  `LivePlot.set_series_visible`.
- **Per-chart "Copy" button** (`gui/pages/live_session_page.py:_copy_chart_image`)
  — copies that chart as an image to the clipboard (`QWidget.grab()` +
  `QApplication.clipboard().setPixmap(...)`).
- **Per-chart "Export CSV" button** (`_export_chart_csv`) — exports only
  that chart's own plotted series (not the full-session CSV) via
  `domain.csv_export.export_series_csv`, to `output_dir` as
  `<first_series_name>_chart_export.csv`.
- **Toolbar "Export CSV" button** (`_reexport_last_session_csvs`) —
  re-writes the last completed session's CSVs from the rows cached in
  `_on_session_finished` (`self._last_session_rows`). Only meaningful after
  at least one Stop; before that it reports "no completed session yet".
- **"Stats" section avg/std/max tiles** — backed by
  `domain.running_stats.RunningStats` (Welford's algorithm; `extreme` is
  the largest-magnitude value, sign preserved). `_hw_ts_latency_stats`
  tracks `pairing_gap_us`, `_optical_sync_stats` tracks `position_gap_ms`
  ("Optical Latency" in the mockup was confirmed to mean `position_gap_ms`).
  Updated every pair in `_on_row_ready` (same cadence as the drop
  counters, skipping excluded pairs), pushed to the tiles in
  `_on_stats_ready` (same throttled cadence the live plots update on).

## Naming

The user-facing terms are "HW TS Latency" (was "Pairing gap") and
"Optical Sync" (was "Position gap") — checkboxes, axis labels, chart
legends, and stat tile labels all use the new names via
`LivePlot.add_series`'s `display_name` param. The underlying data/series
keys are unchanged (`pairing_gap_us`, `position_gap_ms`) — only display
text was renamed, not CSV columns or internal dict keys.

## Known visual limitations (not blocking, just honest about the gap)

- **Camera panel rounded corners**: `VideoPanel`'s `border-radius` styling
  paints correctly on the empty placeholder background, but Qt does
  **not** automatically clip a `QLabel`'s pixmap content to a
  rounded-corner mask — once a real video frame is showing, the corners
  will look square again despite the style. True clipping would need a
  custom paint event or a `QGraphicsEffect`/mask; not attempted since it's
  cosmetic-only and non-trivial.
- **Sidebar scrolling**: the mockup's stats sidebar has `overflow-y:auto`
  (scrolls if content overflows). Not implemented — with today's 8 tiles
  it fits fine on a maximized window, but if more stats get added later
  this may need a `QScrollArea`.
