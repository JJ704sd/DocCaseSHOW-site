"""Conservative local preview against the user-selected V1.0 interface draft."""

import hashlib
import json
import math
from pathlib import Path
from uuid import uuid4

from .business_result import _read, _write_new, read_business_result
from .output_snapshot import SnapshotValidationError, _encode, _path, read_result

_FIELDS = {
    "inquiry": {
        "customer_company": "str",
        "contact_name": "str",
        "contact_phone": "str",
        "signature_email": "str",
        "cargo_type": "str",
        "trade_term": "str",
        "is_bulk": "bool",
        "density": "number",
        "location": "str",
        "destination": "str",
        "remark": "str",
    },
    "requirement": {
        "destination_port": "str",
        "cargo_type": "str",
        "package_type": "str",
        "gross_weight": "number",
        "volume": "number",
        "ctns": "integer",
        "density": "number",
        "location": "str",
        "trade_term": "str",
        "cargo_ready_time": "str",
        "remark": "str",
    },
    "cargo_line": {
        "product_name": "str",
        "ctns": "integer",
        "length_cm": "number",
        "width_cm": "number",
        "height_cm": "number",
        "unit_gross_weight": "number",
        "unit_volume": "number",
    },
}


class Unrepresentable(ValueError):
    def __init__(self, code, detail):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _interface(reference):
    raw = Path(reference["ref"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
        raise SnapshotValidationError("V1.0 interface document SHA-256 mismatch")
    document = json.loads(raw)["data"]["document"]
    if document["document_id"] != "synthetic-interface-document" or document["revision_id"] != 1:
        raise SnapshotValidationError(
            "This draft adapter requires synthetic interface document revision 1"
        )
    return document


def _leaf(path, wrapper, kind, statuses, seen, missing):
    if (
        not isinstance(wrapper, dict)
        or "value" not in wrapper
        or set(wrapper) - {"value", "confidence"}
    ):
        raise Unrepresentable("field_wrapper_unrepresentable", path)
    status = statuses.get(path)
    seen.add(path)
    value = wrapper["value"]
    if status == "missing" and value is None:
        missing.append(path)
    elif status != "known" or value is None:
        raise Unrepresentable("field_state_unrepresentable", path)
    if value is not None:
        valid = (
            kind == "str"
            and isinstance(value, str)
            or kind == "bool"
            and isinstance(value, bool)
            or kind == "integer"
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
            or kind == "number"
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
        )
        if not valid:
            raise Unrepresentable("field_value_unrepresentable", path)
    confidence = wrapper.get("confidence")
    if confidence is not None and (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        raise Unrepresentable("confidence_unrepresentable", path)


def _inquiry(inquiry, applicability):
    if not isinstance(inquiry, dict):
        raise Unrepresentable("inquiry_shape_unrepresentable", "inquiry")
    statuses = {}
    for item in applicability:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "state"}
            or not isinstance(item["path"], str)
            or not isinstance(item["state"], str)
            or item["path"] in statuses
        ):
            raise Unrepresentable("applicability_unrepresentable", "field_applicability")
        statuses[item["path"]] = item["state"]
    seen, missing = set(), []

    def fields(value, kind, prefix, children=()):
        if not isinstance(value, dict) or set(value) - (set(_FIELDS[kind]) | set(children)):
            raise Unrepresentable("unsupported_business_field", prefix or "inquiry")
        for name, wrapper in value.items():
            if name in children:
                continue
            path = f"{prefix}.{name}" if prefix else name
            _leaf(path, wrapper, _FIELDS[kind][name], statuses, seen, missing)
            if wrapper["value"] is not None and (
                name == "cargo_type"
                and wrapper["value"] not in {"general", "dangerous", "non_dangerous"}
                or name == "package_type"
                and wrapper["value"] not in {"carton", "pallet"}
            ):
                raise Unrepresentable("enum_value_unrepresentable", path)

    fields(inquiry, "inquiry", "", ("requirements",))
    requirements = inquiry.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise Unrepresentable("requirements_unrepresentable", "inquiry.requirements")
    for ri, requirement in enumerate(requirements):
        prefix = f"requirements[{ri}]"
        fields(requirement, "requirement", prefix, ("cargo_lines",))
        lines = requirement.get("cargo_lines", [])
        if not isinstance(lines, list):
            raise Unrepresentable("cargo_lines_unrepresentable", prefix)
        for ci, line in enumerate(lines):
            fields(line, "cargo_line", f"{prefix}.cargo_lines[{ci}]")
    if seen != set(statuses):
        raise Unrepresentable("applicability_scope_mismatch", "field_applicability")
    return missing


def _build_wire(business):
    decision = business["agent_decision"]
    supported = {
        "agent_id",
        "decision_ref",
        "task_id",
        "confirmed_at",
        "intent",
        "inquiry_assignment",
        "field_applicability",
        "overall_confidence",
        "matched",
        "suggestions",
        "agent_evidence",
    }
    if set(decision) - supported:
        raise Unrepresentable(
            "unsupported_business_fields", ", ".join(sorted(set(decision) - supported))
        )
    intent = decision["intent"]["value"]
    assignments = decision["inquiry_assignment"]["value"]
    applicability = decision["field_applicability"]["value"]
    if len(decision["task_id"]) > 64:
        raise Unrepresentable("task_id_unrepresentable", "task_id")
    if intent not in {"inquiry", "other"}:
        raise Unrepresentable("intent_unrepresentable", str(intent))
    if len(assignments) > 1:
        raise Unrepresentable("multiple_inquiries_unrepresentable", str(len(assignments)))
    if intent == "inquiry":
        if (
            len(assignments) != 1
            or not isinstance(assignments[0], dict)
            or set(assignments[0]) != {"inquiry"}
        ):
            raise Unrepresentable("inquiry_assignment_unrepresentable", "inquiry_assignment")
        inquiry = assignments[0]["inquiry"]
        missing = _inquiry(inquiry, applicability)
    else:
        if assignments or applicability:
            raise Unrepresentable("non_inquiry_data_unrepresentable", "inquiry_assignment")
        inquiry, missing = None, []
    payload = {
        "task_id": decision["task_id"],
        "result": {"state": "succeeded"},
        "mail_type": intent,
        "inquiry": inquiry,
        "missing_fields": missing,
    }
    overall = decision.get("overall_confidence")
    if overall is not None:
        if (
            isinstance(overall, bool)
            or not isinstance(overall, (int, float))
            or not math.isfinite(overall)
            or not 0 <= overall <= 1
        ):
            raise Unrepresentable("confidence_unrepresentable", "overall_confidence")
        payload["overall_confidence"] = overall
    matched = decision.get("matched")
    if matched is not None:
        if (
            intent != "inquiry"
            or not isinstance(matched, dict)
            or set(matched) - {"matched", "matched_by", "inquiry_no", "version", "candidates"}
            or not isinstance(matched.get("matched"), bool)
            or matched.get("matched_by") not in {"thread", "ai", "none"}
        ):
            raise Unrepresentable("matched_unrepresentable", "matched")
        if matched["matched"] and (
            not matched.get("inquiry_no")
            or not isinstance(matched.get("version"), int)
            or isinstance(matched["version"], bool)
            or matched["version"] < 1
            or matched["matched_by"] == "none"
        ):
            raise Unrepresentable("matched_unrepresentable", "matched")
        if not matched["matched"] and (
            matched["matched_by"] != "none"
            or matched.get("inquiry_no") is not None
            or matched.get("version") is not None
        ):
            raise Unrepresentable("matched_unrepresentable", "matched")
        candidates = matched.get("candidates")
        if candidates is not None:
            if not isinstance(candidates, list):
                raise Unrepresentable("matched_candidates_unrepresentable", "matched.candidates")
            for candidate in candidates:
                if (
                    not isinstance(candidate, dict)
                    or set(candidate) != {"inquiry_no", "version", "score"}
                    or not isinstance(candidate["inquiry_no"], str)
                    or not candidate["inquiry_no"]
                    or not isinstance(candidate["version"], int)
                    or isinstance(candidate["version"], bool)
                    or candidate["version"] < 1
                    or not isinstance(candidate["score"], (int, float))
                    or isinstance(candidate["score"], bool)
                    or not math.isfinite(candidate["score"])
                    or not 0 <= candidate["score"] <= 1
                ):
                    raise Unrepresentable(
                        "matched_candidates_unrepresentable", "matched.candidates"
                    )
        payload["matched"] = matched
    suggestions = decision.get("suggestions", [])
    if not isinstance(suggestions, list) or any(
        not isinstance(item, dict)
        or set(item) != {"type", "field", "question_text"}
        or item["type"] != "followup_question"
        or not isinstance(item["field"], str)
        or not isinstance(item["question_text"], str)
        for item in suggestions
    ):
        raise Unrepresentable("suggestions_unrepresentable", "suggestions")
    payload["suggestions"] = suggestions
    return payload


def _require_current_materials(business):
    for ref in business["material_results"]:
        material = read_result(business["material_store"], ref["result_id"], ref["revision"])
        revisions = [
            int(path.stem)
            for path in _path(
                business["material_store"], ref["result_id"], ref["revision"]
            ).parent.glob("*.json")
            if path.stem.isdecimal()
        ]
        if not revisions or max(revisions) != ref["revision"]:
            raise Unrepresentable("business_material_revision_stale", ref["result_id"])
        if material["material_handoff"]["handoff_state"] != "complete":
            raise Unrepresentable("partial_material_unrepresentable", ref["result_id"])


def project_business_result_draft(
    root, business_store, business_result_id, revision, *, interface_ref
):
    """Save a V1.0 draft preview or an explicit block; never queue a delivery."""
    _interface(interface_ref)
    business = read_business_result(business_store, business_result_id, revision)
    assessment = {
        "assessment_id": uuid4().hex,
        "business_store": str(Path(business_store).resolve()),
        "business_result_id": business_result_id,
        "business_revision": revision,
        "business_payload_sha256": hashlib.sha256(_encode(business)).hexdigest(),
        "interface_ref": interface_ref,
        "target_version": "V1.0",
        "adapter_version": "synthetic-interface/1",
        "state": "blocked",
        "reason": None,
        "details": None,
        "payload_ref": None,
        "delivery_state": "not_queued",
    }
    try:
        _require_current_materials(business)
        payload = _build_wire(business)
        wire = _encode(payload)
        projection_id = uuid4().hex
        record = {
            "projection_id": projection_id,
            "business_store": assessment["business_store"],
            "business_result_id": business_result_id,
            "business_revision": revision,
            "business_payload_sha256": assessment["business_payload_sha256"],
            "interface_ref": interface_ref,
            "adapter_version": assessment["adapter_version"],
            "kind": "unapproved_draft_preview",
            "wire_payload": payload,
            "wire_payload_json": wire.decode("utf-8"),
            "wire_sha256": hashlib.sha256(wire).hexdigest(),
        }
        ref = _write_new(
            _path(Path(root) / "payloads", projection_id, 1), record, "v1-draft-preview/1"
        )
        assessment.update(
            {
                "state": "draft_preview",
                "reason": "v1_mapping_pending_bilateral_review",
                "payload_ref": {"projection_id": projection_id, **ref},
            }
        )
    except Unrepresentable as error:
        assessment.update({"reason": error.code, "details": error.detail})
    ref = _write_new(
        _path(Path(root) / "assessments", assessment["assessment_id"], 1),
        assessment,
        "projection-assessment/1",
    )
    return {**assessment, **ref}


def read_draft_preview(root, projection_id):
    record = _read(_path(Path(root) / "payloads", projection_id, 1), "v1-draft-preview/1")
    if record["projection_id"] != projection_id or record["kind"] != "unapproved_draft_preview":
        raise SnapshotValidationError("Draft projection identity mismatch")
    _interface(record["interface_ref"])
    business = read_business_result(
        record["business_store"], record["business_result_id"], record["business_revision"]
    )
    if (
        hashlib.sha256(_encode(business)).hexdigest() != record["business_payload_sha256"]
        or _build_wire(business) != record["wire_payload"]
    ):
        raise SnapshotValidationError(
            "Draft projection no longer matches its fixed business result"
        )
    has_wire = "wire_payload_json" in record
    if has_wire != ("wire_sha256" in record):
        raise SnapshotValidationError("Draft wire payload metadata is incomplete")
    if has_wire:
        wire = record["wire_payload_json"].encode("utf-8")
        if (
            hashlib.sha256(wire).hexdigest() != record["wire_sha256"]
            or json.loads(wire) != record["wire_payload"]
        ):
            raise SnapshotValidationError("Saved draft wire payload hash or content mismatch")
    return record


def read_current_draft_preview(root, projection_id):
    """Read a draft for current local use; retain historical readback separately."""
    record = read_draft_preview(root, projection_id)
    business = read_business_result(
        record["business_store"], record["business_result_id"], record["business_revision"]
    )
    _require_current_materials(business)
    return record
