"""Local authoritative READ checks for the opt-in group context pass."""

import re

from .agent_gap_evidence import (
    compact_gap_evidence,
    shipment_summary_evidence,
    split_cargo_gap_evidence,
)
from .cargo_table import packing_rows
from .extraction_agent import current_body_text_check
from .inquiry_details import RESPONSIBILITY, agent_body_detail_proofs
from .main_channel import (
    _attachment_document_totals,
    _current_body_blocks,
    _normalize_material_value,
)

PDF_FIELDS = {
    "shipment_gross_weight": "line_gross_weight",
    "shipment_net_weight": "line_net_weight",
}
NUMBER = r"[0-9]+(?:[.,][0-9]+)?"
BOILER = re.compile(
    r"quote based on|rates? based on|报价适用|quote is valid|"
    r"due to heavy congestion|transit times may|standard trading conditions|confidential",
    re.I,
)
UNSAFE_SCOPE = re.compile(
    r"\b(?:per|each|if|unless|alternative|tentative|previous|additional)\b|"
    r"每箱|每托|如果|备选|原先|加单|预计|未确认",
    re.I,
)


def eq(a, b):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) < 1e-8
    if isinstance(a, str) and isinstance(b, str):
        return " ".join(a.casefold().strip(" .。").split()) == " ".join(
            b.casefold().strip(" .。").split()
        )
    return False


def number(v):
    if type(v) in (int, float):
        return v
    if isinstance(v, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", v.strip()):
        return float(v)
    return None


def same_read_proof(req, proposed, cited, opened):
    field = req["name"]
    if field == "package_type" and req["role"] != "historical":
        # Match the first-pass package contract; the cited READ still has to prove the type.
        v = _normalize_material_value(field, proposed, "package_field")
    else:
        v = number(proposed) if req["unit"] or field in {"ctns", "package_count"} else proposed
    if v is None:
        return None
    for source in sorted({e["source"] for e in cited}):
        o = opened[source]
        row, blocks, raw = o["row"], o["blocks"], o["raw"]
        by_pointer = {b["pointer"]: b for b in blocks}
        quoted = [
            (e, by_pointer[e["ref"]["pointer"]]["text"][e["ref"]["start"] : e["ref"]["end"]])
            for e in cited
            if e["source"] == source
        ]
        ptrs = {e["ref"]["pointer"] for e, _ in quoted}
        proofs = []
        if req["role"] == "historical":
            # Source-bound observation only. It does not prove first-inquiry
            # identity, nor is it used in the primary or supplementary output.
            for e, text in quoted:
                if (
                    isinstance(v, str)
                    and 2 <= len(v.strip()) <= 50
                    and re.search(rf"(?<!\w){re.escape(v.strip())}(?!\w)", text, re.I)
                ):
                    return {
                        "value": v.strip(),
                        "unit": None,
                        "source": source,
                        "locators": [e["ref"]["pointer"]],
                    }
            return None
        if req["object"].startswith("cargo-row-"):
            target = req.get("selector")
            if not target:
                continue  # Row ordinal alone is never a proof of page and table.
            for r in packing_rows(blocks, o["execution"]):
                if r["selector"] == target and field in r["facts"]:
                    value, _, ev = r["facts"][field]
                    proofs.append((value, ev[0]["pointer"], [b["pointer"] for b in ev]))
        elif raw is None:
            requirement = {
                "field_name": PDF_FIELDS.get(field, field),
                "source_kind": "attachment",
                "object_selector": row["material_id"] + "/document",
                "material_id": row["material_id"],
                "pdf_table_total": True,
            }
            for value, _, ev in _attachment_document_totals(
                blocks, requirement, o["execution"]["executed_profile"]["reader"]
            ):
                # A proof must include the value cell, even if labels/title are completed locally.
                proofs.append((value, ev[0]["pointer"], [b["pointer"] for b in ev]))
        else:
            scoped = list(_current_body_blocks(blocks))
            current = current_body_text_check(raw, allow_quoted_duplicate=True)
            for name, value, unit, evidence in shipment_summary_evidence(scoped, current):
                anchor, start, end = evidence[0]
                if (
                    name == field
                    and unit == req["unit"]
                    and any(
                        e["ref"]["pointer"] == anchor["pointer"]
                        and e["ref"]["start"] <= start
                        and e["ref"]["end"] >= end
                        for e, _ in quoted
                    )
                ):
                    proofs.append(
                        (value, anchor["pointer"], [b["pointer"] for b, _, _ in evidence])
                    )
            if field == "line_volume":
                for name, value, unit, evidence in split_cargo_gap_evidence(
                    [block for block, end in scoped if end == len(block["text"])], current
                ):
                    allowed = {(block["pointer"], start, end) for block, start, end in evidence}
                    anchor = evidence[-1][0]["pointer"]
                    if (
                        name == field
                        and unit == req["unit"]
                        and anchor in ptrs
                        and all(
                            (e["ref"]["pointer"], e["ref"]["start"], e["ref"]["end"]) in allowed
                            for e, _ in quoted
                        )
                    ):
                        proofs.append(
                            (value, anchor, [block["pointer"] for block, _, _ in evidence])
                        )
            if field == "cargo_ready_time":
                if found := agent_body_detail_proofs(scoped, raw).get(field):
                    value, ev = found
                    proofs.append((value, ev[0][0]["pointer"], [b["pointer"] for b, _, _ in ev]))
            if field == "trade_terms":
                for b, end in scoped:
                    for name, value, _, _, _ in compact_gap_evidence(b["text"][:end], current):
                        if name == field:
                            proofs.append((value, b["pointer"], [b["pointer"]]))
            for e, text in quoted:
                if not current(text) or UNSAFE_SCOPE.search(text) or BOILER.search(text):
                    continue
                ptr = e["ref"]["pointer"]
                if field == "business_remarks" and RESPONSIBILITY.fullmatch(text):
                    proofs.append((text, ptr, [ptr]))
                if field == "shipment_gross_weight" and (
                    m := re.fullmatch(
                        r"(?:total gross weight|总毛重)\s*[,，:：]\s*([0-9]+(?:\.[0-9]+|,[0-9]{1,2})?)\s*kgs?\.?",
                        text,
                        re.I,
                    )
                ):
                    proofs.append((float(m[1].replace(",", ".")), ptr, [ptr]))
                if field == "shipment_chargeable_weight" and (
                    m := re.search(
                        rf"(?:chargeable weight|计费重)\s*[:：]\s*({NUMBER})\s*kgs?\b",
                        text,
                        re.I,
                    )
                ):
                    proofs.append((float(m[1].replace(",", ".")), ptr, [ptr]))
        for value, anchor, pointers in proofs:
            if anchor in ptrs and eq(v, value):
                return {
                    "value": value,
                    "unit": req["unit"],
                    "source": source,
                    "locators": list(dict.fromkeys(pointers)),
                }
    return None
