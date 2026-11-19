"""Reprepare and reread changed materials before appending a material revision."""

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

from .local_handoff import run_local_handoff
from .main_channel import _save, read_result, run_main_channel
from .output_snapshot import SnapshotConflict, _path, _read_entry, commit_result


def _identity(record):
    return {
        "representation": record["representation"],
        "content_hash": record["content"]["sha256"] if record.get("content") else None,
        "original_hash": record["original"]["sha256"] if record.get("original") else None,
        "state": record["state"],
        "role": record["role"],
        "email_ref_id": record["email_ref_id"],
        "kind": record["kind"],
        "format": record["format"],
        "filename": record.get("filename"),
    }


def reprocess_materials(
    root,
    result_id,
    *,
    base_revision,
    raw,
    input_format,
    request_reference,
    output_dir,
    select_and_require,
    reader,
    read_configurations,
    reader_versions,
    field_contract_ref,
    fetch_attachment=None,
    worker_fn=None,
    task_id=None,
    email_ref_id=None,
):
    """Re-run P01 and P03 using caller-supplied scope, requirements and reader.

    ``select_and_require(manifest)`` returns selected_material_ids, field_profile
    and task_scope. All changed ready-to-read materials must be selected and reach
    the actual reader; blocked materials remain visible in the partial result.
    """
    if not isinstance(base_revision, int) or isinstance(base_revision, bool) or base_revision < 1:
        raise ValueError("A positive base_revision is required")
    old, old_context = _read_entry(root, result_id, base_revision)
    store_dir = _path(root, result_id, base_revision).parent
    existing = [int(path.stem) for path in store_dir.glob("*.json") if path.stem.isdecimal()]
    if not existing or max(existing) != base_revision:
        raise SnapshotConflict("Material update base_revision is stale")
    old_manifest = json.loads(Path(old_context["manifest_ref"]["ref"]).read_text(encoding="utf-8"))
    old_records = {item["material_id"]: item for item in old_manifest["materials"]}
    run_root = Path(output_dir).resolve()
    run_root.mkdir(parents=True, exist_ok=False)
    content_root = run_root / "p01"
    prepared = run_local_handoff(
        raw,
        content_root,
        input_format=input_format,
        request_reference=request_reference,
        task_id=task_id,
        email_ref_id=email_ref_id,
        fetch_attachment=fetch_attachment,
    )
    manifest = prepared["received"]["manifest"]
    if manifest["task_id"] != old_manifest["task_id"]:
        raise ValueError("Changed materials must remain in the same task/result series")
    records = {item["material_id"]: item for item in manifest["materials"]}
    changed = sorted(
        mid
        for mid, record in records.items()
        if mid not in old_records or _identity(record) != _identity(old_records[mid])
    )
    removed = sorted(set(old_records) - set(records))
    if not changed and not removed:
        raise ValueError("No material occurrence changed; use fixed-evidence review")
    request = select_and_require(copy.deepcopy(manifest))
    selected = set(request["selected_material_ids"])
    must_read = {
        mid
        for mid in changed
        if records[mid]["state"] == "ready" and records[mid].get("parse_task_id")
    }
    if not must_read.issubset(selected):
        raise ValueError("Changed ready material must be selected for READ")
    if request["field_profile"] is not None:
        targeted = {item["material_id"] for item in request["field_profile"]["requirements"]}
        if not must_read.issubset(targeted):
            raise ValueError(
                "Changed ready material must have an independent extraction requirement"
            )
    result = run_main_channel(
        prepared["manifest_path"],
        content_root,
        expected_task_id=manifest["task_id"],
        expected_request_fingerprint=prepared["request_fingerprint"],
        intake_context=prepared["intake_context"],
        selected_material_ids=selected,
        read_configurations=read_configurations,
        reader_versions=reader_versions,
        field_profile=request["field_profile"],
        task_scope=request["task_scope"],
        field_contract_ref=field_contract_ref,
        output_dir=run_root / "main-channel",
        reader=reader,
        worker_fn=worker_fn,
    )
    if result["result_ref"] is None:
        return {
            "status": result["status"],
            "reason": result["reason"],
            "read_ref": result["read_ref"],
            "progress_ref": result["progress_ref"],
            "affected_material_ids": changed,
            "removed_material_ids": removed,
        }
    read = read_result(result["read_ref"])
    inventory = {item["material_id"]: item for item in read["inventory"]}
    executions = {
        item["execution_id"]: item for item in read["executions"] if item.get("execution_id")
    }
    if any(
        not inventory[mid]["execution_id"]
        or not executions[inventory[mid]["execution_id"]]["reader_started"]
        for mid in must_read
    ):
        raise ValueError(
            "Changed material did not reach the actual reader; result was not committed"
        )
    snapshot = read_result(result["result_ref"])
    handoff = snapshot["material_handoff"]
    previous_versions = {
        item["material_id"]: item for item in old["material_handoff"]["material_versions"]
    }
    versions = {item["material_id"]: item for item in handoff["material_versions"]}
    for mid, version in versions.items():
        if mid not in previous_versions:
            continue
        previous = previous_versions[mid]
        version["material_revision"] = previous["material_revision"] + (mid in changed)
        old_ref, new_ref = previous["representation_ref"], version["representation_ref"]
        representation_changed = (old_ref["sha256"] if old_ref else None) != (
            new_ref["sha256"] if new_ref else None
        ) or previous["representation"] != version["representation"]
        version["representation_revision"] = (
            previous["representation_revision"] + representation_changed
        )
    for binding in handoff["source_bindings"]:
        version = versions[binding["material_id"]]
        binding["material_revision"] = version["material_revision"]
        binding["representation_revision"] = version["representation_revision"]
    for artifact in handoff["artifact_refs"]:
        parsed_path = Path(artifact["parsed_artifact_ref"])
        parsed = json.loads(parsed_path.read_text(encoding="utf-8"))
        for occurrence in parsed["occurrence_bindings"]:
            occurrence["material_revision"] = versions[occurrence["material_id"]][
                "material_revision"
            ]
        artifact["parsed_artifact_sha256"] = _save(parsed_path, parsed)["sha256"]
    handoff["result_id"] = result_id
    handoff["result_revision"] = base_revision + 1
    handoff["base_revision"] = base_revision
    handoff["review_items"].append(
        {
            "kind": "material_reprocessed",
            "base_revision": base_revision,
            "affected_material_ids": changed,
            "removed_material_ids": removed,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    inputs = {
        "task_scope": request["task_scope"],
        "field_profile": request["field_profile"],
        "manifest_ref": {"ref": str(prepared["manifest_path"]), "sha256": read["manifest_sha256"]},
        "read_ref": result["read_ref"],
        "content_root": str(content_root),
        "field_contract_ref": field_contract_ref,
    }
    saved = commit_result(root, snapshot, **inputs, _expected_base=base_revision)
    return {
        **saved,
        "affected_material_ids": changed,
        "removed_material_ids": removed,
        "read_ref": result["read_ref"],
        "preparation_manifest_ref": inputs["manifest_ref"],
    }
