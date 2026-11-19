"""Narrow bridge from saved material snapshots to a reviewed EmailBridge PoC job."""

import hashlib
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .main_channel import _facts
from .main_channel import read_result as read_local_result
from .output_snapshot import read_result

_ALLOWED = {
    "requirements[0].destination_port": ("inquiries.0.fields.destination", "str"),
    "requirements[0].trade_term": ("inquiries.0.fields.incoterm", "str"),
    "requirements[0].cargo_ready_time": ("inquiries.0.fields.ready", "date"),
    "requirements[0].package_type": ("inquiries.0.cargo.0.package_unit", "package"),
    "requirements[0].ctns": ("inquiries.0.totals.package_count", "integer"),
    "requirements[0].gross_weight": ("inquiries.0.totals.gross_kg", "number"),
    "requirements[0].volume": ("inquiries.0.totals.cbm", "number"),
    "requirements[0].cargo_lines[0].product_name": ("inquiries.0.fields.commodity", "str"),
    "requirements[0].cargo_lines[0].ctns": ("inquiries.0.cargo.0.package_count", "integer"),
}

_FACT_FIELDS = {
    "requirements[0].ctns": "ctns",
    "requirements[0].cargo_lines[0].ctns": "ctns",
    "requirements[0].gross_weight": "line_gross_weight",
    "requirements[0].volume": "line_volume",
}

_PACKAGE_EVIDENCE = {
    "carton": re.compile(r"(?<![A-Za-z])(?:cartons?|ctns?)(?![A-Za-z])|箱", re.I),
    "pallet": re.compile(r"(?<![A-Za-z])pallets?(?![A-Za-z])|托盘", re.I),
}


def material_sources(material_store, result_id, revision):
    """Expose verified full TextReader representations, preserving occurrence bindings."""
    snapshot = read_result(material_store, result_id, revision)
    handoff = snapshot["material_handoff"]
    if handoff["handoff_state"] != "complete":
        raise ValueError("EmailBridge local bridge requires complete declared material scope")
    read = read_local_result(snapshot["read_result_ref"])
    executions = {
        item["execution_id"]: item for item in read["executions"] if item.get("execution_id")
    }
    inventory = {item["material_id"]: item for item in read["inventory"]}
    sources, bindings = [], []
    for material in handoff["material_versions"]:
        mid = material["material_id"]
        item = inventory[mid]
        execution = executions.get(item["execution_id"])
        if (
            not execution
            or item["read_state"] != "success"
            or execution["executed_profile"]["reader"] != "TextReader"
        ):
            raise ValueError(
                "EmailBridge local bridge currently requires complete TextReader input"
            )
        artifact = next(
            a for a in handoff["artifact_refs"] if a["execution_id"] == item["execution_id"]
        )
        text = json.loads(Path(artifact["ref"]).read_text(encoding="utf-8"))["text"]
        sources.append({"id": mid, "kind": "text", "name": mid, "text": text})
        bindings.append(
            {
                "source_id": mid,
                "material_id": mid,
                "material_revision": material["material_revision"],
                "representation_sha256": material["representation_ref"]["sha256"],
                "artifact_id": artifact["artifact_id"],
                "artifact_sha256": artifact["artifact_hash"],
                "email_ref_id": material["source_email_ref_id"],
                "source_role": material["source_role"],
            }
        )
    return snapshot, sources, bindings


def _lookup(value, path):
    for part in path.split("."):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def _convert(value, kind):
    if kind in {"str", "date", "package"}:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Reviewed string value is missing")
        if kind == "date":
            date.fromisoformat(value)
        if kind == "package" and value not in {"carton", "pallet"}:
            raise ValueError("Reviewed package type is not supported by this draft mapping")
        return value
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError):
        raise ValueError("Qualified or nonnumeric value cannot be silently projected") from None
    if not number.is_finite() or number <= 0:
        raise ValueError("Reviewed numeric value must be positive and finite")
    if kind == "integer":
        if number != number.to_integral_value():
            raise ValueError("Reviewed count is not an integer")
        return int(number)
    converted = float(number)
    if Decimal(str(converted)) != number:
        raise ValueError("Reviewed number cannot be represented without changing its value")
    return converted


def _supported_by_quotes(target, value, quotes):
    """Check direct lexical support, leaving assignment and meaning to the reviewer."""
    if target in _FACT_FIELDS:
        return any(
            observed == value
            for quote in quotes
            for _, observed, _ in _facts(_FACT_FIELDS[target], quote)
        )
    if target == "requirements[0].package_type":
        return any(_PACKAGE_EVIDENCE[value].search(quote) for quote in quotes)
    normalized = " ".join(value.casefold().split())
    return any(normalized in " ".join(quote.casefold().split()) for quote in quotes)


