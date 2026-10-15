"""Bounded, offline OOXML text extraction; no formulas, macros or links execute."""
from __future__ import annotations

import io
import json
import os
import posixpath
import re
import stat
import struct
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass

from commontrace.ingest import Chunk, _open_regular, _redact_secrets

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_XML_BYTES = 32 * 1024 * 1024
MAX_TEXT_CHARS = 8 * 1024 * 1024
MAX_XML_NODES = 200_000
MAX_XML_DEPTH = 128
MAX_MEMBERS = 4096
MAX_PARTS = 1024
MAX_DIRECTORY_BYTES = 4 * 1024 * 1024
_REL_NS = ("http://schemas.openxmlformats.org/officeDocument/2006/relationships",
           "http://purl.oclc.org/ooxml/officeDocument/relationships")
_SHEET_NS = ("http://schemas.openxmlformats.org/spreadsheetml/2006/main",
             "http://purl.oclc.org/ooxml/spreadsheetml/main")
_SLIDE_NS = ("http://schemas.openxmlformats.org/presentationml/2006/main",
             "http://purl.oclc.org/ooxml/presentationml/main")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _directory_guard(handle) -> None:
    """Check central-directory budgets before ZipFile allocates its entry objects."""
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    length = min(size, 65535 + 22)
    handle.seek(size - length)
    tail = handle.read(length)
    for match in reversed(list(re.finditer(b"PK\x05\x06", tail))):
        offset = match.start()
        if offset + 22 > length:
            continue
        _, disk, directory_disk, disk_entries, entries, directory_size, directory_at, comment = \
            struct.unpack_from("<4s4H2LH", tail, offset)
        if offset + 22 + comment != length:
            continue
        if (disk or directory_disk or disk_entries != entries or entries > MAX_MEMBERS
                or directory_size > MAX_DIRECTORY_BYTES
                or directory_at + directory_size != size - length + offset):
            raise ValueError("Office archive exceeds central-directory budget or uses unsupported ZIP layout")
        handle.seek(0)
        return
    raise ValueError("Office archive has no valid end-of-directory record")


@dataclass
class _Package:
    archive: zipfile.ZipFile
    xml_bytes: int = 0
    nodes: int = 0
    text_chars: int = 0

    def xml(self, part: str) -> ET.Element:
        try:
            info = self.archive.getinfo(part)
        except KeyError as exc:
            raise ValueError(f"Office archive is missing required part {part!r}") from exc
        remaining = MAX_XML_BYTES - self.xml_bytes
        if info.file_size > remaining:
            raise ValueError("Office XML exceeds aggregate decompression budget")
        with self.archive.open(info) as member:
            data = member.read(min(info.file_size, remaining) + 1)
        self.xml_bytes += len(data)
        if self.xml_bytes > MAX_XML_BYTES:
            raise ValueError("Office XML exceeds aggregate decompression budget")
        if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", data.replace(b"\x00", b""), re.IGNORECASE):
            raise ValueError("Office XML declarations/entities are not allowed")
        depth, root = 0, None
        try:
            # No DTD/entities in accepted encodings; explicit element/depth caps
            # bound the tree constructed by the stdlib parser.
            for event, element in ET.iterparse(io.BytesIO(data), events=("start", "end")):  # nosec B314
                if event == "start":
                    depth += 1
                    self.nodes += 1
                    if depth > MAX_XML_DEPTH or self.nodes > MAX_XML_NODES:
                        raise ValueError("Office XML exceeds element/depth budget")
                    if root is None:
                        root = element
                else:
                    depth -= 1
        except ET.ParseError as exc:
            raise ValueError(f"invalid Office XML in {part!r}: {exc}") from exc
        if root is None:
            raise ValueError("Office XML is empty")
        return root

    def text(self, value: str) -> str:
        value = _redact_secrets(value)
        self.text_chars += len(value)
        if self.text_chars > MAX_TEXT_CHARS:
            raise ValueError("Office extracted text exceeds character budget")
        return value

    def relationships(self, part: str) -> dict[str, tuple[str, str]]:
        folder, name = posixpath.split(part)
        rels = posixpath.join(folder, "_rels", name + ".rels")
        try:
            self.archive.getinfo(rels)
        except KeyError:
            return {}
        root = self.xml(rels)
        if _local(root.tag) != "Relationships":
            raise ValueError("invalid Office relationship document")
        result = {}
        for node in root:
            if _local(node.tag) != "Relationship" or node.get("TargetMode") == "External":
                continue
            identity, target, kind = node.get("Id", ""), node.get("Target", ""), node.get("Type", "")
            if not identity or len(identity) > 128 or identity in result or not target or len(target) > 512:
                raise ValueError("invalid or duplicate Office relationship")
            url = urllib.parse.urlsplit(target)
            decoded = urllib.parse.unquote(url.path)
            if url.scheme or url.netloc or url.query or url.fragment or "\\" in decoded or ":" in decoded:
                raise ValueError("Office relationship must name an internal package part")
            resolved = posixpath.normpath(decoded.lstrip("/") if decoded.startswith("/")
                                         else posixpath.join(folder, decoded))
            if resolved in ("", ".", "..") or resolved.startswith("../"):
                raise ValueError("Office relationship escapes the package")
            result[identity] = (resolved, kind.rsplit("/", 1)[-1])
        return result


