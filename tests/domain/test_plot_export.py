import math
import os
from domain.plot_export import export_session_plot, _to_plot_value


def _row(pair_index, pairing_gap=None, position_gap=None, exclude_reason=None):
    return {
        "pair_index": pair_index,
        "pairing_gap_us": pairing_gap,
        "position_gap_ms": position_gap,
        "position_gap_ms_exclude_reason": exclude_reason,
    }


def test_export_session_plot_writes_a_file(tmp_path):
    rows = [
        _row(0, pairing_gap=10.0, position_gap=1.0),
        _row(1, pairing_gap=-5.0, position_gap=None, exclude_reason="frame_drop"),
        _row(2, pairing_gap=8.0, position_gap=2.0),
    ]
    path = str(tmp_path / "plot.png")

    export_session_plot(rows, path)

    assert os.path.exists(path)
    assert os.path.getsize(path) > 0


def test_export_session_plot_handles_empty_rows(tmp_path):
    path = str(tmp_path / "plot.png")

    export_session_plot([], path)

    assert os.path.exists(path)


def test_to_plot_value_passes_through_clean_values():
    assert _to_plot_value(12.5, excluded=False) == 12.5


def test_to_plot_value_nans_out_excluded_values():
    # An excluded pair can still carry a wild real number (e.g. a
    # syncer_outlier's pairing gap, or a frame_drop-excluded position gap) -
    # this must become NaN so it breaks the line instead of dragging the
    # whole plot's y-axis to its scale.
    assert math.isnan(_to_plot_value(-237185.0, excluded=True))


def test_to_plot_value_nans_out_none():
    assert math.isnan(_to_plot_value(None, excluded=False))
