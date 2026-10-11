"""One safe YAML configuration with local/team/server profiles.

Existing explicit environment settings take precedence. Advanced legacy settings
remain supported; the short primary configuration is the recommended interface.
"""

from __future__ import annotations

import json
import math
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache, wraps
from typing import Any, Callable, ParamSpec, TypeVar, overload

_ACTIVE = ContextVar("commontrace_configuration", default=None)
_P = ParamSpec("_P")
_T = TypeVar("_T")
with open(os.path.join(os.path.dirname(__file__), "configuration.schema.json"), encoding="utf-8") as _stream:
    SETTING_PATHS = json.load(_stream)["x-legacy-environment-aliases"]

PRIMARY_ENVIRONMENT = (
    "COMMONTRACE_CONFIG",
    "COMMONTRACE_ROOT",
    "COMMONTRACE_LLM_API_KEY",
    "COMMONTRACE_HUB_API_KEY",
    "COMMONTRACE_APPROVAL_KEY",
    "COMMONTRACE_AGENT_TOKEN",
    "COMMONTRACE_HANDOFF_KEY",
    "COMMONTRACE_EMBED_API_KEY",
    "COMMONTRACE_GEMINI_API_KEY",
    "COMMONTRACE_COMMONS_VERIFY_KEY",
    "HUB_DATABASE_URL",
    "HUB_ADMIN_TOKEN",
    "HUB_METRICS_TOKEN",
    "HUB_API_KEY_PEPPER",
    "HUB_CONSOLE_SECRET",
    "HUB_ENCRYPTION_KEY",
    "HUB_ENCRYPTION_KEY_PREVIOUS",
    "HUB_COMMONS_SIGNING_KEY",
    "HUB_STRIPE_SECRET_KEY",
    "HUB_STRIPE_WEBHOOK_SECRET",
    "HUB_KMS_KEY_ID",
    "COMMONTRACE_NEO4J_PASSWORD",
    "COMMONTRACE_FALKORDB_PASSWORD",
    "HUB_CONNECTOR_SECRET",
    "HUB_LEDGER_SIGNING_KEY",
)


@dataclass(frozen=True)
class Budget:
    calls: int = 20
    tokens: int = 50000
    cost_usd: float | None = None
    seconds: float = 120.0

    def __post_init__(self):
        if (
            any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in (self.calls, self.tokens)
            )
            or not math.isfinite(self.seconds)
            or self.seconds <= 0
            or (self.cost_usd is not None and (not math.isfinite(self.cost_usd) or self.cost_usd < 0))
        ):
            raise ValueError("budgets must be finite and nonnegative")


PRIMARY = {
    "reranker": (),
    "embedding": ("COMMONTRACE_FACT_EMBEDDER", "COMMONTRACE_GRAPH_EMBEDDER", "COMMONTRACE_CONVERSATION_EMBEDDER"),
    "llm_provider": ("COMMONTRACE_LLM_PROVIDER",),
    "llm_model": ("COMMONTRACE_LLM_MODEL",),
    "llm_base_url": ("COMMONTRACE_LLM_BASE_URL",),
    "llm_cache": ("COMMONTRACE_LLM_CACHE",),
    "offline": ("COMMONTRACE_OFFLINE",),
    "redact_pii": ("COMMONTRACE_REDACT_PII",),
    "telemetry": ("COMMONTRACE_TELEMETRY",),
}
DEFAULTS = {
    "profile": "local",
    "host": "127.0.0.1",
    "port": 8765,
    "agents": {},
    "histories": [],
    "model_routes": {},
    "llm_budget": {},
    "settings": {},
    "secrets": {},
    "profiles": {
        "local": {
            "embedding": "bge-small",
            "reranker": "cross-encoder-fast",
            "redact_pii": True,
            "telemetry": False,
            "llm_cache": True,
        },
        "team": {
            "embedding": "bge-small",
            "reranker": "cross-encoder-fast",
            "redact_pii": True,
            "telemetry": False,
            "llm_cache": True,
        },
        "server": {
            "embedding": "bge-small",
            "reranker": "cross-encoder-fast",
            "redact_pii": True,
            "telemetry": False,
            "llm_cache": True,
        },
    },
}


def _credential(name: str) -> bool:
    return bool(
        re.search(
            r"_(?:API_KEY|PASSWORD|TOKEN|SECRET|PEPPER|ENCRYPTION_KEY|SIGNING_KEY|"
            r"APPROVAL_KEY|HANDOFF_KEY|VERIFY_KEY|DATABASE_URL)(?:_|$)",
            name,
        )
    )


