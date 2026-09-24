"""commontrace/semantic_arm.py returns what the semantic subprocess returns.

The MCP server runs the reference script's `rank()` in-process, holding the
model and index between calls; `commontrace query` runs the same script as a
subprocess and parses what it prints. These pin that the two produce the
same slugs in the same order, that the model is loaded once, and that a
rebuilt index is picked up. A deterministic fake model stands in for the
sentence-transformer, as in tests/test_attention_query.py.
"""
from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stdout

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("sentence_transformers")

from commontrace import paths, semantic_arm  # noqa: E402
from commontrace.commands.query_cmd import _slugs_from_semantic_output  # noqa: E402

VOCAB = ["retry", "upload", "deploy", "kubernetes", "password", "refund"]


class FakeModel:
    """Bag-of-words over a tiny vocabulary: deterministic, and different
    queries rank lessons differently."""

    def encode(self, text, **_kw):
        words = str(text).lower().split()
        v = np.array([sum(w.startswith(t) for w in words) for t in VOCAB], dtype=np.float32) + 0.01
        return v / np.linalg.norm(v)


LESSONS = {
    "retry-uploads": ("retry the failed upload", 3),
    "safe-deploys": ("deploy to kubernetes safely", 5),
    "password-reset": ("verify before a password reset", 2),
    "refund-limits": ("refund above the limit", 4),
}


def _store(tmp_path):
    root = str(tmp_path / "store")
    ldir = paths.lessons_dir(root)
    os.makedirs(os.path.join(paths.memory_dir(root), "attention"), exist_ok=True)
    os.makedirs(ldir, exist_ok=True)
    for slug, (desc, imp) in LESSONS.items():
        with open(os.path.join(ldir, f"lesson_{slug}.md"), "w", encoding="utf-8") as fh:
            fh.write(f"---\nname: {slug}\ndescription: {desc}\nimportance: {imp}\n"
                     "status: active\n---\nbody\n")
    _write_index(root)
    return root


def _write_index(root, drop=()):
    model = FakeModel()
    slugs = [s for s in LESSONS if s not in drop]
    np.savez(
        semantic_arm.index_path(root),
        slugs=np.array(slugs),
        embeddings=np.stack([model.encode(LESSONS[s][0]) for s in slugs]),
        model_name=np.array("multi-qa-mpnet-base-dot-v1"),
        encoded_field=np.array("x"), timestamp=np.array("2026-01-01T00:00:00"),
        n_lessons=np.array(len(slugs)),
    )
    # Newer than every lesson file, as a real build would be.
    future = max(os.path.getmtime(os.path.join(paths.lessons_dir(root), f)) for f in
                 os.listdir(paths.lessons_dir(root))) + 5
    os.utime(semantic_arm.index_path(root), (future, future))


@pytest.fixture
def arm(monkeypatch):
    loads = []

    def fake_st(name):
        loads.append(name)
        return FakeModel()

    monkeypatch.setattr(semantic_arm, "_SCRIPT", None)
    monkeypatch.setattr(semantic_arm, "_MODEL", None)
    monkeypatch.setattr(semantic_arm, "_INDEX", {})
    script = semantic_arm._script(os.getcwd())
    monkeypatch.setattr(script, "SentenceTransformer", fake_st)
    monkeypatch.setattr(script, "_append_telemetry", lambda *a, **k: None)
    return script, loads


def _printed_slugs(script, root, query, top_k, monkeypatch):
    """What the subprocess would print, parsed the way the CLI parses it."""
    monkeypatch.setattr(script, "INDEX_PATH", semantic_arm.index_path(root))
    monkeypatch.setattr(script, "LESSONS_DIR", paths.lessons_dir(root))
    monkeypatch.setattr(sys, "argv", ["query.py", "--top-k", str(top_k), "--", query])
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert script.main() == 0
    return _slugs_from_semantic_output(buf.getvalue())


@pytest.mark.parametrize("query,top_k", [
    ("uploads keep failing so retry", 2), ("kubernetes deploy", 1), ("refund request", 3),
])
def test_in_process_ranking_is_what_the_subprocess_prints(tmp_path, arm, monkeypatch, query, top_k):
    script, _ = arm
    root = _store(tmp_path)
    rc, slugs, _warnings = semantic_arm.ranked_slugs(root, query, top_k)
    assert rc == 0
    assert slugs == _printed_slugs(script, root, query, top_k, monkeypatch)
    # The importance>=4 override is part of that output, as it is for the CLI.
    assert {"safe-deploys", "refund-limits"} <= set(slugs)


def test_the_model_is_loaded_once(tmp_path, arm):
    _script, loads = arm
    root = _store(tmp_path)
    for q in ("retry upload", "refund", "deploy"):
        assert semantic_arm.ranked_slugs(root, q, 2)[0] == 0
    assert loads == ["multi-qa-mpnet-base-dot-v1"]


