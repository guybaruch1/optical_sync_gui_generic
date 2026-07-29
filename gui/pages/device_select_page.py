"""Wizard step 1: pick which connected RealSense device to use."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QComboBox, QPushButton

from engine.streams import list_devices


class DeviceSelectPage(QWidget):
    device_chosen = Signal(str, str)  # (serial, name)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._devices = []

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Select a connected RealSense device:"))
        self.combo = QComboBox()
        layout.addWidget(self.combo)
        self.next_button = QPushButton("Next")
        self.next_button.clicked.connect(self._on_next_clicked)
        layout.addWidget(self.next_button)

    def refresh_devices(self, ctx):
        self._devices = list_devices(ctx)
        self.combo.clear()
        for device in self._devices:
            self.combo.addItem("{} ({})".format(device.name, device.serial), userData=device.serial)

    def _on_next_clicked(self):
        serial = self.combo.currentData()
        if serial is None:
            return
        # Emit the name alongside the serial so MainWindow doesn't need to
        # reach into this page's own _devices list later to look it up (which
        # also meant raising if the device had since disappeared, e.g. the
        # camera was unplugged mid-wizard).
        name = next((d.name for d in self._devices if d.serial == serial), "")
        self.device_chosen.emit(serial, name)
