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

# "These memories were injected on occasions that do not overlap", which is
# the precondition a SUM of their contributions needs: without it one
# occasion that received two of them would be counted twice, and value.py
# withholds the total and the ledger rather than double-attributing. Stated
# explicitly here so each test below says which world it is in.
DISJOINT = value.OccasionOverlap(shared_pairs=frozenset(), unique_injected_occasions=400)


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
            _clean_audit(), rate_card=SUPPORT_CARD, overlap=DISJOINT,
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
            overlap=DISJOINT,
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
            _clean_audit(), rate_card=SUPPORT_CARD, overlap=DISJOINT,
        )
        ledger = report.ledger()
        assert [e.slug for e in ledger] == ["helps", "hurts"]
        assert value.verify_ledger(ledger) is None

    def test_a_memory_that_hurts_carries_negative_money(self):
        """The invariant that keeps this a measurement rather than a
        brochure, now visible on the invoice itself."""
        report = value.compute(
            [_effect("hurts", experiment.VERDICT_HURTS, -0.03)],
            _clean_audit(), rate_card=SUPPORT_CARD, overlap=DISJOINT,
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
            _clean_audit(), rate_card=SUPPORT_CARD, overlap=DISJOINT,
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
            _clean_audit(), rate_card=SUPPORT_CARD, overlap=DISJOINT,
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
            _clean_audit(), rate_card=SUPPORT_CARD, overlap=DISJOINT,
        )
        ledger = report.ledger()
        assert value.verify_ledger(list(reversed(ledger))) == 0

    def test_the_chain_starts_from_a_named_genesis(self):
        """An empty-string genesis would let this chain be spliced into any
        other SHA-256 chain that also started from nothing."""
        report = value.compute(
            [_effect("a", experiment.VERDICT_HELPS, 0.05)],
            _clean_audit(), rate_card=SUPPORT_CARD, overlap=DISJOINT,
        )
        [entry] = report.ledger()
        assert entry.previous_hash == value._LEDGER_GENESIS
        assert entry.previous_hash != ""

    def test_an_empty_ledger_verifies(self):
        assert value.verify_ledger([]) is None


