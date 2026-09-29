"""MultiCameraSessionController's own sequencing/relaying logic, tested
against a fake thread_factory and mocked hardware calls - NEVER a real
SessionEngineThread/QThread/camera. Actual concurrent-hardware-thread
behavior stays untested by design, same convention as
engine/session_engine.py itself (see CLAUDE.md's "Testing" note) - this
file only proves the controller's own orchestration is correct given
whatever its collaborators do."""

from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QObject, Signal

from engine.multi_camera_session import CameraSessionSpec, MultiCameraSessionController
from engine.streams import INTER_CAM_SYNC_DEFAULT


class _FakeSessionEngineThread(QObject):
    """Exposes exactly the signals/methods engine.session_engine.
    SessionEngineThread exposes (frame_ready/row_ready/stats_ready/
    session_finished/error, plus QThread's own built-in finished) - a real
    QObject so Signal/connect/emit behave exactly like the real thing,
    just never actually running a background thread or touching hardware."""

    frame_ready = Signal(str, object, int, object)
    row_ready = Signal(dict)
    stats_ready = Signal(dict)
    session_finished = Signal(list)
    error = Signal(str)
    finished = Signal()

    def __init__(self, **kwargs):
        super().__init__()
        self.kwargs = kwargs
        self.started = False
        self.stop_requested = False

    def start(self):
        self.started = True

    def request_stop(self):
        self.stop_requested = True


def _spec(camera_id, is_master, inter_cam_sync_value=1, stream_identities=None,
          hardware_reset_before_start=False, device_serial=None, dual_panel_config=None,
          num_leds=10, switch_time_ms=1.0):
    return CameraSessionSpec(
        camera_id=camera_id,
        is_master=is_master,
        inter_cam_sync_value=inter_cam_sync_value,
        stream_identities=stream_identities or {"stream_a": "infrared1"},
        device_serial=device_serial or "{}_serial".format(camera_id),
        num_leds=num_leds,
        switch_time_ms=switch_time_ms,
        hardware_reset_before_start=hardware_reset_before_start,
        hardware_reset_settle_s=0.0,
        thread_kwargs={"dual_panel_config": dual_panel_config} if dual_panel_config is not None else {},
    )


def _controller(camera_specs, sync_setter=None, device_lookup=None, camera_start_stagger_s=0,
                gmsl_sync=None, thread_factory=None, panel_start=None, panel_stop=None):
    # Defaults the stagger to 0 (instant) - tests that don't care about
    # stagger behavior specifically shouldn't pay a real multi-second sleep
    # just because they happen to construct a 2+ camera controller. Tests
    # that DO care about stagger pass a real value explicitly.
    fake_threads = {}

    def default_thread_factory(**kwargs):
        thread = _FakeSessionEngineThread(**kwargs)
        fake_threads[kwargs["device_serial"]] = thread
        return thread

    controller = MultiCameraSessionController(
        camera_specs=camera_specs,
        thread_factory=thread_factory or default_thread_factory,
        device_lookup=device_lookup or (lambda ctx, serial: MagicMock(name=serial)),
        sync_setter=sync_setter or MagicMock(return_value=True),
        camera_start_stagger_s=camera_start_stagger_s,
        gmsl_sync=gmsl_sync,
        panel_start=panel_start or MagicMock(),
        panel_stop=panel_stop or MagicMock(),
    )
    return controller, fake_threads


# --- Startup sequencing: hardware reset -> genlock roles (all-or-nothing,
# master first) -> start every thread. Verified via invariant checks at the
# moment each collaborator is called, not just call counts. ---

def test_start_all_applies_genlock_roles_before_constructing_any_thread():
    sync_setter = MagicMock(return_value=True)
    controller, _ = _controller([_spec("cam1", True), _spec("cam2", False)], sync_setter=sync_setter)

    def assert_no_threads_yet(device, mode):
        assert controller.threads == {}
        return True
    sync_setter.side_effect = assert_no_threads_yet

    controller.start_all(ctx=object())

    assert sync_setter.call_count == 2
    assert len(controller.threads) == 2


def test_start_all_applies_master_role_before_any_slave_role():
    sync_setter = MagicMock(return_value=True)
    controller, _ = _controller(
        [_spec("cam1", False, device_serial="slave_serial"),
         _spec("cam2", True, device_serial="master_serial")],
        sync_setter=sync_setter,
    )

    def device_lookup(ctx, serial):
        device = MagicMock()
        device.serial = serial
        return device

    controller._device_lookup = device_lookup
    controller.start_all(ctx=object())

    called_serials = [call.args[0].serial for call in sync_setter.call_args_list]
    assert called_serials == ["master_serial", "slave_serial"]


def test_start_all_raises_and_starts_nothing_when_a_device_fails_genlock():
    sync_setter = MagicMock(side_effect=[True, False])
    controller, fake_threads = _controller(
        [_spec("cam1", True), _spec("cam2", False)], sync_setter=sync_setter,
    )

    with pytest.raises(RuntimeError):
        controller.start_all(ctx=object())

    assert controller.threads == {}
    assert all(not t.started for t in fake_threads.values())


