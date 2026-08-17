from unittest.mock import MagicMock

import numpy as np
import pyrealsense2 as rs
from PySide6.QtCore import QObject, Signal

from gui.pages.multi_camera_live_session_page import MultiCameraLiveSessionPage


class _FakeSessionEngineThread(QObject):
    """Same fake used by tests/engine/test_multi_camera_session.py - a real
    QObject exposing SessionEngineThread's exact signals, never a real
    QThread/camera."""
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

    def start(self):
        self.started = True

    def request_stop(self):
        pass


def _pick(stream_type, stream_index, fmt):
    return {"stream_type": stream_type, "stream_index": stream_index, "width": 4, "height": 4, "fps": 30,
            "format": fmt}


def _camera_config(tmp_path, **overrides):
    config = dict(
        device_serial="SN1", pick_a=_pick(rs.stream.infrared, 1, rs.format.y8),
        pick_b=_pick(rs.stream.color, 0, rs.format.bgr8),
        camera_controls={"emitter_enabled": True, "auto_exposure": True, "exposure_a": None, "exposure_b": None},
        switch_time_ms=1.0, scan_direction=1,
        stream_a_threshold=np.full(2, 150.0), stream_b_threshold=np.full(2, 150.0),
        stream_a_xy=np.array([(1, 1), (2, 2)]), stream_b_xy=np.array([(1, 1), (2, 2)]),
        num_leds=2, neighborhood_size=5, frame_drop_threshold_factor=1.5,
        warmup_pairs_to_skip=0, pairing_gap_outlier_threshold_us=100000,
        position_gap_outlier_threshold_ms=5, position_gap_outlier_max_snapshots=200,
        output_root=str(tmp_path), kept_csv_filename="kept.csv", dropped_csv_filename="dropped.csv",
        snapshot_every_n_pairs=20, max_snapshots=2,
        stream_a_roi=(0, 0, 4, 4), stream_b_roi=(0, 0, 4, 4),
        stream_a_label="Infrared 1", stream_b_label="Color",
        dual_panel_config=None, enable_depth_for_ir_sync=True,
        hardware_reset_before_start=False, hardware_reset_settle_s=8.0,
    )
    config.update(overrides)
    return config


def _two_cameras(tmp_path):
    return [
        {"camera_id": "cam1", "label": "D455 A", "is_master": True,
         "config": _camera_config(tmp_path, device_serial="SN1")},
        {"camera_id": "cam2", "label": "D455 B", "is_master": False,
         "config": _camera_config(tmp_path, device_serial="SN2")},
    ]


def _two_cameras_with_three_cams(tmp_path):
    """Three-camera variant for role/slug/display testing."""
    return [
        {"camera_id": "cam1", "label": "D455 A", "is_master": True,
         "config": _camera_config(tmp_path, device_serial="SN1")},
        {"camera_id": "cam2", "label": "D455 B", "is_master": False,
         "config": _camera_config(tmp_path, device_serial="SN2")},
        {"camera_id": "cam3", "label": "D455 C", "is_master": False,
         "config": _camera_config(tmp_path, device_serial="SN3")},
    ]


def test_camera_roles_tags_master_and_numbers_slaves_in_order():
    from gui.pages.multi_camera_live_session_page import _camera_roles

    cameras = [
        {"camera_id": "cam1", "label": "D455 A", "is_master": True, "config": {"device_serial": "SN1"}},
        {"camera_id": "cam2", "label": "D455 B", "is_master": False, "config": {"device_serial": "SN2"}},
        {"camera_id": "cam3", "label": "D455 C", "is_master": False, "config": {"device_serial": "SN3"}},
    ]

    roles = _camera_roles(cameras)

    assert roles["cam1"] == {"tag": "MASTER", "slug": "master", "display": "D455 A (SN SN1)"}
    assert roles["cam2"] == {"tag": "SLAVE 1", "slug": "slave1", "display": "D455 B (SN SN2)"}
    assert roles["cam3"] == {"tag": "SLAVE 2", "slug": "slave2", "display": "D455 C (SN SN3)"}


