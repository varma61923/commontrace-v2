"""Corpus hygiene, without the optional attention extra.

DOCUMENTATION.md §6.4 describes fusion/archive/contradiction detection as
delegated to a companion skill outside this repository, using pairwise
cosine over the optional semantic index. Every one of these tests runs
against the core install alone -- PyYAML, nothing else -- because that is
the whole point: corpus hygiene should not require the attention extra any
more than lexical retrieval does.

`consolidate.build_report` composes three signals, two of them owned by
other modules and tested there: fusion is `commontrace/redundancy.py`
(tests/test_redundancy.py), contradiction is the pre-existing
`commontrace/reliability.py:find_contradictions` (tests/test_reliability.py).
What belongs here is the COMPOSITION -- only active lessons feed all three,
`never_hit` is a plain schema-only signal, `is_clean`/`--strict` reads the
right severity -- and the CLI surface end to end.
"""
from __future__ import annotations

import json

import pytest

from commontrace import consolidate, templates
from commontrace.cli import main


def _lesson(slug, description, applies_when, body, *, status="active",
            uses=0, last_hit="NEVER", do_not_apply_when="n/a"):
    return {
        "name": slug, "status": status, "description": description,
        "applies_when": applies_when, "do_not_apply_when": do_not_apply_when,
        "tags": [], "domain": "other", "uses": uses, "last_hit": last_hit,
        templates.BODY_KEY: body,
    }


SAME_RULE = "Never retry a payment without an idempotency key on the write path."


class TestNeverHit:
    def test_zero_uses_and_never_hit_is_flagged(self):
        assert consolidate.never_hit({"uses": 0, "last_hit": "NEVER"})

    def test_any_use_is_not_flagged(self):
        assert not consolidate.never_hit({"uses": 1, "last_hit": "2026-01-01"})

    def test_a_dated_last_hit_with_zero_uses_is_not_flagged(self):
        """Internally inconsistent, but not the unambiguous "never once
        fired" signal this function exists to report -- report only what is
        certain."""
        assert not consolidate.never_hit({"uses": 0, "last_hit": "2026-01-01"})

    def test_a_missing_last_hit_is_not_flagged(self):
        """`last_hit` is schema-required (protocol/schemas/lesson.schema.json),
        so a real lesson always has it; a dict missing it entirely is a
        malformed record, not a legitimate "never hit" one, and the
        conservative answer is not to flag it -- same posture
        `dosage.is_core` takes for a missing `core` field."""
        assert not consolidate.never_hit({"uses": 0})

    def test_a_null_uses_behaves_like_zero(self):
        """`uses: null` in hand-edited YAML is an empty field, not a
        malformed one -- equivalent to `uses: 0`."""
        assert consolidate.never_hit({"uses": None, "last_hit": "NEVER"})

    def test_a_non_numeric_uses_field_does_not_crash(self):
        """Same tolerant posture as dosage.is_core: a hand-edited YAML file
        can put anything in `uses`, and a malformed value must not crash a
        corpus-wide report over one bad file."""
        assert not consolidate.never_hit({"uses": "not-a-number", "last_hit": "NEVER"})


