"""Project one verified main-channel result into the local pipeline view."""

import hashlib
import json
import re
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from deepseek_extraction import MODEL, MODEL_NAME, THINKING

from doccase_preprocessing.cargo_table import agent_evidence_spans, packing_rows
from doccase_preprocessing.main_channel import _blocks

ROUTES = {"preview", "docling", "deepseek", "deepseek-group"}
ROUTE_ALIASES = {"minimax": "deepseek", "minimax-group": "deepseek-group"}
MAX_ARTIFACT = 16 * 1024 * 1024


def _local(path, root):
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(Path(root).resolve()) or resolved.stat().st_size > MAX_ARTIFACT:
        raise ValueError("Invalid pipeline artifact")
    return resolved


def _json(path, root):
    return json.loads(_local(path, root).read_bytes())


def _verified(ref, root):
    raw = _local(ref["ref"], root).read_bytes()
    if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
        raise ValueError("Invalid pipeline artifact digest")
    return raw


def saved_markdown(result, root, material_id):
    path = _local(result["result_ref"]["ref"], root)
    progress = _json(path.with_name("progress.json"), root)
    if (
        progress.get("result_ref") != result["result_ref"]
        or progress.get("read", {}).get("result_ref") != result["read_ref"]
    ):
        raise ValueError("Markdown progress not bound to READ")
    if not progress.get("markdown"):
        return {"state": "failure", "reason": "markdown_unavailable"}
    return json.loads(_verified(progress["markdown"], root))["materials"][material_id]


def _markdown_view(sidecar, root, mid, execution):
    if sidecar.get("state") not in {"success", "partial_success"}:
        return {
            "state": sidecar.get("state", "failure"),
            "reason": sidecar.get("reason", "markdown_unavailable"),
        }
    refs = {
        k: {"ref": sidecar[f"{k}_ref"], "sha256": sidecar[f"{k}_sha256"]}
        for k in ("markdown", "index")
    }
    markdown = _verified(refs["markdown"], root).decode("utf-8")
    index = json.loads(_verified(refs["index"], root))
    if (index.get("material_id"), index.get("read_sha256"), index.get("execution_id")) != (
        mid,
        execution.get("artifact_hash"),
        execution.get("execution_id"),
    ):
        raise ValueError("Markdown not bound to selected material and READ")
    return {
        "state": sidecar["state"],
        "text": markdown[:180_000],
        "truncated": len(markdown) > 180_000,
        "fragment_count": len(index.get("fragments", [])),
        "limitations": dict(
            Counter(item["reason"] for item in index.get("limitations", []) if item.get("reason"))
        ),
        "sha256": sidecar["markdown_sha256"],
    }


def _xlsx_source_locations(execution, root, sheet):
    """Bind READ cells to verified visible workbook cells, or leave them unlocated."""
    if not sheet:
        return {}
    profile = execution.get("executed_profile", {})
    if profile.get("reader") != "DoclingExcel" or profile.get("requested_scope", {}).get(
        "sheet_names"
    ) != [sheet.get("name")]:
        return {}
    try:
        document = json.loads(
            _verified({"ref": execution["ref"], "sha256": execution["artifact_hash"]}, root)
        )
        groups = {
            g["self_ref"]
            for g in document.get("groups", [])
            if g.get("label") == "sheet" and g.get("name") == sheet["name"]
        }
        if len(groups) != 1:
            return {}
        rows = {row["number"]: row["cells"] for row in sheet["rows"]}
        if len(rows) != len(sheet["rows"]):
            return {}
        columns = sheet["columns"]
        locations = {}
        for table_index, table in enumerate(document.get("tables", [])):
            if table.get("parent", {}).get("$ref") not in groups:
                continue
            prov = table.get("prov", [])
            if len(prov) != 1 or prov[0].get("page_no") != 1:
                continue
            box = prov[0].get("bbox", {})
            if box.get("coord_origin") != "TOPLEFT":
                continue
            coords = [box.get(k) for k in ("l", "t", "r", "b")]
            if any(type(v) not in (int, float) or v != int(v) for v in coords):
                continue
            left, top, right, bottom = map(int, coords)
            data = table["data"]
            if (
                left < 0
                or top < 0
                or right <= left
                or bottom <= top
                or right - left != data["num_cols"]
                or bottom - top != data["num_rows"]
            ):
                continue
            for cell_index, cell in enumerate(data["table_cells"]):
                offsets = [
                    cell.get(k)
                    for k in (
                        "start_row_offset_idx",
                        "end_row_offset_idx",
                        "start_col_offset_idx",
                        "end_col_offset_idx",
                    )
                ]
                if any(type(v) is not int for v in offsets):
                    continue
                sr, er, sc, ec = offsets
                if not (0 <= sr < er <= bottom - top and 0 <= sc < ec <= right - left):
                    continue
                if left + sc >= len(columns):
                    continue
                column = columns[left + sc]
                row = top + sr + 1
                if er != sr + 1 or ec != sc + 1:
                    if not any(
                        (m["row"], m["row_end"], m["col"], m["col_end"])
                        == (row, top + er, left + sc + 1, left + ec)
                        for m in sheet.get("merges", [])
                    ):
                        continue
                native = rows.get(row, {}).get(column)
                text = cell.get("text")
                if (
                    not native
                    or native.get("clipped")
                    or not isinstance(text, str)
                    or text.strip() != str(native.get("value", "")).strip()
                ):
                    continue
                pointer = f"/tables/{table_index}/data/table_cells/{cell_index}/text"
                locations[pointer] = {"sheet": sheet["name"], "cell": f"{column}{row}"}
        return locations
    except (OSError, ValueError, OverflowError, KeyError, TypeError):
        return {}


