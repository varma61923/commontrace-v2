"""Retrieval imprecision must not silently become a causal claim.

The product's most valuable output is a causally-measured effect size, and it
was reachable by a path nobody could see from the log: `query --experiment`
logged an assignment for EVERY retrieved lesson, with no record of how well
each one matched. A lesson that scraped into top-k on an incidental word got a
row identical to one that was squarely on topic, and the occasion's outcome was
attributed to both.

Observed running six fleets: one lesson accumulated 246 assignments against
roughly 80 occasions actually about it, and was reported as significantly
HURTING outcomes (-14.5pp, 95% CI [-26.3, -2.7], p=0.018) after
Benjamini-Hochberg correction. The lesson was fine. The retrieval was not, and
nothing in the log could say so.

These tests cover the three checks that make that visible, and the evidence
they need being recorded in the first place.
"""
import json

import pytest

from commontrace import holdout_io, integrity


def _row(lesson, occasion, injected=True, relevance=0.5, floor=0.10,
         scorer="idf-v2", succeeded=True, salt="s"):
    return integrity.Assignment(
        lesson=lesson, occasion_id=occasion, injected=injected, salt=salt,
        relevance=relevance, floor=floor, scorer=scorer, succeeded=succeeded,
    )


class TestTheEvidenceIsRecorded:
    def test_assignments_carry_relevance_rank_and_settings(self, tmp_path):
        root = str(tmp_path)
        (tmp_path / "memory").mkdir()
        holdout_io.assign_and_log(
            root, ["a", "b"], occasion_id="occ-1", rate=0.5, salt="s",
            relevance={"a": 0.62, "b": 0.11}, scorer="idf-v2", floor=0.10,
        )
        records, corrupt = holdout_io.read_log(root)
        assert corrupt == 0
        by_lesson = {r.lesson: r for r in records}
        assert by_lesson["a"].relevance == pytest.approx(0.62)
        assert by_lesson["b"].relevance == pytest.approx(0.11)
        assert by_lesson["a"].rank == 1
        assert by_lesson["b"].rank == 2
        assert by_lesson["a"].scorer == "idf-v2"
        assert by_lesson["a"].floor == pytest.approx(0.10)

    def test_an_older_log_without_the_evidence_still_reads(self, tmp_path):
        """The precedent holdout_io.read_log already sets for `salt` and
        `revision`: backward compatibility for a measurement is not a nicety,
        because the alternative is a fleet's experiment history becoming
        unreadable on upgrade."""
        root = str(tmp_path)
        (tmp_path / "memory").mkdir()
        with open(holdout_io.holdout_log_path(root), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "occasion_id": "old-1", "lesson": "a", "injected": True,
                "rate": 0.1, "salt": "s",
            }) + "\n")
        records, corrupt = holdout_io.read_log(root)
        assert corrupt == 0
        assert records[0].relevance is None
        assert records[0].rank is None
        assert records[0].scorer is None

    def test_a_recorded_zero_is_not_confused_with_nothing_recorded(self, tmp_path):
        root = str(tmp_path)
        (tmp_path / "memory").mkdir()
        holdout_io.assign_and_log(
            root, ["a"], occasion_id="occ-1", rate=0.5, salt="s",
            relevance={"a": 0.0}, scorer="idf-v2", floor=0.0,
        )
        records, _ = holdout_io.read_log(root)
        assert records[0].relevance == 0.0
        assert records[0].relevance is not None


class TestMarginalEligibility:
    def test_the_246_versus_80_case_is_caught(self):
        """The exact failure, reconstructed."""
        rows = [
            _row("polluted", f"real-{i}", injected=(i % 2 == 0), relevance=0.52)
            for i in range(80)
        ] + [
            # Pulled in on an incidental word, just over the floor. The
            # outcomes of these occasions say nothing about this lesson.
            _row("polluted", f"collateral-{i}", injected=(i % 2 == 0), relevance=0.12)
            for i in range(166)
        ]
        finding = integrity.check_marginal_eligibility(rows)
        assert finding.severity == integrity.SEVERITY_WEAKENS
        assert "polluted" in finding.headline
        assert finding.numbers["worst"]["lesson"] == "polluted"
        assert finding.numbers["worst"]["share"] > 0.6

    def test_an_overwhelmingly_marginal_lesson_invalidates(self):
        rows = [_row("noise", f"o-{i}", relevance=0.11) for i in range(90)] + [
            _row("noise", f"good-{i}", relevance=0.55) for i in range(10)
        ]
        finding = integrity.check_marginal_eligibility(rows)
        assert finding.severity == integrity.SEVERITY_INVALIDATES

    def test_solid_matches_pass(self):
        rows = [_row("clean", f"o-{i}", relevance=0.55) for i in range(50)]
        assert integrity.check_marginal_eligibility(rows).severity == integrity.SEVERITY_OK

    def test_unscored_rows_are_skipped_not_assumed(self):
        """An old log must degrade to "cannot assess" rather than to a
        finding there is no evidence for."""
        rows = [
            integrity.Assignment(lesson="a", occasion_id=f"o-{i}", injected=True, salt="s")
            for i in range(50)
        ]
        finding = integrity.check_marginal_eligibility(rows)
        assert finding.severity == integrity.SEVERITY_OK
        assert finding.numbers["n_scored"] == 0
        assert finding.numbers["n_unscored"] == 50


