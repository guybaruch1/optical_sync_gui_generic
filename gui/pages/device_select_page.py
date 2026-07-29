"""Wizard step 1: pick which connected RealSense device to use.

Device listing is fully generic (engine.streams.list_devices, no PID
restriction - see Task 3) so any connected RealSense device shows up here,
not just D535/D585. Layered on top of that: for the subset of devices that
ARE a D535/D585 Dedicated/Dual RGB variant (engine.rgb_mode.get_mode returns
non-None for them), offer to switch a Dedicated-mode device into Dual RGB
before proceeding - devices get_mode doesn't recognize (e.g. a D435/D455,
where it returns None) skip this step entirely.
"""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QComboBox, QPushButton, QApplication

from engine.streams import list_devices, find_device_by_serial
from engine.rgb_mode import get_mode, ensure_dual_rgb_mode


def _device_label(device_info, mode):
    """Formats a device's combo-box entry. `mode` is the
    engine.rgb_mode.get_mode() result for this device: "dual"/"dedicated"
    get a mode suffix, None (not a recognized D535/D585 variant) gets none."""
    if mode == "dual":
        return "{} - Dual RGB ({})".format(device_info.name, device_info.serial)
    if mode == "dedicated":
        return "{} - Dedicated RGB ({})".format(device_info.name, device_info.serial)
    return "{} ({})".format(device_info.name, device_info.serial)


class DeviceSelectPage(QWidget):
    device_chosen = Signal(str, str)  # (serial, name)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._devices = []
        self.ctx = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Select a connected RealSense device:"))
        self.combo = QComboBox()
        layout.addWidget(self.combo)
        self.status_label = QLabel("")
        layout.addWidget(self.status_label)
        self.next_button = QPushButton("Next")
        self.next_button.clicked.connect(self._on_next_clicked)
        layout.addWidget(self.next_button)

    def refresh_devices(self, ctx):
        self.ctx = ctx
        self._devices = list_devices(ctx)
        self.combo.clear()
        for device in self._devices:
            try:
                mode = get_mode(find_device_by_serial(ctx, device.serial))
            except Exception:
                # The device can vanish between list_devices()'s enumeration
                # and this per-device mode lookup (e.g. unplugged mid-
                # refresh). This method runs from MainWindow.__init__, so a
                # raise here would crash app startup - fall back to "no mode
                # suffix" for this entry instead.
                mode = None
            self.combo.addItem(_device_label(device, mode), userData=device.serial)

    def _on_next_clicked(self):
        serial = self.combo.currentData()
        if serial is None:
            return
        device_info = next((d for d in self._devices if d.serial == serial), None)
        if device_info is None:
            return
        # Emit the name alongside the serial so MainWindow doesn't need to
        # reach into this page's own _devices list later to look it up (which
        # also meant raising if the device had since disappeared, e.g. the
        # camera was unplugged mid-wizard).
        name = device_info.name

        try:
            device = find_device_by_serial(self.ctx, serial)
            mode = get_mode(device)
        except Exception as exc:
            # The device may have vanished (unplugged mid-wizard) between the
            # last refresh_devices() and this click - fail the same way an
            # ensure_dual_rgb_mode failure below does, instead of raising
            # uncaught out of a Qt slot.
            self.status_label.setText("Failed to read device: {}".format(exc))
            self.next_button.setEnabled(True)
            self.combo.setEnabled(True)
            return

        if mode == "dedicated":
            self.status_label.setText("Switching to Dual RGB mode - this takes a few seconds...")
            self.next_button.setEnabled(False)
            self.combo.setEnabled(False)
            QApplication.processEvents()
            try:
                ensure_dual_rgb_mode(self.ctx, device)
            except Exception as exc:
                self.status_label.setText("Failed to switch to Dual RGB mode: {}".format(exc))
                self.next_button.setEnabled(True)
                self.combo.setEnabled(True)
                return
            self.refresh_devices(self.ctx)
            index = self.combo.findData(serial)
            if index != -1:
                self.combo.setCurrentIndex(index)
            self.status_label.setText("")
            self.next_button.setEnabled(True)
            self.combo.setEnabled(True)

        self.device_chosen.emit(serial, name)
