"""Zero-dependency multimodal ingestion (pdf/docx/html/image/audio/subtitles).

Stdlib only: no external libs. Each chunker returns ``list[Chunk]``
using the shared :class:`commontrace.ingest.Chunk` shape, with
``modality`` + source provenance carried on every chunk:

- ``chunk.chunk_type`` encodes the modality (``pdf_text``,
  ``docx_text``, ``html_text``, ``image_description``,
  ``audio_metadata``, ``subtitle_text``).
- ``chunk.source_path`` is the ingested file (provenance).
- ``chunk.modality`` (dynamic attr) is one of
  ``pdf`` / ``docx`` / ``html`` / ``image`` / ``audio`` / ``subtitle``.
- ``chunk.provenance`` (dynamic attr) is a dict with
  ``source_path``, ``modality`` and ``extractor`` keys.

Dispatch contract for the lead:

- ``INGEST_FNS`` maps file extension -> ``callable(path) -> list[Chunk]``.
- ``ingest_multimodal(path)`` dispatches by extension and returns
  an :class:`IngestionResult` with ``chunks_extracted`` populated
  (errors recorded, never raised).

Audio notes: WAV/MP3/OGG/FLAC extractors parse container metadata
only (headers, frame sync, page tables, STREAMINFO) and emit a
descriptive chunk — no audio is decoded.

Subtitle notes: VTT/SRT parsers strip tags/positioning, keep cue
text with ``[HH:MM:SS.mmm --> HH:MM:SS.mmm]`` offsets, and pack cues
to ~3500 chars on cue boundaries.
"""

from __future__ import annotations

import html as _html
import os
import re
import struct
import textwrap
import zipfile

from commontrace.ingest import Chunk, IngestionResult, _fingerprint, _redact_secrets

_MAX_MM_CHUNK = 2000

_EXTRACTOR = "commontrace.ingest.multimodal"


# ---------------------------------------------------------------------------
# Provenance helper
# ---------------------------------------------------------------------------

def _make_chunk(
    content: str,
    path: str,
    modality: str,
    chunk_type: str,
    breadcrumb: str,
    index: int,
) -> Chunk:
    """Build one redacted Chunk with modality + provenance attached."""
    redacted = _redact_secrets(content)
    chunk = Chunk(
        content=redacted,
        source_path=path,
        chunk_id=f"{_fingerprint(path + modality + breadcrumb)}_{index}",
        breadcrumb=breadcrumb,
        chunk_type=chunk_type,
    )
    # Dynamic provenance fields (Chunk dataclass has no slots).
    chunk.modality = modality  # type: ignore[attr-defined]
    chunk.source = path  # type: ignore[attr-defined]
    chunk.provenance = {  # type: ignore[attr-defined]
        "source_path": path,
        "modality": modality,
        "extractor": _EXTRACTOR,
    }
    return chunk


def _split_bounded(
    text: str,
    path: str,
    modality: str,
    chunk_type: str,
    breadcrumb: str,
) -> list[Chunk]:
    text = (text or "").strip()
    if not text:
        return []
    blocks = textwrap.wrap(text, _MAX_MM_CHUNK) or [text]
    return [
        _make_chunk(block, path, modality, chunk_type, breadcrumb, i)
        for i, block in enumerate(blocks)
    ]


# ---------------------------------------------------------------------------
# PDF — BT/ET stream strings, no external libs
# ---------------------------------------------------------------------------

_PDF_BT_ET_RE = re.compile(r"BT(.*?)ET", re.DOTALL)
_PDF_PAREN_RE = re.compile(r"\((?:\\.|[^\\()])*\)")
_PDF_HEX_RE = re.compile(r"<([0-9A-Fa-f\s]+)>")
_PDF_TJ_RE = re.compile(r"(?:Tj|TJ|')", re.DOTALL)


def _unescape_pdf_string(token: str) -> str:
    # token includes surrounding parens
    inner = token[1:-1]
    out: list[str] = []
    i = 0
    while i < len(inner):
        c = inner[i]
        if c == "\\" and i + 1 < len(inner):
            nxt = inner[i + 1]
            mapping = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f",
                       "(": "(", ")": ")", "\\": "\\"}
            if nxt in mapping:
                out.append(mapping[nxt])
                i += 2
                continue
            if nxt in "01234567":
                oct_digits = ""
                j = i + 1
                while j < len(inner) and len(oct_digits) < 3 and inner[j] in "01234567":
                    oct_digits += inner[j]
                    j += 1
                try:
                    out.append(chr(int(oct_digits, 8)))
                except ValueError:
                    pass
                i = j
                continue
            out.append(nxt)
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _decode_pdf_hex(token: str) -> str:
    cleaned = re.sub(r"\s+", "", token)
    if len(cleaned) % 2 == 1:
        cleaned += "0"
    try:
        raw = bytes.fromhex(cleaned)
    except ValueError:
        return ""
    for enc in ("utf-8", "utf-16-be", "latin-1"):
        try:
            text = raw.decode(enc)
            # Heuristic: utf-16-be decode of ascii bytes yields NULs; reject.
            if enc == "utf-16-be" and "\x00" not in text:
                continue
            return text
        except Exception:
            continue
    return raw.decode("latin-1", errors="replace")


