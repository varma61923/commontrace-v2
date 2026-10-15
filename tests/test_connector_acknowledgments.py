"""Source checkpoints acknowledge durable scoped writes, including deletions."""
from __future__ import annotations

import os

import pytest

from commontrace import hierarchical, provenance
from commontrace.connectors.base import decode_state_token
from commontrace.connectors.local_dir import LocalDirConnector
from commontrace.connectors.web_crawler import WebCrawlerConnector


def document(path, text="Payments are retried three times before the operator receives an alert."):
    path.write_text("# Operations\n\n" + text * 3, encoding="utf-8")


def fail(*args, **kwargs):
    raise OSError("injected persistence failure")


@pytest.mark.parametrize("failure", ["write", "retire", "provenance"])
def test_failed_submission_remains_pending_and_resumes(tmp_path, monkeypatch, failure):
    source = tmp_path / "source"
    source.mkdir()
    document(source / "operations.md")
    root = str(tmp_path / "store")
    connector = LocalDirConnector(str(source))
    with monkeypatch.context() as patch:
        module, name = {
            "write": (hierarchical, "add_facts"),
            "retire": (hierarchical, "retire_source"),
            "provenance": (provenance, "append_provenance"),
        }[failure]
        patch.setattr(module, name, fail)
        failed = connector.sync(root)
    assert failed.result.errors
    assert failed.pending == 1
    assert decode_state_token(failed.new_state_token)["files"] == {}
    recovered = connector.sync(root, failed.new_state_token)
    assert not recovered.result.errors
    assert recovered.pending == 0
    assert recovered.result.chunks_extracted > 0
    assert hierarchical.list_facts(root, status="active")
    assert connector.sync(root).result.chunks_extracted == 0


