import math
import os

import matplotlib
matplotlib.use("Agg")

from domain.plot_export import (
    export_session_plot, _build_figure, _to_plot_value,
    _figure_width, _MIN_FIGURE_WIDTH, _MAX_FIGURE_WIDTH,
)


def _row(pair_index, pairing_gap=None, position_gap=None, pairing_gap_excluded=False,
         position_gap_excluded=False, stream_a_frame_drop=False, stream_b_frame_drop=False):
    return {
        "pair_index": pair_index,
        "pairing_gap_us": pairing_gap,
        "pairing_gap_us_excluded": pairing_gap_excluded,
        "position_gap_ms": position_gap,
        "position_gap_ms_excluded": position_gap_excluded,
        "stream_a_frame_drop": stream_a_frame_drop,
        "stream_b_frame_drop": stream_b_frame_drop,
    }


def test_export_session_plot_writes_a_file(tmp_path):
    rows = [
        _row(0, pairing_gap=10.0, position_gap=1.0),
        _row(1, pairing_gap=-5.0, position_gap=None, pairing_gap_excluded=True,
             position_gap_excluded=True, stream_a_frame_drop=True),
        _row(2, pairing_gap=8.0, position_gap=2.0, stream_b_frame_drop=True),
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


def test_figure_width_uses_minimum_for_short_runs():
    assert _figure_width(10) == _MIN_FIGURE_WIDTH


def test_figure_width_grows_with_row_count():
    assert _figure_width(3000) > _figure_width(300)


def test_figure_width_caps_for_very_long_runs():
    assert _figure_width(1_000_000) == _MAX_FIGURE_WIDTH


def test_frame_drop_axis_splits_by_stream_as_mirrored_spikes():
    # Stream A drops alone should plot as +1, stream B alone as -1 - not
    # collapsed into one combined 0/1 "did anything drop" line, and not
    # occluding each other when both happen on the same pair.
    rows = [
        _row(0),
        _row(1, stream_a_frame_drop=True),
        _row(2, stream_b_frame_drop=True),
        _row(3, stream_a_frame_drop=True, stream_b_frame_drop=True),
    ]

    fig = _build_figure(rows)
    drop_ax = fig.axes[2]
    lines = drop_ax.get_lines()

    assert len(lines) == 2
    stream_a_line, stream_b_line = lines
    assert list(stream_a_line.get_ydata()) == [0, 1, 0, 1]
    assert list(stream_b_line.get_ydata()) == [0, 0, -1, -1]
