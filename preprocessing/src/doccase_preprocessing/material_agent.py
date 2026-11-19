"""Bounded evidence and candidate contract for one material's extraction call."""

import json
import math
import re
from collections import defaultdict

from .agent_gap_evidence import (
    compact_cargo_context,
    compact_gap_evidence,
    group_declaration_evidence,
    quote_table_gap_evidence,
    shipment_summary_evidence,
    split_cargo_gap_evidence,
)
from .extraction_agent import AgentResponseError, current_body_text_check, mapped_markdown_fragments
from .inquiry_details import agent_body_detail_proofs, declared_shipment_terms
from .pdf_profile import PDF_READERS

_CUE = re.compile(
    r"total|gross|net weight|chargeable|volume|cbm|ctns?|cartons?|pallet|package|"
    r"destination|origin|port|airport|\b(?:FOB|FCA|EXW|DAP|DDP|CIF|CFR|CNF)\b|"
    r"product|length|width|height|dimensions|ready|contact|company|email|phone|"
    r"毛重|净重|体积|箱数|件数|目的地|起运地|联系人",
    re.I,
)
_TOTAL = re.compile(r"(?:^|\b)(?:total|grand total)\b|总计|合计", re.I)


def material_evidence(sidecar, execution, material_id, blocks, role, original=None):
    """Prefer indexed Markdown fragments. Use only verified in-scope READ for gaps."""
    by_pointer = {block["pointer"]: block for block in blocks}
    indexed = defaultdict(list)
    for fragment, rendered in mapped_markdown_fragments(
        sidecar or {},
        execution,
        material_id,
        source_role=role,
        strict=True,
    ):
        block = by_pointer.get(fragment["read_pointer"])
        if block is not None and fragment["read_end"] <= len(block["text"]):
            indexed[fragment["read_pointer"]].append((fragment, rendered))
    current = current_body_text_check(original) if original is not None else None
    current_prefix = (
        current_body_text_check(original, allow_quoted_duplicate=True)
        if original is not None and role == "current"
        else None
    )
    structured_pointers = (
        {
            b["pointer"]
            for f in (quote_table_gap_evidence, split_cargo_gap_evidence)
            for _, _, _, proof in f(blocks, current_prefix)
            for b, _, _ in proof
        }
        if current_prefix
        else set()
    )
    detail_spans = defaultdict(set)
    if current_prefix:
        for _, _, _, proof in shipment_summary_evidence(
            ((b, len(b["text"])) for b in blocks), current_prefix
        ):
            for b, start, end in proof:
                detail_spans[b["pointer"]].add((start, end))
        for _, _, _, proof in group_declaration_evidence(blocks, current_prefix):
            for b, start, end in proof:
                detail_spans[b["pointer"]].add((start, end))
        for name, (_, proof) in agent_body_detail_proofs(
            ((b, len(b["text"])) for b in blocks), original
        ).items():
            for b, start, end in (
                proof if name in {"cargo_ready_time", "business_remarks"} else proof[:1]
            ):
                detail_spans[b["pointer"]].add((start, end))
    weight = [2 * bool(_CUE.search(b["text"])) + 3 * bool(_TOTAL.search(b["text"])) for b in blocks]
    order = sorted(
        range(len(blocks)),
        key=lambda i: (
            blocks[i]["pointer"] not in structured_pointers,
            -max(weight[max(0, i - 1) : i + 2]),
            i,
        ),
    )
    selected, prefix_fallback, chars = [], [], 0
    for i in order:
        block = blocks[i]
        pointer, text = block["pointer"], block["text"]
        choices = [
            (fragment["read_start"], fragment["read_end"], rendered, "markdown")
            for fragment, rendered in indexed.get(pointer, [])
        ]
        if current_prefix and not block.get("cell"):
            scoped_spans = set(detail_spans.get(pointer, ()))
            scoped_spans.update((a, b) for a, b, _ in declared_shipment_terms(text, current_prefix))
            scoped_spans.update(
                (a, b) for _, _, _, a, b in compact_gap_evidence(text, current_prefix)
            )
            for start, end in sorted(scoped_spans, reverse=True):
                if not any((left, right) == (start, end) for left, right, _, _ in choices):
                    choices.insert(0, (start, end, text[start:end], "read"))
        if not any(start == 0 and end == len(text) for start, end, _, _ in choices):
            if pointer in structured_pointers or current is None or current(text):
                choices.append((0, len(text), text, "read"))
            elif current_prefix and len(text) <= 500 and _CUE.search(text) and current_prefix(text):
                prefix_fallback.append((i, 0, len(text), text, "read"))
        for start, end, rendered, representation in choices:
            if not rendered.strip() or len(rendered) > 500 or chars + len(rendered) > 16000:
                continue
            selected.append((i, start, end, rendered, representation))
            chars += len(rendered)
            if len(selected) >= 128:
                break
        if len(selected) >= 128:
            break
    for fragment in prefix_fallback:
        if len(selected) >= 128:
            break
        if chars + len(fragment[3]) <= 16000:
            selected.append(fragment)
            chars += len(fragment[3])
    selected.sort()
    # Add context after existing evidence IDs so previously bound exact spans
    # remain stable; never add an unbounded paragraph or displace core evidence.
    if current_prefix:
        registered = {(i, a, b) for i, a, b, _, _ in selected}
        contexts = 0
        for i, start, end, _, _ in list(selected):
            if contexts == 8 or len(selected) >= 128:
                break
            block = blocks[i]
            text = block["text"]
            if block.get("cell") or not any(
                f in {"package_type", "line_volume"} and (a, b) == (start, end)
                for f, _, _, a, b in compact_gap_evidence(text, current_prefix)
            ):
                continue
            if (window := compact_cargo_context(text, start, end, current_prefix)) is None:
                continue
            left, right = window
            if (i, left, right) in registered or chars + right - left > 16000:
                continue
            selected.append((i, left, right, text[left:right], "read"))
            registered.add((i, left, right))
            chars += right - left
            contexts += 1
    evidence, bindings = [], {}
    pdf = execution.get("executed_profile", {}).get("reader") in PDF_READERS
    for i, start, end, text, representation in selected:
        block = blocks[i]
        eid = f"E{len(evidence) + 1}"
        item = {"evidence_id": eid, "text": text, "representation": representation}
        provenance = block.get("provenance", [])
        if len(provenance) == 1 and provenance[0].get("page_no"):
            item["page"] = provenance[0]["page_no"]
            box = provenance[0].get("bbox")
            if (
                pdf
                and isinstance(box, dict)
                and all(
                    type(box.get(key)) in {int, float} and math.isfinite(box[key])
                    for key in ("l", "t")
                )
            ):
                item.update(column_x=round(box["l"], 1), row_y=round(box["t"], 1))
        if block.get("inline_group"):
            item["group"] = block["inline_group"]
        if block.get("cell"):
            table = block["pointer"].split("/table_cells/", 1)[0]
            cell = block["cell"]
            item["cell"] = {
                "table": table,
                "row": cell["start_row_offset_idx"] + 1,
                "column": cell["start_col_offset_idx"] + 1,
            }
        evidence.append(item)
        bindings[eid] = (block, start, end, representation)
    return evidence, bindings


def material_candidates(response, requirement_ids, evidence_ids):
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except json.JSONDecodeError as error:
            raise AgentResponseError("invalid_json") from error
    if (
        not isinstance(response, dict)
        or set(response) != {"candidates"}
        or not isinstance(response["candidates"], list)
    ):
        raise AgentResponseError("invalid_response_shape")
    result = defaultdict(list)
    rejected, unbound = 0, []
    for item in response["candidates"][: 3 * len(requirement_ids)]:
        if not isinstance(item, dict) or set(item) != {"requirement_id", "value", "evidence_ids"}:
            rejected += 1
            continue
        rid, ids = item["requirement_id"], item["evidence_ids"]
        if (
            not isinstance(rid, str)
            or rid not in requirement_ids
            or not isinstance(ids, list)
            or any(not isinstance(x, str) for x in ids)
            or len(result[rid]) >= 3
        ):
            rejected += 1
            continue
        if not 1 <= len(ids) <= 8 or len(set(ids)) != len(ids) or not set(ids) <= evidence_ids:
            unbound.append(rid)
            continue
        result[rid].append(item)
    return result, rejected, unbound
