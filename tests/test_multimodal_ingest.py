"""Tests for stdlib multimodal ingestion (pdf/docx/html/image)."""
import struct
import zipfile
import zlib

from commontrace.ingest.multimodal import INGEST_FNS, ingest_multimodal

SECRET = "sk-ant-1234567890abcdef"


def _write(path, data: bytes):
    with open(path, "wb") as fh:
        fh.write(data)


def _minimal_pdf(secret: str = SECRET) -> bytes:
    body = (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj\n"
        b"4 0 obj << /Length 200 >> stream\n"
        b"BT /F1 12 Tf 72 720 Td (Hello multimodal world) Tj ET\n"
        + b"BT /F1 12 Tf 72 700 Td (Secret " + secret.encode() + b" here) Tj ET\n"
        + b"endstream endobj\n"
        b"5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
        b"trailer << /Root 1 0 R >>\n%%EOF\n"
    )
    return body


def _minimal_docx(secret: str = SECRET) -> bytes:
    import io

    document_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        "<w:p><w:r><w:t>Hello docx world</w:t></w:r></w:p>"
        f"<w:p><w:r><w:t>Secret {secret} here</w:t></w:r></w:p>"
        "</w:body></w:document>"
    ).encode("utf-8")
    content_types = (
        '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    ).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def _minimal_html(secret: str = SECRET) -> bytes:
    return (
        "<!DOCTYPE html><html><head><title>Test Page</title>"
        "<style>.x { color: red; }</style>"
        f"<script>var k = '{secret}';</script></head>"
        "<body><h1>Hello</h1>"
        f"<p>Hello html world, secret {secret} here.</p>"
        "<!-- a comment --></body></html>"
    ).encode("utf-8")


def _minimal_png() -> bytes:
    # 1x1 RGBA PNG via stdlib struct + zlib.
    def chunk(ctype: bytes, data: bytes) -> bytes:
        out = struct.pack(">I", len(data)) + ctype + data
        out += struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
        return out

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00\xff\x00\x00"  # filter + one red pixel
    idat = zlib.compress(raw)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _minimal_jpeg() -> bytes:
    # SOI + APP0(JFIF) + SOF0(1x1) + EOI
    soi = b"\xff\xd8"
    jfif_body = b"JFIF\x00\x01\x02\x00\x00\x01\x00\x01\x00\x00"
    app0 = b"\xff\xe0" + struct.pack(">H", len(jfif_body) + 2) + jfif_body
    # SOF0: precision=8, height=1, width=1, components=1 (id=1, sampling=0x11, q=0)
    sof_body = struct.pack(">BHHB", 8, 1, 1, 1) + b"\x01\x11\x00"
    sof0 = b"\xff\xc0" + struct.pack(">H", len(sof_body) + 2) + sof_body
    eoi = b"\xff\xd9"
    return soi + app0 + sof0 + eoi


def _check_provenance(chunks, path, modality):
    assert chunks, "expected at least one chunk"
    for c in chunks:
        assert c.source_path == path
        assert c.breadcrumb, "breadcrumb must carry provenance"
        assert modality in c.chunk_type, f"chunk_type {c.chunk_type!r} must encode modality"
        assert getattr(c, "modality", None) == modality
        prov = getattr(c, "provenance", None)
        assert isinstance(prov, dict)
        assert prov.get("source_path") == path
        assert prov.get("modality") == modality


class TestPdf:
    def test_extract_and_redact(self, tmp_path):
        p = str(tmp_path / "doc.pdf")
        _write(p, _minimal_pdf())
        chunks = INGEST_FNS[".pdf"](p)
        _check_provenance(chunks, p, "pdf")
        joined = " ".join(c.content for c in chunks)
        assert "Hello multimodal world" in joined
        assert "[REDACTED]" in joined
        assert SECRET not in joined

    def test_ingest_multimodal_counts(self, tmp_path):
        p = str(tmp_path / "doc.pdf")
        _write(p, _minimal_pdf())
        res = ingest_multimodal(p)
        assert res.chunks_extracted >= 1
        assert res.source_type == "pdf"
        assert not res.errors


class TestDocx:
    def test_extract_and_redact(self, tmp_path):
        p = str(tmp_path / "doc.docx")
        _write(p, _minimal_docx())
        chunks = INGEST_FNS[".docx"](p)
        _check_provenance(chunks, p, "docx")
        joined = " ".join(c.content for c in chunks)
        assert "Hello docx world" in joined
        assert "[REDACTED]" in joined
        assert SECRET not in joined

    def test_ingest_multimodal_counts(self, tmp_path):
        p = str(tmp_path / "doc.docx")
        _write(p, _minimal_docx())
        res = ingest_multimodal(p)
        assert res.chunks_extracted >= 1
        assert res.source_type == "docx"


class TestHtml:
    def test_extract_strips_tags_and_redacts(self, tmp_path):
        p = str(tmp_path / "page.html")
        _write(p, _minimal_html())
        chunks = INGEST_FNS[".html"](p)
        _check_provenance(chunks, p, "html")
        joined = " ".join(c.content for c in chunks)
        assert "Hello html world" in joined
        assert "<script" not in joined and "<p>" not in joined
        assert "[REDACTED]" in joined
        assert SECRET not in joined

    def test_htm_alias(self, tmp_path):
        assert INGEST_FNS[".htm"] is INGEST_FNS[".html"]
        p = str(tmp_path / "page.htm")
        _write(p, _minimal_html())
        res = ingest_multimodal(p)
        assert res.chunks_extracted >= 1
        assert res.source_type == "html"


class TestImage:
    def test_png_dimensions(self, tmp_path):
        p = str(tmp_path / "img.png")
        _write(p, _minimal_png())
        chunks = INGEST_FNS[".png"](p)
        _check_provenance(chunks, p, "image")
        joined = " ".join(c.content for c in chunks)
        assert "PNG" in joined
        assert "1x1" in joined

    def test_jpeg_dimensions(self, tmp_path):
        p = str(tmp_path / "photo.jpg")
        _write(p, _minimal_jpeg())
        chunks = INGEST_FNS[".jpg"](p)
        _check_provenance(chunks, p, "image")
        joined = " ".join(c.content for c in chunks)
        assert "JPEG" in joined
        assert "1x1" in joined

    def test_jpeg_alias(self):
        assert INGEST_FNS[".jpeg"] is INGEST_FNS[".jpg"]

    def test_image_is_descriptive_not_pixels(self, tmp_path):
        p = str(tmp_path / "img.png")
        _write(p, _minimal_png())
        chunks = INGEST_FNS[".png"](p)
        assert chunks[0].chunk_type == "image_description"
        assert "no pixel" in chunks[0].content.lower()


class TestDispatch:
    def test_ingest_fns_keys(self):
        assert set(INGEST_FNS) == {".pdf", ".docx", ".html", ".htm", ".png", ".jpg", ".jpeg"}

    def test_all_exts_dispatch(self, tmp_path):
        fixtures = {
            ".pdf": _minimal_pdf(),
            ".docx": _minimal_docx(),
            ".html": _minimal_html(),
            ".png": _minimal_png(),
            ".jpg": _minimal_jpeg(),
        }
        for ext, data in fixtures.items():
            p = str(tmp_path / f"f{ext}")
            _write(p, data)
            res = ingest_multimodal(p)
            assert res.chunks_extracted >= 1, ext
            assert not res.errors, (ext, res.errors)

    def test_unknown_extension_errors(self, tmp_path):
        p = str(tmp_path / "f.xyz")
        _write(p, b"hello")
        res = ingest_multimodal(p)
        assert res.chunks_extracted == 0
        assert res.errors

    def test_missing_file_errors(self, tmp_path):
        res = ingest_multimodal(str(tmp_path / "nope.pdf"))
        assert res.errors
        assert res.chunks_extracted == 0
