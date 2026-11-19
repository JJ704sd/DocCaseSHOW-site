"""Serialize Docling READ documents and bind Markdown fragments to READ pointers."""

import hashlib
import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path

EXPORT_VERSION = "docling-markdown-evidence-2"


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = []
        self.hidden_depth = 0
        self.hidden_nonempty = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        style = attributes.get("style") or ""
        hidden = (
            self.hidden_depth
            or "hidden" in attributes
            or re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", style, re.I)
        )
        if hidden:
            if any(attributes.get(key) for key in ("src", "srcset", "href", "alt")):
                self.hidden_nonempty = True
            if tag not in {"img", "br", "input", "meta", "link", "source", "hr"}:
                self.hidden_depth += 1
            return
        if tag in {"head", "script", "style"}:
            self.skip.append(tag)
        elif not self.skip and tag == "blockquote":
            self.parts.append(" QUOTED-SECTION-BOUNDARY ")
        elif not self.skip and tag in {"p", "div", "br", "tr", "td", "th", "li"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if self.hidden_depth:
            self.hidden_depth -= 1
            return
        if self.skip and self.skip[-1] == tag:
            self.skip.pop()
        elif not self.skip and tag in {"p", "div", "tr", "td", "th", "li"}:
            self.parts.append(" ")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in {"img", "br", "input", "meta", "link", "source", "hr"}:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self.hidden_depth:
            self.hidden_nonempty |= bool(data.strip())
        elif not self.skip:
            self.parts.append(data)


def _normal(text):
    return " ".join(text.split())


_QUOTED_HEADER = re.compile(
    r"\b(?:from|de|发件人)\s*[:：].{0,300}?"
    r"(?:sent|date|enviado\s+el|enviada\s+em|fecha|发送时间)\s*[:：].{0,250}?"
    r"(?:to|para|收件人)\s*[:：]|QUOTED-SECTION-BOUNDARY|\bon\s+.{0,120}?\s+wrote\s*:|"
    r"[-_]{3,}\s*(?:original message|forwarded message)[-_ ]*|"
    r"[-_]{3,}\s*original\s*[-_]{3,}",
    re.I,
)


def _source_roles(parts, source_text, source_kind):
    items, seen = [], set()
    for part in parts:
        for item in part.get_unique_doc_items():
            if item.self_ref.startswith("#/texts/") and item.self_ref not in seen:
                seen.add(item.self_ref)
                items.append(item)
    if source_kind != "current_body":
        return {
            item.self_ref: "attachment" if source_kind == "attachment" else "unknown"
            for item in items
        }, False
    if not source_text:
        return {}, False
    hidden_nonempty = False
    if re.search(r"<\s*(?:html|body|div|p|span|table)\b", source_text, re.I):
        parser = _VisibleText()
        parser.feed(source_text)
        source = _normal("".join(parser.parts))
        hidden_nonempty = parser.hidden_nonempty
    else:
        source = _normal(source_text)
    boundaries = [match.start() for match in _QUOTED_HEADER.finditer(source)]
    source_boundary = min(boundaries) if boundaries else None
    read_boundary = None
    for index, item in enumerate(items):
        text = _normal(item.text)
        if _QUOTED_HEADER.search(text):
            read_boundary = index
            break
        if re.fullmatch(r"(?:from|de|发件人)\s*[:：]", text, re.I):
            group = item.parent.cref if item.parent else None
            nearby = [
                other.text
                for other in items[index : index + 13]
                if not group or (other.parent and other.parent.cref == group)
            ]
            if _QUOTED_HEADER.search(_normal(" ".join(nearby))):
                read_boundary = index
                break
    by_text = {}
    for item in items:
        if item.text.strip():
            by_text.setdefault(_normal(item.text), []).append(item.self_ref)
    locations = {}
    for text, refs in by_text.items():
        positions = [match.start() for match in re.finditer(re.escape(text), source)]
        if len(positions) == len(refs):
            locations.update(zip(refs, positions, strict=True))
    out_of_order = set()
    ordered = [
        (item.self_ref, locations[item.self_ref]) for item in items if item.self_ref in locations
    ]
    for left, right in zip(ordered, ordered[1:], strict=False):
        if left[1] > right[1]:
            out_of_order.update((left[0], right[0]))
    roles = {}
    for index, item in enumerate(items):
        offset = locations.get(item.self_ref)
        if (
            offset is None
            or item.self_ref in out_of_order
            or ((source_boundary is None) != (read_boundary is None))
        ):
            roles[item.self_ref] = "unknown"
            continue
        source_role = (
            "quoted" if source_boundary is not None and offset >= source_boundary else "current"
        )
        read_role = "quoted" if read_boundary is not None and index >= read_boundary else "current"
        roles[item.self_ref] = source_role if source_role == read_role else "unknown"
    return roles, hidden_nonempty


def _table_markdown(item, prefix, source_kind, worksheet=None):
    data = item.data.model_dump(mode="json")
    cells = data["table_cells"]
    rows, cols = data["num_rows"], data["num_cols"]
    anchors = {
        (c["start_row_offset_idx"], c["start_col_offset_idx"]): (i, c) for i, c in enumerate(cells)
    }
    spans = any(
        c["end_row_offset_idx"] - c["start_row_offset_idx"] > 1
        or c["end_col_offset_idx"] - c["start_col_offset_idx"] > 1
        for c in cells
    )
    nested = any(c.get("ref") for c in cells)
    header = bool(
        rows
        and cols
        and all(anchors.get((0, col), (None, {}))[1].get("column_header") for col in range(cols))
    )
    simple = header and not spans and not nested
    table_pointer = prefix + item.self_ref[1:]
    pages = {p.page_no for p in item.prov if type(p.page_no) is int and p.page_no > 0}
    page = next(iter(pages)) if len(pages) == 1 else None
    chunks, fragments, limitations, occupied = [], [], [], set()
    pos = 0

    def add(value):
        nonlocal pos
        chunks.append(value)
        pos += len(value)

    if not simple:
        add("<table>\n")
    for row in range(rows):
        add("|" if simple else "<tr>")
        for col in range(cols):
            if (row, col) in occupied:
                continue
            found = anchors.get((row, col))
            if not found:
                add("  |" if simple else "<td></td>")
                continue
            cell_index, cell = found
            end_row, end_col = cell["end_row_offset_idx"], cell["end_col_offset_idx"]
            occupied.update(
                (r, c)
                for r in range(row, end_row)
                for c in range(col, end_col)
                if (r, c) != (row, col)
            )
            if simple:
                add(" ")
            else:
                tag = "th" if cell.get("column_header") or cell.get("row_header") else "td"
                attrs = f' rowspan="{end_row - row}"' if end_row - row > 1 else ""
                attrs += f' colspan="{end_col - col}"' if end_col - col > 1 else ""
                add(f"<{tag}{attrs}>")
            raw = cell.get("text") or ""
            if cell.get("ref"):
                add("[nested structure in READ]")
                limitations.append(
                    {
                        "reason": "nested_table_structure_requires_read",
                        "read_pointer": prefix
                        + item.self_ref[1:]
                        + f"/data/table_cells/{cell_index}",
                    }
                )
            elif raw:
                rendered = (
                    html.escape(raw, quote=False).replace("|", "&#124;").replace("\n", "<br>")
                )
                start = pos
                add(rendered)
                fragments.append(
                    {
                        "read_pointer": table_pointer + f"/data/table_cells/{cell_index}/text",
                        "read_start": 0,
                        "read_end": len(raw),
                        "markdown_start": start,
                        "markdown_end": pos,
                        "markdown_encoding": "html",
                        "source_role": "attachment" if source_kind == "attachment" else "unknown",
                        "provenance": [p.model_dump(mode="json") for p in item.prov],
                        "table_location": {
                            "table_pointer": table_pointer,
                            "page": page,
                            "row": row + 1,
                            "row_end": end_row,
                            "column": col + 1,
                            "column_end": end_col,
                            **({"worksheet": worksheet} if worksheet else {}),
                        },
                        "cell": {
                            key: cell.get(key)
                            for key in (
                                "start_row_offset_idx",
                                "end_row_offset_idx",
                                "start_col_offset_idx",
                                "end_col_offset_idx",
                                "column_header",
                                "row_header",
                            )
                        },
                    }
                )
            add(" |" if simple else f"</{tag}>")
        add("\n" if simple else "</tr>\n")
        if simple and row == 0:
            add("|" + " --- |" * cols + "\n")
    if not simple:
        add("</table>")
    return "".join(chunks).rstrip("\n"), fragments, limitations


def export_document(
    document,
    *,
    read_sha256,
    material_id,
    execution_id,
    source_kind,
    source_text=None,
    pointer_prefix="",
    pages=None,
    worksheet=None,
):
    """Serialize one Docling document; offsets are constructed while emitting parts."""
    from docling_core.transforms.serializer.markdown import MarkdownDocSerializer, MarkdownParams

    serializer = MarkdownDocSerializer(
        doc=document, params=MarkdownParams(escape_underscores=False, pages=pages)
    )
    parts = serializer.get_parts()
    roles, hidden_nonempty = _source_roles(parts, source_text, source_kind)
    chunks, fragments, limitations = [], [], []
    emitted_refs = {
        item.self_ref for part in parts if part.text for item in part.get_unique_doc_items()
    }
    for item in document.texts:
        belongs = pages is None or any(p.page_no in pages for p in item.prov)
        if belongs and item.text.strip() and item.self_ref not in emitted_refs:
            limitations.append(
                {
                    "reason": "read_content_not_exported",
                    "read_pointer": pointer_prefix + item.self_ref[1:] + "/text",
                }
            )
    if hidden_nonempty:
        limitations.append({"reason": "hidden_source_content_not_confirmed_in_read"})
    position = 0
    rendered_tables = set()

    def append_part(text, local_fragments=()):
        nonlocal position
        if chunks:
            chunks.append("\n\n")
            position += 2
        start = position
        chunks.append(text)
        position += len(text)
        for fragment in local_fragments:
            fragment["evidence_id"] = f"E{len(fragments) + 1}"
            fragment["markdown_start"] += start
            fragment["markdown_end"] += start
            fragments.append(fragment)
        return start

    for part in parts:
        if not part.text:
            continue
        items = part.get_unique_doc_items()
        all_tables = items and all(item.self_ref.startswith("#/tables/") for item in items)
        for table in items if all_tables else [None]:
            text = part.text
            local_fragments, local_limits = [], []
            if table is not None:
                if table.self_ref in rendered_tables:
                    continue
                text, local_fragments, local_limits = _table_markdown(
                    table, pointer_prefix, source_kind, worksheet
                )
                rendered_tables.add(table.self_ref)
            start = append_part(text, local_fragments)
            if table is not None:
                limitations.extend(local_limits)
            elif (
                len(items) == 1
                and items[0].self_ref.startswith("#/texts/")
                and items[0].text == text
            ):
                item = items[0]
                role = roles.get(item.self_ref, "unknown")
                fragments.append(
                    {
                        "evidence_id": f"E{len(fragments) + 1}",
                        "read_pointer": pointer_prefix + item.self_ref[1:] + "/text",
                        "read_start": 0,
                        "read_end": len(item.text),
                        "markdown_start": start,
                        "markdown_end": position,
                        "source_role": role,
                        "provenance": [p.model_dump(mode="json") for p in item.prov],
                    }
                )
                if source_kind == "current_body" and role == "unknown":
                    limitations.append(
                        {
                            "reason": "source_scope_unconfirmed",
                            "read_pointer": pointer_prefix + item.self_ref[1:] + "/text",
                        }
                    )
            else:
                reason = (
                    "image_placeholder_not_text"
                    if any(item.self_ref.startswith("#/pictures/") for item in items)
                    else "markdown_read_mapping_unconfirmed"
                )
                limitations.append(
                    {
                        "reason": reason,
                        "markdown_start": start,
                        "markdown_end": position,
                        "read_items": [pointer_prefix + item.self_ref[1:] for item in items],
                    }
                )
    for table in document.tables:
        belongs = pages is None or any(p.page_no in pages for p in table.prov)
        if (
            not belongs
            or table.self_ref in rendered_tables
            or not any(c.text.strip() for c in table.data.table_cells)
        ):
            continue
        if source_kind == "attachment":
            # The generic serializer can hide a real table in a picture/group
            # part. Render directly from the same verified READ, not a new read.
            text, local_fragments, local_limits = _table_markdown(
                table, pointer_prefix, source_kind, worksheet
            )
            append_part(text, local_fragments)
            limitations.extend(local_limits)
        elif table.self_ref not in emitted_refs:
            limitations.append(
                {
                    "reason": "read_table_not_exported",
                    "read_pointer": pointer_prefix + table.self_ref[1:],
                }
            )
    markdown = "".join(chunks)
    index = {
        "version": EXPORT_VERSION,
        "read_sha256": read_sha256,
        "material_id": material_id,
        "execution_id": execution_id,
        "source_kind": source_kind,
        "state": "partial_success" if limitations else "success",
        "fragments": fragments,
        "limitations": limitations,
    }
    return markdown, index


def export_saved_read(request):
    """Export one verified Docling JSON artifact, selecting one PDF part per page."""
    from docling_core.types.doc import DoclingDocument
    from pydantic import ValidationError

    read_path = Path(request["read_path"])
    raw = read_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != request["read_sha256"]:
        raise ValueError("READ artifact SHA-256 mismatch for Markdown export")
    saved = json.loads(raw)
    source_text = None
    if request.get("source_path"):
        original = Path(request["source_path"]).read_bytes()
        if hashlib.sha256(original).hexdigest() != request["source_sha256"]:
            raise ValueError("Source representation SHA-256 mismatch for Markdown export")
        source_text = original.decode("utf-8-sig")
    specs, missing_pages, supplemental = [], [], []
    if saved.get("kind") == "docling_pdf_read_bundle":
        priority = {"native": 2, "ocr": 1, "structure": 0}
        choices, available = {}, {}
        for index, part in enumerate(saved["parts"]):
            part_bytes = Path(part["ref"]).read_bytes()
            if (
                hashlib.sha256(part_bytes).hexdigest() != part["sha256"]
                or json.loads(part_bytes) != part["document"]
            ):
                raise ValueError("PDF READ part SHA-256 mismatch for Markdown export")
            declared = part["document"].get("pages", {})
            page_numbers = [int(page) for page in declared]
            if not page_numbers and part.get("pages"):
                page_numbers = list(range(part["pages"][0], part["pages"][-1] + 1))
            for page in page_numbers:
                has_text = any(
                    str(item.get("text", "")).strip()
                    and any(p.get("page_no") == page for p in item.get("prov", []))
                    for item in part["document"].get("texts", [])
                )
                candidate = (int(has_text), priority.get(part["kind"], -1), index)
                available.setdefault(page, []).append(index)
                if candidate > choices.get(page, (-1, -2, -1)):
                    choices[page] = candidate
        missing_pages = [page for page in saved.get("expected_pages", []) if page not in choices]
        specs = [
            (saved["parts"][index]["document"], f"/parts/{index}/document", {page})
            for page, (_, _, index) in sorted(choices.items())
        ]
        for page, (_, _, selected) in sorted(choices.items()):
            for index in available[page]:
                part = saved["parts"][index]
                if index == selected or part["kind"] != "structure":
                    continue
                for table_index, table in enumerate(part["document"].get("tables", [])):
                    table_pages = {p.get("page_no") for p in table.get("prov", [])}
                    if page not in table_pages and not (
                        not table_pages and part.get("pages") == [page, page]
                    ):
                        continue
                    pointer = f"/parts/{index}/document/tables/{table_index}"
                    supplemental.append(
                        {
                            "page": page,
                            "read_pointer": pointer,
                            "cell_pointers": [
                                pointer + f"/data/table_cells/{n}/text"
                                for n, cell in enumerate(
                                    table.get("data", {}).get("table_cells", [])
                                )
                                if isinstance(cell.get("text"), str)
                            ],
                        }
                    )
    else:
        specs = [(saved, "", None)]
    chunks, fragments, limitations = [], [], []
    if request.get("read_state") == "partial_success":
        limitations.append({"reason": "source_read_partial"})
    offset = 0
    for doc_data, prefix, pages in specs:
        try:
            document = DoclingDocument.model_validate(doc_data)
        except ValidationError:
            return {"state": "failure", "reason": "unsupported_docling_read_document"}
        text, result = export_document(
            document,
            read_sha256=request["read_sha256"],
            material_id=request["material_id"],
            execution_id=request["execution_id"],
            source_kind=request["source_kind"],
            source_text=source_text,
            pointer_prefix=prefix,
            pages=pages,
            worksheet=request.get("worksheet"),
        )
        if not text:
            continue
        if chunks:
            chunks.append("\n\n")
            offset += 2
        for fragment in result["fragments"]:
            fragment["evidence_id"] = f"E{len(fragments) + 1}"
            fragment["markdown_start"] += offset
            fragment["markdown_end"] += offset
            fragments.append(fragment)
        for limitation in result["limitations"]:
            if "markdown_start" in limitation:
                limitation["markdown_start"] += offset
                limitation["markdown_end"] += offset
            limitations.append(limitation)
        chunks.append(text)
        offset += len(text)
    for structure in supplemental:
        if chunks:
            chunks.append("\n\n")
            offset += 2
        text = f"[Supplemental READ table structure: {structure['read_pointer']}]"
        structure["markdown_start"], structure["markdown_end"] = offset, offset + len(text)
        chunks.append(text)
        offset += len(text)
    if supplemental:
        limitations.append(
            {"reason": "supplemental_pdf_tables_referenced_not_inlined", "count": len(supplemental)}
        )
    missing_pages = sorted(set(missing_pages) | set(request.get("unread_pages", [])))
    if missing_pages:
        limitations.append({"reason": "source_pages_unread", "pages": missing_pages})
    text = "".join(chunks)
    if not text:
        return {"state": "failure", "reason": "no_readable_docling_content"}
    output = Path(request["output_dir"])
    output.mkdir(parents=True, exist_ok=False)
    markdown_path, index_path = output / "content.md", output / "index.json"
    markdown_bytes = text.encode("utf-8")
    markdown_path.write_bytes(markdown_bytes)
    index = {
        "version": EXPORT_VERSION,
        "read_sha256": request["read_sha256"],
        "material_id": request["material_id"],
        "execution_id": request["execution_id"],
        "source_kind": request["source_kind"],
        "source_sha256": request.get("source_sha256"),
        "markdown_sha256": hashlib.sha256(markdown_bytes).hexdigest(),
        "state": "partial_success" if limitations else "success",
        "fragments": fragments,
        "structural_refs": supplemental,
        "limitations": limitations,
    }
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "state": index["state"],
        "markdown_ref": str(markdown_path.resolve()),
        "markdown_sha256": index["markdown_sha256"],
        "index_ref": str(index_path.resolve()),
        "index_sha256": hashlib.sha256(index_path.read_bytes()).hexdigest(),
        "read_sha256": request["read_sha256"],
        "fragment_count": len(fragments),
        "limitations": limitations,
    }
