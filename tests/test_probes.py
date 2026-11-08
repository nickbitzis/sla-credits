from slacredits.probes import ProbeData, popcount, regions_of, window_down


def make():
    p = ProbeData()
    p.fail("api", "us-east", 0, 10)       # one region, minutes 0-9
    p.fail("api", "eu-west", 5, 15)       # two regions on 5-9
    p.fail("api", "ap-south", 8, 9)       # three regions on minute 8
    return p


def test_two_of_three_requires_two_regions():
    assert make().down_minutes("api", 2, 0, 100) == [5, 6, 7, 8, 9]


def test_one_of_three_counts_any_region():
    assert len(make().down_minutes("api", 1, 0, 100)) == 15


def test_three_of_three_only_full_outage():
    assert make().down_minutes("api", 3, 0, 100) == [8]


def test_down_minutes_respects_range():
    assert make().down_minutes("api", 1, 6, 9) == [6, 7, 8]


def test_other_components_unaffected():
    assert make().down_minutes("dashboard", 1, 0, 100) == []


def test_mask_and_regions():
    p = make()
    assert popcount(p.mask("api", 8)) == 3
    assert regions_of(p.mask("api", 6)) == ["us-east", "eu-west"]


def test_timeline_runs_group_identical_masks():
    runs = make().timeline("api", 0, 100)
    assert [(s, e) for s, e, _ in runs] == [(0, 5), (5, 8), (8, 9), (9, 10), (10, 15)]


def test_cache_invalidated_on_new_failure():
    p = make()
    assert p.down_minutes("api", 2, 0, 100) == [5, 6, 7, 8, 9]
    p.fail("api", "ap-south", 20, 22)
    p.fail("api", "us-east", 20, 21)
    assert p.down_minutes("api", 2, 0, 100) == [5, 6, 7, 8, 9, 20]


def test_window_any_down_minute_counts_whole_window():
    minutes, windows = window_down({7}, set(), 0, 20, threshold=1)
    assert (minutes, windows) == (5, 1)


def test_window_threshold_three():
    assert window_down({5, 6}, set(), 0, 20, threshold=3) == (0, 0)
    assert window_down({5, 6, 9}, set(), 0, 20, threshold=3) == (5, 1)


def test_window_excluded_minutes_are_not_counted():
    # window 5-9: minutes 5, 6 down, minutes 8, 9 excluded -> 3 eligible minutes of downtime
    assert window_down({5, 6}, {8, 9}, 0, 20, threshold=1) == (3, 1)


def test_window_clipped_at_range_boundary():
    assert window_down({11}, set(), 0, 12, threshold=1) == (2, 1)


def test_fingerprint_changes_with_data():
    a, b = make(), make()
    assert a.fingerprint() == b.fingerprint()
    b.fail("export", "us-east", 50, 51)
    assert a.fingerprint() != b.fingerprint()
