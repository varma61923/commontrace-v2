"""Zero-dependency multimodal ingestion (pdf/docx/html/image).

Stdlib only: no external libs. Each chunker returns ``list[Chunk]``
using the shared :class:`commontrace.ingest.Chunk` shape, with
``modality`` + source provenance carried on every chunk:

- ``chunk.chunk_type`` encodes the modality (``pdf_text``,
  ``docx_text``, ``html_text``, ``image_description``).
- ``chunk.source_path`` is the ingested file (provenance).
- ``chunk.modality`` (dynamic attr) is one of
  ``pdf`` / ``docx`` / ``html`` / ``image``.
- ``chunk.provenance`` (dynamic attr) is a dict with
  ``source_path``, ``modality`` and ``extractor`` keys.

Dispatch contract for the lead:

- ``INGEST_FNS`` maps file extension -> ``callable(path) -> list[Chunk]``.
- ``ingest_multimodal(path)`` dispatches by extension and returns
  an :class:`IngestionResult` with ``chunks_extracted`` populated
  (errors recorded, never raised).
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

        root = ET.fromstring(xml_bytes)
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
}

_EXT_MODALITY = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".html": "html",
    ".htm": "html",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
}

_MODALITY_SOURCE_TYPE = {
    "pdf": "pdf",
    "docx": "docx",
    "html": "html",
    "image": "image",
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
