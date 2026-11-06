"""Pinned benchmark downloads are verified before they replace anything on disk."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "fetch_memory_benchmarks",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "fetch_memory_benchmarks.py"),
)
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
    monkeypatch.setitem(
        fetcher.DATASETS, "locomo10.json", ("https://example.invalid/locomo10.json", hashlib.sha256(body).hexdigest())
    )
    seen: list = []
    path = fetcher.fetch("locomo10.json", str(tmp_path), opener=_serve(body, seen))
    assert Path(path).read_bytes() == body and len(seen) == 1
    fetcher.fetch("locomo10.json", str(tmp_path), opener=_serve(b"never fetched", seen))
    assert len(seen) == 1


def test_a_tampered_download_is_refused_and_leaves_nothing(tmp_path, monkeypatch):
    monkeypatch.setitem(
        fetcher.DATASETS, "beam_100K.parquet", ("https://huggingface.co/datasets/x/resolve/abc/y.parquet", "0" * 64)
    )
    monkeypatch.setenv("HF_TOKEN", "hf_test")
    seen: list = []
    with pytest.raises(RuntimeError, match="does not match"):
        fetcher.fetch("beam_100K.parquet", str(tmp_path), opener=_serve(b"tampered", seen))
    assert os.listdir(tmp_path) == []
    assert seen[0].get_header("Authorization") == "Bearer hf_test"


def test_concurrent_fetches_own_their_temporary_files(tmp_path, monkeypatch):
    body = b"pinned benchmark bytes"
    monkeypatch.setitem(
        fetcher.DATASETS, "locomo10.json", ("https://example.invalid/data", hashlib.sha256(body).hexdigest())
    )
    barrier = threading.Barrier(2)

    class Response(io.BytesIO):
        def read(self, size=-1):
            if self.tell() == 0:
                barrier.wait(timeout=10)
            return super().read(size)

    def download():
        return fetcher.fetch("locomo10.json", str(tmp_path), opener=lambda *_, **__: Response(body))

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(download) for _ in range(2)]
        paths = [future.result(timeout=15) for future in futures]
    assert paths[0] == paths[1]
    assert Path(paths[0]).read_bytes() == body
    assert sorted(p.name for p in tmp_path.iterdir()) == ["locomo10.json"]


def test_preexisting_partial_symlink_never_overwrites_another_file(tmp_path, monkeypatch):
    body = b"verified dataset"
    monkeypatch.setitem(
        fetcher.DATASETS, "locomo10.json", ("https://example.invalid/data", hashlib.sha256(body).hexdigest())
    )
    protected = tmp_path / "keep.txt"
    protected.write_bytes(b"private local data")
    (tmp_path / "locomo10.json.partial").symlink_to(protected)
    path = fetcher.fetch("locomo10.json", str(tmp_path), opener=_serve(body, []))
    assert protected.read_bytes() == b"private local data"
    assert not Path(path).is_symlink() and Path(path).read_bytes() == body


def test_interrupted_fetch_keeps_existing_target_and_cleans_its_partial(tmp_path):
    target = tmp_path / "locomo10.json"
    target.write_bytes(b"previous incomplete data")

    class Response(io.BytesIO):
        def read(self, size=-1):
            if self.tell():
                raise TimeoutError("interrupted read")
            return super().read(size)

    with pytest.raises(TimeoutError, match="interrupted"):
        fetcher.fetch("locomo10.json", str(tmp_path), opener=lambda *_, **__: Response(b"partial"))
    assert target.read_bytes() == b"previous incomplete data"
    assert list(tmp_path.iterdir()) == [target]
