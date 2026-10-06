from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from commontrace import llm
from commontrace import secrets_provider as secrets


class Client:
    def __init__(self):
        self.calls = []
        self.value = "first"

    def get_secret_value(self, **kwargs):
        self.calls.append(kwargs)
        return {"SecretString": self.value}


def test_aws_cache_rotation_expiry_and_invalidation():
    client = Client()
    provider = secrets.AWSSecretsProvider(client, ttl=10)
    clock = [1000.0]
    provider._cache._clock = lambda: clock[0]
    assert provider.get("id") == "first"
    client.value = "rotated"
    assert provider.get("id") == "first"
    clock[0] += 10
    assert provider.get("id") == "rotated"
    assert client.calls == [{"SecretId": "id", "VersionStage": "AWSCURRENT"}] * 2
    client.value = "third"
    provider.invalidate("id")
    assert provider.get("id") == "third"


def test_aws_fields_and_errors_never_expose_secret_values():
    client = Client()
    client.value = json.dumps({"api_key": "very-sensitive-key"})
    provider = secrets.AWSSecretsProvider(client, ttl=0)
    assert provider.get_field("id", "api_key") == "very-sensitive-key"
    with pytest.raises(RuntimeError) as error:
        provider.get_field("id", "missing")
    assert "very-sensitive-key" not in str(error.value)
    client.value = "invalid secret payload"
    with pytest.raises(RuntimeError) as error:
        provider.get_field("id", "api_key")
    assert "invalid secret payload" not in str(error.value)


def test_provider_failures_are_not_cached_or_replaced_with_plaintext(monkeypatch):
    class Unavailable:
        def get_secret_value(self, **kwargs):
            raise RuntimeError("upstream response contained a-secret-value")

    provider = secrets.AWSSecretsProvider(Unavailable())
    monkeypatch.setenv("TEST_SECRET", "stale-plaintext")
    for _ in range(2):
        with pytest.raises(RuntimeError) as error:
            secrets.env_secret("TEST_SECRET", provider=provider)
        assert "a-secret-value" not in str(error.value) and "stale-plaintext" not in str(error.value)
    assert provider._cache.stats()["misses"] == 2


def test_file_precedence_and_rotation_and_aws_opt_in(tmp_path, monkeypatch):
    path = tmp_path / "key"
    path.write_text("file-secret\n")
    monkeypatch.setenv("TEST_SECRET", "environment-secret")
    monkeypatch.setenv("TEST_SECRET_FILE", str(path))
    monkeypatch.setenv("TEST_SECRET_AWS_SECRET_ID", "secret/id")
    monkeypatch.setenv("TEST_SECRET_AWS_SECRET_FIELD", "key")
    client = Client()
    client.value = '{"key": "aws-secret"}'
    monkeypatch.setattr(secrets, "_AWS_PROVIDERS", SimpleNamespace(
        get_or_load=lambda *_a: secrets.AWSSecretsProvider(client)))
    assert secrets.env_secret("TEST_SECRET") == "file-secret"
    path.write_text("rotated-file\n")
    assert secrets.env_secret("TEST_SECRET") == "rotated-file"
    monkeypatch.delenv("TEST_SECRET_FILE")
    assert secrets.env_secret("TEST_SECRET") == "aws-secret"
    monkeypatch.delenv("TEST_SECRET_AWS_SECRET_ID")
    assert secrets.env_secret("TEST_SECRET") == "environment-secret"


def test_llm_config_uses_rotating_mounted_secret(tmp_path, monkeypatch):
    path = tmp_path / "key"
    path.write_text("first\n")
    monkeypatch.setenv("COMMONTRACE_LLM_API_KEY_FILE", str(path))
    monkeypatch.setenv("COMMONTRACE_LLM_PROVIDER", "anthropic")
    assert llm.load_config().api_key == "first"
    path.write_text("next\n")
    assert llm.load_config().api_key == "next"


def test_secret_provider_is_an_extensible_structural_contract():
    provider = SimpleNamespace(get=lambda name: "resolved-" + name)
    assert secrets.env_secret("CUSTOM", provider=provider) == "resolved-CUSTOM"


def test_llm_secret_failure_is_classified_as_provider_unavailability(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONTRACE_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("COMMONTRACE_LLM_API_KEY_FILE", str(tmp_path / "missing"))
    monkeypatch.setenv("COMMONTRACE_LLM_API_KEY", "stale-key-must-not-be-used")
    with pytest.raises(llm.LLMUnavailable, match="API secret") as error:
        llm.load_config()
    assert "stale-key-must-not-be-used" not in str(error.value)
