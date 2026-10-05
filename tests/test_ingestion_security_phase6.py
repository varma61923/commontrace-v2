"""Regressions for untrusted ingestion and generated SQL boundaries."""
import json
import os
import sqlite3
import zipfile

import pytest

from commontrace import sql_guard
from commontrace.ingest import Chunk, _read_text, catalog, multimodal
from commontrace.ingest.pipeline import TextChunker


def test_chunker_streams_long_paragraph_without_losing_content():
    text = 'a' * (1024 * 1024 + 17)
    chunks = list(TextChunker(max_chars=2000, overlap=0).apply([Chunk(text, 'x', 'x')]))
    assert ''.join(c.content.strip() for c in chunks) == text
    assert all(len(c.content) <= 2002 for c in chunks)
    assert len({c.chunk_id for c in chunks}) == len(chunks)


@pytest.mark.parametrize('options', [{'max_chars': 0}, {'max_chars': -1}, {'max_chars': True}, {'overlap': -1}])
def test_chunker_rejects_nonprogressing_configuration(options):
    with pytest.raises(ValueError):
        TextChunker(**options)


def test_bounded_reader_rejects_leaf_ancestor_and_fifo(tmp_path):
    directory = tmp_path / 'real'
    directory.mkdir()
    source = directory / 'note.txt'
    source.write_text('allowed source')
    leaf = tmp_path / 'leaf.txt'
    leaf.symlink_to(source)
    parent = tmp_path / 'linked'
    parent.symlink_to(directory, target_is_directory=True)
    for path in (leaf, parent / 'note.txt'):
        with pytest.raises(OSError):
            _read_text(str(path))
    fifo = tmp_path / 'pipe.txt'
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match='regular'):
        _read_text(str(fifo))


def test_reader_uses_pinned_parent_when_path_is_swapped(tmp_path, monkeypatch):
    source_dir = tmp_path / 'source'
    source_dir.mkdir()
    source = source_dir / 'note.txt'
    source.write_text('pinned content')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'note.txt').write_text('private content')
    original_open = os.open
    swapped = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == 'note.txt' and kwargs.get('dir_fd') is not None and not swapped:
            swapped = True
            source_dir.rename(tmp_path / 'original')
            source_dir.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, 'open', racing_open)
    assert _read_text(str(source)) == 'pinned content'
    assert swapped


def test_actual_read_bound_is_enforced_even_if_size_is_stale(tmp_path, monkeypatch):
    from commontrace import ingest
    source = tmp_path / 'note.txt'
    source.write_bytes(b'abcde')
    monkeypatch.setattr(ingest, 'MAX_TEXT_FILE_BYTES', 4)
    original_fstat = os.fstat

    def stale_fstat(fd):
        info = original_fstat(fd)
        values = list(info)
        values[6] = 1
        return os.stat_result(values)

    monkeypatch.setattr(os, 'fstat', stale_fstat)
    with pytest.raises(ValueError, match='larger'):
        _read_text(str(source))


def test_catalog_returns_only_registered_snapshots(tmp_path):
    root = tmp_path / 'store'
    root.mkdir()
    external = tmp_path / 'private.txt'
    external.write_text('secret source')
    assert catalog.get_document(str(root), str(external)) is None
    doc = catalog.record_document(str(root), str(external), 'authorized snapshot')
    external.write_text('new secret')
    assert catalog.get_document(str(root), str(external))['content'] == 'authorized snapshot'
    assert catalog.get_document(str(root), doc.id)['content'] == 'authorized snapshot'


def test_catalog_rejects_symlink_body_and_invalid_manifest_ids(tmp_path):
    root = str(tmp_path)
    doc = catalog.record_document(root, 'source.txt', 'registered content')
    body = tmp_path / 'memory' / 'documents' / (doc.id + '.json')
    external = tmp_path / 'outside.json'
    external.write_text(json.dumps({'id': doc.id, 'content': 'secret'}))
    body.unlink()
    body.symlink_to(external)
    assert catalog.get_document(root, doc.id) is None
    manifest = tmp_path / 'memory' / 'documents.jsonl'
    manifest.write_text(json.dumps({'id': '../../outside', 'source_path': 'malicious'}) + '\n')
    assert catalog.get_document(root, 'malicious') is None


