"""Ledger resumption + manifest accounting for every ingestor, and fingerprint equivalence.

The hardcoded digests below were generated from the pre-move implementations in
hub/crud.py and commontrace/hub_client.py; they prove the move to
commontrace.fingerprints kept every output identical.
"""
from __future__ import annotations

import json
import os
import struct

import pytest

from commontrace import fingerprints as fp
from commontrace.ingest import pipeline as pl

CONTRIBUTE_DIGEST = "9e0dfed304997679be8297ba998266284111b4482ba82a7f9ff6d864496fad9c"
CONTRIBUTE_EMPTY_DIGEST = "0d92436fde5e0860c31b00ca5c43915647581a8a48d3671766af8dba7a2e1bc6"
AMEND_DIGEST = "32ac7712a6742727051669a7a4aa11e1d00bde6d97255966149c6968130220f4"
AMEND_NONE_DIGEST = "7544d8fe0cd59e8f6f832287150de9f5f64df7e0cc5a9d3950091af4b10f66d2"
PUSH_DIGEST = "6a2fd51561c907db80846f30a4f7f65ee632f0d6b334bd596820ed220daf981a"
TRACE_PUSH_DIGEST = "60692f74510103929f8f99caef29a806578368198351c228c94106026e8b5001"
CONTENT_NORM_DIGEST = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
SHORT_DIGEST = "ba7816bf8f01cfea"
MD5_DIGEST = "900150983cd24fb0d6963f7d28e17f72"
FILE_DIGEST = "sha256:35e9dba52033ffeaa9b127e9d367e8901c49f54fd7a39c0e57d601e6acc0f974"


@pytest.fixture()
def store(tmp_path):
    root = str(tmp_path / "store")
    from commontrace import paths

    os.makedirs(paths.lessons_dir(root), exist_ok=True)
    os.makedirs(paths.traces_dir(root), exist_ok=True)
    return root


class TestFingerprintEquivalence:
    def test_request_hashes_match_premove_digests(self):
        assert fp.contribute_request_hash("T", "C", "S", ["b", "a"], "agent", {"k": 1}, "prof") == CONTRIBUTE_DIGEST
        assert fp.contribute_request_hash("T", "C", "S", [], "") == CONTRIBUTE_EMPTY_DIGEST
        assert fp.amend_request_hash("id-1", "T", "C", "S", ["t"], {"r": True}) == AMEND_DIGEST
        assert fp.amend_request_hash("id-1", None, None, None, None, None) == AMEND_NONE_DIGEST
        assert fp.push_fingerprint("T", "C", "S", ["t1", "t2"]) == PUSH_DIGEST
        assert fp.trace_push_fingerprint("T", "C", "S", ["t"], {"r": False}) == TRACE_PUSH_DIGEST

    def test_content_hashes_match_premove_digests(self):
        assert fp.content_hash("  Hello   WORLD\n") == CONTENT_NORM_DIGEST
        assert fp.content_hash("hello world") == CONTENT_NORM_DIGEST
        assert fp.short_fingerprint("abc") == SHORT_DIGEST
        assert fp.md5_hex("abc") == MD5_DIGEST

    def test_file_fingerprint_matches_premove_digest(self, tmp_path):
        target = tmp_path / "hello.txt"
        target.write_text("hello ledger", encoding="utf-8")
        assert fp.file_fingerprint(str(target)) == FILE_DIGEST
        assert pl.file_fingerprint(str(target)) == FILE_DIGEST

    def test_old_call_sites_agree_with_fingerprints(self):
        import sys

        sys.path.insert(0, "hub")
        try:
            from commontrace import hub_client
            from hub import crud
        finally:
            sys.path.remove("hub")
        assert crud._contribute_request_hash("T", "C", "S", ["b", "a"], "agent", {"k": 1}, "prof") == CONTRIBUTE_DIGEST
        assert crud._amend_request_hash("id-1", "T", "C", "S", ["t"], {"r": True}) == AMEND_DIGEST
        assert hub_client._push_fingerprint("T", "C", "S", ["t1", "t2"]) == PUSH_DIGEST
        assert hub_client._trace_push_fingerprint("T", "C", "S", ["t"], {"r": False}) == TRACE_PUSH_DIGEST