@contextmanager
def _package(path: str):
    with _open_regular(path, MAX_FILE_BYTES) as handle:
        initial = os.fstat(handle.fileno())
        _directory_guard(handle)
        with zipfile.ZipFile(handle) as archive:
            names = set()
            for info in archive.infolist():
                name = info.filename
                if (name in names or len(name) > 512 or name.startswith("/") or "\\" in name
                        or ".." in name.split("/") or info.flag_bits & 1
                        or stat.S_ISLNK(info.external_attr >> 16)
                        or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    raise ValueError("Office archive contains ambiguous or unsupported members")
                names.add(name)
            yield _Package(archive)
        final = os.fstat(handle.fileno())
        if (initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns) != \
                (final.st_size, final.st_mtime_ns, final.st_ctime_ns):
            raise ValueError("Office source changed during extraction; retry required")


def _relationship_id(node: ET.Element) -> str:
    return next((node.get("{" + namespace + "}id", "") for namespace in _REL_NS
                 if node.get("{" + namespace + "}id")), "")


def _root_kind(root: ET.Element, namespaces: tuple[str, ...], kind: str) -> None:
    if root.tag not in tuple("{" + namespace + "}" + kind for namespace in namespaces):
        raise ValueError(f"Office part is not a supported {kind} document")


def _chunks(path: str, modality: str, text: str, breadcrumb: str, metadata: dict) -> list[Chunk]:
    from commontrace.ingest.multimodal import _MAX_MM_CHUNK, _make_chunk

    text = _redact_secrets(text)
    chunks, start = [], 0
    while start < len(text):
        end = min(start + _MAX_MM_CHUNK, len(text))
        if end < len(text):
            cut = text.rfind(" ", start, end)
            if cut > start:
                end = cut + 1
        chunk = _make_chunk(text[start:end], path, modality, modality + "_text", breadcrumb, len(chunks))
        chunk.provenance.update(extractor="commontrace.ingest.office", **metadata)
        chunks.append(chunk)
        start = end
    return chunks


def _cell_value(cell: ET.Element, strings: list[str]) -> str:
    kind = cell.get("t", "n")
    value_node = next((node for node in cell if _local(node.tag) == "v"), None)
    value = (value_node.text or "") if value_node is not None else ""
    if kind == "inlineStr":
        inline = next((node for node in cell if _local(node.tag) == "is"), None)
        return _rich_text(inline) if inline is not None else ""
    if kind == "s":
        if not re.fullmatch(r"[0-9]{1,10}", value) or int(value) >= len(strings):
            raise ValueError("spreadsheet cell has an invalid shared-string reference")
        return strings[int(value)]
    if kind == "b":
        if value not in ("0", "1", "false", "true"):
            raise ValueError("spreadsheet cell has an invalid boolean value")
        return "TRUE" if value in ("1", "true") else "FALSE"
    if kind not in ("n", "str", "e", "d"):
        raise ValueError("spreadsheet cell has an unsupported value type")
    if value_node is None and any(_local(node.tag) == "f" for node in cell):
        return "[formula has no stored cached value]"
    return value


def _rich_text(root: ET.Element) -> str:
    # Phonetic rPh annotations are not displayed cell values.
    return "".join(node.text or "" for child in root
                   for node in ([child] if _local(child.tag) == "t" else child if _local(child.tag) == "r" else [])
                   if _local(node.tag) == "t")


def chunk_xlsx(path: str) -> list[Chunk]:
    """Stored cell values, in workbook/sheet/row order; no formula evaluation."""
    path = os.path.abspath(path)
    with _package(path) as package:
        workbook = package.xml("xl/workbook.xml")
        _root_kind(workbook, _SHEET_NS, "workbook")
        relationships = package.relationships("xl/workbook.xml")
        strings_part = next((part for part, kind in relationships.values() if kind == "sharedStrings"), None)
        strings = []
        if strings_part:
            root = package.xml(strings_part)
            _root_kind(root, _SHEET_NS, "sst")
            strings = [_rich_text(item) for item in root if _local(item.tag) == "si"]
        chunks, names = [], set()
        sheets = [node for node in workbook.iter() if _local(node.tag) == "sheet"]
        if len(sheets) > MAX_PARTS:
            raise ValueError("spreadsheet exceeds sheet budget")
        for sheet in sheets:
            name = sheet.get("name", "")
            if not name or len(name) > 128 or name.casefold() in names:
                raise ValueError("spreadsheet has invalid or duplicate sheet names")
            names.add(name.casefold())
            part, kind = relationships.get(_relationship_id(sheet), ("", ""))
            if kind != "worksheet":
                raise ValueError("spreadsheet sheet has no internal worksheet relationship")
            root = package.xml(part)
            _root_kind(root, _SHEET_NS, "worksheet")
            seen_rows, previous_row = set(), 0
            for row in (node for node in root.iter() if _local(node.tag) == "row"):
                index = row.get("r", str(previous_row + 1))
                if not re.fullmatch(r"[1-9][0-9]{0,6}", index) or int(index) > 1_048_576 or index in seen_rows:
                    raise ValueError("spreadsheet has invalid or duplicate row addresses")
                seen_rows.add(index)
                previous_row = int(index)
                values, seen_cells, previous_column = [], set(), 0
                for cell in (node for node in row if _local(node.tag) == "c"):
                    address = cell.get("r", "")
                    if not address:
                        number, letters = previous_column + 1, ""
                        while number:
                            number, remainder = divmod(number - 1, 26)
                            letters = chr(65 + remainder) + letters
                        address = letters + index
                    match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]{0,6})", address)
                    if not match or match.group(2) != index or address in seen_cells:
                        raise ValueError("spreadsheet has invalid or duplicate cell addresses")
                    column_number = 0
                    for letter in match.group(1):
                        column_number = column_number * 26 + ord(letter) - 64
                    if column_number > 16_384:
                        raise ValueError("spreadsheet cell exceeds column address budget")
                    previous_column = column_number
                    seen_cells.add(address)
                    value = _cell_value(cell, strings)
                    if value:
                        cell_type = cell.get("t", "n")
                        encoded = json.dumps(value, ensure_ascii=False) if cell_type in ("s", "str", "d", "inlineStr") \
                            else value
                        values.append(package.text((" | " if values else "") + address + "[" + cell_type + "]="
                                                   + encoded))
                if values:
                    breadcrumb = f"{os.path.basename(path)} / sheet {name} / row {index}"
                    chunks.extend(_chunks(path, "xlsx", "".join(values), breadcrumb,
                                          {"source_part": part, "sheet": name, "row": int(index),
                                           "sheet_state": sheet.get("state", "visible"),
                                           "value_policy": "stored-values-no-formatting-no-evaluation"}))
        if not chunks:
            chunks = _chunks(path, "xlsx", "[XLSX] No stored cell values.", os.path.basename(path),
                             {"source_part": "xl/workbook.xml", "kind": "empty_document"})
        return chunks


