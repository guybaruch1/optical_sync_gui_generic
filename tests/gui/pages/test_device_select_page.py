import pyrealsense2 as rs

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


# --- The combo entry's mode suffix is purely informational here - actually
# switching mode now lives on Stream Config (see that page's own tests) ---

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


def test_refresh_devices_labels_show_each_devices_current_mode(qapp):
    page = DeviceSelectPage()
    page.refresh_devices(FakeCtx([D585_DUAL, D585_DEDICATED, D435]))

    labels = [page.combo.itemText(i) for i in range(page.combo.count())]
    assert labels == [
        "Intel RealSense D585 - Dual RGB (SN_DUAL)",
        "Intel RealSense D585 - Dedicated RGB (SN_DEDICATED)",
        "Intel RealSense D435 (SN_D435)",
    ]


# --- Next: a pure device pick, emitted immediately - no mode business at
# all (that moved to Stream Config, which Edit reaches directly - see
# gui/main_window.py's _on_edit_camera_requested) ---

def test_next_clicked_emits_the_selected_device(qapp):
    page = DeviceSelectPage()
    page.refresh_devices(FakeCtx([D585_DUAL]))

    emitted = []
    page.device_chosen.connect(lambda serial, name: emitted.append((serial, name)))
    page._on_next_clicked()

    assert emitted == [("SN_DUAL", "Intel RealSense D585")]


def test_next_clicked_does_nothing_when_nothing_selected(qapp):
    page = DeviceSelectPage()
    page.refresh_devices(FakeCtx([]))

    emitted = []
    page.device_chosen.connect(lambda serial, name: emitted.append((serial, name)))
    page._on_next_clicked()

    assert emitted == []


# --- Back button: Device Select is the first page of a camera's sub-flow -
# nothing running here to stop, so Back just emits back_requested (MainWindow
# routes it to the Camera Hub) ---

def test_back_button_emits_back_requested(qapp):
    page = DeviceSelectPage()
    emitted = []
    page.back_requested.connect(lambda: emitted.append(True))

    page.back_button.click()

    assert emitted == [True]


# --- exclude_serials: hides an already-configured camera from the picker,
# so adding a new camera can't accidentally re-select one already in use ---

def test_refresh_devices_excludes_already_configured_serials(qapp):
    page = DeviceSelectPage()
    page.refresh_devices(FakeCtx([D585_DUAL, D585_DEDICATED, D435]), exclude_serials={"SN_DEDICATED"})

    serials = [page.combo.itemData(i) for i in range(page.combo.count())]
    assert serials == ["SN_DUAL", "SN_D435"]


def test_refresh_devices_excludes_nothing_by_default(qapp):
    page = DeviceSelectPage()
    page.refresh_devices(FakeCtx([D585_DUAL, D435]))

    assert page.combo.count() == 2