def extract_pdf_text(path: str) -> str:
    """Extract text strings from PDF content streams (BT...ET regions)."""
    with open(path, "rb") as fh:
        raw = fh.read()
    text = raw.decode("latin-1", errors="replace")
    regions = _PDF_BT_ET_RE.findall(text)
    # Fallback: scan whole file if no BT/ET blocks found.
    if not regions:
        regions = [text]
    parts: list[str] = []
    for region in regions:
        # Only consider string tokens near a text-showing operator.
        # Simplest robust approach: collect parenthesized + hex strings
        # inside regions that mention a text operator at all.
        if "Tj" not in region and "TJ" not in region and "'" not in region and regions != [text]:
            continue
        for m in _PDF_PAREN_RE.finditer(region):
            decoded = _unescape_pdf_string(m.group(0))
            if decoded.strip():
                parts.append(decoded)
        for m in _PDF_HEX_RE.finditer(region):
            # Skip dict delimiters like << >> (regex only matches hex chars
            # so '<<' won't match; but guard tiny tokens).
            token = m.group(1)
            if len(re.sub(r"\s+", "", token)) < 4:
                continue
            # Only accept plausible text hex (printable-heavy after decode).
            decoded = _decode_pdf_hex(token)
            if decoded.strip() and sum(c.isprintable() or c.isspace() for c in decoded) >= max(1, len(decoded) // 2):
                # Avoid swallowing binary stream noise: require the region
                # to look like a content stream (has Tj/TJ).
                if "Tj" in region or "TJ" in region:
                    parts.append(decoded)
    # Join: PDF Tj tokens are usually word/line fragments.
    return "\n".join(p.strip() for p in parts if p.strip())


def chunk_pdf(path: str) -> list[Chunk]:
    text = extract_pdf_text(path)
    base = os.path.basename(path)
    if not text.strip():
        # Still emit one descriptive chunk so dispatch counts the file.
        return [_make_chunk(
            f"[PDF] No extractable text in {base}.",
            path, "pdf", "pdf_text", base, 0,
        )]
    return _split_bounded(text, path, "pdf", "pdf_text", base)


# ---------------------------------------------------------------------------
# DOCX — stdlib zipfile + XML paragraph text
# ---------------------------------------------------------------------------

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_W_P = f"{_W_NS}p"
_W_T = f"{_W_NS}t"
_W_TAB = f"{_W_NS}tab"
_W_BR = f"{_W_NS}br"


def extract_docx_paragraphs(path: str) -> list[str]:
    """Return paragraph texts from word/document.xml (stdlib only)."""
    with zipfile.ZipFile(path, "r") as zf:
        try:
            xml_bytes = zf.read("word/document.xml")
        except KeyError:
            raise ValueError(f"not a docx: missing word/document.xml in {path!r}")
    xml_text = xml_bytes.decode("utf-8", errors="replace")
    # Try ElementTree first.
    try:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(xml_bytes)  # nosec B314
        paras: list[str] = []
        for p in root.iter(_W_P):
            bits: list[str] = []
            for node in p.iter():
                if node.tag == _W_T and node.text:
                    bits.append(node.text)
                elif node.tag in (_W_TAB,):
                    bits.append("\t")
                elif node.tag in (_W_BR,):
                    bits.append("\n")
            para = "".join(bits).strip()
            if para:
                paras.append(para)
        if paras:
            return paras
    except Exception:
        pass
    # Regex fallback: group <w:t> runs by <w:p> blocks.
    paras = []
    for p_block in re.findall(r"<w:p[\s>].*?</w:p>", xml_text, re.DOTALL):
        runs = re.findall(r"<w:t[^>]*>(.*?)</w:t>", p_block, re.DOTALL)
        para = "".join(_html.unescape(r) for r in runs).strip()
        if para:
            paras.append(para)
    return paras


def chunk_docx(path: str) -> list[Chunk]:
    paras = extract_docx_paragraphs(path)
    base = os.path.basename(path)
    if not paras:
        return [_make_chunk(
            f"[DOCX] No extractable text in {base}.",
            path, "docx", "docx_text", base, 0,
        )]
    chunks: list[Chunk] = []
    buf = ""
    idx = 0
    for para in paras:
        if len(buf) + len(para) + 2 > _MAX_MM_CHUNK and buf.strip():
            chunks.append(_make_chunk(buf.strip(), path, "docx", "docx_text", base, idx))
            idx += 1
            buf = ""
        buf += para + "\n\n"
    if buf.strip():
        chunks.append(_make_chunk(buf.strip(), path, "docx", "docx_text", base, idx))
    return chunks


# ---------------------------------------------------------------------------
# HTML — tag-strip to text
# ---------------------------------------------------------------------------

_SCRIPT_STYLE_RE = re.compile(
    r"<\s*(script|style|noscript)[^>]*>.*?</\s*\1\s*>", re.IGNORECASE | re.DOTALL
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_HEADING_RE = re.compile(r"<\s*h[1-3][^>]*>(.*?)</\s*h[1-3}\s]*>", re.IGNORECASE | re.DOTALL)
_TITLE_RE = re.compile(r"<\s*title[^>]*>(.*?)</\s*title\s*>", re.IGNORECASE | re.DOTALL)


def extract_html_text(path: str) -> tuple[str, str]:
    """Return (title, plain_text) from an HTML file via tag stripping."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    title_m = _TITLE_RE.search(raw)
    title = _TAG_RE.sub("", title_m.group(1)).strip() if title_m else ""
    title = _html.unescape(title)
    text = _SCRIPT_STYLE_RE.sub(" ", raw)
    text = _COMMENT_RE.sub(" ", text)
    # Block-level tags become newlines to preserve section breaks.
    text = re.sub(r"</\s*(p|div|br|li|tr|h[1-6]|section|article)\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*(br|hr)[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub(" ", text)
    text = _html.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return title, text


def chunk_html(path: str) -> list[Chunk]:
    title, text = extract_html_text(path)
    base = os.path.basename(path)
    breadcrumb = f"{base} :: {title}" if title else base
    if not text.strip():
        return [_make_chunk(
            f"[HTML] No extractable text in {base}.",
            path, "html", "html_text", breadcrumb, 0,
        )]
    return _split_bounded(text, path, "html", "html_text", breadcrumb)


# ---------------------------------------------------------------------------
# Images — PNG/JPEG dimension + EXIF-ish header parse via struct
# ---------------------------------------------------------------------------

def _parse_png(data: bytes) -> tuple[int | None, int | None, dict]:
    info: dict = {"format": "PNG"}
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    info["color_type"] = None
    info["bit_depth"] = None
    width = height = None
    offset = 8
    while offset + 8 <= len(data):
        (length,) = struct.unpack(">I", data[offset:offset + 4])
        ctype = data[offset + 4:offset + 8]
        offset += 8
        chunk_data = data[offset:offset + length]
        if ctype == b"IHDR" and length >= 13:
            width, height, bit_depth, color_type, comp, filt, inter = struct.unpack(
                ">IIBBBBB", chunk_data[:13]
            )
            info.update(
                bit_depth=bit_depth, color_type=color_type,
                compression=comp, filter=filt, interlace=inter,
            )
            break
        offset += length + 4  # data + CRC
        if ctype == b"IEND" or offset > len(data):
            break
    info["width"] = width
    info["height"] = height
    return width, height, info


def _parse_jpeg(data: bytes) -> tuple[int | None, int | None, dict]:
    info: dict = {"format": "JPEG"}
    if data[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG")
    info["jfif"] = None
    info["exif_present"] = False
    info["app_segments"] = []
    width = height = None
    sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                   0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    pos = 2
    n = len(data)
    while pos + 4 <= n:
        if data[pos] != 0xFF:
            pos += 1
            continue
        # Skip padding FF bytes.
        while pos < n and data[pos] == 0xFF:
            pos += 1
        if pos >= n:
            break
        marker = data[pos]
        pos += 1
        if marker == 0xD9:  # EOI
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue  # standalone markers
        if pos + 2 > n:
            break
        (seg_len,) = struct.unpack(">H", data[pos:pos + 2])
        if seg_len < 2:
            break
        seg = data[pos + 2:pos + seg_len]
        if marker == 0xE0 and seg[:5] == b"JFIF\x00":  # APP0 JFIF
            try:
                major, minor = seg[5], seg[6]
                info["jfif"] = f"{major}.{minor}"
            except IndexError:
                info["jfif"] = "present"
            info["app_segments"].append("APP0/JFIF")
        elif marker == 0xE1:  # APP1 — possibly EXIF
            info["app_segments"].append("APP1")
            if seg[:6] == b"Exif\x00\x00":
                info["exif_present"] = True
        elif 0xE0 <= marker <= 0xEF:
            info["app_segments"].append(f"APP{marker - 0xE0}")
        elif marker in sof_markers and len(seg) >= 5:
            # SOF: precision(1) + height(2) + width(2) + components(1)...
            try:
                precision = seg[0]
                (height, width) = struct.unpack(">HH", seg[1:5])
                info["sof"] = f"SOF{marker - 0xC0}"
                info["precision"] = precision
                break
            except struct.error:
                pass
        pos += seg_len
    info["width"] = width
    info["height"] = height
    return width, height, info


def _describe_image(path: str) -> tuple[str, str, dict]:
    """Return (format_label, description, info) without decoding pixels."""
    with open(path, "rb") as fh:
        data = fh.read()
    size = len(data)
    ext = os.path.splitext(path)[1].lower()
    base = os.path.basename(path)
    info: dict
    if ext == ".png" or data[:8] == b"\x89PNG\r\n\x1a\n":
        width, height, info = _parse_png(data)
        label = "PNG"
    elif ext in (".jpg", ".jpeg") or data[:2] == b"\xff\xd8":
        width, height, info = _parse_jpeg(data)
        label = "JPEG"
    else:
        raise ValueError(f"unsupported image type for {path!r}")
    dims = f"{width}x{height}" if width and height else "unknown dimensions"
    bits: list[str] = [f"Image {label} {dims} from {base} ({size} bytes)."]
    if info.get("bit_depth") is not None:
        bits.append(f"bit_depth={info['bit_depth']}, color_type={info['color_type']}.")
    if info.get("jfif"):
        bits.append(f"JFIF version {info['jfif']}.")
    if info.get("exif_present"):
        bits.append("EXIF APP1 segment present.")
    elif info.get("app_segments"):
        bits.append(f"Segments: {', '.join(info['app_segments'])}.")
    bits.append("Descriptive metadata only; no pixel data extracted.")
    return label, " ".join(bits), info


def chunk_image(path: str) -> list[Chunk]:
    label, description, _info = _describe_image(path)
    base = os.path.basename(path)
    return [_make_chunk(
        description, path, "image", "image_description",
        f"{base} [{label} {os.path.getsize(path)}B]", 0,
    )]


# ---------------------------------------------------------------------------
# Audio — container metadata only (WAV/MP3/OGG/FLAC), no audio decode
# ---------------------------------------------------------------------------

_WAV_FORMAT_NAMES = {
    1: "PCM",
    3: "IEEE float",
    6: "A-law",
    7: "mu-law",
    0xFFFE: "WAVE_FORMAT_EXTENSIBLE",
}


def _parse_wav(data: bytes) -> dict:
    """Parse RIFF/WAVE header + chunks; return fmt/data metadata dict."""
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a WAV file (missing RIFF/WAVE header)")
    channels = sample_rate = bits = byte_rate = None
    audio_fmt = 1
    data_size: int | None = None
    pos = 12
    n = len(data)
    while pos + 8 <= n:
        cid = data[pos:pos + 4]
        (size,) = struct.unpack("<I", data[pos + 4:pos + 8])
        body_off = pos + 8
        avail = max(0, n - body_off)
        if cid == b"fmt " and avail >= 16:
            audio_fmt, channels, sample_rate, byte_rate, _align, bits = struct.unpack(
                "<HHIIHH", data[body_off:body_off + 16]
            )
        elif cid == b"data" and data_size is None:
            data_size = min(size, avail)
        # Chunks are word-aligned: odd sizes carry one pad byte.
        pos = body_off + size + (size & 1)
    if channels is None or sample_rate is None or bits is None or byte_rate is None:
        raise ValueError("WAV missing fmt chunk")
    duration = (data_size / byte_rate) if (data_size is not None and byte_rate) else None
    return {
        "format": _WAV_FORMAT_NAMES.get(audio_fmt, f"format {audio_fmt}"),
        "channels": channels,
        "sample_rate": sample_rate,
        "bits": bits,
        "byte_rate": byte_rate,
        "data_size": data_size,
        "duration": duration,
    }


_MPEG_BITRATES: dict[tuple[int, int], list[int | None]] = {
    # (mpeg_version_key, layer) -> bitrate table in kbps, index 0..15.
    # version key 1 = MPEG-1, 2 = MPEG-2/2.5.
    (1, 1): [None, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448, None],
    (1, 2): [None, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, None],
    (1, 3): [None, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, None],
    (2, 1): [None, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, None],
    (2, 2): [None, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, None],
    (2, 3): [None, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, None],
}

_MPEG_VERSION_LABEL = {3: "MPEG-1", 2: "MPEG-2", 0: "MPEG-2.5"}
_MPEG_LAYER_LABEL = {1: "Layer I", 2: "Layer II", 3: "Layer III"}
_MPEG_MODE_LABEL = {0: "stereo", 1: "joint stereo", 2: "dual channel", 3: "mono"}


def _mpeg_header_at(data: bytes, pos: int) -> dict | None:
    """Decode one MPEG audio frame header at ``pos``; None if invalid."""
    if pos + 4 > len(data):
        return None
    b0, b1, b2, b3 = data[pos:pos + 4]
    if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
        return None
    ver = (b1 >> 3) & 0x03
    layer_bits = (b1 >> 1) & 0x03
    br_idx = (b2 >> 4) & 0x0F
    sr_idx = (b2 >> 2) & 0x03
    padding = (b2 >> 1) & 0x01
    if ver == 1 or layer_bits == 0 or br_idx in (0, 15) or sr_idx == 3:
        return None
    layer = 4 - layer_bits  # bits 11->L1, 10->L2, 01->L3
    vkey = 1 if ver == 3 else 2
    if ver == 3:
        sample_rate = (44100, 48000, 32000)[sr_idx]
    elif ver == 2:
        sample_rate = (22050, 24000, 16000)[sr_idx]
    else:
        sample_rate = (11025, 12000, 8000)[sr_idx]
    bitrate = _MPEG_BITRATES[(vkey, layer)][br_idx]
    if not bitrate:
        return None
    if layer == 1:
        frame_len = (12 * bitrate * 1000 // sample_rate + padding) * 4
    elif layer == 2 or vkey == 1:
        frame_len = 144 * bitrate * 1000 // sample_rate + padding
    else:  # MPEG-2/2.5 Layer III uses 72x slots.
        frame_len = 72 * bitrate * 1000 // sample_rate + padding
    if frame_len < 4:
        return None
    return {
        "version": _MPEG_VERSION_LABEL[ver],
        "layer": _MPEG_LAYER_LABEL[layer],
        "bitrate_kbps": bitrate,
        "sample_rate": sample_rate,
        "padding": padding,
        "mode": _MPEG_MODE_LABEL[(b3 >> 6) & 0x03],
        "frame_len": frame_len,
    }


def _parse_mp3(data: bytes) -> dict:
    """Scan for the first MPEG frame sync; estimate bitrate/duration."""
    offset = 0
    if data[:3] == b"ID3" and len(data) >= 10:
        # ID3v2: 10-byte header, size is 4 synchsafe bytes.
        sync_size = sum(b << (7 * (3 - i)) for i, b in enumerate(data[6:10]))
        offset = min(len(data), 10 + sync_size)
    info: dict | None = None
    frame_at = -1
    # Cap the scan so pathological files stay cheap; headers live up front.
    for pos in range(offset, min(len(data) - 4, offset + 1_048_576)):
        info = _mpeg_header_at(data, pos)
        if info is not None:
            frame_at = pos
            break
    if info is None or frame_at < 0:
        raise ValueError("no MPEG frame sync found (not an MP3 stream)")
    # Verify against the expected next frame when the file is long enough.
    verified = False
    nxt = frame_at + info["frame_len"]
    if nxt + 4 <= len(data):
        nxt_info = _mpeg_header_at(data, nxt)
        verified = bool(
            nxt_info
            and nxt_info["bitrate_kbps"] == info["bitrate_kbps"]
            and nxt_info["sample_rate"] == info["sample_rate"]
        )
    audio_bytes = len(data) - frame_at
    if len(data) >= 128 and data[-128:-125] == b"TAG":
        audio_bytes -= 128  # trailing ID3v1 tag is not audio
    bitrate = info["bitrate_kbps"]
    duration = (audio_bytes * 8 / (bitrate * 1000)) if bitrate else None
    return {
        "version": info["version"],
        "layer": info["layer"],
        "bitrate_kbps": bitrate,
        "sample_rate": info["sample_rate"],
        "mode": info["mode"],
        "first_frame_at": frame_at,
        "next_frame_verified": verified,
        "audio_bytes": audio_bytes,
        "duration": duration,
    }


def _parse_ogg(data: bytes) -> dict:
    """Walk Ogg page headers; count pages/serials without decoding."""
    if len(data) < 27 or data[0:4] != b"OggS":
        raise ValueError("not an OGG container (missing OggS capture pattern)")
    pos = 0
    n = len(data)
    pages = 0
    serials: set[int] = set()
    max_granule = -1
    bos = eos = False
    while pos + 27 <= n:
        if data[pos:pos + 4] != b"OggS":
            break
        htype = data[pos + 5]
        (granule,) = struct.unpack("<q", data[pos + 6:pos + 14])
        (serial,) = struct.unpack("<I", data[pos + 14:pos + 18])
        seg_count = data[pos + 26]
        if pos + 27 + seg_count > n:
            break
        payload = sum(data[pos + 27:pos + 27 + seg_count])
        pages += 1
        serials.add(serial)
        max_granule = max(max_granule, granule)
        if htype & 0x02:
            bos = True
        if htype & 0x04:
            eos = True
        pos += 27 + seg_count + payload
        if pos >= n:
            break
    if pages == 0:
        raise ValueError("no OGG pages found")
    return {
        "pages": pages,
        "serials": sorted(serials),
        "max_granule": max_granule,
        "bos": bos,
        "eos": eos,
    }


_FLAC_BLOCK_NAMES = {
    0: "STREAMINFO",
    1: "PADDING",
    2: "APPLICATION",
    3: "SEEKTABLE",
    4: "VORBIS_COMMENT",
    5: "CUESHEET",
    6: "PICTURE",
}


def _parse_flac(data: bytes) -> dict:
    """Parse FLAC metadata blocks; extract STREAMINFO fields."""
    if data[:4] != b"fLaC":
        raise ValueError("not a FLAC stream (missing fLaC marker)")
    pos = 4
    n = len(data)
    blocks: list[str] = []
    streaminfo: dict | None = None
    last = False
    while not last:
        if pos + 4 > n:
            raise ValueError("truncated FLAC metadata header")
        b0 = data[pos]
        last = bool(b0 & 0x80)
        btype = b0 & 0x7F
        length = int.from_bytes(data[pos + 1:pos + 4], "big")
        body = data[pos + 4:pos + 4 + length]
        if len(body) < length:
            raise ValueError("truncated FLAC metadata block")
        blocks.append(_FLAC_BLOCK_NAMES.get(btype, f"BLOCK{btype}"))
        if btype == 0:
            if length < 34:
                raise ValueError("truncated FLAC STREAMINFO block")
            packed = int.from_bytes(body[10:18], "big")
            sample_rate = (packed >> 44) & 0xFFFFF
            channels = ((packed >> 41) & 0x7) + 1
            bits = ((packed >> 36) & 0x1F) + 1
            total_samples = packed & 0xFFFFFFFFF
            if not sample_rate:
                raise ValueError("FLAC STREAMINFO has zero sample rate")
            streaminfo = {
                "sample_rate": sample_rate,
                "channels": channels,
                "bits": bits,
                "total_samples": total_samples,
                "duration": total_samples / sample_rate,
                "min_block": int.from_bytes(body[0:2], "big"),
                "max_block": int.from_bytes(body[2:4], "big"),
            }
        pos += 4 + length
    if streaminfo is None:
        raise ValueError("FLAC missing STREAMINFO block")
    streaminfo["blocks"] = blocks
    return streaminfo


def _describe_audio(path: str) -> tuple[str, str]:
    """Return (label, description) for an audio container; no decode."""
    with open(path, "rb") as fh:
        data = fh.read()
    size = len(data)
    ext = os.path.splitext(path)[1].lower()
    base = os.path.basename(path)
    if ext == ".wav" or (data[:4] == b"RIFF" and data[8:12] == b"WAVE"):
        info = _parse_wav(data)
        dur = f"{info['duration']:.2f}s" if info["duration"] is not None else "unknown duration"
        data_n = info["data_size"] if info["data_size"] is not None else 0
        desc = (
            f"Audio WAV {info['format']} {info['channels']} channel(s) from {base} "
            f"({size} bytes). Sample rate {info['sample_rate']} Hz, "
            f"{info['bits']}-bit, byte rate {info['byte_rate']} B/s. "
            f"Data {data_n} bytes, duration ~{dur}. "
            f"Descriptive metadata only; no audio decoded."
        )
        return "WAV", desc
    if ext == ".mp3" or data[:3] == b"ID3":
        info = _parse_mp3(data)
        dur = f"{info['duration']:.2f}s" if info["duration"] is not None else "unknown duration"
        ver = "verified" if info["next_frame_verified"] else "single-frame estimate"
        desc = (
            f"Audio MP3 {info['version']} {info['layer']} from {base} ({size} bytes). "
            f"Bitrate ~{info['bitrate_kbps']} kbps (CBR estimate, {ver}), "
            f"sample rate {info['sample_rate']} Hz, {info['mode']}. "
            f"Estimated duration ~{dur}. "
            f"Descriptive metadata only; no audio decoded."
        )
        return "MP3", desc
    if ext == ".ogg" or data[:4] == b"OggS":
        info = _parse_ogg(data)
        serials = ", ".join(f"0x{s:08X}" for s in info["serials"])
        desc = (
            f"Audio OGG from {base} ({size} bytes). {info['pages']} page(s), "
            f"stream serial(s): {serials}. Max granule position {info['max_granule']} "
            f"(samples; rate requires codec decode). "
            f"Descriptive metadata only; no audio decoded."
        )
        return "OGG", desc
    if ext == ".flac" or data[:4] == b"fLaC":
        info = _parse_flac(data)
        desc = (
            f"Audio FLAC {info['channels']} channel(s) from {base} ({size} bytes). "
            f"Sample rate {info['sample_rate']} Hz, {info['bits']}-bit, "
            f"total samples {info['total_samples']}. Duration ~{info['duration']:.2f}s. "
            f"Metadata blocks: {', '.join(info['blocks'])}. "
            f"Descriptive metadata only; no audio decoded."
        )
        return "FLAC", desc
    raise ValueError(f"unsupported audio type for {path!r}")


def _chunk_audio(path: str) -> list[Chunk]:
    label, description = _describe_audio(path)
    base = os.path.basename(path)
    return [_make_chunk(
        description, path, "audio", "audio_metadata",
        f"{base} [{label} {os.path.getsize(path)}B]", 0,
    )]


def chunk_wav(path: str) -> list[Chunk]:
    return _chunk_audio(path)


def chunk_mp3(path: str) -> list[Chunk]:
    return _chunk_audio(path)


def chunk_ogg(path: str) -> list[Chunk]:
    return _chunk_audio(path)


def chunk_flac(path: str) -> list[Chunk]:
    return _chunk_audio(path)


# ---------------------------------------------------------------------------
# Subtitles — VTT + SRT cue parse, tag strip, ~3500-char cue-boundary chunks
# ---------------------------------------------------------------------------

_SUB_CHUNK = 3500

_SUB_TAG_RE = re.compile(r"<[^>]*>")
_SUB_WS_RE = re.compile(r"[ \t\xa0]+")


def _parse_sub_timestamp(ts: str) -> float:
    """Parse HH:MM:SS.mmm / MM:SS.mmm (dot or comma) to seconds."""
    token = ts.strip().split()[0].replace(",", ".")
    if "." in token:
        main, frac = token.split(".", 1)
        millis = (frac + "000")[:3]
    else:
        main, millis = token, "000"
    parts = main.split(":")
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, m, s = "0", parts[0], parts[1]
    else:
        raise ValueError(f"bad subtitle timestamp: {ts!r}")
    try:
        return int(h) * 3600 + int(m) * 60 + int(s) + int(millis) / 1000.0
    except ValueError:
        raise ValueError(f"bad subtitle timestamp: {ts!r}")


def _format_sub_timestamp(seconds: float) -> str:
    total_ms = int(round(max(0.0, seconds) * 1000))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _clean_cue_lines(lines: list[str]) -> str:
    """Strip tags/positioning artifacts; collapse whitespace; join cues."""
    cleaned: list[str] = []
    for line in lines:
        text = _SUB_TAG_RE.sub("", line)
        text = _html.unescape(text)
        text = _SUB_WS_RE.sub(" ", text).strip()
        if text:
            cleaned.append(text)
    return " ".join(cleaned)


def _parse_vtt_cues(text: str) -> list[tuple[float, float, str]]:
    """Parse WEBVTT cues -> [(start_s, end_s, text)]. Skips bad cues."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cues: list[tuple[float, float, str]] = []
    i = 0
    n = len(lines)
    # Skip BOM/header line and any leading blanks.
    while i < n and not lines[i].strip():
        i += 1
    if i < n and lines[i].lstrip("\ufeff").strip().upper().startswith("WEBVTT"):
        i += 1
    while i < n:
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if line.upper().startswith("NOTE"):
            # NOTE comment block runs to the next blank line.
            i += 1
            while i < n and lines[i].strip():
                i += 1
            continue
        if line.upper() in ("STYLE", "REGION"):
            i += 1
            while i < n and lines[i].strip():
                i += 1
            continue
        if "-->" not in line:
            # Possible cue identifier: only meaningful if the next
            # non-blank line is a timestamp line.
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            if j < n and "-->" in lines[j]:
                i += 1
                continue
            i += 1
            continue
        try:
            start_tok, rest = line.split("-->", 1)
            end_tok = rest.strip().split()[0]
            start = _parse_sub_timestamp(start_tok)
            end = _parse_sub_timestamp(end_tok)
        except (ValueError, IndexError):
            i += 1
            continue
        i += 1
        body: list[str] = []
        while i < n and lines[i].strip():
            body.append(lines[i])
            i += 1
        cleaned = _clean_cue_lines(body)
        if cleaned:
            cues.append((start, end, cleaned))
    return cues


def _parse_srt_cues(text: str) -> list[tuple[float, float, str]]:
    """Parse SubRip cues -> [(start_s, end_s, text)]. Skips bad cues."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    cues: list[tuple[float, float, str]] = []
    for block in re.split(r"\n[ \t]*\n", normalized.strip()):
        lines = [ln for ln in block.split("\n")]
        ts_idx = next((k for k, ln in enumerate(lines) if "-->" in ln), -1)
        if ts_idx < 0:
            continue
        try:
            start_tok, rest = lines[ts_idx].split("-->", 1)
            end_tok = rest.strip().split()[0]
            start = _parse_sub_timestamp(start_tok)
            end = _parse_sub_timestamp(end_tok)
        except (ValueError, IndexError):
            continue
        cleaned = _clean_cue_lines(lines[ts_idx + 1:])
        if cleaned:
            cues.append((start, end, cleaned))
    return cues


def _chunk_subtitle_cues(
    cues: list[tuple[float, float, str]],
    path: str,
    kind: str,
) -> list[Chunk]:
    """Pack cues to ~3500 chars on cue boundaries, offsets as timestamps."""
    base = os.path.basename(path)
    if not cues:
        return [_make_chunk(
            f"[Subtitle {kind}] No extractable cues in {base}.",
            path, "subtitle", "subtitle_text", base, 0,
        )]
    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_len = 0
    chunk_start = cues[0][0]
    chunk_end = cues[0][1]
    idx = 0
    for start, end, cue_text in cues:
        line = f"[{_format_sub_timestamp(start)} --> {_format_sub_timestamp(end)}] {cue_text}"
        if buf and buf_len + len(line) + 1 > _SUB_CHUNK:
            breadcrumb = (
                f"{base} [{_format_sub_timestamp(chunk_start)} --> "
                f"{_format_sub_timestamp(chunk_end)}]"
            )
            chunks.append(_make_chunk(
                "\n".join(buf), path, "subtitle", "subtitle_text",
                breadcrumb, idx,
            ))
            idx += 1
            buf = []
            buf_len = 0
            chunk_start = start
        buf.append(line)
        buf_len += len(line) + 1
        chunk_end = end
    if buf:
        breadcrumb = (
            f"{base} [{_format_sub_timestamp(chunk_start)} --> "
            f"{_format_sub_timestamp(chunk_end)}]"
        )
        chunks.append(_make_chunk(
            "\n".join(buf), path, "subtitle", "subtitle_text",
            breadcrumb, idx,
        ))
    return chunks


def chunk_vtt(path: str) -> list[Chunk]:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    return _chunk_subtitle_cues(_parse_vtt_cues(text), path, "VTT")


def chunk_srt(path: str) -> list[Chunk]:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    return _chunk_subtitle_cues(_parse_srt_cues(text), path, "SRT")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

INGEST_FNS: dict[str, object] = {
    ".pdf": chunk_pdf,
    ".docx": chunk_docx,
    ".html": chunk_html,
    ".htm": chunk_html,
    ".png": chunk_image,
    ".jpg": chunk_image,
    ".jpeg": chunk_image,
    ".wav": chunk_wav,
    ".mp3": chunk_mp3,
    ".ogg": chunk_ogg,
    ".flac": chunk_flac,
    ".vtt": chunk_vtt,
    ".srt": chunk_srt,
}

_EXT_MODALITY = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".html": "html",
    ".htm": "html",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".wav": "audio",
    ".mp3": "audio",
    ".ogg": "audio",
    ".flac": "audio",
    ".vtt": "subtitle",
    ".srt": "subtitle",
}

_MODALITY_SOURCE_TYPE = {
    "pdf": "pdf",
    "docx": "docx",
    "html": "html",
    "image": "image",
    "audio": "audio",
    "subtitle": "subtitle",
}


def ingest_multimodal(path: str) -> IngestionResult:
    """Dispatch a single file to its stdlib extractor.

    Returns an :class:`IngestionResult` with ``chunks_extracted``
    populated. Unknown extensions and parse failures are recorded in
    ``errors`` — never raised. The extracted chunks are also stashed on
    ``result.chunks`` for callers that want them without a second call.
    """
    ext = os.path.splitext(path)[1].lower()
    modality = _EXT_MODALITY.get(ext, "")
    source_type = _MODALITY_SOURCE_TYPE.get(modality, "multimodal")
    result = IngestionResult(source_path=path, source_type=source_type)
    fn = INGEST_FNS.get(ext)  # type: ignore[assignment]
    if fn is None:
        result.errors.append(f"unsupported multimodal extension: {ext!r} for {path!r}")
        result.chunks = []  # type: ignore[attr-defined]
        return result
    try:
        chunks = fn(path)  # type: ignore[operator]
        result.chunks_extracted = len(chunks)
        result.chunks = chunks  # type: ignore[attr-defined]
    except FileNotFoundError:
        result.errors.append(f"file not found: {path!r}")
        result.chunks = []  # type: ignore[attr-defined]
    except Exception as exc:
        result.errors.append(f"{modality or 'multimodal'} parse error for {path!r}: {exc}")
        result.chunks = []  # type: ignore[attr-defined]
    return result
