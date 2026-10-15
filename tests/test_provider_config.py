from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

pytest.importorskip("pydantic")

from pydantic import ValidationError  # noqa: E402

from commontrace import embeddings, llm, providers  # noqa: E402
from commontrace.exceptions import CapabilityError, ConfigurationError  # noqa: E402
from commontrace.provider_config import EmbedderConfig, LLMConfig, VectorStoreConfig  # noqa: E402
from commontrace.vector_store import VectorRecord  # noqa: E402


def test_factories_expose_native_providers_and_never_import_from_configuration():
    assert {"openai", "anthropic", "ollama", "local", "bedrock", "vertex", "gemini"} <= set(
        providers.LLMFactory.names()
    )
    with pytest.raises(ValidationError):
        LLMConfig(provider="anthropic", model="test", import_path="os.system")
    with pytest.raises(ValidationError):
        LLMConfig(provider="os.system", model="test")


@pytest.mark.parametrize("patch", [
    {"base_url": "http://remote.example/v1"},
    {"base_url": "https://user:password@api.example/v1"},
    {"base_url": "https://api.example/v1?token=private"},
    {"base_url": "https://api.example/v1\n"},
    {"model": "\nprivate"},
    {"api_key_env": "../credentials"},
    {"api_key_env": "KEY"},
])
def test_validation_errors_hide_inline_secrets(patch):
    with pytest.raises(ValidationError) as caught:
        LLMConfig.model_validate({"provider": "anthropic", "model": "test",
                                  "api_key": "fixture-secret-never-log", **patch})
    assert "fixture-secret-never-log" not in str(caught.value)


def test_public_configuration_and_schema_hide_credentials():
    value = LLMConfig(provider="anthropic", model="test", api_key="fixture-secret-never-log")
    assert "fixture-secret-never-log" not in repr(value)
    assert "api_key" not in value.public_dict()
    assert "fixture-secret-never-log" not in value.model_dump_json()
    schema = LLMConfig.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["api_key"]["anyOf"][0]["writeOnly"] is True
    postgres = VectorStoreConfig(provider="postgres", owner="o", dataset="d", model="m", dimension=2,
                                 dsn="postgresql://fixture:private@localhost/db")
    assert "private" not in repr(postgres) and "dsn" not in postgres.public_dict()


def test_local_configuration_does_not_read_unrelated_cloud_secrets(monkeypatch):
    monkeypatch.setenv("COMMONTRACE_LLM_API_KEY_FILE", "/does/not/exist")
    cfg = LLMConfig(provider="local", model="local-model").runtime_config()
    assert cfg.api_key == ""