def _page_with_fake_threads():
    fake_threads = {}

    def thread_factory(**kwargs):
        thread = _FakeSessionEngineThread(**kwargs)
        fake_threads[kwargs["device_serial"]] = thread
        return thread

    page = MultiCameraLiveSessionPage(
        thread_factory=thread_factory,
        device_lookup=lambda ctx, serial: MagicMock(),
        sync_setter=MagicMock(return_value=True),
        # 0, not the real multi-second-per-extra-camera default - these are
        # fake threads with nothing to actually collide over on a real USB
        # bus, and 7+ tests here construct 2-camera pages.
        camera_start_stagger_s=0,
    )
    return page, fake_threads


def test_set_cameras_builds_one_tab_per_camera(qapp, tmp_path):
    page, _ = _page_with_fake_threads()

    page.set_cameras(object(), _two_cameras(tmp_path))

    # 3, not 2: the Cross-Camera Sync tab (added first) plus one tab per
    # camera - see test_cross_camera_tab_is_first for the same count.
    assert page.tabs.count() == 3
    assert "cam1" in page._panels
    assert "cam2" in page._panels


def test_set_cameras_with_two_cameras_builds_one_cross_series_per_shared_identity(qapp, tmp_path):
    page, _ = _page_with_fake_threads()

    page.set_cameras(object(), _two_cameras(tmp_path))

    # infrared1 and color are shared between both cameras' identical picks.
    assert len(page._cross_pair_series_keys) == 2


def test_set_cameras_with_one_camera_has_no_cross_series(qapp, tmp_path):
    page, _ = _page_with_fake_threads()
    cameras = _two_cameras(tmp_path)[:1]

    page.set_cameras(object(), cameras)

    assert page._cross_pair_series_keys == {}


def test_start_all_sessions_locks_toolbar_and_starts_every_thread(qapp, tmp_path):
    page, fake_threads = _page_with_fake_threads()
    page.set_cameras(object(), _two_cameras(tmp_path))

    page.start_all_sessions()

    assert not page.start_button.isEnabled()
    assert page.stop_button.isEnabled()
    assert not page.duration_spinbox.isEnabled()
    assert fake_threads["SN1"].started
    assert fake_threads["SN2"].started


# --- Genlock: each camera's OWN config-embedded inter_cam_sync_value
# (resolved by gui/main_window.py at Start-time, keyed by camera model/role
# via engine.streams.resolve_inter_cam_sync_value) rides through unchanged
# into that camera's own CameraSessionSpec - this page never re-derives or
# guesses it, only reads what MainWindow already decided. ---

def test_start_all_sessions_carries_each_cameras_own_inter_cam_sync_value_into_its_spec(qapp, tmp_path):
    page, _ = _page_with_fake_threads()
    cameras = _two_cameras(tmp_path)
    cameras[0]["config"]["inter_cam_sync_value"] = 1  # master
    cameras[1]["config"]["inter_cam_sync_value"] = 2  # slave
    page.set_cameras(object(), cameras)

    page.start_all_sessions()

    specs_by_camera_id = {spec.camera_id: spec for spec in page._controller._camera_specs}
    assert specs_by_camera_id["cam1"].inter_cam_sync_value == 1
    assert specs_by_camera_id["cam2"].inter_cam_sync_value == 2


def test_start_all_sessions_defaults_inter_cam_sync_value_to_none_when_config_omits_it(qapp, tmp_path):
    # _camera_config() (this test file's own helper) doesn't set
    # inter_cam_sync_value at all - matches a real camera model with no
    # settings.yaml camera.inter_cam_sync entry (MainWindow leaves it None
    # in that case too - see resolve_inter_cam_sync_value's own docstring).
    page, _ = _page_with_fake_threads()
    page.set_cameras(object(), _two_cameras(tmp_path))

    page.start_all_sessions()

    specs_by_camera_id = {spec.camera_id: spec for spec in page._controller._camera_specs}
    assert specs_by_camera_id["cam1"].inter_cam_sync_value is None
    assert specs_by_camera_id["cam2"].inter_cam_sync_value is None


def test_start_all_sessions_carries_each_cameras_own_num_leds_and_switch_time_ms_into_its_spec(qapp, tmp_path):
    page, _ = _page_with_fake_threads()
    cameras = _two_cameras(tmp_path)
    cameras[0]["config"]["num_leds"] = 20
    cameras[0]["config"]["switch_time_ms"] = 3.0
    page.set_cameras(object(), cameras)

    page.start_all_sessions()

    spec = next(s for s in page._controller._camera_specs if s.camera_id == "cam1")
    assert spec.num_leds == 20
    assert spec.switch_time_ms == 3.0


