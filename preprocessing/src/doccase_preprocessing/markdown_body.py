"""Loss-aware Markdown representation for one selected local email body."""

import hashlib
import html
import re
from html.parser import HTMLParser

VERSION = "body-markdown-4"
_LEGACY_VERSION = "body-markdown-1"
_PACKING_VERSION = "body-markdown-3"
_SUPPORTED_VERSIONS = {_LEGACY_VERSION, "body-markdown-2", _PACKING_VERSION, VERSION}
_PACKING_COLUMNS = ("package", "length", "width", "height", "cbm", "gross weight")
_MARKDOWN = re.compile(r"([\\`*_\[\]<>])")
_LEGACY_MARKDOWN = re.compile(r"([\\`*_{}\[\]<>#+\-.!|])")
_HEADER = re.compile(r"^\s*(?:from|de|发件人)\s*[:：]", re.I)
_SENT = re.compile(r"^\s*(?:sent|date|enviado(?: el)?|fecha|发送时间|日期)\s*[:：]", re.I)
_TO = re.compile(r"^\s*(?:to|para|收件人)\s*[:：]", re.I)
_PORTUGUESE_SENT = re.compile(r"^\s*enviad[oa]\s+em\s*[:：]", re.I)
_RETAIN_BLOCK_FOR = {
    "table_without_explicit_header",
    "merged_table_cells",
    "nested_table",
    "irregular_table",
    "quoted_table",
    "unsupported_list_structure",
}


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _escape(text, *, table=False, version=VERSION):
    if version == _LEGACY_VERSION:
        return _LEGACY_MARKDOWN.sub(r"\\\1", text)
    escaped = _MARKDOWN.sub(r"\\\1", text)
    escaped = re.sub(
        r"^( {0,3})(#{1,6})(?=\s|$)",
        lambda match: match.group(1) + "".join("\\#" for _ in match.group(2)),
        escaped,
    )
    escaped = re.sub(r"^( {0,3})([+-])(?=\s)", r"\1\\\2", escaped)
    escaped = re.sub(r"^( {0,3}\d{1,9})([.)])(?=\s)", r"\1\\\2", escaped)
    return escaped.replace("|", "\\|") if table else escaped


class _Fallback(Exception):
    pass


class _Converter:
    def __init__(self, source, version):
        self.source = source
        self.version = version
        self.output = ""
        self.segments = []

    def emit(self, text, fragments, section="current", prefix="", suffix="\n\n", table=False):
        if not text:
            return
        if section == "quoted":
            prefix = "> " + prefix
        encoded = _escape(text, table=table, version=self.version)
        self.output += prefix
        start = len(self.output)
        self.output += encoded
        self.segments.append(
            {
                "text": text,
                "section": section,
                "md_start": start,
                "md_end": len(self.output),
                "md_text": encoded,
                "source_fragments": fragments,
            }
        )
        self.output += suffix

    def fragment(self, start, end, value=None):
        raw = self.source[start:end]
        return {
            "start": start,
            "end": end,
            "raw": raw,
            "decoded": html.unescape(raw) if value is None else value,
        }

    def resolve_headers(self):
        # A recognisable forwarding header starts the historical section. An
        # isolated From/De label is not enough to establish a boundary.
        for index, item in enumerate(self.segments):
            if item["section"] != "current" or not _HEADER.match(item["text"]):
                continue
            window = [x["text"] for x in self.segments[index : index + 16]]
            sent = any(
                _SENT.match(x) or (self.version == VERSION and _PORTUGUESE_SENT.match(x))
                for x in window
            )
            if not sent or not any(_TO.match(x) for x in window):
                raise _Fallback("ambiguous_quote_boundary")
            for following in self.segments[index:]:
                following["section"] = "quoted"
            break


