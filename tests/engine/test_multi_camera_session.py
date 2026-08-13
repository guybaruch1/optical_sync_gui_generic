"""MultiCameraSessionController's own sequencing/relaying logic, tested
against a fake thread_factory and mocked hardware calls - NEVER a real
SessionEngineThread/QThread/camera. Actual concurrent-hardware-thread
behavior stays untested by design, same convention as
engine/session_engine.py itself (see CLAUDE.md's "Testing" note) - this
file only proves the controller's own orchestration is correct given
whatever its collaborators do."""

from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QObject, Signal

from engine.multi_camera_session import CameraSessionSpec, MultiCameraSessionController


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
          hardware_reset_before_start=False, device_serial=None, dual_panel_config=None):
    return CameraSessionSpec(
        camera_id=camera_id,
        is_master=is_master,
        inter_cam_sync_value=inter_cam_sync_value,
        stream_identities=stream_identities or {"stream_a": "infrared1"},
        device_serial=device_serial or "{}_serial".format(camera_id),
        hardware_reset_before_start=hardware_reset_before_start,
        hardware_reset_settle_s=0.0,
        thread_kwargs={"dual_panel_config": dual_panel_config} if dual_panel_config is not None else {},
    )


def _controller(camera_specs, sync_setter=None, device_lookup=None):
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


def test_start_all_skips_genlock_entirely_for_a_lone_camera():
    # inter_cam_sync_value=None - e.g. a single-camera run using this
    # controller for consistency, with nothing to genlock against.
    sync_setter = MagicMock(return_value=True)
    controller, _ = _controller([_spec("cam1", True, inter_cam_sync_value=None)], sync_setter=sync_setter)

    controller.start_all(ctx=object())

    sync_setter.assert_not_called()
    assert len(controller.threads) == 1


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

    fake_threads["s1"].row_ready.emit({"pair_index": 1, "stream_a_ts_us": 1_000_000.0, "stream_a_frame_drop": False})
    fake_threads["s2"].row_ready.emit({"pair_index": 2, "stream_a_ts_us": 1_000_010.0, "stream_a_frame_drop": False})

    assert len(cross_rows) == 1
    assert cross_rows[0]["master_camera_id"] == "cam1"
    assert cross_rows[0]["slave_camera_id"] == "cam2"
    assert cross_rows[0]["pairing_gap_us"] == -10.0


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

    fake_threads["s1"].row_ready.emit({"pair_index": 1, "stream_a_ts_us": 1_000_000.0, "stream_a_frame_drop": False})
    fake_threads["s2"].row_ready.emit({"pair_index": 2, "stream_a_ts_us": 1_000_010.0, "stream_a_frame_drop": False})
    fake_threads["s1"].stats_ready.emit({"pair_index": 1})

    assert len(cross_stats) == 1
    assert cross_stats[0][("cam2", "infrared1")]["pairing_gap_us"] == -10.0


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