# --- Reset-to-default: a genlock role already applied to an earlier camera
# must never linger after a LATER camera fails to apply its own - otherwise a
# real device is left stuck as e.g. "slave", which can hang the next time it's
# used standalone (the same "value persists in camera firmware across app
# restarts" risk CLAUDE.md already documents for gain). ---

def test_start_all_resets_any_already_applied_genlock_role_when_a_later_camera_fails():
    sync_setter = MagicMock(side_effect=[True, False])
    master_device = MagicMock()
    master_device.serial = "master_serial"
    slave_device = MagicMock()
    slave_device.serial = "slave_serial"

    def device_lookup(ctx, serial):
        return master_device if serial == "master_serial" else slave_device

    controller, _ = _controller(
        [_spec("cam1", True, device_serial="master_serial"),
         _spec("cam2", False, device_serial="slave_serial")],
        sync_setter=sync_setter, device_lookup=device_lookup,
    )

    with pytest.raises(RuntimeError):
        controller.start_all(ctx=object())

    reset_calls = [call for call in sync_setter.call_args_list if call.args[1] == INTER_CAM_SYNC_DEFAULT]
    assert len(reset_calls) == 1
    assert reset_calls[0].args[0] is master_device  # the one that had actually succeeded


def test_start_all_never_resets_a_camera_whose_genlock_value_was_none_when_a_later_camera_fails():
    sync_setter = MagicMock(return_value=False)
    none_device = MagicMock()
    none_device.serial = "none_serial"
    fails_device = MagicMock()
    fails_device.serial = "fails_serial"

    def device_lookup(ctx, serial):
        return none_device if serial == "none_serial" else fails_device

    controller, _ = _controller(
        [_spec("cam1", True, inter_cam_sync_value=None, device_serial="none_serial"),
         _spec("cam2", False, device_serial="fails_serial")],
        sync_setter=sync_setter, device_lookup=device_lookup,
    )

    with pytest.raises(RuntimeError):
        controller.start_all(ctx=object())

    # Only the non-None spec's device was ever passed to sync_setter at all -
    # the None spec's device never appears, not even for a reset attempt.
    called_devices = {call.args[0] for call in sync_setter.call_args_list}
    assert none_device not in called_devices
    assert fails_device in called_devices


def test_start_all_resets_hardware_before_any_genlock_role_is_applied():
    sync_setter = MagicMock(return_value=True)
    reset_device = MagicMock()

    def device_lookup(ctx, serial):
        return reset_device if serial == "cam1_serial" else MagicMock()

    controller, _ = _controller(
        [_spec("cam1", True, hardware_reset_before_start=True)],
        sync_setter=sync_setter,
    )
    controller._device_lookup = device_lookup

    def assert_reset_already_happened(device, mode):
        reset_device.hardware_reset.assert_called_once()
        return True
    sync_setter.side_effect = assert_reset_already_happened

    controller.start_all(ctx=object())

    reset_device.hardware_reset.assert_called_once()


def test_start_all_never_lets_a_camera_thread_redo_its_own_hardware_reset():
    # The controller already performed any needed reset before role
    # assignment - a thread redoing it internally could race/undo the
    # role the controller just applied.
    controller, fake_threads = _controller(
        [_spec("cam1", True, hardware_reset_before_start=True, device_serial="cam1_serial")],
    )

    controller.start_all(ctx=object())

    assert fake_threads["cam1_serial"].kwargs["hardware_reset_before_start"] is False


PANEL_CONFIG = {"stream_a_panel_port": 1, "stream_b_panel_port": 0, "relay_port": 6}


def _shared_dual_panel_specs(second_config=PANEL_CONFIG):
    master = _spec("cam1", True, device_serial="s1", dual_panel_config=dict(PANEL_CONFIG), switch_time_ms=2.5)
    master.thread_kwargs["scan_direction"] = -1
    slave = _spec("cam2", False, device_serial="s2", dual_panel_config=dict(second_config), switch_time_ms=9.0)
    slave.thread_kwargs["scan_direction"] = 1
    return [master, slave]


def test_start_all_allows_two_cameras_sharing_the_same_dual_panels():
    # Both cameras look at the same two panels (one IR, one color) on one
    # hub + relay - allowed, as long as it is literally the same wiring.
    controller, fake_threads = _controller(_shared_dual_panel_specs())

    controller.start_all(ctx=object())

    assert len(controller.threads) == 2
    assert all(t.started for t in fake_threads.values())


def test_shared_dual_panels_are_armed_once_with_masters_settings_before_any_thread():
    events = []
    panel_start = MagicMock(side_effect=lambda *a: events.append(("arm",) + a))

    def thread_factory(**kwargs):
        events.append(("thread", kwargs["device_serial"]))
        return _FakeSessionEngineThread(**kwargs)

    controller, _ = _controller(_shared_dual_panel_specs(), thread_factory=thread_factory,
                                panel_start=panel_start)
    controller.start_all(ctx=object())

    assert events == [("arm", 2.5, -1, PANEL_CONFIG), ("thread", "s1"), ("thread", "s2")]


def test_shared_dual_panels_are_never_driven_by_any_camera_thread():
    controller, fake_threads = _controller(_shared_dual_panel_specs())

    controller.start_all(ctx=object())

    assert all(t.kwargs["drive_panel"] is False for t in fake_threads.values())


