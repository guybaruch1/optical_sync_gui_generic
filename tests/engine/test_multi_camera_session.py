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


def _controller(camera_specs, sync_setter=None, device_lookup=None, camera_start_stagger_s=0):
    # Defaults the stagger to 0 (instant) - tests that don't care about
    # stagger behavior specifically shouldn't pay a real multi-second sleep
    # just because they happen to construct a 2+ camera controller. Tests
    # that DO care about stagger pass a real value explicitly.
    fake_threads = {}

    def thread_factory(**kwargs):
        thread = _FakeSessionEngineThread(**kwargs)
        fake_threads[kwargs["device_serial"]] = thread
        return thread

    controller = MultiCameraSessionController(
        camera_specs=camera_specs,
        thread_factory=thread_factory,
        device_lookup=device_lookup or (lambda ctx, serial: MagicMock(name=serial)),
        sync_setter=sync_setter or MagicMock(return_value=True),
        camera_start_stagger_s=camera_start_stagger_s,
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


def test_start_all_raises_and_starts_nothing_when_two_cameras_want_dual_panel_mode():
    # engine.dual_panel_control's relay/hub singletons (_dual_panel_primed,
    # _relay_connection, _dual_panel_lock) represent exactly ONE shared
    # relay/hub for the whole app - two cameras' threads both calling
    # start_scanning()/stop_scanning() concurrently would corrupt each
    # other's state (confirmed real wiring on the rig this was designed
    # for: all panels across all cameras share one relay). v1 scope is "at
    # most one configured camera may use dual-panel mode per run" - see the
    # multi-camera design doc's "Design detail" section 6.
    panel_config = {"stream_a_panel_port": 1, "stream_b_panel_port": 0, "relay_port": 6}
    controller, fake_threads = _controller([
        _spec("cam1", True, device_serial="s1", dual_panel_config=panel_config),
        _spec("cam2", False, device_serial="s2", dual_panel_config=panel_config),
    ])

    with pytest.raises(RuntimeError):
        controller.start_all(ctx=object())

    assert controller.threads == {}
    assert all(not t.started for t in fake_threads.values())


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
