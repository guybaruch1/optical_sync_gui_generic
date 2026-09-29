"""engine.gmsl_sync's pure logic, tested against fake v4l2-ctl / TSC /
device collaborators - never real /dev nodes or ioctls (hardware-only,
same convention as engine/led_panel.py)."""

from unittest.mock import MagicMock

import pytest
import pyrealsense2 as rs

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


class _FakeExtModule:
    CDI_TSC_DEV = "/dev/cdi_tsc"

    def __init__(self, fail_first_stop=False, fail_start=False):
        self.calls = []
        self._fail_first_stop = fail_first_stop
        self._fail_start = fail_start

    def tsc_fsync(self, fd, on):
        self.calls.append(("fsync", fd, on))
        if on == 0 and self._fail_first_stop:
            self._fail_first_stop = False
            raise OSError("not running")
        if on == 1 and self._fail_start:
            raise OSError("ioctl failed")

    def tsc_set_rate(self, fd, fps, duty):
        self.calls.append(("set_rate", fd, fps, duty))


def _tsc_io(ext):
    closed = []
    io = gmsl_sync.KernelTscIO(ext_module=ext, open_fn=lambda path, flags: 7, close_fn=closed.append)
    return io, closed


def test_kernel_tsc_io_start_matches_ext_sync_gen_enable_sequence():
    ext = _FakeExtModule(fail_first_stop=True)  # a leading stop that fails is ignored, like the script
    io, closed = _tsc_io(ext)

    io.start(30, 50)

    assert ext.calls == [("fsync", 7, 0), ("set_rate", 7, 30, 50), ("fsync", 7, 1)]
    assert closed == [7]


def test_kernel_tsc_io_stop_and_fd_closed_on_failure():
    ext = _FakeExtModule(fail_start=True)
    io, closed = _tsc_io(ext)

    with pytest.raises(RuntimeError, match="ioctl"):
        io.start(30, 50)
    assert closed == [7]

    io.stop()
    assert ext.calls[-1] == ("fsync", 7, 0)


def test_kernel_tsc_io_permission_error_mentions_udev_rule():
    io = gmsl_sync.KernelTscIO(ext_module=_FakeExtModule(),
                               open_fn=MagicMock(side_effect=PermissionError()), close_fn=MagicMock())
    with pytest.raises(RuntimeError, match='KERNEL=="cdi_tsc", MODE="0666"'):
        io.start(30, 50)


def _gmsl_sync(order, kernel=None, tsc_start_error=None):
    kernel = kernel or _FakeKernel({"/dev/video2": 0, "/dev/video10": 0})

    def run(node, *args, timeout=15):
        if args and args[0] == "-c":
            order.append(("write", node, args[1]))
        return kernel(node, *args, timeout=timeout)

    def tsc_start(fps, duty):
        order.append(("tsc_start", fps, duty))
        if tsc_start_error is not None:
            raise tsc_start_error

    tsc = MagicMock()
    tsc.start.side_effect = tsc_start
    tsc.stop.side_effect = lambda: order.append(("tsc_stop",))
    glob_fn = _fake_glob({"/dev/video-rs-*": NODES})
    sync = gmsl_sync.GmslTscSync(
        control="camera_sync_mode", sync_mode_value=2, fps=30, duty_percent=50, settle_s=5.0,
        run_v4l2=run, tsc_io=tsc, sleep=lambda s: order.append(("sleep", s)), glob_fn=glob_fn,
    )
    return sync, kernel, tsc


def test_engage_order_is_mode_then_tsc_then_settle():
    order = []
    sync, kernel, _ = _gmsl_sync(order)

    sync.engage()

    assert order == [("write", "/dev/video2", "camera_sync_mode=2"),
                     ("write", "/dev/video10", "camera_sync_mode=2"),
                     ("tsc_start", 30, 50), ("sleep", 5.0)]


def test_engage_restores_mode_when_tsc_start_fails():
    order = []
    sync, kernel, _ = _gmsl_sync(order, tsc_start_error=RuntimeError("ioctl failed"))

    with pytest.raises(RuntimeError, match="ioctl"):
        sync.engage()

    assert kernel.values == {"/dev/video2": 0, "/dev/video10": 0}
    assert ("sleep", 5.0) not in order


def test_disengage_stops_tsc_then_restores_and_is_idempotent():
    order = []
    sync, kernel, tsc = _gmsl_sync(order, kernel=_FakeKernel({"/dev/video2": 1, "/dev/video10": 0}))
    sync.engage()
    order.clear()

    sync.disengage()
    sync.disengage()

    assert order[0] == ("tsc_stop",)
    assert kernel.values == {"/dev/video2": 1, "/dev/video10": 0}
    assert tsc.stop.call_count == 1