class TestIngestorResumption:
    def test_code_resume_skips_unchanged(self, store, tmp_path):
        from commontrace.ingest import ingest_code_repository

        src = tmp_path / "src"
        src.mkdir()
        (src / "m.py").write_text('"""Helpers."""\n\ndef run():\n    """Run it."""\n    return 1\n', encoding="utf-8")
        first = ingest_code_repository(store, str(src))
        assert first.chunks_extracted > 0
        second = ingest_code_repository(store, str(src))
        assert second.chunks_extracted == 0 and second.graph_nodes_written == 0
        assert second.skipped_unchanged == 1
        forced = ingest_code_repository(store, str(src), force=True)
        assert forced.chunks_extracted == first.chunks_extracted and forced.skipped_unchanged == 0

    def test_markdown_resume_skips_unchanged(self, store, tmp_path):
        from commontrace.ingest import ingest_markdown_documentation

        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "guide.md").write_text("# Architecture\n\nPrefer async IO for all service calls everywhere. "
                                        "This keeps tail latency low under load.\n",
                                       encoding="utf-8")
        first = ingest_markdown_documentation(store, str(docs), scope="arch")
        assert first.facts_written > 0
        second = ingest_markdown_documentation(store, str(docs), scope="arch")
        assert second.facts_written == 0 and second.skipped_unchanged == 1

    def test_json_logs_resume_skips_unchanged(self, store, tmp_path):
        from commontrace.ingest import ingest_json_logs

        log = tmp_path / "svc.jsonl"
        log.write_text(
            json.dumps({"level": "ERROR", "message": "timeout to payments service"}) + "\n"
            + json.dumps({"level": "ERROR", "message": "timeout to payments service"}) + "\n",
            encoding="utf-8",
        )
        first = ingest_json_logs(store, str(log), scope="pay")
        assert first.traces_written == 1
        second = ingest_json_logs(store, str(log), scope="pay")
        assert second.traces_written == 0 and second.skipped_unchanged == 1

    def test_transcript_resume_skips_unchanged(self, store, tmp_path):
        from commontrace.ingest import ingest_failure_transcript

        run = tmp_path / "run.jsonl"
        run.write_text(json.dumps({"step_index": 1, "status": "ERROR", "content": "database is unreachable"}) + "\n",
                       encoding="utf-8")
        first = ingest_failure_transcript(store, str(run))
        assert first.traces_written == 1
        second = ingest_failure_transcript(store, str(run))
        assert second.traces_written == 0 and second.skipped_unchanged == 1

    def test_triples_file_resume_skips_unchanged(self, store, tmp_path):
        from commontrace.ingest import ingest_fact_triples

        path = tmp_path / "triples.json"
        path.write_text(json.dumps([{"subject": "svc:api", "predicate": "depends_on", "object": "svc:db"}]),
                        encoding="utf-8")
        first = ingest_fact_triples(str(path), store)
        assert first.facts_written == 1
        second = ingest_fact_triples(str(path), store)
        assert second.facts_written == 0 and second.skipped_unchanged == 1

    def test_triples_list_input_still_works_without_ledger(self, store):
        from commontrace.ingest import ingest_fact_triples

        res = ingest_fact_triples([{"subject": "svc:a", "predicate": "depends_on", "object": "svc:b"}], store)
        assert res.facts_written == 1

    def test_multimodal_resume_skips_unchanged(self, store, tmp_path):
        from commontrace.ingest import ingest_multimodal_document

        page = tmp_path / "page.html"
        page.write_text("<html><head><title>Runbook</title></head><body><p>" + ("Retry with backoff. " * 40)
                        + "</p></body></html>", encoding="utf-8")
        first = ingest_multimodal_document(store, str(page))
        assert first.chunks_extracted > 0
        second = ingest_multimodal_document(store, str(page))
        assert second.chunks_extracted == 0 and second.skipped_unchanged == 1