def test_a_rebuilt_index_is_picked_up(tmp_path, arm):
    root = _store(tmp_path)
    _rc, before, _w = semantic_arm.ranked_slugs(root, "retry upload", 1)
    assert before[0] == "retry-uploads"
    _write_index(root, drop=("retry-uploads",))
    _rc, after, _w = semantic_arm.ranked_slugs(root, "retry upload", 1)
    assert "retry-uploads" not in after


def test_a_corrupt_index_is_an_error_not_a_crash(tmp_path, arm):
    root = _store(tmp_path)
    with open(semantic_arm.index_path(root), "wb") as fh:
        fh.write(b"not a zip")
    rc, slugs, warnings = semantic_arm.ranked_slugs(root, "retry", 2)
    assert rc != 0 and slugs == [] and "corrupted" in warnings[0]


# --- the index keeps itself current ------------------------------------------

class WideFakeModel(FakeModel):
    """FakeModel at the real model's width, so the reference builder (which
    writes EMBEDDING_DIM-wide rows) can use it."""

    def encode(self, text, **kw):
        if isinstance(text, (list, tuple)):
            return np.stack([self.encode(t) for t in text])
        v = np.zeros(768, dtype=np.float32)
        v[: len(VOCAB)] = super().encode(text)
        return v


@pytest.fixture
def wide_arm(monkeypatch):
    loads = []

    def fake_st(name):
        loads.append(name)
        return WideFakeModel()

    monkeypatch.setattr(semantic_arm, "_SCRIPT", None)
    monkeypatch.setattr(semantic_arm, "_BUILDER", None)
    monkeypatch.setattr(semantic_arm, "_MODEL", None)
    monkeypatch.setattr(semantic_arm, "_INDEX", {})
    script = semantic_arm._script(os.getcwd())
    monkeypatch.setattr(script, "SentenceTransformer", fake_st)
    builder = semantic_arm._builder(os.getcwd())
    monkeypatch.setattr(builder, "SentenceTransformer",
                        lambda name: pytest.fail("the builder must reuse the held model"))
    return loads


def _add_lesson(root, slug, desc, importance=3):
    path = os.path.join(paths.lessons_dir(root), f"lesson_{slug}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"---\nname: {slug}\ndescription: {desc}\nimportance: {importance}\n"
                 "status: active\n---\nbody\n")
    # The index was built a while ago; this lesson was approved since.
    past = os.path.getmtime(path) - 60
    os.utime(semantic_arm.index_path(root), (past, past))


def test_a_stale_index_is_refreshed_before_ranking(tmp_path, wide_arm):
    from commontrace.commands.query_cmd import _index_is_unusable

    root = str(tmp_path / "store")
    os.makedirs(paths.lessons_dir(root))
    os.makedirs(os.path.dirname(semantic_arm.index_path(root)))
    for slug, (desc, imp) in LESSONS.items():
        with open(os.path.join(paths.lessons_dir(root), f"lesson_{slug}.md"), "w",
                  encoding="utf-8") as fh:
            fh.write(f"---\nname: {slug}\ndescription: {desc}\nimportance: {imp}\n"
                     "status: active\n---\nbody\n")
    assert semantic_arm.ensure_fresh(root) == ""  # built from nothing
    _add_lesson(root, "refund-password-check", "password check before a refund")
    assert _index_is_unusable(root)  # the precondition: stale

    assert semantic_arm.ensure_fresh(root) == ""
    assert _index_is_unusable(root) == ""
    rc, slugs, _w = semantic_arm.ranked_slugs(root, "password refund", 1)
    assert rc == 0 and slugs[0] == "refund-password-check"
    assert wide_arm == ["multi-qa-mpnet-base-dot-v1"]  # one load, shared by build and rank


def test_the_cli_refreshes_a_stale_index_and_falls_back_only_if_that_fails(tmp_path, monkeypatch):
    from commontrace.commands import query_cmd

    state = {"stale": "lesson_x.md changed after the index was last built", "calls": 0}
    monkeypatch.setattr(query_cmd, "_index_is_unusable", lambda root: state["stale"])

    def build(root, relative, args, hint, capture=False):
        state["calls"] += 1
        state["stale"] = ""
        return 0, "Index built: 5 lessons (1 encoded, 4 reused from cache)\n"

    monkeypatch.setattr(query_cmd, "run_script", build)
    assert query_cmd._refresh_stale_index(str(tmp_path)) == ""
    assert state["calls"] == 1

    state["stale"] = "no semantic index has been built yet"
    monkeypatch.setattr(query_cmd, "run_script", lambda *a, **k: (1, ""))
    assert query_cmd._refresh_stale_index(str(tmp_path)) == "no semantic index has been built yet"