def test_shared_dual_panels_stop_once_only_after_every_thread_finished():
    panel_stop = MagicMock()
    controller, fake_threads = _controller(_shared_dual_panel_specs(), panel_stop=panel_stop)
    controller.start_all(ctx=object())

    controller.stop_all()
    fake_threads["s1"].finished.emit()
    panel_stop.assert_not_called()  # cam2 may still be capturing
    fake_threads["s2"].finished.emit()

    panel_stop.assert_called_once_with(PANEL_CONFIG)


def test_start_all_still_rejects_two_cameras_with_different_dual_panel_wiring():
    other = {"stream_a_panel_port": 3, "stream_b_panel_port": 2, "relay_port": 6}
    panel_start = MagicMock()
    controller, fake_threads = _controller(_shared_dual_panel_specs(second_config=other),
                                           panel_start=panel_start)

    with pytest.raises(RuntimeError, match="same"):
        controller.start_all(ctx=object())

    assert controller.threads == {}
    panel_start.assert_not_called()


def test_shared_panel_arm_failure_starts_no_thread_and_undoes_gmsl():
    gmsl = MagicMock()
    panel_start = MagicMock(side_effect=RuntimeError("hub switch failed"))
    controller, fake_threads = _controller(_shared_dual_panel_specs(), gmsl_sync=gmsl,
                                           panel_start=panel_start)

    with pytest.raises(RuntimeError, match="hub switch"):
        controller.start_all(ctx=object())

    assert fake_threads == {}
    gmsl.disengage.assert_called_once()


def test_one_dual_panel_camera_still_drives_its_own_panels():
    panel_start = MagicMock()
    controller, fake_threads = _controller([
        _spec("cam1", True, device_serial="s1", dual_panel_config=dict(PANEL_CONFIG)),
        _spec("cam2", False, device_serial="s2"),
    ], panel_start=panel_start)

    controller.start_all(ctx=object())

    panel_start.assert_not_called()
    assert all(t.kwargs["drive_panel"] is True for t in fake_threads.values())


def test_start_all_allows_exactly_one_camera_in_dual_panel_mode():
    panel_config = {"stream_a_panel_port": 1, "stream_b_panel_port": 0, "relay_port": 6}
    controller, fake_threads = _controller([
        _spec("cam1", True, device_serial="s1", dual_panel_config=panel_config),
        _spec("cam2", False, device_serial="s2"),  # single-panel/no panel
    ])

    controller.start_all(ctx=object())

    assert len(controller.threads) == 2
    assert all(t.started for t in fake_threads.values())


def test_start_all_allows_zero_cameras_in_dual_panel_mode():
    controller, fake_threads = _controller([
        _spec("cam1", True, device_serial="s1"), _spec("cam2", False, device_serial="s2"),
    ])

    controller.start_all(ctx=object())

    assert len(controller.threads) == 2


# --- Real bug: two cameras sharing a USB hub/controller (e.g. an Acroname
# hub) - starting both threads back-to-back with zero delay let camera 1's
# rs.pipeline().start() (already documented elsewhere in this codebase as
# having unpredictable USB-level side effects) collide with camera 2's own
# device-opening sequence, producing a real-hardware "resolve_and_group: no
# matching profile found... after a reconnect" failure on the second
# camera. A settle delay between each camera's thread start - same "give
# the hardware a moment" pattern as hardware_reset_settle_s/
# hub_switch_settle_s elsewhere in this project - reduces that collision
# window. ---

def test_start_all_staggers_camera_thread_starts():
    controller, fake_threads = _controller(
        [_spec("cam1", True, device_serial="s1"), _spec("cam2", False, device_serial="s2")],
        camera_start_stagger_s=2.0,
    )

    with patch("engine.multi_camera_session.time.sleep") as mock_sleep:
        controller.start_all(ctx=object())

    mock_sleep.assert_called_once_with(2.0)
    assert fake_threads["s1"].started
    assert fake_threads["s2"].started


def test_start_all_does_not_sleep_before_starting_the_first_camera():
    controller, fake_threads = _controller([_spec("cam1", True, device_serial="s1")], camera_start_stagger_s=2.0)

    with patch("engine.multi_camera_session.time.sleep") as mock_sleep:
        controller.start_all(ctx=object())

    mock_sleep.assert_not_called()


def test_start_all_stagger_defaults_to_a_positive_settle_time():
    # Real-hardware-tunable default, same humility this project already
    # applies to every other guessed hardware timing constant - not claimed
    # to be exactly right, just a reasonable starting point. Bypasses
    # _controller()'s own test-convenience default (0, for every OTHER
    # test's speed) to check MultiCameraSessionController's real default.
    fake_threads = {}

    def thread_factory(**kwargs):
        thread = _FakeSessionEngineThread(**kwargs)
        fake_threads[kwargs["device_serial"]] = thread
        return thread

    controller = MultiCameraSessionController(
        camera_specs=[_spec("cam1", True, device_serial="s1"), _spec("cam2", False, device_serial="s2")],
        thread_factory=thread_factory,
        device_lookup=lambda ctx, serial: MagicMock(name=serial),
        sync_setter=MagicMock(return_value=True),
    )

    with patch("engine.multi_camera_session.time.sleep") as mock_sleep:
        controller.start_all(ctx=object())

    assert mock_sleep.call_count == 1
    assert mock_sleep.call_args[0][0] > 0


