import os
from unittest.mock import patch

from gui.pages.calibration_page import CalibrationPage


def _minimal_context(output_root, **overrides):
    ctx = dict(
        ctx=None, device_serial="123456",
        pick_a={"stream_type": "infrared", "stream_index": 1, "sensor_index": 0,
                "width": 4, "height": 4, "fps": 30, "format": "y8"},
        pick_b={"stream_type": "color", "stream_index": 0, "sensor_index": 1,
                "width": 4, "height": 4, "fps": 30, "format": "bgr8"},
        camera_controls=[], stream_a_roi=(0, 0, 4, 4), stream_b_roi=(0, 0, 4, 4),
        config_path="config.yaml", camera_name="Intel RealSense D455",
        output_root=output_root,
    )
    ctx.update(overrides)
    return ctx


def test_set_context_mints_a_fresh_folder_under_output_root(qapp, tmp_path):
    page = CalibrationPage()

    page.set_context(**_minimal_context(str(tmp_path)))

    output_dir = page._pending_args["output_dir"]
    assert os.path.isdir(output_dir)
    assert os.path.dirname(output_dir) == str(tmp_path)


def test_two_page_visits_mint_two_different_folders(qapp, tmp_path):
    page = CalibrationPage()

    page.set_context(**_minimal_context(str(tmp_path)))
    first_output_dir = page._pending_args["output_dir"]
    page.set_context(**_minimal_context(str(tmp_path)))
    second_output_dir = page._pending_args["output_dir"]

    assert first_output_dir != second_output_dir


def test_repeated_run_clicks_within_one_visit_share_the_same_folder(qapp, tmp_path):
    page = CalibrationPage()
    page.set_context(**_minimal_context(str(tmp_path)))
    seen_output_dirs = []

    def _fake_run_calibration(**kwargs):
        seen_output_dirs.append(kwargs["output_dir"])

    with patch.object(page, "_run_calibration", side_effect=_fake_run_calibration):
        page._on_run_clicked()
        page._on_run_clicked()

    assert len(seen_output_dirs) == 2
    assert seen_output_dirs[0] == seen_output_dirs[1]
