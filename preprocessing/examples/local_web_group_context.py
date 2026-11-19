"""Opt-in second pass for missing workbench shipment fields on saved READ."""

import hashlib
import re
from collections import defaultdict
from time import perf_counter

from local_web_agent_context import _PRIVATE
from local_web_pipeline import _local, saved_markdown

from doccase_preprocessing.agent_gap_evidence import split_cargo_gap_evidence
from doccase_preprocessing.extraction_agent import (
    AgentServiceError,
    current_body_text_check,
    mapped_markdown_fragments,
)
from doccase_preprocessing.group_context_agent import (
    context_packet,
    first_inquiry_evidence,
    merge_context_response,
    quoted_message_id,
)
from doccase_preprocessing.group_context_proofs import BOILER, PDF_FIELDS, same_read_proof
from doccase_preprocessing.inquiry_details import agent_body_detail_proofs, inquiry_body_details
from doccase_preprocessing.local_handoff import _read_content
from doccase_preprocessing.main_channel import (
    _attachment_document_totals,
    _blocks,
    _current_body_blocks,
    _quoted_offset,
    read_result,
)

CONTEXT_TIMEOUT = 25
MAX_CONTEXT_CHARS = 18000
MAX_FRAGMENTS = 144
FIELDS = {
    "package_type": (
        None,
        re.compile(
            r"(?<![a-z])(?:wooden cases?|crates?|pallets?|cartons?|packages?|pkgs?)(?![a-z])|木箱|托盘|纸箱|包装",
            re.I,
        ),
    ),
    "line_volume": ("CBM", re.compile(r"(?<![a-z])(?:cbm|m3|volume)(?![a-z])|m³|立方|体积", re.I)),
    "business_remarks": (
        None,
        re.compile(
            r"\b(?:account|release|charges?|split|batter(?:y|ies)|stackable)\b|放货|分批|带电|叠放|承担",
            re.I,
        ),
    ),
    "shipment_gross_weight": ("kg", re.compile(r"\b(?:gross weight|gw|g\.w\.?)(?!\w)|毛重", re.I)),
}
_SIGNOFF = re.compile(
    r"^(?:best(?: regards)?|kind regards|regards|sincerely|saludos|thanks(?: and regards)?|此致|顺祝商祺)[ ,.!。]*$",
    re.I,
)
_PERSONAL = re.compile(
    r"^(?:dear|hello|hi|hola|from|to|cc|subject|sent|contact|mobile|fax|tel\.?|phone|"
    r"发件人|收件人|联系人|抄送|发件时间)(?:\b|[:：])|"
    r"\b(?:street|avenue|address|postal code|zip code|invoice no|tax id)\b|地址|邮编|电话|传真|手机",
    re.I,
)
_BUSINESS_CONTEXT = re.compile(
    r"\b(?:cargo details|total|packing list|shipper|consignee|delivery|shipment|net weight|nw|"
    r"gross weight|weight|chargeable|dimensions?|incoterms?)\b|货物|合计|总计|装箱|重量|尺寸",
    re.I,
)
_CONTACT_FIELDS = {"customer_company", "contact_name", "contact_phone", "signature_email"}


def missing_fields(inputs, baseline, overrides):
    present = set()
    for email_id, material_id, kind, replay, _ in inputs:
        fields = overrides.get((email_id, material_id), baseline.get((email_id, material_id), ()))
        document_totals = replay[2]["format"] == "pdf" or replay[5] is not None
        for field in fields:
            if field["status"] != "conflict" and not (
                field["status"] == "known" and field.get("value") is not None
            ):
                continue
            name = field["name"]
            if kind == "body":
                present.add(name)
            elif document_totals and name in {"line_gross_weight", "line_volume"}:
                # These profiles expose document totals, never entries from cargo_rows.
                present.add("shipment_gross_weight" if name == "line_gross_weight" else name)
    return [field for field in FIELDS if field not in present]