class TestAssignmentConcentration:
    def _peers(self, relevance=0.55, n=80):
        rows = []
        for slug in ("peer_a", "peer_b", "peer_c"):
            rows += [_row(slug, f"{slug}-{i}", relevance=relevance) for i in range(n)]
        return rows

    def test_a_lesson_that_is_both_far_more_frequent_and_weaker_is_flagged(self):
        rows = self._peers() + [
            _row("greedy", f"g-{i}", relevance=0.15) for i in range(246)
        ]
        finding = integrity.check_assignment_concentration(rows)
        assert finding.severity == integrity.SEVERITY_WEAKENS
        assert "greedy" in finding.headline
        assert finding.numbers["flagged"] == ["greedy"]

    def test_a_genuinely_broad_lesson_matching_strongly_is_not_flagged(self):
        """Breadth alone is not a defect. A lesson that applies to many
        occasions AND matches them as well as its peers match theirs is doing
        its job, and flagging it would train people to ignore this check."""
        rows = self._peers() + [
            _row("broad", f"b-{i}", relevance=0.60) for i in range(246)
        ]
        assert integrity.check_assignment_concentration(rows).severity == integrity.SEVERITY_OK

    def test_too_few_lessons_to_compare_is_not_a_finding(self):
        rows = [_row("only", f"o-{i}") for i in range(100)]
        finding = integrity.check_assignment_concentration(rows)
        assert finding.severity == integrity.SEVERITY_OK
        assert finding.numbers["n_lessons"] == 1


class TestScorerDrift:
    def test_one_configuration_throughout_is_fine(self):
        rows = [_row("a", f"o-{i}", scorer="idf-v2", floor=0.10) for i in range(20)]
        assert integrity.check_scorer_drift(rows).severity == integrity.SEVERITY_OK

    def test_a_changed_scorer_invalidates(self):
        """Same reasoning as check_assignment_drift, one level up: that check
        catches a changed randomization, this catches a changed denominator."""
        rows = [_row("a", f"o-{i}", scorer="count-v1") for i in range(10)]
        rows += [_row("a", f"n-{i}", scorer="idf-v2") for i in range(10)]
        finding = integrity.check_scorer_drift(rows)
        assert finding.severity == integrity.SEVERITY_INVALIDATES
        assert "scorer" in finding.headline

    def test_a_changed_floor_invalidates(self):
        rows = [_row("a", f"o-{i}", floor=0.10) for i in range(10)]
        rows += [_row("a", f"n-{i}", floor=0.25) for i in range(10)]
        finding = integrity.check_scorer_drift(rows)
        assert finding.severity == integrity.SEVERITY_INVALIDATES
        assert "floor" in finding.headline

    def test_an_old_log_with_no_settings_recorded_is_not_drift(self):
        rows = [
            integrity.Assignment(lesson="a", occasion_id=f"o-{i}", injected=True, salt="s")
            for i in range(20)
        ]
        assert integrity.check_scorer_drift(rows).severity == integrity.SEVERITY_OK


class TestTheChecksAreWiredIntoTheReport:
    def test_audit_runs_them_and_reflects_them_in_the_verdict(self):
        rows = [_row("noise", f"o-{i}", relevance=0.11, floor=0.10) for i in range(90)]
        rows += [_row("noise", f"g-{i}", relevance=0.55, floor=0.10) for i in range(10)]
        report = integrity.audit(rows)
        checks = {f.check for f in report.findings}
        assert {"marginal_eligibility", "assignment_concentration", "scorer_drift"} <= checks
        assert report.verdict == integrity.VERDICT_COMPROMISED
        # "A compromised experiment produces no number. Not a hedged number
        # -- none." (commontrace/value.py)
        assert not report.readable
