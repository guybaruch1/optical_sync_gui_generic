"""Wizard step 1: pick which connected RealSense device to use.

Device listing is fully generic (engine.streams.list_devices, no PID
restriction - see Task 3) so any connected RealSense device shows up here,
not just D535/D585. Layered on top of that: for the subset of devices that
ARE a D535/D585 Dedicated/Dual RGB variant (engine.rgb_mode.get_mode returns
non-None for them), a "RGB Mode" choice (Dual RGB / Dedicated RGB radio
buttons) appears, defaulted to whatever mode the device is actually in
right now - devices get_mode doesn't recognize (e.g. a D435/D455, where it
returns None) never show this choice at all. Switching only actually
happens if the operator picks the OTHER mode than what's currently active;
picking the mode it's already in is a no-op, same as leaving the choice
untouched."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QComboBox, QPushButton, QApplication,
    QGroupBox, QRadioButton, QButtonGroup, QCheckBox,
)

from engine.streams import list_devices, find_device_by_serial
from engine.rgb_mode import get_mode, ensure_mode


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

        self.mode_group_box = QGroupBox("RGB Mode")
        mode_layout = QVBoxLayout(self.mode_group_box)
        self.dual_radio = QRadioButton("Dual RGB (2C)")
        self.dedicated_radio = QRadioButton("Dedicated RGB (3C)")
        mode_button_group = QButtonGroup(self.mode_group_box)
        mode_button_group.addButton(self.dual_radio)
        mode_button_group.addButton(self.dedicated_radio)
        mode_layout.addWidget(self.dual_radio)
        mode_layout.addWidget(self.dedicated_radio)
        self.mode_group_box.setVisible(False)
        layout.addWidget(self.mode_group_box)

        # Manual operator toggle, independent of which camera/test gets
        # picked afterward - the app has no way to know whether the
        # physical 2-panel rig (2 LED panels sharing one Acroname USB hub +
        # one external trigger relay) is actually connected right now, so
        # the operator decides. See engine/dual_panel_control.py and
        # settings.yaml's dual_panel: section (hub port numbers/relay COM
        # port - fixed wiring specifics, not a per-run choice).
        self.dual_panel_checkbox = QCheckBox("Use dual LED panel (Acroname hub + external trigger)")
        layout.addWidget(self.dual_panel_checkbox)

        self.combo.currentIndexChanged.connect(self._on_device_selection_changed)

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
        # currentIndexChanged may or may not have already fired while the
        # combo was being populated above (Qt's own timing, not relied on) -
        # call explicitly so the mode box always reflects whatever ends up
        # selected after a refresh.
        self._on_device_selection_changed(self.combo.currentIndex())

    def _on_device_selection_changed(self, index):
        serial = self.combo.itemData(index)
        mode = None
        if serial is not None and self.ctx is not None:
            try:
                mode = get_mode(find_device_by_serial(self.ctx, serial))
            except Exception:
                mode = None  # device vanished between refresh and this lookup
        if mode is None:
            self.mode_group_box.setVisible(False)
            return
        self.mode_group_box.setVisible(True)
        self.dual_radio.setChecked(mode == "dual")
        self.dedicated_radio.setChecked(mode == "dedicated")

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
            # ensure_mode failure below does, instead of raising uncaught
            # out of a Qt slot.
            self.status_label.setText("Failed to read device: {}".format(exc))
            self.next_button.setEnabled(True)
            self.combo.setEnabled(True)
            return

        if mode is not None:
            target_mode = "dual" if self.dual_radio.isChecked() else "dedicated"
            if target_mode != mode:
                target_label = "Dual RGB" if target_mode == "dual" else "Dedicated RGB"
                self.status_label.setText("Switching to {} mode - this takes a few seconds...".format(target_label))
                self.next_button.setEnabled(False)
                self.combo.setEnabled(False)
                self.mode_group_box.setEnabled(False)
                QApplication.processEvents()
                try:
                    ensure_mode(self.ctx, device, target_mode)
                except Exception as exc:
                    self.status_label.setText("Failed to switch to {} mode: {}".format(target_label, exc))
                    self.next_button.setEnabled(True)
                    self.combo.setEnabled(True)
                    self.mode_group_box.setEnabled(True)
                    return
                self.refresh_devices(self.ctx)
                index = self.combo.findData(serial)
                if index != -1:
                    self.combo.setCurrentIndex(index)
                self.status_label.setText("")
                self.next_button.setEnabled(True)
                self.combo.setEnabled(True)
                self.mode_group_box.setEnabled(True)

        self.device_chosen.emit(serial, name)
