from __future__ import annotations

import os


def env_secret(name: str, default: str = "") -> str:
    file_path = os.environ.get(f"{name}_FILE")
    if not file_path:
        return os.environ.get(name, default)
    try:
        with open(file_path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError as exc:
        raise RuntimeError(
            f"{name}_FILE='{file_path}' is set but could not be read: {exc}"
        ) from None