def test_failed_update_keeps_prior_checkpoint(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    path = source / "operations.md"
    document(path)
    root = str(tmp_path / "store")
    connector = LocalDirConnector(str(source))
    original = connector.sync(root)
    document(path, "Payment events now retry five times with jitter before notifying operators.")
    os.utime(path, (2_000_000_000, 2_000_000_000))
    with monkeypatch.context() as patch:
        patch.setattr(hierarchical, "add_facts", fail)
        failed = connector.sync(root)
    assert failed.pending == 1
    assert failed.new_state_token == original.new_state_token
    recovered = connector.sync(root)
    assert recovered.pending == 0
    assert any("five times" in fact.statement for fact in hierarchical.list_facts(root, status="active"))


def test_failed_deletion_retries_until_source_retired(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    path = source / "operations.md"
    document(path)
    root = str(tmp_path / "store")
    connector = LocalDirConnector(str(source))
    original = connector.sync(root)
    path.unlink()
    with monkeypatch.context() as patch:
        patch.setattr(hierarchical, "retire_source", fail)
        failed = connector.sync(root)
    assert failed.pending == 1
    assert failed.new_state_token == original.new_state_token
    assert hierarchical.list_facts(root, status="active")
    assert connector.sync(root).pending == 0
    assert not hierarchical.list_facts(root, status="active")


def test_same_source_has_independent_scoped_checkpoints_and_retirement(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    path = source / "operations.md"
    document(path)
    root = str(tmp_path / "store")
    connector = LocalDirConnector(str(source))
    one = connector.sync(root, scope="tenant:one")
    two = connector.sync(root, scope="tenant:two")
    assert one.result.facts_written and two.result.facts_written
    path.unlink()
    assert connector.sync(root, scope="tenant:one").pending == 0
    remaining = hierarchical.list_facts(root, status="active")
    assert remaining and all(fact.scopes == ["tenant:two"] for fact in remaining)
    with pytest.raises(ValueError, match="source and owner scope"):
        connector.sync(root, one.new_state_token, scope="tenant:two")


def test_explicit_cursor_cannot_skip_another_directory(tmp_path):
    root = str(tmp_path / "store")
    for name in ("one", "two"):
        source = tmp_path / name
        source.mkdir()
        document(source / "operations.md")
    first = LocalDirConnector(str(tmp_path / "one")).sync(root)
    with pytest.raises(ValueError, match="source and owner scope"):
        LocalDirConnector(str(tmp_path / "two")).sync(root, first.new_state_token)


def test_crawler_retries_write_failure_and_isolates_scopes(tmp_path, monkeypatch):
    root = str(tmp_path / "store")
    crawler = WebCrawlerConnector()
    monkeypatch.setattr(crawler, "_allowed", lambda _url: True)
    monkeypatch.setattr(crawler, "_fetch_text", lambda _url: "# Operations\n\n" +
                        "Invoices must include the purchase order before the payment queue processes them. " * 3)
    url = "https://example.com/operations"
    with monkeypatch.context() as patch:
        patch.setattr(hierarchical, "add_facts", fail)
        failed = crawler.sync(root, urls=[url], scope="tenant:one")
    assert failed.pending == 1
    assert decode_state_token(failed.new_state_token)["visited"] == []
    recovered = crawler.sync(root, urls=[url], scope="tenant:one")
    assert recovered.pending == 0 and recovered.result.facts_written
    other = crawler.sync(root, urls=[url], scope="tenant:two")
    assert other.result.facts_written
    with pytest.raises(ValueError, match="source and owner scope"):
        crawler.sync(root, recovered.new_state_token, urls=[url], scope="tenant:two")


def test_local_read_failure_preserves_existing_facts_and_checkpoint(tmp_path, monkeypatch):
    from commontrace.connectors import local_dir

    source = tmp_path / "source"
    source.mkdir()
    path = source / "operations.md"
    document(path)
    root = str(tmp_path / "store")
    connector = LocalDirConnector(str(source))
    before = connector.sync(root)
    document(path, "Payment events now retry five times with jitter before notifying operators.")
    with monkeypatch.context() as patch:
        patch.setattr(local_dir, "_read_text", fail)
        failed = connector.sync(root)
    assert failed.result.errors and failed.pending == 1
    assert failed.new_state_token == before.new_state_token
    assert hierarchical.list_facts(root, status="active")
    assert connector.sync(root).pending == 0


@pytest.mark.parametrize("delete_before_upgrade", [False, True])
@pytest.mark.parametrize("initial_scan_failure", [False, True])
def test_legacy_scoped_sources_retire_without_touching_other_owners(
        tmp_path, monkeypatch, delete_before_upgrade, initial_scan_failure):
    from commontrace.connectors import local_dir
    from commontrace.connectors.base import chunk_markdown_text, record_chunks, save_connector_state
    from commontrace.ingest import IngestionResult

    source = tmp_path / "source"
    source.mkdir()
    path = source / "operations.md"
    document(path)
    root = str(tmp_path / "store")
    connector = LocalDirConnector(str(source))
    chunks = chunk_markdown_text(path.read_text(encoding="utf-8"), str(path))
    for scope in ("tenant:one", "tenant:two"):
        record_chunks(root, chunks, source_id="local_dir:" + str(path), scope=scope,
                      run_id="legacy", connector="local_dir",
                      result=IngestionResult(source_path=str(path), source_type="local_dir"))
    stamp = path.stat()
    save_connector_state(root, connector._key_for(str(source)), "", {
        "files": {path.name: {"mtime": stamp.st_mtime, "size": float(stamp.st_size)}}})
    token = None
    if delete_before_upgrade:
        path.unlink()
    if initial_scan_failure:
        with monkeypatch.context() as patch:
            patch.setattr(local_dir, "_scan_markdown_files", fail)
            failed = connector.sync(root, scope="tenant:one")
        assert failed.pending and failed.result.errors
        token = failed.new_state_token
    if not delete_before_upgrade:
        assert connector.sync(root, token, scope="tenant:one").result.chunks_extracted
        path.unlink()
        token = None
    assert connector.sync(root, token, scope="tenant:one").pending == 0
    remaining = hierarchical.list_facts(root, status="active")
    assert remaining and all(fact.scopes == ["tenant:two"] for fact in remaining)


def test_document_pipeline_checkpoints_are_per_destination(tmp_path):
    from commontrace.conversation import Store
    from commontrace.ingest.pipeline import create_document_pipeline

    path = tmp_path / "operations.md"
    document(path)
    root = str(tmp_path / "store")
    for scope in ("tenant:one", "tenant:two"):
        first = create_document_pipeline(str(path), root, scope=scope, contextualize="none").run()
        assert first.facts_written
        again = create_document_pipeline(str(path), root, scope=scope, contextualize="none").run()
        assert again.chunks_extracted == 0
    assert {tuple(fact.scopes) for fact in hierarchical.list_facts(root, status="active")} == {
        ("tenant:one",), ("tenant:two",)}
    for space in ("one", "two"):
        first = create_document_pipeline(str(path), root, space=space, contextualize="none").run()
        assert first.chunks_extracted
        with Store(root, space) as store:
            assert store.sessions()
        again = create_document_pipeline(str(path), root, space=space, contextualize="none").run()
        assert again.chunks_extracted == 0


def test_same_size_change_with_preserved_mtime_is_not_skipped(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    path = source / "operations.md"
    document(path)
    root = str(tmp_path / "store")
    connector = LocalDirConnector(str(source))
    connector.sync(root)
    stamp = path.stat()
    path.write_text(path.read_text().replace("three", "seven"), encoding="utf-8")
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert path.stat().st_size == stamp.st_size
    assert connector.sync(root).result.chunks_extracted
    assert any("seven times" in fact.statement for fact in hierarchical.list_facts(root, status="active"))


def test_source_change_during_read_keeps_prior_generation_pending(tmp_path, monkeypatch):
    from commontrace.connectors import local_dir

    source = tmp_path / "source"
    source.mkdir()
    path = source / "operations.md"
    document(path)
    root = str(tmp_path / "store")
    read = local_dir._read_text

    def changing_read(filename):
        text = read(filename)
        document(path, "Payment events now retry five times with jitter before notifying operators.")
        return text

    with monkeypatch.context() as patch:
        patch.setattr(local_dir, "_read_text", changing_read)
        result = LocalDirConnector(str(source)).sync(root)
    assert result.pending == 1 and result.result.errors
    assert not hierarchical.list_facts(root, status="active")
    assert LocalDirConnector(str(source)).sync(root).pending == 0


@pytest.mark.parametrize("failure", ["stat", "subdirectory"])
def test_incomplete_enumeration_never_acknowledges_deletion(tmp_path, monkeypatch, failure):
    source = tmp_path / "source"
    nested = source / "nested"
    nested.mkdir(parents=True)
    path = nested / "operations.md"
    document(path)
    root = str(tmp_path / "store")
    connector = LocalDirConnector(str(source))
    before = connector.sync(root)
    original_stat, original_scandir = os.stat, os.scandir

    def inaccessible_stat(filename, *args, **kwargs):
        if str(filename) == str(path):
            raise PermissionError("source metadata temporarily inaccessible")
        return original_stat(filename, *args, **kwargs)

    def inaccessible_scandir(filename):
        if str(filename) == str(nested):
            raise PermissionError("source subtree temporarily inaccessible")
        return original_scandir(filename)

    with monkeypatch.context() as patch:
        patch.setattr(os, "stat" if failure == "stat" else "scandir",
                      inaccessible_stat if failure == "stat" else inaccessible_scandir)
        failed = connector.sync(root)
    assert failed.pending == 1 and failed.result.errors
    assert failed.new_state_token == before.new_state_token
    assert hierarchical.list_facts(root, status="active")
    assert connector.sync(root).pending == 0