def test_catalog_atomic_write_does_not_follow_existing_body_symlink(tmp_path):
    doc = catalog.record_document(str(tmp_path), 'source.txt', 'first')
    external = tmp_path / 'outside.json'
    external.write_text('untouched')
    body = tmp_path / 'memory' / 'documents' / (doc.id + '.json')
    body.unlink()
    body.symlink_to(external)
    catalog.record_document(str(tmp_path), 'source.txt', 'second')
    assert external.read_text() == 'untouched'
    assert catalog.get_document(str(tmp_path), doc.id)['content'] == 'second'


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-16', 'utf-32'])
def test_docx_rejects_entity_declarations_before_xml_parse(tmp_path, encoding):
    path = tmp_path / 'entity.docx'
    xml = '<!DOCTYPE doc [<!ENTITY secret "expanded">]><doc>&secret;</doc>'
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('word/document.xml', xml.encode(encoding))
    with pytest.raises(ValueError, match='declarations/entities'):
        multimodal.extract_docx_paragraphs(str(path))


def test_docx_depth_budget_and_long_paragraph_chunk_bound(tmp_path):
    path = tmp_path / 'deep.docx'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', '<doc>' * 257 + '</doc>' * 257)
    with pytest.raises(ValueError, match='budget'):
        multimodal.extract_docx_paragraphs(str(path))
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>' + 'a' * 10000 + '</w:t></w:r></w:p></w:body></w:document>')
    chunks = multimodal.chunk_docx(str(path))
    assert ''.join(c.content for c in chunks) == 'a' * 10000
    assert all(len(c.content) <= 2000 for c in chunks)
    assert len({c.chunk_id for c in chunks}) == len(chunks)


def test_unclosed_html_script_never_becomes_retrievable_text(tmp_path):
    path = tmp_path / 'bad.html'
    path.write_text('<h1>Visible</h1><script>' * 8000 + 'private script text')
    title, text = multimodal.extract_html_text(str(path))
    assert text.strip() == 'Visible'
    assert not title


def test_unclosed_pdf_regions_complete_and_keep_existing_fallback(tmp_path):
    path = tmp_path / 'bad.pdf'
    path.write_bytes(b'%PDF-1.4\n' + b'BT ' * 8000 + b'(recoverable text) Tj')
    assert multimodal.extract_pdf_text(str(path)) == 'recoverable text'


@pytest.fixture
def database(tmp_path):
    path = str(tmp_path / 'data.db')
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE t (n INTEGER)')
        conn.executemany('INSERT INTO t VALUES (?)', [(n,) for n in range(20)])
    return path


@pytest.mark.parametrize('query,expected', [
    ('SELECT n FROM (SELECT n FROM t LIMIT 10) ORDER BY n DESC', [9, 8]),
    ('SELECT n FROM t LIMIT -1', [0, 1]),
    ('SELECT n FROM t LIMIT (10 + 5)', [0, 1]),
    ('SELECT n FROM t LIMIT 5,10', [5, 6]),
    ('WITH c AS (SELECT n FROM t LIMIT 10) SELECT n FROM c', [0, 1]),
    ('SELECT n FROM t LIMIT 10 OFFSET 5', [5, 6]),
])
def test_sql_caps_outer_results_preserving_nested_and_offset_semantics(database, query, expected):
    result = sql_guard.execute_guarded_sql(database, query, max_rows=2)
    assert [row['n'] for row in result['rows']] == expected


def test_sql_preserves_literals_and_quoted_identifiers(database):
    query = "SELECT '--keep /* literal */ ;' AS \"delete\", 1 AS [limit], 2 AS `drop` -- strip\n"
    result = sql_guard.execute_guarded_sql(database, query)
    assert result['rows'] == [{'delete': '--keep /* literal */ ;', 'limit': 1, 'drop': 2}]


@pytest.mark.parametrize('query', ['SELECT * FROM pragma_database_list', "SELECT load_extension('/tmp/extension')"])
def test_sql_authorizer_denies_pragma_table_and_extension(database, query):
    with pytest.raises(sql_guard.SqlGuardError):
        sql_guard.execute_guarded_sql(database, query)


def test_sql_scalar_and_total_result_budgets(database):
    with pytest.raises(sql_guard.SqlGuardError, match='resource limits'):
        sql_guard.execute_guarded_sql(database, 'SELECT randomblob(1000000000)')
    with pytest.raises(sql_guard.SqlGuardError, match='result exceeded'):
        sql_guard.execute_guarded_sql(database, 'SELECT randomblob(900000) FROM t', max_rows=20)


def test_sql_progress_deadline_stops_unbounded_recursive_query(database):
    with pytest.raises(sql_guard.SqlGuardError, match='timeout'):
        sql_guard.execute_guarded_sql(database,
            'WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c) SELECT count(*) FROM c',
            timeout_seconds=0.01)


