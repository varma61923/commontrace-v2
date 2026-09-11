"""Saying what the experiment would measure before it could see the answer,
and handing over the rows it was measured from.

Two halves of the same problem: every number this product bills on is
computed by this product. The value ledger already makes the invoice
tamper-evident and (with a key) authenticates who issued it -- but both of
those establish only that the issuer's own arithmetic was not altered
afterwards. Neither lets the customer disagree with the arithmetic, and
neither stops the design being settled after the results were visible.
"""
from __future__ import annotations

import datetime

import pytest

from commontrace import integrity, prereg, raw_export, value


def _registration(**overrides) -> prereg.Preregistration:
    defaults = dict(
        primary_outcome="resolved",
        minimum_practical_effect=0.10,
        holdout_rate=0.10,
        planned_occasions=3000,
        stopping_rule=prereg.STOP_SEQUENTIAL,
        salt="salt-a",
        now=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
    )
    defaults.update(overrides)
    return prereg.register(**defaults)


def _assignments(rows):
    return [
        integrity.Assignment(
            lesson=lesson, occasion_id=occasion, injected=injected,
            succeeded=succeeded, rate=0.5, salt="salt-a",
        )
        for lesson, occasion, injected, succeeded in rows
    ]


class TestRegistrationRefusesAnUnrunnableDesign:
    def test_an_unnamed_primary_outcome_is_refused(self):
        """An experiment that has not named the thing it measures cannot be
        said to have found it."""
        with pytest.raises(prereg.PreregError, match="primary outcome"):
            _registration(primary_outcome="   ")

    def test_a_holdout_rate_of_zero_is_refused(self):
        with pytest.raises(prereg.PreregError, match="holdout_rate"):
            _registration(holdout_rate=0.0)

    def test_a_holdout_rate_of_one_is_refused(self):
        with pytest.raises(prereg.PreregError, match="holdout_rate"):
            _registration(holdout_rate=1.0)

    def test_an_unknown_stopping_rule_is_refused(self):
        with pytest.raises(prereg.PreregError, match="stopping rule"):
            _registration(stopping_rule="when-it-looks-good")

    def test_a_non_positive_planned_size_is_refused(self):
        with pytest.raises(prereg.PreregError, match="planned_occasions"):
            _registration(planned_occasions=0)


class TestTheFingerprint:
    def test_it_is_stable_across_round_trips(self):
        registered = _registration()
        assert prereg.loads(prereg.dumps(registered)).fingerprint() == (
            registered.fingerprint()
        )

    def test_changing_any_commitment_changes_it(self):
        base = _registration().fingerprint()
        for change in (
            {"primary_outcome": "escalated"},
            {"minimum_practical_effect": 0.05},
            {"holdout_rate": 0.2},
            {"planned_occasions": 1000},
            {"stopping_rule": prereg.STOP_FIXED_N},
            {"salt": "salt-b"},
        ):
            assert _registration(**change).fingerprint() != base, change


