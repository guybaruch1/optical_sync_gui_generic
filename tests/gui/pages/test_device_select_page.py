import pyrealsense2 as rs

import gui.pages.device_select_page as device_select_page_module
from gui.pages.device_select_page import DeviceSelectPage, _device_label
from engine.streams import DeviceInfo


def test_device_label_shows_dual_rgb_mode():
    info = DeviceInfo(name="Intel RealSense D585", serial="123456789")
    assert _device_label(info, "dual") == "Intel RealSense D585 - Dual RGB (123456789)"


def test_device_label_shows_dedicated_rgb_mode():
    info = DeviceInfo(name="Intel RealSense D585", serial="123456789")
    assert _device_label(info, "dedicated") == "Intel RealSense D585 - Dedicated RGB (123456789)"


def test_device_label_omits_mode_suffix_for_non_d585_family_device():
    # get_mode() returns None for devices with no Dedicated/Dual RGB concept
    # at all (e.g. a D435/D455) - the label should just show name (serial),
    # with no mode suffix.
    info = DeviceInfo(name="Intel RealSense D435", serial="987654321")
    assert _device_label(info, None) == "Intel RealSense D435 (987654321)"


# --- RGB Mode choice (2C/3C radio buttons): only shown for a recognized
# D535/D585 variant, defaulted to whatever mode the device is actually in ---

class FakeDevice:
    def __init__(self, name, serial, pid):
        self._name = name
        self._serial = serial
        self._pid = pid

    def get_info(self, info):
        if info == rs.camera_info.name:
            return self._name
        if info == rs.camera_info.serial_number:
            return self._serial
        if info == rs.camera_info.product_id:
            return self._pid
        return None

    def supports(self, info):
        return info == rs.camera_info.product_id


class FakeCtx:
    def __init__(self, devices):
        self._devices = devices

    def query_devices(self):
        return self._devices


D585_DUAL = FakeDevice("Intel RealSense D585", "SN_DUAL", "0C04")
D585_DEDICATED = FakeDevice("Intel RealSense D585", "SN_DEDICATED", "0C05")
D435 = FakeDevice("Intel RealSense D435", "SN_D435", "0000")  # unrecognized PID - no 2C/3C concept


def test_selecting_dual_rgb_d585_shows_mode_box_checked_to_dual(qapp):
    page = DeviceSelectPage()
    page.refresh_devices(FakeCtx([D585_DUAL]))
    assert page.mode_group_box.isVisibleTo(page)
    assert page.dual_radio.isChecked()
    assert not page.dedicated_radio.isChecked()


def test_selecting_dedicated_rgb_d585_shows_mode_box_checked_to_dedicated(qapp):
    page = DeviceSelectPage()
    page.refresh_devices(FakeCtx([D585_DEDICATED]))
    assert page.mode_group_box.isVisibleTo(page)
    assert page.dedicated_radio.isChecked()
    assert not page.dual_radio.isChecked()


def test_selecting_non_family_device_hides_mode_box(qapp):
    page = DeviceSelectPage()
    page.refresh_devices(FakeCtx([D435]))
    assert not page.mode_group_box.isVisibleTo(page)


def test_switching_from_dedicated_to_dual_device_updates_mode_box(qapp):
    page = DeviceSelectPage()
    page.refresh_devices(FakeCtx([D585_DEDICATED, D585_DUAL]))
    assert page.dedicated_radio.isChecked()  # first device, index 0

    page.combo.setCurrentIndex(1)  # the dual-mode device

    assert page.dual_radio.isChecked()
    assert not page.dedicated_radio.isChecked()


# --- _on_next_clicked: only actually switches mode if the chosen radio
# differs from the device's current mode ---

def test_next_does_not_switch_mode_when_chosen_radio_matches_current_mode(qapp, monkeypatch):
    calls = []
    monkeypatch.setattr(device_select_page_module, "ensure_mode", lambda ctx, device, target_mode: calls.append(target_mode))
    page = DeviceSelectPage()
    page.refresh_devices(FakeCtx([D585_DUAL]))
    assert page.dual_radio.isChecked()  # already matches current mode - untouched

    emitted = []
    page.device_chosen.connect(lambda serial, name: emitted.append((serial, name)))
    page._on_next_clicked()

    assert calls == []
    assert emitted == [("SN_DUAL", "Intel RealSense D585")]


def test_next_switches_mode_when_chosen_radio_differs_from_current_mode(qapp, monkeypatch):
    calls = []
    monkeypatch.setattr(device_select_page_module, "ensure_mode", lambda ctx, device, target_mode: calls.append(target_mode))
    page = DeviceSelectPage()
    page.refresh_devices(FakeCtx([D585_DUAL]))
    page.dedicated_radio.setChecked(True)  # operator explicitly picked the OTHER mode

    emitted = []
    page.device_chosen.connect(lambda serial, name: emitted.append((serial, name)))
    page._on_next_clicked()

    assert calls == ["dedicated"]
    assert emitted == [("SN_DUAL", "Intel RealSense D585")]


def test_next_shows_error_and_does_not_emit_when_switch_fails(qapp, monkeypatch):
    def _raise(ctx, device, target_mode):
        raise RuntimeError("hardware reset timed out")

    monkeypatch.setattr(device_select_page_module, "ensure_mode", _raise)
    page = DeviceSelectPage()
    page.refresh_devices(FakeCtx([D585_DUAL]))
    page.dedicated_radio.setChecked(True)

    emitted = []
    page.device_chosen.connect(lambda serial, name: emitted.append((serial, name)))
    page._on_next_clicked()

    assert emitted == []
    assert "Failed to switch" in page.status_label.text()
    assert page.next_button.isEnabled()
    assert page.combo.isEnabled()
    assert page.mode_group_box.isEnabled()


def test_next_does_not_touch_mode_for_non_family_device(qapp, monkeypatch):
    calls = []
    monkeypatch.setattr(device_select_page_module, "ensure_mode", lambda ctx, device, target_mode: calls.append(target_mode))
    page = DeviceSelectPage()
    page.refresh_devices(FakeCtx([D435]))

    emitted = []
    page.device_chosen.connect(lambda serial, name: emitted.append((serial, name)))
    page._on_next_clicked()

    assert calls == []
    assert emitted == [("SN_D435", "Intel RealSense D435")]


# --- Dual LED panel checkbox: a manual, operator-driven choice, independent
# of which camera/device gets picked - see engine/dual_panel_control.py ---

def test_dual_panel_checkbox_defaults_unchecked(qapp):
    page = DeviceSelectPage()
    assert not page.dual_panel_checkbox.isChecked()


def test_dual_panel_checkbox_stays_checked_across_device_refresh(qapp):
    # Confirms the checkbox's state isn't tied to/reset by device
    # selection - it's an independent, persistent operator choice.
    page = DeviceSelectPage()
    page.dual_panel_checkbox.setChecked(True)
    page.refresh_devices(FakeCtx([D435]))
    assert page.dual_panel_checkbox.isChecked()