def test_python310_authorizer_preserves_safe_analytics(database, monkeypatch):
    original_connect = sqlite3.connect

    class LegacyConnection:
        def __init__(self, conn):
            object.__setattr__(self, 'conn', conn)
        def __getattr__(self, name):
            if name == 'setlimit':
                raise AttributeError(name)
            return getattr(self.conn, name)
        def __setattr__(self, name, value):
            setattr(self.conn, name, value)

    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **kw: LegacyConnection(original_connect(*a, **kw)))
    assert sql_guard.execute_guarded_sql(database, 'SELECT count(*) AS n FROM t')['rows'] == [{'n': 20}]
    for query in ['SELECT randomblob(1000000000)', "SELECT group_concat(n) FROM t", "SELECT printf('%1000000000d', 1)"]:
        with pytest.raises(sql_guard.SqlGuardError):
            sql_guard.execute_guarded_sql(database, query)


def test_role_tag_cleanup_handles_unclosed_and_nested_blocks():
    from commontrace.ingest import sanitize_contextualizer_text
    assert sanitize_contextualizer_text('<system>' * 8000 + 'document text') == 'document text'
    assert sanitize_contextualizer_text('safe <SYSTEM>a<system>b</system>c</SYSTEM> tail') == 'safe tail'
    assert sanitize_contextualizer_text('before <developer>private</developer> after') == 'before after'


def test_fingerprint_preserves_formulas_and_refuses_growth():
    import hashlib
    import io

    from commontrace.fingerprints import stream_fingerprint
    data = b'abcdefghij' * 20
    assert stream_fingerprint(io.BytesIO(data), len(data)) == 'sha256:' + hashlib.sha256(data).hexdigest()
    expected_sample = hashlib.sha256(str(len(data)).encode() + data[:10] + data[-10:]).hexdigest()
    assert stream_fingerprint(io.BytesIO(data), len(data), lazy_hash_bytes=100, sample_bytes=10) == 'sample:' + expected_sample
    with pytest.raises(ValueError, match='grew'):
        stream_fingerprint(io.BytesIO(data + b'growth'), len(data))
    with pytest.raises(ValueError, match='changed size'):
        stream_fingerprint(io.BytesIO(data[:-1]), len(data))


def test_ledger_rejects_substituted_symlink_and_size_before_hash(tmp_path):
    from commontrace.ingest.pipeline import Ledger
    source = tmp_path / 'source.txt'
    source.write_text('content')
    ledger = Ledger(str(tmp_path))
    assert ledger.changed(str(source))
    ledger.note(str(source), 'ingested')
    ledger.commit()
    source.unlink()
    outside = tmp_path / 'outside.txt'
    outside.write_text('private')
    source.symlink_to(outside)
    with pytest.raises(OSError):
        ledger.changed(str(source))
    with pytest.raises(ValueError, match='larger'):
        ledger.changed(str(outside), max_bytes=3)


def test_deep_json_lines_are_skipped_without_crashing(tmp_path):
    from commontrace.ingest import IngestionResult, _failure_turns
    source = tmp_path / 'turns.jsonl'
    source.write_text('[' * 2000 + '0' + ']' * 2000 + '\n' + json.dumps({'status': 'ERROR', 'content': 'recoverable failure'}) + '\n')
    result = IngestionResult(str(source), 'transcript')
    assert _failure_turns(str(source), result) == [{'status': 'ERROR', 'content': 'recoverable failure'}]


def test_pdf_escaped_opening_parentheses_scan_and_valid_escape(tmp_path):
    path = tmp_path / 'escaped.pdf'
    path.write_bytes(b'%PDF\nBT ' + b'\\(' * 8000 + b' Tj ET')
    assert multimodal.extract_pdf_text(str(path)) == ''
    path.write_bytes(b'%PDF\nBT (valid \\(literal\\) text) Tj ET')
    assert multimodal.extract_pdf_text(str(path)) == 'valid (literal) text'


def test_catalog_manifest_cache_hit_is_immutable_and_invalidates_on_replacement(tmp_path, monkeypatch):
    doc = catalog.record_document(str(tmp_path), 'source.txt', 'payload')
    assert catalog.get_document(str(tmp_path), doc.id)['content'] == 'payload'
    original_loads = json.loads
    manifest_parses = 0

    def counting_loads(value, *a, **kw):
        nonlocal manifest_parses
        if isinstance(value, bytes) and b'"chunk_count"' in value:
            manifest_parses += 1
        return original_loads(value, *a, **kw)

    monkeypatch.setattr(json, 'loads', counting_loads)
    first = catalog.get_document(str(tmp_path), doc.id)
    first['content'] = 'caller mutation'
    assert catalog.get_document(str(tmp_path), doc.id)['content'] == 'payload'
    assert manifest_parses == 0
    ids, sources = catalog._registered_documents(str(tmp_path))
    with pytest.raises(TypeError):
        sources['fake'] = doc.id
    manifest = tmp_path / 'memory' / 'documents.jsonl'
    replacement = tmp_path / 'replacement.jsonl'
    replacement.write_bytes(b'')
    os.replace(replacement, manifest)
    assert catalog.get_document(str(tmp_path), doc.id) is None