class TestBuildReport:
    def test_only_active_lessons_are_considered_for_fusion(self):
        """A candidate still in review is not yet competing for a retrieval
        slot -- same reasoning the write-time duplicate gate
        (lesson_cmd.py:_active_lesson_texts) documents."""
        lessons = [
            _lesson("first", "Payment webhook delivered more than once.",
                    "A webhook is retried after a timeout.", SAME_RULE),
            _lesson("second", "Duplicate charge from a retried webhook.",
                    "A webhook is retried after a timeout.", SAME_RULE, status="review"),
        ]
        report = consolidate.build_report(lessons)
        assert report.fuse == ()
        assert report.n_active == 1

    def test_fusion_candidates_are_found_among_active_lessons(self):
        lessons = [
            _lesson("first", "Payment webhook delivered more than once.",
                    "A webhook is retried after a timeout.", SAME_RULE),
            _lesson("second", "Duplicate charge from a retried webhook.",
                    "A webhook is retried after a timeout.", SAME_RULE),
        ]
        report = consolidate.build_report(lessons)
        assert len(report.fuse) == 1
        assert {report.fuse[0].a, report.fuse[0].b} == {"first", "second"}

    def test_distinct_lessons_produce_no_fusion_candidate(self):
        lessons = [
            _lesson("payments", "Payment webhook delivered more than once.",
                    "A webhook is retried after a timeout.", SAME_RULE),
            _lesson("security", "Credentials rotated too infrequently.",
                    "A service account key is older than the rotation policy.",
                    "Rotate service account credentials every ninety days."),
        ]
        report = consolidate.build_report(lessons)
        assert report.fuse == ()

    def test_never_hit_active_lessons_are_archive_candidates(self):
        lessons = [
            _lesson("used", "x", "y", "z", uses=5, last_hit="2026-01-01"),
            _lesson("unused", "x", "y", "z", uses=0, last_hit="NEVER"),
        ]
        report = consolidate.build_report(lessons)
        assert report.archive == ("unused",)

    def test_review_lessons_are_never_archive_candidates(self):
        """Archiving is about a lesson that IS competing and never wins.
        One still in review has never competed at all."""
        lessons = [_lesson("draft", "x", "y", "z", status="review", uses=0, last_hit="NEVER")]
        report = consolidate.build_report(lessons)
        assert report.archive == ()

    def test_contradictions_are_delegated_to_reliability_py(self):
        """Not reinvented -- the existing, independently tested
        find_contradictions is what runs here."""
        lessons = [
            _lesson("always", "Always retry a failed webhook delivery",
                    "a webhook delivery fails transiently", "body"),
            _lesson("never", "Never retry a failed webhook delivery",
                    "a webhook delivery fails transiently", "body"),
        ]
        report = consolidate.build_report(lessons)
        assert len(report.contradict) == 1
        assert {report.contradict[0].slug_a, report.contradict[0].slug_b} == {"always", "never"}

    def test_an_empty_corpus_reports_cleanly(self):
        report = consolidate.build_report([])
        assert report.n_active == 0
        assert report.is_clean


class TestIsClean:
    def test_clean_when_nothing_is_found(self):
        assert consolidate.build_report([]).is_clean

    def test_any_fusion_candidate_is_not_clean(self):
        lessons = [
            _lesson("first", "x", "y", SAME_RULE),
            _lesson("second", "x", "y", SAME_RULE),
        ]
        assert not consolidate.build_report(lessons).is_clean

    def test_any_archive_candidate_is_not_clean(self):
        lessons = [_lesson("unused", "x", "y", "z", uses=0, last_hit="NEVER")]
        assert not consolidate.build_report(lessons).is_clean

    def test_only_high_severity_contradictions_are_not_clean(self):
        """Matches `commontrace reliability --strict`'s own precedent: a
        review-severity (lexical-only) contradiction is worth surfacing but
        not worth failing a build over. Constructed directly rather than
        through `build_report`, to isolate `is_clean`'s own logic from
        whatever fusion/archive signals a real lesson pair would also
        trigger."""
        from commontrace import reliability as rel

        review_contradiction = rel.Contradiction(
            slug_a="always", slug_b="never", activation_overlap=0.9,
            polarity_a=1.0, polarity_b=-1.0, lift_a=None, lift_b=None,
            signals=["opposite prescriptive/prohibitive polarity"], severity="review",
        )
        report = consolidate.ConsolidationReport(
            n_active=2, contradict=(review_contradiction,),
        )
        assert not report.high_severity_contradictions
        assert report.is_clean

    def test_a_high_severity_contradiction_is_not_clean(self):
        from commontrace import reliability as rel

        high_contradiction = rel.Contradiction(
            slug_a="a", slug_b="b", activation_overlap=0.9,
            polarity_a=1.0, polarity_b=-1.0, lift_a=0.3, lift_b=-0.3,
            signals=["opposite measured effect on task success"], severity="high",
        )
        report = consolidate.ConsolidationReport(n_active=2, contradict=(high_contradiction,))
        assert report.high_severity_contradictions
        assert not report.is_clean