def test_disengage_after_failed_engage_is_noop():
    order = []
    kernel = _FakeKernel({"/dev/video2": 0, "/dev/video10": 0}, fail_write_on={"/dev/video2"})
    sync, _, tsc = _gmsl_sync(order, kernel=kernel)
    with pytest.raises(RuntimeError):
        sync.engage()

    sync.disengage()  # must not raise

    tsc.stop.assert_not_called()


def test_disengage_never_raises_when_tsc_stop_fails():
    order = []
    sync, kernel, tsc = _gmsl_sync(order)
    sync.engage()
    tsc.stop.side_effect = OSError("gone")

    sync.disengage()  # must not raise

    assert kernel.values == {"/dev/video2": 0, "/dev/video10": 0}  # restore still ran


def test_disengage_all_engaged_undoes_only_engaged_syncs():
    # Final-review I2/M5: closing the app mid-run must restore the kernel
    # mode too (a camera left in external sync gives no frames on the next
    # free-running run), and must not touch a trigger this app never started.
    order = []
    engaged, kernel, tsc = _gmsl_sync(order, kernel=_FakeKernel({"/dev/video2": 0, "/dev/video10": 0}))
    never_engaged, _, idle_tsc = _gmsl_sync([])
    engaged.engage()

    gmsl_sync.disengage_all_engaged()

    tsc.stop.assert_called_once()
    idle_tsc.stop.assert_not_called()
    assert kernel.values == {"/dev/video2": 0, "/dev/video10": 0}

    gmsl_sync.disengage_all_engaged()  # nothing left - a no-op
    tsc.stop.assert_called_once()


def _device(name="Intel RealSense D585", usb=False):
    device = MagicMock()
    device.get_info.side_effect = lambda info: name if info == rs.camera_info.name else "x"
    device.supports.side_effect = lambda info: usb if info == rs.camera_info.usb_type_descriptor else True
    return device


REMOTE = {"mode": "remote"}


def _detect(panel_connection=REMOTE, devices=None, tsc_exists=True):
    devices = devices if devices is not None else {"s1": _device(), "s2": _device()}
    return gmsl_sync.detect_gmsl_tsc_rig(
        panel_connection, list(devices), lambda serial: devices[serial],
        path_exists=lambda path: tsc_exists,
    )


def test_detect_true_for_two_gmsl_d585_on_remote():
    assert _detect() is True


@pytest.mark.parametrize("kwargs", [
    {"panel_connection": {"mode": "local"}},
    {"panel_connection": {}},
    {"devices": {"s1": _device()}},
    {"devices": {"s1": _device(), "s2": _device(), "s3": _device()}},
    {"devices": {"s1": _device(), "s2": _device(name="Intel RealSense D455")}},
    {"devices": {"s1": _device(), "s2": _device(usb=True)}},
    {"tsc_exists": False},
])
def test_detect_false_when_any_condition_fails(kwargs):
    assert _detect(**kwargs) is False


def test_detect_false_when_lookup_raises():
    def lookup(serial):
        raise RuntimeError("No connected device")

    assert gmsl_sync.detect_gmsl_tsc_rig(REMOTE, ["s1", "s2"], lookup, path_exists=lambda p: True) is False


def test_detect_never_looks_up_devices_in_local_mode():
    lookup = MagicMock()
    gmsl_sync.detect_gmsl_tsc_rig({"mode": "local"}, ["s1", "s2"], lookup, path_exists=lambda p: True)
    lookup.assert_not_called()


def test_resolve_sync_nodes_stops_at_first_missing_v4l2_ctl():
    # M2: the "install v4l-utils" line must not repeat once per probed node.
    run = MagicMock(return_value=(127, "", "v4l2-ctl not found on PATH. Install it with 'sudo apt install v4l-utils'."))
    glob_fn = _fake_glob({"/dev/video-rs-*": ["/dev/video-rs-depth-0", "/dev/video-rs-depth-1"],
                          "/dev/video[0-9]*": ["/dev/video0", "/dev/video1", "/dev/video2"]})

    with pytest.raises(RuntimeError) as excinfo:
        gmsl_sync.resolve_sync_nodes("camera_sync_mode", run_v4l2=run, glob_fn=glob_fn)

    assert str(excinfo.value).count("v4l-utils") == 1
    assert run.call_count == 1


def test_kernel_tsc_io_missing_device_is_a_friendly_error():
    # M6
    io = gmsl_sync.KernelTscIO(ext_module=_FakeExtModule(),
                               open_fn=MagicMock(side_effect=FileNotFoundError()), close_fn=MagicMock())
    with pytest.raises(RuntimeError, match="TSC driver not loaded"):
        io.start(30, 50)


