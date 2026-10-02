from __future__ import annotations

import math
import os

import pytest

from commontrace import compaction
from commontrace.compaction import summarize_messages


def _transcript():
    # Each evicted message carries a unique single-token fact marker.
    return [
        {"role": "user", "content": "Kickoff for Project Phoenix. We use Postgres 15 with PgBouncer for pooling."},
        {"role": "assistant", "content": "We decided to migrate the billing service first; the Zebrafish queue handles retries."},
        {"role": "user", "content": "TODO Quintessa dashboard is still pending; we need to confirm OAuth scopes with the identity provider."},
        {"role": "assistant", "content": "Confirmed the Redis cache TTL is 300 seconds. Next step is load testing with the Kestrel harness."},
        {"role": "user", "content": "Tail message one about the release notes."},
        {"role": "assistant", "content": "Tail message two about the changelog."},
    ]


def _no_llm(monkeypatch):
    monkeypatch.delenv("COMMONTRACE_LLM_API_KEY", raising=False)
    monkeypatch.delenv("COMMONTRACE_LLM_PROVIDER", raising=False)


class TestShape:
    def test_seven_sections_with_expected_keys(self, monkeypatch):
        _no_llm(monkeypatch)
        result = summarize_messages(_transcript(), keep_pct=0.5)
        assert set(result.keys()) == set(compaction.SUMMARY_KEYS)
        assert len(compaction.SUMMARY_KEYS) == 7

    def test_hyphenated_aliases_read_same_sections(self, monkeypatch):
        _no_llm(monkeypatch)
        result = summarize_messages(_transcript(), keep_pct=0.5)
        assert result["what-happened"] == result["what_happened"]
        assert result["key-decisions"] == result["key_decisions"]
        assert result["open-state"] == result["open_state"]
        assert "lookup-hints" in result

    def test_section_types(self, monkeypatch):
        _no_llm(monkeypatch)
        result = summarize_messages(_transcript(), keep_pct=0.5)
        assert isinstance(result["what_happened"], str)
        assert isinstance(result["key_decisions"], list)
        assert isinstance(result["open_state"], list)
        assert isinstance(result["lookup_hints"], list)
        assert isinstance(result["kept_messages"], list)
        assert isinstance(result["evicted_count"], int)
        assert result["method"] in ("llm", "extractive")


class TestWindowing:
    def test_keep_pct_keeps_exact_tail(self, monkeypatch):
        _no_llm(monkeypatch)
        messages = [{"role": "user", "content": f"message number {i}"} for i in range(10)]
        result = summarize_messages(messages, mode="sliding_window", keep_pct=0.3)
        assert result["kept_messages"] == messages[-3:]
        assert result["evicted_count"] == 7

    def test_keep_pct_zero_evicts_everything(self, monkeypatch):
        _no_llm(monkeypatch)
        messages = _transcript()
        result = summarize_messages(messages, keep_pct=0.0)
        assert result["kept_messages"] == []
        assert result["evicted_count"] == len(messages)

    def test_keep_pct_one_keeps_everything(self, monkeypatch):
        _no_llm(monkeypatch)
        messages = _transcript()
        result = summarize_messages(messages, keep_pct=1.0)
        assert result["kept_messages"] == messages
        assert result["evicted_count"] == 0
        assert result["lookup_hints"] == []
        assert result["what_happened"] == ""

    def test_keep_pct_rounds_up_to_cover_partial_messages(self, monkeypatch):
        _no_llm(monkeypatch)
        messages = [{"role": "user", "content": f"m{i}"} for i in range(7)]
        result = summarize_messages(messages, keep_pct=0.3)
        want_keep = int(math.ceil(7 * 0.3))
        assert len(result["kept_messages"]) == want_keep
        assert result["kept_messages"] == messages[-want_keep:]

    def test_empty_transcript(self, monkeypatch):
        _no_llm(monkeypatch)
        result = summarize_messages([], keep_pct=0.3)
        assert result["kept_messages"] == []
        assert result["evicted_count"] == 0
        assert result["lookup_hints"] == []
        assert result["method"] == "extractive"

    def test_hyphenated_mode_spelling_accepted(self, monkeypatch):
        _no_llm(monkeypatch)
        result = summarize_messages(_transcript(), mode="sliding-window", keep_pct=0.5)
        assert result["evicted_count"] == 3

    def test_bad_mode_rejected(self, monkeypatch):
        _no_llm(monkeypatch)
        with pytest.raises(ValueError):
            summarize_messages(_transcript(), mode="random", keep_pct=0.5)

    @pytest.mark.parametrize("bad", [-0.1, 1.5, "lots"])
    def test_bad_keep_pct_rejected(self, monkeypatch, bad):
        _no_llm(monkeypatch)
        with pytest.raises(ValueError):
            summarize_messages(_transcript(), keep_pct=bad)

    def test_string_and_turn_like_messages_normalized(self, monkeypatch):
        _no_llm(monkeypatch)

        class Turn:
            def __init__(self, role, content):
                self.role = role
                self.content = content

        messages = ["plain string message", Turn("assistant", "turn object reply")]
        result = summarize_messages(messages, keep_pct=1.0)
        assert result["kept_messages"] == [
            {"role": "user", "content": "plain string message"},
            {"role": "assistant", "content": "turn object reply"},
        ]


