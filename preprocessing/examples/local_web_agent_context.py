"""Attach bounded context to unresolved workbench body fields on the same READ."""

import json
import re

from doccase_preprocessing.agent_gap_evidence import split_cargo_gap_evidence
from doccase_preprocessing.extraction_agent import current_body_text_check
from doccase_preprocessing.material_agent import material_evidence
from doccase_preprocessing.semantic_context import SEMANTIC_CONTEXT_FIELDS, semantic_context_spans

_PRIVATE = re.compile(
    r"https?://|www\.|@|[$€¥￥]|\b(?:password|token|api[ -]?key|bank account|iban|swift)\b"
    r"|\b(?:USD|CNY|RMB|EUR)\s*[\d,.]+|密码|验证码|银行账号"
    r"|\+?\d(?:[(). \-]*\d){6,}",
    re.I,
)
_MULTI_SHIPMENT_FIELDS = frozenset(
    {
        "cargo_origin",
        "pickup_city",
        "departure_port",
        "destination",
        "trade_terms",
        "cargo_type",
        "package_count",
        "package_type",
        "ctns",
        "shipment_gross_weight",
        "shipment_net_weight",
        "shipment_chargeable_weight",
        "line_gross_weight",
        "line_net_weight",
        "line_volume",
        "cargo_ready_time",
        "business_remarks",
    }
)
_CURRENCY = re.compile(r"\b(?:USD|CNY|RMB|EUR)\b", re.I)
_FIELD_CUES = {
    name: re.compile(pattern, re.I)
    for name, pattern in {
        "package_count": r"(?<![a-z])(?:packages?|pkgs?)\b|包装件数",
        "ctns": r"(?<![a-z])(?:cartons?|ctns?)\b|纸箱|箱数",
        "package_type": r"(?<![a-z])(?:cartons?|ctns?|pallets?|plts?)\b|\bwooden\s+(?:cases?|crates?)\b|纸箱|托盘|木箱",
        "line_volume": r"(?<![a-z])(?:cbm|m3)\b|\b(?:volume|cubage|volumen)\b|m³|体积|立方",
        "cargo_origin": r"\b(?:cargo origin|ship from|place of origin)\b|货源地|发货地",
        "pickup_city": r"\b(?:pick[ -]?up|collection)\s+(?:city|location|address)\s*[:：]\s*\S+|提货城市\s*[:：]\s*\S+",
        "departure_port": r"\b(?:AOL|POL|origin airport|departure (?:airport|port))\b|起运港|起运机场",
        "destination": r"\b(?:destination|destino|AOD|POD|port of discharge)\b|\bfrom\s+[a-z][a-z .,'-]{1,60}\s+to\s+[a-z]|\bto\s+(?-i:[A-Z]{3})\b|目的地|目的港",
        "trade_terms": r"\b(?:incoterms?|EXW|FCA|CPT|CIP|DAP|DPU|DDP|FAS|FOB|CFR|CIF)\b|贸易条款",
        "cargo_ready_time": r"\b(?:cargo ready|ready date|ready for pickup)\b|货好|备妥",
        "cargo_type": r"\b(?:cargo type|general cargo|dangerous goods|carga general)\b|货物类型|普货|危险品",
        "cargo_description": r"\b(?:commodity|cargo description|description of goods)\b|货物品名",
        "business_remarks": r"\b(?:remarks?|release|partial shipment|split shipment|batter(?:y|ies)|shipper account)\b|备注|放货|分批|带电",
        "customer_company": r"\b(?:customer company|company name)\b|客户公司",
        "contact_name": r"\bcontact (?:person|name)\b|联系人",
        "contact_phone": r"\bcontact (?:phone|telephone|tel)\b|联系电话",
        "signature_email": r"\bsignature e-?mail\b|签名邮箱",
        "salesperson": r"\b(?:salesperson|sales representative)\b|业务员",
        "internal_reference": r"\b(?:internal reference|our reference)\b|需求单号",
        "shipment_net_weight": r"\b(?:net weight|n\.?\s*w\.?)\b|净重",
        "shipment_gross_weight": r"\b(?:gross weight|g\.?\s*w\.?)\b|毛重",
        "shipment_chargeable_weight": r"\b(?:chargeable weight|c\.?\s*w\.?)\b|计费重",
        "line_net_weight": r"\b(?:net weight|n\.?\s*w\.?)\b|净重",
        "line_gross_weight": r"\b(?:gross weight|g\.?\s*w\.?)\b|毛重",
        "unit_net_weight": r"\b(?:unit|per (?:piece|item))\b.*\bnet\b|单件净重",
        "unit_gross_weight": r"\b(?:unit|per (?:piece|item))\b.*\bgross\b|单件毛重",
    }.items()
}
_TOTAL = re.compile(r"\b(?:grand\s+)?total\b|合计|总计", re.I)