def chunk_pptx(path: str) -> list[Chunk]:
    """Slide paragraphs and speaker notes in presentation order, with part identity."""
    path = os.path.abspath(path)
    with _package(path) as package:
        presentation = package.xml("ppt/presentation.xml")
        _root_kind(presentation, _SLIDE_NS, "presentation")
        relationships = package.relationships("ppt/presentation.xml")
        slides = [node for node in presentation.iter() if _local(node.tag) == "sldId"]
        if len(slides) > MAX_PARTS:
            raise ValueError("presentation exceeds slide budget")
        chunks, seen = [], set()
        for index, slide in enumerate(slides, 1):
            part, kind = relationships.get(_relationship_id(slide), ("", ""))
            if kind != "slide" or part in seen:
                raise ValueError("presentation has missing or duplicate slide relationships")
            seen.add(part)
            root = package.xml(part)
            _root_kind(root, _SLIDE_NS, "sld")
            parts = [(part, root, "slide")]
            for notes, relation in package.relationships(part).values():
                if relation == "notesSlide":
                    notes_root = package.xml(notes)
                    _root_kind(notes_root, _SLIDE_NS, "notes")
                    parts.append((notes, notes_root, "speaker_notes"))
            for source_part, content, category in parts:
                paragraph = 0
                for shape in (node for node in content.iter() if _local(node.tag) in ("sp", "graphicFrame")):
                    if category == "speaker_notes" and any(
                            _local(node.tag) == "ph" and node.get("type") in ("sldNum", "hdr", "ftr", "dt")
                            for node in shape.iter()):
                        continue
                    for node in shape.iter():
                        if _local(node.tag) != "p":
                            continue
                        text = "".join(child.text or "" if _local(child.tag) == "t" else "\n"
                                       for child in node.iter() if _local(child.tag) in ("t", "br")).strip()
                        if not text:
                            continue
                        paragraph += 1
                        text = package.text(text)
                        breadcrumb = f"{os.path.basename(path)} / slide {index} / {category} / paragraph {paragraph}"
                        chunks.extend(_chunks(path, "pptx", text, breadcrumb,
                                              {"source_part": source_part, "slide": index, "kind": category,
                                               "paragraph": paragraph}))
        if not chunks:
            chunks = _chunks(path, "pptx", "[PPTX] No extractable slide text.", os.path.basename(path),
                             {"source_part": "ppt/presentation.xml", "kind": "empty_document"})
        return chunks