def test_start_all_stagger_applies_before_every_camera_after_the_first():
    controller, fake_threads = _controller(
        [_spec("cam1", True, device_serial="s1"), _spec("cam2", False, device_serial="s2"),
         _spec("cam3", False, device_serial="s3")],
        camera_start_stagger_s=1.5,
    )

    with patch("engine.multi_camera_session.time.sleep") as mock_sleep:
        controller.start_all(ctx=object())

    assert mock_sleep.call_count == 2  # before camera 2, and before camera 3
    assert all(call.args == (1.5,) for call in mock_sleep.call_args_list)


def test_start_all_skips_genlock_entirely_for_a_lone_camera():
    # inter_cam_sync_value=None - e.g. a single-camera run using this
    # controller for consistency, with nothing to genlock against.
    sync_setter = MagicMock(return_value=True)
    controller, _ = _controller([_spec("cam1", True, inter_cam_sync_value=None)], sync_setter=sync_setter)

    controller.start_all(ctx=object())

    sync_setter.assert_not_called()
    assert len(controller.threads) == 1


def test_controller_builds_cross_camera_pair_specs_using_real_camera_session_spec_num_leds(qapp):
    specs = [
        _spec("cam1", True, num_leds=20, switch_time_ms=2.5),
        _spec("cam2", False, num_leds=999, switch_time_ms=999.0),
    ]

    controller, _ = _controller(specs)

    assert controller._reconciler is not None
    pair_spec = controller._reconciler._pair_specs[0]
    assert pair_spec.num_leds == 20
    assert pair_spec.switch_time_ms == 2.5


# --- Signal relaying: per-camera signals pass through tagged with
# camera_id; row_ready additionally feeds the cross-camera reconciler. ---

def test_camera_row_ready_passes_through_tagged_with_camera_id():
    controller, fake_threads = _controller([_spec("cam1", True, device_serial="s1")])
    controller.start_all(ctx=object())
    received = []
    controller.camera_row_ready.connect(lambda camera_id, row: received.append((camera_id, row)))

    fake_threads["s1"].row_ready.emit({"pair_index": 1, "stream_a_ts_us": 100.0, "stream_a_frame_drop": False})

    assert received == [("cam1", {"pair_index": 1, "stream_a_ts_us": 100.0, "stream_a_frame_drop": False})]


def test_matching_rows_from_master_and_slave_emit_cross_pair_ready():
    controller, fake_threads = _controller([
        _spec("cam1", True, device_serial="s1", stream_identities={"stream_a": "infrared1"}),
        _spec("cam2", False, device_serial="s2", stream_identities={"stream_a": "infrared1"}),
    ])
    controller.start_all(ctx=object())
    cross_rows = []
    controller.cross_pair_ready.connect(cross_rows.append)

    # First pair is the reconciler's own HW-ts calibration pair (see
    # engine.cross_camera_reconciler.CrossCameraReconciler's docstring) -
    # always reports 0.0. Second pair, after calibration (offset learned:
    # 10), reports the genuine residual (-5). global_ts_us mirrors ts_us
    # here (see engine.cross_camera_reconciler's own tests) so matching -
    # now driven by global ts - still succeeds for these hand-built rows.
    fake_threads["s1"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_000.0, "stream_a_global_ts_us": 1_000_000.0,
        "stream_a_frame_drop": False,
    })
    fake_threads["s2"].row_ready.emit({
        "pair_index": 2, "stream_a_ts_us": 1_000_010.0, "stream_a_global_ts_us": 1_000_010.0,
        "stream_a_frame_drop": False,
    })
    fake_threads["s1"].row_ready.emit({
        "pair_index": 3, "stream_a_ts_us": 1_100_000.0, "stream_a_global_ts_us": 1_100_000.0,
        "stream_a_frame_drop": False,
    })
    fake_threads["s2"].row_ready.emit({
        "pair_index": 4, "stream_a_ts_us": 1_100_015.0, "stream_a_global_ts_us": 1_100_015.0,
        "stream_a_frame_drop": False,
    })

    assert len(cross_rows) == 2
    assert cross_rows[0]["master_camera_id"] == "cam1"
    assert cross_rows[0]["slave_camera_id"] == "cam2"
    assert cross_rows[0]["pairing_gap_us"] == 0.0
    assert cross_rows[1]["pairing_gap_us"] == -5.0


def test_single_camera_run_never_emits_cross_camera_signals():
    controller, fake_threads = _controller([_spec("cam1", True, device_serial="s1")])
    controller.start_all(ctx=object())
    cross_rows = []
    controller.cross_pair_ready.connect(cross_rows.append)

    fake_threads["s1"].row_ready.emit({"pair_index": 1, "stream_a_ts_us": 1_000_000.0, "stream_a_frame_drop": False})

    assert cross_rows == []


