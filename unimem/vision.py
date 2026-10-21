"""Image captioning for multimodal ingestion (default OFF)."""
from __future__ import annotations

import mimetypes
import os

from unimem import llm as _llm_mod
from unimem.configuration import getenv as _configuration_getenv

VISION_PROVIDERS = ("anthropic", "openai-compatible", "ollama")

DEFAULT_PROMPT = "Describe this image in one or two sentences for a searchable memory index."

MAX_IMAGE_BYTES = 10 * 1024 * 1024

_ENABLE_ENV = "UNIMEM_VISION_ENABLED"
_TRUTHY = ("1", "true", "yes", "y", "on")


def _enabled(flag: bool | None) -> bool:
    if flag is not None:
        return bool(flag)
    return _configuration_getenv(_ENABLE_ENV, "").strip().lower() in _TRUTHY


def _sniff_image_mime(blob: bytes) -> str | None:
    # Magic-byte detection for the formats the stdlib `imghdr` module used to
    # recognise; `imghdr` was removed in Python 3.13, where importing it failed
    # silently and every extensionless JPEG/WebP/BMP/TIFF became octet-stream.
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if blob.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if blob.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(blob) >= 12 and blob.startswith(b"RIFF") and blob[8:12] == b"WEBP":
        return "image/webp"
    if blob.startswith(b"BM"):
        return "image/bmp"
    if blob.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    return None


def _mime_for(path: str, blob: bytes) -> str:
    guessed, _ = mimetypes.guess_type(path)
    if guessed and guessed.startswith("image/"):
        return guessed
    return _sniff_image_mime(blob) or "application/octet-stream"


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
            size = os.fstat(fh.fileno()).st_size
            # Bound allocation even for oversized images or files that grow
            # after the size check. One extra byte detects that growth.
            blob = fh.read(MAX_IMAGE_BYTES + 1)
    except (OSError, ValueError):
        return None
    except Exception:  # noqa: BLE001 - never raise from a caption helper
        return None

    mime = _mime_for(path, blob)
    metadata = _metadata(mime, max(size, len(blob)), path)

    if not _enabled(enabled):
        return metadata
    if max(size, len(blob)) > MAX_IMAGE_BYTES:
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
