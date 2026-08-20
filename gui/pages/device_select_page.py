"""Wizard step 1: pick which connected RealSense device to use.

Device listing is fully generic (engine.streams.list_devices, no PID
restriction - see Task 3) so any connected RealSense device shows up here,
not just D535/D585. For the subset of devices that ARE a D535/D585
Dedicated/Dual RGB variant (engine.rgb_mode.get_mode returns non-None for
them), each combo entry's label carries a "- Dual RGB"/"- Dedicated RGB"
suffix showing its CURRENT mode - purely informational here, since actually
switching mode now lives on Stream Config (gui/pages/stream_config_page.py),
not this page. It moved there because a mode switch changes which streams
the device exposes, which would invalidate whatever Test/Sensor Options
Stream Config had already resolved - the switch has to happen (and refresh
those lists) on the SAME page that shows them, and because Edit (see
gui/main_window.py's _on_edit_camera_requested) skips this page entirely,
so a control only reachable here wouldn't be reachable from Edit at all.

This page is now a pure device picker: pick one from the combo, click Next,
device_chosen fires immediately - no mode business, no per-run rig-wiring
checkbox (see stream_config_page.py's own dual_panel_checkbox, moved there
for the same "Edit skips this page" reason)."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QComboBox, QPushButton

from engine.streams import list_devices, find_device_by_serial
from engine.rgb_mode import get_mode


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
    back_requested = Signal()

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
        self.back_button = QPushButton("Back")
        self.back_button.clicked.connect(self.back_requested.emit)
        layout.addWidget(self.back_button)
        self.next_button = QPushButton("Next")
        self.next_button.clicked.connect(self._on_next_clicked)
        layout.addWidget(self.next_button)

    def refresh_devices(self, ctx, exclude_serials=None):
        # exclude_serials: every already-configured camera's serial (Camera
        # Hub's Add flow) - hides it here so the same physical camera can't
        # accidentally be added to the test twice. None/empty means nothing
        # excluded (MainWindow.__init__'s own unconditional first call, and
        # any direct-call test/tooling that doesn't go through the hub).
        exclude_serials = exclude_serials or set()
        self.ctx = ctx
        self._devices = [d for d in list_devices(ctx) if d.serial not in exclude_serials]
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
        self.device_chosen.emit(serial, device_info.name)
