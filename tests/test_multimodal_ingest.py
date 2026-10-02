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
    def chunk(ctype: bytes, data: bytes) -> bytes:
        out = struct.pack(">I", len(data)) + ctype + data
        out += struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
        return out

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00\xff\x00\x00"
    idat = zlib.compress(raw)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _minimal_wav(channels: int = 1, rate: int = 8000, bits: int = 16,
                 seconds: int = 1) -> bytes:
    block_align = channels * bits // 8
    byte_rate = rate * block_align
    data = b"\x00" * (rate * seconds * block_align)
    fmt = struct.pack("<HHIIHH", 1, channels, rate, byte_rate, block_align, bits)
    out = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    out += b"fmt " + struct.pack("<I", 16) + fmt
    out += b"data" + struct.pack("<I", len(data)) + data
    return out


def _minimal_mp3() -> bytes:
    hdr = bytes([0xFF, 0xFB, 0x90, 0x64])
    frame_len = 144 * 128000 // 44100
    frame = hdr + b"\x00" * (frame_len - 4)
    return frame + frame


def _minimal_ogg(serial: int = 0x12345678) -> bytes:
    header = (
        b"OggS" + bytes([0, 0x02])
        + struct.pack("<q", 0)
        + struct.pack("<I", serial)
        + struct.pack("<I", 0)
        + struct.pack("<I", 0)
        + bytes([1, 4])
    )
    return header + b"abcd"


def _minimal_flac(sample_rate: int = 44100, channels: int = 2,
                  bits: int = 16, total_samples: int = 44100) -> bytes:
    packed = (
        (sample_rate << 44)
        | ((channels - 1) << 41)
        | ((bits - 1) << 36)
        | total_samples
    )
    body = (
        struct.pack(">H", 4096) + struct.pack(">H", 4096)
        + b"\x00\x00\x00" + b"\x00\x00\x00"
        + packed.to_bytes(8, "big")
        + b"\x00" * 16
    )
    assert len(body) == 34
    return b"fLaC" + bytes([0x80]) + len(body).to_bytes(3, "big") + body


def _minimal_vtt() -> bytes:
    return (
        "WEBVTT\n"
        "\n"
        "00:00:01.000 --> 00:00:04.000\n"
        "Hello <i>subtitle</i> world\n"
        "\n"
        "00:00:05.500 --> 00:00:07.000 align:start position:0%\n"
        "Second cue with <v Speaker>voice</v> tag\n"
    ).encode("utf-8")


def _minimal_srt() -> bytes:
    return (
        "1\n"
        "00:00:01,000 --> 00:00:04,000\n"
        "Hello <b>srt</b> world\n"
        "\n"
        "2\n"
        "00:00:05,500 --> 00:00:07,000\n"
        "Second cue here\n"
    ).encode("utf-8")


def _minimal_jpeg() -> bytes:
    soi = b"\xff\xd8"
    jfif_body = b"JFIF\x00\x01\x02\x00\x00\x01\x00\x01\x00\x00"
    app0 = b"\xff\xe0" + struct.pack(">H", len(jfif_body) + 2) + jfif_body
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


class TestAudio:
    def test_wav_metadata(self, tmp_path):
        p = str(tmp_path / "tone.wav")
        _write(p, _minimal_wav(channels=1, rate=8000, bits=16, seconds=1))
        chunks = INGEST_FNS[".wav"](p)
        _check_provenance(chunks, p, "audio")
        assert chunks[0].chunk_type == "audio_metadata"
        joined = " ".join(c.content for c in chunks)
        assert "WAV" in joined
        assert "8000" in joined
        assert "16-bit" in joined
        assert "1.00s" in joined

    def test_wav_stereo(self, tmp_path):
        p = str(tmp_path / "stereo.wav")
        _write(p, _minimal_wav(channels=2, rate=44100, bits=16, seconds=2))
        res = ingest_multimodal(p)
        assert res.chunks_extracted >= 1
        assert res.source_type == "audio"
        assert not res.errors
        joined = " ".join(c.content for c in res.chunks)
        assert "2 channel" in joined
        assert "44100" in joined
        assert "2.00s" in joined

    def test_mp3_bitrate_estimate(self, tmp_path):
        p = str(tmp_path / "song.mp3")
        _write(p, _minimal_mp3())
        chunks = INGEST_FNS[".mp3"](p)
        _check_provenance(chunks, p, "audio")
        joined = " ".join(c.content for c in chunks)
        assert "MPEG-1" in joined
        assert "128" in joined
        assert "44100" in joined
        assert "duration" in joined.lower()

    def test_ogg_pages(self, tmp_path):
        p = str(tmp_path / "clip.ogg")
        _write(p, _minimal_ogg())
        chunks = INGEST_FNS[".ogg"](p)
        _check_provenance(chunks, p, "audio")
        joined = " ".join(c.content for c in chunks)
        assert "OGG" in joined
        assert "1 page" in joined
        assert "12345678" in joined

    def test_flac_streaminfo(self, tmp_path):
        p = str(tmp_path / "track.flac")
        _write(p, _minimal_flac())
        chunks = INGEST_FNS[".flac"](p)
        _check_provenance(chunks, p, "audio")
        joined = " ".join(c.content for c in chunks)
        assert "FLAC" in joined
        assert "44100" in joined
        assert "16-bit" in joined
        assert "2 channel" in joined
        assert "1.00s" in joined

    def test_audio_is_descriptive_not_decoded(self, tmp_path):
        p = str(tmp_path / "tone.wav")
        _write(p, _minimal_wav())
        chunks = INGEST_FNS[".wav"](p)
        assert "no audio decoded" in chunks[0].content.lower()

    def test_malformed_audio_errors_not_raise(self, tmp_path):
        for ext in (".wav", ".mp3", ".ogg", ".flac"):
            p = str(tmp_path / f"bad{ext}")
            _write(p, b"\x00\x01\x02not audio data")
            res = ingest_multimodal(p)
            assert res.errors, ext
            assert res.chunks_extracted == 0, ext