# --- Output layout: ONE shared run folder, one subfolder per camera, plus
# a combined cross_camera_sync.csv/plot written once the whole run finishes -
# see domain/run_output.py's create_camera_subdir and domain/csv_export.py's
# /domain/plot_export.py's export_cross_camera_* functions. ---

def test_start_all_sessions_creates_one_shared_run_dir_with_per_camera_subfolders(qapp, tmp_path):
    import os
    page, fake_threads = _page_with_fake_threads()
    page.set_cameras(object(), _two_cameras(tmp_path))

    page.start_all_sessions()

    cam1_dir = page._panels["cam1"]._context["output_dir"]
    cam2_dir = page._panels["cam2"]._context["output_dir"]
    assert os.path.dirname(cam1_dir) == os.path.dirname(cam2_dir) == page._run_dir
    assert "cam1" in os.path.basename(cam1_dir)
    assert "cam2" in os.path.basename(cam2_dir)


def test_all_sessions_finished_writes_cross_camera_csv_and_plot(qapp, tmp_path):
    import os
    page, fake_threads = _page_with_fake_threads()
    page.set_cameras(object(), _two_cameras(tmp_path))
    page.start_all_sessions()

    fake_threads["SN1"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_000.0, "stream_b_ts_us": 1_000_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_010.0, "stream_b_ts_us": 1_000_010.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN1"].session_finished.emit([])
    fake_threads["SN1"].finished.emit()
    fake_threads["SN2"].session_finished.emit([])
    fake_threads["SN2"].finished.emit()

    csv_path = os.path.join(page._run_dir, "cross_camera_sync.csv")
    plot_path = os.path.join(page._run_dir, "cross_camera_sync_plot.png")
    assert os.path.exists(csv_path)
    assert os.path.exists(plot_path)
    assert os.path.getsize(csv_path) > 0


def test_all_sessions_finished_skips_cross_camera_export_with_one_camera(qapp, tmp_path):
    import os
    page, fake_threads = _page_with_fake_threads()
    page.set_cameras(object(), _two_cameras(tmp_path)[:1])

    page.start_all_sessions()
    fake_threads["SN1"].session_finished.emit([])
    fake_threads["SN1"].finished.emit()

    assert not os.path.exists(os.path.join(page._run_dir, "cross_camera_sync.csv"))


def test_camera_frame_ready_routes_to_the_right_panel(qapp, tmp_path):
    page, fake_threads = _page_with_fake_threads()
    page.set_cameras(object(), _two_cameras(tmp_path))
    page.start_all_sessions()
    image = np.zeros((4, 4), dtype=np.uint8)

    fake_threads["SN2"].frame_ready.emit("stream_a", image, 0, None)

    assert page._panels["cam2"]._last_stream_a_image is image
    assert page._panels["cam1"]._last_stream_a_image is None


def test_cross_pair_ready_does_not_plot_directly(qapp, tmp_path):
    # Efficiency fix: row_ready-cadence callbacks must stay O(1) (CLAUDE.md's
    # documented row_ready/stats_ready split) - add_point only happens on the
    # throttled stats_ready cadence, in _on_cross_stats_ready.
    page, fake_threads = _page_with_fake_threads()
    page.set_cameras(object(), _two_cameras(tmp_path))
    page.start_all_sessions()
    series_key = page._cross_pair_series_keys[("cam2", "infrared1")]

    fake_threads["SN1"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_000.0, "stream_b_ts_us": 1_000_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_010.0, "stream_b_ts_us": 1_000_010.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })

    assert page.cross_plot.get_series_data(series_key)[1] == []
    # 2, not 1: _camera_config's two cameras share BOTH "infrared1" and
    # "color" identities (see test_set_cameras_with_two_cameras_builds_one_
    # cross_series_per_shared_identity), so one row_ready from each camera
    # legitimately produces one cross_pair_ready per shared identity.
    assert len(page._cross_rows) == 2