def test_cross_stats_ready_emits_latest_cross_rows_on_any_camera_stats_tick():
    controller, fake_threads = _controller([
        _spec("cam1", True, device_serial="s1", stream_identities={"stream_a": "infrared1"}),
        _spec("cam2", False, device_serial="s2", stream_identities={"stream_a": "infrared1"}),
    ])
    controller.start_all(ctx=object())
    cross_stats = []
    controller.cross_stats_ready.connect(cross_stats.append)

    # First pair calibrates (reports 0.0); second pair reports the genuine
    # HW-ts residual (-5) once calibrated - see engine.cross_camera_reconciler.
    # CrossCameraReconciler's docstring. global_ts_us mirrors ts_us here so
    # matching - now driven by global ts - still succeeds for these
    # hand-built rows.
    fake_threads["s1"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_000.0, "stream_a_global_ts_us": 1_000_000.0,
        "stream_a_frame_drop": False,
    })
    fake_threads["s2"].row_ready.emit({
        "pair_index": 2, "stream_a_ts_us": 1_000_010.0, "stream_a_global_ts_us": 1_000_010.0,
        "stream_a_frame_drop": False,
    })
    fake_threads["s1"].row_ready.emit({
        "pair_index": 3, "stream_a_ts_us": 1_100_000.0, "stream_a_global_ts_us": 1_100_000.0,
        "stream_a_frame_drop": False,
    })
    fake_threads["s2"].row_ready.emit({
        "pair_index": 4, "stream_a_ts_us": 1_100_015.0, "stream_a_global_ts_us": 1_100_015.0,
        "stream_a_frame_drop": False,
    })
    fake_threads["s1"].stats_ready.emit({"pair_index": 1})

    assert len(cross_stats) == 1
    assert cross_stats[0][("cam2", "infrared1")]["pairing_gap_us"] == -5.0


def test_cross_stats_ready_never_fires_before_any_cross_row_exists():
    controller, fake_threads = _controller([_spec("cam1", True, device_serial="s1")])
    controller.start_all(ctx=object())
    cross_stats = []
    controller.cross_stats_ready.connect(cross_stats.append)

    fake_threads["s1"].stats_ready.emit({"pair_index": 1})

    assert cross_stats == []


# --- Lifecycle: stop_all requests every thread stop; all_sessions_finished
# only fires once every started thread's own finished signal has fired -
# mirrors LiveSessionPage's existing "finished, not session_finished/error,
# re-enables Start" reasoning, generalized to N threads. ---

def test_stop_all_requests_stop_on_every_camera_thread():
    controller, fake_threads = _controller([
        _spec("cam1", True, device_serial="s1"), _spec("cam2", False, device_serial="s2"),
    ])
    controller.start_all(ctx=object())

    controller.stop_all()

    assert all(t.stop_requested for t in fake_threads.values())


def test_all_sessions_finished_waits_for_every_thread():
    controller, fake_threads = _controller([
        _spec("cam1", True, device_serial="s1"), _spec("cam2", False, device_serial="s2"),
    ])
    controller.start_all(ctx=object())
    finished_payloads = []
    controller.all_sessions_finished.connect(finished_payloads.append)

    fake_threads["s1"].session_finished.emit([{"pair_index": 1}])
    fake_threads["s1"].finished.emit()
    assert finished_payloads == []  # cam2 hasn't finished yet

    fake_threads["s2"].session_finished.emit([{"pair_index": 2}])
    fake_threads["s2"].finished.emit()
    assert finished_payloads == [{"cam1": [{"pair_index": 1}], "cam2": [{"pair_index": 2}]}]


# --- Reset-to-default on finish: once every started camera thread has
# genuinely finished (not merely stop_all() being called - request_stop() is
# non-blocking, so a camera's rs.pipeline() may still be mid-teardown on its
# own thread), every genlock role this run actually applied gets reset back
# to INTER_CAM_SYNC_DEFAULT - so a camera never sits stuck as "slave" the next
# time it's used standalone. ---

def test_genlock_roles_are_reset_to_default_once_every_camera_thread_has_finished():
    sync_setter = MagicMock(return_value=True)
    device_a, device_b = MagicMock(), MagicMock()

    def device_lookup(ctx, serial):
        return device_a if serial == "s1" else device_b

    controller, fake_threads = _controller(
        [_spec("cam1", True, device_serial="s1"), _spec("cam2", False, device_serial="s2")],
        sync_setter=sync_setter, device_lookup=device_lookup,
    )
    controller.start_all(ctx=object())
    sync_setter.reset_mock()  # only care about post-finish calls from here on

    fake_threads["s1"].finished.emit()
    assert sync_setter.call_count == 0  # cam2 hasn't finished yet - too early to reset

    fake_threads["s2"].finished.emit()

    reset_calls = [call for call in sync_setter.call_args_list if call.args[1] == INTER_CAM_SYNC_DEFAULT]
    reset_devices = {call.args[0] for call in reset_calls}
    assert reset_devices == {device_a, device_b}


