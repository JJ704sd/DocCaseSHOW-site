"""Local document review workbench; serves only loopback and does not fetch URLs."""

import argparse
import base64
import functools
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from email import policy
from email.parser import BytesHeaderParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

import p02_local_read as p02
from deepseek_extraction import MODEL, load_deepseek_agent
from local_attachment_markdown import attachment_view, image_read, text_read, workbook_read
from local_group_compare import COMPARISON_FIELDS, compare_material_totals
from local_parse_server import FIELD_CONTRACT, _indexed_profile, _response, current_body_profile
from local_web_agent_context import (
    approved_model_fragment,
    context_gaps,
    natural_gap_material_request,
    restrict_material_response,
    workbench_material_evidence,
)
from local_web_cases import CASES as DEMO_CASES
from local_web_cases import case_files
from local_web_evaluation import saved_evaluation
from local_web_group import build_group_review, compare_group_routes
from local_web_group_context import missing_fields, run_context
from local_web_measurements import body_measurement
from local_web_pipeline import ROUTE_ALIASES, ROUTES, pipeline_view, saved_markdown
from local_web_preview import render_original_html

from doccase_preprocessing.body_objects import body_objects
from doccase_preprocessing.cargo_table import agent_evidence_spans, packing_rows
from doccase_preprocessing.extraction_agent import AgentServiceError
from doccase_preprocessing.html_scope import BodyText, current_inline_cids, quote_boundary
from doccase_preprocessing.inquiry_runtime import (
    build_inquiry_result,
    cargo_line_identity,
    inquiry_identity,
)
from doccase_preprocessing.local_handoff import run_local_file_handoff, run_local_handoff
from doccase_preprocessing.main_channel import (
    BODY_WEIGHT_FIELDS,
    INQUIRY_FIELDS,
    SPLIT_INQUIRY_FIELDS,
    WEIGHT_INQUIRY_PROFILE,
    _blocks,
    _body_total_fields,
    _facts,
    _pdf_table_field,
    read_result,
    read_validated_result,
    run_main_channel,
    scoped_material_evidence,
)
from doccase_preprocessing.models import Limits
from doccase_preprocessing.object_agent import object_packet, validate_object_response

