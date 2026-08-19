"""Tests for memory/attention/query.py's handling of a tampered index.npz.

index.npz is a local build artifact, but nothing stops one arriving on a machine
via git clone/fork/sync rather than a local `build_index.py` run. Its `model_name`
field must not be trusted blindly -- see the comment in query.py for the CVE
context (a malicious model_name could point SentenceTransformer at an arbitrary,
code-executing Hugging Face Hub repo).
"""
import importlib
import os
import sys

import numpy as np
import pytest

pytest.importorskip("sentence_transformers")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "memory", "attention"))
import query as attn_query  # noqa: E402


def _write_index(path, model_name, n=2):
    np.savez(
        path,
        slugs=np.array([f"lesson_{i}" for i in range(n)]),
        embeddings=np.zeros((n, 3), dtype=np.float32),
        model_name=np.array(model_name),
        encoded_field=np.array("x"),
        timestamp=np.array("2026-01-01T00:00:00"),
        n_lessons=np.array(n),
    )


class TestTamperedModelName:
    def test_untrusted_model_name_is_rejected_without_loading(self, tmp_path, monkeypatch, capsys):
        index_path = tmp_path / "index.npz"
        _write_index(str(index_path), "attacker/malicious-repo")
        monkeypatch.setattr(attn_query, "INDEX_PATH", str(index_path))
        monkeypatch.setattr(attn_query, "LESSONS_DIR", str(tmp_path))

        called = []
        monkeypatch.setattr(
            attn_query, "SentenceTransformer", lambda name: called.append(name) or object()
        )
        monkeypatch.setattr(sys, "argv", ["query.py", "some task"])

        rc = attn_query.main()
        assert rc == 1
        assert called == []  # SentenceTransformer must never be constructed
        err = capsys.readouterr().err
        assert "attacker/malicious-repo" in err

    def test_trusted_model_name_still_loads(self, tmp_path, monkeypatch):
        index_path = tmp_path / "index.npz"
        _write_index(str(index_path), attn_query._TRUSTED_MODEL_NAME)
        monkeypatch.setattr(attn_query, "INDEX_PATH", str(index_path))
        monkeypatch.setattr(attn_query, "LESSONS_DIR", str(tmp_path))

        class FakeModel:
            def encode(self, *a, **k):
                return np.zeros(3, dtype=np.float32)

        called = []
        monkeypatch.setattr(
            attn_query, "SentenceTransformer", lambda name: called.append(name) or FakeModel()
        )
        monkeypatch.setattr(sys, "argv", ["query.py", "some task"])

        rc = attn_query.main()
        assert rc == 0
        assert called == [attn_query._TRUSTED_MODEL_NAME]
