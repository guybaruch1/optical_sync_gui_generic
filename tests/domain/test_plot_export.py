import math
import os

import matplotlib
matplotlib.use("Agg")

from domain.plot_export import (
    export_session_plot, _build_figure, _to_plot_value,
    _figure_width, _MIN_FIGURE_WIDTH, _MAX_FIGURE_WIDTH,
    export_cross_camera_plot,
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


# --- export_cross_camera_plot: one line per (slave_camera_id,
# stream_identity) pair, same NaN-for-excluded convention as the intra-camera
# plot, sourced from the exact rows engine.cross_camera_reconciler produces. ---

def _cross_row(pair_index, slave_camera_id="cam2", stream_identity="infrared1",
                pairing_gap_us=-10.0, excluded=False,
                global_ts_gap_us=-10.0, global_ts_gap_us_excluded=False,
                position_gap_ms=1.0, position_gap_ms_excluded=False):
    return {
        "pair_index": pair_index, "master_camera_id": "cam1", "slave_camera_id": slave_camera_id,
        "stream_identity": stream_identity,
        "pairing_gap_us": pairing_gap_us, "pairing_gap_us_excluded": excluded,
        "global_ts_gap_us": global_ts_gap_us, "global_ts_gap_us_excluded": global_ts_gap_us_excluded,
        "position_gap_ms": position_gap_ms, "position_gap_ms_excluded": position_gap_ms_excluded,
    }


def test_export_cross_camera_plot_writes_a_file(tmp_path):
    rows = [_cross_row(0), _cross_row(1, pairing_gap_us=-12.0)]
    path = str(tmp_path / "cross_camera_sync_plot_slave1.png")

    export_cross_camera_plot(rows, path, title="Slave 1: D455 B (SN 1)  vs.  Master: D455 A (SN 0)")

    assert os.path.exists(path)
    assert os.path.getsize(path) > 0


def test_export_cross_camera_plot_handles_empty_rows(tmp_path):
    path = str(tmp_path / "cross_camera_sync_plot_slave1.png")

    export_cross_camera_plot([], path, title="Slave 1")

    assert os.path.exists(path)


def test_export_cross_camera_plot_draws_one_line_per_identity():
    # Rows are pre-filtered to ONE slave by the caller (gui/pages/
    # multi_camera_live_session_page.py) - a single figure can still have
    # multiple lines if that one slave shares multiple stream identities
    # with master. 4, not 2: each identity now draws BOTH an HW TS Latency
    # line and a Global TS Latency line on this same axis (see
    # test_export_cross_camera_plot_draws_global_ts_gap_as_dashed_line_same_color_as_hw_ts_latency
    # for how they're told apart).
    rows = [
        _cross_row(0, stream_identity="infrared1", pairing_gap_us=-10.0),
        _cross_row(1, stream_identity="infrared1", pairing_gap_us=-11.0),
        _cross_row(0, stream_identity="color", pairing_gap_us=5.0),
    ]

    import matplotlib.pyplot as plt
    from domain.plot_export import _build_cross_camera_figure
    fig = _build_cross_camera_figure(rows, title="Slave 1")
    lines = fig.axes[0].get_lines()

    assert len(lines) == 4
    plt.close(fig)


def test_export_cross_camera_plot_draws_global_ts_gap_as_dashed_line_same_color_as_hw_ts_latency():
    rows = [_cross_row(0, stream_identity="infrared1", pairing_gap_us=-10.0, global_ts_gap_us=-2.0)]

    import matplotlib.pyplot as plt
    from domain.plot_export import _build_cross_camera_figure
    fig = _build_cross_camera_figure(rows, title="Slave 1")
    hw_line, global_line = fig.axes[0].get_lines()

    assert hw_line.get_linestyle() == "-"
    assert global_line.get_linestyle() == "--"
    assert hw_line.get_color() == global_line.get_color()
    assert global_line.get_ydata()[0] == -2.0
    plt.close(fig)


def test_export_cross_camera_plot_nans_out_excluded_global_ts_gap_values():
    rows = [_cross_row(0, global_ts_gap_us=99999.0, global_ts_gap_us_excluded=True)]

    import matplotlib.pyplot as plt
    from domain.plot_export import _build_cross_camera_figure
    fig = _build_cross_camera_figure(rows, title="Slave 1")
    _, global_line = fig.axes[0].get_lines()

    assert math.isnan(global_line.get_ydata()[0])
    plt.close(fig)


def test_export_cross_camera_plot_nans_out_excluded_values():
    rows = [_cross_row(0, pairing_gap_us=99999.0, excluded=True)]

    import matplotlib.pyplot as plt
    from domain.plot_export import _build_cross_camera_figure
    fig = _build_cross_camera_figure(rows, title="Slave 1")
    line = fig.axes[0].get_lines()[0]

    assert math.isnan(line.get_ydata()[0])
    plt.close(fig)


def test_export_cross_camera_plot_draws_position_gap_on_second_axis():
    rows = [
        _cross_row(0, stream_identity="infrared1"),
        _cross_row(1, stream_identity="infrared1"),
        _cross_row(0, stream_identity="color"),
    ]

    import matplotlib.pyplot as plt
    from domain.plot_export import _build_cross_camera_figure
    fig = _build_cross_camera_figure(rows, title="Slave 1")
    lines = fig.axes[1].get_lines()

    assert len(lines) == 2
    plt.close(fig)


def test_export_cross_camera_plot_nans_out_excluded_position_gap_values():
    rows = [_cross_row(0, position_gap_ms=99.0, position_gap_ms_excluded=True)]

    import matplotlib.pyplot as plt
    from domain.plot_export import _build_cross_camera_figure
    fig = _build_cross_camera_figure(rows, title="Slave 1")
    line = fig.axes[1].get_lines()[0]

    assert math.isnan(line.get_ydata()[0])
    plt.close(fig)


def test_export_cross_camera_plot_sets_the_given_title():
    rows = [_cross_row(0)]

    import matplotlib.pyplot as plt
    from domain.plot_export import _build_cross_camera_figure
    fig = _build_cross_camera_figure(rows, title="Slave 1: D455 B  vs.  Master: D455 A")

    assert fig._suptitle.get_text() == "Slave 1: D455 B  vs.  Master: D455 A"
    plt.close(fig)


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