def test_manifest_cache_counts_distinct_sources_and_utf8_bytes(tmp_path, monkeypatch):
    path = tmp_path / 'memory' / 'documents.jsonl'
    path.parent.mkdir()
    repeated_id = 'a' * 16
    rows = [{'id': repeated_id, 'source_path': 'source-' + str(n) + '-' + 'é' * 100} for n in range(10)]
    path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))
    monkeypatch.setattr(catalog, '_MAX_CACHED_DOCUMENTS', 5)
    ids, sources = catalog._registered_documents(str(tmp_path))
    assert ids == frozenset({repeated_id}) and len(sources) == 10
    assert str(path) not in catalog._MANIFEST_CACHE
    monkeypatch.setattr(catalog, '_MAX_CACHED_DOCUMENTS', 100)
    monkeypatch.setattr(catalog, '_MAX_MANIFEST_CACHE_BYTES', 1024)
    catalog._registered_documents(str(tmp_path))
    assert str(path) not in catalog._MANIFEST_CACHE


def test_same_document_writers_publish_matching_payload_and_manifest(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    first_replaced = threading.Event()
    release_first = threading.Event()
    second_done = threading.Event()
    original_replace = os.replace
    trapped = False

    def paused_replace(source, target, *args, **kwargs):
        nonlocal trapped
        result = original_replace(source, target, *args, **kwargs)
        if kwargs.get('dst_dir_fd') is not None and not trapped:
            trapped = True
            first_replaced.set()
            assert release_first.wait(5)
        return result

    monkeypatch.setattr(os, 'replace', paused_replace)

    def second_writer():
        try:
            return catalog.record_document(str(tmp_path), 'same.txt', 'second content')
        finally:
            second_done.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(catalog.record_document, str(tmp_path), 'same.txt', 'first content')
        assert first_replaced.wait(5)
        second = pool.submit(second_writer)
        try:
            blocked = not second_done.wait(0.1)
        finally:
            release_first.set()
        first.result(timeout=5)
        last = second.result(timeout=5)
    assert blocked
    body = catalog.get_document(str(tmp_path), last.id)
    summary = catalog.list_documents(str(tmp_path))[0]
    assert body['content'] == 'second content'
    assert body['fingerprint'] == summary['fingerprint'] == last.fingerprint
    assert body['updated_at'] == summary['updated_at'] == last.updated_at


def test_sql_rejects_unbounded_requested_row_count_before_connect(database, monkeypatch):
    def forbidden_connect(*args, **kwargs):
        raise AssertionError('invalid budget reached database connection')
    monkeypatch.setattr(sqlite3, 'connect', forbidden_connect)
    with pytest.raises(sql_guard.SqlGuardError, match='at most'):
        sql_guard.execute_guarded_sql(database, 'SELECT 1', max_rows=10**30)


def test_ledger_detects_same_size_edit_with_restored_mtime(tmp_path):
    from commontrace.ingest.pipeline import Ledger
    source = tmp_path / 'source.txt'
    source.write_text('first')
    ledger = Ledger(str(tmp_path))
    assert ledger.changed(str(source))
    ledger.note(str(source), 'ingested')
    ledger.commit()
    stamp = source.stat()
    source.write_text('other')
    os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert ledger.changed(str(source))


def test_ledger_note_keeps_identity_of_loaded_generation(tmp_path):
    from commontrace.ingest.pipeline import Ledger
    source = tmp_path / 'source.txt'
    source.write_text('first')
    ledger = Ledger(str(tmp_path))
    assert ledger.changed(str(source))
    loaded = dict(ledger.pending[str(source)])
    replacement = tmp_path / 'replacement.txt'
    replacement.write_text('other')
    os.utime(replacement, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns))
    os.replace(replacement, source)
    ledger.note(str(source), 'ingested')
    assert ledger.pending[str(source)]['inode'] == loaded['inode']
    assert ledger.pending[str(source)]['fingerprint'] == loaded['fingerprint']
    ledger.commit()
    assert ledger.changed(str(source))


def test_docx_textbox_nested_paragraphs_keep_original_preorder_and_text(tmp_path):
    path = tmp_path / 'textbox.docx'
    xml = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           '<w:body><w:p><w:r><w:t>Outer </w:t><w:drawing><w:txbxContent>'
           '<w:p><w:r><w:t>Text box</w:t></w:r></w:p>'
           '</w:txbxContent></w:drawing><w:t> tail</w:t></w:r></w:p></w:body></w:document>')
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', xml)
    assert multimodal.extract_docx_paragraphs(str(path)) == ['Outer Text box tail', 'Text box']
