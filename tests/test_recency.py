"""Tests for commontrace/recency.py.

The behavior worth protecting: the ORDER a competitor's own shipped bug got
wrong. Memary's `_select_top_entities` does `np.argsort(counts)[:TOP_ENTITIES]`
-- ascending sort sliced from the front, which picks the LOWEST-count
entities while reading as "top" ranking. The equivalent mistake here would be
scoring an old lesson higher than a fresh one, so every test below is framed
as an ordering assertion, not just a formula check.
"""
import datetime

from commontrace import recency


def _iso_days_ago(days: float, now: datetime.datetime) -> str:
    return (now - datetime.timedelta(days=days)).isoformat()


class TestAdjustmentOrdering:
    """The one property that matters: fresher never scores worse than
    staler, for any pair of ages."""

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
        """Not just two points -- the whole curve must not invert anywhere,
        which is exactly the class of bug an off-by-one or a flipped sign
        in the decay formula would produce."""
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
        """`last_hit: 2026-07-01` (no quotes) is what a real lesson file
        looks like -- commontrace/lesson_cache.py's own docstring on why
        PyYAML materializes this as a date object, not a string, is the
        same edge case this must tolerate when it arrives as either."""
        now = datetime.datetime(2026, 7, 2, tzinfo=datetime.timezone.utc)
        adj = recency.adjustment("2026-07-01", now=now)
        assert adj > 0.9  # one day old, near the maximum

    def test_a_hand_edited_garbage_date_does_not_crash_and_reads_as_never(self):
        """Lessons are explicitly meant to be hand-edited and are not
        schema-validated before reaching this function -- the same posture
        retrieval.py's own `_rank_int` takes for importance/uses."""
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
        # 10 days is many half-lives under a 1-day half-life (near the
        # floor) but barely any under a 1000-day one (near the maximum).
        assert short < long