def test_validate_gmsl_tsc_settings_rejects_unknown_keys():
    # M4
    assert gmsl_sync.unknown_gmsl_tsc_settings_keys({"duty_percent": 25}) == []
    assert gmsl_sync.unknown_gmsl_tsc_settings_keys({"duty_pct": 25, "settle": 1}) == ["duty_pct", "settle"]


# --- Self-heal after a killed run: leftover external-sync mode / trigger. ---

def test_control_default_reads_driver_default():
    assert gmsl_sync.control_default(L_WITH_SYNC, "camera_sync_mode") == 0
    assert gmsl_sync.control_default(L_WITHOUT_SYNC, "camera_sync_mode") is None


def test_apply_sync_mode_restores_to_driver_default_when_found_already_in_sync_mode():
    # A killed ticked run leaves mode 2; restoring "as found" would keep it
    # stuck at 2 forever.
    kernel = _FakeKernel({"/dev/video2": 2, "/dev/video10": 0})

    restore_to = gmsl_sync.apply_sync_mode(NODES, "camera_sync_mode", 2, run_v4l2=kernel)

    assert restore_to == {"/dev/video2": 0, "/dev/video10": 0}


def _free_run(kernel, tsc=None):
    tsc = tsc or MagicMock()
    return gmsl_sync.reset_leftover_sync(
        "camera_sync_mode", run_v4l2=kernel, tsc_io=tsc, glob_fn=_fake_glob({"/dev/video-rs-*": NODES})), tsc


def test_reset_leftover_sync_resets_only_non_default_nodes_and_stops_trigger():
    kernel = _FakeKernel({"/dev/video2": 2, "/dev/video10": 0})

    reset_nodes, tsc = _free_run(kernel)

    assert reset_nodes == ["/dev/video2"]
    assert kernel.writes == [("/dev/video2", 0)]
    tsc.stop.assert_called_once()


def test_reset_leftover_sync_on_clean_rig_writes_nothing_but_still_stops_trigger():
    # The trigger has no GET ioctl - a still-pulsing one from a killed run
    # can't be detected, and a free-running run must not have one.
    kernel = _FakeKernel({"/dev/video2": 0, "/dev/video10": 0})

    reset_nodes, tsc = _free_run(kernel)

    assert reset_nodes == []
    assert kernel.writes == []
    tsc.stop.assert_called_once()


def test_reset_leftover_sync_raises_when_reset_does_not_stick():
    kernel = _FakeKernel({"/dev/video2": 2, "/dev/video10": 0}, ignore_write_on={"/dev/video2"})

    with pytest.raises(RuntimeError, match="free-running"):
        _free_run(kernel)


def test_reset_leftover_sync_raises_when_trigger_stop_fails():
    tsc = MagicMock()
    tsc.stop.side_effect = RuntimeError("TSC ioctl failed: x")

    with pytest.raises(RuntimeError, match="TSC"):
        _free_run(_FakeKernel({"/dev/video2": 0, "/dev/video10": 0}), tsc=tsc)


def test_free_run_guard_engage_resets_and_disengage_is_a_noop():
    kernel = _FakeKernel({"/dev/video2": 2, "/dev/video10": 2})
    tsc = MagicMock()
    guard = gmsl_sync.GmslFreeRunGuard(control="camera_sync_mode", run_v4l2=kernel, tsc_io=tsc,
                                       glob_fn=_fake_glob({"/dev/video-rs-*": NODES}))

    guard.engage()
    writes_after_engage = list(kernel.writes)
    guard.disengage()
    gmsl_sync.disengage_all_engaged()

    assert guard.reset_nodes == NODES
    assert kernel.writes == writes_after_engage  # nothing written back
    tsc.stop.assert_called_once()


# --- Review fixes: restore reporting, unreadable as-found, any-count free-run
# cleanup, single-camera detection. ---

def test_restore_reports_write_failure_and_readback_mismatch():
    kernel = _FakeKernel({"/dev/video2": 2, "/dev/video10": 2},
                         fail_write_on={"/dev/video2"}, ignore_write_on={"/dev/video10"})

    problems = gmsl_sync.restore_sync_mode({"/dev/video2": 0, "/dev/video10": 0}, "camera_sync_mode",
                                           run_v4l2=kernel)

    assert len(problems) == 2
    assert "/dev/video2" in problems[0] and "failed" in problems[0]
    assert "/dev/video10" in problems[1] and "reads back 2" in problems[1]


