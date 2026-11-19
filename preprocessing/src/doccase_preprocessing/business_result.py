"""Persist a caller-supplied business Agent decision separately from materials."""

import copy
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from .output_snapshot import (
    SnapshotConflict,
    SnapshotStorageError,
    SnapshotValidationError,
    _encode,
    _path,
)
from .output_snapshot import (
    read_result as read_material_result,
)


def _validate_business(record):
    decision = record["agent_decision"]
    if not isinstance(decision, dict) or not all(
        isinstance(decision.get(key), str) and decision[key].strip()
        for key in ("agent_id", "decision_ref", "task_id", "confirmed_at")
    ):
        raise ValueError("Business Agent identity, decision reference, task and time are required")
    for name in ("intent", "inquiry_assignment", "field_applicability"):
        if not isinstance(decision.get(name), dict) or decision[name].get("confirmed") is not True:
            raise ValueError(
                "Business Agent must explicitly confirm intent, assignment and applicability"
            )
        if "value" not in decision[name]:
            raise ValueError("Each business confirmation must include its actual decision")
    if not isinstance(decision["intent"]["value"], str) or not decision["intent"]["value"].strip():
        raise ValueError("Confirmed intent must have an explicit value")
    if not isinstance(decision["inquiry_assignment"]["value"], list) or not isinstance(
        decision["field_applicability"]["value"], list
    ):
        raise ValueError("Confirmed assignment and applicability must be lists")
    baseline = record["context_baseline"]
    if not isinstance(baseline, dict) or not baseline.get("ref") or not baseline.get("sha256"):
        raise ValueError("An independently fixed business context baseline is required")
    if hashlib.sha256(Path(baseline["ref"]).read_bytes()).hexdigest() != baseline["sha256"]:
        raise ValueError("Business context baseline SHA-256 mismatch")
    evidence = decision.get("agent_evidence")
    if evidence is not None:
        if not isinstance(evidence, dict) or set(evidence) != {"ref", "sha256"}:
            raise ValueError("Business Agent evidence reference is invalid")
        if hashlib.sha256(Path(evidence["ref"]).read_bytes()).hexdigest() != evidence["sha256"]:
            raise ValueError("Business Agent evidence SHA-256 mismatch")
    refs = record["material_results"]
    if not isinstance(refs, list) or not refs:
        raise ValueError("At least one exact material result revision is required")
    identities = []
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {"result_id", "revision"}:
            raise ValueError("Material inputs require exactly result_id and revision")
        material = read_material_result(record["material_store"], ref["result_id"], ref["revision"])
        if material["material_handoff"]["task_id"] != decision["task_id"]:
            raise ValueError("Business decision and material task differ")
        identities.append((ref["result_id"], ref["revision"]))
    if len(set(identities)) != len(identities):
        raise ValueError("Duplicate material result revision")
    if decision["agent_id"] == "EmailBridge-PoC":
        if evidence is None or len(refs) != 1:
            raise ValueError(
                "EmailBridge decision requires its review evidence and one material revision"
            )
        from .emailbridge_local import reviewed_business_decision

        reviewed = json.loads(Path(evidence["ref"]).read_bytes())
        rebuilt = reviewed_business_decision(
            reviewed["job"],
            material_store=record["material_store"],
            result_id=refs[0]["result_id"],
            revision=refs[0]["revision"],
            reviewer=reviewed["reviewer"],
            confirmed_intent=reviewed["confirmed_intent"],
            mappings=reviewed["mappings"],
            agent_evidence=evidence,
        )
        if any(decision.get(key) != value for key, value in rebuilt.items()):
            raise ValueError("EmailBridge business decision does not match its reviewed evidence")


def _write_new(path, record, format_name):
    payload = _encode(record)
    envelope = {
        "format": format_name,
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "payload": record,
    }
    raw = _encode(envelope) + b"\n"
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise SnapshotConflict("Business artifact revision already exists") from error
    except OSError as error:
        raise SnapshotStorageError(f"Atomic business artifact save failed: {error}") from error
    finally:
        if temporary.exists():
            temporary.unlink()
    return {"ref": str(path), "payload_sha256": envelope["payload_sha256"]}


