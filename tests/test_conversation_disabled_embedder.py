"""Disabling embeddings works even on hosts with the optional models installed."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from commontrace.conversation import Options, Store, embed, recall
from commontrace.frameworks import MemoryTools


@pytest.fixture
def available_models_must_not_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed, "available", lambda: True)

    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("disabled embeddings constructed an encoder")

    monkeypatch.setattr(embed, "Embedder", unexpected)


@pytest.mark.parametrize("choice", [None, "none", "auto"])
@pytest.mark.parametrize("read_only", [False, True])
def test_disabled_recall_preserves_evidence_and_speaker_filter_without_loading_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, available_models_must_not_load: None,
    choice: str | None, read_only: bool,
) -> None:
    monkeypatch.setenv("COMMONTRACE_CONVERSATION_EMBEDDER", "none")
    with Store(str(tmp_path), "private") as store:
        store.add("session", [{"text": "The release codename is Kestrel.", "speaker": "owner@example.org"},
                              {"text": "The release codename is Falcon.", "speaker": "other@example.org"}])
    with Store(str(tmp_path), "private", read_only=read_only) as store:
        result = recall(store, "release codename", options=Options(
            embedder=choice, rerank=None, summaries=False, speakers=("OWNER@EXAMPLE.ORG",),
        ))
        assert "Kestrel" in result.context and "Falcon" not in result.context
        assert result.turns and result.tokens <= 1500
        assert not store._embedders
    assert not list((tmp_path / "memory").rglob("embeddings-*.db"))


@pytest.mark.parametrize("asynchronous", [False, True])
def test_native_framework_recall_with_explicit_none_on_embedding_capable_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, available_models_must_not_load: None, asynchronous: bool,
) -> None:
    monkeypatch.setenv("COMMONTRACE_CONVERSATION_EMBEDDER", "arctic-m")
    bundle = MemoryTools(str(tmp_path), "private", "session", options=Options(
        embedder="none", rerank=None, summaries=False,
    ))
    bundle.remember("What is the release name?", "The release name is Kestrel.")
    response = asyncio.run(bundle.arecall("release name")) if asynchronous else bundle.recall("release name")
    result = json.loads(response)
    assert "Kestrel" in result["context"] and result["turns"]
