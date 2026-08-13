"""New wizard hub/overview page for the multi-camera sync test: lists every
configured camera as a card (name, master badge, configured/needs-setup
state) and lets the operator add up to MAX_CAMERAS cameras, edit any one
non-linearly (re-enters the existing, unmodified Device Select -> Stream
Config -> ROI Select -> Calibration -> Threshold Tuning flow for just that
camera_id - see gui/main_window.py), designate exactly one as master, or
remove one, then start the multi-camera Live Session once every configured
camera has finished its own sub-flow and exactly one master is designated.

This page is deliberately a "dumb" view, same convention as every other
wizard page: it holds no persistent camera configuration itself and never
decides which camera IS master on its own - MainWindow owns that state
(self._cameras / self._master_camera_id per the multi-camera design doc)
and drives this page's display via set_cameras(); this page only emits
events for MainWindow to react to. See docs/superpowers's multi-camera
design doc's "Design detail" section 4."""

from dataclasses import dataclass

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QGroupBox


@dataclass
class CameraSummary:
    """One card's worth of display state - camera_id is the only thing
    MainWindow needs back on any of this page's signals; label/is_master/
    configured are purely for display."""
    camera_id: str
    label: str
    is_master: bool
    configured: bool


class _CameraCard(QGroupBox):
    def __init__(self, summary, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)

        text = summary.label
        if summary.is_master:
            text += "  [MASTER]"
        if not summary.configured:
            text += "  (needs setup)"
        self.label_widget = QLabel(text)
        layout.addWidget(self.label_widget)

        self.edit_button = QPushButton("Edit")
        layout.addWidget(self.edit_button)

        self.master_button = QPushButton("Set as Master")
        self.master_button.setEnabled(not summary.is_master)
        layout.addWidget(self.master_button)

        self.remove_button = QPushButton("Remove")
        layout.addWidget(self.remove_button)


class CameraHubPage(QWidget):
    add_camera_requested = Signal()
    edit_camera_requested = Signal(str)   # camera_id
    master_change_requested = Signal(str)  # camera_id
    remove_camera_requested = Signal(str)  # camera_id
    start_multi_camera_session_requested = Signal()

    # "up to 3 cameras (up to 6 sensors)" per the multi-camera design doc.
    MAX_CAMERAS = 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self._summaries = []
        self._cards = {}  # camera_id -> _CameraCard, rebuilt on every set_cameras()

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Configured cameras:"))

        self._cards_container = QWidget()
        self._cards_layout = QVBoxLayout(self._cards_container)
        layout.addWidget(self._cards_container)

        self.add_camera_button = QPushButton("Add Camera")
        self.add_camera_button.clicked.connect(self.add_camera_requested.emit)
        layout.addWidget(self.add_camera_button)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

        self.start_button = QPushButton("Start Multi-Camera Live Session")
        self.start_button.clicked.connect(self._on_start_clicked)
        layout.addWidget(self.start_button)

        self.set_cameras([])

    def set_cameras(self, summaries):
        """Rebuilds every card from scratch - a camera removed from
        `summaries` must not leave a stale card behind, and this page never
        tries to diff/reuse cards across calls (simpler, and the card count
        is always small - at most MAX_CAMERAS)."""
        self._summaries = list(summaries)

        while self._cards_layout.count():
            item = self._cards_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._cards = {}

        for summary in self._summaries:
            card = _CameraCard(summary)
            card.edit_button.clicked.connect(
                lambda _=None, cid=summary.camera_id: self.edit_camera_requested.emit(cid)
            )
            card.master_button.clicked.connect(
                lambda _=None, cid=summary.camera_id: self.master_change_requested.emit(cid)
            )
            card.remove_button.clicked.connect(
                lambda _=None, cid=summary.camera_id: self.remove_camera_requested.emit(cid)
            )
            self._cards_layout.addWidget(card)
            self._cards[summary.camera_id] = card

        self.add_camera_button.setEnabled(len(self._summaries) < self.MAX_CAMERAS)
        self.start_button.setEnabled(self._can_start())

    def _can_start(self):
        if not self._summaries:
            return False
        if not all(summary.configured for summary in self._summaries):
            return False
        return sum(1 for summary in self._summaries if summary.is_master) == 1

    def _on_start_clicked(self):
        if self._can_start():
            self.start_multi_camera_session_requested.emit()