class TestManifestAccounting:
    def test_manifest_round_trips(self, store, tmp_path):
        from commontrace import _jsonl
        from commontrace.ingest import ingest_markdown_documentation

        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "a.md").write_text("# Guide\n\nPrefer async IO for all service calls everywhere.\n", encoding="utf-8")
        ingest_markdown_documentation(store, str(docs))
        first = pl.Ledger(store)
        rows = first.manifest()
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "ingested"
        for key in ("path", "size", "mtime_ns", "fingerprint", "status"):
            assert row[key] not in (None, ""), key
        assert row["fingerprint"].startswith("sha256:")
        on_disk = {r["path"]: r for r in _jsonl.read_rows(first.path)}
        assert on_disk == {r["path"]: r for r in rows}
        second = pl.Ledger(store)
        assert {r["path"]: r for r in second.manifest()} == on_disk

    def test_commit_leaves_no_temp_files(self, store, tmp_path):
        from commontrace.ingest import ingest_code_repository

        src = tmp_path / "src"
        src.mkdir()
        (src / "m.py").write_text("x = 1\n", encoding="utf-8")
        ingest_code_repository(store, str(src))
        from commontrace import paths

        memdir = paths.memory_dir(store)
        assert not [f for f in os.listdir(memdir) if f.endswith(".tmp")]

    def test_max_files_truncation_is_counted(self, store, tmp_path):
        from commontrace.ingest import ingest_code_repository

        src = tmp_path / "src"
        src.mkdir()
        for i in range(5):
            (src / f"m{i}.py").write_text(f"VALUE_{i} = {i}\n", encoding="utf-8")
        res = ingest_code_repository(store, str(src), max_files=2)
        assert res.truncated == 3
        assert res.to_dict()["truncated"] == 3

    def test_large_files_are_counted_not_silent(self, store, tmp_path, monkeypatch):
        import commontrace.ingest as ingest

        monkeypatch.setattr(ingest, "MAX_TEXT_FILE_BYTES", 10)
        src = tmp_path / "src"
        src.mkdir()
        (src / "big.py").write_text("x = '" + "y" * 100 + "'\n", encoding="utf-8")
        res = ingest.ingest_code_repository(store, str(src))
        assert res.chunks_extracted == 0 and res.skipped_large == 1
        manifest = {r["path"]: r for r in pl.Ledger(store).manifest()}
        assert manifest[os.path.abspath(str(src / "big.py"))]["status"] == "skipped_large"

    def test_unsupported_formats_are_counted_not_silent(self, store, tmp_path):
        from commontrace.ingest import ingest_multimodal_document

        blob = tmp_path / "blob"
        blob.mkdir()
        (blob / "data.parquet").write_bytes(b"PAR1" + b"\x00" * 64)
        res = ingest_multimodal_document(store, str(blob))
        assert res.skipped_unsupported == 1
        manifest = {r["path"]: r for r in pl.Ledger(store).manifest()}
        assert manifest[os.path.abspath(str(blob / "data.parquet"))]["status"] == "skipped_unsupported"

    def test_document_pipeline_counts_gaps(self, store, tmp_path, monkeypatch):
        import commontrace.ingest as ingest

        monkeypatch.setattr(ingest, "MAX_TEXT_FILE_BYTES", 10)
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "a.md").write_text("# A\n\n" + ("word " * 50), encoding="utf-8")
        (docs / "b.ipynb").write_text('{"cells": []}', encoding="utf-8")
        res = pl.create_document_pipeline(str(docs), store, contextualize="none", max_files=10).run()
        assert res.skipped_large == 1 and res.skipped_unsupported == 1
        assert res.to_dict()["skipped_large"] == 1
        stats = pl.create_document_pipeline(str(docs), store, contextualize="none", max_files=1).loader.stats
        assert stats["truncated"] >= 0

    def test_document_pipeline_truncation_counter(self, store, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        for i in range(4):
            (docs / f"d{i}.md").write_text(f"# Doc {i}\n\nThis paragraph states operational fact number {i} here.\n",
                                           encoding="utf-8")
        res = pl.create_document_pipeline(str(docs), store, contextualize="none", max_files=1).run()
        assert res.truncated == 3

    def test_pdf_extraction_quality_in_manifest(self, store, tmp_path):
        from commontrace.ingest import ingest_multimodal_document

        pdf = tmp_path / "note.pdf"
        pdf.write_bytes(b"%PDF-1.4\nBT /F1 12 Tf 72 712 Td (Hello ledger quality world) Tj ET\n%%EOF")
        res = ingest_multimodal_document(store, str(pdf))
        assert res.chunks_extracted == 1
        manifest = {r["path"]: r for r in pl.Ledger(store).manifest()}
        detail = manifest[os.path.abspath(str(pdf))].get("detail", {})
        assert detail["bytes"] == os.path.getsize(str(pdf))
        assert detail["chars"] > 0

    def test_image_extensions_only_stdlib_decodable(self, tmp_path):
        from commontrace.ingest import multimodal as mm

        assert set(mm.IMAGE_EXTENSIONS) <= {".png", ".jpg", ".jpeg"}
        gif = tmp_path / "anim.gif"
        gif.write_bytes(b"GIF89a" + b"\x00" * 32)
        res = mm.ingest_multimodal(str(gif))
        assert res.chunks_extracted == 0 and res.errors
        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
        png = tmp_path / "dot.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\x00\x00\x00\x00"
                        + struct.pack(">I", 0) + b"IEND" + b"\x00\x00\x00\x00")
        chunks = mm.INGEST_FNS[".png"](str(png))
        assert len(chunks) == 1 and "1x1" in chunks[0].content


