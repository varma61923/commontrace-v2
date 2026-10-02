from __future__ import annotations

import io
import json
import sys
import types
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
        monkeypatch.setenv("COMMONTRACE_LLM_PROVIDER", "cohere")
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


class TestCloudProviders:
    REPLY = ('{"rule": "r", "applies_when": "a", "do_not_apply_when": "d", "evidence": ["e1"]}')

    def _env(self, monkeypatch, provider, **extra):
        for k in ("COMMONTRACE_LLM_API_KEY", "COMMONTRACE_LLM_MODEL", "COMMONTRACE_LLM_REGION",
                  "COMMONTRACE_LLM_PROJECT", "AWS_REGION", "COMMONTRACE_LLM_PRICES"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("COMMONTRACE_LLM_PROVIDER", provider)
        for k, v in extra.items():
            monkeypatch.setenv(k, v)

    def test_cloud_providers_need_no_api_key_but_name_their_model_and_place(self, monkeypatch):
        self._env(monkeypatch, "bedrock")
        with pytest.raises(llm.LLMUnavailable, match="COMMONTRACE_LLM_MODEL, COMMONTRACE_LLM_REGION"):
            llm.load_config()
        self._env(monkeypatch, "bedrock", COMMONTRACE_LLM_MODEL="m", AWS_REGION="eu-west-1")
        assert llm.load_config().region == "eu-west-1"
        self._env(monkeypatch, "vertex", COMMONTRACE_LLM_MODEL="m", COMMONTRACE_LLM_REGION="europe-west4")
        with pytest.raises(llm.LLMUnavailable, match="COMMONTRACE_LLM_PROJECT"):
            llm.load_config()

    def test_an_api_key_is_still_required_for_the_others(self, monkeypatch):
        self._env(monkeypatch, "anthropic")
        with pytest.raises(llm.LLMUnavailable, match="COMMONTRACE_LLM_API_KEY"):
            llm.load_config()

    def test_bedrock_uses_the_converse_shape(self, monkeypatch):
        calls = {}

        class Client:
            def converse(self, **kw):
                calls.update(kw)
                return {"output": {"message": {"role": "assistant", "content": [{"text": TestCloudProviders.REPLY}]}},
                        "usage": {"inputTokens": 11, "outputTokens": 7}}

        fake = types.SimpleNamespace(client=lambda service, region_name=None: (
            calls.update(service=service, region=region_name), Client())[1])
        monkeypatch.setitem(sys.modules, "boto3", fake)
        cfg = llm.Config(provider="bedrock", model="m-1", api_key="", region="eu-west-1")
        d = llm.draft("p", allowed_evidence_ids={"e1"}, config=cfg)
        assert calls["service"] == "bedrock-runtime" and calls["region"] == "eu-west-1"
        assert calls["modelId"] == "m-1" and calls["messages"][0]["content"] == [{"text": "p"}]
        assert calls["inferenceConfig"]["temperature"] == 0
        assert d.rule == "r" and d.provenance["usage"] == {"input_tokens": 11, "output_tokens": 7, "estimated": False}

    def test_vertex_uses_the_genai_client_in_vertex_mode(self, monkeypatch):
        seen = {}

        class Models:
            def generate_content(self, *, model, contents, config):
                seen.update(model=model, contents=contents, temperature=config.temperature)
                return types.SimpleNamespace(
                    text=TestCloudProviders.REPLY,
                    usage_metadata=types.SimpleNamespace(prompt_token_count=5, candidates_token_count=3))

        class Client:
            def __init__(self, **kw):
                seen["client"] = kw
                self.models = Models()

        genai = pytest.importorskip("google.genai")
        monkeypatch.setattr(genai, "Client", Client)
        cfg = llm.Config(provider="vertex", model="gem", api_key="", region="europe-west4", project="proj")
        d = llm.draft("p", allowed_evidence_ids={"e1"}, config=cfg)
        assert seen["client"] == {"vertexai": True, "project": "proj", "location": "europe-west4"}
        assert seen["model"] == "gem" and seen["temperature"] == 0
        assert d.provenance["usage"]["input_tokens"] == 5

    @pytest.mark.parametrize("provider,module", [("bedrock", "boto3"), ("vertex", "google")])
    def test_a_missing_sdk_degrades_with_the_extra_named(self, monkeypatch, provider, module):
        monkeypatch.setitem(sys.modules, module, None)
        cfg = llm.Config(provider=provider, model="m", api_key="", region="r", project="p")
        with pytest.raises(llm.LLMUnavailable, match=r"commontrace\[llm\]"):
            llm.draft("p", config=cfg)

    def test_an_sdk_failure_is_unavailable_not_a_crash(self, monkeypatch):
        class Client:
            def converse(self, **kw):
                raise RuntimeError("AccessDeniedException")

        monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda *a, **k: Client()))
        with pytest.raises(llm.LLMUnavailable, match="AccessDeniedException"):
            llm.draft("p", config=llm.Config(provider="bedrock", model="m", api_key="", region="r"))


class TestCost:
    PRICES = {"m": {"input_per_mtok": 3.0, "output_per_mtok": 15.0}}

    def test_cost_comes_only_from_the_owners_price_table(self, tmp_path, monkeypatch):
        usage = {"input_tokens": 2000, "output_tokens": 500}
        assert llm.cost_usd(usage, "m", self.PRICES) == pytest.approx((2000 * 3 + 500 * 15) / 1e6)
        assert llm.cost_usd(usage, "other-model", self.PRICES) is None
        monkeypatch.delenv("COMMONTRACE_LLM_PRICES", raising=False)
        assert llm.cost_usd(usage, "m") is None
        path = tmp_path / "prices.json"
        path.write_text(json.dumps(self.PRICES))
        monkeypatch.setenv("COMMONTRACE_LLM_PRICES", str(path))
        assert llm.cost_usd(usage, "m") == pytest.approx(0.0135)
        path.write_text("not json")
        assert llm.cost_usd(usage, "m") is None

    def test_a_draft_carries_cost_when_a_table_exists_and_not_otherwise(self, tmp_path, monkeypatch):
        monkeypatch.setattr(llm, "_call_anthropic", lambda cfg, prompt: (
            TestCloudProviders.REPLY, {"input_tokens": 1000, "output_tokens": 100}))
        cfg = llm.Config(provider="anthropic", model="m", api_key="k")
        monkeypatch.delenv("COMMONTRACE_LLM_PRICES", raising=False)
        assert "cost_usd" not in llm.draft("p", config=cfg).provenance
        path = tmp_path / "p.json"
        path.write_text(json.dumps(self.PRICES))
        monkeypatch.setenv("COMMONTRACE_LLM_PRICES", str(path))
        assert llm.draft("p", config=cfg).provenance["cost_usd"] == pytest.approx(0.0045)
