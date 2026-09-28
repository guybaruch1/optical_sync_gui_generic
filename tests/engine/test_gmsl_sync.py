"""engine.gmsl_sync's pure logic, tested against fake v4l2-ctl / TSC /
device collaborators - never real /dev nodes or ioctls (hardware-only,
same convention as engine/led_panel.py)."""

from unittest.mock import MagicMock

import pytest

from engine import gmsl_sync


# Real `v4l2-ctl -L` shape on the D585 GMSL driver (from
# check_d585_sync_v4l2.py's own docstring).
L_WITH_SYNC = (
    "User Controls\n"
    "\n"
    "      laser_power_on_off 0x009a4001 (bool)   : default=1 value=1 flags=volatile, execute-on-write\n"
    "        camera_sync_mode 0x009a4010 (menu)   : min=0 max=2 default=0 value=0\n"
    "                                0: Default\n"
    "                                1: Master\n"
    "                                2: External Sync\n"
)
L_WITHOUT_SYNC = "      exposure_absolute 0x009a0902 (int)    : min=1 max=200000 step=1 default=33 value=33\n"


def test_controls_in_ignores_menu_value_lines():
    assert gmsl_sync.controls_in(L_WITH_SYNC) == ["laser_power_on_off", "camera_sync_mode"]


def test_parse_control_value_handles_menu_label():
    assert gmsl_sync.parse_control_value("camera_sync_mode: 2 (External Sync)", "camera_sync_mode") == 2


def test_parse_control_value_plain_and_unparseable():
    assert gmsl_sync.parse_control_value("camera_sync_mode: 0", "camera_sync_mode") == 0
    assert gmsl_sync.parse_control_value("garbage", "camera_sync_mode") is None


def test_control_range_reads_min_max():
    assert gmsl_sync.control_range(L_WITH_SYNC, "camera_sync_mode") == (0, 2)
    assert gmsl_sync.control_range(L_WITHOUT_SYNC, "camera_sync_mode") is None


def _fake_v4l2(listing_by_node):
    def run(node, *args, timeout=15):
        if args == ("-L",):
            if node not in listing_by_node:
                return 1, "", "no such device"
            return 0, listing_by_node[node], ""
        raise AssertionError("unexpected v4l2-ctl call {} {}".format(node, args))
    return run


def _fake_glob(mapping):
    return lambda pattern: list(mapping.get(pattern, []))


def test_resolve_sync_nodes_uses_udev_symlinks_and_skips_metadata_nodes():
    listing = {"/dev/video-rs-depth-0": L_WITH_SYNC, "/dev/video-rs-depth-1": L_WITH_SYNC}
    glob_fn = _fake_glob({"/dev/video-rs-*": [
        "/dev/video-rs-depth-0", "/dev/video-rs-depth-md-0",
        "/dev/video-rs-depth-1", "/dev/video-rs-depth-md-1",
    ]})
    run = MagicMock(side_effect=_fake_v4l2(listing))

    nodes = gmsl_sync.resolve_sync_nodes("camera_sync_mode", run_v4l2=run, glob_fn=glob_fn)

    assert nodes == ["/dev/video-rs-depth-0", "/dev/video-rs-depth-1"]
    probed = {call.args[0] for call in run.call_args_list}
    assert not any("-md-" in node for node in probed)


def test_resolve_sync_nodes_falls_back_to_full_video_scan():
    listing = {"/dev/video0": L_WITHOUT_SYNC, "/dev/video2": L_WITH_SYNC,
               "/dev/video10": L_WITH_SYNC, "/dev/video-rs-color-0": L_WITHOUT_SYNC}
    glob_fn = _fake_glob({
        "/dev/video-rs-*": ["/dev/video-rs-color-0"],
        "/dev/video[0-9]*": ["/dev/video10", "/dev/video0", "/dev/video2"],
    })

    nodes = gmsl_sync.resolve_sync_nodes("camera_sync_mode", run_v4l2=_fake_v4l2(listing), glob_fn=glob_fn)

    assert nodes == ["/dev/video2", "/dev/video10"]  # numeric /dev order, not string order


def test_resolve_sync_nodes_raises_when_not_exactly_two():
    listing = {"/dev/video0": L_WITH_SYNC}
    glob_fn = _fake_glob({"/dev/video-rs-*": [], "/dev/video[0-9]*": ["/dev/video0"]})

    with pytest.raises(RuntimeError, match="expected 2"):
        gmsl_sync.resolve_sync_nodes("camera_sync_mode", run_v4l2=_fake_v4l2(listing), glob_fn=glob_fn)