def test_matching_rows_plot_a_cross_camera_hw_ts_point_on_stats_ready(qapp, tmp_path):
    page, fake_threads = _page_with_fake_threads()
    page.set_cameras(object(), _two_cameras(tmp_path))
    page.start_all_sessions()

    # First pair is the reconciler's own calibration pair (see
    # engine.cross_camera_reconciler.CrossCameraReconciler's docstring -
    # genlock stabilizes phase/rate between two devices, not their absolute
    # HW-timestamp epoch, so the first-ever match learns that constant
    # offset rather than measuring anything yet) - always reports 0.0.
    fake_threads["SN1"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_000.0, "stream_b_ts_us": 1_000_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_010.0, "stream_b_ts_us": 1_000_010.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    # Second pair, after calibration (offset learned: 10) - reports the
    # genuine residual (-5), not the raw absolute difference.
    fake_threads["SN1"].row_ready.emit({
        "pair_index": 2, "stream_a_ts_us": 1_100_000.0, "stream_b_ts_us": 1_100_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 2, "stream_a_ts_us": 1_100_015.0, "stream_b_ts_us": 1_100_015.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
    })

    # The plot point only appears once the throttled stats_ready cadence
    # fires - mirrors piggybacking on any camera's own stats_ready
    # (engine.multi_camera_session.MultiCameraSessionController._on_stats_ready).
    fake_threads["SN1"].stats_ready.emit({"pair_index": 2})

    series_key = page._cross_pair_series_keys[("cam2", "infrared1")]
    _, ys = page.cross_plot.get_series_data(series_key)
    assert ys == [-5.0]


def test_matching_rows_plot_a_cross_camera_optical_sync_point_on_stats_ready(qapp, tmp_path):
    page, fake_threads = _page_with_fake_threads()
    page.set_cameras(object(), _two_cameras(tmp_path))
    page.start_all_sessions()

    # Calibration pair - HW TS offset learned; LED indices equal (no
    # assertion needed on this one).
    fake_threads["SN1"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_000.0, "stream_b_ts_us": 1_000_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
        "stream_a_last_led": 0, "position_gap_ms_excluded": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 1, "stream_a_ts_us": 1_000_010.0, "stream_b_ts_us": 1_000_010.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
        "stream_a_last_led": 0, "position_gap_ms_excluded": False,
    })
    # Second pair: master detects LED 1, slave detects LED 0. _camera_config's
    # default num_leds=2, switch_time_ms=1.0 ->
    # compute_position_gap(1, 0, 2) == 1, * 1.0 == 1.0ms.
    fake_threads["SN1"].row_ready.emit({
        "pair_index": 2, "stream_a_ts_us": 1_100_000.0, "stream_b_ts_us": 1_100_000.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
        "stream_a_last_led": 1, "position_gap_ms_excluded": False,
    })
    fake_threads["SN2"].row_ready.emit({
        "pair_index": 2, "stream_a_ts_us": 1_100_015.0, "stream_b_ts_us": 1_100_015.0,
        "stream_a_frame_drop": False, "stream_b_frame_drop": False,
        "stream_a_last_led": 0, "position_gap_ms_excluded": False,
    })

    fake_threads["SN1"].stats_ready.emit({"pair_index": 2})

    series_key = page._cross_pair_series_keys[("cam2", "infrared1")]
    _, ys = page.cross_position_plot.get_series_data(series_key)
    assert ys == [1.0]


def test_cross_camera_tab_is_first(qapp, tmp_path):
    page, _ = _page_with_fake_threads()

    page.set_cameras(object(), _two_cameras(tmp_path))

    assert page.tabs.tabText(0) == "Cross-Camera Sync"
    assert page.tabs.count() == 3  # cross-camera tab + 2 per-camera tabs


def test_all_sessions_finished_reenables_start(qapp, tmp_path):
    page, fake_threads = _page_with_fake_threads()
    page.set_cameras(object(), _two_cameras(tmp_path))
    page.start_all_sessions()

    fake_threads["SN1"].session_finished.emit([])
    fake_threads["SN1"].finished.emit()
    fake_threads["SN2"].session_finished.emit([])
    fake_threads["SN2"].finished.emit()

    assert page.start_button.isEnabled()
    assert not page.stop_button.isEnabled()
    assert page.duration_spinbox.isEnabled()


def test_stop_all_sessions_requests_stop_on_the_controller(qapp, tmp_path):
    page, fake_threads = _page_with_fake_threads()
    page.set_cameras(object(), _two_cameras(tmp_path))
    page.start_all_sessions()

    page.stop_all_sessions()  # must not raise even though fake threads don't track stop_requested
