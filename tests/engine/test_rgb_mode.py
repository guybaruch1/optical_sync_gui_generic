from engine.rgb_mode import get_mode


class FakeDevice:
    def __init__(self, pid, has_product_id=True):
        self._pid = pid
        self._has_product_id = has_product_id

    def get_info(self, info):
        return self._pid

    def supports(self, info):
        return self._has_product_id


def test_get_mode_returns_dual_for_dual_pid():
    assert get_mode(FakeDevice("0C04")) == "dual"


def test_get_mode_returns_dedicated_for_dedicated_pid():
    assert get_mode(FakeDevice("0C05")) == "dedicated"


def test_get_mode_returns_none_for_unrecognized_pid():
    assert get_mode(FakeDevice("FFFF")) is None


def test_get_mode_returns_none_when_device_has_no_product_id():
    assert get_mode(FakeDevice("0C04", has_product_id=False)) is None
