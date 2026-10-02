from __future__ import annotations

import pytest

from commontrace import experiment, integrity, value

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
        ledger = self._report().ledger()
        key = b"issuer-secret-key"
        sig = value.sign_ledger(ledger, key, org_id="org_1", issued_at="t")
        assert not value.verify_ledger_signature(
            ledger, sig, key, org_id="org_2", issued_at="t"
        )

    def test_a_signature_cannot_be_replayed_at_a_later_date(self):
        ledger = self._report().ledger()
        key = b"issuer-secret-key"
        sig = value.sign_ledger(ledger, key, org_id="org_1", issued_at="2026-01-01T00:00:00Z")
        assert not value.verify_ledger_signature(
            ledger, sig, key, org_id="org_1", issued_at="2026-09-11T00:00:00Z"
        )

    def test_a_fabricated_replacement_chain_verifies_but_does_not_sign(self):
        genuine = self._report().ledger()
        key = b"issuer-secret-key"
        genuine_sig = value.sign_ledger(genuine, key, org_id="org_1", issued_at="t")

        fabricated = value.compute(
            [_effect("helps", experiment.VERDICT_HELPS, 0.05)],
            _clean_audit(), rate_card=SUPPORT_CARD, overlap=DISJOINT,
        ).ledger()
        assert value.verify_ledger(fabricated) is None
        assert not value.verify_ledger_signature(
            fabricated, genuine_sig, key, org_id="org_1", issued_at="t"
        )

    def test_a_tail_edit_that_still_passes_verify_ledger_fails_the_signature(self):
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

        assert value.verify_ledger(tampered) is None
        assert not value.verify_ledger_signature(
            tampered, sig, key, org_id="org_1", issued_at="t"
        )

    def test_an_empty_ledger_still_signs_off_the_genesis(self):
        key = b"issuer-secret-key"
        sig = value.sign_ledger([], key, org_id="org_1", issued_at="t")
        assert value.ledger_root([]) == value._LEDGER_GENESIS
        assert value.verify_ledger_signature([], sig, key, org_id="org_1", issued_at="t")


class TestTheLedgerRefusesWhenTheNumberWould:
    def test_a_compromised_experiment_gets_no_ledger(self):
        compromised = integrity.audit([
            integrity.Assignment(
                lesson="L", occasion_id=f"o{i}", injected=i % 2 == 0, rate=0.5,
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
