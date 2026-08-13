import datetime
import os

from domain.run_output import create_run_dir, build_live_session_config_suffix, create_camera_subdir


def test_create_run_dir_uses_kind_and_timestamp(tmp_path):
    now = datetime.datetime(2026, 8, 6, 11, 42, 51)

    run_dir = create_run_dir(str(tmp_path), "live_session", now=now)

    assert run_dir == os.path.join(str(tmp_path), "live_session_2026-08-06_11-42-51")
    assert os.path.isdir(run_dir)


def test_create_run_dir_uses_kind_prefix_verbatim(tmp_path):
    now = datetime.datetime(2026, 8, 6, 11, 42, 51)

    run_dir = create_run_dir(str(tmp_path), "calibration", now=now)

    assert os.path.basename(run_dir) == "calibration_2026-08-06_11-42-51"


def test_create_run_dir_avoids_colliding_with_an_existing_folder(tmp_path):
    # Two runs within the same wall-clock second must never share/reuse a
    # folder - that would silently reintroduce the overwrite bug this
    # helper exists to fix.
    now = datetime.datetime(2026, 8, 6, 11, 42, 51)

    first = create_run_dir(str(tmp_path), "live_session", now=now)
    second = create_run_dir(str(tmp_path), "live_session", now=now)
    third = create_run_dir(str(tmp_path), "live_session", now=now)

    assert first != second != third
    assert os.path.basename(second) == "live_session_2026-08-06_11-42-51_2"
    assert os.path.basename(third) == "live_session_2026-08-06_11-42-51_3"
    assert os.path.isdir(first) and os.path.isdir(second) and os.path.isdir(third)


def test_create_run_dir_defaults_now_to_current_time(tmp_path):
    run_dir = create_run_dir(str(tmp_path), "calibration")

    assert os.path.isdir(run_dir)
    assert os.path.basename(run_dir).startswith("calibration_")


def test_create_run_dir_appends_suffix_when_provided(tmp_path):
    now = datetime.datetime(2026, 8, 6, 11, 42, 51)

    run_dir = create_run_dir(str(tmp_path), "live_session", now=now, suffix="1280x720_30fps")

    assert os.path.basename(run_dir) == "live_session_2026-08-06_11-42-51_1280x720_30fps"


def test_create_run_dir_suffix_defaults_to_none_and_changes_nothing(tmp_path):
    # Regression guard: omitting suffix must produce the EXACT same filename
    # shape as before this param existed - calibration_page.py's own
    # create_run_dir(output_root, "calibration") call relies on this.
    now = datetime.datetime(2026, 8, 6, 11, 42, 51)

    run_dir = create_run_dir(str(tmp_path), "calibration", now=now)

    assert os.path.basename(run_dir) == "calibration_2026-08-06_11-42-51"


def test_create_run_dir_collision_numbering_appends_after_suffix(tmp_path):
    now = datetime.datetime(2026, 8, 6, 11, 42, 51)

    first = create_run_dir(str(tmp_path), "live_session", now=now, suffix="1280x720_30fps")
    second = create_run_dir(str(tmp_path), "live_session", now=now, suffix="1280x720_30fps")

    assert os.path.basename(first) == "live_session_2026-08-06_11-42-51_1280x720_30fps"
    assert os.path.basename(second) == "live_session_2026-08-06_11-42-51_1280x720_30fps_2"


def test_build_live_session_config_suffix_manual_exposure_whole_switch_time():
    suffix = build_live_session_config_suffix(
        width=1280, height=720, fps=30, duration_s=200,
        auto_exposure=False, exposure_a=100, exposure_b=100,
        display_stride=10, switch_time_ms=1.0,
    )
    assert suffix == "1280x720_30fps_200s_manualA100B100_interval10_switch1ms"


def test_build_live_session_config_suffix_manual_exposure_with_different_per_stream_values():
    # exposure_a/exposure_b can genuinely differ now (engine.streams.
    # exposure_for_group) - both must show up distinctly in the folder name,
    # not collapse to one shared value.
    suffix = build_live_session_config_suffix(
        width=1280, height=720, fps=30, duration_s=200,
        auto_exposure=False, exposure_a=100, exposure_b=8500,
        display_stride=10, switch_time_ms=1.0,
    )
    assert suffix == "1280x720_30fps_200s_manualA100B8500_interval10_switch1ms"


# --- create_camera_subdir: one per configured camera under a shared
# multi-camera run's own parent folder (create_run_dir), so every camera's
# CSVs/plots/snapshots stay together with the run they belong to instead of
# scattered across independent top-level output/live_session_<timestamp>/
# folders. ---

def test_create_camera_subdir_creates_a_folder_under_the_run_dir(tmp_path):
    run_dir = str(tmp_path / "live_session_2026-08-13_10-00-00")
    os.makedirs(run_dir)

    subdir = create_camera_subdir(run_dir, "camera_1", label="Intel RealSense D455")

    assert os.path.isdir(subdir)
    assert os.path.dirname(subdir) == run_dir
    assert os.path.basename(subdir) == "camera_camera_1_Intel_RealSense_D455"


def test_create_camera_subdir_without_a_label(tmp_path):
    run_dir = str(tmp_path / "live_session_2026-08-13_10-00-00")
    os.makedirs(run_dir)

    subdir = create_camera_subdir(run_dir, "camera_2")

    assert os.path.basename(subdir) == "camera_camera_2"


def test_create_camera_subdir_is_idempotent_for_the_same_camera(tmp_path):
    # Unlike create_run_dir's collision-numbering (meant for genuinely
    # separate runs), re-calling for the SAME camera_id within one already-
    # unique run_dir must return the same folder, not a numbered duplicate.
    run_dir = str(tmp_path / "live_session_2026-08-13_10-00-00")
    os.makedirs(run_dir)

    first = create_camera_subdir(run_dir, "camera_1", label="D455")
    second = create_camera_subdir(run_dir, "camera_1", label="D455")

    assert first == second
    assert os.path.isdir(first)


def test_build_live_session_config_suffix_auto_exposure_and_unlimited_duration():
    suffix = build_live_session_config_suffix(
        width=1280, height=720, fps=30, duration_s=None,
        auto_exposure=True, exposure_a=None, exposure_b=None,
        display_stride=10, switch_time_ms=0.5,
    )
    assert suffix == "1280x720_30fps_unlimited_auto_interval10_switch0.5ms"