class TestSubtitles:
    def test_vtt_parse_and_cleanup(self, tmp_path):
        p = str(tmp_path / "caps.vtt")
        _write(p, _minimal_vtt())
        chunks = INGEST_FNS[".vtt"](p)
        _check_provenance(chunks, p, "subtitle")
        assert chunks[0].chunk_type == "subtitle_text"
        joined = "\n".join(c.content for c in chunks)
        assert "Hello subtitle world" in joined
        assert "Second cue with voice tag" in joined
        assert "<i>" not in joined and "<v" not in joined
        assert "position:0%" not in joined
        assert "00:00:01.000 --> 00:00:04.000" in joined
        assert SECRET not in joined

    def test_srt_parse_and_cleanup(self, tmp_path):
        p = str(tmp_path / "caps.srt")
        _write(p, _minimal_srt())
        chunks = INGEST_FNS[".srt"](p)
        _check_provenance(chunks, p, "subtitle")
        joined = "\n".join(c.content for c in chunks)
        assert "Hello srt world" in joined
        assert "Second cue here" in joined
        assert "<b>" not in joined
        assert "00:00:01.000 --> 00:00:04.000" in joined

    def test_srt_ingest_source_type(self, tmp_path):
        p = str(tmp_path / "caps.srt")
        _write(p, _minimal_srt())
        res = ingest_multimodal(p)
        assert res.chunks_extracted >= 1
        assert res.source_type == "subtitle"
        assert not res.errors

    def test_chunking_respects_cue_boundaries(self, tmp_path):
        lines = ["WEBVTT", ""]
        for i in range(200):
            start = i * 2
            lines.append(
                f"{start // 3600:02d}:{(start % 3600) // 60:02d}:{start % 60:02d}.000 --> "
                f"{start // 3600:02d}:{(start % 3600) // 60:02d}:{(start + 1) % 60:02d}.000"
            )
            lines.append(f"Cue number {i} with enough padding text to fill chunks x.")
            lines.append("")
        p = str(tmp_path / "long.vtt")
        _write(p, "\n".join(lines).encode("utf-8"))
        chunks = INGEST_FNS[".vtt"](p)
        assert len(chunks) >= 2
        for c in chunks:
            assert len(c.content) <= 3500 + 200, "chunk exceeds target + one cue"
            assert "-->" in c.content, "cue offsets must travel with text"
            assert c.breadcrumb and "-->" in c.breadcrumb
        joined = "\n".join(c.content for c in chunks)
        assert "Cue number 0 " in joined
        assert "Cue number 199 " in joined

    def test_malformed_subtitles_safe(self, tmp_path):
        for ext, fn in ((".vtt", _minimal_vtt), (".srt", _minimal_srt)):
            p = str(tmp_path / f"bad{ext}")
            _write(p, b"not a subtitle\nno timestamps here\n--> broken \n")
            res = ingest_multimodal(p)
            assert res.chunks, ext
            assert not res.errors, (ext, res.errors)


class TestDispatch:
    def test_ingest_fns_keys(self):
        assert set(INGEST_FNS) == {
            ".pdf", ".docx", ".html", ".htm", ".png", ".jpg", ".jpeg",
            ".wav", ".mp3", ".ogg", ".flac", ".vtt", ".srt",
        }

    def test_all_exts_dispatch(self, tmp_path):
        fixtures = {
            ".pdf": _minimal_pdf(),
            ".docx": _minimal_docx(),
            ".html": _minimal_html(),
            ".png": _minimal_png(),
            ".jpg": _minimal_jpeg(),
            ".wav": _minimal_wav(),
            ".mp3": _minimal_mp3(),
            ".ogg": _minimal_ogg(),
            ".flac": _minimal_flac(),
            ".vtt": _minimal_vtt(),
            ".srt": _minimal_srt(),
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