def test_genlock_reset_never_touches_a_camera_whose_genlock_value_was_none_on_finish():
    sync_setter = MagicMock(return_value=True)
    controller, fake_threads = _controller(
        [_spec("cam1", True, inter_cam_sync_value=None, device_serial="s1")], sync_setter=sync_setter,
    )
    controller.start_all(ctx=object())
    sync_setter.assert_not_called()  # existing behavior: nothing applied at start

    fake_threads["s1"].finished.emit()

    sync_setter.assert_not_called()  # new behavior: nothing to reset either


def test_all_sessions_finished_still_emits_even_if_resetting_a_genlock_role_raises():
    sync_setter = MagicMock(side_effect=[True, True, RuntimeError("device unplugged"), True])
    controller, fake_threads = _controller(
        [_spec("cam1", True, device_serial="s1"), _spec("cam2", False, device_serial="s2")],
        sync_setter=sync_setter,
    )
    controller.start_all(ctx=object())
    finished_payloads = []
    controller.all_sessions_finished.connect(finished_payloads.append)

    fake_threads["s1"].finished.emit()
    fake_threads["s2"].finished.emit()

    # 2 role-assignment calls at start + 2 reset calls at finish, even though
    # the first reset call raised - one bad device can't block resetting the
    # rest, or suppress all_sessions_finished firing.
    assert sync_setter.call_count == 4
    assert len(finished_payloads) == 1


# --- GMSL TSC sync: engaged after genlock, before any thread; disengaged
# only after every thread finished. ---

def _gmsl_specs():
    return [_spec("cam1", True, inter_cam_sync_value=None, device_serial="s1"),
            _spec("cam2", False, inter_cam_sync_value=None, device_serial="s2")]


def test_start_all_engages_gmsl_before_any_thread_starts():
    events = []
    gmsl = MagicMock()
    gmsl.engage.side_effect = lambda: events.append("engage")

    def thread_factory(**kwargs):
        events.append("thread")
        return _FakeSessionEngineThread(**kwargs)

    controller, _ = _controller(_gmsl_specs(), gmsl_sync=gmsl, thread_factory=thread_factory)
    controller.start_all(ctx=object())

    assert events == ["engage", "thread", "thread"]


def test_start_all_starts_no_thread_when_gmsl_engage_fails():
    gmsl = MagicMock()
    gmsl.engage.side_effect = RuntimeError("readback mismatch")
    controller, fake_threads = _controller(_gmsl_specs(), gmsl_sync=gmsl)

    with pytest.raises(RuntimeError, match="readback"):
        controller.start_all(ctx=object())
    assert fake_threads == {}


def test_start_all_disengages_gmsl_if_thread_start_raises():
    gmsl = MagicMock()

    def thread_factory(**kwargs):
        raise RuntimeError("thread construction failed")

    controller, _ = _controller(_gmsl_specs(), gmsl_sync=gmsl, thread_factory=thread_factory)

    with pytest.raises(RuntimeError, match="thread construction"):
        controller.start_all(ctx=object())
    gmsl.disengage.assert_called_once()


def test_gmsl_disengaged_only_after_every_thread_finished():
    gmsl = MagicMock()
    controller, fake_threads = _controller(_gmsl_specs(), gmsl_sync=gmsl)
    controller.start_all(ctx=object())

    fake_threads["s1"].finished.emit()
    gmsl.disengage.assert_not_called()
    fake_threads["s2"].finished.emit()
    gmsl.disengage.assert_called_once()


def test_gmsl_restore_failure_is_reported_and_run_still_finishes():
    gmsl = MagicMock()
    gmsl.disengage.return_value = ["/dev/video2: camera_sync_mode reads back 2, not 0"]
    controller, fake_threads = _controller(_gmsl_specs(), gmsl_sync=gmsl)
    errors, finished = [], []
    controller.camera_error.connect(lambda cid, message: errors.append((cid, message)))
    controller.all_sessions_finished.connect(finished.append)
    controller.start_all(ctx=object())

    fake_threads["s1"].finished.emit()
    fake_threads["s2"].finished.emit()

    assert errors == [("GMSL sync", "Could not fully undo GMSL sync: "
                                    "/dev/video2: camera_sync_mode reads back 2, not 0")]
    assert len(finished) == 1


def test_stop_all_never_disengages_gmsl_by_itself():
    gmsl = MagicMock()
    controller, _ = _controller(_gmsl_specs(), gmsl_sync=gmsl)
    controller.start_all(ctx=object())

    controller.stop_all()

    gmsl.disengage.assert_not_called()


def test_partial_thread_start_failure_defers_gmsl_disengage_until_started_threads_finish():
    # Final-review I1: disengaging while camera 1 is still streaming would
    # stop its trigger and rewrite its V4L2 mode under a live pipeline.
    gmsl = MagicMock()
    started = {}

    def thread_factory(**kwargs):
        if kwargs["device_serial"] == "s2":
            raise RuntimeError("second camera failed")
        thread = _FakeSessionEngineThread(**kwargs)
        started[kwargs["device_serial"]] = thread
        return thread

    controller, _ = _controller(_gmsl_specs(), gmsl_sync=gmsl, thread_factory=thread_factory)

    with pytest.raises(RuntimeError, match="second camera"):
        controller.start_all(ctx=object())

    assert started["s1"].stop_requested is True
    gmsl.disengage.assert_not_called()
    started["s1"].finished.emit()
    gmsl.disengage.assert_called_once()


