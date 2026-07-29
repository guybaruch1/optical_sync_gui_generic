from gui.pages.device_select_page import _device_label
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