def load(root: str, *, profile: str | None = None) -> dict:
    import yaml

    filename = os.environ.get("COMMONTRACE_CONFIG") or os.path.join(root, "commontrace.yaml")
    data = {}
    if os.path.exists(filename):
        with open(filename, encoding="utf-8") as stream:
            raw = stream.read(64001)
        if len(raw) > 64000:
            raise ValueError("configuration exceeds 64 KiB")
        data = yaml.safe_load(raw) or {}
    return _validate(data, root=root, profile=profile)


def _validate(data, *, root, profile=None):
    if not isinstance(data, dict) or set(data) - set(DEFAULTS):
        raise ValueError("unsupported configuration keys")
    result = {**DEFAULTS, **data}
    selected = profile or result["profile"]
    if selected not in ("local", "team", "server"):
        raise ValueError("configuration profile must be local, team or server")
    profiles = result["profiles"]
    if not isinstance(profiles, dict) or any(k not in ("local", "team", "server") for k in profiles):
        raise ValueError("unsupported configuration profiles")
    supplied = profiles.get(selected, {})
    if not isinstance(supplied, dict):
        raise ValueError("profile options must be a mapping")
    options = {**DEFAULTS["profiles"][selected], **{k: v for k, v in supplied.items() if k != "settings"}}
    if set(options) - set(PRIMARY) or any(not isinstance(v, (str, bool, int)) for v in options.values()):
        raise ValueError("unsupported profile options")
    if any(
        type(options.get(k)) is not bool for k in ("offline", "redact_pii", "telemetry", "llm_cache") if k in options
    ):
        raise ValueError("boolean profile options must be true or false")
    settings = _flatten(result["settings"])
    settings.update(_flatten(supplied.get("settings", {})))
    if set(settings) - set(SETTING_PATHS.values()):
        raise ValueError("unsupported configuration setting")
    secret_paths = {path for name, path in SETTING_PATHS.items() if _credential(name)}
    secret_paths.add("runtime.root")
    if set(settings) & secret_paths:
        raise ValueError("credentials and process paths require secrets references or explicit environment settings")
    secrets = result["secrets"]
    if (
        not isinstance(secrets, dict)
        or len(secrets) > 64
        or any(
            not isinstance(k, str)
            or not re.fullmatch(r"(?:COMMONTRACE|HUB)_[A-Z0-9_]{1,100}", k)
            or not isinstance(v, str)
            or not 0 < len(v) <= 4096
            or not v.startswith(("env:", "file:", "aws:"))
            or not v.partition(":")[2]
            for k, v in secrets.items()
        )
    ):
        raise ValueError("secrets require bounded env:, file: or aws: references")
    if (
        not isinstance(result["agents"], dict)
        or len(result["agents"]) > 8
        or any(
            name not in ("claude", "codex", "cursor", "cline", "gemini", "kimi", "windsurf", "openhands")
            or not isinstance(value, dict)
            or set(value) - {"config_path"}
            or any(not isinstance(item, str) or len(item) > 4096 for item in value.values())
            for name, value in result["agents"].items()
        )
        or not isinstance(result["histories"], list)
        or len(result["histories"]) > 64
        or any(
            not isinstance(item, dict)
            or set(item) != {"agent", "path"}
            or item["agent"] not in ("claude", "codex", "cursor", "gemini")
            or not isinstance(item["path"], str)
            or not 0 < len(item["path"]) <= 4096
            for item in result["histories"]
        )
    ):
        raise ValueError("invalid bounded agent or history configuration")
    if not isinstance(result["host"], str) or type(result["port"]) is not int or not 1 <= result["port"] <= 65535:
        raise ValueError("host and port are invalid")
    routes = result["model_routes"]
    if (
        not isinstance(routes, dict)
        or len(routes) > 16
        or any(not isinstance(k, str) or not isinstance(v, str) or not 0 < len(v) <= 128 for k, v in routes.items())
    ):
        raise ValueError("model_routes maps purpose names to bounded model names")
    if not isinstance(result["llm_budget"], dict):
        raise ValueError("llm_budget must be a mapping")
    try:
        Budget(**result["llm_budget"])
    except TypeError as exc:
        raise ValueError("unsupported LLM budget options") from exc
    result.update(profile=selected, options=options, settings=settings, root=os.path.abspath(root))
    return result