def test_shared_panel_stop_failure_is_reported_and_never_blocks_finishing():
    panel_stop = MagicMock(side_effect=RuntimeError("relay COM port gone"))
    controller, fake_threads = _controller(_shared_dual_panel_specs(), panel_stop=panel_stop)
    errors, finished = [], []
    controller.camera_error.connect(lambda cid, msg: errors.append((cid, msg)))
    controller.all_sessions_finished.connect(finished.append)
    controller.start_all(ctx=object())

    fake_threads["s1"].finished.emit()
    fake_threads["s2"].finished.emit()

    assert len(finished) == 1
    assert errors == [("LED panels", "Failed to stop the shared LED panels: relay COM port gone")]


# --- Post-Start GMSL sync check: once every camera streams, re-read the
# V4L2 mode; no frames / early stop / wrong mode -> gmsl_sync_failed + stop. ---

def _verifying_controller(verify_result=None):
    gmsl = MagicMock()
    gmsl.verify.return_value = verify_result or []
    gmsl.nodes = ["/dev/video2", "/dev/video10"]
    controller, fake_threads = _controller(_gmsl_specs(), gmsl_sync=gmsl)
    verified, failed = [], []
    controller.gmsl_sync_verified.connect(verified.append)
    controller.gmsl_sync_failed.connect(failed.append)
    controller.start_all(ctx=object())
    return controller, fake_threads, gmsl, verified, failed


def test_gmsl_sync_verified_only_once_every_camera_streams():
    controller, fake_threads, gmsl, verified, failed = _verifying_controller()

    fake_threads["s1"].row_ready.emit({"pair_index": 0})
    gmsl.verify.assert_not_called()
    fake_threads["s2"].row_ready.emit({"pair_index": 0})
    fake_threads["s2"].row_ready.emit({"pair_index": 1})

    gmsl.verify.assert_called_once()
    assert verified == [["/dev/video2", "/dev/video10"]]
    assert failed == []
    assert not any(thread.stop_requested for thread in fake_threads.values())


def test_gmsl_sync_mode_wrong_after_streams_open_fails_and_stops_run():
    controller, fake_threads, gmsl, verified, failed = _verifying_controller(
        verify_result=["camera_sync_mode on /dev/video2 reads 0 after the streams opened, expected 2"])

    fake_threads["s1"].row_ready.emit({"pair_index": 0})
    fake_threads["s2"].row_ready.emit({"pair_index": 0})

    assert verified == []
    assert len(failed) == 1 and "/dev/video2 reads 0" in failed[0]
    assert all(thread.stop_requested for thread in fake_threads.values())


def test_gmsl_camera_stopping_before_first_frame_fails_with_its_error():
    controller, fake_threads, gmsl, verified, failed = _verifying_controller()
    fake_threads["s1"].row_ready.emit({"pair_index": 0})

    fake_threads["s2"].error.emit("Frame didn't arrive within 5000")
    fake_threads["s2"].finished.emit()

    assert len(failed) == 1
    assert "cam2" in failed[0] and "Frame didn't arrive within 5000" in failed[0]
    assert fake_threads["s1"].stop_requested
    gmsl.verify.assert_not_called()


def test_gmsl_no_frames_within_timeout_fails():
    controller, fake_threads, gmsl, verified, failed = _verifying_controller()
    fake_threads["s1"].row_ready.emit({"pair_index": 0})

    controller._on_gmsl_verify_timeout()

    assert len(failed) == 1 and "cam2" in failed[0] and "cam1" not in failed[0]
    assert all(thread.stop_requested for thread in fake_threads.values())


def test_operator_stop_before_streaming_is_not_a_sync_failure():
    controller, fake_threads, gmsl, verified, failed = _verifying_controller()

    controller.stop_all()
    controller._on_gmsl_verify_timeout()
    for thread in fake_threads.values():
        thread.finished.emit()

    assert failed == [] and verified == []


def test_free_run_guard_run_has_no_sync_check():
    class _Guard:
        def engage(self):
            pass

        def disengage(self):
            return []

    controller, fake_threads = _controller(_gmsl_specs(), gmsl_sync=_Guard())
    failed = []
    controller.gmsl_sync_failed.connect(failed.append)
    controller.start_all(ctx=object())

    for thread in fake_threads.values():
        thread.finished.emit()

    assert failed == []


def test_gmsl_run_hands_the_global_ts_warmup_to_the_reconciler():
    class _Sync:
        global_ts_skip_s = 10.0

        def engage(self):
            pass

        def disengage(self):
            return []

    controller, _ = _controller(_gmsl_specs(), gmsl_sync=_Sync())
    assert controller._reconciler._global_ts_warmup_us == 10_000_000

    controller, _ = _controller(_gmsl_specs(), gmsl_sync=MagicMock())
    assert controller._reconciler._global_ts_warmup_us == 0

    controller, _ = _controller(_gmsl_specs())
    assert controller._reconciler._global_ts_warmup_us == 0


# --- GMSL TSC sync runs: open cameras back to back (each right after the
# previous camera's pipeline.start() returned), like the reference script. ---

