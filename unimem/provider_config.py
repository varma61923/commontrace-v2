"""Strict, optional provider configuration models for trusted application code.

Import this module with the ``providers`` extra. The core runtime keeps its lean
dataclass configuration; factory callers can validate mappings with these models.
Secrets never appear in model representations, JSON exports or validation errors.
"""
from __future__ import annotations

import ipaddress
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

if TYPE_CHECKING:
    from unimem.llm import Config

_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")


# Values copied from examples instead of a real key: fail at configuration time, not
# as an opaque 401 from the provider on the first request.
_PLACEHOLDER_SECRET = re.compile(r"(?i)(?:none|null|undefined|changeme|change-me|xxx+|\.\.\.|<[^>]*>|"
                                 r"(?:your|my)[-_ ]?(?:api)?[-_ ]?(?:key|token|secret)(?:[-_ ]?here)?|"
                                 r"sk-\.\.\.|sk-x+|replace[-_ ]?me)")

class _Configuration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    def public_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"api_key", "dsn"})


class LLMConfig(_Configuration):
    provider: str
    model: str = Field(min_length=1, max_length=256)
    api_key: SecretStr | None = Field(default=None, repr=False)
    api_key_env: str | None = None
    base_url: str | None = Field(default=None, max_length=2048)
    region: str | None = Field(default=None, min_length=1, max_length=128)
    project: str | None = Field(default=None, min_length=1, max_length=256)
    cache_namespace: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("provider")
    @classmethod
    def provider_name(cls, value: str) -> str:
        if not _NAME.fullmatch(value):
            raise ValueError("provider must be a lowercase registered name")
        from unimem.providers import LLMFactory

        if value not in LLMFactory.names():
            raise ValueError("LLM provider is not registered")
        return value

    @field_validator("model", "region", "project", "cache_namespace")
    @classmethod
    def single_line(cls, value: str | None) -> str | None:
        if value is not None and (value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError("provider settings must be nonempty single-line values")
        return value

    @field_validator("api_key_env")
    @classmethod
    def secret_reference(cls, value: str | None) -> str | None:
        if value is not None and not _ENV.fullmatch(value):
            raise ValueError("api_key_env must name an environment secret")
        return value

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value != value.strip() or any(ord(c) <= 32 or ord(c) == 127 for c in value):
            raise ValueError("provider endpoint must not contain whitespace or control characters")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            raise ValueError("provider endpoint is not a valid URL") from None
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or (port is not None and not 1 <= port <= 65535)):
            raise ValueError("provider endpoint must omit credentials, query and fragment")
        loopback = parsed.hostname.lower() == "localhost"
        try:
            loopback |= ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            pass
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ValueError("provider endpoints require HTTPS or loopback HTTP")
        return value.rstrip("/")

    @model_validator(mode="after")
    def routing(self) -> LLMConfig:
        if self.api_key is not None and self.api_key_env is not None:
            raise ValueError("choose an inline secret or api_key_env, not both")
        if self.provider == "openai-compatible" and not self.base_url:
            raise ValueError("openai-compatible requires base_url")
        if self.provider in {"bedrock", "vertex"} and not self.region:
            raise ValueError("cloud providers require region")
        if self.provider == "vertex" and not self.project:
            raise ValueError("vertex requires project")
        return self

    def runtime_config(self) -> Config:
        from unimem.exceptions import ConfigurationError
        from unimem.llm import Config
        from unimem.providers import LLM_CREDENTIALS
        from unimem.secrets_provider import env_secret

        requires_key = self.provider not in {"local", "ollama", "bedrock", "vertex"} \
            and LLM_CREDENTIALS.get(self.provider, True)
        if self.provider == "openai-compatible" and self.base_url is not None:
            requires_key = urlsplit(self.base_url).scheme != "http"
        key = self.api_key.get_secret_value() if self.api_key is not None else (
            env_secret(self.api_key_env or "UNIMEM_LLM_API_KEY")
            if requires_key or self.api_key_env is not None else ""
        )
        key = key.strip()
        if (requires_key and not key) or len(key) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in key):
            raise ConfigurationError("the selected provider requires a valid API secret")
        if key and _PLACEHOLDER_SECRET.fullmatch(key):
            raise ConfigurationError("the API secret is a documentation placeholder, not a key")
        provider, base = self.provider, self.base_url
        if provider in {"openai", "ollama"}:
            base = base or ("https://api.openai.com/v1" if provider == "openai" else "http://localhost:11434/v1")
            provider = "openai-compatible"
        return Config(provider=provider, model=self.model, api_key=key, base_url=base,
                      region=self.region, project=self.project, cache_namespace=self.cache_namespace)


class EmbedderConfig(_Configuration):
    provider: str
    model: str = Field(min_length=1, max_length=128)
    dimensions: int | None = Field(default=None, ge=32, le=8192)
    cache_only: bool = True

    @field_validator("provider", "model")
    @classmethod
    def single_line(cls, value: str) -> str:
        if value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("provider and model must be single-line values without surrounding whitespace")
        return value

    def tag(self) -> str:
        from unimem import embeddings

        base = self.model if self.provider == "local" and self.model in embeddings.LOCAL_MODELS else \
            f"{self.provider}:{self.model}"
        return base + (f"@{self.dimensions}" if self.dimensions is not None else "")

    @model_validator(mode="after")
    def registered_model(self) -> EmbedderConfig:
        from unimem.embeddings import EmbeddingError, parse

        try:
            parse(self.tag())
        except EmbeddingError:
            raise ValueError("invalid provider, model or embedding dimensions") from None
        return self


class VectorStoreConfig(_Configuration):
    provider: str
    owner: str
    dataset: str
    model: str
    dimension: int = Field(ge=1, le=8192)
    dsn: SecretStr | None = Field(default=None, repr=False)
    dsn_env: str | None = None

    @field_validator("dsn_env")
    @classmethod
    def secret_reference(cls, value: str | None) -> str | None:
        if value is not None and not _ENV.fullmatch(value):
            raise ValueError("dsn_env must name an environment secret")
        return value

    @model_validator(mode="after")
    def pinned_scope(self) -> VectorStoreConfig:
        from unimem.providers import VECTORS, register_local_backends
        from unimem.vector_store import _Scope

        register_local_backends()
        if self.provider not in VECTORS.names():
            raise ValueError("vector provider is not registered")
        try:
            _Scope(self.owner, self.dataset, self.model, self.dimension)
        except ValueError:
            raise ValueError("invalid owner, dataset, model or dimension") from None
        if self.dsn is not None and self.dsn_env is not None:
            raise ValueError("choose an inline DSN or dsn_env, not both")
        if self.provider == "postgres" and self.dsn is None and self.dsn_env is None:
            raise ValueError("postgres requires a server-configured DSN")
        return self
