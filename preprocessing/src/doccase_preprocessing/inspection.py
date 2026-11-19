"""Bounded format/structure checks, not document extraction or coverage acceptance."""

from __future__ import annotations

import posixpath
import re
import struct
import warnings
import zipfile
import zlib
from io import BytesIO
from pathlib import PurePosixPath
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader
from pypdf.errors import DependencyError, FileNotDecryptedError, PyPdfError
from pypdf.generic import ArrayObject, DictionaryObject, NumberObject

from .models import Inspection, Issue, Limits, MaterialError

_EXTENSIONS = {
    ".pdf": "pdf",
    ".xlsx": "xlsx",
    ".docx": "docx",
    ".xls": "xls",
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".txt": "text",
    ".html": "html",
    ".htm": "html",
}
_MIMES = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-excel": "xls",
    "image/png": "png",
    "image/jpeg": "jpeg",
    "text/plain": "text",
    "text/html": "html",
}
_GENERIC_MIMES = {"", "application/octet-stream"}
_CFB = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
_OFFICE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "http://purl.oclc.org/ooxml/officeDocument/relationships",
)
_SHEET_NS = (
    "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "http://purl.oclc.org/ooxml/spreadsheetml/main",
)
_WORD_NS = (
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "http://purl.oclc.org/ooxml/wordprocessingml/main",
)
_MAIN_TYPES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml": "xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml": "docx",
}


def _limit(actual: int, limit: int, resource: str) -> None:
    if actual > limit:
        raise MaterialError(
            "limit_exceeded",
            f"{resource} exceeds the configured limit",
            actual=actual,
            limit=limit,
            resource=resource,
            content_coverage="not_assessed",
        )


def _match(fmt: str, extension: str, mime: str) -> None:
    if (extension and _EXTENSIONS.get(extension) != fmt) or (
        mime not in _GENERIC_MIMES and _MIMES.get(mime) != fmt
    ):
        raise MaterialError(
            "format_mismatch",
            "Filename, MIME and detected format disagree",
            extension=extension,
            mime=mime,
            detected_format=fmt,
        )


class _NoDoctype(ET.TreeBuilder):
    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        # Parser-level rejection also covers UTF-16; byte regexes do not.
        raise MaterialError("corrupt", "XML DOCTYPE and entity declarations are forbidden")


def _xml(data: bytes) -> ET.Element:
    try:
        return ET.fromstring(data, parser=ET.XMLParser(target=_NoDoctype()))
    except ET.ParseError as exc:
        raise MaterialError("corrupt", "Malformed package XML") from exc
    except LookupError as exc:
        raise MaterialError("corrupt", "Package XML declares an unknown encoding") from exc


def _target(source: str, target: str) -> str:
    target = unquote(target)
    if not target or "\\" in target or "\x00" in target or re.search(r"[:?#]", target):
        raise MaterialError("corrupt", "Invalid internal package relationship target")
    if target.startswith("//"):
        raise MaterialError("corrupt", "Network relationship cannot be an internal part")
    parts: list[str] = []
    path = target if target.startswith("/") else posixpath.join(posixpath.dirname(source), target)
    for segment in path.split("/"):
        if segment == "..":
            if not parts:
                raise MaterialError("corrupt", "Relationship escapes the package")
            parts.pop()
        elif segment not in {"", "."}:
            parts.append(segment)
    if not parts:
        raise MaterialError("corrupt", "Relationship does not identify a part")
    return "/".join(parts)


def _relationships(root: ET.Element) -> dict[str, dict[str, str]]:
    if root.tag != f"{{{_RELS}}}Relationships":
        raise MaterialError("corrupt", "Invalid relationships root")
    result = {}
    for child in root:
        if child.tag != f"{{{_RELS}}}Relationship":
            raise MaterialError("corrupt", "Invalid relationship element")
        rel = child.attrib
        key = rel.get("Id")
        if not key or key in result or not rel.get("Type") or not rel.get("Target"):
            raise MaterialError("corrupt", "Incomplete or duplicate relationship")
        if rel.get("TargetMode", "Internal") not in {"Internal", "External"}:
            raise MaterialError("corrupt", "Invalid relationship mode")
        result[key] = rel
    return result


