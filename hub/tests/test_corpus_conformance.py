"""The curated Knowledge Base corpus is held to the same schema as every
customer trace.

It was not. `contribute_trace` runs `validate_trace` (the protocol schema
in protocol/schemas/trace.schema.json) and `validate_size` on every trace
any organisation contributes. `commons_seed` -- the loader for the
operator's own curated corpus -- checked that `title` and `solution_text`
were non-empty and stopped there.

That is the wrong way round. A customer's trace is read by that customer;
the curated corpus is read by EVERY organisation and quoted back to them
as substrate knowledge. It is the one body of content in this product that
crosses every tenant boundary, and it was the one held to the weaker
standard.

So: one definition of a well-formed trace, applied to both. The test that
matters most here is `TestTheShippedCorpusConforms` -- it validates the
actual file this repository ships, so the invariant cannot quietly lapse
the next time someone adds an entry by hand.
"""
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
    """Against the real file, not a fixture. If someone hand-adds an entry
    that does not match the schema, this is what says so."""

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
        """`source` becomes the entry's `shared_rationale`, which is the
        only thing telling an operator months later WHY this claim is in a
        corpus every customer reads. Not schema-required, but a curated
        corpus without it cannot be reviewed, only trusted."""
        missing = [
            (i, rec.get("title", "?")) for i, rec in _records() if not rec.get("source")
        ]
        assert not missing, f"curated record(s) with no `source`: {missing}"

    def test_every_review_after_is_parseable(self):
        """An unparseable horizon is a skipped line at load time -- the
        entry silently never enters the corpus at all."""
        bad = [
            (i, rec["review_after"])
            for i, rec in _records()
            if rec.get("review_after") and manage._parse_review_after(rec["review_after"]) is None
        ]
        assert not bad, f"unparseable review_after values: {bad}"


class TestTheConformanceCheckItself:
    """A check that passes everything is not a check."""

    def test_an_empty_title_is_rejected(self):
        assert manage._corpus_schema_problem(
            {"title": "", "context_text": "c", "solution_text": "s"})

    def test_a_missing_solution_is_rejected(self):
        assert manage._corpus_schema_problem({"title": "t", "context_text": "c"})

    def test_a_wrong_typed_field_is_rejected_not_crashed(self):
        """A hand-edited corpus produces type errors as readily as missing
        ones, and the loader must report them rather than raise."""
        problem = manage._corpus_schema_problem(
            {"title": {"nested": "object"}, "context_text": "c", "solution_text": "s"})
        # Specifically NOT stringified into a plausible-looking title.
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
        """`validate-corpus` has to run in CI, where there is no Hub and no
        database URL -- so a missing config degrades to schema-only rather
        than refusing to check anything."""
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
        """One problem per run would mean fixing a corpus is N round trips;
        the point of a pre-flight is to see the whole list."""
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