def _read(path, format_name):
    try:
        envelope = json.loads(path.read_bytes())
        payload = envelope["payload"]
        if (
            envelope["format"] != format_name
            or hashlib.sha256(_encode(payload)).hexdigest() != envelope["payload_sha256"]
        ):
            raise ValueError("Business artifact payload SHA-256 mismatch")
        return payload
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SnapshotValidationError(f"Business artifact readback failed: {error}") from error


def save_business_result(
    root, *, material_store, material_results, context_baseline, agent_decision
):
    """Save an already-confirmed decision and exact inputs; this does not call an Agent."""
    record = {
        "business_result_id": uuid4().hex,
        "business_revision": 1,
        "material_store": str(Path(material_store).resolve()),
        "material_results": copy.deepcopy(material_results),
        "context_baseline": copy.deepcopy(context_baseline),
        "agent_decision": copy.deepcopy(agent_decision),
    }
    try:
        _validate_business(record)
        _encode(record)
    except (OSError, ValueError, KeyError, TypeError, SnapshotValidationError) as error:
        raise SnapshotValidationError(f"Business decision validation failed: {error}") from error
    ref = _write_new(
        _path(root, record["business_result_id"], 1), record, "business-result-snapshot/1"
    )
    return {"business_result_id": record["business_result_id"], "business_revision": 1, **ref}


def read_business_result(root, business_result_id, revision):
    record = _read(_path(root, business_result_id, revision), "business-result-snapshot/1")
    try:
        if (
            record["business_result_id"] != business_result_id
            or record["business_revision"] != revision
        ):
            raise ValueError("Business result identity mismatch")
        _validate_business(record)
        return record
    except (OSError, ValueError, KeyError, TypeError, SnapshotValidationError) as error:
        raise SnapshotValidationError(f"Business result readback failed: {error}") from error


def record_unavailable_projection(root, business_store, business_result_id, revision):
    """Record the real stop until an approved, implemented V1.0 mapping exists."""
    business = read_business_result(business_store, business_result_id, revision)
    assessment = {
        "assessment_id": uuid4().hex,
        "business_store": str(Path(business_store).resolve()),
        "business_result_id": business_result_id,
        "business_revision": revision,
        "business_payload_sha256": hashlib.sha256(_encode(business)).hexdigest(),
        "target_version": "V1.0",
        "state": "blocked",
        "reason": "approved_v1_mapping_unavailable",
        "payload_ref": None,
        "delivery_state": "not_queued",
    }
    ref = _write_new(
        _path(root, assessment["assessment_id"], 1), assessment, "projection-assessment/1"
    )
    return {**assessment, **ref}


def read_projection_assessment(root, assessment_id):
    assessment = _read(_path(root, assessment_id, 1), "projection-assessment/1")
    if assessment["assessment_id"] != assessment_id:
        raise SnapshotValidationError("Projection assessment identity mismatch")
    business = read_business_result(
        assessment["business_store"],
        assessment["business_result_id"],
        assessment["business_revision"],
    )
    if hashlib.sha256(_encode(business)).hexdigest() != assessment["business_payload_sha256"]:
        raise SnapshotValidationError("Projection assessment business result hash mismatch")
    if "interface_ref" in assessment:
        from .output_project import _interface, read_draft_preview

        _interface(assessment["interface_ref"])
        reference = assessment["payload_ref"]
        if reference is not None:
            project_root = Path(root).resolve().parent
            expected = _path(project_root / "payloads", reference["projection_id"], 1)
            if Path(reference["ref"]).resolve() != expected:
                raise SnapshotValidationError("Projection payload reference mismatch")
            preview = read_draft_preview(project_root, reference["projection_id"])
            if (
                hashlib.sha256(_encode(preview)).hexdigest() != reference["payload_sha256"]
                or preview["business_payload_sha256"] != assessment["business_payload_sha256"]
                or preview["interface_ref"] != assessment["interface_ref"]
            ):
                raise SnapshotValidationError("Projection payload association mismatch")
    return assessment