def _required_xml(xml: dict[str, ET.Element], part: str) -> ET.Element:
    if part not in xml:
        raise MaterialError("corrupt", "Required XML part is missing", part=part)
    return xml[part]


def _internal_part(source: str, rel: dict[str, str]) -> str:
    if rel.get("TargetMode") == "External":
        raise MaterialError("corrupt", "Required document part is external; it was not fetched")
    return _target(source, rel["Target"])


def _read_zip(raw: bytes, limits: Limits) -> tuple[set[str], dict[str, ET.Element]]:
    xml: dict[str, ET.Element] = {}
    try:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            members = archive.infolist()
            _limit(len(members), limits.max_archive_entries, "archive_entries")
            _limit(
                sum(member.file_size for member in members),
                limits.max_unpacked_bytes,
                "unpacked_bytes",
            )
            names: set[str] = set()
            for member in members:
                name = member.filename
                if name in names:
                    raise MaterialError("corrupt", "Duplicate ZIP member", member=name)
                names.add(name)
                # No extraction occurs, but ambiguous/unsafe part names are rejected.
                if (
                    member.orig_filename != name
                    or name.startswith("/")
                    or "\\" in name
                    or ":" in name
                    or any(p in {".", "..", ""} for p in name.rstrip("/").split("/"))
                ):
                    raise MaterialError("corrupt", "Invalid ZIP member name", member=name)
                if member.flag_bits & 1:
                    raise MaterialError("encrypted", "Encrypted ZIP member", member=name)
                if member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise MaterialError("unsupported_format", "Unsupported ZIP compression")
            unpacked = 0
            for member in members:
                is_xml = member.filename.endswith((".xml", ".rels"))
                chunks = []
                with archive.open(member) as stream:
                    while chunk := stream.read(64 * 1024):
                        unpacked += len(chunk)
                        _limit(unpacked, limits.max_unpacked_bytes, "unpacked_bytes")
                        if is_xml:
                            chunks.append(chunk)
                if is_xml:
                    xml[member.filename] = _xml(b"".join(chunks))
            return names, xml
    except (zipfile.BadZipFile, EOFError, zlib.error, UnicodeDecodeError) as exc:
        raise MaterialError("corrupt", "ZIP structure or member CRC is invalid") from exc
    except NotImplementedError as exc:
        # zipfile uses this for unsupported compression/encryption flags.
        raise MaterialError("unsupported_format", "Unsupported ZIP member encoding") from exc


def _positive_int(value: str | None) -> int | None:
    if value is None:
        return None
    if not re.fullmatch(r"[0-9]+", value) or len(value) > 10 or int(value) < 1:
        raise MaterialError("corrupt", "Invalid positive worksheet index")
    return int(value)


def _hidden(value: str | None) -> bool:
    if value not in {None, "0", "1", "false", "true"}:
        raise MaterialError("corrupt", "Invalid worksheet hidden flag")
    return value in {"1", "true"}


