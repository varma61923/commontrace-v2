"""Image captioning for multimodal ingestion (default OFF)."""
from __future__ import annotations

import mimetypes
import os

from commontrace import llm as _llm_mod

VISION_PROVIDERS = ("anthropic", "openai-compatible", "ollama")

DEFAULT_PROMPT = "Describe this image in one or two sentences for a searchable memory index."

MAX_IMAGE_BYTES = 10 * 1024 * 1024

_ENABLE_ENV = "COMMONTRACE_VISION_ENABLED"
_TRUTHY = ("1", "true", "yes", "y", "on")


def _enabled(flag: bool | None) -> bool:
    if flag is not None:
        return bool(flag)
    return os.environ.get(_ENABLE_ENV, "").strip().lower() in _TRUTHY


def _mime_for(path: str, blob: bytes) -> str:
    guessed, _ = mimetypes.guess_type(path)
    if guessed and guessed.startswith("image/"):
        return guessed
    try:
        import imghdr

        kind = imghdr.what(None, h=blob)
    except Exception:  # noqa: BLE001 - detection is best-effort
        kind = None
    if kind:
        return f"image/{'jpeg' if kind == 'jpg' else kind}"
    if blob[:8].startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if blob[:3] == b"GIF":
        return "image/gif"
    return "application/octet-stream"


def _metadata(mime: str, size: int, path: str) -> str:
    name = os.path.basename(path)
    return f"image {name} ({mime}, {size} bytes)"


def vision_enabled(flag: bool | None = None) -> bool:
    """Whether an LLM caption may be attempted (explicit flag wins, else env)."""
    return _enabled(flag)


def describe_image(
    path: str,
    prompt: str = DEFAULT_PROMPT,
    *,
    config: _llm_mod.Config | None = None,
    enabled: bool | None = None,
) -> str | None:
    """Describe the image at `path`, or return metadata when vision is off."""
    try:
        with open(path, "rb") as fh:
            blob = fh.read()
    except (OSError, ValueError):
        return None
    except Exception:  # noqa: BLE001 - never raise from a caption helper
        return None

    mime = _mime_for(path, blob)
    metadata = _metadata(mime, len(blob), path)

    if not _enabled(enabled):
        return metadata
    if len(blob) > MAX_IMAGE_BYTES:
        return metadata
    try:
        cfg = config or _llm_mod.load_config()
    except Exception:  # noqa: BLE001 - no credentials, no caption
        return metadata
    provider = "openai-compatible" if cfg.provider == "ollama" else cfg.provider
    if provider not in VISION_PROVIDERS:
        return metadata
    try:
        text, _usage = _llm_mod.complete_with_image(cfg, prompt, blob, mime)
    except Exception:  # noqa: BLE001 - an LLM outage is metadata, not a crash
        return metadata
    text = (text or "").strip()
    return text if text else metadata
