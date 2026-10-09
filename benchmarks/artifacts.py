"""Content bindings for optional local benchmark libraries and cached models."""
from __future__ import annotations

import hashlib
import importlib
import os


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def package_source(package):
    module = importlib.import_module(package)
    root = os.path.dirname(module.__file__)
    digest = hashlib.sha256()
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(files):
            if name.endswith(".py"):
                path = os.path.join(directory, name)
                digest.update(os.path.relpath(path, root).encode() + b"\0")
                digest.update(bytes.fromhex(file_sha256(path)))
    return digest.hexdigest()


def model_artifact(name):
    from huggingface_hub import try_to_load_from_cache

    # Read local artifacts only. Never fetch a model while producing metadata.
    config = try_to_load_from_cache(name, "config.json")
    if not isinstance(config, str):
        raise ValueError("embedding profile lacks a cached model configuration")
    directory = os.path.dirname(config)
    artifacts = {}
    for root, dirs, files in os.walk(directory):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for filename in sorted(files):
            path = os.path.join(root, filename)
            artifacts[os.path.relpath(path, directory).replace(os.sep, "/")] = file_sha256(path)
    if not any(name in artifacts for name in ("model.safetensors", "pytorch_model.bin")):
        raise ValueError("embedding profile lacks cached model weights")
    return {"model": name, "revision": os.path.basename(directory), "artifacts_sha256": artifacts}