def _xlsx_packing_rows(execution, locations, sheet):
    """Project only READ row facts whose complete proof matches workbook cells."""
    if not locations:
        return []
    native = {
        (sheet["name"], f"{column}{row['number']}"): cell
        for row in sheet["rows"]
        for column, cell in row["cells"].items()
    }
    rows = []
    for row in packing_rows(_blocks(execution)[1], execution):
        if row["selector"].get("worksheet") != sheet["name"]:
            continue
        facts = row["facts"]
        identity = facts.get("product_name")
        if not identity or not all(b["pointer"] in locations for b in identity[2]):
            continue
        fields = {}
        for name, (value, unit, proof) in facts.items():
            if name == "ctns":
                unit = "carton"
            sources = [
                {"pointer": b["pointer"], "excerpt": b["text"], **locations[b["pointer"]]}
                for b in proof
                if b["pointer"] in locations
            ]
            verified = len(sources) == len(proof)
            if name == "line_volume" and verified and sources:
                source = sources[0]
                cell = native.get((source["sheet"], source["cell"]), {})
                fmt = re.fullmatch(r"0\.(0{1,6})(?:_\s)?\s*", str(cell.get("number_format", "")))
                if fmt:
                    try:
                        rounded = Decimal(str(value)).quantize(
                            Decimal(1).scaleb(-len(fmt[1])), rounding=ROUND_HALF_UP
                        )
                        if rounded > 0:
                            value = float(rounded)
                    except InvalidOperation:
                        pass
            fields[name] = {
                "value": value if verified else None,
                "unit": unit,
                "status": "known" if verified else "uncertain",
                "reason": None if verified else "target_cell_unverified",
                "sources": sources,
            }
            if name == "item_quantity":
                fields[name]["score_policy"] = "requires_independent_gold"
        for name in (
            "ctns",
            "item_quantity",
            "line_net_weight",
            "line_gross_weight",
            "line_volume",
        ):
            if name not in fields:
                fields[name] = {
                    "value": None,
                    "unit": None,
                    "status": "uncertain",
                    "reason": "header_or_value_unavailable",
                    "sources": [],
                }
                if name == "item_quantity":
                    fields[name]["score_policy"] = "requires_independent_gold"
        rows.append(
            {
                **row["selector"],
                "ordinal": row["ordinal"],
                "fields": fields,
                "evidence_spans": agent_evidence_spans(
                    row, lambda block: locations.get(block["pointer"])
                ),
            }
        )
    return rows