class TestCheckingTheRunAgainstThePromise:
    def test_an_unregistered_run_says_so_rather_than_passing(self):
        """Silence would read as approval. The absence of a registration is
        itself the finding."""
        result = prereg.check(None)
        assert not result.registered
        assert not result.clean
        assert "not pre-registered" in result.note

    def test_a_run_matching_its_registration_is_clean(self):
        result = prereg.check(
            _registration(), actual_salt="salt-a", actual_holdout_rate=0.10,
            actual_detectable=0.10, actual_primary_outcome="resolved",
        )
        assert result.clean
        assert result.deviations == []

    def test_a_moved_endpoint_is_reported(self):
        """Every number computed on the new outcome can be individually
        correct and the conclusion still unsupported."""
        result = prereg.check(
            _registration(), actual_primary_outcome="escalated",
        )
        assert result.registered and not result.clean
        assert any(d.field == "primary_outcome" for d in result.deviations)

    def test_a_changed_detectable_effect_is_reported(self):
        result = prereg.check(_registration(), actual_detectable=0.02)
        [deviation] = [
            d for d in result.deviations if d.field == "minimum_practical_effect"
        ]
        assert "10.0%" in deviation.promised and "2.0%" in deviation.actual

    def test_a_different_salt_means_a_different_experiment(self):
        result = prereg.check(_registration(), actual_salt="salt-b")
        [deviation] = [d for d in result.deviations if d.field == "randomization"]
        assert "DIFFERENT EXPERIMENT" in deviation.detail

    def test_a_changed_holdout_rate_is_reported(self):
        result = prereg.check(_registration(), actual_holdout_rate=0.25)
        assert any(d.field == "holdout_rate" for d in result.deviations)

    def test_stopping_a_fixed_n_design_early_is_reported(self):
        result = prereg.check(
            _registration(stopping_rule=prereg.STOP_FIXED_N),
            actual_occasions=900,
        )
        [deviation] = [d for d in result.deviations if d.field == "stopping_rule"]
        assert "optional stop" in deviation.detail

    def test_a_sequential_design_may_stop_whenever_it_likes(self):
        """That is what registering the sequential rule buys: the boundary
        already accounts for looking, so an early stop is not a deviation."""
        result = prereg.check(
            _registration(stopping_rule=prereg.STOP_SEQUENTIAL),
            actual_occasions=900,
        )
        assert result.clean

    def test_registering_after_the_data_started_is_reported(self):
        """The case worth catching even when nobody was dishonest: it usually
        means the run started before anyone settled what it measured."""
        result = prereg.check(
            _registration(now=datetime.datetime(2026, 3, 1, tzinfo=datetime.timezone.utc)),
            first_observation_at=datetime.datetime(2026, 1, 15, tzinfo=datetime.timezone.utc),
        )
        [deviation] = [d for d in result.deviations if d.field == "registered_at"]
        assert "already arriving" in deviation.detail

    def test_registering_before_the_data_is_clean(self):
        result = prereg.check(
            _registration(now=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)),
            first_observation_at=datetime.datetime(2026, 1, 15, tzinfo=datetime.timezone.utc),
        )
        assert result.clean

    def test_a_naive_timestamp_is_not_a_crash(self):
        """Timestamps arrive from a database, a JSON file and a CLI; one of
        them will be naive, and a validity check that raises on its own input
        is worse than the thing it checks."""
        result = prereg.check(
            _registration(), first_observation_at=datetime.datetime(2026, 1, 15),
        )
        assert result.registered

    def test_unknown_facts_are_not_checked_rather_than_assumed_to_match(self):
        """Callers know different subsets. Treating "not supplied" as
        "matches" would make the check pass for a run nobody looked at."""
        result = prereg.check(_registration())
        assert result.clean  # nothing contradicted, nothing invented


