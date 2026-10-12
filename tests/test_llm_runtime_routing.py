from __future__ import annotations

from commontrace import configuration, llm
from commontrace.llm_runtime import LLMRuntime, purpose


def test_project_model_route_is_selected_and_manifested_without_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(
        configuration,
        "current",
        lambda: {"model_routes": {"extract": "small-model"}, "llm_budget": {}},
    )
    monkeypatch.setattr(llm, "cost_usd", lambda _usage, _model: 0.0)
    base = llm.Config("openai-compatible", "default-model", "secret-value", "https://model.example")
    seen = []

    def complete(_prompt, *, config):
        seen.append(config)
        return "answer", {"input_tokens": 2, "output_tokens": 1}

    runtime = LLMRuntime(str(tmp_path), "routing", routes={"default": base}, complete=complete)

    with runtime.scope(), purpose("extract"):
        assert llm.complete("question")[0] == "answer"

    manifest = runtime.manifest()
    assert seen[0].model == "small-model"
    assert manifest["configured_model_routes"] == {"extract": "small-model"}
    assert manifest["selected_routes"] == {
        "extract": {"provider": "openai-compatible", "model": "small-model"}
    }
    assert "secret-value" not in repr(manifest)


def test_explicit_purpose_route_takes_precedence_over_project_model_route(tmp_path, monkeypatch):
    monkeypatch.setattr(
        configuration,
        "current",
        lambda: {"model_routes": {"extract": "small-model"}, "llm_budget": {}},
    )
    monkeypatch.setattr(llm, "cost_usd", lambda _usage, _model: 0.0)
    base = llm.Config("openai-compatible", "base-model", "", "https://model.example")
    explicit = llm.Config("ollama", "explicit-model", "")
    runtime = LLMRuntime(
        str(tmp_path),
        "routing-precedence",
        routes={"default": base, "extract": explicit},
        complete=lambda _prompt, *, config: ("answer", {"input_tokens": 1, "output_tokens": 1}),
    )

    runtime.complete("question", purpose="extract")

    assert runtime.manifest()["selected_routes"]["extract"] == {
        "provider": "ollama",
        "model": "explicit-model",
    }