def reviewed_business_decision(
    job,
    *,
    material_store,
    result_id,
    revision,
    reviewer,
    confirmed_intent,
    mappings,
    agent_evidence,
):
    """Translate a confirmed PoC job only with an explicit supported review mapping."""
    snapshot, sources, bindings = material_sources(material_store, result_id, revision)
    if (
        job.get("status") != "confirmed"
        or not job.get("confirmed_at")
        or job.get("sources") != sources
        or not isinstance(reviewer, str)
        or not reviewer.strip()
        or confirmed_intent != "inquiry"
        or any(issue.get("level") in {"error", "warning"} for issue in job.get("issues", []))
    ):
        raise ValueError(
            "A confirmed EmailBridge job without unresolved review issues and bound to the material sources is required"
        )
    result = job["result"]
    if len(result["inquiries"]) != 1 or result["notes"]:
        raise ValueError("This bridge requires one inquiry without unrepresented notes")
    inquiry = result["inquiries"][0]
    if len(inquiry["cargo"]) != 1 or inquiry["weight_breaks"]:
        raise ValueError("This bridge requires one cargo line without weight breaks")
    if (
        not isinstance(mappings, dict)
        or not mappings
        or any(
            target not in _ALLOWED or source != _ALLOWED[target][0]
            for target, source in mappings.items()
        )
    ):
        raise ValueError("Review mapping contains an unsupported field or semantic scope")
    present = set()
    for group in ("fields", "totals", "cargo", "weight_breaks"):
        rows = inquiry[group] if group in {"cargo", "weight_breaks"} else [inquiry[group]]
        for index, row in enumerate(rows):
            for name, cell in row.items():
                if cell["value"] is not None:
                    prefix = f"inquiries.0.{group}." + (
                        f"{index}." if group in {"cargo", "weight_breaks"} else ""
                    )
                    present.add(prefix + name)
    if set(mappings.values()) != present:
        raise ValueError("Every nonempty PoC business field must be explicitly mapped")
    by_source = {source["id"]: source for source in sources}
    req = {"cargo_lines": [{}]}
    applicability = []
    for target, source_path in mappings.items():
        cell = _lookup(result, source_path)
        source = by_source.get(cell["source_id"])
        if (
            cell["provenance"] != "extracted"
            or not source
            or not cell["quote"]
            or cell["quote"] not in source["text"]
        ):
            raise ValueError("Mapped business field lacks exact extracted source evidence")
        for extra in cell.get("evidence", []):
            extra_source = by_source.get(extra.get("source_id"))
            if (
                not extra_source
                or not extra.get("quote")
                or extra["quote"] not in extra_source["text"]
            ):
                raise ValueError("Supplementary business evidence is not present in its source")
        value = _convert(cell["value"], _ALLOWED[target][1])
        quotes = [cell["quote"], *(extra["quote"] for extra in cell.get("evidence", []))]
        if not _supported_by_quotes(target, value, quotes):
            raise ValueError(f"Business field {target} is not supported by its cited evidence")
        target_name = target.rsplit(".", 1)[1]
        target_record = req["cargo_lines"][0] if ".cargo_lines[0]." in target else req
        target_record[target_name] = {"value": value, "confidence": None}
        applicability.append({"path": target, "state": "known"})
    raw = Path(agent_evidence["ref"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != agent_evidence["sha256"]:
        raise ValueError("EmailBridge review evidence SHA-256 mismatch")
    evidence = json.loads(raw)
    if (
        evidence.get("job") != job
        or evidence.get("source_bindings") != bindings
        or evidence.get("mappings") != mappings
        or evidence.get("reviewer") != reviewer
        or evidence.get("confirmed_intent") != confirmed_intent
    ):
        raise ValueError("EmailBridge review evidence does not match the confirmed inputs")
    return {
        "agent_id": "EmailBridge-PoC",
        "decision_ref": "emailbridge:" + job["id"] + ":" + str(job["revision"]),
        "task_id": snapshot["material_handoff"]["task_id"],
        "confirmed_at": job["confirmed_at"],
        "intent": {"confirmed": True, "value": confirmed_intent},
        "inquiry_assignment": {"confirmed": True, "value": [{"inquiry": {"requirements": [req]}}]},
        "field_applicability": {"confirmed": True, "value": applicability},
        "agent_evidence": agent_evidence,
    }
