"""One safe YAML configuration with local/team/server profiles.

Existing explicit environment settings take precedence. Advanced legacy settings
remain supported; the short primary configuration is the recommended interface.
"""

from __future__ import annotations

import os
from contextlib import contextmanager

import yaml

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


def load(root: str, *, profile: str | None = None) -> dict:
    filename = os.path.join(root, "commontrace.yaml")
    data = {}
    if os.path.exists(filename):
        with open(filename, encoding="utf-8") as stream:
            raw = stream.read(64001)
        if len(raw) > 64000:
            raise ValueError("configuration exceeds 64 KiB")
        data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict) or set(data) - set(DEFAULTS):
        raise ValueError("unsupported configuration keys")
    result = {**DEFAULTS, **data}
    selected = profile or result["profile"]
    if selected not in ("local", "team", "server"):
        raise ValueError("configuration profile must be local, team or server")
    profiles = result["profiles"]
    if not isinstance(profiles, dict) or any(k not in ("local", "team", "server") for k in profiles):
        raise ValueError("unsupported configuration profiles")
    options = {**DEFAULTS["profiles"][selected], **profiles.get(selected, {})}
    if set(options) - set(PRIMARY) or any(not isinstance(v, (str, bool, int)) for v in options.values()):
        raise ValueError("unsupported profile options")
    if not isinstance(result["agents"], dict) or not isinstance(result["histories"], list):
        raise ValueError("agents must be a mapping and histories a list")
    if not isinstance(result["host"], str) or type(result["port"]) is not int or not 1 <= result["port"] <= 65535:
        raise ValueError("host and port are invalid")
    routes = result["model_routes"]
    if (
        not isinstance(routes, dict)
        or len(routes) > 16
        or any(not isinstance(k, str) or not isinstance(v, str) or not 0 < len(v) <= 128 for k, v in routes.items())
    ):
        raise ValueError("model_routes maps purpose names to bounded model names")
    from commontrace.llm_runtime import Budget

    if not isinstance(result["llm_budget"], dict):
        raise ValueError("llm_budget must be a mapping")
    try:
        Budget(**result["llm_budget"])
    except TypeError as exc:
        raise ValueError("unsupported LLM budget options") from exc
    result.update(profile=selected, options=options)
    return result


@contextmanager
def apply(config: dict):
    inserted = []
    try:
        for key, value in config["options"].items():
            for variable in PRIMARY[key]:
                if variable not in os.environ:
                    os.environ[variable] = ("1" if value else "0") if isinstance(value, bool) else str(value)
                    inserted.append(variable)
        yield config
    finally:
        for variable in inserted:
            os.environ.pop(variable, None)