def pipeline_view(result, read, response, record, route, root, *, xlsx_sheet=None, xlsx_fields=()):
    mid = record["material_id"]
    inventory = next(r for r in read["inventory"] if r["material_id"] == mid)
    execution = next(
        (r for r in read["executions"] if r.get("execution_id") == inventory.get("execution_id")),
        {},
    )
    result_path = _local(result["result_ref"]["ref"], root)
    progress = _json(result_path.with_name("progress.json"), root)
    if progress.get("result_ref") != result["result_ref"]:
        raise ValueError("Pipeline progress does not match selected result")
    sidecars = (
        json.loads(_verified(progress["markdown"], root))["materials"]
        if progress.get("markdown")
        else {}
    )
    events = (
        json.loads(_verified(progress["agent"], root))["events"] if progress.get("agent") else []
    )
    matching = [e for e in events if e.get("material_id") == mid]
    accepted = {e["requirement_id"] for e in matching if e.get("outcome") == "accepted"}
    xlsx_locations = (
        _xlsx_source_locations(execution, root, xlsx_sheet) if record["format"] == "xlsx" else {}
    )
    fields = []
    for field in response["fields"]:
        evidence = []
        for entry in field["sources"]:
            source = entry["source"]
            if source["material_id"] != mid:
                continue
            loc = source["locator"]
            evidence.append(
                {
                    "excerpt": source["excerpt"],
                    "page": None if record["format"] == "xlsx" else loc.get("page"),
                    "pointer": loc.get("json_pointer"),
                    "start": loc.get("char_start"),
                    "end": loc.get("char_end"),
                    "kind": source["kind"],
                    **xlsx_locations.get(loc.get("json_pointer"), {}),
                }
            )
        name = field["field_name"]
        fields.append(
            {
                "name": name,
                "value": field["value"],
                "unit": field["unit"],
                "status": field["status"],
                "reason": next((i["code"] for i in field["issues"]), None),
                "sources": evidence,
                **{key: field[key] for key in ("quantity_scope", "quantity_kind") if key in field},
                "via": "agent"
                if f"REQ-{name.upper()}" in accepted
                else "rules"
                if evidence
                else None,
            }
        )
    if xlsx_sheet and xlsx_locations:
        # The workbook preview recognizes product quantities outside the main
        # profile. Surface them only when every source cell is also in this READ.
        by_cell = {}
        for pointer, location in xlsx_locations.items():
            by_cell.setdefault((location["sheet"], location["cell"]), []).append(pointer)
        native = {
            (xlsx_sheet["name"], f"{column}{row['number']}"): cell["value"]
            for row in xlsx_sheet["rows"]
            for column, cell in row["cells"].items()
            if not cell.get("clipped")
        }
        for field in xlsx_fields:
            if (
                field["name"] != "item_quantity"
                or field["status"] != "known"
                or not field["sources"]
            ):
                continue
            evidence = []
            for source in field["sources"]:
                location = (source.get("sheet"), source.get("cell"))
                pointers = by_cell.get(location, [])
                if (
                    len(pointers) != 1
                    or location not in native
                    or str(native[location]).strip() != source["excerpt"].strip()
                ):
                    break
                evidence.append({**source, "pointer": pointers[0], "kind": "attachment"})
            else:
                fields.append({**field, "sources": evidence, "via": "rules"})
    return {
        "route": route,
        "preprocessing": {
            "material_id": mid,
            "filename": record["filename"],
            "kind": record["kind"],
            "format": record["format"],
            "state": record["state"],
            "sha256": record["content"]["sha256"],
            "mime_path": record.get("metadata", {}).get("mime_path"),
        },
        "read": {
            "state": inventory["read_state"],
            "reason": inventory.get("reason"),
            "reader": execution.get("executed_profile", {}).get("reader"),
            "sha256": execution.get("artifact_hash"),
            "unread_pages": execution.get("result", {}).get("unread_pages", []),
        },
        "markdown": _markdown_view(sidecars.get(mid, {}), root, mid, execution),
        "agent": {
            "enabled": route == "deepseek",
            "model": MODEL if route == "deepseek" else None,
            "model_name": MODEL_NAME if route == "deepseek" else None,
            "requested_effort": None,
            "thinking": THINKING if route == "deepseek" else None,
            "adapter_requested": any(e.get("request_sha256") for e in matching),
            "events": [
                {
                    k: e.get(k)
                    for k in (
                        "requirement_id",
                        "stage",
                        "outcome",
                        "reason",
                        "reason_detail",
                        "field_name",
                        "evidence_pointers",
                        "representation",
                        "representations",
                        "requested_fields",
                        "proposed",
                        "rejected_shape",
                        "markdown_fragments",
                        "read_fragments",
                        "read_context_added",
                        "read_fallback_reason",
                    )
                }
                for e in matching
            ],
        },
        "structured": {
            "state": response["status"],
            "profile": response["profile_id"],
            "fields": fields,
            **(
                {"packing_rows": _xlsx_packing_rows(execution, xlsx_locations, xlsx_sheet)}
                if record["format"] == "xlsx" and xlsx_sheet
                else {}
            ),
        },
    }