STATIC = Path(__file__).resolve().parents[1] / "local_web"
WORKER = Path(__file__).with_name("local_web_worker.py")
LIMITS = Limits()
TEXT_LIMIT = 180_000
LINE_LIMIT = 3000
MAX_ACTIVE = 12
MAX_GROUP = 12
EARLY_EMAIL_HEADER_BYTES = 64 * 1024
MAX_GROUP_AGENT_CALLS = 12
PREVIEW_CSP = (
    "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
    "base-uri 'none'; frame-ancestors 'self'; form-action 'none'"
)
MIMES = {
    ".txt": "text/plain",
    ".html": "text/html",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
FORMATS = {"text", "html", "pdf", "png", "jpeg", "xlsx"}
PARSE_SCOPES = {"all", "body", "attachments"}
CARGO = (
    "package_count",
    "ctns",
    "line_net_weight",
    "line_gross_weight",
    "line_volume",
    "unit_net_weight",
    "unit_gross_weight",
)
BODY = ("package_count", "line_gross_weight", "line_volume", "ctns", *INQUIRY_FIELDS)
GROUP_BODY = tuple(
    dict.fromkeys(
        (
            "package_count",
            "line_volume",
            "ctns",
            "package_type",
            *BODY_WEIGHT_FIELDS,
            *(name for name in SPLIT_INQUIRY_FIELDS if name != "chargeable_weight"),
        )
    )
)
TABLE_TOTAL_FIELDS = ("ctns", "line_net_weight", "line_gross_weight", "line_volume")
SHIPMENT_ROUTE = re.compile(
    r"\b(?i:shipment\s+from)\s+[A-Z]{3}\s+(?i:to)\s+(?P<destination>[A-Z]{3})\b"
)
ROUTE_ALTERNATIVE = re.compile(r"^\s*(?:[/&-]|(?:,|(?i:or|and)\b)\s*[A-Z]{3}\b)")
BODY_OBSERVATIONS = (
    ("pieces", re.compile(r"Pcs\.?\s*[:：]\s*\d+", re.I)),
    ("weight", re.compile(r"Weight\s*[:：]\s*\d+(?:\.\d+)?\s*(?:kgs?|g|lbs?)", re.I)),
)
TOTAL_LABEL = re.compile(r"(?:grand\s+)?total\s*[:：]?|合计(?:\s*total)?", re.I)


def inline_signature_fields(read, sender, material_id, read_sha256):
    """Extract only sender-bound signature-card OCR facts with original locations."""
    sender = str(sender or "").strip().casefold()
    if (
        read.get("reader") != "RapidOCR"
        or len(read.get("parts", [])) != 1
        or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", sender)
    ):
        return []
    lines = [
        (i, item) for part in read.get("parts", []) for i, item in enumerate(part.get("lines", []))
    ]
    emails = [(i, item) for i, item in lines if item.get("text", "").strip().casefold() == sender]
    if len(emails) != 1:
        return []

    def box(item):
        points = item.get("location", {}).get("box", [])
        if len(points) != 4 or any(
            len(p) != 2 or not all(isinstance(v, (int, float)) for v in p) for p in points
        ):
            return None
        return (
            min(p[0] for p in points),
            min(p[1] for p in points),
            max(p[0] for p in points),
            max(p[1] for p in points),
        )

    email_box = box(emails[0][1])
    if not email_box:
        return []

    def source(index, item):
        return {
            "kind": "inline_signature",
            "material_id": material_id,
            "read_material_id": read["material_id"],
            "read_sha256": read_sha256,
            "pointer": f"/parts/0/lines/{index}/text",
            "line": item["location"]["line"],
            "box": item["location"]["box"],
            "excerpt": item["text"],
        }

    def field(name, value, items):
        return {
            "name": name,
            "value": value,
            "unit": None,
            "status": "known",
            "reason": None,
            "sources": [source(i, item) for i, item in items],
            "via": "rules",
        }

    output = [field("signature_email", emails[0][1]["text"].strip(), emails)]
    first_name = re.split(r"[._+\-]", sender.split("@", 1)[0])[0]
    names = [
        (i, item, b)
        for i, item in lines
        if (b := box(item))
        and b[1] < email_box[1]
        and abs(b[0] - email_box[0]) <= (email_box[2] - email_box[0]) / 2
        and re.fullmatch(
            r"[A-ZÀ-ÖØ-Þ][\w'’.-]+(?:\s+[A-ZÀ-ÖØ-Þ][\w'’.-]+){1,3}", item["text"].strip()
        )
        and item["text"].split()[0].casefold() == first_name
    ]
    if len(names) == 1:
        i, item, _ = names[0]
        output.append(field("contact_name", item["text"].strip(), [(i, item)]))
    domain = sender.split("@", 1)[1].split(".", 1)[0]
    companies = [
        (i, item)
        for i, item in lines
        if re.sub(r"[^a-z0-9]", "", item.get("text", "").casefold()) == domain and len(domain) >= 5
    ]
    if len(companies) == 1:
        output.append(field("customer_company", companies[0][1]["text"].strip(), companies))
    phones = [
        (i, item)
        for i, item in lines
        if (b := box(item))
        and b[1] > email_box[1]
        and abs(b[0] - email_box[0]) <= (email_box[2] - email_box[0]) / 2
        and re.fullmatch(r"\+?[0-9() .-]{7,28}", item["text"].strip())
        and 7 <= sum(c.isdigit() for c in item["text"]) <= 16
    ]
    if phones:
        values = dict.fromkeys(item["text"].strip() for _, item in phones)
        output.append(field("contact_phone", "；".join(values), phones))
    return output


def merge_signature_fields(fields, additions):
    """Keep existing values and conflicts; attach source evidence for equal values."""
    result = deepcopy(fields)
    for candidate in additions:
        existing = next((f for f in result if f["name"] == candidate["name"]), None)
        if existing is None:
            result.append(candidate)
        elif existing.get("status") not in {"known", "conflict"}:
            existing.update(candidate)
        elif existing.get("status") == candidate["status"] == "known":
            if existing.get("value") == candidate["value"]:
                sources = list(existing.get("sources", []))
                sources.extend(source for source in candidate["sources"] if source not in sources)
                existing["sources"] = sources
            else:
                existing.update(
                    value=None,
                    status="conflict",
                    reason="signature_value_conflict",
                    sources=[*existing.get("sources", []), *candidate["sources"]],
                )
    return result


def split_body(text, html=False, *, scope_quotes=True):
    cells = {}
    if html:
        parser = BodyText(scope_quotes)
        parser.feed(text)
        parser.close()
        current = "".join(value if not quote else "\n" for quote, value, _ in parser.parts)
        history = "".join(value for quote, value, _ in parser.parts if quote)
        boundary = quote_boundary(current) if scope_quotes else len(current)
        offset = 0
        for quote, value, pointer in parser.parts:
            if not quote and pointer:
                cells.setdefault(pointer, []).append((offset, value))
            offset += 1 if quote else len(value)
        selected = []
        for pointer, parts in cells.items():
            cell = parser.cells[pointer]
            if not cell["table"]["valid"] or any(
                offset + len(value) > boundary for offset, value in parts
            ):
                continue
            value = "".join(part for _, part in parts)
            if value.strip() and not (
                scope_quotes and any(line.lstrip().startswith(">") for line in value.splitlines())
            ):
                selected.append(
                    {**cell, "table": cell["table"]["number"], "text": " ".join(value.split())}
                )
    else:
        current, history = text, ""
        boundary = quote_boundary(current) if scope_quotes else len(current)
        selected = []
    if boundary < len(current):
        history = current[boundary:].strip() + "\n" + history
        current = current[:boundary]
    active, quoted = [], []
    for line in current.splitlines():
        (quoted if scope_quotes and line.lstrip().startswith(">") else active).append(line.strip())
    return "\n".join(active).strip(), "\n".join([*quoted, history.strip()]).strip(), selected


def collect_fields(names, blocks, structured=None, *, source_kind=None):
    """Reuse current field rules for explicitly labelled text/cells, with UI locators."""
    fields = []
    for name in names:
        matches = {}
        for text, location in blocks:
            for match, value, unit in _facts(name, text, source_kind=source_kind):
                if name == "ctns":
                    unit = "carton"
                source = {"excerpt": match.group(), "start": match.start(), **location}
                evidence = matches.setdefault((value, unit), [])
                if source not in evidence:
                    evidence.append(source)
        for value, unit, sources in (structured or {}).get(name, []):
            evidence = matches.setdefault((value, "carton" if name == "ctns" else unit), [])
            evidence.extend(source for source in sources if source not in evidence)
        if len(matches) == 1:
            (value, unit), sources = next(iter(matches.items()))
            status, reason = "known", None
        elif matches:
            value, unit, status, reason = None, None, "conflict", "multiple_values"
            sources = [source for group in matches.values() for source in group]
        else:
            value, unit, sources, status, reason = None, None, [], "uncertain", "no_evidence"
        fields.append(
            {
                "name": name,
                "value": value,
                "unit": unit,
                "status": status,
                "sources": sources[:20],
                "reason": reason,
            }
        )
    return fields


def inquiry_body_inputs(lines):
    """Recognize shipment wording without losing the original evidence or its scope."""
    blocks, observations = [], []
    for number, line in enumerate(lines, 1):
        location = {"line": number}
        blocks.append((line, location))
        for route in SHIPMENT_ROUTE.finditer(line):
            if not ROUTE_ALTERNATIVE.match(line[route.end() :]):
                blocks.append(
                    (
                        "Destination: " + route["destination"],
                        {**location, "excerpt": route[0], "start": route.start()},
                    )
                )
        for kind, pattern in BODY_OBSERVATIONS:
            if pattern.fullmatch(line):
                observations.append({**location, "kind": kind, "excerpt": line})
    return blocks, observations


def table_block(text, pointer, row, row_end, col, col_end, location, *, header=False):
    return {
        "text": text,
        "pointer": pointer,
        "location": location,
        "cell": {
            "start_row_offset_idx": row,
            "end_row_offset_idx": row_end,
            "start_col_offset_idx": col,
            "end_col_offset_idx": col_end,
            "column_header": header,
        },
    }


def total_table_blocks(blocks):
    markers = [block for block in blocks if TOTAL_LABEL.fullmatch(block["text"])]
    if not markers:
        return []
    rows = {}
    for block in blocks:
        rows.setdefault(block["cell"]["start_row_offset_idx"], []).append(block)
    marker = markers[0]
    row = marker["cell"]["start_row_offset_idx"]
    # Some mail tables use a row with only TOTAL, followed by the final data row.
    # Treat that label as the summary marker only when the grid has no spanning cells.
    if (
        len(markers) == 1
        and len(rows[row]) == 1
        and max(rows) == row + 1
        and row >= min(rows) + 2
        and all(
            b["cell"]["end_row_offset_idx"] - b["cell"]["start_row_offset_idx"] == 1
            and b["cell"]["end_col_offset_idx"] - b["cell"]["start_col_offset_idx"] == 1
            for b in blocks
        )
    ):
        end = max(b["cell"]["end_col_offset_idx"] for b in blocks)
        projected = {
            **marker,
            "cell": {
                **marker["cell"],
                "start_row_offset_idx": row + 1,
                "end_row_offset_idx": row + 2,
                "start_col_offset_idx": end,
                "end_col_offset_idx": end + 1,
            },
        }
        return [block for block in blocks if block is not marker] + [projected]
    return blocks


def table_evidence(evidence):
    return [{"excerpt": block["text"], **block["location"]} for block in evidence]


def html_table_fields(cells, *, is_body):
    tables = {}
    for cell in cells:
        tables.setdefault(cell["table"], []).append(cell)
    display, structured = [], {}
    for number, group in tables.items():
        display.append({"number": number, "cells": group})
        blocks = [
            table_block(
                c["text"],
                c["pointer"],
                c["row"],
                c["row_end"],
                c["col"],
                c["col_end"],
                {
                    "pointer": c["pointer"],
                    "table": number,
                    "row": c["row"] + 1,
                    "column": c["col"] + 1,
                },
                header=c["header"],
            )
            for c in group
        ]
        totals = total_table_blocks(blocks) if is_body else blocks
        for name in BODY if is_body else CARGO:
            found = (
                _body_total_fields(blocks, {"field_name": name, "source_kind": "current_body"})
                if is_body
                else []
            )
            if name in TABLE_TOTAL_FIELDS:
                found.extend(_pdf_table_field(totals, name))
            if found:
                structured.setdefault(name, []).extend(
                    (value, unit, table_evidence(evidence)) for value, unit, evidence in found
                )
    return display, structured


def body_read_objects(read, record, original, *, email_sha, subject=""):
    """Use only the selected, hash-verified READ for body shipment observations."""
    inventory = next(
        (
            item
            for item in read.get("inventory", [])
            if item.get("material_id") == record.get("material_id")
        ),
        None,
    )
    if (
        not inventory
        or inventory.get("read_state") not in {"success", "partial_success"}
        or not inventory.get("intake_source_bound")
        or inventory.get("representation_sha256") != record.get("content", {}).get("sha256")
    ):
        return {"shipments": [], "packing_rows": []}
    execution = next(
        (
            item
            for item in read.get("executions", [])
            if item.get("execution_id") == inventory.get("execution_id")
        ),
        None,
    )
    if not execution or not execution.get("ref"):
        return {"shipments": [], "packing_rows": []}
    document, _ = _blocks(execution)
    return body_objects(
        document,
        original,
        email_sha=email_sha,
        material_sha=record["content"]["sha256"],
        subject=subject,
    )


def text_view(raw, fmt, is_body):
    source = raw.decode("utf-8-sig")
    text, quoted, cells = split_body(source, fmt == "html", scope_quotes=is_body)
    visible = text[:TEXT_LIMIT]
    split = visible.splitlines()
    lines = split[:LINE_LIMIT]
    # Avoid deriving a field from a cut-off final line.
    examined = lines[:-1] if len(text) > TEXT_LIMIT and len(split) <= LINE_LIMIT else lines
    blocks, observations = (
        inquiry_body_inputs(examined)
        if is_body
        else ([(line, {"line": i}) for i, line in enumerate(examined, 1)], [])
    )
    clipped = max(len(source), len(text), len(quoted)) > TEXT_LIMIT or len(split) > LINE_LIMIT
    tables, structured = html_table_fields(cells, is_body=is_body) if not clipped else ([], {})
    fields = collect_fields(
        BODY if is_body else (*BODY, *[n for n in CARGO if n not in BODY]),
        blocks,
        structured,
        source_kind="current_body" if is_body else "attachment",
    )
    return {
        "type": "text",
        "original": source[:TEXT_LIMIT],
        "lines": lines,
        "quoted": quoted[:TEXT_LIMIT],
        "fields": fields,
        "observations": observations,
        "tables": tables,
        "clipped": clipped,
    }


PACKING_HEADERS = {
    "product_name": {"DESCRIPTION", "PRODUCT", "PRODUCTNAME", "品名", "产品名称"},
    "style": {"STYLENO", "STYLE", "SKU", "款号"},
    "size": {"SIZE", "尺码"},
    "color": {"COLOR", "COLOUR", "颜色"},
    "ctns": {"CTN", "CTNS", "CARTONS", "TOTALCTNS", "TOTALCARTONS", "箱数", "总箱数"},
    "item_quantity": {"TOTALQTY", "TOTALQUANTITY", "TOTALPCS", "TOTALQTY(PCS)", "总件数", "总数量"},
    "line_gross_weight": {"TOTALGW(KG)", "TOTALGW", "TOTALGROSSWEIGHT(KG)", "总毛重"},
    "line_net_weight": {"TOTALNW(KG)", "TOTALNW", "TOTALNETWEIGHT(KG)", "总净重"},
    "line_volume": {"TOTALCBM", "TOTALM3", "TOTALM³", "总体积"},
    "carton_size": {"CARTONSIZE", "箱规"},
}


def xlsx_packing(sheet, table):
    """Identify a packing grid with separate carton and product-quantity columns."""
    rows = {}
    for block in table:
        row = block["cell"]["start_row_offset_idx"] + 1
        col = sheet["columns"][block["cell"]["start_col_offset_idx"]]
        rows.setdefault(row, {})[col] = block
    required = {"ctns", "item_quantity"}
    metrics = {"line_net_weight", "line_gross_weight", "line_volume"}
    layouts = []
    for row, cells in rows.items():
        normalized = {
            col: re.sub(r"[\s._'’]+", "", cell["text"]).upper() for col, cell in cells.items()
        }
        columns = {}
        for name, aliases in PACKING_HEADERS.items():
            matches = [col for col, value in normalized.items() if value in aliases]
            if len(matches) == 1:
                columns[name] = matches[0]
        if required <= columns.keys() and metrics & columns.keys():
            layouts.append((row, columns))
    if len(layouts) != 1:
        return None, {}
    header, columns = layouts[0]
    totals = [
        (n, c)
        for n, cells in rows.items()
        if n > header
        for c in cells.values()
        if TOTAL_LABEL.fullmatch(c["text"])
        or re.fullmatch(r"total\s*(?:总计|合计)|总计", c["text"], re.I)
    ]
    if len(totals) != 1:
        return None, {}
    total, marker = totals[0]

    def count(cell):
        if cell and (m := re.fullmatch(r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.0+)?", cell["text"])):
            return int(m[1].replace(",", ""))
        return None

    detail_rows = [
        n
        for n, cells in rows.items()
        if header < n < total
        and any(count(cells.get(columns[name])) is not None for name in required)
    ]
    found = {}
    for name, unit in (("ctns", "carton"), ("item_quantity", "piece")):
        col = columns[name]
        cell = rows[total].get(col)
        if (number := count(cell)) is not None:
            found[name] = [(number, unit, table_evidence([cell, rows[header][col], marker]))]
    return {
        "header_row": header,
        "total_row": total,
        "detail_rows": detail_rows,
        "columns": columns,
    }, found


def xlsx_explicit_quantity(table):
    """Keep a stated SET/PCS total tied to its quantity and unit headers."""
    markers = [block for block in table if TOTAL_LABEL.fullmatch(block["text"])]
    if len(markers) != 1:
        return []
    marker = markers[0]
    total_row = marker["cell"]["start_row_offset_idx"]
    matches = []
    for value in table:
        if value["cell"]["start_row_offset_idx"] != total_row:
            continue
        amount = re.fullmatch(
            r"(?P<number>\d{1,3}(?:,\d{3})+|\d+)(?:\.0+)?\s*"
            r"(?P<unit>sets?|pcs?|pieces?)",
            value["text"],
            re.I,
        )
        if not amount:
            continue
        unit = "set" if amount["unit"].lower().startswith("set") else "piece"
        col = value["cell"]["start_col_offset_idx"]
        unit_alias = r"sets?" if unit == "set" else r"pcs?|pieces?"
        headers = [
            block
            for block in table
            if block["cell"]["start_col_offset_idx"] == col
            and block["cell"]["start_row_offset_idx"] < total_row
            and re.fullmatch(unit_alias, block["text"], re.I)
        ]
        for header in headers:
            row = header["cell"]["start_row_offset_idx"]
            labels = [
                block
                for block in table
                if block["cell"]["start_col_offset_idx"] == col
                and block["cell"]["start_row_offset_idx"] == row - 1
                and re.fullmatch(r"(?:数量|件数|qty\.?|quantity)", block["text"], re.I)
            ]
            if len(labels) == 1:
                matches.append(
                    (
                        int(amount["number"].replace(",", "")),
                        unit,
                        table_evidence([value, labels[0], header, marker]),
                    )
                )
    return matches if len(matches) == 1 else []


def excel_view(sheets):
    blocks, structured = [], {}
    display = []
    for sheet_index, sheet in enumerate(sheets):
        table = []
        columns = sheet["columns"]
        for row in sheet["rows"]:
            number, cells = row["number"], row["cells"]
            for index, column in enumerate(columns):
                if column not in cells or cells[column]["clipped"]:
                    continue
                text = cells[column]["value"]
                loc = {"sheet": sheet["name"], "cell": f"{column}{number}"}
                blocks.append((text, loc))
                if not sheet["clipped"] and text.strip():
                    table.append(
                        table_block(
                            text.strip(),
                            f"/tables/{sheet_index}/data/table_cells/{len(table)}/text",
                            number - 1,
                            number,
                            index,
                            index + 1,
                            loc,
                        )
                    )
                if (
                    index + 1 < len(columns)
                    and (adj := cells.get(columns[index + 1]))
                    and not adj["clipped"]
                ):
                    # The existing rule still has to recognize the full explicit label/value.
                    if re.fullmatch(r"[\w. ()/\-]+[:：]?", text) and not re.search(r"\d", text):
                        blocks.append(
                            (
                                text.rstrip(":： ") + ": " + adj["value"],
                                {**loc, "end_cell": f"{columns[index + 1]}{number}"},
                            )
                        )
        for name in TABLE_TOTAL_FIELDS:
            for value, unit, evidence in _pdf_table_field(table, name):
                structured.setdefault(name, []).append((value, unit, table_evidence(evidence)))
        packing, quantities = xlsx_packing(sheet, table)
        public_sheet = {
            **sheet,
            "rows": [
                {
                    **row,
                    "cells": {
                        column: {k: v for k, v in cell.items() if k != "number_format"}
                        for column, cell in row["cells"].items()
                    },
                }
                for row in sheet["rows"]
            ],
        }
        display.append({**public_sheet, "packing": packing} if packing else public_sheet)
        for name, candidates in quantities.items():
            structured.setdefault(name, []).extend(candidates)
        if candidates := xlsx_explicit_quantity(table):
            structured.setdefault("item_quantity", []).extend(candidates)
    return {
        "type": "xlsx",
        "sheets": display,
        "clipped": any(s["clipped"] for s in sheets),
        "fields": collect_fields(
            (*CARGO, "item_quantity") if "item_quantity" in structured else CARGO,
            blocks,
            structured,
        ),
    }


def pdf_packing_rows(blocks, execution):
    """Expose verified rule facts with their PDF value and supporting proof locations."""
    rows = []
    for row in packing_rows(blocks, execution):
        page = row["selector"]["page"]
        rows.append(
            {
                **row["selector"],
                "ordinal": row["ordinal"],
                "evidence_spans": agent_evidence_spans(
                    row, lambda block, page=page: {"page": page}
                ),
                "fields": {
                    name: {
                        "value": value,
                        "unit": unit,
                        "sources": [
                            {"page": page, "pointer": b["pointer"], "excerpt": b["text"]}
                            for b in proof
                        ],
                    }
                    for name, (value, unit, proof) in row["facts"].items()
                },
            }
        )
    return rows


def pdf_pages(document, blocks, page_count):
    pages = {i: {"number": i, "lines": [], "tables": []} for i in range(1, page_count + 1)}
    for block in blocks:
        if "cell" in block or not block["text"].strip():
            continue
        for prov in block["provenance"]:
            if page := pages.get(prov.get("page_no")):
                if len(page["lines"]) < 1000:
                    page["lines"].append({"text": block["text"], "pointer": block["pointer"]})
                else:
                    page["clipped"] = True
    documents = (
        [(f"/parts/{i}/document", part["document"]) for i, part in enumerate(document["parts"])]
        if document.get("kind") == "docling_pdf_read_bundle"
        else [("", document)]
    )
    for prefix, observed in documents:
        for table_index, table in enumerate(observed.get("tables", [])):
            data = table.get("data", {})
            cells = []
            for index, cell in enumerate(data.get("table_cells", [])[:3000]):
                cells.append(
                    {
                        "row": cell.get("start_row_offset_idx"),
                        "row_end": cell.get("end_row_offset_idx"),
                        "col": cell.get("start_col_offset_idx"),
                        "col_end": cell.get("end_col_offset_idx"),
                        "text": cell.get("text", ""),
                        "pointer": f"{prefix}/tables/{table_index}/data/table_cells/{index}/text",
                    }
                )
            for prov in table.get("prov", []):
                if page := pages.get(prov.get("page_no")):
                    if cells:
                        page["tables"].append(
                            {
                                "cells": cells,
                                "rows": data.get("num_rows"),
                                "cols": data.get("num_cols"),
                            }
                        )
                    if len(data.get("table_cells", [])) > len(cells):
                        page["clipped"] = True
    return list(pages.values())


def pdf_fields(fields):
    output = []
    for field in fields:
        sources = []
        for entry in field["sources"]:
            source = entry["source"]
            loc = source["locator"]
            sources.append(
                {
                    "excerpt": source["excerpt"],
                    "page": loc.get("page"),
                    "pointer": loc.get("json_pointer"),
                    "start": loc.get("char_start"),
                }
            )
        output.append(
            {
                "name": field["field_name"],
                "value": field["value"],
                "unit": field["unit"],
                "status": field["status"],
                "sources": sources,
                "reason": next((issue["code"] for issue in field["issues"]), None),
            }
        )
    return output


def comparison_entry(row, detail):
    fields = {}
    observed = detail.get("fields", [])
    for name, unit in COMPARISON_FIELDS:
        source_name = name
        if (
            row["kind"] == "body"
            and name == "line_gross_weight"
            and any(f["name"] == "shipment_gross_weight" for f in observed)
        ):
            source_name = "shipment_gross_weight"
        items = [field for field in observed if field["name"] == source_name]
        conflict = any(field["status"] == "conflict" for field in items)
        known = (
            len(items) == 1
            and items[0]["status"] == "known"
            and items[0]["unit"] == unit
            and items[0]["sources"]
            and type(items[0]["value"]) in {int, float}
        )
        fields[name] = {
            "value": items[0]["value"] if known else None,
            "unit": unit,
            "status": "known" if known else "conflict" if conflict else "uncertain",
            "sources": [source for item in items for source in item["sources"]],
            "reason": (
                None
                if known
                else "multiple_values"
                if conflict
                else (items[0].get("reason") if items else None)
                or row.get("reason")
                or "no_evidence"
            ),
        }
    return {**row, "fields": fields}


def upload_email_header(raw):
    """Preview routing headers before the material worker starts."""
    prefix = raw[:EARLY_EMAIL_HEADER_BYTES]
    boundary = re.search(rb"\r?\n\r?\n", prefix)
    if boundary is None:
        return []
    try:
        header = BytesHeaderParser(policy=policy.default).parsebytes(prefix[: boundary.end()])
        return [
            {
                "subject": str(header.get("subject") or ""),
                "sent_at": str(header.get("date") or ""),
                "thread_headers": {
                    "message_id": str(header.get("message-id") or ""),
                    "in_reply_to": str(header.get("in-reply-to") or ""),
                    "references": re.findall(r"<[^<>\s]+>", str(header.get("references") or "")),
                },
            }
        ]
    except (TypeError, ValueError, IndexError):
        return []


class Workbench:
    def __init__(
        self,
        output,
        python,
        cache,
        *,
        extraction_agent=None,
        group_context_fallback=False,
        business_record_provider=None,
    ):
        self.output = Path(output).resolve()
        self.python, self.cache = Path(python), Path(cache).resolve()
        self.jobs = {}
        self.lock = threading.Lock()
        self.model_lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.group_pool = ThreadPoolExecutor(max_workers=1)
        self.group_runs = {}
        self.ocr_runs = {}
        self.versions = None
        self.extraction_agent = extraction_agent
        self.business_record_provider = business_record_provider
        self.material_agent = getattr(extraction_agent, "extract_material", None)
        self.object_agent = getattr(extraction_agent, "extract_objects", None)
        self.group_context_agent = (
            getattr(extraction_agent, "extract_group_context", None)
            if group_context_fallback
            else None
        )

    def _group_agent_enabled(self, selected):
        return bool(
            self.material_agent
            and (len(selected) >= 2 or self.group_context_agent)
            and any(j["route"] == "deepseek-group" for j in selected)
        )

    def submit(self, filename, raw, parse_scope="all", route="preview"):
        route = ROUTE_ALIASES.get(route, route)
        if (
            route not in ROUTES
            or (route == "deepseek" and self.extraction_agent is None)
            or (route == "deepseek-group" and self.material_agent is None)
        ):
            raise ValueError("pipeline_route_unavailable")
        is_email = Path(filename).suffix.lower() == ".eml"
        if route == "deepseek-group" and not is_email:
            route = "docling"
        parse_scope = parse_scope if is_email else "all"
        metadata = upload_email_header(raw) if is_email else []
        with self.lock:
            if (
                sum(job["state"] in {"queued", "running"} for job in self.jobs.values())
                >= MAX_ACTIVE
            ):
                return None
            key = uuid4().hex
            self.jobs[key] = {
                "id": key,
                "filename": filename,
                "is_email": is_email,
                "parse_scope": parse_scope,
                "route": route,
                "state": "queued",
                "materials": [],
                "metadata": metadata,
                "issues": [],
                "assets": {},
                "details": {},
                "replays": {},
                "origins": {},
                "email_sha256": None,
                "html_previews": {},
                "inline_images": {},
                "xlsx_sheet_views": {},
            }
        self.pool.submit(self.run, key, filename, raw, parse_scope, route)
        return key

    def summary(self, key):
        with self.lock:
            job = self.jobs.get(key)
            if job is not None:
                return {
                    name: value if name != "materials" else value[:]
                    for name, value in job.items()
                    if name
                    not in {
                        "assets",
                        "details",
                        "replays",
                        "origins",
                        "email_sha256",
                        "html_previews",
                        "inline_images",
                        "xlsx_sheet_views",
                    }
                }

    def original_html(self, key, index):
        with self.lock:
            job = self.jobs.get(key, {})
            preview = job.get("html_previews", {}).get(index)
            images = job.get("inline_images", {})
        if preview is None:
            return None
        root, content = preview
        text = p02.verified_content(root, content).decode("utf-8-sig")
        cache = {}

        def resolve(cid):
            if cid not in cache:
                cache[cid] = None
                if image := images.get(cid):
                    descriptor, fmt = image
                    try:
                        data = p02.verified_content(root, descriptor)
                    except (OSError, ValueError):
                        pass
                    else:
                        encoded = base64.b64encode(data).decode("ascii")
                        cache[cid] = f"data:image/{fmt};base64,{encoded}"
            return cache[cid]

        return render_original_html(text, resolve, image_budget=LIMITS.max_input_bytes)

    def comparison(self, key, attachment_ids):
        with self.lock:
            job = self.jobs.get(key)
            if job is None:
                raise LookupError("not_found")
            if not job["is_email"]:
                raise ValueError("comparison_not_email")
            origins, email = job["origins"], job["email_sha256"]
            inventory = {row["id"]: row for row in job["materials"] if row["id"] in origins}
            if (
                not attachment_ids
                or len(set(attachment_ids)) != len(attachment_ids)
                or any(
                    mid not in inventory
                    or inventory[mid]["kind"] not in {"inline", "attachment"}
                    or inventory[mid]["format"] not in FORMATS
                    for mid in attachment_ids
                )
            ):
                raise ValueError("comparison_invalid_selection")
            bodies = [row for row in inventory.values() if row["kind"] == "body"]
            if len(bodies) == 1:
                body = comparison_entry(bodies[0], job["details"].get(bodies[0]["id"], {}))
            else:
                body = comparison_entry(
                    {
                        "id": None,
                        "kind": "body",
                        "state": "uncertain",
                        "reason": "ambiguous_body" if bodies else "no_current_body",
                    },
                    {},
                )
            documents = [
                comparison_entry(inventory[mid], job["details"].get(mid, {}))
                for mid in attachment_ids
            ]

        def occurrence(entry):
            return {
                "email_sha256": email,
                **origins.get(entry["id"], {}),
                "id": entry["id"],
                "fields": [
                    {**field, "field_name": name} for name, field in entry["fields"].items()
                ],
            }

        totals, duplicates = compare_material_totals(
            occurrence(body), [occurrence(d) for d in documents]
        )
        for name, total in totals.items():
            total["selected_status"] = (
                "known"
                if total["selected_document_sum"] is not None
                else "conflict"
                if any(d["fields"][name]["status"] == "conflict" for d in documents)
                else "uncertain"
            )
        return {
            "scope": "selected_attachments_from_one_email",
            "body": body,
            "attachments": documents,
            "totals": totals,
            "duplicates": [group["run_ids"] for group in duplicates],
        }

    def _group_selection(self, current, ids):
        if not 1 <= len(ids) <= MAX_GROUP or len(set(ids)) != len(ids) or current not in ids:
            raise ValueError("group_invalid_selection")
        selected = [self.jobs.get(key) for key in (current, *(i for i in ids if i != current))]
        if any(job is None for job in selected):
            raise LookupError("not_found")
        if any(not job["is_email"] for job in selected):
            raise ValueError("group_invalid_selection")
        return selected, tuple(sorted(ids))

    def _inline_signature(self, job):
        bodies = [row for row in job["materials"] if row.get("kind") == "body"]
        if len(bodies) != 1 or bodies[0]["id"] not in job.get("html_previews", {}):
            return []
        root, content = job["html_previews"][bodies[0]["id"]]
        try:
            html = p02.verified_content(root, content).decode("utf-8-sig")
            current = set(current_inline_cids(html))
            sender = next(iter(job.get("metadata", [])), {}).get("from_addr")
            output = []
            for row in job["materials"]:
                if row.get("kind") != "inline" or row.get("format") not in {"png", "jpeg"}:
                    continue
                cids = [
                    cid
                    for cid, (ref, _) in job.get("inline_images", {}).items()
                    if cid in current and ref.get("sha256") == row.get("sha256")
                ]
                chain = job["details"].get(row["id"], {}).get("chain", {})
                info, read_info = chain.get("preprocessing", {}), chain.get("read", {})
                mid, read_sha = info.get("material_id"), read_info.get("sha256")
                if (
                    len(cids) != 1
                    or not mid
                    or read_info.get("state") not in {"success", "partial_success"}
                ):
                    continue
                path = (self.output / job["id"] / ("attachment-" + mid) / "read.json").resolve()
                if not path.is_relative_to(self.output):
                    continue
                data = path.read_bytes()
                read = json.loads(data)
                if (
                    hashlib.sha256(data).hexdigest() != read_sha
                    or read.get("material_id") != mid
                    or read.get("input_sha256") != row.get("sha256")
                ):
                    continue
                output.extend(inline_signature_fields(read, sender, row["id"], read_sha))
            return output
        except (OSError, ValueError, UnicodeError, KeyError, TypeError):
            return []

    def group_review(
        self, current, ids, *, confirmed_first_email_id=None, first_inquiry_evidence=None
    ):
        with self.lock:
            selected, key = self._group_selection(current, ids)
            run = self.group_runs.get(key, {})
            overrides = run.get("fields", {})
            enabled = self._group_agent_enabled(selected)
            waiting = any(j["state"] in {"queued", "running"} for j in selected) or any(
                job_id in ids and task["state"] == "running"
                for (job_id, _, _), task in self.ocr_runs.items()
            )
            has_reads = any(j["replays"] for j in selected)
            observations, measurements = [], []
            for job in selected:
                bodies = [row for row in job["materials"] if row["kind"] == "body"]
                if len(bodies) == 1:
                    mid = bodies[0]["id"]
                    sources = job["details"].get(mid, {}).get("observations", [])
                    if sources:
                        observations.append(
                            {
                                "email_id": job["id"],
                                "material_id": mid,
                                "total": len(sources),
                                "sources": sources[:5],
                            }
                        )
                    measurement = (
                        job["details"].get(mid, {}).get("chain", {}).get("body_measurement")
                    )
                    if measurement:
                        measurements.append(
                            {"email_id": job["id"], "material_id": mid, **measurement}
                        )
            jobs = [
                {
                    **{
                        k: job.get(k)
                        for k in (
                            "id",
                            "filename",
                            "metadata",
                            "state",
                            "reason",
                            "parse_scope",
                            "route",
                            "email_sha256",
                        )
                    },
                    "materials": [
                        {
                            **row,
                            "mime_path": job["origins"].get(row["id"], {}).get("mime_path"),
                            "fields": job["details"].get(row["id"], {}).get("fields", []),
                            "packing_rows": job["details"]
                            .get(row["id"], {})
                            .get("packing_rows", []),
                            "body_shipments": job["details"]
                            .get(row["id"], {})
                            .get("body_shipments", []),
                            "read_material_id": (
                                job["details"].get(row["id"], {}).get("chain") or {}
                            )
                            .get("preprocessing", {})
                            .get("material_id"),
                            "read_bound": (job["details"].get(row["id"], {}).get("chain") or {})
                            .get("read", {})
                            .get("state")
                            in {"success", "partial_success"},
                        }
                        for row in job["materials"]
                    ],
                }
                for job in selected
            ]
            signatures = {job["id"]: self._inline_signature(job) for job in selected}
            for job in jobs:
                for row in job["materials"]:
                    if row["kind"] == "body":
                        row["fields"] = merge_signature_fields(row["fields"], signatures[job["id"]])
            agent = {k: v for k, v in run.items() if k not in {"fields", "object_fields"}}
        rules = build_group_review(jobs, GROUP_BODY)
        if overrides:
            applied_jobs = [
                {
                    **job,
                    "materials": [
                        {
                            **row,
                            "fields": merge_signature_fields(
                                overrides.get((job["id"], row["id"]), row["fields"]),
                                signatures[job["id"]],
                            )
                            if row["kind"] == "body"
                            else overrides.get((job["id"], row["id"]), row["fields"]),
                        }
                        for row in job["materials"]
                    ],
                }
                for job in jobs
            ]
            result = build_group_review(applied_jobs, GROUP_BODY)
        else:
            result = rules
            applied_jobs = jobs
        if run.get("object_fields"):
            applied_jobs = deepcopy(applied_jobs)
            for job in applied_jobs:
                for material in job["materials"]:
                    for collection, field_key in (
                        ("body_shipments", "direct_fields"),
                        ("packing_rows", "fields"),
                    ):
                        for obj in material.get(collection, []):
                            object_id = (
                                cargo_line_identity(job["id"], material["id"], obj)
                                if collection == "packing_rows"
                                else obj.get("object_id")
                            )
                            updates = run["object_fields"].get(
                                (job["id"], material["id"], object_id), {}
                            )
                            for name, value in updates.items():
                                existing = obj[field_key].get(name)
                                if existing is None or (
                                    existing.get("value") is None
                                    and existing.get("status") == "uncertain"
                                ):
                                    obj[field_key][name] = value
        system_result = None
        if self.business_record_provider is not None:
            try:
                system_result = self.business_record_provider(inquiry_identity(ids), tuple(ids))
            except Exception:
                system_result = {"state": "query_failed"}
        structured_result = build_inquiry_result(
            result,
            applied_jobs,
            confirmed_first_email_id=confirmed_first_email_id,
            system_result=system_result,
            context_supplements=(run.get("context") or {}).get("supplements", ()),
            first_inquiry_evidence=first_inquiry_evidence,
        )
        return {
            **result,
            "structured_result": structured_result,
            "body_observations": observations,
            "body_measurements": measurements,
            **(
                {"route_comparison": compare_group_routes(rules, result)}
                if run.get("state") in {"complete", "partial"}
                else {}
            ),
            "agent": {
                "enabled": enabled,
                "context_enabled": enabled and self.group_context_agent is not None,
                "state": "waiting" if waiting else "ready" if has_reads else "no_read",
                "max_requests": MAX_GROUP_AGENT_CALLS,
                **agent,
            },
        }

    def start_group_agent(self, current, ids):
        with self.lock:
            selected, key = self._group_selection(current, ids)
            if not self._group_agent_enabled(selected):
                raise ValueError("group_agent_not_enabled")
            if key in self.group_runs:
                return self.group_runs[key]["state"]
            if any(j["state"] in {"queued", "running"} for j in selected) or any(
                job_id in ids and task["state"] == "running"
                for (job_id, _, _), task in self.ocr_runs.items()
            ):
                raise ValueError("group_agent_waiting")
            if any(r["state"] == "running" for r in self.group_runs.values()):
                raise ValueError("group_agent_busy")
            inputs, seen, initial_fields = [], set(), {}
            for job in selected:
                for row in job["materials"]:
                    context = job["replays"].get(row["id"])
                    identity = (row["kind"] == "body", row.get("sha256"))
                    if context and identity not in seen:
                        seen.add(identity)
                        original = job["details"].get(row["id"], {}).get("fields", [])
                        initial_fields[(job["id"], row["id"])] = original
                        gaps = context_gaps(original)
                        inputs.append((job["id"], row["id"], row["kind"], context, gaps))
            if not inputs:
                raise ValueError("group_agent_no_read")
            self.group_runs[key] = {"state": "running", "total": len(inputs), "requests": 0}
        # Keep the first body and first attachment ahead of additional replies
        # so both kinds can be evaluated within the group request budget.
        inputs.sort(key=lambda item: item[2] != "body")
        for index, item in enumerate(inputs[1:], 1):
            if item[2] != "body":
                inputs.insert(1, inputs.pop(index))
                break
        self.group_pool.submit(self._run_group_agent, key, inputs, initial_fields)
        return "running"

    def _run_group_agent(self, key, inputs, initial_fields):
        run_id = uuid4().hex
        fields, validation, requests, accepted, corroborated, rejected, failures, evaluated = (
            {},
            [],
            0,
            0,
            0,
            0,
            0,
            0,
        )

        narrow_multi, baseline_fields = False, []

        def adapter(receipt, request, *, timeout_seconds):
            nonlocal requests
            request = natural_gap_material_request(
                request, baseline_fields, multi_shipment=narrow_multi
            )
            if not request["requirements"] or not request["evidence"]:
                receipt.update(request_state="not_sent", reason="no_evidenced_gap")
                return {"candidates": []}
            receipt["requested_fields"] = [item["field_name"] for item in request["requirements"]]
            receipt["evidence_ids"] = [item["evidence_id"] for item in request["evidence"]]
            receipt["markdown_fragments"] = sum(
                item.get("representation") == "markdown" for item in request["evidence"]
            )
            receipt["read_fragments"] = sum(
                item.get("representation") == "read" for item in request["evidence"]
            )
            if requests >= MAX_GROUP_AGENT_CALLS:
                raise AgentServiceError("group_agent_request_limit")
            requests += 1
            windows = [e["context_for"] for e in request["evidence"] if e.get("context_for")]
            if windows:
                receipt.update(
                    context_fragments=len(windows),
                    context_fields=sorted({f for window in windows for f in window}),
                )
            response = self.material_agent(request, timeout_seconds=timeout_seconds)
            response, unsent_requirements, unsent_evidence = restrict_material_response(
                response, request
            )
            if unsent_requirements:
                receipt["unsent_requirement_candidates"] = unsent_requirements
            if unsent_evidence:
                receipt["unsent_evidence_candidates"] = unsent_evidence
            return response

        for job_id, row_id, kind, context, gaps in inputs:
            if requests >= MAX_GROUP_AGENT_CALLS:
                break
            evaluated += 1
            prepared, root, record, job_root, baseline, sheet_name = context
            with self.lock:
                detail = self.jobs[job_id]["details"].get(str(row_id), {})
                narrow_multi = (
                    kind == "body"
                    and sum(
                        s.get("source_role") == "current_body"
                        for s in detail.get("body_shipments", [])
                    )
                    > 1
                )
            baseline_fields = initial_fields.get((job_id, row_id), [])
            sheet_view = None
            if sheet_name is not None:
                with self.lock:
                    detail = self.jobs[job_id]["details"].get(str(row_id), {})
                    private = self.jobs[job_id].get("xlsx_sheet_views", {}).get(str(row_id))
                    matches = [
                        s
                        for s in ([private] if private else detail.get("sheets", []))
                        if s["name"] == sheet_name
                    ]
                    if len(matches) == 1:
                        sheet_view = matches[0]
            receipt = {}

            try:
                evidence = functools.partial(
                    workbench_material_evidence, gaps if kind == "body" else ()
                )
                with scoped_material_evidence(evidence):
                    _, _, chain = self.main_result(
                        prepared,
                        root,
                        record,
                        job_root,
                        "deepseek",
                        replay=(baseline, functools.partial(adapter, receipt), run_id),
                        sheet_name=sheet_name,
                        sheet_view=sheet_view,
                    )
                events = chain["agent"]["events"]
                if receipt.get("request_state") == "not_sent":
                    for event in events:
                        if (
                            event.get("stage") == "material_agent"
                            and event.get("outcome") == "received"
                        ):
                            event.update(outcome="skipped", reason=receipt["reason"])
                elif "requested_fields" in receipt:
                    for event in events:
                        if event.get("stage") == "material_agent":
                            event.update(
                                requested_fields=len(receipt["requested_fields"]),
                                markdown_fragments=receipt["markdown_fragments"],
                                read_fragments=receipt["read_fragments"],
                            )
                events.extend(
                    {
                        "stage": "material_candidate",
                        "outcome": "rejected",
                        "reason": "evidence_binding_failed",
                        "reason_detail": "evidence_not_sent",
                        "requirement_id": rid,
                    }
                    for rid in receipt.get("unsent_evidence_candidates", [])
                )
                events.extend(
                    {
                        "stage": "material_candidate",
                        "outcome": "rejected",
                        "reason": "evidence_binding_failed",
                        "reason_detail": "requirement_not_sent",
                        "requirement_id": rid,
                    }
                    for rid in receipt.get("unsent_requirement_candidates", [])
                )
                accepted += sum(e["outcome"] == "accepted" for e in events)
                corroborated += sum(e["outcome"] == "corroborated" for e in events)
                rejected += sum(
                    (e.get("rejected_shape") or 0) + (e["outcome"] == "rejected") for e in events
                )
                failures += sum(e["outcome"] in {"failed", "fallback"} for e in events)
                if events:
                    validation.append(
                        {"email_id": job_id, "material_id": row_id, "events": events, **receipt}
                    )
                if any(e["outcome"] == "accepted" for e in events):
                    fields[(job_id, row_id)] = chain["structured"]["fields"]
            except Exception as error:
                failures += 1
                print(
                    f"Group extraction {run_id}/{row_id}: {type(error).__name__}", file=sys.stderr
                )
                if os.getenv("DOCCASE_DEBUG"):
                    traceback.print_exc(limit=5, file=sys.stderr)
        context_result = None
        if self.group_context_agent:
            try:
                missing = missing_fields(inputs, initial_fields, fields)
                context_result = run_context(
                    inputs,
                    missing,
                    self.group_context_agent,
                    budget=requests < MAX_GROUP_AGENT_CALLS,
                )
            except Exception as error:
                # A failed second pass must not hide the ordinary group result.
                context_result = {"state": "local_failure", "supplements": []}
                print(f"Group context {run_id}: {type(error).__name__}", file=sys.stderr)
                if os.getenv("DOCCASE_DEBUG"):
                    traceback.print_exc(limit=5, file=sys.stderr)
        object_fields, object_events = {}, []
        if self.object_agent:
            used = requests + (context_result or {}).get("requests", 0)
            with self.lock:
                objects = []
                for job_id, row_id, material_kind, _, _ in inputs:
                    detail = self.jobs[job_id]["details"].get(row_id, {})
                    collections = (
                        (("body_shipments", "shipment"), ("packing_rows", "cargo_line"))
                        if material_kind == "body"
                        else (("packing_rows", "cargo_line"),)
                    )
                    for collection, kind in collections:
                        for source in detail.get(collection, []):
                            obj = deepcopy(source)
                            if kind == "cargo_line":
                                obj["object_id"] = cargo_line_identity(job_id, row_id, obj)
                                if material_kind == "attachment":
                                    obj["source_role"] = "attachment"
                            objects.append((job_id, row_id, kind, obj))
            for job_id, row_id, kind, obj in objects:
                packet, bindings, reason = object_packet(obj, kind, approved_model_fragment)
                audit = {
                    "email_id": job_id,
                    "material_id": row_id,
                    "object_id": obj["object_id"],
                    "kind": kind,
                }
                if not packet:
                    object_events.append(
                        {
                            **audit,
                            "outcome": "not_sent"
                            if reason == "no_approved_fragment"
                            else "unselected",
                            "reason": reason,
                        }
                    )
                    continue
                audit.update(
                    fields=[r["field"] for r in packet["requirements"]],
                    evidence=[
                        {"evidence_id": eid, "pointer": span["pointer"]}
                        for eid, span in bindings.items()
                    ],
                )
                if used >= MAX_GROUP_AGENT_CALLS:
                    object_events.append(
                        {**audit, "outcome": "not_sent", "reason": "request_limit"}
                    )
                    continue
                used += 1
                try:
                    response = self.object_agent(packet, timeout_seconds=15)
                    existing = (
                        obj.get("fields", {})
                        if kind == "cargo_line"
                        else obj.get("direct_fields", {})
                    )
                    updates, events = validate_object_response(packet, bindings, response, existing)
                    if updates:
                        object_fields[(job_id, row_id, obj["object_id"])] = updates
                    object_events.append(
                        {**audit, "outcome": "response_received", "events": events}
                    )
                except (AgentServiceError, TimeoutError):
                    object_events.append(
                        {
                            **audit,
                            "outcome": "request_failed",
                            "reason": "timeout_or_service_failure",
                        }
                    )
            requests = used - (context_result or {}).get("requests", 0)
        with self.lock:
            self.group_runs[key] = {
                "state": "partial" if failures or evaluated < len(inputs) else "complete",
                "total": len(inputs),
                "evaluated": evaluated,
                "requests": requests,
                "accepted": accepted,
                "corroborated": corroborated,
                "rejected": rejected,
                "failures": failures,
                "validation": validation,
                "fields": fields,
                "object_fields": object_fields,
                "object_events": object_events,
                **({"context": context_result} if context_result is not None else {}),
            }

    def pdf_ocr(self, key, index, page, *, start=False):
        identity = (key, index, page)
        with self.lock:
            job = self.jobs.get(key, {})
            detail = job.get("details", {}).get(index)
            if not detail or detail["type"] != "pdf":
                raise LookupError("not_found")
            if not 1 <= page <= len(detail["pages"]):
                raise ValueError("ocr_invalid_page")
            prior = self.ocr_runs.get(identity)
            if not start or (prior and prior["state"] in {"running", "complete"}):
                return prior or {"state": "not_requested"}
            context = job["replays"].get(index)
            if not context:
                raise ValueError("ocr_read_unavailable")
            if any(
                k[0] == key and k[1] == index and v["state"] == "running"
                for k, v in self.ocr_runs.items()
            ):
                raise ValueError("ocr_busy")
            if any(
                key in ids and run["state"] == "running" for ids, run in self.group_runs.items()
            ):
                raise ValueError("ocr_agent_running")
            self.ocr_runs[identity] = {"state": "running"}
            route = job["route"]
        self.pool.submit(self._run_pdf_ocr, identity, context, route, detail)
        return {"state": "running"}

    def _run_pdf_ocr(self, identity, context, route, old):
        key, index, page = identity
        prepared, root, record, job_root, baseline, _ = context
        replay_context = []
        try:
            detail = self.pdf_view(
                prepared,
                root,
                record,
                job_root,
                route,
                remember=replay_context.append,
                supplement=(baseline, page),
            )
            if not any(
                p["kind"] == "ocr" and p["pages"] == [page, page] and p["status"] == "success"
                for p in detail["pdf_passes"]
            ):
                raise ValueError("ocr_pass_incomplete")
            # An extra pass may add evidence or mark a conflict, but cannot erase a known value.
            for field in old["fields"]:
                if field["status"] == "known" and not any(
                    f["name"] == field["name"]
                    and (
                        f["status"] == "conflict"
                        or (f["status"], f["value"], f["unit"])
                        == ("known", field["value"], field["unit"])
                    )
                    for f in detail["fields"]
                ):
                    raise ValueError("ocr_baseline_field_changed")
                if field["status"] == "conflict" and not any(
                    f["name"] == field["name"] and f["status"] == "conflict"
                    for f in detail["fields"]
                ):
                    raise ValueError("ocr_baseline_conflict_changed")
            with self.lock:
                job = self.jobs[key]
                job["details"][index] = detail
                job["replays"][index] = replay_context[0]
                row = job["materials"][int(index)]
                job["materials"][int(index)] = {
                    **row,
                    "state": "partial" if detail["unread_pages"] or detail["warnings"] else "ready",
                    "known": sum(f["status"] == "known" for f in detail["fields"]),
                }
                states = [item["state"] for item in job["materials"] if item["state"] != "skipped"]
                if job["state"] not in {"queued", "running"}:
                    job["state"] = "ready" if all(s == "ready" for s in states) else "partial"
                for ids in [ids for ids in self.group_runs if key in ids]:
                    del self.group_runs[ids]
                self.ocr_runs[identity] = {"state": "complete"}
        except Exception as error:
            with self.lock:
                self.ocr_runs[identity] = {"state": "failed", "reason": "ocr_supplement_failed"}
            print(f"PDF OCR {key}/{index}/{page}: {type(error).__name__}", file=sys.stderr)
            if os.getenv("DOCCASE_DEBUG"):
                traceback.print_exc(limit=5, file=sys.stderr)

    def set(self, key, **values):
        with self.lock:
            self.jobs[key].update(values)

    def item(self, key, index, **values):
        with self.lock:
            job = self.jobs[key]
            if "detail" in values:
                job["details"][str(index)] = values.pop("detail")
            if "replay" in values:
                job["replays"][str(index)] = values.pop("replay")
            if "sheet_view" in values:
                job["xlsx_sheet_views"][str(index)] = values.pop("sheet_view")
            job["materials"][index] = {**job["materials"][index], **values}

    def auxiliary(self, operation, root, content, **options):
        request = {
            "operation": operation,
            "root": str(root),
            "path": content["path"],
            "sha256": content["sha256"],
            "cache": str(self.cache),
            **options,
        }
        env = {
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }

        def call():
            return subprocess.run(
                [str(self.python), str(WORKER)],
                input=json.dumps(request),
                encoding="utf-8",
                capture_output=True,
                timeout=60,
                env=env,
                check=True,
            )

        if operation == "image_ocr":
            with self.model_lock:
                result = call()
        else:
            result = call()
        return json.loads(result.stdout)

    def main_result(
        self,
        prepared,
        root,
        record,
        job_root,
        route,
        *,
        remember=None,
        replay=None,
        sheet_name=None,
        sheet_view=None,
        supplement=None,
    ):
        call = functools.partial(p02.worker, python=self.python, model_cache=self.cache)

        def worker(request):
            # Serialize local model use; independent text and spreadsheet jobs can continue.
            with self.model_lock:
                try:
                    return call(request)
                except RuntimeError:
                    if request["operation"] != "convert":
                        raise
                    return {
                        "conversion_status": "failure",
                        "errors": [{"code": "worker_failed"}],
                        "versions": request["versions"],
                        "json_path": None,
                        "json_sha256": None,
                        "termination_reason": "failure",
                    }

        if self.versions is None:
            self.versions = worker({"operation": "probe", "hard_timeout_seconds": 20})
        manifest = prepared["received"]["manifest"]
        is_pdf = record["format"] == "pdf"
        profile_name = "indexed-pdf-document-cargo" if is_pdf else WEIGHT_INQUIRY_PROFILE
        profile = (
            _indexed_profile(profile_name, manifest, record)
            if is_pdf
            else current_body_profile(manifest, record, profile_name)
        )
        if sheet_name is not None:
            profile_name = "local-xlsx-document-totals"
            profile = _indexed_profile("indexed-pdf-document", manifest, record)
            profile["id"] = profile_name
            profile["requirements"] = [
                {key: value for key, value in req.items() if not key.startswith("pdf_")}
                for req in profile["requirements"]
                if req["field_name"] in TABLE_TOTAL_FIELDS
            ]
        chain_options = {}
        output = job_root / ("read-" + record["material_id"])
        reader = p02.parse_prepared
        pdf_support = p02.pdf_supplement_support() if is_pdf and not replay else None
        if supplement:
            previous, page = supplement
            previous_read = read_result(previous["read_ref"])
            execution = next(e for e in previous_read["executions"] if e.get("ref"))
            pdf_support = p02.pdf_supplement_support(ocr_page=page)
            pdf_support["read_pdf_adaptive"] = functools.partial(
                pdf_support["read_pdf_adaptive"], reuse_result=execution["result"]
            )
            pdf_support["table_supplement_pages"] = lambda *_: []
            output = job_root / ("ocr-" + uuid4().hex)
        if replay:
            baseline, adapter, run_id = replay
            fixed_read = read_result(baseline["read_ref"])

            def reader(*_args, **_kwargs):
                return deepcopy(fixed_read)

            output = job_root / ("group-" + run_id + "-" + record["material_id"])
            sidecar = saved_markdown(baseline, job_root, record["material_id"])
        if route != "preview":

            def export(*args):
                if replay:
                    return sidecar
                try:
                    return p02.export_read_markdown(*args, worker_fn=worker)
                except (RuntimeError, TimeoutError, subprocess.TimeoutExpired):
                    return {"state": "failure", "reason": "markdown_export_failed"}

            chain_options["markdown_provider"] = export
            if replay:
                chain_options.update(material_agent=adapter, agent_timeout_seconds=15)
            elif route == "deepseek":
                chain_options.update(
                    extraction_agent=self.extraction_agent,
                    agent_read_fallback=True,
                    agent_timeout_seconds=15,
                )
        result = run_main_channel(
            prepared["manifest_path"],
            root,
            expected_task_id=manifest["task_id"],
            expected_request_fingerprint=prepared["request_fingerprint"],
            intake_context=prepared["intake_context"],
            selected_material_ids={record["material_id"]},
            read_configurations=p02.configurations(
                cache_path=self.cache,
                declared_versions=self.versions,
                docling_text=route != "preview",
                **({"sheet_name": sheet_name} if sheet_name is not None else {}),
            ),
            reader_versions=self.versions,
            field_profile=profile,
            field_contract_ref={
                "version": "0.1.0",
                "ref": str(FIELD_CONTRACT),
                "sha256": hashlib.sha256(FIELD_CONTRACT.read_bytes()).hexdigest(),
            },
            output_dir=output,
            reader=reader,
            worker_fn=worker,
            pdf_support=pdf_support,
            **chain_options,
        )
        if replay and result["read_ref"]["sha256"] != baseline["read_ref"]["sha256"]:
            raise ValueError("Group extraction did not reuse the uploaded READ")
        read = read_result(result["read_ref"])
        snapshot = read_validated_result(result["result_ref"], content_root=root)
        response = _response(
            manifest["task_id"], profile_name, job_root.name, result, snapshot, read, manifest
        )
        chain = (
            pipeline_view(
                result,
                read,
                response,
                record,
                route,
                job_root,
                xlsx_sheet=sheet_view,
                xlsx_fields=excel_view([sheet_view])["fields"] if sheet_view else (),
            )
            if route != "preview"
            else None
        )
        if chain and record["kind"] == "body":
            chain["body_measurement"] = body_measurement(read, record, root)
        if (
            remember
            and route in {"docling", "deepseek-group"}
            and chain
            and chain["read"]["state"] in {"success", "partial_success"}
        ):
            minimal = {
                k: prepared[k] for k in ("manifest_path", "request_fingerprint", "intake_context")
            }
            minimal["received"] = {"manifest": manifest}
            remember((minimal, root, record, job_root, result, sheet_name))
        return read, response, chain

    def attachment_chain(self, root, record, job_root, route, detail, *, parts, reader, profile):
        try:
            chain = attachment_view(
                record,
                detail,
                route,
                job_root,
                lambda read: self.auxiliary(
                    "attachment_markdown", root, record["content"], read=read
                ),
                parts=parts,
                reader=reader,
                profile=profile,
            )
        except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError):
            detail["chain_error"] = "markdown_export_failed"
        else:
            detail["chain"] = chain
            detail["fields"] = chain["structured"]["fields"]

    def pdf_view(
        self, prepared, root, record, job_root, route="preview", *, remember=None, supplement=None
    ):
        read, response, chain = self.main_result(
            prepared, root, record, job_root, route, remember=remember, supplement=supplement
        )
        execution = next((item for item in read["executions"] if item.get("ref")), None)
        pages = record["scope"]["page_count"]
        document, blocks = _blocks(execution) if execution else ({}, [])
        info = response.get("pdf_read", {})
        warnings = [
            f"{p['kind']}_pass_failed" for p in info.get("passes", []) if p["status"] != "success"
        ]
        return {
            "type": "pdf",
            "pages": pdf_pages(document, blocks, pages),
            "fields": pdf_fields(response["fields"]),
            "cargo_rows": response.get("cargo_rows", []),
            "packing_rows": pdf_packing_rows(blocks, execution) if execution else [],
            "observations": response.get("document_observations", []),
            "dimensions": response.get("dimension_observations", []),
            "unread_pages": info.get("unread_pages", list(range(1, pages + 1))),
            "pdf_passes": [
                {k: p.get(k) for k in ("kind", "pages", "status", "trigger", "reason")}
                for p in info.get("passes", [])
            ],
            "warnings": list(dict.fromkeys(warnings)),
            "failed": not execution,
            **({"chain": chain} if chain else {}),
        }

    def run(self, key, filename, raw, parse_scope, route="preview"):
        self.set(key, state="running")
        job_root = self.output / key
        root = job_root / "intake"
        suffix = Path(filename).suffix.lower()
        try:
            if suffix == ".eml":
                prepared = run_local_handoff(
                    raw,
                    root,
                    input_format="eml",
                    task_id=key,
                    email_ref_id=key,
                    request_reference="local-web:" + key,
                )
            else:
                prepared = run_local_file_handoff(
                    raw,
                    root,
                    filename=filename,
                    mime=MIMES.get(suffix, "application/octet-stream"),
                    task_id=key,
                )
            manifest = prepared["received"]["manifest"]
            original = next(
                (r for r in manifest["materials"] if r["kind"] == "original_email"), None
            )
            email_sha256 = (
                original["content"]["sha256"] if original and original["content"] else None
            )
            materials, assets, origins, inputs = [], {}, {}, []
            html_previews, inline_images = {}, {}
            seen_cids = set()
            export_incomplete = suffix == ".eml" and any(
                (email.get("subject") or "").startswith("[部分内容未导出]")
                for email in manifest["emails"]
            )
            for received in prepared["received"]["materials"]:
                record = received["record"]
                if record["kind"] == "original_email" or not record["selected_for_parsing"]:
                    continue
                index = len(materials)
                kind = (
                    "body"
                    if record["kind"] == "body"
                    else "file"
                    if suffix != ".eml"
                    else record["kind"]
                )
                fmt = (
                    record["format"]
                    or Path(record["filename"]).suffix.lower().lstrip(".")
                    or "unknown"
                )
                reason = next((issue["code"] for issue in record["issues"]), None)
                content = record["content"]
                skipped = (parse_scope == "body" and kind != "body") or (
                    parse_scope == "attachments" and kind == "body"
                )
                materials.append(
                    {
                        "id": str(index),
                        "filename": record["filename"],
                        "kind": kind,
                        "format": fmt,
                        "state": "skipped" if skipped else "queued",
                        "reason": "skipped_by_scope" if skipped else reason,
                        "pages": (record["scope"] or {}).get("page_count"),
                        "sha256": content["sha256"] if content else None,
                    }
                )
                if content and record["state"] == "ready" and fmt in {"pdf", "png", "jpeg"}:
                    assets[str(index)] = (root, content, fmt)
                if (
                    email_sha256
                    and record["parent_material_id"] == original["material_id"]
                    and record["email_ref_id"] == original["email_ref_id"]
                    and record["role"] == "current"
                    and (mime_path := record["metadata"].get("mime_path"))
                ):
                    origins[str(index)] = {
                        "mime_path": mime_path,
                        "attachment_sha256": content["sha256"] if content else None,
                    }
                    materials[-1]["comparison_eligible"] = (
                        kind in {"inline", "attachment"} and fmt in FORMATS
                    )
                    if (
                        kind == "body"
                        and fmt == "html"
                        and content
                        and not skipped
                        and record["state"] == "ready"
                    ):
                        html_previews[str(index)] = (root, content)
                    if kind in {"inline", "attachment"} and (cid := record.get("cid")):
                        if cid in seen_cids:
                            inline_images.pop(cid, None)
                        elif content and fmt in {"png", "jpeg"} and record["state"] == "ready":
                            inline_images[cid] = (content, fmt)
                        seen_cids.add(cid)
                inputs.append((record, received["content"]))
            self.set(
                key,
                materials=materials,
                assets=assets,
                origins=origins,
                email_sha256=email_sha256,
                html_previews=html_previews,
                inline_images=inline_images,
                metadata=[
                    {
                        key: value
                        for key, value in email.items()
                        if key
                        in {
                            "email_ref_id",
                            "subject",
                            "from_addr",
                            "to_addrs",
                            "cc_addrs",
                            "sent_at",
                            "thread_headers",
                            "source",
                        }
                    }
                    for email in manifest["emails"]
                ]
                if suffix == ".eml"
                else [],
                issues=[issue["code"] for issue in manifest["issues"]],
            )
        except Exception:
            self.set(key, state="failed", reason="intake_failed")
            print(f"Intake {key}: {sys.exc_info()[0].__name__}", file=sys.stderr)
            return

        for index, (record, data) in enumerate(inputs):
            row = materials[index]
            if row["state"] == "skipped":
                continue
            fmt, reason = row["format"], row["reason"]
            if fmt not in FORMATS or record["state"] != "ready":
                unsupported = (
                    reason in {None, "unsupported_format", "unsupported", "conversion_required"}
                    and fmt not in FORMATS
                )
                self.item(
                    key,
                    index,
                    state="unsupported" if unsupported else "failed",
                    reason=reason or "unsupported_format",
                )
                continue
            self.item(key, index, state="running", reason=None)

            def remember_replay(context, item_index=index):
                self.item(key, item_index, replay=context)

            if suffix != ".eml":
                remember_replay = None
            try:
                private_sheet_view = None
                if fmt in {"html", "text"}:
                    detail = text_view(data, fmt, row["kind"] == "body" or suffix != ".eml")
                    if row["id"] in html_previews:
                        detail["mail_preview"] = {
                            "export_incomplete": export_incomplete,
                            "replacement_characters": data.count(b"\xef\xbf\xbd"),
                        }
                    if route != "preview" and row["kind"] == "body":
                        try:
                            if not self.python.is_file():
                                raise FileNotFoundError("Docling runtime")
                            body_read, _, detail["chain"] = self.main_result(
                                prepared, root, record, job_root, route, remember=remember_replay
                            )
                            detail["fields"] = detail["chain"]["structured"]["fields"]
                            email = next(
                                (
                                    item
                                    for item in manifest.get("emails", [])
                                    if item.get("email_ref_id") == record.get("email_ref_id")
                                ),
                                {},
                            )
                            objects = body_read_objects(
                                body_read,
                                record,
                                data.decode("utf-8-sig"),
                                email_sha=email_sha256,
                                subject=email.get("subject", ""),
                            )
                            detail["body_shipments"] = objects["shipments"]
                            detail["packing_rows"] = objects["packing_rows"]
                        except Exception as error:
                            detail["chain_error"] = (
                                "reader_missing"
                                if isinstance(error, FileNotFoundError)
                                else "pipeline_failed"
                            )
                            print(
                                f"Pipeline {key}/{index}: {type(error).__name__}", file=sys.stderr
                            )
                    elif route != "preview":
                        self.attachment_chain(
                            root,
                            record,
                            job_root,
                            route,
                            detail,
                            parts=text_read(detail),
                            reader="HTMLParser" if fmt == "html" else "UTF-8",
                            profile="local-text-attachment",
                        )
                elif not self.python.is_file():
                    self.item(key, index, state="failed", reason="reader_missing")
                    continue
                elif fmt == "pdf":
                    detail = self.pdf_view(
                        prepared,
                        root,
                        record,
                        job_root,
                        route,
                        remember=lambda context, i=index: self.item(key, i, replay=context),
                    )
                elif fmt == "xlsx":
                    value = self.auxiliary("xlsx_values", root, record["content"])
                    sheets = value["sheets"]
                    private_sheet_view = sheets[0] if len(sheets) == 1 else None
                    detail = excel_view(sheets)
                    if route == "deepseek-group" and len(sheets) == 1:
                        try:
                            _, _, detail["chain"] = self.main_result(
                                prepared,
                                root,
                                record,
                                job_root,
                                route,
                                remember=remember_replay,
                                sheet_name=sheets[0]["name"],
                                sheet_view=sheets[0],
                            )
                            if detail["chain"]["read"]["state"] in {"success", "partial_success"}:
                                detail["fields"] = detail["chain"]["structured"]["fields"]
                                detail["packing_rows"] = detail["chain"]["structured"].get(
                                    "packing_rows", []
                                )
                        except (
                            OSError,
                            RuntimeError,
                            ValueError,
                            KeyError,
                            subprocess.SubprocessError,
                        ):
                            detail["chain_error"] = "pipeline_failed"
                    elif route != "preview":
                        self.attachment_chain(
                            root,
                            record,
                            job_root,
                            route,
                            detail,
                            parts=workbook_read(detail),
                            reader="openpyxl",
                            profile="local-xlsx-packing",
                        )
                else:
                    value = self.auxiliary("image_ocr", root, record["content"])
                    if value.get("error"):
                        self.item(key, index, state="failed", reason=value["error"])
                        continue
                    lines = value["lines"]
                    detail = {
                        "type": "image",
                        "lines": lines,
                        "size": value.get("size"),
                        "fields": collect_fields(
                            CARGO,
                            [
                                (line["text"], {"line": n, "box": line["box"]})
                                for n, line in enumerate(lines, 1)
                            ],
                        ),
                    }
                    if not lines:
                        detail["warnings"] = ["ocr_empty"]
                    if route != "preview":
                        self.attachment_chain(
                            root,
                            record,
                            job_root,
                            route,
                            detail,
                            parts=image_read(detail),
                            reader="RapidOCR",
                            profile="local-image-cargo",
                        )
                partial = (
                    detail.get("clipped")
                    or detail.get("unread_pages")
                    or detail.get("warnings")
                    or detail.get("chain_error")
                )
                state = "failed" if detail.get("failed") else "partial" if partial else "ready"
                self.item(
                    key,
                    index,
                    state=state,
                    detail=detail,
                    reason="read_failed" if state == "failed" else None,
                    known=sum(field["status"] == "known" for field in detail["fields"]),
                    **({"sheet_view": private_sheet_view} if private_sheet_view else {}),
                )
            except (TimeoutError, subprocess.TimeoutExpired):
                self.item(key, index, state="failed", reason="timeout")
            except Exception:
                traceback.print_exc(limit=3, file=sys.stderr) if os.getenv(
                    "DOCCASE_DEBUG"
                ) else print(
                    f"Reader {key}/{index} ({fmt}): {sys.exc_info()[0].__name__}", file=sys.stderr
                )
                self.item(key, index, state="failed", reason="read_failed")

        states = [m["state"] for m in materials if m["state"] != "skipped"]
        status = (
            "ready"
            if states and all(s == "ready" for s in states)
            else "partial"
            if any(s in {"ready", "partial"} for s in states)
            else "failed"
        )
        self.set(
            key,
            state=status,
            reason=None if states else "no_selected_materials" if materials else "no_materials",
        )


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, status, payload, content_type, *, csp=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                csp
                or "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
            )
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def json(self, status, payload):
            self.reply(
                status,
                json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def trusted(self):
            host = self.headers.get("Host")
            origin = self.headers.get("Origin")
            valid = host == f"127.0.0.1:{self.server.server_port}" and (
                origin is None or origin == f"http://{host}"
            )
            return valid and self.headers.get("Sec-Fetch-Site") in {None, "none", "same-origin"}

        @staticmethod
        def group_query(query_string):
            try:
                query = parse_qs(query_string, keep_blank_values=True, max_num_fields=MAX_GROUP + 2)
            except ValueError:
                raise ValueError("group_invalid_selection") from None
            ids, current, first = (
                query.get("email", []),
                query.get("current", []),
                query.get("first_inquiry", []),
            )
            if (
                set(query) - {"email", "current", "first_inquiry"}
                or not {"email", "current"} <= set(query)
                or len(current) != 1
                or len(first) > 1
                or not all(re.fullmatch(r"[0-9a-f]{32}", key) for key in [*current, *ids, *first])
                or (first and first[0] not in ids)
            ):
                raise ValueError("group_invalid_selection")
            return current[0], ids, first[0] if first else None

        @staticmethod
        def ocr_route(path):
            return re.fullmatch(r"/api/jobs/([0-9a-f]{32})/materials/(\d{1,3})/ocr/(\d{1,4})", path)

        def ocr_reply(self, match, *, start=False):
            job, item, page = match.groups()
            try:
                result = app.pdf_ocr(job, item, int(page), start=start)
            except LookupError:
                return self.json(404, {"error": "not_found"})
            except ValueError as error:
                return self.json(409, {"error": str(error)})
            return self.json(202 if start else 200, result)

        def do_POST(self):
            if not self.trusted():
                return self.json(403, {"error": "local_only"})
            parsed = urlsplit(self.path)
            if match := self.ocr_route(parsed.path):
                if self.headers.get("X-DocCase-OCR") != "page":
                    return self.json(403, {"error": "local_only"})
                return self.ocr_reply(match, start=True)
            if parsed.path == "/api/group-extraction":
                if self.headers.get("X-DocCase-Group") not in {"deepseek", "minimax"}:
                    return self.json(403, {"error": "local_only"})
                try:
                    current, ids, _ = self.group_query(parsed.query)
                    state = app.start_group_agent(current, ids)
                except LookupError:
                    return self.json(404, {"error": "not_found"})
                except ValueError as error:
                    return self.json(409, {"error": str(error)})
                return self.json(202, {"state": state})
            if self.headers.get("X-DocCase-File") is None:
                return self.json(403, {"error": "local_only"})
            if self.path != "/api/jobs":
                return self.json(404, {"error": "not_found"})
            parse_scope = self.headers.get("X-DocCase-Parse-Scope", "all")
            route = self.headers.get("X-DocCase-Route", "preview")
            route = ROUTE_ALIASES.get(route, route)
            if parse_scope not in PARSE_SCOPES:
                return self.json(400, {"error": "invalid_parse_scope"})
            if route not in ROUTES or (
                route.startswith("deepseek") and app.extraction_agent is None
            ):
                return self.json(400, {"error": "pipeline_route_unavailable"})
            name = unquote(self.headers["X-DocCase-File"]).replace("\\", "/").split("/")[-1]
            name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
            length = self.headers.get("Content-Length", "")
            if (
                not name
                or len(name) > 250
                or not re.fullmatch(r"[0-9]{1,10}", length)
                or int(length) == 0
            ):
                return self.json(400, {"error": "invalid_file"})
            if int(length) > LIMITS.max_input_bytes:
                return self.json(413, {"error": "file_too_large"})
            self.connection.settimeout(15)
            try:
                raw = self.rfile.read(int(length))
            except TimeoutError:
                return self.json(408, {"error": "upload_timeout"})
            if len(raw) != int(length):
                return self.json(400, {"error": "invalid_file"})
            key = app.submit(name, raw, parse_scope, route)
            if not key:
                return self.json(429, {"error": "queue_full"})
            return self.json(202, {"id": key, "metadata": app.summary(key)["metadata"]})

        def do_GET(self):
            if not self.trusted():
                return self.json(403, {"error": "local_only"})
            parsed = urlsplit(self.path)
            path = parsed.path
            if match := self.ocr_route(path):
                return self.ocr_reply(match)
            if path == "/api/health":
                return self.json(
                    200,
                    {
                        "status": "ready",
                        "service": "doccase-workbench",
                        "deepseek": app.extraction_agent is not None,
                        "agent_model": MODEL if app.extraction_agent is not None else None,
                        "group_context": app.group_context_agent is not None,
                    },
                )
            if path == "/api/demo-cases":
                return self.json(200, {"cases": DEMO_CASES})
            if path == "/api/evaluation/latest":
                try:
                    return self.json(200, saved_evaluation())
                except (OSError, KeyError, TypeError, ValueError):
                    return self.json(422, {"error": "evaluation_unavailable"})
            if case := re.fullmatch(r"/api/demo-cases/([a-z-]+)", path):
                try:
                    return self.json(200, {"files": case_files(case[1])})
                except LookupError:
                    return self.json(404, {"error": "not_found"})
            if path in {"/api/group-review", "/api/group-result"}:
                try:
                    current, ids, first = self.group_query(parsed.query)
                    result = app.group_review(current, ids, confirmed_first_email_id=first)
                except LookupError:
                    return self.json(404, {"error": "not_found"})
                except ValueError:
                    return self.json(400, {"error": "group_invalid_selection"})
                return self.json(
                    200, result["structured_result"] if path == "/api/group-result" else result
                )
            if comparison := re.fullmatch(r"/api/jobs/([0-9a-f]{32})/comparison", path):
                try:
                    query = parse_qs(
                        parsed.query, keep_blank_values=True, max_num_fields=LIMITS.max_materials
                    )
                    ids = query.get("attachment", [])
                    if set(query) != {"attachment"} or not all(
                        re.fullmatch(r"\d{1,3}", mid) for mid in ids
                    ):
                        raise ValueError("comparison_invalid_selection")
                    result = app.comparison(comparison[1], ids)
                except LookupError:
                    return self.json(404, {"error": "not_found"})
                except ValueError as error:
                    code = (
                        str(error)
                        if str(error) == "comparison_not_email"
                        else "comparison_invalid_selection"
                    )
                    return self.json(400, {"error": code})
                return self.json(200, result)
            static = {
                "/": ("index.html", "text/html"),
                "/app.css": ("app.css", "text/css"),
                "/app.js": ("app.js", "text/javascript"),
                "/cases.js": ("cases.js", "text/javascript"),
                "/group-review.js": ("group-review.js", "text/javascript"),
                "/pipeline.js": ("pipeline.js", "text/javascript"),
            }
            if path in static:
                name, content_type = static[path]
                return self.reply(
                    200, (STATIC / name).read_bytes(), content_type + "; charset=utf-8"
                )
            match = re.fullmatch(
                r"/api/jobs/([0-9a-f]{32})(?:/materials/(\d{1,3})(?:/(image|original-html|page/\d{1,4}))?)?",
                path,
            )
            if match:
                key, index, asset = match.groups()
                if index is None:
                    if summary := app.summary(key):
                        return self.json(200, summary)
                else:
                    if asset == "original-html":
                        try:
                            rendered = app.original_html(key, index)
                        except (OSError, ValueError):
                            return self.json(422, {"error": "preview_unavailable"})
                        if rendered is not None:
                            return self.reply(
                                200, rendered, "text/html; charset=utf-8", csp=PREVIEW_CSP
                            )
                        return self.json(404, {"error": "not_found"})
                    with app.lock:
                        job = app.jobs.get(key, {})
                        data = job.get("assets" if asset else "details", {}).get(index)
                    if data and not asset:
                        return self.json(200, data)
                    if data and asset:
                        root, content, fmt = data
                        try:
                            if asset == "image" and fmt in {"png", "jpeg"}:
                                raw = p02.verified_content(root, content)
                                return self.reply(200, raw, f"image/{fmt}")
                            if asset.startswith("page/") and fmt == "pdf":
                                result = app.auxiliary(
                                    "pdf_preview", root, content, page=int(asset[5:])
                                )
                                if "png" in result:
                                    return self.reply(
                                        200, base64.b64decode(result["png"]), "image/png"
                                    )
                        except (OSError, ValueError, subprocess.SubprocessError):
                            return self.json(422, {"error": "preview_unavailable"})
            return self.json(404, {"error": "not_found"})

    return Handler


class LoopbackServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18770)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "output/local-workbench",
    )
    parser.add_argument("--docling-python", type=Path, default=p02.DOCLING_PYTHON)
    parser.add_argument("--model-cache", type=Path, default=p02.MODEL_CACHE)
    parser.add_argument(
        "--extraction-agent", choices=("deepseek",), help="Enable explicit DeepSeek imports"
    )
    parser.add_argument(
        "--group-context-fallback",
        action="store_true",
        help="Allow one group context pass after material extraction for unresolved fields",
    )
    args = parser.parse_args()
    if args.group_context_fallback and not args.extraction_agent:
        parser.error("--group-context-fallback requires --extraction-agent deepseek")
    agent = None
    if args.extraction_agent:
        try:
            agent = load_deepseek_agent()
        except ValueError as error:
            parser.error(str(error))
    app = Workbench(
        args.output,
        args.docling_python,
        args.model_cache,
        extraction_agent=agent,
        group_context_fallback=args.group_context_fallback,
    )
    with LoopbackServer(("127.0.0.1", args.port), make_handler(app)) as server:
        print(f"http://127.0.0.1:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            app.pool.shutdown(cancel_futures=True)
            app.group_pool.shutdown(cancel_futures=True)


if __name__ == "__main__":
    main()
