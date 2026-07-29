from domain.running_stats import RunningStats


def test_empty_stats_has_no_min_max():
    stats = RunningStats()
    assert stats.count == 0
    assert stats.min is None
    assert stats.max is None


def test_mean_of_single_value():
    stats = RunningStats()
    stats.update(10.0)
    assert stats.mean == 10.0
    assert stats.std == 0.0
    assert stats.min == 10.0
    assert stats.max == 10.0


def test_mean_and_std_match_known_values():
    stats = RunningStats()
    for value in (2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0):
        stats.update(value)
    assert stats.count == 8
    assert stats.mean == 5.0
    assert round(stats.std, 4) == 2.0


def test_min_and_max_track_true_extremes_not_signed_magnitude():
    stats = RunningStats()
    for value in (-37.0, -38.5, -40.0, -36.0):
        stats.update(value)
    assert stats.min == -40.0
    assert stats.max == -36.0


def test_min_and_max_with_mixed_signs():
    stats = RunningStats()
    stats.update(-5.0)
    stats.update(12.0)
    assert stats.min == -5.0
    assert stats.max == 12.0
