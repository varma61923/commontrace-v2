"""`commontrace commons ask` -- the client half of the knowledge-base lookup.

The property that matters most is the same one that matters for `sign`:
the question is MinHashed locally and the TEXT never goes on the wire. A
regression there would silently convert the product's privacy claim into a
false statement, which is worse than a crash.
"""
from __future__ import annotations

import argparse
import json

import pytest

from commontrace import overlap
from commontrace.commands import commons_cmd


class TestQuestionSigning:
    def test_a_question_signs_to_the_agreed_width(self):
        sig = commons_cmd.sign_question("customer charged twice for one order")
        assert len(sig) == commons_cmd.COMMONS_NUM_PERM
        assert all(isinstance(v, int) and not isinstance(v, bool) for v in sig)

    def test_signing_is_deterministic(self):
        """Assignment must be reproducible: the same question asked twice
        must compare identically against the corpus."""
        q = "postgres connection pool exhausted"
        assert commons_cmd.sign_question(q) == commons_cmd.sign_question(q)

    def test_it_uses_the_same_hash_as_stored_failures(self):
        """A question and a trace must land in the same signature space, or
        every similarity score is meaningless."""
        q = "webhook delivered twice"
        assert commons_cmd.sign_question(q) == overlap.minhash(q, commons_cmd.COMMONS_NUM_PERM)

    def test_the_signature_matches_what_the_hub_would_compute(self):
        pytest.importorskip("hub.commons", reason="hub package not importable in this env")
        from hub import commons as hub_commons

        q = "customer charged twice for one order"
        assert commons_cmd.sign_question(q) == hub_commons.signature_for(q, "", [])

    def test_different_questions_do_not_collide(self):
        a = commons_cmd.sign_question("postgres connection pool exhausted")
        b = commons_cmd.sign_question("react hydration mismatch on dates")
        assert a != b


class TestRendering:
    def _result(self, **over):
        base = {
            "n_candidates": 1,
            "n_commons_traces": 46,
            "n_commons_traces_total": 46,
            "corpus_truncated": False,
            "candidates": [{
                "rank": 1,
                "similarity": 0.125,
                "commons_hits": 3,
                "trace": {
                    "title": "Payment webhook delivered more than once",
                    "context_text": "The provider re-delivers after a timeout",
                    "solution_text": "Persist the provider event id",
                    "tags": ["webhooks", "idempotency"],
                    "agent_type": "code",
                    "trust": 0.5,
                },
            }],
            "note": "Ranked CANDIDATES, not coverage.",
        }
        base.update(over)
        return base

    def test_it_renders_the_solution(self):
        out = commons_cmd._render_candidates(self._result(), "charged twice")
        assert "Persist the provider event id" in out
        assert "Payment webhook delivered more than once" in out

    def test_it_shows_corroboration_when_a_trace_has_delivered_hits(self):
        """The Stack-Overflow-shaped signal: this answer has covered other
        fleets' real failures before."""
        out = commons_cmd._render_candidates(self._result(), "q")
        assert "covered 3 other fleet failure(s)" in out

    def test_it_always_carries_the_candidates_not_coverage_note(self):
        out = commons_cmd._render_candidates(self._result(), "q")
        assert "not coverage" in out.lower()

    def test_no_candidates_against_a_populated_corpus_explains_why(self):
        out = commons_cmd._render_candidates(
            self._result(n_candidates=0, candidates=[]), "unrelated question"
        )
        assert "lexical" in out.lower()

    def test_an_empty_commons_says_cold_start_not_no_answer(self):
        """An empty corpus returning nothing is a cold start by construction,
        not evidence the commons cannot help."""
        out = commons_cmd._render_candidates(
            self._result(n_candidates=0, candidates=[], n_commons_traces=0,
                         n_commons_traces_total=0),
            "anything",
        )
        assert "cold start" in out.lower()

    def test_a_truncated_scan_is_disclosed(self):
        out = commons_cmd._render_candidates(
            self._result(corpus_truncated=True, n_commons_traces=20_000,
                         n_commons_traces_total=50_000),
            "q",
        )
        assert "scan limit" in out.lower()

    def test_it_survives_a_candidate_missing_optional_fields(self):
        """Hub payloads are data, not guarantees -- a renderer that raises on
        a missing optional field turns a partial answer into no answer."""
        out = commons_cmd._render_candidates(
            self._result(candidates=[{"rank": 1, "similarity": 0.1, "commons_hits": 0,
                                       "trace": {}}]),
            "q",
        )
        assert "(untitled)" in out


class TestArgWiring:
    def test_ask_is_registered_with_its_flags(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        commons_cmd.add_parser(sub)
        args = parser.parse_args(["commons", "ask", "why does this fail", "--limit", "3"])
        assert args.question == "why does this fail"
        assert args.limit == 3
        assert args.func is commons_cmd.run_ask

    def test_an_empty_question_is_refused_before_any_network_call(self, capsys, monkeypatch):
        def explode(*a, **kw):  # pragma: no cover - must never run
            raise AssertionError("resolved the hub for an empty question")

        monkeypatch.setattr(commons_cmd, "_resolve_hub", explode)
        args = argparse.Namespace(question="   ", limit=None, agent_type="", json=False,
                                  hub_url=None, hub_api_key=None)
        assert commons_cmd.run_ask(args) == 1
        assert "ask what?" in capsys.readouterr().err

    def test_json_mode_emits_parseable_json(self, monkeypatch, capsys):
        payload = {"n_candidates": 0, "candidates": [], "note": "n"}
        monkeypatch.setattr(commons_cmd, "_resolve_hub", lambda a: ("http://h/mcp", "k"))
        monkeypatch.setattr(commons_cmd.hub_client, "commons_search",
                            lambda *a, **kw: payload)
        monkeypatch.setattr(commons_cmd.asyncio, "run", lambda coro: payload)
        args = argparse.Namespace(question="q", limit=None, agent_type="", json=True,
                                  hub_url=None, hub_api_key=None)
        assert commons_cmd.run_ask(args) == 0
        assert json.loads(capsys.readouterr().out) == payload
