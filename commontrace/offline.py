"""Air-gapped operation: refuse every non-loopback network call, and keep models cache-only.

Turn it on with ``commontrace --offline <command>`` or ``COMMONTRACE_OFFLINE=1``;
``commontrace --local <command>`` also selects the in-process local model.
Ingestion, lexical and (already cached) semantic retrieval, the causal engine,
the gateway and the console keep working. Hosted LLM providers, knowledge
connectors, web crawling and Hub sync refuse with a `CapabilityError` naming
the switch, rather than attempting a connection. A model runtime on loopback
(for example a local Ollama) stays reachable.

No DNS lookup is made to decide this: a host is local only when it is a
literal loopback address or ``localhost``.
"""
from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit

from commontrace.exceptions import CapabilityError

ENV = "COMMONTRACE_OFFLINE"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
# Read by huggingface_hub / transformers / datasets: load only what is already cached.
MODEL_HUB_SWITCHES = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE")


class OfflineError(CapabilityError):
    code = "offline_mode"
    default_remediation = "Unset COMMONTRACE_OFFLINE (or drop --offline) to allow network access."


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() in _TRUTHY


def enable() -> None:
    """Switch this process (and the children it starts) to offline mode."""
    os.environ[ENV] = "1"
    for name in MODEL_HUB_SWITCHES:
        os.environ[name] = "1"


def enable_local() -> None:
    """Keyless operation: offline mode plus the in-process model, unless a provider is configured.

    Ingestion, retrieval and measurement never need a model; LLM-assisted steps
    (drafting, reflection, contextualizing) then run on cached local weights.
    """
    enable()
    os.environ.setdefault("COMMONTRACE_LLM_PROVIDER", "local")


def apply_environment() -> None:
    """Propagate an inherited COMMONTRACE_OFFLINE to the model-hub switches."""
    if enabled():
        enable()


def is_loopback(url: str) -> bool:
    host = (urlsplit(url).hostname or "").strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_url(url: str, purpose: str) -> None:
    """Raise `OfflineError` when offline mode forbids reaching `url`."""
    if enabled() and not is_loopback(url):
        raise OfflineError(f"offline mode: {purpose} needs network access to a non-local host")


def status() -> dict:
    return {"offline": enabled(),
            "model_hub_cache_only": all(os.environ.get(n) == "1" for n in MODEL_HUB_SWITCHES)}