class TestEvictedFactCoverage:
    def test_every_evicted_fact_in_summary_or_hints(self, monkeypatch):
        _no_llm(monkeypatch)
        messages = _transcript()
        result = summarize_messages(messages, keep_pct=1 / 3, llm_enabled=False)
        assert result["method"] == "extractive"
        # keep_pct=1/3 on 6 messages keeps the 2 tail messages, evicts the first 4.
        assert result["evicted_count"] == 4
        combined = " ".join([
            result["what_happened"],
            *result["key_decisions"],
            *result["open_state"],
        ]).lower()
        hints = set(result["lookup_hints"])
        for fact in ("phoenix", "postgres", "pgbouncer", "zebrafish",
                     "quintessa", "oauth", "redis", "kestrel"):
            assert fact in combined or fact in hints, f"evicted fact {fact!r} lost"

    def test_extractive_captures_decisions_and_open_state(self, monkeypatch):
        _no_llm(monkeypatch)
        result = summarize_messages(_transcript(), keep_pct=1 / 3, llm_enabled=False)
        assert any("decided" in d.lower() for d in result["key_decisions"])
        assert any("quintessa" in o.lower() for o in result["open_state"])

    def test_extractive_covers_head_and_tail_sentences(self, monkeypatch):
        _no_llm(monkeypatch)
        result = summarize_messages(_transcript(), keep_pct=1 / 3, llm_enabled=False)
        assert "Project Phoenix" in result["what_happened"]
        assert "Kestrel" in result["what_happened"]


class TestFallback:
    def test_llm_unavailable_uses_extractive(self, monkeypatch):
        from commontrace import llm as llm_module

        def _boom():
            raise llm_module.LLMUnavailable("no key in test")

        monkeypatch.setattr(llm_module, "load_config", _boom)
        result = summarize_messages(_transcript(), keep_pct=0.5)
        assert result["method"] == "extractive"
        assert result["what_happened"]
        assert result["lookup_hints"]

    def test_llm_enabled_false_skips_llm(self, monkeypatch):
        _no_llm(monkeypatch)
        monkeypatch.setattr(compaction, "_llm_summarize", lambda texts: (_ for _ in ()).throw(AssertionError("must not call LLM")))
        result = summarize_messages(_transcript(), keep_pct=0.5, llm_enabled=False)
        assert result["method"] == "extractive"

    def test_garbage_llm_reply_falls_back(self, monkeypatch):
        from commontrace import llm as llm_module

        monkeypatch.setattr(llm_module, "load_config",
                            lambda: llm_module.Config(provider="anthropic", model="m", api_key="k"))
        monkeypatch.setattr(llm_module, "_call_anthropic", lambda cfg, prompt: ("not json at all {{{", {}))
        result = summarize_messages(_transcript(), keep_pct=0.5)
        assert result["method"] == "extractive"
        assert result["what_happened"]


class TestLLMPath:
    def test_llm_summary_used_when_available(self, monkeypatch):
        _no_llm(monkeypatch)
        canned = {
            "what_happened": "Team kicked off Phoenix with Postgres pooling.",
            "key_decisions": ["Migrate billing first."],
            "open_state": ["Quintessa dashboard pending."],
        }
        monkeypatch.setattr(compaction, "_llm_summarize", lambda texts: dict(canned))
        result = summarize_messages(_transcript(), keep_pct=0.5)
        assert result["method"] == "llm"
        assert result["what_happened"] == canned["what_happened"]
        assert result["key_decisions"] == canned["key_decisions"]
        assert result["open_state"] == canned["open_state"]
        # Hints stay extractive-derived so evicted facts remain recoverable.
        assert "phoenix" in set(result["lookup_hints"])


class TestLookupHints:
    def test_hints_consumable_by_entity_index(self, monkeypatch):
        _no_llm(monkeypatch)
        from commontrace import retrieval

        result = summarize_messages(_transcript(), keep_pct=0.5, llm_enabled=False)
        hints = result["lookup_hints"]
        assert hints
        assert hints == sorted(set(hints))
        lessons = [("p", {
            "name": "x", "description": "d", "applies_when": "a",
            "tags": hints[:5], "domain": "testing",
        })]
        index = retrieval.build_entity_index(lessons)
        assert retrieval._entity_overlap(" ".join(hints), 0, lessons, index) > 0

    def test_hints_round_trip_through_query_tokenizer(self, monkeypatch):
        _no_llm(monkeypatch)
        from commontrace import retrieval

        result = summarize_messages(_transcript(), keep_pct=1 / 3, llm_enabled=False)
        hints = result["lookup_hints"]
        requiered = retrieval._entity_tokens_for_query(" ".join(hints))
        assert set(hints) <= requiered


class TestPurity:
    def test_module_does_not_touch_agent_loop(self):
        path = os.path.join(os.path.dirname(compaction.__file__), "compaction.py")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        assert "import agent_loop" not in source
        assert "from commontrace.agent_loop" not in source
        assert "from .agent_loop" not in source
        assert "commontrace.agent_loop" not in source

    def test_render_markdown_has_seven_sections(self, monkeypatch):
        _no_llm(monkeypatch)
        result = summarize_messages(_transcript(), keep_pct=0.5)
        rendered = compaction.render_markdown(result)
        for heading in ("What Happened", "Key Decisions", "Open State",
                        "Lookup Hints", "Kept Messages", "Evicted Count", "Method"):
            assert f"## {heading}" in rendered
