"""An effect estimate is a statement about the world when it was measured.

The product's argument is that a memory earns its place by measured effect.
Nothing in that sentence had a date in it, and every part of it should: six
months later the API the lesson described is deprecated, the policy it
encoded has changed -- and the estimate is unchanged, because nothing re-ran
it. The lesson is still active, still injected, still counted, still billed.

The Hub already expired graduation from the pinned working-set block at a
180-day horizon. The value ledger -- the surface attached to money -- had no
horizon at all, so the two surfaces disagreed about whether the same evidence
was current and the one that disagreed was the one the customer pays on.

What these tests defend, in order of how badly getting it wrong would hurt:

1. **The asymmetry.** Expiring every stale verdict is the obvious
   implementation and is wrong in the vendor's favour: a stale HURTS that
   stopped counting would RAISE the invoice, letting a vendor delete its own
   harms by waiting. A stale HELPS stops counting; a stale HURTS keeps
   counting. Both rules move the figure down.
2. **Undated is not fresh.** "We cannot tell when this was measured" and
   "this was measured too long ago" have the same standing in an argument
   about whether a number is current.
3. **Opting in is a decision.** Switching a horizon on changes an invoice, so
   a caller that passes none must get exactly the historical behaviour.
4. **The withheld memory leaves the signed ledger too**, or the ledger and
   the figure disagree about the same period.
"""
from __future__ import annotations

import datetime

import pytest

from commontrace import decay, experiment, integrity, value

NOW = datetime.datetime(2026, 9, 12, tzinfo=datetime.timezone.utc)
HORIZON = 180

#: The memories were injected on non-overlapping occasion sets, so they may
#: legitimately be added (commontrace/value.py's aggregate gate). Without
#: this the aggregate is withheld and the ledger is empty for a reason that
#: has nothing to do with decay.
DISJOINT = value.OccasionOverlap(
    shared_pairs=frozenset(), unique_injected_occasions=400)


def _ago(days: float) -> str:
    return (NOW - datetime.timedelta(days=days)).isoformat()


def _effect(slug, verdict, effect, n_injected=200, half_width=0.02):
    return experiment.CausalEffect(
        lesson_slug=slug, n_injected=n_injected, n_withheld=n_injected,
        rate_injected=0.8, rate_withheld=0.8 - effect, effect=effect,
        ci_low=effect - half_width, ci_high=effect + half_width, p_value=0.001,
        significant=verdict in (experiment.VERDICT_HELPS, experiment.VERDICT_HURTS),
        min_detectable_effect=0.02, verdict=verdict,
    )


def _clean_audit():
    return integrity.audit([
        integrity.Assignment(
            lesson="L", occasion_id=f"o{i}", injected=i % 2 == 0,
            rate=0.5, succeeded=(i % 3 != 0),
        )
        for i in range(400)
    ])


# --- the freshness judgement -------------------------------------------------

class TestFreshness:
    def test_recent_evidence_is_current(self):
        f = decay.freshness(_ago(10), now=NOW, horizon_days=HORIZON)
        assert f.state == decay.FRESH
        assert f.is_current
        assert 9 <= f.age_days <= 11

    def test_evidence_past_the_horizon_is_stale(self):
        f = decay.freshness(_ago(400), now=NOW, horizon_days=HORIZON)
        assert f.state == decay.STALE
        assert not f.is_current
        assert "past the 180-day evidence horizon" in f.describe()

    def test_the_boundary_is_inclusive(self):
        """Exactly at the horizon is still current: an off-by-one here
        expires a memory a day early and the operator cannot tell why."""
        assert decay.freshness(
            _ago(HORIZON), now=NOW, horizon_days=HORIZON).is_current

    def test_undated_evidence_is_not_current(self):
        """Assuming in favour of the vendor because a timestamp is missing
        is how missing timestamps become convenient."""
        for missing in (None, "", "   ", 42, [], "not-a-date"):
            f = decay.freshness(missing, now=NOW, horizon_days=HORIZON)
            assert f.state == decay.UNDATED, missing
            assert not f.is_current
            assert f.age_days is None

    def test_a_naive_timestamp_is_read_as_utc(self):
        naive = (NOW - datetime.timedelta(days=5)).replace(tzinfo=None).isoformat()
        assert decay.freshness(naive, now=NOW, horizon_days=HORIZON).is_current

    def test_a_datetime_works_as_well_as_a_string(self):
        """This is fed by a JSONL line, a Postgres column and a hand-edited
        YAML field."""
        moment = NOW - datetime.timedelta(days=5)
        assert decay.freshness(moment, now=NOW, horizon_days=HORIZON).is_current

    def test_clock_skew_does_not_produce_a_negative_age(self):
        future = (NOW + datetime.timedelta(seconds=30)).isoformat()
        assert decay.freshness(future, now=NOW, horizon_days=HORIZON).age_days == 0.0

    def test_a_zero_or_negative_horizon_is_clamped(self):
        assert decay.freshness(_ago(1), now=NOW, horizon_days=0).horizon_days == 1


# --- the asymmetry -----------------------------------------------------------

