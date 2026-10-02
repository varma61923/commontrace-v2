"""Connector auto-sync framework tests.

Covers the ABC contract, state-token resume, secret redaction on fetched
content, and connector dry-run (zero writes).
"""
from __future__ import annotations

import argparse
import json
import os

from commontrace.commands import sync_cmd
from commontrace.connectors.base import (
    Connector,
    SyncResult,
    decode_state_token,
    encode_state_token,
    load_connector_state,
)
from commontrace.connectors.local_dir import LocalDirConnector
from commontrace.connectors.web_crawler import WebCrawlerConnector


def _write_md(path, heading, body):
    path.write_text("# %s\n\n%s\n" % (heading, body), encoding="utf-8")


def _state_file(root):
    return os.path.join(root, "memory", "connectors", "state.json")


def _facts_file(root):
    return os.path.join(root, "memory", "facts", "facts.jsonl")


def _prov_file(root):
    return os.path.join(root, "memory", "graph", "provenance.jsonl")


class TestABCContract:
    def test_both_connectors_implement_the_contract(self):
        for cls in (LocalDirConnector, WebCrawlerConnector):
            assert issubclass(cls, Connector)
            for method in ("authorize", "sync", "webhook_handler"):
                assert callable(getattr(cls, method))
                assert getattr(cls, method) is not getattr(Connector, method)

    def test_authorize_returns_ok_dict(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        auth = LocalDirConnector(source_dir=str(src)).authorize()
        assert auth["ok"] is True
        assert LocalDirConnector(source_dir="/nonexistent-xyz").authorize()["ok"] is False
        assert WebCrawlerConnector().authorize()["ok"] is True

    def test_webhook_handlers_never_raise_on_bad_input(self, tmp_path):
        root = str(tmp_path / "store")
        assert LocalDirConnector(source_dir="/tmp").webhook_handler(root, {})["ok"] is False
        assert LocalDirConnector(source_dir="/tmp").webhook_handler(root, "nope")["ok"] is False
        assert WebCrawlerConnector().webhook_handler(root, {})["ok"] is False
        queued = WebCrawlerConnector().webhook_handler(root, {"url": "https://example.com/x"})
        assert queued["ok"] is True and queued["queued"].endswith("/x")

    def test_state_tokens_round_trip(self):
        cursor = {"files": {"a.md": {"mtime": 1.0, "size": 5.0}}}
        token = encode_state_token(cursor)
        assert isinstance(token, str) and token
        assert decode_state_token(token) == cursor
        assert decode_state_token("!!!not-a-token!!!") == {}
        assert decode_state_token(None) == {}


class TestStateTokenResume:
    def test_second_sync_with_no_changes_syncs_nothing(self, tmp_path):
        root = str(tmp_path / "store")
        src = tmp_path / "src"
        src.mkdir()
        _write_md(src / "a.md", "Guide",
                  "This guide explains the widget workflow in detail with many words. " * 4)
        conn = LocalDirConnector()
        first = conn.sync(root, source_dir=str(src), scope="test")
        assert isinstance(first, SyncResult)
        assert first.result.chunks_extracted > 0
        assert first.new_state_token
        # Persisted to git-tracked JSON under memory/.
        assert os.path.exists(_state_file(root))
        with open(_state_file(root), encoding="utf-8") as fh:
            assert json.load(fh)  # valid JSON map

        second = conn.sync(root, first.new_state_token, source_dir=str(src), scope="test")
        assert second.result.chunks_extracted == 0

    def test_changed_file_is_picked_up_on_resume(self, tmp_path):
        root = str(tmp_path / "store")
        src = tmp_path / "src"
        src.mkdir()
        _write_md(src / "a.md", "Guide",
                  "This guide explains the widget workflow in detail with many words. " * 4)
        conn = LocalDirConnector()
        first = conn.sync(root, source_dir=str(src))
        assert first.result.chunks_extracted > 0
        # Touch the file with new content + newer mtime.
        target = src / "a.md"
        _write_md(target, "Guide",
                  "This guide explains the widget workflow plus a brand new section. " * 6)
        os.utime(target, (first.result.chunks_extracted + 10_000, first.result.chunks_extracted + 10_000))
        second = conn.sync(root, first.new_state_token, source_dir=str(src))
        assert second.result.chunks_extracted > 0

    def test_persisted_state_resumes_without_explicit_token(self, tmp_path):
        root = str(tmp_path / "store")
        src = tmp_path / "src"
        src.mkdir()
        _write_md(src / "a.md", "Guide",
                  "This guide explains the widget workflow in detail with many words. " * 4)
        conn = LocalDirConnector()
        conn.sync(root, source_dir=str(src))
        resumed = LocalDirConnector().sync(root, source_dir=str(src))
        assert resumed.result.chunks_extracted == 0

    def test_crawler_skips_visited_urls_on_resume(self, tmp_path, monkeypatch):
        root = str(tmp_path / "store")
        conn = WebCrawlerConnector()
        monkeypatch.setattr(conn, "_allowed", lambda url: True)
        monkeypatch.setattr(
            conn, "_fetch_text",
            lambda url: "# Page\n\nBody text with enough words to chunk properly. " * 6,
        )
        first = conn.sync(root, urls=["https://example.com/a"])
        assert first.result.chunks_extracted > 0
        entry = load_connector_state(root, "web_crawler:default")
        assert entry.get("state_token")
        second = conn.sync(root, first.new_state_token, urls=["https://example.com/a"])
        assert second.result.chunks_extracted == 0


class TestRedaction:
    def test_fetched_secrets_are_redacted(self, tmp_path, monkeypatch):
        root = str(tmp_path / "store")
        secret = "sk-" + "A" * 32
        conn = WebCrawlerConnector()
        monkeypatch.setattr(conn, "_allowed", lambda url: True)
        monkeypatch.setattr(
            conn, "_fetch",
            lambda url: "<html><h1>Keys</h1><p>deploy key %s donot share</p><p>%s</p></html>"
                       % (secret, "filler words for chunk length. " * 10),
        )
        result = conn.sync(root, urls=["https://example.com/keys"])
        assert result.result.chunks_extracted > 0
        for chunk in result.chunks:
            assert secret not in chunk.content
        assert any("[REDACTED]" in c.content for c in result.chunks)


class TestDryRun:
    def test_local_dir_dry_run_writes_nothing_but_reports_chunks(self, tmp_path):
        root = str(tmp_path / "store")
        src = tmp_path / "src"
        src.mkdir()
        _write_md(src / "a.md", "Guide",
                  "This guide explains the widget workflow in detail with many words. " * 4)
        conn = LocalDirConnector()
        result = conn.sync(root, source_dir=str(src), dry_run=True)
        assert result.dry_run is True
        assert result.result.chunks_extracted > 0
        assert not os.path.exists(_state_file(root))
        assert not os.path.exists(_facts_file(root))
        assert not os.path.exists(_prov_file(root))

    def test_crawler_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        root = str(tmp_path / "store")
        conn = WebCrawlerConnector()
        monkeypatch.setattr(conn, "_allowed", lambda url: True)
        monkeypatch.setattr(
            conn, "_fetch_text",
            lambda url: "# Page\n\nBody text with enough words to chunk properly. " * 6,
        )
        result = conn.sync(root, urls=["https://example.com/a"], dry_run=True)
        assert result.result.chunks_extracted > 0
        assert not os.path.exists(_state_file(root))
        assert not os.path.exists(_facts_file(root))
        assert not os.path.exists(_prov_file(root))

    def test_sync_cli_dry_run(self, tmp_path, capsys):
        root = str(tmp_path / "store")
        src = tmp_path / "src"
        src.mkdir()
        _write_md(src / "a.md", "Guide",
                  "This guide explains the widget workflow in detail with many words. " * 4)
        args = argparse.Namespace(
            connector="local_dir", source=[str(src)], scope="test", dry_run=True,
            run_id="", state_token=None, max_files=200, max_pages=20, timeout=10,
            dest=root, push=False, pull=False, push_traces=False, query="", tags="",
            hub_url=None, hub_api_key=None, quiet=False, fail_if_unconfigured=False,
        )
        assert sync_cmd.run(args) == 0
        out = capsys.readouterr().out
        assert "local_dir" in out and "dry-run" in out
        assert not os.path.exists(_state_file(root))

    def test_sync_cli_requires_source(self, tmp_path, capsys):
        args = argparse.Namespace(
            connector="local_dir", source=[], scope="", dry_run=True,
            run_id="", state_token=None, max_files=200, max_pages=20, timeout=10,
            dest=str(tmp_path / "store"), push=False, pull=False, push_traces=False,
            query="", tags="", hub_url=None, hub_api_key=None, quiet=False,
            fail_if_unconfigured=False,
        )
        assert sync_cmd.run(args) == 2

    def test_provenance_carries_source_path_and_run_id(self, tmp_path):
        root = str(tmp_path / "store")
        src = tmp_path / "src"
        src.mkdir()
        _write_md(src / "a.md", "Guide",
                  "This guide explains the widget workflow in detail with many words. " * 4)
        result = LocalDirConnector().sync(root, source_dir=str(src), run_id="run-123")
        assert result.result.chunks_extracted > 0
        with open(_prov_file(root), encoding="utf-8") as fh:
            records = [json.loads(line) for line in fh if line.strip()]
        assert records
        assert all(r["run_id"] == "run-123" for r in records)
        assert all(r["source_path"] for r in records)
