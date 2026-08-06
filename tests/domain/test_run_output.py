import datetime
import os

from domain.run_output import create_run_dir


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
