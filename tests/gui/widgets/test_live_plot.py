from gui.widgets.live_plot import LivePlot


def test_add_point_accumulates_series_data(qapp):
    plot = LivePlot()
    plot.add_series("pairing_gap_us", color="r")
    plot.add_point("pairing_gap_us", x=0, y=10.0)
    plot.add_point("pairing_gap_us", x=1, y=-5.0)

    xs, ys = plot.get_series_data("pairing_gap_us")
    assert xs == [0, 1]
    assert ys == [10.0, -5.0]


def test_set_series_visible_toggles_curve_visibility(qapp):
    plot = LivePlot()
    plot.add_series("position_gap_ms", color="g")
    plot.set_series_visible("position_gap_ms", False)
    assert plot._curves["position_gap_ms"].isVisible() is False
    plot.set_series_visible("position_gap_ms", True)
    assert plot._curves["position_gap_ms"].isVisible() is True


def test_two_independent_series_do_not_interfere(qapp):
    plot = LivePlot()
    plot.add_series("a", color="r")
    plot.add_series("b", color="b")
    plot.add_point("a", 0, 1.0)
    plot.add_point("b", 0, 99.0)
    assert plot.get_series_data("a") == ([0], [1.0])
    assert plot.get_series_data("b") == ([0], [99.0])


def test_add_point_bounds_history_to_max_points(qapp):
    # A long-running session must not let each point's cost grow with the
    # whole session's length - old points fall off instead of accumulating
    # forever (see LivePlot's docstring for why this matters).
    plot = LivePlot(max_points=3)
    plot.add_series("pairing_gap_us", color="r")
    for i in range(5):
        plot.add_point("pairing_gap_us", i, float(i))

    xs, ys = plot.get_series_data("pairing_gap_us")
    assert xs == [2, 3, 4]
    assert ys == [2.0, 3.0, 4.0]


def test_addLegend_is_configured(qapp):
    plot = LivePlot()
    assert plot.getPlotItem().legend is not None


def test_clear_empties_all_series(qapp):
    plot = LivePlot()
    plot.add_series("a", color="r")
    plot.add_series("b", color="b")
    plot.add_point("a", 0, 1.0)
    plot.add_point("b", 0, 99.0)

    plot.clear_data()

    assert plot.get_series_data("a") == ([], [])
    assert plot.get_series_data("b") == ([], [])


def test_add_point_after_clear_starts_fresh_not_appended_to_old_data(qapp):
    # A new session's pair_index restarts at 0 - without clear(), its points
    # would land alongside/on top of whatever the previous session left in
    # the deque instead of replacing it.
    plot = LivePlot()
    plot.add_series("a", color="r")
    plot.add_point("a", 0, 1.0)
    plot.add_point("a", 1, 2.0)

    plot.clear_data()
    plot.add_point("a", 0, 5.0)

    assert plot.get_series_data("a") == ([0], [5.0])