def test_restore_returns_no_problems_on_success():
    kernel = _FakeKernel({"/dev/video2": 2, "/dev/video10": 2})

    assert gmsl_sync.restore_sync_mode({"/dev/video2": 0, "/dev/video10": 1}, "camera_sync_mode",
                                       run_v4l2=kernel) == []


def test_apply_sync_mode_restores_unreadable_node_to_driver_default():
    # An as-found value that could not be read must not mean "restore
    # nothing" - the node would stay in external sync after the run.
    kernel = _FakeKernel({"/dev/video2": 1, "/dev/video10": 0})
    failed_reads = []

    def run(node, *args, timeout=15):
        # Only the first -C on /dev/video2 (the as-found read) fails.
        if args == ("-C", "camera_sync_mode") and node == "/dev/video2" and not failed_reads:
            failed_reads.append(node)
            return 1, "", "read failed"
        return kernel(node, *args, timeout=timeout)

    assert gmsl_sync.apply_sync_mode(NODES, "camera_sync_mode", 2, run_v4l2=run) == {
        "/dev/video2": 0, "/dev/video10": 0}


def test_disengage_reports_what_could_not_be_undone():
    order = []
    sync, kernel, tsc = _gmsl_sync(order)
    sync.engage()
    tsc.stop.side_effect = RuntimeError("TSC ioctl failed: x")
    kernel.fail_write_on = {"/dev/video10"}

    problems = sync.disengage()

    assert problems[0].startswith("TSC trigger could not be stopped")
    assert any("/dev/video10" in problem for problem in problems[1:])
    assert sync.disengage() == []  # idempotent


def test_resolve_sync_nodes_any_count_accepts_one_or_three():
    for nodes in (["/dev/video-rs-depth-0"],
                  ["/dev/video-rs-depth-0", "/dev/video-rs-depth-1", "/dev/video-rs-depth-2"]):
        run = _fake_v4l2({node: L_WITH_SYNC for node in nodes})
        found = gmsl_sync.resolve_sync_nodes("camera_sync_mode", run_v4l2=run,
                                             glob_fn=_fake_glob({"/dev/video-rs-*": nodes}), expected=None)
        assert found == nodes


def test_resolve_sync_nodes_any_count_still_raises_on_none():
    with pytest.raises(RuntimeError, match="at least 1"):
        gmsl_sync.resolve_sync_nodes("camera_sync_mode", run_v4l2=_fake_v4l2({}),
                                     glob_fn=_fake_glob({}), expected=None)


def test_reset_leftover_sync_covers_a_single_attached_camera():
    kernel = _FakeKernel({"/dev/video2": 2})

    reset_nodes = gmsl_sync.reset_leftover_sync(
        "camera_sync_mode", run_v4l2=kernel, tsc_io=MagicMock(),
        glob_fn=_fake_glob({"/dev/video-rs-*": ["/dev/video2"]}))

    assert reset_nodes == ["/dev/video2"]
    assert kernel.values == {"/dev/video2": 0}


def test_detect_gmsl_camera():
    detect = gmsl_sync.detect_gmsl_camera
    lookup = {"s1": _device(), "usb": _device(usb=True), "d455": _device(name="Intel RealSense D455")}.__getitem__

    assert detect(REMOTE, "s1", lookup, path_exists=lambda p: True) is True
    assert detect({"mode": "local"}, "s1", lookup, path_exists=lambda p: True) is False
    assert detect(REMOTE, "s1", lookup, path_exists=lambda p: False) is False
    assert detect(REMOTE, "usb", lookup, path_exists=lambda p: True) is False
    assert detect(REMOTE, "d455", lookup, path_exists=lambda p: True) is False
    assert detect(REMOTE, "missing", lookup, path_exists=lambda p: True) is False


def test_verify_passes_while_nodes_stay_in_sync_mode():
    sync, kernel, _ = _gmsl_sync([])
    sync.engage()

    assert sync.verify() == []
    assert sync.nodes == NODES


def test_verify_reports_a_node_knocked_out_of_sync_mode_after_streams_opened():
    sync, kernel, _ = _gmsl_sync([])
    sync.engage()
    kernel.values["/dev/video10"] = 0  # e.g. stream start reset it

    problems = sync.verify()

    assert problems == ["camera_sync_mode on /dev/video10 reads 0 after the streams opened, expected 2"]


def test_verify_reports_unreadable_node_and_never_engaged():
    sync, kernel, _ = _gmsl_sync([])
    assert "never engaged" in " ".join(sync.verify())

    sync.engage()
    kernel.unreadable = {"/dev/video2"}
    assert sync.verify() == ["camera_sync_mode on /dev/video2 reads nothing after the streams opened, expected 2"]
