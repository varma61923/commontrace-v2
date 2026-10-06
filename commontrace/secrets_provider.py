"""Rotatable secrets with explicit provider injection and optional AWS support.

FILE takes precedence, then explicit AWS secret references, then environment.
Provider failures never fall back to a stale plaintext secret. Values and SDK
exception messages are excluded from diagnostics.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from typing import Protocol

from commontrace.runtime_cache import RuntimeCache


class SecretProvider(Protocol):
    def get(self, name: str) -> str:
        """Resolve a secret, or raise if the configured source is unavailable."""


class AWSSecretsProvider:
    """Read AWSCURRENT with a bounded refresh TTL; SDK credentials stay external.

    Inject a client for tests/custom SDK configuration. JSON fields can be
    selected by ``get_field``. ``invalidate`` forces an immediate refresh.
    """

    def __init__(self, client=None, *, region: str | None = None, ttl: float = 60.0) -> None:
        self._client, self.region = client, region
        self._cache = RuntimeCache[str](max_entries=128, max_bytes=1024 * 1024, ttl=ttl,
                                       weigh=lambda k, v: 512 + sys.getsizeof(k) + sys.getsizeof(v))

    def get(self, name: str) -> str:
        def load() -> str:
            try:
                client = self._client
                if client is None:
                    # Optional SDK: server-only/core installs stay stdlib-safe.
                    boto3 = importlib.import_module("boto3")
                    client = boto3.client("secretsmanager", region_name=self.region)
                result = client.get_secret_value(SecretId=name, VersionStage="AWSCURRENT")
                value = result.get("SecretString")
                if not isinstance(value, str) or not value:
                    raise ValueError("expected a nonempty SecretString")
                return value
            except Exception as exc:
                # SDK errors can include request/response data; never echo them.
                raise RuntimeError(f"AWS secret resolution failed ({type(exc).__name__})") from None

        return self._cache.get_or_load(name, load)

    def get_field(self, name: str, field: str) -> str:
        try:
            parsed = json.loads(self.get(name))
            value = parsed.get(field) if isinstance(parsed, dict) else None
            if not isinstance(value, str) or not value:
                raise ValueError("expected a nonempty JSON string field")
            return value
        except (ValueError, RecursionError):
            raise RuntimeError("AWS secret must contain the configured nonempty JSON string field") from None

    def invalidate(self, name: str | None = None) -> None:
        if name is None:
            self._cache.clear()
        else:
            self._cache.invalidate(name)


# SDK/provider objects are injected by callers; environment configuration uses
# one provider per region, bounded to prevent unbounded configuration retention.
_AWS_PROVIDERS = RuntimeCache[AWSSecretsProvider](max_entries=8, max_bytes=1024 * 1024,
                                                ttl=3600, weigh=lambda _k, _v: 128 * 1024)


def env_secret(name: str, default: str = "", *, provider: SecretProvider | None = None) -> str:
    """Resolve an injected provider or configured environment/file/AWS source.

    ``NAME_AWS_SECRET_ID`` opts into AWS; ``NAME_AWS_SECRET_FIELD`` selects a
    field from SecretString JSON. ``NAME_AWS_REGION`` optionally sets the region.
    File values are read anew on every invocation, reflecting mounted rotation.
    """
    if provider is not None:
        return provider.get(name)
    file_path = os.environ.get(f"{name}_FILE")
    if file_path:
        try:
            with open(file_path, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            raise RuntimeError(f"{name}_FILE='{file_path}' is set but could not be read") from None
    secret_id = os.environ.get(f"{name}_AWS_SECRET_ID")
    if secret_id:
        region = os.environ.get(f"{name}_AWS_REGION") or None
        aws = _AWS_PROVIDERS.get_or_load(region, lambda: AWSSecretsProvider(region=region))
        field = os.environ.get(f"{name}_AWS_SECRET_FIELD")
        return aws.get_field(secret_id, field) if field else aws.get(secret_id)
    return os.environ.get(name, default)
