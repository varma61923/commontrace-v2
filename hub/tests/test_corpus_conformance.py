from __future__ import annotations

import json
from pathlib import Path

import pytest

from hub import manage

CORPUS = Path(__file__).resolve().parents[2] / "commons" / "seed" / "substrate-v1.jsonl"


def _records():
    with open(CORPUS, "r", encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if line:
                yield i, json.loads(line)


class TestTheShippedCorpusConforms:
    def test_the_corpus_file_exists_and_is_not_empty(self):
        assert CORPUS.exists(), f"curated corpus missing at {CORPUS}"
        assert sum(1 for _ in _records()) > 0

    def test_every_curated_record_matches_the_trace_schema(self):
        offenders = [
            (i, rec.get("title", "?"), problem)
            for i, rec in _records()
            if (problem := manage._corpus_schema_problem(rec))
        ]
        assert not offenders, f"{len(offenders)} curated record(s) do not conform: {offenders}"

    def test_every_curated_record_carries_provenance(self):
        missing = [
            (i, rec.get("title", "?")) for i, rec in _records() if not rec.get("source")
        ]
        assert not missing, f"curated record(s) with no `source`: {missing}"

    def test_every_review_after_is_parseable(self):
        bad = [
            (i, rec["review_after"])
            for i, rec in _records()
            if rec.get("review_after") and manage._parse_review_after(rec["review_after"]) is None
        ]
        assert not bad, f"unparseable review_after values: {bad}"


class TestTheConformanceCheckItself:
    def test_an_empty_title_is_rejected(self):
        assert manage._corpus_schema_problem(
            {"title": "", "context_text": "c", "solution_text": "s"})

    def test_a_missing_solution_is_rejected(self):
        assert manage._corpus_schema_problem({"title": "t", "context_text": "c"})

    def test_a_wrong_typed_field_is_rejected_not_crashed(self):
        problem = manage._corpus_schema_problem(
            {"title": {"nested": "object"}, "context_text": "c", "solution_text": "s"})
        assert "title must be a string" in problem
        assert "every tag must be a string" in manage._corpus_schema_problem(
            {"title": "t", "context_text": "c", "solution_text": "s", "tags": [1, 2]})

    def test_a_good_record_passes(self):
        assert manage._corpus_schema_problem({
            "title": "Payment webhook delivered more than once",
            "context_text": "the provider re-delivers after a timeout",
            "solution_text": "persist the event id and check it before any side effect",
            "tags": ["webhooks", "idempotency"],
            "agent_type": "code",
        }) == ""

    def test_the_size_check_is_skipped_without_a_config(self):
        huge = {
            "title": "t", "context_text": "x" * 5_000_000,
            "solution_text": "s", "tags": [],
        }
        assert manage._corpus_schema_problem(huge, None) == ""

    def test_the_size_check_applies_when_a_config_is_given(self, config):
        huge = {
            "title": "t", "context_text": "x" * (config.max_text_chars + 1),
            "solution_text": "s", "tags": [],
        }
        problem = manage._corpus_schema_problem(huge, config)
        assert "size limit" in problem


@pytest.mark.asyncio
class TestValidateCorpusCommand:
    async def test_it_accepts_the_shipped_corpus(self, capsys):
        assert await manage.validate_corpus(str(CORPUS)) is True
        assert "conform" in capsys.readouterr().out

    async def test_it_reports_every_problem_at_once(self, tmp_path, capsys):
        corpus = tmp_path / "bad.jsonl"
        corpus.write_text(
            '{"title": "fine", "context_text": "c", "solution_text": "s"}\n'
            '{"title": "", "context_text": "c", "solution_text": "s"}\n'
            "not json at all\n"
            '["not an object"]\n',
            encoding="utf-8",
        )
        assert await manage.validate_corpus(str(corpus)) is False
        err = capsys.readouterr().err
        assert "3 problem(s)" in err
        assert "line 2" in err and "line 3" in err and "line 4" in err

    async def test_a_missing_file_is_an_error_not_a_crash(self, tmp_path, capsys):
        assert await manage.validate_corpus(str(tmp_path / "nope.jsonl")) is False
        assert "cannot read" in capsys.readouterr().err