class _Html(_Converter, HTMLParser):
    _BLOCK = {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li"}

    def __init__(self, source, version):
        _Converter.__init__(self, source, version)
        HTMLParser.__init__(self, convert_charrefs=False)
        self.lines = [0]
        for match in re.finditer("\n", source):
            self.lines.append(match.end())
        self.fragments = []
        self.ignored = []
        self.quote = []
        self.list_depth = 0
        self.in_item = False
        self.table = None
        self.row = None
        self.cell = None
        self.links = []

    def position(self):
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def section(self):
        return "quoted" if self.quote else "current"

    def _check_element(self, tag, attrs):
        if tag in {"sup", "sub"} and self.version == VERSION:
            opening = self.get_starttag_text()
            rest = self.source[self.position() + len(opening) :]
            if opening.endswith("/>") or re.match(rf"\s*</{tag}\s*>", rest, re.I):
                return
        if tag in {"sup", "sub", "svg", "canvas", "math", "textarea", "input"}:
            raise _Fallback("unsupported_html_semantics")
        if re.search(
            r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", attrs.get("style", ""), re.I
        ):
            raise _Fallback("hidden_html_content")

    def flush(self):
        if not self.fragments:
            return
        text = re.sub(r"\s+", " ", "".join(x["decoded"] for x in self.fragments)).strip()
        fragments, self.fragments = self.fragments, []
        if self.cell is not None:
            self.cell["fragments"].extend(fragments)
            self.cell["text"] += (" " if self.cell["text"] and text else "") + text
        else:
            self.emit(text, fragments, self.section(), "- " if self.in_item else "")

    def handle_starttag(self, tag, attrs):
        if self.ignored:
            if tag in {"head", "style", "script"}:
                self.ignored.append(tag)
            return
        if tag in {"head", "style", "script"}:
            self.ignored.append(tag)
            return
        attrs = dict(attrs)
        self._check_element(tag, attrs)
        if tag == "blockquote" or "gmail_quote" in attrs.get("class", ""):
            self.flush()
            self.quote.append(tag)
        if tag == "table":
            self.flush()
            if self.table is not None:
                raise _Fallback("nested_table")
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in {"td", "th"} and self.table is not None:
            if self.cell is not None or any(
                attrs.get(x, "1") != "1" for x in ("rowspan", "colspan")
            ):
                raise _Fallback("merged_table_cells")
            self.cell = {
                "text": "",
                "fragments": [],
                "header": tag == "th",
                "section": self.section(),
            }
        elif tag in self._BLOCK:
            self.flush()
            if tag == "li":
                self.in_item = True
        elif tag in {"ul", "ol"}:
            self.flush()
            self.list_depth += 1
            if self.list_depth > 1 or tag == "ol":
                raise _Fallback("unsupported_list_structure")
        elif tag == "a" and attrs.get("href"):
            start = self.position()
            self.links.append(
                (
                    attrs["href"],
                    self.fragment(start, start + len(self.get_starttag_text()), attrs["href"]),
                )
            )
        elif tag == "img":
            self.flush()
            start = self.position()
            raw = self.get_starttag_text()
            src, alt = attrs.get("src", ""), attrs.get("alt", "")
            if self.version == VERSION and not (src or alt or attrs.get("srcset")):
                return  # Empty layout placeholder, not an image reference.
            if not src or re.search(r"[\s()]", src):
                raise _Fallback("unsupported_image_reference")
            self.emit(
                f"[image: {alt}] {src}", [self.fragment(start, start + len(raw), alt)], "unknown"
            )

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in {"br", "img", "meta", "link", "hr", "input"}:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.ignored:
            if tag == self.ignored[-1]:
                self.ignored.pop()
            return
        if tag in {"td", "th"} and self.cell is not None:
            self.flush()
            self.row.append(self.cell)
            self.cell = None
        elif tag == "tr" and self.table is not None and self.row is not None:
            self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            rows = self.table
            self.table = None
            header = rows[0] if rows else []
            explicit_header = header and all(cell["header"] for cell in header)
            # This packing grid uses ordinary cells for a complete, fixed label row.
            packing_header = (
                self.version in {_PACKING_VERSION, VERSION}
                and len(rows) > 1
                and tuple(cell["text"].casefold() for cell in header) == _PACKING_COLUMNS
            )
            if not (explicit_header or packing_header):
                raise _Fallback("table_without_explicit_header")
            if any(len(row) != len(rows[0]) for row in rows) or any(
                cell["section"] != rows[0][0]["section"] for row in rows for cell in row
            ):
                raise _Fallback("irregular_table")
            if rows[0][0]["section"] != "current":
                raise _Fallback("quoted_table")
            for index, row in enumerate(rows):
                self.output += "| "
                for col, cell in enumerate(row):
                    self.emit(
                        cell["text"], cell["fragments"], cell["section"], suffix="", table=True
                    )
                    self.output += " | " if col + 1 < len(row) else " |\n"
                if index == 0:
                    self.output += "| " + " | ".join("---" for _ in row) + " |\n"
            self.output += "\n"
        elif tag == "a" and self.links:
            href, fragment = self.links.pop()
            self.flush()
            self.emit(href, [fragment], "unknown")
        elif tag in self._BLOCK:
            self.flush()
            if tag == "li":
                self.in_item = False
        elif tag in {"ul", "ol"}:
            self.flush()
            self.list_depth -= 1
        if self.quote and tag == self.quote[-1]:
            self.flush()
            self.quote.pop()

    def _record_fragment(self, fragment):
        self.fragments.append(fragment)

    def handle_data(self, data):
        if not self.ignored:
            start = self.position()
            self._record_fragment(self.fragment(start, start + len(data), data))

    def _entity(self):
        if not self.ignored:
            start = self.position()
            match = re.match(
                r"&(?:#\d+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]+);?", self.source[start:]
            )
            if match:
                self._record_fragment(self.fragment(start, start + len(match.group())))

    def handle_entityref(self, name):
        self._entity()

    def handle_charref(self, name):
        self._entity()


class _HtmlWithBlocks(_Html):
    """Retain HTML islands that cannot be represented by a Markdown pipe table/list."""

    _STRUCTURES = {"table", "ol", "ul"}
    _TEXT_UNITS = {"td", "th", "li", "caption"}
    _DIV_END = re.compile(r"</div\s*>", re.I)
    _BLOCK_CHILD = re.compile(r"<(?:div|p|table|ol|ul|blockquote|h[1-6])\b", re.I)
    _EMPHASIS = re.compile(r"<(?:b|strong)\b", re.I)

    def __init__(self, source, version, *, retain_paragraphs=False):
        super().__init__(source, version)
        # Keep the original inline groups alongside lists; joining them into one
        # line can turn a partial location into a competing destination candidate.
        self.block_tags = self._STRUCTURES | {"p", "div"} if retain_paragraphs else self._STRUCTURES
        self.structures = []

    def _emphasized_inline_div(self):
        start = self.position() + len(self.get_starttag_text())
        close = self._DIV_END.search(self.source, start)
        if not close:
            return False
        return self._EMPHASIS.search(
            self.source, start, close.start()
        ) and not self._BLOCK_CHILD.search(self.source, start, close.start())

    def handle_starttag(self, tag, attrs):
        if not self.structures:
            if self.ignored or tag not in self.block_tags:
                return super().handle_starttag(tag, attrs)
            if tag == "div" and not self._emphasized_inline_div():
                return super().handle_starttag(tag, attrs)
            self.flush()
            self.block_start = self.position()
            self.block_section = self.section()
            self.units = [(None, [])]
            self.block_segments = []

        attrs = dict(attrs)
        self._check_element(tag, attrs)
        if tag in {"blockquote", "head", "script", "style", "pre"} or "gmail_quote" in attrs.get(
            "class", ""
        ):
            raise _Fallback("unsupported_embedded_html_scope")
        if tag in self.block_tags:
            self.structures.append(tag)
        if tag in self._TEXT_UNITS:
            self.units.append((tag, []))
        elif tag in self._BLOCK:
            self.units[-1][1].append(None)

    def _record_fragment(self, fragment):
        if self.structures:
            self.units[-1][1].append(fragment)
        else:
            super()._record_fragment(fragment)

    def _finish_unit(self, tag):
        kind, parts = self.units.pop()
        if tag != kind:
            raise _Fallback("unclosed_structure")
        text = _normalized("".join(part["decoded"] if part else " " for part in parts))
        if text:
            fragments = [part for part in parts if part]
            self.block_segments.append(
                (fragments[0]["start"], fragments[-1]["end"], text, fragments)
            )

    def _emit_block(self):
        if len(self.units) != 1:
            raise _Fallback("unclosed_structure")
        self._finish_unit(None)
        end = self.source.index(">", self.position()) + 1
        raw = self.source[self.block_start : end].replace("\r", " ").replace("\n", " ")
        # A blank source line would prematurely end a Markdown HTML block.
        self.output += "\n\n" + ("<blockquote>" if self.block_section == "quoted" else "")
        offset = len(self.output) - self.block_start
        self.output += raw + ("</blockquote>" if self.block_section == "quoted" else "") + "\n\n"
        for start, end, text, fragments in sorted(self.block_segments, key=lambda part: part[0]):
            self.segments.append(
                {
                    "text": text,
                    "section": self.block_section,
                    "md_start": offset + start,
                    "md_end": offset + end,
                    "md_text": self.output[offset + start : offset + end],
                    "source_fragments": fragments,
                }
            )

    def handle_endtag(self, tag):
        if not self.structures:
            return super().handle_endtag(tag)
        if tag in self._TEXT_UNITS:
            self._finish_unit(tag)
        elif tag in self._BLOCK:
            self.units[-1][1].append(None)
        if tag in self.block_tags:
            if self.structures.pop() != tag:
                raise _Fallback("unclosed_structure")
            if not self.structures:
                self._emit_block()

    def close(self):
        super().close()
        if self.structures:
            raise _Fallback("unclosed_structure")


def _read_html(converter, source):
    converter.feed(source)
    converter.close()
    converter.flush()
    if converter.table is not None or converter.cell is not None or converter.quote:
        raise _Fallback("unclosed_structure")
    return converter


def convert_body(raw, mime, *, version=VERSION):
    if version not in _SUPPORTED_VERSIONS:
        raise ValueError("Unsupported Markdown conversion version")
    source = raw.decode("utf-8-sig")
    try:
        if mime == "text/plain":
            converter = _Converter(source, version)
            pos = 0
            for line in source.splitlines(keepends=True):
                text = line.rstrip("\r\n")
                section = "quoted" if re.match(r"^\s*>\s", text) else "current"
                converter.emit(text, [converter.fragment(pos, pos + len(text), text)], section)
                pos += len(line)
        elif mime == "text/html":
            try:
                converter = _read_html(_Html(source, version), source)
            except _Fallback as error:
                if version != VERSION or str(error) not in _RETAIN_BLOCK_FOR:
                    raise
                converter = _read_html(
                    _HtmlWithBlocks(
                        source,
                        version,
                        retain_paragraphs=str(error) == "unsupported_list_structure",
                    ),
                    source,
                )
        else:
            raise _Fallback("unsupported_body_format")
        converter.resolve_headers()
        markdown = converter.output.encode("utf-8")
        mapping = {
            "version": version,
            "source_mime": mime,
            "source_sha256": _sha(raw),
            "markdown_sha256": _sha(markdown),
            "coordinate_system": "unicode_codepoint_half_open",
            "segments": converter.segments,
        }
        return markdown, mapping, None
    except _Fallback as error:
        return None, None, str(error)


def validate_mapping(source_raw, markdown_raw, mapping):
    if (
        mapping["version"] not in _SUPPORTED_VERSIONS
        or mapping["source_sha256"] != _sha(source_raw)
        or mapping["markdown_sha256"] != _sha(markdown_raw)
    ):
        raise ValueError("Markdown mapping content hash mismatch")
    source, markdown = source_raw.decode("utf-8-sig"), markdown_raw.decode("utf-8")
    for item in mapping["segments"]:
        if (
            item["section"] not in {"current", "quoted", "unknown"}
            or markdown[item["md_start"] : item["md_end"]] != item["md_text"]
        ):
            raise ValueError("Markdown segment mismatch")
        for fragment in item["source_fragments"]:
            if source[fragment["start"] : fragment["end"]] != fragment["raw"]:
                raise ValueError("Markdown source fragment mismatch")
    regenerated, expected, reason = convert_body(
        source_raw, mapping["source_mime"], version=mapping["version"]
    )
    if reason is not None or regenerated != markdown_raw or expected != mapping:
        raise ValueError("Markdown mapping does not reproduce from source")


def _normalized(text):
    return re.sub(r"\s+", " ", text).strip()


def _matches(text, source):
    return text == source or (len(text) >= 3 and text in source)


def mapped_section(text, mapping):
    text = _normalized(text)
    if not text:
        return "unknown"
    sections = {
        item["section"] for item in mapping["segments"] if _matches(text, _normalized(item["text"]))
    }
    return sections.pop() if len(sections) == 1 else "unknown"


def mapped_sections(texts, mapping):
    sections = [mapped_section(text, mapping) for text in texts]
    if mapping["version"] == _LEGACY_VERSION:
        return sections

    occurrences = {}
    normalized_texts = [_normalized(text) for text in texts]
    for index, text in enumerate(normalized_texts):
        if sections[index] == "unknown" and text:
            occurrences.setdefault(text, []).append(index)
    source = [(_normalized(item["text"]), item["section"]) for item in mapping["segments"]]
    for key, indices in occurrences.items():
        matches = [(text, role) for text, role in source if _matches(key, text)]
        if {role for _, role in matches} != {"current", "quoted"}:
            continue
        # For v4 an exact line can also be part of a longer quoted paragraph.
        # Resolve it only if every matching segment survives intact and in order.
        if mapping["version"] == VERSION:
            read_matches = [
                (i, text) for i, text in enumerate(normalized_texts) if _matches(key, text)
            ]
        else:
            read_matches = [(i, key) for i in indices]
        if [text for _, text in read_matches] == [text for text, _ in matches]:
            for (index, _), (_, role) in zip(read_matches, matches, strict=True):
                sections[index] = role
    return sections