class TestTheRawExport:
    ROWS = [
        ("a", "o1", True, True),
        ("a", "o2", False, False),
        ("b", "o1", True, True),
        ("b", "o3", True, None),   # assigned, never reported
    ]

    def test_every_arm_decision_is_exported_including_unresolved_ones(self):
        """An occasion assigned an arm and never reported IS the attrition
        question; exporting only resolved rows hands over a record with the
        evidence already removed."""
        result = raw_export.export(_assignments(self.ROWS))
        assert result.n_rows == 4
        assert result.n_unresolved == 1
        assert result.n_occasions == 3
        assert result.n_memories == 2

    def test_the_csv_has_a_fixed_header(self):
        result = raw_export.export(_assignments(self.ROWS))
        assert result.csv_text.splitlines()[0] == ",".join(raw_export.COLUMNS)

    def test_arms_are_spelled_out_not_left_as_bare_booleans(self):
        result = raw_export.export(_assignments(self.ROWS))
        assert "injected" in result.csv_text and "withheld" in result.csv_text

    def test_the_digest_identifies_the_data_not_the_file_order(self):
        """Rows come back from a database in whatever order the planner
        chose. A digest over the bytes as written would differ between two
        exports of identical data and would prove nothing."""
        forward = raw_export.digest_of(_assignments(self.ROWS))
        backward = raw_export.digest_of(_assignments(list(reversed(self.ROWS))))
        assert forward == backward

    def test_changing_one_outcome_changes_the_digest(self):
        changed = list(self.ROWS)
        changed[0] = ("a", "o1", True, False)
        assert raw_export.digest_of(_assignments(changed)) != raw_export.digest_of(
            _assignments(self.ROWS)
        )

    def test_dropping_a_row_changes_the_digest(self):
        assert raw_export.digest_of(_assignments(self.ROWS[:-1])) != (
            raw_export.digest_of(_assignments(self.ROWS))
        )

    def test_an_exported_csv_verifies_against_its_own_digest(self):
        result = raw_export.export(_assignments(self.ROWS))
        assert raw_export.verify(result.csv_text, result.digest)

    def test_an_edited_csv_does_not_verify(self):
        result = raw_export.export(_assignments(self.ROWS))
        tampered = result.csv_text.replace("withheld", "injected", 1)
        assert not raw_export.verify(tampered, result.digest)

    def test_a_csv_with_the_wrong_columns_does_not_verify(self):
        result = raw_export.export(_assignments(self.ROWS))
        assert not raw_export.verify("memory,occasion\na,o1\n", result.digest)

    def test_an_empty_export_is_still_well_formed(self):
        result = raw_export.export([])
        assert result.n_rows == 0
        assert raw_export.verify(result.csv_text, result.digest)


class TestAnchoringTheInvoiceToTheEvidence:
    """The chain proves the printed lines were not edited. These bind the
    invoice to the rows it was computed from and the design it was promised
    under, so an issuer cannot hand over a valid signature alongside an
    unrelated export."""

    @staticmethod
    def _ledger():
        from commontrace import experiment

        effects = [experiment.CausalEffect(
            lesson_slug="a", n_injected=200, n_withheld=200, rate_injected=0.8,
            rate_withheld=0.75, effect=0.05, ci_low=0.03, ci_high=0.07,
            p_value=0.001, significant=True, min_detectable_effect=0.02,
            verdict=experiment.VERDICT_HELPS,
        )]
        audit = integrity.audit([
            integrity.Assignment(lesson="a", occasion_id=f"o{i}", injected=i % 2 == 0,
                                 rate=0.5, succeeded=(i % 3 != 0))
            for i in range(400)
        ])
        return value.compute(effects, audit, value_per_occasion=25.0).ledger()

    KEY = b"issuer-key"

    def test_a_signature_covers_the_evidence_digest(self):
        ledger = self._ledger()
        signature = value.sign_ledger(
            ledger, self.KEY, org_id="org", issued_at="t",
            evidence_digest="digest-of-the-real-rows",
        )
        assert value.verify_ledger_signature(
            ledger, signature, self.KEY, org_id="org", issued_at="t",
            evidence_digest="digest-of-the-real-rows",
        )
        assert not value.verify_ledger_signature(
            ledger, signature, self.KEY, org_id="org", issued_at="t",
            evidence_digest="digest-of-some-other-rows",
        )

    def test_a_signature_covers_the_preregistration(self):
        ledger = self._ledger()
        registered = _registration()
        signature = value.sign_ledger(
            ledger, self.KEY, org_id="org", issued_at="t",
            prereg_fingerprint=registered.fingerprint(),
        )
        assert not value.verify_ledger_signature(
            ledger, signature, self.KEY, org_id="org", issued_at="t",
            prereg_fingerprint=_registration(minimum_practical_effect=0.02).fingerprint(),
        )

    def test_an_unanchored_signature_is_distinct_from_an_anchored_one(self):
        """Empty strings are part of the signed payload, so "no evidence was
        attached" cannot be passed off as "evidence was attached"."""
        ledger = self._ledger()
        bare = value.sign_ledger(ledger, self.KEY, org_id="org", issued_at="t")
        anchored = value.sign_ledger(
            ledger, self.KEY, org_id="org", issued_at="t", evidence_digest="d",
        )
        assert bare != anchored