def approved_model_fragment(text):
    return (
        isinstance(text, str)
        and 0 < len(text) <= 240
        and text.count("\n") <= 2
        and not _PRIVATE.search(text)
        and not _CURRENCY.search(text)
    )


def multi_shipment_material_request(request, baseline_fields):
    """Keep global contact gaps in the old adapter; shipment gaps need an object request."""
    settled = {field["name"] for field in baseline_fields if field.get("status") != "uncertain"}
    requirements = [
        r
        for r in request["requirements"]
        if r["field_name"] not in settled and r["field_name"] not in _MULTI_SHIPMENT_FIELDS
    ]
    evidence = [item for item in request["evidence"] if approved_model_fragment(item.get("text"))]
    return {**request, "requirements": requirements, "evidence": evidence}


def natural_gap_material_request(request, baseline_fields, *, multi_shipment=False):
    """Send only unresolved fields with their field-specific, source-bound evidence."""
    settled = {
        f["name"]
        for f in baseline_fields
        if f.get("status") != "uncertain" or f.get("value") is not None
    }
    gaps = {
        f["name"]
        for f in baseline_fields
        if f.get("status") == "uncertain" and f.get("value") is None
    } - settled
    if multi_shipment:
        gaps -= _MULTI_SHIPMENT_FIELDS
    safe = [e for e in request["evidence"] if approved_model_fragment(e.get("text"))]
    requirements, selected = [], set()
    for requirement in request["requirements"]:
        name = requirement["field_name"]
        cue = _FIELD_CUES.get(name)
        if name not in gaps or cue is None:
            continue
        anchors = [e for e in safe if cue.search(e["text"])]
        if not anchors:
            continue
        context = [e for e in safe if name in e.get("context_for", {})]
        changes = [e for e in anchors if re.search(r"\bfrom\b.+\bto\b", e["text"], re.I)]
        if name == "package_count" and changes:
            chosen = changes
        elif context and any(e in context for e in anchors):
            chosen = context
        elif any(e.get("cell") for e in anchors):
            chosen = []
            for anchor in anchors:
                cell = anchor.get("cell")
                if not cell:
                    continue
                labels = [
                    e
                    for e in safe
                    if e.get("cell", {}).get("table") == cell["table"] and _TOTAL.search(e["text"])
                ]
                values = [
                    e
                    for e in safe
                    if e.get("cell", {}).get("table") == cell["table"]
                    and e["cell"].get("column") == cell["column"]
                    and any(e["cell"].get("row") == label["cell"].get("row") for label in labels)
                ]
                if values:
                    chosen.extend([anchor, *labels, *values])
        else:
            chosen = anchors
        if not chosen:
            continue
        requirements.append(requirement)
        selected.update(e["evidence_id"] for e in chosen)
    names = {r["field_name"] for r in requirements}
    evidence = []
    for item in safe:
        if item["evidence_id"] not in selected:
            continue
        item = dict(item)
        if "context_for" in item:
            item["context_for"] = {k: v for k, v in item["context_for"].items() if k in names}
            if not item["context_for"]:
                del item["context_for"]
        evidence.append(item)
    result = {**request, "requirements": requirements, "evidence": evidence}
    if "source_index" in request:
        result["source_index"] = {eid: request["source_index"][eid] for eid in selected}
    return result