def _workbook(xml: dict[str, ET.Element], main: str) -> Inspection:
    root = _required_xml(xml, main)
    if root.tag not in {f"{{{ns}}}workbook" for ns in _SHEET_NS}:
        raise MaterialError("corrupt", "Invalid workbook root")
    ns = root.tag.removesuffix("workbook")
    rel_part = posixpath.join(posixpath.dirname(main), "_rels", posixpath.basename(main) + ".rels")
    rels = _relationships(_required_xml(xml, rel_part))
    sheets = []
    seen_names, seen_ids, seen_parts = set(), set(), set()
    for sheet in root.findall(f"{ns}sheets/{ns}sheet"):
        name, sheet_id = sheet.get("name"), sheet.get("sheetId")
        rel_id = next((sheet.get(f"{{{r}}}id") for r in _OFFICE if sheet.get(f"{{{r}}}id")), None)
        state = sheet.get("state", "visible")
        if (
            not name
            or name in seen_names
            or sheet_id is None
            or sheet_id in seen_ids
            or rel_id not in rels
            or state not in {"visible", "hidden", "veryHidden"}
        ):
            raise MaterialError("corrupt", "Incomplete, duplicate or invalid workbook sheet")
        _positive_int(sheet_id)
        rel = rels[rel_id]
        if rel["Type"] not in {f"{r}/worksheet" for r in _OFFICE}:
            raise MaterialError("unsupported_format", "Non-worksheet sheet type is not supported")
        part = _internal_part(main, rel)
        if part in seen_parts:
            raise MaterialError("corrupt", "Multiple sheets reference the same worksheet")
        seen_names.add(name)
        seen_ids.add(sheet_id)
        seen_parts.add(part)
        worksheet = _required_xml(xml, part)
        if worksheet.tag != f"{ns}worksheet":
            raise MaterialError("corrupt", "Invalid worksheet root", part=part)
        dimension = worksheet.find(f"{ns}dimension")
        rows = [
            _positive_int(row.get("r"))
            for row in worksheet.findall(f"{ns}sheetData/{ns}row")
            if _hidden(row.get("hidden"))
        ]
        columns = []
        for column in worksheet.findall(f"{ns}cols/{ns}col"):
            if _hidden(column.get("hidden")):
                start, end = _positive_int(column.get("min")), _positive_int(column.get("max"))
                if start is None or end is None or start > end:
                    raise MaterialError("corrupt", "Invalid hidden column range")
                columns.append({"min": start, "max": end})
        merges = [merge.get("ref") for merge in worksheet.findall(f"{ns}mergeCells/{ns}mergeCell")]
        if any(not ref for ref in merges):
            raise MaterialError("corrupt", "Merge range is missing")
        sheets.append(
            {
                "name": name,
                "sheet_id": sheet_id,
                "relationship_id": rel_id,
                "state": state,
                "part": part,
                "dimension": dimension.get("ref") if dimension is not None else None,
                "dimension_source": "worksheet_declared",
                "hidden_rows": rows,
                "hidden_columns": columns,
                "merged_ranges": merges,
            }
        )
    if not sheets:
        raise MaterialError("corrupt", "Workbook has no declared worksheets")
    return Inspection(
        "xlsx",
        "docling",
        {
            "sheet_count": len(sheets),
            "sheets": sheets,
            "formula_cache": "not_assessed",
            "nonempty_cell_coverage": "not_assessed",
            "content_coverage": "not_assessed",
        },
        [
            Issue(
                "coverage_unverified",
                "Worksheet structure registered; formula cache freshness and actual nonempty-cell "
                "coverage are not verified. Formulas were not evaluated and fields were not extracted.",
            )
        ],
    )


def _ooxml(raw: bytes, extension: str, mime: str, limits: Limits) -> Inspection:
    names, xml = _read_zip(raw, limits)
    types = xml.get("[Content_Types].xml")
    overrides = types.findall(f"{{{_CT}}}Override") if types is not None else []
    formats = {
        _MAIN_TYPES[o.get("ContentType")] for o in overrides if o.get("ContentType") in _MAIN_TYPES
    }
    if not formats:
        formats = {
            fmt
            for prefix, fmt in (("xl/", "xlsx"), ("word/", "docx"))
            if any(name.startswith(prefix) for name in names)
        }
    if len(formats) != 1:
        if formats:
            raise MaterialError("corrupt", "Ambiguous OOXML document type")
        if extension in _EXTENSIONS or mime in _MIMES:
            _match("zip", extension, mime)
        raise MaterialError("unsupported_format", "Ordinary ZIP archives are not supported")
    fmt = formats.pop()
    _match(fmt, extension, mime)
    if types is None or types.tag != f"{{{_CT}}}Types":
        raise MaterialError("corrupt", "OOXML content types part is missing or invalid")
    rels = _relationships(_required_xml(xml, "_rels/.rels"))
    main_rels = [
        rel for rel in rels.values() if rel["Type"] in {f"{r}/officeDocument" for r in _OFFICE}
    ]
    if len(main_rels) != 1:
        raise MaterialError("corrupt", "OOXML requires one office document relationship")
    main = _internal_part("", main_rels[0])
    main_types = [
        o.get("ContentType") for o in overrides if _target("", o.get("PartName", "")) == main
    ]
    if len(main_types) != 1:
        raise MaterialError("corrupt", "Missing or duplicate main part content type")
    if _MAIN_TYPES.get(main_types[0]) != fmt:
        raise MaterialError("format_mismatch", "OOXML main part content type disagrees with format")
    # Validate relationship XML without ever resolving external resources.
    external_count = sum(
        rel.get("TargetMode") == "External"
        for name, root in xml.items()
        if name.endswith(".rels")
        for rel in _relationships(root).values()
    )
    if fmt == "xlsx":
        result = _workbook(xml, main)
    else:
        root = _required_xml(xml, main)
        if root.tag not in {f"{{{ns}}}document" for ns in _WORD_NS}:
            raise MaterialError("corrupt", "Invalid Word document root")
        ns = root.tag.removesuffix("document")
        if root.find(f"{ns}body") is None:
            raise MaterialError("corrupt", "Word document body is missing")
        result = Inspection(
            "docx",
            "docling",
            {
                "page_count": None,
                "media_parts": sorted(
                    n for n in names if n.startswith("word/media/") and not n.endswith("/")
                ),
                "content_coverage": "not_assessed",
            },
        )
    result.scope.update(
        {
            "main_part": main,
            "archive_entry_count": len(names),
            "external_relationship_count": external_count,
            "external_relationships_followed": False,
        }
    )
    return result