class TestTheLedgerSignature:
    """verify_ledger proves a chain is internally consistent -- nobody
    edited, dropped, or reordered a line. It does NOT prove who produced the
    chain, because its genesis and algorithm are both public: anyone who can
    write to wherever a ledger is stored can fabricate an entire replacement
    chain from different figures and it will verify exactly as cleanly as a
    genuine one. sign_ledger/verify_ledger_signature close that: only the
    holder of the signing key can produce a signature the customer accepts.
    """

    def _report(self):
        return value.compute(
            [
                _effect("helps", experiment.VERDICT_HELPS, 0.05),
                _effect("hurts", experiment.VERDICT_HURTS, -0.03),
            ],
            _clean_audit(), rate_card=SUPPORT_CARD, overlap=DISJOINT,
        )

    def test_a_genuine_signature_verifies(self):
        ledger = self._report().ledger()
        key = b"issuer-secret-key"
        sig = value.sign_ledger(ledger, key, org_id="org_1", issued_at="2026-09-11T00:00:00Z")
        assert value.verify_ledger_signature(
            ledger, sig, key, org_id="org_1", issued_at="2026-09-11T00:00:00Z"
        )

    def test_the_wrong_key_is_rejected(self):
        ledger = self._report().ledger()
        sig = value.sign_ledger(ledger, b"real-key", org_id="org_1", issued_at="t")
        assert not value.verify_ledger_signature(
            ledger, sig, b"guessed-key", org_id="org_1", issued_at="t"
        )

    def test_a_signature_cannot_be_replayed_onto_another_org(self):
        """Binding org_id into the payload stops a signature minted for one
        customer's ledger from being presented as if it authenticated a
        different customer's identical-looking chain."""
        ledger = self._report().ledger()
        key = b"issuer-secret-key"
        sig = value.sign_ledger(ledger, key, org_id="org_1", issued_at="t")
        assert not value.verify_ledger_signature(
            ledger, sig, key, org_id="org_2", issued_at="t"
        )

    def test_a_signature_cannot_be_replayed_at_a_later_date(self):
        """Binding issued_at stops an old, genuinely-issued signature from
        being re-presented later as if it were freshly minted."""
        ledger = self._report().ledger()
        key = b"issuer-secret-key"
        sig = value.sign_ledger(ledger, key, org_id="org_1", issued_at="2026-01-01T00:00:00Z")
        assert not value.verify_ledger_signature(
            ledger, sig, key, org_id="org_1", issued_at="2026-09-11T00:00:00Z"
        )

    def test_a_fabricated_replacement_chain_verifies_but_does_not_sign(self):
        """THE attack this exists to stop. An attacker with write access to
        storage (but not the signing key) can regenerate an entirely
        different, internally-consistent chain from scratch -- verify_ledger
        alone cannot tell it apart from a genuine one, because both the
        genesis and the algorithm are public. A signature under a key the
        attacker does not hold is the one thing they cannot forge."""
        genuine = self._report().ledger()
        key = b"issuer-secret-key"
        genuine_sig = value.sign_ledger(genuine, key, org_id="org_1", issued_at="t")

        fabricated = value.compute(
            [_effect("helps", experiment.VERDICT_HELPS, 0.05)],
            _clean_audit(), rate_card=SUPPORT_CARD, overlap=DISJOINT,
        ).ledger()
        assert value.verify_ledger(fabricated) is None  # internally consistent...
        assert not value.verify_ledger_signature(  # ...but not genuinely issued
            fabricated, genuine_sig, key, org_id="org_1", issued_at="t"
        )

    def test_a_tail_edit_that_still_passes_verify_ledger_fails_the_signature(self):
        """The gap verify_ledger alone cannot close: an attacker who edits
        only the LAST line and recomputes just that line's own entry_hash to
        match produces a chain that still passes verify_ledger cleanly
        (nothing downstream depends on the last entry), because
        verify_ledger only ever checks that each hash follows from its own
        row -- it has no independent opinion on what the row SHOULD say.
        The signature does: it was minted over the ORIGINAL root, and the
        edit changed the root (the last entry's hash), so it no longer
        matches -- exactly the tamper-after-signing case a hash chain with
        no key cannot catch on its own."""
        import dataclasses
        import hashlib

        ledger = self._report().ledger()
        key = b"issuer-secret-key"
        sig = value.sign_ledger(ledger, key, org_id="org_1", issued_at="t")

        last = ledger[-1]
        new_occasions = 999_999.0
        new_money = new_occasions * last.rate
        row = value._FIELD_SEP.join((
            str(last.index), last.slug, last.verdict,
            f"{new_occasions:.6f}", f"{last.rate:.6f}", f"{new_money:.6f}",
        ))
        new_hash = hashlib.sha256(
            (last.previous_hash + value._FIELD_SEP + row).encode("utf-8")
        ).hexdigest()
        tampered_last = dataclasses.replace(
            last, occasions_improved=new_occasions, money=round(new_money, 2),
            entry_hash=new_hash,
        )
        tampered = list(ledger[:-1]) + [tampered_last]

        assert value.verify_ledger(tampered) is None  # the gap: still "consistent"
        assert not value.verify_ledger_signature(     # the close: signature disagrees
            tampered, sig, key, org_id="org_1", issued_at="t"
        )

    def test_an_empty_ledger_still_signs_off_the_genesis(self):
        """Zero counted lines is itself a claim worth authenticating -- an
        issuer might understate an invoice down to nothing just as easily as
        inflate one, and the root falls back to the named genesis rather
        than being undefined."""
        key = b"issuer-secret-key"
        sig = value.sign_ledger([], key, org_id="org_1", issued_at="t")
        assert value.ledger_root([]) == value._LEDGER_GENESIS
        assert value.verify_ledger_signature([], sig, key, org_id="org_1", issued_at="t")


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
            compromised, rate_card=SUPPORT_CARD, overlap=DISJOINT,
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
            None, rate_card=SUPPORT_CARD, overlap=DISJOINT,
        )
        assert not report.readable
        assert report.ledger() == []