def test_resolve_sync_nodes_reports_missing_v4l2_ctl():
    run = lambda node, *args, timeout=15: (127, "", "v4l2-ctl not found on PATH. Install it with 'sudo apt install v4l-utils'.")
    glob_fn = _fake_glob({"/dev/video-rs-*": ["/dev/video-rs-depth-0"], "/dev/video[0-9]*": ["/dev/video0"]})

    with pytest.raises(RuntimeError, match="v4l-utils"):
        gmsl_sync.resolve_sync_nodes("camera_sync_mode", run_v4l2=run, glob_fn=glob_fn)


class _FakeKernel:
    """Per-node control values, answering -L/-C/-c like v4l2-ctl."""

    def __init__(self, values, max_value=2, fail_write_on=(), ignore_write_on=(), unreadable=()):
        self.values = dict(values)
        self.max_value = max_value
        self.fail_write_on = set(fail_write_on)
        self.ignore_write_on = set(ignore_write_on)
        self.unreadable = set(unreadable)
        self.writes = []

    def __call__(self, node, *args, timeout=15):
        if args == ("-L",):
            return 0, "camera_sync_mode 0x009a4010 (menu) : min=0 max={} default=0 value={}".format(
                self.max_value, self.values[node]), ""
        if args == ("-C", "camera_sync_mode"):
            if node in self.unreadable:
                return 1, "", "read failed"
            return 0, "camera_sync_mode: {} (label)".format(self.values[node]), ""
        if len(args) == 2 and args[0] == "-c":
            value = int(args[1].split("=")[1])
            self.writes.append((node, value))
            if node in self.fail_write_on:
                return 1, "", "VIDIOC_S_EXT_CTRLS: failed: Invalid argument"
            if node not in self.ignore_write_on:
                self.values[node] = value
            return 0, "", ""
        raise AssertionError(args)


NODES = ["/dev/video2", "/dev/video10"]


def test_apply_sync_mode_writes_confirms_and_returns_as_found():
    kernel = _FakeKernel({"/dev/video2": 0, "/dev/video10": 1})

    as_found = gmsl_sync.apply_sync_mode(NODES, "camera_sync_mode", 2, run_v4l2=kernel)

    assert as_found == {"/dev/video2": 0, "/dev/video10": 1}
    assert kernel.values == {"/dev/video2": 2, "/dev/video10": 2}


def test_apply_sync_mode_out_of_range_writes_nothing():
    kernel = _FakeKernel({"/dev/video2": 0, "/dev/video10": 0}, max_value=2)

    with pytest.raises(RuntimeError, match="0..2"):
        gmsl_sync.apply_sync_mode(NODES, "camera_sync_mode", 3, run_v4l2=kernel)
    assert kernel.writes == []


def test_apply_sync_mode_write_failure_restores_earlier_node():
    kernel = _FakeKernel({"/dev/video2": 0, "/dev/video10": 0}, fail_write_on={"/dev/video10"})

    with pytest.raises(RuntimeError, match="/dev/video10"):
        gmsl_sync.apply_sync_mode(NODES, "camera_sync_mode", 2, run_v4l2=kernel)
    assert kernel.values["/dev/video2"] == 0  # restored


def test_apply_sync_mode_readback_mismatch_raises_and_restores():
    kernel = _FakeKernel({"/dev/video2": 0, "/dev/video10": 0}, ignore_write_on={"/dev/video10"})

    with pytest.raises(RuntimeError, match="readback"):
        gmsl_sync.apply_sync_mode(NODES, "camera_sync_mode", 2, run_v4l2=kernel)
    assert kernel.values == {"/dev/video2": 0, "/dev/video10": 0}


def test_restore_writes_as_found_values():
    kernel = _FakeKernel({"/dev/video2": 2, "/dev/video10": 2})

    gmsl_sync.restore_sync_mode({"/dev/video2": 0, "/dev/video10": 1}, "camera_sync_mode", run_v4l2=kernel)

    assert kernel.values == {"/dev/video2": 0, "/dev/video10": 1}


def test_restore_skips_unreadable_as_found():
    kernel = _FakeKernel({"/dev/video2": 2, "/dev/video10": 2})

    gmsl_sync.restore_sync_mode({"/dev/video2": None, "/dev/video10": 0}, "camera_sync_mode", run_v4l2=kernel)

    assert kernel.writes == [("/dev/video10", 0)]


def test_restore_never_raises():
    def exploding(node, *args, timeout=15):
        raise OSError("gone")

    gmsl_sync.restore_sync_mode({"/dev/video2": 0}, "camera_sync_mode", run_v4l2=exploding)  # must not raise