def restrict_material_response(response, request):
    """Reject candidates for fields or evidence that were not sent to the Agent."""
    try:
        parsed = json.loads(response) if isinstance(response, str) else response
    except (TypeError, ValueError):
        return response, [], []
    if not isinstance(parsed, dict) or not isinstance(parsed.get("candidates"), list):
        return response, [], []
    sent = {item["evidence_id"] for item in request["evidence"]}
    wanted = {item["requirement_id"] for item in request["requirements"]}
    kept, unsent_requirements, unsent_evidence = [], [], []
    for item in parsed["candidates"]:
        if (
            isinstance(item, dict)
            and isinstance(item.get("requirement_id"), str)
            and item["requirement_id"] not in wanted
        ):
            unsent_requirements.append(item["requirement_id"])
        elif (
            isinstance(item, dict)
            and isinstance(item.get("evidence_ids"), list)
            and any(eid not in sent for eid in item["evidence_ids"] if isinstance(eid, str))
        ):
            unsent_evidence.append(item.get("requirement_id"))
        else:
            kept.append(item)
    return {**parsed, "candidates": kept}, unsent_requirements, unsent_evidence


def context_gaps(fields):
    unresolved = {f["name"] for f in fields if f["status"] == "uncertain"}
    settled = {f["name"] for f in fields if f["status"] != "uncertain"}
    return SEMANTIC_CONTEXT_FIELDS & (unresolved - settled)


def workbench_material_evidence(gaps, sidecar, execution, material_id, blocks, role, original):
    evidence, bindings = material_evidence(sidecar, execution, material_id, blocks, role, original)
    if not gaps or role != "current" or original is None:
        return evidence, bindings

    current = current_body_text_check(original, allow_quoted_duplicate=True)
    windows = semantic_context_spans(blocks, current, lambda s: not _PRIVATE.search(s), gaps)
    by_span = {}
    chars = sum(len(e["text"]) for e in evidence)
    for item in evidence:
        block, start, end, _ = bindings[item["evidence_id"]]
        if item["text"] == block["text"][start:end]:
            by_span.setdefault((block["pointer"], start, end), item)
    for field, window in windows.items():
        if len(window) < 2:
            continue
        spans = [(b["pointer"], a, z) for b, a, z, _ in window]
        additions = [s for s in spans if s not in by_span]
        if (
            len(evidence) + len(additions) > 128
            or chars + sum(z - a for _, a, z in additions) > 16000
        ):
            continue
        for position, (span, (block, start, end, snippet)) in enumerate(
            zip(spans, window, strict=True), 1
        ):
            if span not in by_span:
                eid = f"E{len(evidence) + 1}"
                item = {"evidence_id": eid, "text": snippet, "representation": "read"}
                if cell := block.get("cell"):
                    item["cell"] = {
                        "table": block["pointer"].split("/table_cells/", 1)[0],
                        "row": cell["start_row_offset_idx"] + 1,
                        "column": cell["start_col_offset_idx"] + 1,
                    }
                by_span[span] = item
                evidence.append(item)
                bindings[eid] = (block, start, end, "read")
                chars += len(snippet)
            by_span[span].setdefault("context_for", {})[field] = position
    if "line_volume" in gaps:
        proofs = list(split_cargo_gap_evidence(blocks, current))
        if len(proofs) == 1:
            proof = proofs[0][3]
            keys = {(block["pointer"], start, end) for block, start, end in proof}
            for item in evidence:
                block, start, end, _ = bindings[item["evidence_id"]]
                if (block["pointer"], start, end) not in keys and "context_for" in item:
                    item["context_for"].pop("line_volume", None)
                    if not item["context_for"]:
                        del item["context_for"]
            for position, (block, start, end) in enumerate(proof, 1):
                if item := by_span.get((block["pointer"], start, end)):
                    item.setdefault("context_for", {}).setdefault("line_volume", position)
    return evidence, bindings