def _pdf(raw: bytes, limits: Limits) -> Inspection:
    try:
        reader = PdfReader(BytesIO(raw), strict=True)
        if reader.is_encrypted:
            raise MaterialError("encrypted", "PDF is encrypted", page_count=None)
        pages = reader.root_object.get("/Pages")
        pages = pages.get_object() if pages is not None else None
        if not isinstance(pages, DictionaryObject):
            raise MaterialError("corrupt", "PDF page tree is missing or invalid", page_count=None)
        children, count = pages.get("/Kids"), pages.get("/Count")
        count = count.get_object() if count is not None else None
        if (
            children is None
            or not isinstance(children.get_object(), ArrayObject)
            or not isinstance(count, NumberObject)
            or count < 0
        ):
            raise MaterialError("corrupt", "PDF page tree is incomplete", page_count=None)
        try:
            page_count = len(reader.pages)
        except TypeError as exc:
            # pypdf 6.16.1 _flatten tests /Type membership on a numeric /Kids entry.
            # Normalize only that reproduced input error, not unrelated library/program bugs.
            if str(exc) != "argument of type 'NumberObject' is not iterable":
                raise
            raise MaterialError(
                "corrupt", "PDF page tree contains a numeric child", page_count=None
            ) from exc
        if page_count != count:
            raise MaterialError(
                "corrupt", "PDF page tree count disagrees with enumerated pages", page_count=None
            )
    except (FileNotDecryptedError, DependencyError) as exc:
        raise MaterialError("encrypted", "PDF is encrypted", page_count=None) from exc
    except (PyPdfError, EOFError) as exc:
        raise MaterialError(
            "corrupt", "PDF cannot be opened or its pages enumerated", page_count=None
        ) from exc
    _limit(page_count, limits.max_pdf_pages, "pdf_pages")
    return Inspection(
        "pdf",
        "docling",
        {
            "page_count": page_count,
            "encrypted": False,
            "content_coverage": "not_assessed",
        },
    )


def _image(raw: bytes, fmt: str, limits: Limits) -> Inspection:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw), formats=["PNG", "JPEG"]) as img:
                width, height = img.size
                _limit(width * height, limits.max_image_pixels, "image_pixels")
                if img.format.lower() != fmt:
                    raise MaterialError("format_mismatch", "Image decoder disagrees with signature")
                # JPEG is single-frame and has no n_frames; PNG exposes it for APNG.
                frame_count = getattr(img, "n_frames", 1)
                if frame_count > 1:
                    raise MaterialError(
                        "unsupported_format",
                        "Animated PNG is not supported",
                        animated=True,
                        frame_count=frame_count,
                    )
                img.verify()
            # verify checks PNG CRC; load also detects truncated pixel data (notably JPEG).
            with Image.open(BytesIO(raw), formats=["PNG", "JPEG"]) as img:
                img.load()
    except Image.DecompressionBombError as exc:
        # Pillow's pinned decoder has an additional hard limit before open() returns.
        values = re.findall(r"\d+", str(exc))
        raise MaterialError(
            "limit_exceeded",
            "Image exceeds the decoder pixel limit",
            actual=int(values[0]),
            limit=min(limits.max_image_pixels, int(values[1])),
            resource="image_pixels",
            content_coverage="not_assessed",
        ) from exc
    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        EOFError,
        struct.error,
        zlib.error,
    ) as exc:
        raise MaterialError("corrupt", "Image integrity check failed") from exc
    return Inspection(
        fmt,
        "docling",
        {
            "width": width,
            "height": height,
            "pixel_count": width * height,
            "frame_count": frame_count,
            "animated": False,
            "content_coverage": "not_assessed",
        },
    )


