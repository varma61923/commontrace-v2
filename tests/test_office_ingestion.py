"""Real OOXML package fixtures exercise offline extraction and retry boundaries."""
import json
import os
import zipfile

import pytest

from commontrace.ingest import Chunk, IngestionResult, office, pipeline

S = office._SHEET_NS[0]
P = office._SLIDE_NS[0]
R = office._REL_NS[0]
A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def package(path, parts):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in parts.items():
            archive.writestr(name, value)
    return str(path)


def rels(*items):
    return '<Relationships>' + ''.join(
        f'<Relationship Id="{identity}" Target="{target}" Type="{R}/{kind}" {extra}/>'
        for identity, target, kind, extra in items) + '</Relationships>'


def spreadsheet(rows="", **extra):
    return {
        "xl/workbook.xml": f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets>'
        '<sheet name="Revenue" sheetId="1" r:id="r1" state="hidden"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": rels(("r1", "worksheets/sheet9.xml", "worksheet", ""),
                                               ("ss", "strings.xml", "sharedStrings", "")),
        "xl/strings.xml": f'<sst xmlns="{S}"><si><r><t>  stored </t></r><rPh><t>ignore</t></rPh>'
        '<r><t>value  </t></r></si></sst>',
        "xl/worksheets/sheet9.xml": f'<worksheet xmlns="{S}"><sheetData>{rows}</sheetData></worksheet>',
        **extra,
    }


def test_stored_values_and_provenance(tmp_path, monkeypatch):
    rows = '<row r="3"><c r="A3" t="s"><v>0</v></c><c r="C3"><f>1+1</f><v>2</v></c>'
    rows += '<c r="D3"><f>NOW()</f></c><c r="E3" t="b"><v>1</v></c></row>'
    path = package(tmp_path / "data.xlsx", spreadsheet(rows))
    chunks = office.chunk_xlsx(path)
    assert len(chunks) == 1
    assert chunks[0].content == 'A3[s]="  stored value  " | C3[n]=2 | D3[n]=[formula has no stored cached value] | E3[b]=TRUE'
    assert chunks[0].provenance["sheet"] == "Revenue"
    assert chunks[0].provenance["row"] == 3
    assert chunks[0].provenance["sheet_state"] == "hidden"
    assert chunks[0].provenance["source_part"] == "xl/worksheets/sheet9.xml"
    monkeypatch.chdir(tmp_path)
    assert office.chunk_xlsx("data.xlsx")[0].chunk_id == chunks[0].chunk_id


def test_long_cell_preserves_spaces_and_bounds(tmp_path):
    value = " a  b " * 1000
    path = package(tmp_path / "long.xlsx", spreadsheet(f'<row><c t="inlineStr"><is><t>{value}</t></is></c></row>'))
    chunks = office.chunk_xlsx(path)
    assert len(chunks) > 1
    assert all(len(c.content) <= 2000 for c in chunks)
    assert ''.join(c.content for c in chunks) == 'A1[inlineStr]=' + json.dumps(value)


def test_missing_cell_address_follows_previous_sparse_column(tmp_path):
    path = package(tmp_path / "sparse.xlsx", spreadsheet('<row><c r="Z1"><v>1</v></c><c><v>2</v></c></row>'))
    assert office.chunk_xlsx(path)[0].content == "Z1[n]=1 | AA1[n]=2"


def presentation():
    slide = f'<p:sld xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree><p:sp><p:txBody>'
    slide += '<a:p><a:r><a:t>Revenue</a:t></a:r><a:br/><a:r><a:t>grew 20%</a:t></a:r></a:p>'
    slide += '</p:txBody></p:sp></p:spTree></p:cSld></p:sld>'
    notes = f'<p:notes xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree>'
    notes += '<p:sp><p:txBody><a:p><a:r><a:t>Explain growth</a:t></a:r></a:p></p:txBody></p:sp>'
    notes += '<p:sp><p:nvSpPr><p:nvPr><p:ph type="sldNum"/></p:nvPr></p:nvSpPr>'
    notes += '<p:txBody><a:p><a:r><a:t>99</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:notes>'
    return {
        "ppt/presentation.xml": f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst>'
        '<p:sldId id="99" r:id="s"/></p:sldIdLst></p:presentation>',
        "ppt/_rels/presentation.xml.rels": rels(("s", "slides/slide9.xml", "slide", "")),
        "ppt/slides/slide9.xml": slide,
        "ppt/slides/_rels/slide9.xml.rels": rels(("n", "../notesSlides/notes1.xml", "notesSlide", ""),
                                                ("h", "https://example.com", "hyperlink", 'TargetMode="External"')),
        "ppt/notesSlides/notes1.xml": notes,
    }


