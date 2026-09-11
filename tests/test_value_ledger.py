"""Tiered rates and a ledger a customer can check.

`value.py`'s existing rules are what make its number defensible rather than
promotional: a compromised experiment produces no figure, an underpowered
memory contributes nothing, and memories that HURT are subtracted rather than
dropped. Everything here has to hold those, because the two additions are
exactly the kind that erode them if nobody is watching:

  a rate card   invites a negotiated input to be read back later as if it
                had been measured;
  a ledger      makes a figure look audited, which is worse than no ledger
                at all when the figure should not have been stated.

So the tests below check the refusals as carefully as the arithmetic.
"""
from __future__ import annotations

import pytest

from commontrace import experiment, integrity, value

SUPPORT_CARD = value.RateCard(tiers=(
    value.Tier("L1 informational", 0.55, 8.00),
    value.Tier("L2 transactional", 0.30, 35.00),
    value.Tier("L3 technical", 0.13, 120.00),
    value.Tier("critical escalation", 0.02, 350.00),
))


def _effect(slug, verdict, effect, n_injected=200):
    return experiment.CausalEffect(
        lesson_slug=slug, n_injected=n_injected, n_withheld=n_injected,
        rate_injected=0.8, rate_withheld=0.8 - effect, effect=effect,
        ci_low=effect - 0.02, ci_high=effect + 0.02, p_value=0.001,
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


class TestTheRateCard:
    def test_the_blended_rate_is_the_share_weighted_cost(self):
        expected = 0.55 * 8 + 0.30 * 35 + 0.13 * 120 + 0.02 * 350
        assert SUPPORT_CARD.blended_rate == pytest.approx(expected)

    def test_shares_that_do_not_cover_every_occasion_are_refused(self):
        """A mix summing to less than one prices a volume that was never
        measured, and summing to more counts occasions twice."""
        with pytest.raises(ValueError, match="sum to"):
            value.RateCard(tiers=(value.Tier("only half", 0.5, 10.0),))

    def test_a_rounding_sized_gap_is_tolerated(self):
        thirds = value.RateCard(tiers=(
            value.Tier("a", 1 / 3, 9.0),
            value.Tier("b", 1 / 3, 9.0),
            value.Tier("c", 1 / 3, 9.0),
        ))
        assert thirds.blended_rate == pytest.approx(9.0)

    def test_a_negative_rate_is_refused(self):
        with pytest.raises(ValueError, match="negative cost"):
            value.RateCard(tiers=(value.Tier("bad", 1.0, -5.0),))

    def test_an_empty_card_is_refused(self):
        with pytest.raises(ValueError, match="at least one tier"):
            value.RateCard(tiers=())


class TestTheRateCardPricesTheReport:
    def test_a_card_prices_the_same_occasions_as_a_flat_rate_would(self):
        report = value.compute(
            [_effect("a", experiment.VERDICT_HELPS, 0.05)],
            _clean_audit(), rate_card=SUPPORT_CARD,
        )
        assert report.readable
        assert report.money == pytest.approx(
            report.occasions_improved * SUPPORT_CARD.blended_rate
        )

    def test_a_card_wins_over_a_flat_rate(self):
        """Both supplied means the customer has superseded the flat number;
        quietly preferring the vaguer one would price against a figure they
        have already replaced."""
        report = value.compute(
            [_effect("a", experiment.VERDICT_HELPS, 0.05)],
            _clean_audit(), value_per_occasion=1.0, rate_card=SUPPORT_CARD,
        )
        assert report.rate == pytest.approx(SUPPORT_CARD.blended_rate)

    def test_no_rate_at_all_still_yields_a_count_and_no_money(self):
        report = value.compute(
            [_effect("a", experiment.VERDICT_HELPS, 0.05)], _clean_audit()
        )
        assert report.occasions_improved > 0
        assert report.money is None


class TestTheLedger:
    def test_every_counted_memory_gets_a_line(self):
        report = value.compute(
            [
                _effect("helps", experiment.VERDICT_HELPS, 0.05),
                _effect("hurts", experiment.VERDICT_HURTS, -0.03),
                _effect("weak", experiment.VERDICT_UNDERPOWERED, 0.09),
            ],
            _clean_audit(), rate_card=SUPPORT_CARD,
        )
        ledger = report.ledger()
        assert [e.slug for e in ledger] == ["helps", "hurts"]
        assert value.verify_ledger(ledger) is None

    def test_a_memory_that_hurts_carries_negative_money(self):
        """The invariant that keeps this a measurement rather than a
        brochure, now visible on the invoice itself."""
        report = value.compute(
            [_effect("hurts", experiment.VERDICT_HURTS, -0.03)],
            _clean_audit(), rate_card=SUPPORT_CARD,
        )
        [entry] = report.ledger()
        assert entry.money < 0

    def test_editing_a_figure_breaks_the_chain(self):
        report = value.compute(
            [
                _effect("a", experiment.VERDICT_HELPS, 0.05),
                _effect("b", experiment.VERDICT_HELPS, 0.04),
                _effect("c", experiment.VERDICT_HELPS, 0.03),
            ],
            _clean_audit(), rate_card=SUPPORT_CARD,
        )
        ledger = report.ledger()
        import dataclasses
        tampered = list(ledger)
        tampered[1] = dataclasses.replace(tampered[1], occasions_improved=999.0)
        assert value.verify_ledger(tampered) == 1

    def test_deleting_an_inconvenient_line_breaks_the_chain(self):
        """The failure mode a spreadsheet cannot detect: quietly dropping the
        one memory that HURT before sending the invoice."""
        report = value.compute(
            [
                _effect("a", experiment.VERDICT_HELPS, 0.05),
                _effect("hurts", experiment.VERDICT_HURTS, -0.03),
                _effect("c", experiment.VERDICT_HELPS, 0.03),
            ],
            _clean_audit(), rate_card=SUPPORT_CARD,
        )
        ledger = report.ledger()
        assert value.verify_ledger(ledger) is None
        without_the_bad_news = [ledger[0], ledger[2]]
        assert value.verify_ledger(without_the_bad_news) == 1

    def test_reordering_to_bury_a_line_breaks_the_chain(self):
        report = value.compute(
            [
                _effect("a", experiment.VERDICT_HELPS, 0.05),
                _effect("b", experiment.VERDICT_HELPS, 0.04),
            ],
            _clean_audit(), rate_card=SUPPORT_CARD,
        )
        ledger = report.ledger()
        assert value.verify_ledger(list(reversed(ledger))) == 0

    def test_the_chain_starts_from_a_named_genesis(self):
        """An empty-string genesis would let this chain be spliced into any
        other SHA-256 chain that also started from nothing."""
        report = value.compute(
            [_effect("a", experiment.VERDICT_HELPS, 0.05)],
            _clean_audit(), rate_card=SUPPORT_CARD,
        )
        [entry] = report.ledger()
        assert entry.previous_hash == value._LEDGER_GENESIS
        assert entry.previous_hash != ""

    def test_an_empty_ledger_verifies(self):
        assert value.verify_ledger([]) is None


class TestTheLedgerRefusesWhenTheNumberWould:
    def test_a_compromised_experiment_gets_no_ledger(self):
        """THE rule. A verifiable chain computed off a biased sample would
        make an unsupportable figure look audited -- strictly worse than no
        ledger, because it invites the reader to trust it."""
        compromised = integrity.audit([
            integrity.Assignment(
                lesson="L", occasion_id=f"o{i}", injected=i % 2 == 0, rate=0.5,
                # Only the treated arm ever reports: textbook attrition.
                succeeded=True if i % 2 == 0 else None,
            )
            for i in range(400)
        ])
        assert not compromised.readable
        report = value.compute(
            [_effect("a", experiment.VERDICT_HELPS, 0.05)],
            compromised, rate_card=SUPPORT_CARD,
        )
        assert report.money is None
        assert report.ledger() == []

    def test_no_agreed_rate_means_no_ledger(self):
        report = value.compute(
            [_effect("a", experiment.VERDICT_HELPS, 0.05)], _clean_audit()
        )
        assert report.ledger() == []

    def test_an_unaudited_run_gets_no_ledger(self):
        report = value.compute(
            [_effect("a", experiment.VERDICT_HELPS, 0.05)],
            None, rate_card=SUPPORT_CARD,
        )
        assert not report.readable
        assert report.ledger() == []