def _encrypted_cfb(raw: bytes) -> bool:
    # Recognize directory-entry-shaped stream names, not arbitrary text in an XLS cell.
    # This is only a container signature check, not a CFB stream reader/decryptor.
    names = set()
    for offset in range(512, len(raw) - 127, 128):
        size = struct.unpack_from("<H", raw, offset + 64)[0]
        if raw[offset + 66] == 2 and size in {30, 34}:
            names.add(raw[offset : offset + size])
    return {
        "EncryptionInfo\0".encode("utf-16-le"),
        "EncryptedPackage\0".encode("utf-16-le"),
    } <= names


def inspect_material(
    raw: bytes,
    filename: str,
    mime: str,
    limits: Limits | None = None,
    *,
    allow_markdown: bool = False,
) -> Inspection:
    """Check supplied bytes only. Routes plan parsing; no parser/converter is executed."""
    limits = limits if limits is not None else Limits()
    _limit(len(raw), limits.max_material_bytes, "material_bytes")
    extension = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    mime = mime.partition(";")[0].strip().lower()
    declared = (
        "markdown"
        if allow_markdown and extension == ".md" and mime == "text/markdown"
        else _EXTENSIONS.get(extension) or _MIMES.get(mime)
    )
    if declared and declared != "markdown":
        _match(declared, extension, mime)

    if raw.startswith(b"%PDF-"):
        _match("pdf", extension, mime)
        return _pdf(raw, limits)
    if raw.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return _ooxml(raw, extension, mime, limits)
    if raw.startswith(_CFB):
        if len(raw) < 512:
            raise MaterialError("corrupt", "Truncated CFB container")
        if _encrypted_cfb(raw):
            if declared and declared not in {"xlsx", "docx"}:
                raise MaterialError(
                    "format_mismatch", "Encrypted OOXML disagrees with declared format"
                )
            raise MaterialError("encrypted", "OLE container has encrypted OOXML stream signatures")
        if declared == "xls":
            return Inspection(
                "xls",
                None,
                {
                    "container": "cfb",
                    "sheet_count": None,
                    "format_identification": "cfb_signature_and_declarations",
                    "content_coverage": "not_assessed",
                },
                [
                    Issue(
                        "conversion_required", "Legacy XLS requires a separate conversion or reader"
                    )
                ],
            )
        if declared:
            raise MaterialError("format_mismatch", "CFB signature disagrees with declared format")
        raise MaterialError("unsupported_format", "Unidentified CFB document")
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        _match("png", extension, mime)
        return _image(raw, "png", limits)
    if raw.startswith(b"\xff\xd8"):
        _match("jpeg", extension, mime)
        return _image(raw, "jpeg", limits)
    if declared in {"text", "html", "markdown"}:
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise MaterialError(
                "invalid_encoding", "Text must use valid UTF-8", offset=exc.start
            ) from exc
        if "\x00" in text:
            raise MaterialError("format_mismatch", "NUL-containing binary data is not UTF-8 text")
        return Inspection(
            declared,
            "text" if declared == "text" else "docling",
            {
                "encoding": "utf-8",
                "byte_count": len(raw),
                "character_count": len(text),
                "content_coverage": "not_assessed",
            },
        )
    if declared:
        raise MaterialError(
            "format_mismatch", "Required format signature is absent", declared_format=declared
        )
    raise MaterialError("unsupported_format", "Material format is not supported")