class TestRender:
    def test_renders_without_data(self):
        out = consolidate.render(consolidate.build_report([]))
        assert "Consolidation Report" in out
        assert "No near-duplicate active lessons detected." in out
        assert "Every active lesson has been retrieved at least once." in out
        assert "No contradictions detected" in out

    def test_states_that_nothing_is_changed_automatically(self):
        out = consolidate.render(consolidate.build_report([]))
        assert "nothing is merged, archived, or edited automatically" in out.lower()

    def test_reports_a_fusion_pair_with_its_similarity(self):
        lessons = [
            _lesson("first", "x", "y", SAME_RULE),
            _lesson("second", "x", "y", SAME_RULE),
        ]
        out = consolidate.render(consolidate.build_report(lessons))
        assert "`first`" in out and "`second`" in out
        assert "100%" in out


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write_lesson(store, slug, description, applies_when, body, **extra):
    from commontrace import lesson_io, paths

    root = str(store)
    path = f"{paths.lessons_dir(root)}/lesson_{slug}.md"
    fm = {
        "name": slug, "status": "active", "description": description,
        "applies_when": applies_when, "do_not_apply_when": "n/a",
        "importance": 3, "importance_rationale": "fixture", "tags": [],
        "agent_type": "code", "domain": "other", "uses": 0, "last_hit": "NEVER",
    }
    fm.update(extra)
    lesson_io.write_lesson(path, fm, body, root=root, actor="test", reason="fixture")


class TestConsolidateCLI:
    def test_no_active_lessons_explains_rather_than_failing(self, store, capsys):
        main(["init", "--agent-type", "code", "--dest", str(store)])
        capsys.readouterr()
        assert main(["consolidate", "--dest", str(store)]) == 0
        assert "no active lessons" in capsys.readouterr().out

    def test_finds_a_fusion_candidate_end_to_end(self, store, capsys):
        main(["init", "--agent-type", "code", "--dest", str(store)])
        _write_lesson(store, "first", "Payment webhook delivered more than once.",
                      "A webhook is retried after a timeout.", SAME_RULE)
        _write_lesson(store, "second", "Duplicate charge from a retried webhook.",
                      "A webhook is retried after a timeout.", SAME_RULE)
        capsys.readouterr()
        assert main(["consolidate", "--dest", str(store)]) == 0
        out = capsys.readouterr().out
        assert "`first`" in out and "`second`" in out

    def test_json_output_is_well_formed(self, store, capsys):
        main(["init", "--agent-type", "code", "--dest", str(store)])
        _write_lesson(store, "first", "Payment webhook delivered more than once.",
                      "A webhook is retried after a timeout.", SAME_RULE)
        _write_lesson(store, "second", "Duplicate charge from a retried webhook.",
                      "A webhook is retried after a timeout.", SAME_RULE)
        capsys.readouterr()
        assert main(["consolidate", "--dest", str(store), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["n_active"] == 2
        assert len(payload["fuse"]) == 1
        assert {payload["fuse"][0]["a"], payload["fuse"][0]["b"]} == {"first", "second"}

    def test_strict_fails_the_build_on_a_fusion_candidate(self, store, capsys):
        main(["init", "--agent-type", "code", "--dest", str(store)])
        _write_lesson(store, "first", "Payment webhook delivered more than once.",
                      "A webhook is retried after a timeout.", SAME_RULE)
        _write_lesson(store, "second", "Duplicate charge from a retried webhook.",
                      "A webhook is retried after a timeout.", SAME_RULE)
        capsys.readouterr()
        assert main(["consolidate", "--dest", str(store), "--strict"]) == 1
        assert "fusion candidate" in capsys.readouterr().err

    def test_strict_passes_a_clean_corpus(self, store, capsys):
        main(["init", "--agent-type", "code", "--dest", str(store)])
        _write_lesson(store, "payments", "Payment webhook delivered more than once.",
                      "A webhook is retried after a timeout.", SAME_RULE,
                      uses=1, last_hit="2026-01-01")
        capsys.readouterr()
        assert main(["consolidate", "--dest", str(store), "--strict"]) == 0

    def test_custom_redundancy_threshold_is_forwarded(self, store, capsys):
        """A store's own retrieval redundancy_threshold and this report's
        threshold are independently configurable -- both read the same
        module default, but nothing forces a caller to use it."""
        main(["init", "--agent-type", "code", "--dest", str(store)])
        _write_lesson(store, "payments", "Payment webhook delivered more than once.",
                      "A webhook is retried after a timeout.", SAME_RULE)
        _write_lesson(store, "security", "Credentials rotated too infrequently.",
                      "A service account key is older than the rotation policy.",
                      "Rotate service account credentials every ninety days.")
        capsys.readouterr()
        # A near-zero threshold makes even unrelated lessons "fuse"
        # candidates -- proves the flag actually reaches build_report.
        assert main([
            "consolidate", "--dest", str(store), "--redundancy-threshold", "0.01",
        ]) == 0
        out = capsys.readouterr().out
        assert "`payments`" in out and "`security`" in out
