"""Provider protocol failures must use the API's recoverable error boundary."""
import json
import sys
from types import SimpleNamespace

import pytest

from unimem import llm


@pytest.mark.parametrize("provider,reply", [
    ("anthropic", {"content": [{"text": 123}]}),
    ("anthropic", {"content": "wrong"}),
    ("anthropic", {"content": [], "usage": "wrong"}),
    ("anthropic", {"content": [], "usage": {"input_tokens": "100"}}),
    ("openai-compatible", {"choices": {"wrong": True}}),
    ("openai-compatible", {"choices": [{"message": "wrong"}]}),
    ("openai-compatible", {"choices": [{"message": {"content": ["wrong"]}}]}),
    ("openai-compatible", {"choices": [], "usage": {"completion_tokens": -1}}),
    ("gemini", {"candidates": {"wrong": True}}),
    ("gemini", []),
    ("gemini", {"candidates": [{"content": "wrong"}]}),
    ("gemini", {"candidates": [{"content": {"parts": [{"text": None}]}}]}),
    ("gemini", {"candidates": [{"content": {}}], "usageMetadata": {"candidatesTokenCount": "2"}}),
    ("bedrock", {"output": {"message": "wrong"}}),
    ("bedrock", {"output": {"message": {"content": [{"text": False}]}}}),
    ("bedrock", {"usage": {"inputTokens": True}}),
])
def test_malformed_provider_reply_is_unavailable_for_text_and_vision(monkeypatch, provider, reply):
    monkeypatch.setattr(llm, "_post_json", lambda *_args, **_kwargs: reply)
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(
        client=lambda *_args, **_kwargs: SimpleNamespace(converse=lambda **_kwargs: reply)))
    config = llm.Config(provider, "response-contract-test", "synthetic-key", base_url="https://provider.invalid")
    with pytest.raises(llm.LLMUnavailable):
        llm.complete("synthetic prompt", config)
    if provider in ("anthropic", "openai-compatible"):
        with pytest.raises(llm.LLMUnavailable):
            llm.complete_with_image(config, "synthetic prompt", b"image", "image/png")


def test_bedrock_client_initialization_failure_is_unavailable(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("synthetic expired credentials")

    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=unavailable))
    with pytest.raises(llm.LLMUnavailable):
        llm.complete("prompt", llm.Config("bedrock", "client-initialization-test", "", region="us-east-1"))


def test_gateway_maps_malformed_model_reply_to_a_recoverable_503(tmp_path, monkeypatch):
    from unimem.gateway import Gateway

    monkeypatch.setenv("UNIMEM_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("UNIMEM_LLM_MODEL", "gateway-response-contract-test")
    monkeypatch.setenv("UNIMEM_LLM_API_KEY", "synthetic-key")
    monkeypatch.setattr(llm, "_post_json", lambda *_args, **_kwargs: {"content": [{"text": False}]})
    token = "g" * 40
    gateway = Gateway(str(tmp_path), token=token)
    headers = {"Authorization": "Bearer " + token, "Host": "localhost"}
    try:
        added = gateway.handle("POST", "/v1/conversation/add", headers, json.dumps({
            "space": "review", "session": "session", "messages": [
                {"speaker": "user", "text": "The staging review must finish before release."},
            ],
        }).encode())
        assert added.status == 200
        response = gateway.handle("POST", "/v1/conversation/summarize", headers,
            json.dumps({"space": "review", "session": "session", "mode": "model"}).encode())
        assert response.status == 503
        assert json.loads(response.body)["error"]["code"] == "llm_unavailable"
    finally:
        gateway.close()
