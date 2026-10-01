"""commontrace/llm.py: strict-JSON LLM drafting, and the refusal paths that
matter more than the happy path -- a draft this module accepts is one that
skips straight past `distill`'s own "proposing the conclusion is not honest"
scaffold, so what it refuses is the point.
"""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from commontrace import llm


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen(payload: dict):
    def _opener(request, timeout=None):
        return FakeResponse(json.dumps(payload).encode("utf-8"))
    return _opener


class TestLoadConfig:
    def test_no_api_key_is_unavailable(self, monkeypatch):
        monkeypatch.delenv("COMMONTRACE_LLM_API_KEY", raising=False)
        with pytest.raises(llm.LLMUnavailable, match="COMMONTRACE_LLM_API_KEY"):
            llm.load_config()

    def test_defaults_to_anthropic_and_the_default_model(self, monkeypatch):
        monkeypatch.setenv("COMMONTRACE_LLM_API_KEY", "k")
        monkeypatch.delenv("COMMONTRACE_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("COMMONTRACE_LLM_MODEL", raising=False)
        config = llm.load_config()
        assert config.provider == "anthropic"
        assert config.model == llm.DEFAULT_MODEL

    def test_unsupported_provider_is_unavailable(self, monkeypatch):
        monkeypatch.setenv("COMMONTRACE_LLM_API_KEY", "k")
        monkeypatch.setenv("COMMONTRACE_LLM_PROVIDER", "bedrock")
        with pytest.raises(llm.LLMUnavailable, match="not supported"):
            llm.load_config()

    def test_openai_compatible_without_base_url_is_unavailable(self, monkeypatch):
        monkeypatch.setenv("COMMONTRACE_LLM_API_KEY", "k")
        monkeypatch.setenv("COMMONTRACE_LLM_PROVIDER", "openai-compatible")
        monkeypatch.delenv("COMMONTRACE_LLM_BASE_URL", raising=False)
        with pytest.raises(llm.LLMUnavailable, match="COMMONTRACE_LLM_BASE_URL"):
            llm.load_config()

    @pytest.mark.parametrize("bad", ["file:///etc/passwd", "ftp://host/x", "localhost:11434/v1", "http://"])
    def test_a_non_http_base_url_is_refused(self, monkeypatch, bad):
        monkeypatch.setenv("COMMONTRACE_LLM_API_KEY", "k")
        monkeypatch.setenv("COMMONTRACE_LLM_PROVIDER", "openai-compatible")
        monkeypatch.setenv("COMMONTRACE_LLM_BASE_URL", bad)
        with pytest.raises(llm.LLMUnavailable, match="http"):
            llm.load_config()

    def test_a_directly_built_config_cannot_reach_a_file_url(self, monkeypatch):
        def must_not_open(request, timeout=None):
            raise AssertionError("urlopen must not be reached for a file:// URL")

        monkeypatch.setattr(urllib.request, "urlopen", must_not_open)
        config = llm.Config(provider="openai-compatible", model="m", api_key="k", base_url="file:///etc")
        with pytest.raises(llm.LLMUnavailable, match="non-http"):
            llm.draft("prompt", config=config)

    def test_openai_compatible_with_base_url_is_valid(self, monkeypatch):
        monkeypatch.setenv("COMMONTRACE_LLM_API_KEY", "k")
        monkeypatch.setenv("COMMONTRACE_LLM_PROVIDER", "openai-compatible")
        monkeypatch.setenv("COMMONTRACE_LLM_BASE_URL", "http://localhost:11434/v1")
        config = llm.load_config()
        assert config.base_url == "http://localhost:11434/v1"


_GOOD = {
    "rule": "Always set an idempotency key on webhook handlers.",
    "applies_when": "handling an inbound webhook that may be retried.",
    "do_not_apply_when": "the handler is naturally idempotent already.",
    "evidence": ["occ-1", "occ-2"],
}


def _config(**kwargs):
    defaults = dict(provider="anthropic", model="claude-sonnet-5", api_key="k")
    defaults.update(kwargs)
    return llm.Config(**defaults)


class TestDraftParsing:
    def test_a_complete_reply_produces_a_draft(self, monkeypatch):
        monkeypatch.setattr(llm, "_call_anthropic", lambda cfg, prompt: (json.dumps(_GOOD), {
            "input_tokens": 100, "output_tokens": 40,
        }))
        result = llm.draft("prompt", allowed_evidence_ids={"occ-1", "occ-2"}, config=_config())
        assert result.rule == _GOOD["rule"]
        assert result.applies_when == _GOOD["applies_when"]
        assert result.evidence == ["occ-1", "occ-2"]
        assert result.unverifiable_evidence == []
        assert result.provenance["usage"] == {"input_tokens": 100, "output_tokens": 40, "estimated": False}
        assert result.provenance["model"] == "claude-sonnet-5"

    def test_a_fenced_code_block_is_unwrapped(self, monkeypatch):
        fenced = "```json\n" + json.dumps(_GOOD) + "\n```"
        monkeypatch.setattr(llm, "_call_anthropic", lambda cfg, prompt: (fenced, {}))
        result = llm.draft("prompt", config=_config())
        assert result.rule == _GOOD["rule"]

    def test_missing_usage_is_estimated_and_labelled(self, monkeypatch):
        monkeypatch.setattr(llm, "_call_anthropic", lambda cfg, prompt: (json.dumps(_GOOD), {}))
        result = llm.draft("a prompt of some length", config=_config())
        assert result.provenance["usage"]["estimated"] is True
        assert result.provenance["usage"]["input_tokens"] > 0

    def test_prompt_hash_is_reproducible(self, monkeypatch):
        monkeypatch.setattr(llm, "_call_anthropic", lambda cfg, prompt: (json.dumps(_GOOD), {}))
        r1 = llm.draft("same prompt", config=_config())
        r2 = llm.draft("same prompt", config=_config())
        assert r1.provenance["prompt_sha256"] == r2.provenance["prompt_sha256"]

    def test_non_json_reply_is_rejected(self, monkeypatch):
        monkeypatch.setattr(llm, "_call_anthropic", lambda cfg, prompt: ("not json at all", {}))
        with pytest.raises(llm.LLMDraftRejected, match="not valid JSON"):
            llm.draft("prompt", config=_config())

    def test_a_json_array_top_level_is_rejected(self, monkeypatch):
        monkeypatch.setattr(llm, "_call_anthropic", lambda cfg, prompt: (json.dumps([1, 2]), {}))
        with pytest.raises(llm.LLMDraftRejected, match="not a JSON object"):
            llm.draft("prompt", config=_config())

    def test_a_missing_required_key_is_rejected(self, monkeypatch):
        incomplete = dict(_GOOD)
        del incomplete["do_not_apply_when"]
        monkeypatch.setattr(llm, "_call_anthropic", lambda cfg, prompt: (json.dumps(incomplete), {}))
        with pytest.raises(llm.LLMDraftRejected, match="do_not_apply_when"):
            llm.draft("prompt", config=_config())

    def test_an_empty_rule_is_rejected(self, monkeypatch):
        blank = dict(_GOOD, rule="   ")
        monkeypatch.setattr(llm, "_call_anthropic", lambda cfg, prompt: (json.dumps(blank), {}))
        with pytest.raises(llm.LLMDraftRejected, match="'rule'"):
            llm.draft("prompt", config=_config())

    def test_evidence_not_a_list_is_rejected(self, monkeypatch):
        bad = dict(_GOOD, evidence="occ-1")
        monkeypatch.setattr(llm, "_call_anthropic", lambda cfg, prompt: (json.dumps(bad), {}))
        with pytest.raises(llm.LLMDraftRejected, match="'evidence' must be a JSON array"):
            llm.draft("prompt", config=_config())


class TestEvidenceVerification:
    def test_partially_verifiable_evidence_is_split_not_dropped_silently(self, monkeypatch):
        cited = dict(_GOOD, evidence=["occ-1", "occ-made-up"])
        monkeypatch.setattr(llm, "_call_anthropic", lambda cfg, prompt: (json.dumps(cited), {}))
        result = llm.draft("prompt", allowed_evidence_ids={"occ-1", "occ-2"}, config=_config())
        assert result.evidence == ["occ-1"]
        assert result.unverifiable_evidence == ["occ-made-up"]

    def test_wholly_fabricated_evidence_is_refused(self, monkeypatch):
        cited = dict(_GOOD, evidence=["occ-fabricated"])
        monkeypatch.setattr(llm, "_call_anthropic", lambda cfg, prompt: (json.dumps(cited), {}))
        with pytest.raises(llm.LLMDraftRejected, match="nothing verifiable"):
            llm.draft("prompt", allowed_evidence_ids={"occ-1", "occ-2"}, config=_config())

    def test_no_allowed_set_means_no_verification_is_attempted(self, monkeypatch):
        cited = dict(_GOOD, evidence=["whatever"])
        monkeypatch.setattr(llm, "_call_anthropic", lambda cfg, prompt: (json.dumps(cited), {}))
        result = llm.draft("prompt", config=_config())
        assert result.evidence == ["whatever"]
        assert result.unverifiable_evidence == []


class TestHttpLayer:
    def test_anthropic_request_shape(self, monkeypatch):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
            captured["body"] = json.loads(request.data)
            return FakeResponse(json.dumps({
                "content": [{"type": "text", "text": json.dumps(_GOOD)}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }).encode("utf-8"))

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        result = llm.draft("do the thing", config=_config(model="claude-sonnet-5"))
        assert captured["url"] == llm._ANTHROPIC_URL
        assert captured["headers"]["x-api-key"] == "k"
        assert captured["headers"]["anthropic-version"] == llm._ANTHROPIC_VERSION
        assert captured["body"]["model"] == "claude-sonnet-5"
        assert captured["body"]["messages"] == [{"role": "user", "content": "do the thing"}]
        assert result.provenance["usage"] == {"input_tokens": 10, "output_tokens": 5, "estimated": False}

    def test_openai_compatible_request_shape(self, monkeypatch):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
            return FakeResponse(json.dumps({
                "choices": [{"message": {"content": json.dumps(_GOOD)}}],
            }).encode("utf-8"))

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        config = _config(provider="openai-compatible", base_url="http://localhost:11434/v1")
        llm.draft("do the thing", config=config)
        assert captured["url"] == "http://localhost:11434/v1/chat/completions"
        assert captured["headers"]["authorization"] == "Bearer k"

    def test_an_http_error_is_unavailable_not_a_crash(self, monkeypatch):
        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", {}, io.BytesIO(b"bad key"))

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(llm.LLMUnavailable, match="401"):
            llm.draft("prompt", config=_config())

    def test_a_network_error_is_unavailable_not_a_crash(self, monkeypatch):
        def fake_urlopen(request, timeout=None):
            raise urllib.error.URLError("no route to host")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(llm.LLMUnavailable, match="could not reach"):
            llm.draft("prompt", config=_config())