def test_slide_notes_and_external_links(tmp_path):
    chunks = office.chunk_pptx(package(tmp_path / "deck.pptx", presentation()))
    assert [c.content for c in chunks] == ["Revenue\ngrew 20%", "Explain growth"]
    assert [c.provenance["kind"] for c in chunks] == ["slide", "speaker_notes"]
    assert all(c.provenance["slide"] == 1 for c in chunks)
    assert chunks[1].provenance["source_part"] == "ppt/notesSlides/notes1.xml"


@pytest.mark.parametrize("rows", [
    '<row r="0"/>', '<row r="1"/><row r="1"/>', '<row r="1"><c r="A2"/></row>',
    '<row><c r="XFE1"/></row>', '<row><c r="A1"/><c r="A1"/></row>',
    '<row><c t="s"><v>999</v></c></row>', '<row><c t="b"><v>yes</v></c></row>',
    '<row><c t="unknown"><v>x</v></c></row>',
])
def test_invalid_cell_addresses_and_values(tmp_path, rows):
    with pytest.raises(ValueError):
        office.chunk_xlsx(package(tmp_path / "bad.xlsx", spreadsheet(rows)))


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
def test_xml_entities_rejected(tmp_path, encoding):
    parts = spreadsheet()
    parts["xl/workbook.xml"] = '<!DOCTYPE workbook [<!ENTITY bomb "secret">]><workbook/>'.encode(encoding)
    with pytest.raises(ValueError, match="declarations/entities"):
        office.chunk_xlsx(package(tmp_path / "bad.xlsx", parts))


@pytest.mark.parametrize("budget,value", [("MAX_XML_BYTES", 5), ("MAX_XML_NODES", 1),
                                          ("MAX_XML_DEPTH", 1), ("MAX_TEXT_CHARS", 5)])
def test_aggregate_budgets(tmp_path, monkeypatch, budget, value):
    path = package(tmp_path / "bounded.xlsx", spreadsheet('<row><c><v>123456</v></c></row>'))
    monkeypatch.setattr(office, budget, value)
    with pytest.raises(ValueError, match="budget"):
        office.chunk_xlsx(path)


def test_directory_limit_before_zip_allocation(tmp_path, monkeypatch):
    path = package(tmp_path / "many.xlsx", spreadsheet())
    monkeypatch.setattr(office, "MAX_MEMBERS", 1)
    monkeypatch.setattr(office.zipfile, "ZipFile", lambda *a, **k: pytest.fail("allocated directory before check"))
    with pytest.raises(ValueError, match="central-directory"):
        office.chunk_xlsx(path)


@pytest.mark.parametrize("target", ["../../outside.xml", "https://example.com/sheet.xml", "%2e%2e/%2e%2e/secret"])
def test_internal_targets_cannot_escape(tmp_path, target):
    parts = spreadsheet()
    parts["xl/_rels/workbook.xml.rels"] = rels(("r1", target, "worksheet", ""))
    with pytest.raises(ValueError):
        office.chunk_xlsx(package(tmp_path / "bad.xlsx", parts))


@pytest.mark.parametrize("name", ["../escape", "/absolute", "back\\slash"])
def test_unsafe_archive_members(tmp_path, name):
    with pytest.raises(ValueError, match="members"):
        office.chunk_xlsx(package(tmp_path / "bad.xlsx", spreadsheet(**{name: "unused"})))


def test_empty_documents_are_explicit(tmp_path):
    assert "No stored cell values" in office.chunk_xlsx(package(tmp_path / "empty.xlsx", spreadsheet()))[0].content
    parts = presentation()
    parts["ppt/presentation.xml"] = f'<presentation xmlns="{P}"/>'
    assert "No extractable slide text" in office.chunk_pptx(package(tmp_path / "empty.pptx", parts))[0].content


def test_sheet_order_comes_from_workbook(tmp_path):
    parts = spreadsheet('<row><c><v>9</v></c></row>')
    parts["xl/workbook.xml"] = parts["xl/workbook.xml"].replace(
        '</sheets>', '<sheet name="Second" sheetId="2" r:id="r2"/></sheets>')
    parts["xl/_rels/workbook.xml.rels"] = rels(
        ("r2", "worksheets/sheet1.xml", "worksheet", ""),
        ("r1", "worksheets/sheet9.xml", "worksheet", ""))
    parts["xl/worksheets/sheet1.xml"] = f'<worksheet xmlns="{S}"><sheetData><row><c><v>1</v></c></row></sheetData></worksheet>'
    chunks = office.chunk_xlsx(package(tmp_path / "ordered.xlsx", parts))
    assert [c.provenance["sheet"] for c in chunks] == ["Revenue", "Second"]
    assert [c.content for c in chunks] == ["A1[n]=9", "A1[n]=1"]