class TestConversationSessionKeys:
    def test_same_basename_different_dirs_get_distinct_sessions(self, tmp_path):
        from commontrace.conversation import Store

        store = str(tmp_path / "store")
        os.makedirs(os.path.join(store, "memory"), exist_ok=True)
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        (dir_a / "notes.md").write_text("# Alpha notes\n\nThe alpha service restarts at midnight daily.\n",
                                        encoding="utf-8")
        (dir_b / "notes.md").write_text("# Beta notes\n\nThe beta service compacts its queue hourly.\n", encoding="utf-8")
        pl.create_document_pipeline(str(dir_a), store, space="s", contextualize="none").run()
        pl.create_document_pipeline(str(dir_b), store, space="s", contextualize="none").run()
        with Store(store, "s") as conv:
            sessions = sorted(s["id"] for s in conv.sessions())
        assert len(sessions) == 2
        assert all(s.startswith("doc:notes.md-") and len(s) > len("doc:notes.md-") for s in sessions)

    def test_rerun_reuses_session_without_duplicates(self, tmp_path):
        from commontrace.conversation import Store

        store = str(tmp_path / "store")
        os.makedirs(os.path.join(store, "memory"), exist_ok=True)
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "notes.md").write_text("# Notes\n\nThegamma service restarts at midnight daily here.\n",
                                       encoding="utf-8")
        pl.create_document_pipeline(str(docs), store, space="s", contextualize="none").run()
        pl.create_document_pipeline(str(docs), store, space="s", contextualize="none").run()
        with Store(store, "s") as conv:
            sessions = [s["id"] for s in conv.sessions()]
        assert len(sessions) == 1 and sessions[0].startswith("doc:notes.md-")