class TestStaleResolvesAgainstTheVendor:
    def _f(self, days):
        return decay.freshness(_ago(days), now=NOW, horizon_days=HORIZON)

    def _counts(self, verdict, days):
        return decay.still_counts(
            verdict, self._f(days),
            helps=experiment.VERDICT_HELPS, hurts=experiment.VERDICT_HURTS,
        )

    def test_a_fresh_helps_counts(self):
        counts, why = self._counts(experiment.VERDICT_HELPS, 10)
        assert counts and why == ""

    def test_a_stale_helps_stops_counting(self):
        counts, why = self._counts(experiment.VERDICT_HELPS, 400)
        assert not counts
        assert "not billed" in why
        assert "Re-run the holdout" in why

    def test_a_stale_hurts_KEEPS_counting(self):
        """The failure the obvious implementation has. A stale HURTS that
        stopped counting would RAISE the invoice -- a vendor deleting its own
        damage by waiting long enough."""
        counts, why = self._counts(experiment.VERDICT_HURTS, 400)
        assert counts
        assert why == ""

    def test_a_stale_verdict_that_never_counted_gets_no_invented_reason(self):
        """UNDERPOWERED was already excluded for its own reason; staleness
        must not overwrite it with one that sends the operator elsewhere."""
        counts, why = self._counts(experiment.VERDICT_UNDERPOWERED, 400)
        assert not counts
        assert why == ""


# --- through the value report ------------------------------------------------

class TestValueReport:
    EFFECTS = [
        _effect("fresh-helps", experiment.VERDICT_HELPS, 0.20),
        _effect("stale-helps", experiment.VERDICT_HELPS, 0.20),
        _effect("stale-hurts", experiment.VERDICT_HURTS, -0.15),
    ]
    DATES = {
        "fresh-helps": _ago(10),
        "stale-helps": _ago(400),
        "stale-hurts": _ago(400),
    }

    def _with_horizon(self, **kwargs):
        return value.compute(
            self.EFFECTS, _clean_audit(), last_measured=self.DATES,
            evidence_horizon_days=HORIZON, now=NOW, **kwargs,
        )

    def test_no_horizon_is_exactly_the_historical_behaviour(self):
        """Switching a horizon on changes an invoice, so it is a decision
        rather than an upgrade."""
        report = value.compute(self.EFFECTS, _clean_audit())
        assert report.n_counted == 3
        assert report.decay is None

    def test_a_horizon_withholds_the_stale_helps(self):
        report = self._with_horizon()
        counted = {m.slug for m in report.memories if m.counted}
        assert "stale-helps" not in counted
        assert "fresh-helps" in counted

    def test_a_horizon_keeps_the_stale_hurts(self):
        report = self._with_horizon()
        counted = {m.slug for m in report.memories if m.counted}
        assert "stale-hurts" in counted

    def test_both_rules_move_the_figure_down(self):
        """Not a coincidence -- it is the rule. When evidence decays it
        resolves against the party who benefits from the doubt."""
        without = value.compute(self.EFFECTS, _clean_audit())
        with_horizon = self._with_horizon()
        assert with_horizon.occasions_improved < without.occasions_improved

    def test_the_withheld_memory_explains_itself(self):
        report = self._with_horizon()
        stale = next(m for m in report.memories if m.slug == "stale-helps")
        assert not stale.counted
        assert "400 days ago" in stale.why_not

    def test_the_withheld_memory_leaves_the_signed_ledger_too(self):
        """Otherwise the ledger and the figure describe different periods."""
        report = self._with_horizon(
            value_per_occasion=25.0, overlap=DISJOINT)
        entries = report.ledger()
        assert entries
        slugs = {e.slug for e in entries}
        assert "stale-helps" not in slugs
        assert "fresh-helps" in slugs

    def test_the_report_says_what_the_horizon_did(self):
        rendered = self._with_horizon().decay.render()
        assert "no longer billed" in rendered
        assert "STILL counted" in rendered
        assert "stale-helps" in rendered

    def test_an_undated_effect_is_withheld_like_an_expired_one(self):
        report = value.compute(
            self.EFFECTS, _clean_audit(),
            last_measured={"fresh-helps": _ago(10)},  # the others have no date
            evidence_horizon_days=HORIZON, now=NOW,
        )
        counted = {m.slug for m in report.memories if m.counted}
        assert "stale-helps" not in counted
        # ...and the undated HURTS still counts, for the same reason a dated
        # stale one does.
        assert "stale-hurts" in counted

    def test_everything_fresh_changes_nothing(self):
        fresh = {slug: _ago(5) for slug in self.DATES}
        without = value.compute(self.EFFECTS, _clean_audit())
        with_horizon = value.compute(
            self.EFFECTS, _clean_audit(), last_measured=fresh,
            evidence_horizon_days=HORIZON, now=NOW,
        )
        assert with_horizon.n_counted == without.n_counted
        assert with_horizon.occasions_improved == pytest.approx(
            without.occasions_improved)
        assert with_horizon.decay.stale == ()


# --- what an operator does about it ------------------------------------------

class TestDecayReport:
    def _report(self):
        return value.compute(
            TestValueReport.EFFECTS, _clean_audit(),
            last_measured=TestValueReport.DATES,
            evidence_horizon_days=HORIZON, now=NOW,
        ).decay

    def test_it_lists_what_to_re_measure_oldest_first(self):
        due = self._report().due_for_remeasurement
        assert set(due) == {"stale-helps", "stale-hurts"}

    def test_it_separates_stale_from_actually_withheld(self):
        """A stale HURTS is still counted, and calling it "decayed" without
        that distinction sends an operator to re-measure the wrong thing."""
        report = self._report()
        assert {i.slug for i in report.stale} == {"stale-helps", "stale-hurts"}
        assert {i.slug for i in report.withheld} == {"stale-helps"}

    def test_an_empty_run_says_so_rather_than_rendering_nothing(self):
        assert "No measured effects" in decay.DecayReport().render()

    def test_a_clean_run_says_everything_is_inside_the_horizon(self):
        report = value.compute(
            TestValueReport.EFFECTS, _clean_audit(),
            last_measured={s: _ago(5) for s in TestValueReport.DATES},
            evidence_horizon_days=HORIZON, now=NOW,
        ).decay
        assert "All evidence is inside the horizon" in report.render()