def test_openai_alias_uses_conventional_key_and_correct_endpoint(monkeypatch):
    monkeypatch.delenv("COMMONTRACE_LLM_API_KEY", raising=False)
    monkeypatch.delenv("COMMONTRACE_LLM_API_KEY_FILE", raising=False)
    monkeypatch.setenv("COMMONTRACE_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-openai-key")
    monkeypatch.delenv("COMMONTRACE_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("COMMONTRACE_LLM_MODEL", raising=False)
    cfg = llm.load_config()
    assert cfg.provider == "openai-compatible" and cfg.base_url == "https://api.openai.com/v1"
    assert cfg.api_key == "fixture-openai-key" and cfg.model == "gpt-4.1-mini"


def test_factory_keeps_credentials_and_models_per_instance(monkeypatch):
    monkeypatch.setenv("FIRST_MODEL_KEY", "fixture-first")
    monkeypatch.setenv("SECOND_MODEL_KEY", "fixture-second")
    monkeypatch.setenv("COMMONTRACE_LLM_API_KEY", "global-unchanged")
    monkeypatch.setenv("COMMONTRACE_LLM_CACHE", "0")
    seen = []

    def complete(config, prompt):
        seen.append((config.model, config.api_key, prompt))
        return config.model, {"input_tokens": 1}

    monkeypatch.setitem(providers.LLMS._factories, "fixture-model", lambda: complete)
    monkeypatch.setitem(providers.LLM_CREDENTIALS, "fixture-model", True)
    first = providers.LLMFactory.create("fixture-model", {"model": "first", "api_key_env": "FIRST_MODEL_KEY"})
    second = providers.LLMFactory.create("fixture-model", {"model": "second", "api_key_env": "SECOND_MODEL_KEY"})
    assert first.generate("one") == "first"
    assert second.complete("two") == ("second", {"input_tokens": 1})
    assert seen == [("first", "fixture-first", "one"), ("second", "fixture-second", "two")]
    assert llm.env_secret("COMMONTRACE_LLM_API_KEY") == "global-unchanged"
    with pytest.raises(ConfigurationError):
        providers.LLMFactory.create("fixture-model", {"provider": "anthropic", "model": "test"})


def test_legacy_dataclass_factory_does_not_require_a_mapping_configuration(monkeypatch):
    cfg = llm.Config("local", "test", "")
    monkeypatch.setattr(llm, "complete", lambda prompt, config: (prompt, {"model": config.model}))
    model = providers.LLMFactory.create("local", cfg)
    assert model.complete("prompt") == ("prompt", {"model": "test"})
    with pytest.raises(ConfigurationError):
        providers.LLMFactory.create("anthropic", cfg)


def test_custom_embedding_tags_validate_and_preserve_dimensions(monkeypatch):
    seen = []

    def factory(spec):
        seen.append(spec)
        return SimpleNamespace(spec=spec, embed=lambda texts, query: [[1.0] + [0.0] * 31 for _ in texts])

    monkeypatch.setitem(embeddings._CUSTOM, "fixture2", factory)
    cfg = EmbedderConfig(provider="fixture2", model="dense", dimensions=32)
    value = providers.EmbedderFactory.create(cfg)
    assert value.spec == embeddings.Spec("fixture2", "dense", 32)
    assert embeddings.provider("fixture2:dense@64").spec.dimensions == 64
    assert len(seen) == 2
    assert embeddings.provider(" fixture2:dense@32\n").spec.dimensions == 32
    for tag in ("fixture2:dense@3", "fixture2:dense@99999", "fixture2:den\nse"):
        with pytest.raises(embeddings.EmbeddingError):
            embeddings.provider(tag)
    with pytest.raises(ValidationError):
        EmbedderConfig(provider="local", model="minilm", dimensions=32)


def test_embedding_registration_is_atomic_under_concurrent_startup(monkeypatch):
    monkeypatch.setattr(embeddings, "_CUSTOM", {})

    def register(_):
        try:
            embeddings.register("fixture2", lambda spec: SimpleNamespace(spec=spec))
            return True
        except embeddings.EmbeddingError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(register, range(24))) == 1
    assert embeddings.known()["custom"] == ["fixture2"]


def test_custom_embedding_is_available_without_sentence_transformers(monkeypatch):
    from commontrace.conversation import embed

    monkeypatch.setitem(embeddings._CUSTOM, "fixture2", lambda spec: SimpleNamespace(spec=spec))
    monkeypatch.setattr(embed.importlib.util, "find_spec", lambda name: object() if name == "numpy" else None)
    assert embed.available_for("fixture2:dense@32")


