"""Serialize bounded attachment READ data through Docling with exact source anchors."""

import hashlib
import html
import json
from collections import Counter


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def workbook_read(detail):
    parts = []
    for sheet in detail["sheets"]:
        columns = {col: i for i, col in enumerate(sheet["columns"])}
        merges = {(m["row"], m["col"]): m for m in sheet.get("merges", [])}
        occupied = set()
        cells = []
        for row in sheet["rows"]:
            r = row["number"]
            for column, item in sorted(row["cells"].items(), key=lambda c: columns[c[0]]):
                c = columns[column] + 1
                if item["clipped"] or not item["value"].strip() or (r, c) in occupied:
                    continue
                span = merges.get((r, c), {})
                end_r, end_c = span.get("row_end", r), span.get("col_end", c)
                occupied.update((y, x) for y in range(r, end_r + 1) for x in range(c, end_c + 1))
                cells.append(
                    {
                        "text": item["value"],
                        "location": {"sheet": sheet["name"], "cell": f"{column}{r}"},
                        "row": r - 1,
                        "row_end": end_r,
                        "col": c - 1,
                        "col_end": end_c,
                        "header": r == sheet.get("packing", {}).get("header_row"),
                    }
                )
        parts.append({"title": sheet["name"], "cells": cells})
    return parts


def text_read(detail):
    parts = [
        {
            "lines": [
                {"text": text, "location": {"line": number}}
                for number, text in enumerate(detail["lines"], 1)
                if text.strip()
            ],
        }
    ]
    for table in detail.get("tables", []):
        parts.append(
            {
                "title": f"表格 {table['number']}",
                "cells": [
                    {
                        **cell,
                        "location": {
                            "pointer": cell["pointer"],
                            "table": table["number"],
                            "row": cell["row"] + 1,
                            "column": cell["col"] + 1,
                        },
                    }
                    for cell in table["cells"]
                    if cell["text"].strip()
                ],
            }
        )
    return parts


def image_read(detail):
    return [
        {
            "lines": [
                {"text": line["text"], "location": {"line": number, "box": line["box"]}}
                for number, line in enumerate(detail["lines"], 1)
                if line["text"].strip()
            ]
        }
    ]


def serialize(read):
    """Worker side: use Docling's document model and the existing indexed exporter."""
    from docling_core.types.doc import DocItemLabel, DoclingDocument, TableCell, TableData
    from p02_markdown_export import export_document

    document = DoclingDocument(name="Attachment")
    pointers = {}
    headings = set()
    for part in read["parts"]:
        if part.get("title"):
            headings.add(document.add_heading(part["title"]).self_ref[1:])
        for line in part.get("lines", []):
            item = document.add_text(label=DocItemLabel.TEXT, text=line["text"])
            pointers[item.self_ref[1:] + "/text"] = line
        cells = part.get("cells", [])
        if not cells:
            continue
        table = document.add_table(
            data=TableData(
                num_rows=max(c["row_end"] for c in cells),
                num_cols=max(c["col_end"] for c in cells),
                table_cells=[
                    TableCell(
                        text=c["text"],
                        start_row_offset_idx=c["row"],
                        end_row_offset_idx=c["row_end"],
                        start_col_offset_idx=c["col"],
                        end_col_offset_idx=c["col_end"],
                        row_span=c["row_end"] - c["row"],
                        col_span=c["col_end"] - c["col"],
                        column_header=c.get("header", False),
                    )
                    for c in cells
                ],
            )
        )
        for i, cell in enumerate(cells):
            pointers[f"{table.self_ref[1:]}/data/table_cells/{i}/text"] = cell
    markdown, index = export_document(
        document,
        read_sha256=digest(encode(read)),
        material_id=read["material_id"],
        execution_id=read["material_id"],
        source_kind="attachment",
    )
    for fragment in index["fragments"]:
        item = pointers[fragment["read_pointer"]]
        fragment["location"] = item["location"]
    # Section headings label the source scope; they are not candidate evidence.
    index["limitations"] = [
        limit
        for limit in index["limitations"]
        if not limit.get("read_items") or not set(limit["read_items"]).issubset(headings)
    ]
    return {"markdown": markdown, "index": index}