def test_symlink_and_duplicate_members_rejected(tmp_path):
    path = package(tmp_path / "real.xlsx", spreadsheet())
    link = tmp_path / "link.xlsx"
    link.symlink_to(path)
    with pytest.raises((ValueError, OSError)):
        office.chunk_xlsx(str(link))
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr("xl/workbook.xml", "bad")
    with pytest.raises(ValueError, match="members"):
        office.chunk_xlsx(path)


def test_source_change_rejected(tmp_path, monkeypatch):
    path = package(tmp_path / "changed.xlsx", spreadsheet())
    original = office._Package.xml

    def changing(package, part):
        value = original(package, part)
        stamp = os.stat(path).st_mtime_ns
        os.utime(path, ns=(stamp + 1_000_000, stamp + 1_000_000))
        return value

    monkeypatch.setattr(office._Package, "xml", changing)
    with pytest.raises(ValueError, match="changed during extraction"):
        office.chunk_xlsx(path)


def test_shared_string_expansion_is_bounded(tmp_path, monkeypatch):
    path = package(tmp_path / "repeat.xlsx", spreadsheet(
        '<row>' + ''.join('<c t="s"><v>0</v></c>' for _ in range(20)) + '</row>'))
    monkeypatch.setattr(office, "MAX_TEXT_CHARS", 100)
    with pytest.raises(ValueError, match="character budget"):
        office.chunk_xlsx(path)


def test_parse_error_is_visible_and_retryable(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    bad = docs / "broken.xlsx"
    bad.write_bytes(b"broken")
    (docs / "good.md").write_text("Database backups run every day at noon.")
    store = str(tmp_path / "store")
    preview = pipeline.create_document_pipeline(str(docs), store, contextualize="none").preview(limit=None)
    assert preview.errors and "broken.xlsx" in preview.errors[0]
    assert not os.path.exists(store)
    result = pipeline.create_document_pipeline(str(docs), store, contextualize="none").run()
    assert result.errors and result.facts_written
    package(bad, spreadsheet('<row><c t="inlineStr"><is><t>Quarterly revenue rose significantly.</t></is></c></row>'))
    repaired = pipeline.create_document_pipeline(str(docs), store, contextualize="none").run()
    assert not repaired.errors and repaired.facts_written
    assert repaired.skipped_unchanged == 1


def test_cli_preview_and_background_job_report_parse_failure(tmp_path, capsys):
    from commontrace import jobs
    from commontrace.cli import main

    source = tmp_path / "broken.pptx"
    source.write_bytes(b"not a ZIP")
    root = str(tmp_path / "store")
    args = ["ingest", str(source), "--type", "docs", "--json", "--dest", root]
    assert main(args + ["--preview"]) == 1
    assert json.loads(capsys.readouterr().out)["errors"]
    assert not os.path.exists(root)
    assert main(["init", "--dest", root]) == 0
    capsys.readouterr()
    assert main(args + ["--background"]) == 0
    queued = json.loads(capsys.readouterr().out)
    main(["jobs", "run", "--dest", root])
    assert jobs.get(root, queued["id"]).status == "failed"


def test_provenance_keeps_extraction_and_rejects_invalid_before_writes(tmp_path, monkeypatch):
    from commontrace import hierarchical, provenance
    from commontrace.connectors.base import record_chunks

    root = str(tmp_path / "store")
    chunk = Chunk("Revenue grew twenty percent.", "deck.pptx", "chunk1", "Revenue", "pptx_text", "pptx",
                  provenance={"slide": 3, "source_part": "ppt/slides/slide2.xml", "api_key": "short-secret"})
    result = IngestionResult("deck.pptx", "pptx")
    assert record_chunks(root, [chunk], source_id="deck", scope="", run_id="r", connector="documents", result=result)
    detail = provenance.list_provenance(root, "chunk1")[0]["detail"]
    assert detail["extraction"]["slide"] == 3
    assert detail["extraction"]["api_key"] != "short-secret"
    assert chunk.provenance["api_key"] == "short-secret"
    monkeypatch.setattr(hierarchical, "add_facts", lambda *a, **k: pytest.fail("wrote invalid metadata"))
    for invalid in [float("nan"), object()]:
        chunk.provenance = {"invalid": invalid}
        result = IngestionResult("deck.pptx", "pptx")
        assert not record_chunks(root, [chunk], source_id="deck", scope="", run_id="r", connector="documents", result=result)
        assert result.errors
