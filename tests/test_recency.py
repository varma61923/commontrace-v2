import datetime

from commontrace import recency


def _iso_days_ago(days: float, now: datetime.datetime) -> str:
    return (now - datetime.timedelta(days=days)).isoformat()


class TestAdjustmentOrdering:
    def test_a_lesson_hit_today_scores_higher_than_one_hit_last_year(self):
        now = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
        fresh = recency.adjustment(_iso_days_ago(0, now), now=now)
        stale = recency.adjustment(_iso_days_ago(365, now), now=now)
        assert fresh > stale

    def test_never_hit_scores_no_better_than_any_dated_hit(self):
        now = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
        never = recency.adjustment("NEVER", now=now)
        for age in (0, 1, 30, 180, 3650):
            assert never <= recency.adjustment(_iso_days_ago(age, now), now=now)

    def test_a_lesson_hit_today_scores_the_maximum(self):
        now = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
        assert recency.adjustment(_iso_days_ago(0, now), now=now) == 1.0

    def test_exactly_one_half_life_old_is_neutral(self):
        now = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
        adj = recency.adjustment(
            _iso_days_ago(recency.DEFAULT_HALF_LIFE_DAYS, now), now=now,
        )
        assert abs(adj) < 1e-9

    def test_arbitrarily_old_never_scores_below_the_never_hit_floor(self):
        now = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
        ancient = recency.adjustment(_iso_days_ago(100_000, now), now=now)
        assert ancient >= recency.NEVER_HIT_ADJUSTMENT

    def test_monotonic_across_a_range_of_ages(self):
        now = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
        ages = [0, 1, 5, 10, 30, 90, 180, 365, 730, 3650]
        scores = [recency.adjustment(_iso_days_ago(a, now), now=now) for a in ages]
        assert scores == sorted(scores, reverse=True)


class TestParsing:
    def test_never_is_the_never_hit_floor(self):
        assert recency.adjustment("NEVER") == recency.NEVER_HIT_ADJUSTMENT

    def test_empty_is_treated_like_never(self):
        assert recency.adjustment("") == recency.NEVER_HIT_ADJUSTMENT

    def test_a_bare_yaml_date_parses(self):
        now = datetime.datetime(2026, 7, 2, tzinfo=datetime.timezone.utc)
        adj = recency.adjustment("2026-07-01", now=now)
        assert adj > 0.9

    def test_a_hand_edited_garbage_date_does_not_crash_and_reads_as_never(self):
        assert recency.adjustment("not-a-date") == recency.NEVER_HIT_ADJUSTMENT

    def test_an_iso_timestamp_with_z_suffix_parses(self):
        now = datetime.datetime(2026, 7, 2, tzinfo=datetime.timezone.utc)
        adj = recency.adjustment("2026-07-01T00:00:00Z", now=now)
        assert adj > 0.9


class TestRecencyLookup:
    def test_builds_one_entry_per_lesson_keyed_by_name(self):
        now = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
        lessons = [
            ("path/a", {"name": "a", "last_hit": _iso_days_ago(0, now)}),
            ("path/b", {"name": "b", "last_hit": "NEVER"}),
        ]
        lookup = recency.recency_lookup(lessons, now=now)
        assert set(lookup) == {"a", "b"}
        assert lookup["a"] > lookup["b"]

    def test_a_half_life_override_is_honoured(self):
        now = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
        lessons = [("path/a", {"name": "a", "last_hit": _iso_days_ago(10, now)})]
        short = recency.recency_lookup(lessons, now=now, half_life_days=1.0)["a"]
        long = recency.recency_lookup(lessons, now=now, half_life_days=1000.0)["a"]
        assert short < long