def test_legacy_wrong_dimension_vectors_are_recomputed(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")

    from commontrace.conversation import embed

    calls = []

    class Fixed:
        def __init__(self, spec):
            self.spec = spec

        def embed(self, texts, *, query):
            calls.append(list(texts))
            return [[1.0] + [0.0] * 31 for _ in texts]

    monkeypatch.setitem(embeddings._CUSTOM, "fixture2", Fixed)
    value = embed.Embedder(str(tmp_path), "fixture2:dense@32")
    try:
        value.db.execute("INSERT INTO vec VALUES (?, ?)", ("hash", np.ones(64, dtype=np.float16).tobytes()))
        assert value.vectors([("hash", "deployment note")]).shape == (1, 32)
        assert value.vectors([("hash", "deployment note")]).shape == (1, 32)
        assert calls == [["deployment note"]]
    finally:
        value.close()


def test_validated_sqlite_factory_preserves_scope_and_supports_real_operations(tmp_path):
    async def run():
        factory = providers.BackendFactory(str(tmp_path))
        value = await factory.from_config({"provider": "sqlite", "owner": "account-a", "dataset": "project",
                                           "model": "test", "dimension": 2})
        try:
            assert value.tenant == "account-a" and value.namespace == "project"
            await value.upsert([VectorRecord("one", (1.0, 0.0), "generation")])
            assert [hit.key for hit in await value.search((1.0, 0.0))] == ["one"]
            assert await value.delete(["one"]) == 1
        finally:
            await value.close()

    asyncio.run(run())


def test_registered_backend_cannot_silently_override_authenticated_scope(tmp_path, monkeypatch):
    async def run():
        closed = []

        class WrongScope:
            tenant = "someone-else"
            namespace = "project"
            model = "test"
            dimension = 2

            async def close(self):
                closed.append(True)

        monkeypatch.setitem(providers.VECTORS._factories, "fixture-store", lambda **kwargs: WrongScope())
        with pytest.raises(CapabilityError, match="authenticated scope"):
            await providers.BackendFactory(str(tmp_path)).vector(
                "fixture-store", owner="account-a", dataset="project", model="test", dimension=2,
            )
        assert closed == [True]

    asyncio.run(run())


def test_insert_prepares_only_redacted_content_and_reuses_vectors(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    from commontrace.conversation.store import Store

    calls = []

    class Fixed:
        def __init__(self, spec):
            self.spec = spec

        def embed(self, texts, *, query):
            calls.extend(texts)
            return [[1.0] + [0.0] * 31 for _ in texts]

    monkeypatch.setitem(embeddings._CUSTOM, "fixture2", Fixed)
    secret = "sk-" + "a" * 48
    with Store(str(tmp_path), "project") as store:
        result = store.add("session", [{"speaker": "user", "text": "OPENAI_API_KEY=" + secret}],
                           cache_vectors=True, embedder="fixture2:dense@32")
        assert result["embedding_cache"]["ready"] is True
        assert all(isinstance(value, int) for value in result["embedding_cache"]["revision"])
        assert calls and all(secret not in text for text in calls)
        count = len(calls)
        repeat = store.add("session", [], cache_vectors=True, embedder="fixture2:dense@32")
        assert repeat["embedding_cache"]["ready"] is True and len(calls) == count


def test_insert_cache_failure_preserves_source_and_hides_provider_secrets(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    from commontrace.conversation.store import Store

    class Broken:
        def __init__(self, spec):
            self.spec = spec

        def embed(self, texts, *, query):
            raise RuntimeError("provider private credentials")

    monkeypatch.setitem(embeddings._CUSTOM, "fixture2", Broken)
    with Store(str(tmp_path), "project") as store:
        result = store.add("session", [{"speaker": "user", "text": "Deploy on Friday"}],
                           cache_vectors=True, embedder="fixture2:dense@32")
        assert result["added"] == 1
        assert result["embedding_cache"] == {"ready": False, "error_type": "RuntimeError"}
        assert store.db.execute("SELECT count(*) FROM turns").fetchone()[0] == 1


def test_gateway_insertion_cache_flags_are_typed_and_wired(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    import json

    from commontrace import gateway

    class Fixed:
        def __init__(self, spec):
            self.spec = spec

        def embed(self, texts, *, query):
            return [[1.0] + [0.0] * 31 for _ in texts]

    monkeypatch.setitem(embeddings._CUSTOM, "fixture2", Fixed)
    app = gateway.Gateway(str(tmp_path), token="typed-insertion-cache")
    headers = {"Authorization": "Bearer typed-insertion-cache"}
    request = {"space": "project", "session": "session", "messages": [{"speaker": "user", "text": "Deploy Friday"}],
               "cache_vectors": True, "embedder": "fixture2:dense@32"}
    response = app.handle("POST", "/v1/conversation/add", headers, json.dumps(request).encode())
    assert response.status == 200 and json.loads(response.body)["embedding_cache"]["ready"]
    jsonschema = pytest.importorskip("jsonschema")

    from tests.test_gateway_openapi_contract import _json_schema

    document = app._openapi({}, {})
    schema = document["paths"]["/v1/conversation/add"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
    jsonschema.Draft4Validator(_json_schema(schema)).validate(json.loads(response.body))
    request["cache_vectors"] = "true"
    assert app.handle("POST", "/v1/conversation/add", headers, json.dumps(request).encode()).status == 400


@pytest.mark.parametrize("provider,endpoint", [
    ("openai", "https://api.openai.com/v1"), ("ollama", "http://localhost:11434/v1"),
])
def test_legacy_factory_aliases_keep_configuration_local_without_optional_models(monkeypatch, provider, endpoint):
    cfg = llm.Config(provider, "test-model", "fixture-key")
    monkeypatch.setattr(llm, "complete", lambda prompt, config: (prompt, {"provider": config.provider}))
    model = providers.LLMFactory.create(provider, cfg)
    assert model.config.provider == "openai-compatible" and model.config.base_url == endpoint
    assert cfg.provider == provider and cfg.base_url is None
    assert model.complete("query")[1]["provider"] == "openai-compatible"


@pytest.mark.parametrize("placeholder", ["your-api-key", "<API_KEY>", "sk-...", "changeme", "None", "YOUR_TOKEN_HERE"])
def test_documentation_placeholder_secrets_fail_at_configuration(placeholder):
    with pytest.raises(ConfigurationError, match="placeholder"):
        LLMConfig(provider="anthropic", model="test", api_key=placeholder).runtime_config()
    assert LLMConfig(provider="anthropic", model="test", api_key="sk-ant-real-looking-0123").runtime_config().api_key