def _open_saved(kind, replay):
    _, root, record, job_root, baseline, _ = replay
    mid = record["material_id"]
    _local(baseline["read_ref"]["ref"], job_root)
    read = read_result(baseline["read_ref"])
    selected = next(r for r in read["inventory"] if r["material_id"] == mid)
    execution = next(e for e in read["executions"] if e["execution_id"] == selected["execution_id"])
    _local(execution["ref"], job_root)
    document, blocks = _blocks(execution)
    ocr_prefixes = (
        tuple(
            f"/parts/{i}/document/"
            for i, part in enumerate(document.get("parts", ()))
            if part["kind"] == "ocr"
        )
        if document.get("kind") == "docling_pdf_read_bundle"
        else ()
    )
    raw = (
        _read_content(root.resolve(), record["content"]).decode("utf-8-sig")
        if kind == "body"
        else None
    )
    mapped, bad_markdown = defaultdict(list), False
    try:
        sidecar = saved_markdown(baseline, job_root, mid)
        if sidecar.get("state") in {"success", "partial_success"}:
            for key in ("markdown_ref", "index_ref"):
                _local(sidecar[key], job_root)
        for f, _ in mapped_markdown_fragments(
            sidecar,
            execution,
            mid,
            source_role="current" if raw is not None else "attachment",
            strict=True,
        ):
            mapped[f["read_pointer"]].append((f["read_start"], f["read_end"]))
    except (OSError, ValueError, KeyError, TypeError):
        mapped.clear()
        bad_markdown = True
    return {
        "row": record,
        "execution": execution,
        "blocks": blocks,
        "raw": raw,
        "ocr_prefixes": ocr_prefixes,
        "mapped": mapped,
        "bad_markdown": bad_markdown,
    }


def _body_lines(o, missing):
    blocks, raw = o["blocks"], o["raw"]
    current = current_body_text_check(raw, allow_quoted_duplicate=True)
    scoped = list(_current_body_blocks(blocks))
    private = {
        value
        for finder in (inquiry_body_details, agent_body_detail_proofs)
        for name, (value, _) in finder(scoped, raw).items()
        if name in _CONTACT_FIELDS and isinstance(value, str) and len(value.strip()) > 2
    }
    volume_proofs = {
        (block["pointer"], start, end)
        for name, _, _, proof in split_cargo_gap_evidence(
            [block for block, end in scoped if end == len(block["text"])], current
        )
        if name in missing
        for block, start, end in proof
    }
    lines = []

    def selected_lines():
        window = {(line[0]["pointer"], line[1], line[2]) for line in _body_window(lines)}
        window.update(volume_proofs)
        return [line for line in lines if line and (line[0]["pointer"], line[1], line[2]) in window]

    for block, end in scoped:
        for m in re.finditer(r"[^\r\n]+", block["text"][:end]):
            value = m[0].strip()
            if _SIGNOFF.fullmatch(value):
                return selected_lines()
            start = m.start() + len(m[0]) - len(m[0].lstrip())
            if (
                not value
                or len(value) > 360
                or not current(value)
                or _PRIVATE.search(value)
                or _PERSONAL.search(value)
                or BOILER.search(value)
            ):
                lines.append(None)
                continue
            fields = {field for field in missing if FIELDS[field][1].search(value)}
            masked = value
            for token in sorted(private, key=lambda t: (-len(t), t)):
                masked = re.sub(
                    rf"(?<!\w){re.escape(token)}(?!\w)", "[redacted]", masked, flags=re.I
                )
            lines.append((block, start, start + len(value), masked, fields))
    return selected_lines()


def _body_window(lines):
    leads = [i for i, line in enumerate(lines) if line and line[4]]
    keep = set(leads)
    for i in leads:
        for j in (i - 1, i + 1):
            if 0 <= j < len(lines) and lines[j] and _BUSINESS_CONTEXT.search(lines[j][3]):
                keep.add(j)
    return [lines[i] for i in sorted(keep)]