class _FakeThreadWithCaptureStarted(_FakeSessionEngineThread):
    capture_started = Signal()


class _BackToBackSync:
    start_cameras_back_to_back = True
    global_ts_skip_s = 0.0

    def __init__(self):
        self.verified = 0

    def engage(self):
        pass

    def verify(self):
        self.verified += 1
        return []

    def disengage(self):
        return []


def _back_to_back_controller(specs=None, camera_start_stagger_s=0):
    created = []

    def factory(**kwargs):
        thread = _FakeThreadWithCaptureStarted(**kwargs)
        created.append(thread)
        return thread

    controller, _ = _controller(specs or _gmsl_specs(), gmsl_sync=_BackToBackSync(), thread_factory=factory,
                                camera_start_stagger_s=camera_start_stagger_s)
    return controller, created


def test_back_to_back_opens_the_next_camera_only_once_the_previous_stream_is_open():
    specs = _gmsl_specs() + [_spec("cam3", False, inter_cam_sync_value=None, device_serial="s3")]
    controller, created = _back_to_back_controller(specs)

    controller.start_all(ctx=object())
    assert [t.kwargs["device_serial"] for t in created] == ["s1"]

    created[0].capture_started.emit()
    assert [t.kwargs["device_serial"] for t in created] == ["s1", "s2"]

    created[1].capture_started.emit()
    assert [t.kwargs["device_serial"] for t in created] == ["s1", "s2", "s3"]
    assert all(t.started for t in created)


def test_back_to_back_never_waits_the_usb_stagger():
    controller, created = _back_to_back_controller(camera_start_stagger_s=100)

    with patch("engine.multi_camera_session.time.sleep") as sleep:
        controller.start_all(ctx=object())
        created[0].capture_started.emit()

    sleep.assert_not_called()
    assert len(created) == 2


def test_back_to_back_camera_that_ends_before_its_stream_opens_stops_the_chain():
    controller, created = _back_to_back_controller()
    finished, failed = [], []
    controller.all_sessions_finished.connect(finished.append)
    controller.gmsl_sync_failed.connect(failed.append)
    controller.start_all(ctx=object())

    created[0].error.emit("pipeline.start() failed")
    created[0].finished.emit()

    assert len(created) == 1  # the second camera is never opened
    assert len(finished) == 1
    assert len(failed) == 1 and "pipeline.start() failed" in failed[0]


def test_back_to_back_stop_before_the_next_camera_opens_never_opens_it():
    controller, created = _back_to_back_controller()
    controller.start_all(ctx=object())

    controller.stop_all()
    created[0].capture_started.emit()

    assert len(created) == 1
    assert created[0].stop_requested


def test_back_to_back_sync_check_waits_for_every_camera_not_just_the_first():
    controller, created = _back_to_back_controller()
    sync = controller._gmsl_sync
    controller.start_all(ctx=object())

    created[0].row_ready.emit({"pair_index": 0})
    assert sync.verified == 0  # camera 2 isn't even open yet

    created[0].capture_started.emit()
    created[1].row_ready.emit({"pair_index": 0})
    assert sync.verified == 1


def test_back_to_back_falls_back_to_starting_all_when_threads_cannot_signal():
    created = []

    def factory(**kwargs):
        thread = _FakeSessionEngineThread(**kwargs)  # no capture_started signal
        created.append(thread)
        return thread

    controller, _ = _controller(_gmsl_specs(), gmsl_sync=_BackToBackSync(), thread_factory=factory)
    controller.start_all(ctx=object())

    assert len(created) == 2


def test_without_back_to_back_every_thread_starts_at_once_as_before():
    created = []

    def factory(**kwargs):
        thread = _FakeThreadWithCaptureStarted(**kwargs)
        created.append(thread)
        return thread

    controller, _ = _controller(_gmsl_specs(), gmsl_sync=MagicMock(), thread_factory=factory)
    controller.start_all(ctx=object())

    assert len(created) == 2


# --- Cross-camera matching window: half a frame when every stream shares one
# fps, so an offset pair is never matched one frame off. ---

def _spec_with_fps(camera_id, is_master, fps_a, fps_b=None):
    spec = _spec(camera_id, is_master, inter_cam_sync_value=None, device_serial=camera_id + "_serial")
    spec.thread_kwargs["pick_a"] = {"fps": fps_a}
    spec.thread_kwargs["pick_b"] = {"fps": fps_b} if fps_b else None
    return spec


def test_match_window_is_half_a_frame_when_every_stream_shares_one_fps():
    controller, _ = _controller([_spec_with_fps("cam1", True, 30, 30), _spec_with_fps("cam2", False, 30)])
    assert controller._reconciler._max_match_gap_us == pytest.approx(1e6 / 60)

    controller, _ = _controller([_spec_with_fps("cam1", True, 15, 15), _spec_with_fps("cam2", False, 15, 15)])
    assert controller._reconciler._max_match_gap_us == pytest.approx(1e6 / 30)


def test_match_window_keeps_50_ms_for_mixed_fps():
    controller, _ = _controller([_spec_with_fps("cam1", True, 30, 15), _spec_with_fps("cam2", False, 30)])
    assert controller._reconciler._max_match_gap_us == 50_000
