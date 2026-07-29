"""Generic live key/value readout. Two layouts, both grouped under section
headers (e.g. "Live Data", "Stats") - matches the Optical Sync GUI design
mockup (claude.ai/design project "GUI layout redesign options", file
Optical Sync GUI.dc.html): individual "stat tile" cards (add_field) for
single values, and a compact min/avg/std/max grid (add_stats_table) for
a RunningStats-backed metric. Both register their value QLabels in the
same _value_labels dict, so set_value() works identically either way."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QGridLayout, QLabel, QFrame, QSizePolicy

SECTION_HEADER_STYLE = (
    "color: #555555; font-weight: 600; font-size: 10pt;"
    "text-transform: uppercase; letter-spacing: 1px; border: none; background: transparent;"
)
TILE_STYLE = "background-color: #ffffff; border: 1px solid #e3e1db; border-radius: 6px;"
TILE_LABEL_STYLE = (
    "color: #555555; font-weight: 600; font-size: 9pt;"
    "text-transform: uppercase; letter-spacing: 1px; border: none; background: transparent;"
)
TILE_VALUE_STYLE = "color: #1b7a63; font-weight: 700; font-size: 15pt; border: none; background: transparent;"

STATS_TABLE_COLUMNS = ("min", "avg", "std", "max")
STATS_TABLE_HEADER_STYLE = "color: #898781; font-weight: 600; font-size: 8pt; border: none; background: transparent;"
STATS_TABLE_ROW_LABEL_STYLE = "color: #3a3a3a; font-weight: 600; font-size: 9pt; border: none; background: transparent;"
STATS_TABLE_VALUE_STYLE = "color: #1b7a63; font-weight: 700; font-size: 10pt; border: none; background: transparent;"


class StatsPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(10)
        self._value_labels = {}

    def add_section_header(self, text):
        header = QLabel(text)
        header.setStyleSheet(SECTION_HEADER_STYLE)
        self._layout.addWidget(header)

    def add_field(self, key, label):
        tile = QFrame()
        tile.setStyleSheet(TILE_STYLE)
        tile_layout = QVBoxLayout(tile)
        tile_layout.setContentsMargins(10, 8, 10, 8)
        tile_layout.setSpacing(2)

        label_widget = QLabel(label)
        label_widget.setStyleSheet(TILE_LABEL_STYLE)
        value_widget = QLabel("-")
        value_widget.setStyleSheet(TILE_VALUE_STYLE)
        tile_layout.addWidget(label_widget)
        tile_layout.addWidget(value_widget)

        self._value_labels[key] = value_widget
        self._layout.addWidget(tile)

    def add_stats_table(self, rows):
        """rows: list of (key, label) pairs, one per metric. Builds a header
        row (blank corner + min/avg/std/max) plus one row per metric, each
        with 4 value cells registered as "{key}_min"/"{key}_avg"/"{key}_std"/
        "{key}_max" - so set_value() works unchanged, no new setter needed."""
        table = QWidget()
        grid = QGridLayout(table)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)

        for col, column_name in enumerate(STATS_TABLE_COLUMNS, start=1):
            header = QLabel(column_name)
            header.setStyleSheet(STATS_TABLE_HEADER_STYLE)
            header.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            grid.addWidget(header, 0, col)

        for row, (key, label) in enumerate(rows, start=1):
            label_widget = QLabel(label)
            label_widget.setStyleSheet(STATS_TABLE_ROW_LABEL_STYLE)
            label_widget.setWordWrap(True)
            grid.addWidget(label_widget, row, 0)
            for col, column_name in enumerate(STATS_TABLE_COLUMNS, start=1):
                value_widget = QLabel("-")
                value_widget.setStyleSheet(STATS_TABLE_VALUE_STYLE)
                value_widget.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                grid.addWidget(value_widget, row, col)
                self._value_labels["{}_{}".format(key, column_name)] = value_widget

        grid.setColumnStretch(0, 1)
        self._layout.addWidget(table)

    def set_value(self, key, value):
        if key not in self._value_labels:
            return
        self._value_labels[key].setText(str(value))