def _attachment_lines(o, missing):
    # Include only anchored packing totals and supporting headers from one document.
    mid = o["row"]["material_id"]
    reader = o["execution"]["executed_profile"]["reader"]
    fragments = {}
    for field in missing:
        if field not in {"line_volume", "shipment_gross_weight"}:
            continue
        req = {
            "field_name": PDF_FIELDS.get(field, field),
            "source_kind": "attachment",
            "object_selector": mid + "/document",
            "material_id": mid,
            "pdf_table_total": True,
        }
        for _, unit, evidence in _attachment_document_totals(o["blocks"], req, reader):
            if unit != FIELDS[field][0]:
                continue
            if any(
                not b["text"].strip()
                or len(b["text"]) > 220
                or _PRIVATE.search(b["text"])
                or _PERSONAL.search(b["text"])
                or BOILER.search(b["text"])
                for b in evidence
            ):
                continue
            for index, b in enumerate(evidence):
                row = fragments.setdefault(b["pointer"], (b, 0, len(b["text"]), b["text"], set()))
                if index == 0:
                    row[4].add(field)
    return list(fragments.values())


def build_context(inputs, missing):
    sources, opened, quotes, by_field, problems = [], {}, [], defaultdict(set), []
    remaining = MAX_CONTEXT_CHARS
    for number, (email_id, material_id, kind, replay, _) in enumerate(inputs, 1):
        if len(sources) >= MAX_FRAGMENTS or remaining <= 0:
            break
        source = f"M{number}"
        try:
            o = _open_saved(kind, replay)
            if o["bad_markdown"]:
                problems.append(
                    {
                        "email_id": email_id,
                        "material_id": material_id,
                        "reason": "markdown_unverified",
                    }
                )
            if o["raw"] is not None:
                blocks = o["blocks"]
                for i, block in enumerate(blocks):
                    if (offset := _quoted_offset(blocks, i)) is None:
                        continue
                    quote = [block["text"][offset:], *(b["text"] for b in blocks[i + 1 : i + 12])]
                    quotes.append(
                        {
                            "email_id": email_id,
                            "material_id": material_id,
                            "pointer": block["pointer"],
                            "quote_sha256": hashlib.sha256("\n".join(quote).encode()).hexdigest(),
                            "message_id": quoted_message_id(quote),
                        }
                    )
                    break
                lines = _body_lines(o, missing)
            else:
                lines = _attachment_lines(o, missing)
        except (OSError, ValueError, KeyError, TypeError, StopIteration):
            problems.append(
                {"email_id": email_id, "material_id": material_id, "reason": "read_unverified"}
            )
            continue
        # Do not send half of a context window or let a large document fill the packet.
        if not lines or len(lines) > min(36, MAX_FRAGMENTS - len(sources)):
            if lines:
                problems.append(
                    {
                        "email_id": email_id,
                        "material_id": material_id,
                        "reason": "context_over_budget",
                    }
                )
            continue
        size = sum(len(line[3]) for line in lines)
        if size > remaining:
            problems.append(
                {"email_id": email_id, "material_id": material_id, "reason": "context_over_budget"}
            )
            continue
        remaining -= size
        opened[source] = {**o, "email_id": email_id, "material_id": material_id}
        for block, start, end, excerpt, fields in lines:
            pointer = block["pointer"]
            pages = sorted(
                {p["page_no"] for p in block["provenance"] if type(p.get("page_no")) is int}
            )
            is_ocr = block.get("pdf_pass") == "ocr" or pointer.startswith(o["ocr_prefixes"])
            indexed = any(a <= start < end <= z for a, z in o["mapped"][pointer])
            representation = (
                ("ocr_markdown" if is_ocr else "docling_markdown")
                if indexed
                else ("ocr_read" if is_ocr else "read")
            )
            ref = {"pointer": pointer, "start": start, "end": end, "pages": pages}
            if cell := block.get("cell"):
                ref["cell"] = {
                    "row": cell["start_row_offset_idx"] + 1,
                    "last_row": cell["end_row_offset_idx"],
                    "column": cell["start_col_offset_idx"] + 1,
                    "last_column": cell["end_col_offset_idx"],
                }
            sources.append(
                {
                    "id": f"E{len(sources) + 1}",
                    "source": source,
                    "scope": source,
                    "role": "supplement",
                    "kind": kind,
                    "ref": ref,
                    "representation": representation,
                    "text": excerpt,
                }
            )
            for field in fields:
                by_field[field].add(source)
    needs = [
        {
            "id": name,
            "name": name,
            "role": "supplement",
            "object": "shipment",
            "unit": FIELDS[name][0],
            "sources": by_field[name],
        }
        for name in missing
    ]
    history = first_inquiry_evidence(None, quotes)
    return (
        context_packet(needs, sources, max_chars=MAX_CONTEXT_CHARS),
        opened,
        {
            "materials": problems,
            "historical_candidates": [
                {k: item[k] for k in ("email_id", "material_id", "pointer", "status")}
                for item in history["historical_evidence"]
            ],
        },
    )


