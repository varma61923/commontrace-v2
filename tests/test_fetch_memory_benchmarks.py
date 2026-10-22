"""Pinned benchmark downloads are verified before they replace anything on disk."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import os

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "fetch_memory_benchmarks",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "fetch_memory_benchmarks.py"))
fetcher = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fetcher)


def _serve(body: bytes, seen: list):
    def opener(request, timeout):
        seen.append(request)
        return io.BytesIO(body)
    return opener


def test_every_source_is_pinned_to_an_exact_revision_and_digest():
    for name, (url, digest) in fetcher.DATASETS.items():
        assert url.startswith("https://") and "/main/" not in url, name
        assert len(digest) == 64 and int(digest, 16) >= 0, name


def test_a_matching_download_is_installed_and_reused(tmp_path, monkeypatch):
    body = b'[{"sample_id": "x"}]'
    monkeypatch.setitem(fetcher.DATASETS, "locomo10.json",
                        ("https://example.invalid/locomo10.json", hashlib.sha256(body).hexdigest()))
    seen: list = []
    path = fetcher.fetch("locomo10.json", str(tmp_path), opener=_serve(body, seen))
    assert open(path, "rb").read() == body and len(seen) == 1
    fetcher.fetch("locomo10.json", str(tmp_path), opener=_serve(b"never fetched", seen))
    assert len(seen) == 1


def test_a_tampered_download_is_refused_and_leaves_nothing(tmp_path, monkeypatch):
    monkeypatch.setitem(fetcher.DATASETS, "beam_100K.parquet",
                        ("https://huggingface.co/datasets/x/resolve/abc/y.parquet", "0" * 64))
    monkeypatch.setenv("HF_TOKEN", "hf_test")
    seen: list = []
    with pytest.raises(RuntimeError, match="does not match"):
        fetcher.fetch("beam_100K.parquet", str(tmp_path), opener=_serve(b"tampered", seen))
    assert os.listdir(tmp_path) == []
    assert seen[0].get_header("Authorization") == "Bearer hf_test"