def location_key(location):
    for keys in (("sheet", "cell"), ("pointer",), ("line",)):
        if all(location.get(key) is not None for key in keys):
            return tuple(location[key] for key in keys)
    return ()


def attachment_view(record, detail, route, root, export, *, parts, reader, profile):
    mid = record["material_id"]
    read = {
        "material_id": mid,
        "input_sha256": record["content"]["sha256"],
        "reader": reader,
        "parts": parts,
    }
    read_bytes = encode(read)
    anchors = {
        location_key(item["location"]): item
        for part in parts
        for kind in ("lines", "cells")
        for item in part.get(kind, [])
    }
    sources = {key: item["text"] for key, item in anchors.items()}
    result = export(read)
    md, index = result["markdown"], result["index"]
    if (index["material_id"], index["read_sha256"]) != (mid, digest(read_bytes)):
        raise ValueError("Attachment Markdown not bound to the material READ")
    mapped = {}
    for fragment in index["fragments"]:
        key = location_key(fragment["location"])
        start, end = fragment["markdown_start"], fragment["markdown_end"]
        if not 0 <= start < end <= len(md):
            raise ValueError("Attachment Markdown source offsets out of range")
        text = md[start:end]
        if fragment.get("markdown_encoding") == "html":
            text = html.unescape(text.replace("<br>", "\n"))
        if (
            not key
            or key not in anchors
            or text != sources[key]
            or fragment["location"] != anchors[key]["location"]
            or key in mapped
        ):
            raise ValueError("Attachment Markdown source mismatch")
        mapped[key] = fragment
    limitations = Counter(item["reason"] for item in index["limitations"])
    if detail.get("clipped"):
        limitations["attachment_range_limited"] += 1
    if not sources:
        limitations["attachment_no_read_text"] += 1
    if sources.keys() - mapped.keys():
        limitations["attachment_source_not_indexed"] = len(sources.keys() - mapped.keys())
    fields = []
    for original in detail["fields"]:
        field = {**original, "sources": []}
        for source in original["sources"]:
            keys = [location_key(source)]
            if source.get("end_cell"):
                keys.append((source["sheet"], source["end_cell"]))
            if not all(key in sources for key in keys):
                continue
            text = sources[keys[0]]
            if len(keys) == 2:
                text = text.rstrip(":： ") + ": " + sources[keys[1]]
            if source["excerpt"] not in text:
                continue
            field["sources"].append(
                {
                    **source,
                    "markdown_evidence": [
                        mapped[key]["evidence_id"] for key in keys if key in mapped
                    ],
                }
            )
        if original["status"] == "known" and len(field["sources"]) != len(original["sources"]):
            field.update(
                status="uncertain", value=None, unit=None, reason="attachment_source_unverified"
            )
        field["via"] = "rules" if field["sources"] else None
        fields.append(field)
    folder = root / ("attachment-" + mid)
    folder.mkdir()
    md_bytes = md.encode()
    state = "partial_success" if limitations else "success"
    index.update(
        state=state,
        markdown_sha256=digest(md_bytes),
        input_sha256=read["input_sha256"],
        limitation_counts=dict(limitations),
    )
    (folder / "read.json").write_bytes(read_bytes)
    (folder / "content.md").write_bytes(md_bytes)
    (folder / "index.json").write_bytes(encode(index))
    (folder / "fields.json").write_bytes(encode(fields))
    return {
        "route": route,
        "preprocessing": {
            "material_id": mid,
            "filename": record["filename"],
            "kind": record["kind"],
            "format": record["format"],
            "state": record["state"],
            "sha256": read["input_sha256"],
            "mime_path": record.get("metadata", {}).get("mime_path"),
        },
        "read": {
            "state": "partial_success" if detail.get("clipped") or not sources else "success",
            "reader": reader,
            "sha256": digest(read_bytes),
        },
        "markdown": {
            "state": state,
            "text": md[:180_000],
            "truncated": len(md) > 180_000,
            "fragment_count": len(mapped),
            "limitations": dict(limitations),
            "sha256": digest(md_bytes),
        },
        "agent": {"enabled": False, "adapter_requested": False, "events": []},
        "structured": {
            "state": "success" if all(f["status"] == "known" for f in fields) else "partial",
            "profile": profile,
            "fields": fields,
        },
    }