def _flatten(data: dict, prefix="") -> dict:
    if not isinstance(data, dict):
        raise ValueError("settings must be a mapping")
    out = {}
    for key, value in data.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,100}", key):
            raise ValueError("invalid configuration setting name")
        name = prefix + key
        if isinstance(value, dict):
            if name.count(".") >= 3:
                raise ValueError("configuration settings are nested too deeply")
            out.update(_flatten(value, name + "."))
        elif type(value) in (str, bool, int, float) and len(str(value)) <= 16000:
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("configuration setting must be finite")
            out[name] = value
        else:
            raise ValueError("configuration setting must be a bounded scalar")
    return out


@lru_cache(maxsize=128)
def _cached(root, stamp):
    return load(root)


def current() -> dict | None:
    selected = _ACTIVE.get()
    if selected is not None:
        return selected
    root = os.environ.get("COMMONTRACE_ROOT") or os.getcwd()
    filename = os.environ.get("COMMONTRACE_CONFIG") or os.path.join(root, "commontrace.yaml")
    try:
        stat = os.stat(filename)
    except FileNotFoundError:
        return None
    return _cached(root, (filename, stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size))


@overload
def getenv(name: str, default: None = None) -> str | None: ...


@overload
def getenv(name: str, default: str) -> str: ...


def getenv(name: str, default: str | None = None) -> str | None:
    """Explicit environment overrides YAML; configuration never mutates the process."""
    if name in os.environ:
        return os.environ[name]
    config = current()
    if config is not None:
        if name in config.get("secrets", {}):
            from commontrace.secrets_provider import env_secret

            return env_secret(name, default or "")
        path = SETTING_PATHS.get(name)
        value = config.get("settings", {}).get(path)
        if value is None:
            value = next(
                (config.get("options", {}).get(key) for key, variables in PRIMARY.items() if name in variables), None
            )
        if value is not None:
            return ("1" if value else "0") if isinstance(value, bool) else str(value)
    return default


def secret_reference(name: str) -> str:
    config = current()
    return config.get("secrets", {}).get(name, "") if config else ""


@contextmanager
def apply(config: dict):
    token = _ACTIVE.set(config)
    try:
        yield config
    finally:
        _ACTIVE.reset(token)


@contextmanager
def using(root: str):
    """Select a store's configuration for this call, preserving explicit profiles."""
    active = _ACTIVE.get()
    if active is not None and active.get("root") == os.path.abspath(root):
        yield active
        return
    filename = os.environ.get("COMMONTRACE_CONFIG") or os.path.join(root, "commontrace.yaml")
    if not os.path.isfile(filename):
        yield active
        return
    stat = os.stat(filename)
    with apply(_cached(os.path.abspath(root), (filename, stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size))):
        yield _ACTIVE.get()


def configured(function: Callable[_P, _T]) -> Callable[_P, _T]:
    """Route a root-first operation or MemoryClient method through its store YAML."""

    @wraps(function)
    def run(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        first: Any = args[0] if args else kwargs["root"]
        root = first if isinstance(first, str) else first.root
        with using(root):
            return function(*args, **kwargs)

    return run


def migrate_environment(root: str, *, write=False) -> dict:
    """Move legacy nonsecrets to YAML and store only references for credentials.

    Existing YAML values win. The report contains names, never credential values.
    Explicit environment overrides continue to work during a rolling migration.
    """
    import yaml

    filename = os.path.join(root, "commontrace.yaml")
    data = {}
    if os.path.isfile(filename):
        with open(filename, encoding="utf-8") as stream:
            data = yaml.safe_load(stream.read(64001)) or {}
        if not isinstance(data, dict):
            raise ValueError("configuration must be a mapping")
    settings = data.setdefault("settings", {})
    secrets = data.setdefault("secrets", {})
    migrated = []
    for name, path in sorted(SETTING_PATHS.items()):
        if name not in os.environ or name == "COMMONTRACE_ROOT":
            continue
        if _credential(name):
            if name.endswith("_FILE"):
                secrets.setdefault(name.removesuffix("_FILE"), "file:" + os.environ[name])
            else:
                secrets.setdefault(name, "env:" + name)
        else:
            group, key = path.split(".", 1)
            settings.setdefault(group, {}).setdefault(key, os.environ[name])
        migrated.append(name)
    _validate(data, root=root)
    serialized = yaml.safe_dump(data, sort_keys=True)
    if len(serialized) > 64000:
        raise ValueError("configuration exceeds 64 KiB")
    if write:
        import tempfile

        from commontrace import paths

        paths.enforce_boundary(root, filename)
        paths.safe_prepare_output_path(filename)
        descriptor, temporary = tempfile.mkstemp(prefix=".commontrace-config-", dir=root)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, filename)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)
    return {"migrated_names": migrated, "written": write, "configuration": filename, "credentials_copied": False}