def run_context(inputs, missing, adapter, *, budget=True):
    result = {
        "state": "no_gap",
        "requests": 0,
        "model_seconds": None,
        "supplements": [],
        "rejections": [],
    }
    if not missing:
        return result
    if not budget:
        return {**result, "state": "request_limit", "missing": missing}
    packet, opened, audit = build_context(inputs, missing)
    result.update(audit, missing=missing)
    if not packet:
        return {**result, "state": "no_evidence"}
    refs = {source["id"]: source for source in packet["sources"]}
    result.update(
        requests=1, fragments=len(refs), requested=[r["name"] for r in packet["requirements"]]
    )
    start = perf_counter()
    try:
        response = adapter(packet, timeout_seconds=CONTEXT_TIMEOUT)
    except TimeoutError:
        return {**result, "state": "timeout", "model_seconds": round(perf_counter() - start, 3)}
    except AgentServiceError:
        return {
            **result,
            "state": "service_failure",
            "model_seconds": round(perf_counter() - start, 3),
        }
    result["model_seconds"] = round(perf_counter() - start, 3)
    updates, _, rejected = merge_context_response(
        {}, packet, response, lambda req, value, cited: same_read_proof(req, value, cited, opened)
    )
    for field, item in updates.items():
        o = opened[item["source"]]
        proof = {}
        # Keep the cited value span, plus locally verified labels/headers where available.
        for e in [*(refs[i] for i in item["cited"]), *refs.values()]:
            pointer = e["ref"]["pointer"]
            if e["source"] == item["source"] and pointer in item["locators"]:
                proof.setdefault(pointer, e)
        result["supplements"].append(
            {
                "name": field,
                "value": item["value"],
                "unit": item["unit"],
                "role": "supplement",
                "email_id": o["email_id"],
                "material_id": o["material_id"],
                "sources": [
                    {
                        "pointer": e["ref"]["pointer"],
                        "start": e["ref"]["start"],
                        "end": e["ref"]["end"],
                        "page": next(iter(e["ref"]["pages"]), None),
                        "excerpt": e["text"],
                        "representation": e["representation"],
                        **({"table_cell": e["ref"]["cell"]} if "cell" in e["ref"] else {}),
                    }
                    for e in proof.values()
                ],
            }
        )
    result["rejections"] = [
        {"reason": e["reason"], **({"name": e["id"]} if e.get("id") in missing else {})}
        for e in rejected
    ]
    result["state"] = (
        "invalid_response"
        if any(e["reason"] in {"invalid_json", "invalid_shape"} for e in rejected)
        else "complete"
    )
    return result
