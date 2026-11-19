"""Project sender-bound company evidence from current inline image READs."""

import copy
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import p02_local_read as p02

from doccase_preprocessing.inline_company import (
    _box,
    _company_lines,
    eligible_inline_images,
    sender_domain,
    signature_section_evidence,
)
from doccase_preprocessing.local_handoff import _read_content
from doccase_preprocessing.main_channel import (
    _complete,
    _coverage,
    _save,
    read_result,
    read_validated_result,
    validate_material_handoff,
)


def _source(record, read_ref, line_info, *, source_id, start=None, end=None):
    index, line = line_info[:2]
    text = line["text"]
    start = line_info[2] if start is None and len(line_info) > 2 else (start or 0)
    end = (
        line_info[3] if end is None and len(line_info) > 3 else (len(text) if end is None else end)
    )
    artifact_id = "inline-ocr-" + record["material_id"]
    source = {
        "source_id": source_id,
        "email_ref_id": record["email_ref_id"],
        "kind": "signature",
        "material_id": record["material_id"],
        "excerpt": text[start:end],
        "locator": {
            "json_pointer": f"/parts/0/lines/{index}/text",
            "char_start": start,
            "char_end": end,
            "region": _box(line),
        },
        "location_status": "exact",
    }
    binding = {
        "source_id": source_id,
        "material_id": record["material_id"],
        "material_revision": 1,
        "content_hash": (record.get("original") or {}).get("sha256"),
        "artifact_id": artifact_id,
        "artifact_hash": read_ref["sha256"],
        "text_version": read_ref["sha256"],
        "representation_hash": record["content"]["sha256"],
        "representation_revision": 1,
        "locator_basis": "artifact",
        "coordinate_system": "unicode_codepoint_half_open",
        "character_unit": "unicode_codepoint",
        "mapping_status": "artifact_only",
        "origin_locator": None,
        "representation_locator": None,
        "docling_provenance": [],
        "docling_cell": None,
        "section_evidence": None,
    }
    return {"source": source, "binding": binding}


def project_inline_company(response, *, manifest, target, content_root, image_reader):
    """Supplement the local response; the independently validated material handoff stays intact."""
    company = next(
        (field for field in response["fields"] if field["field_name"] == "customer_company"), None
    )
    if (
        not company
        or company["status"] not in {"uncertain", "missing"}
        or target.get("kind") != "body"
        or target.get("role") != "current"
        or target.get("format") != "html"
    ):
        return response
    domain = sender_domain(manifest, target)
    if not domain:
        return response
    images = eligible_inline_images(manifest, target, content_root)
    if not images:
        return response

    output = copy.deepcopy(response)
    company = next(field for field in output["fields"] if field["field_name"] == "customer_company")
    audit = {
        "scope": "local_response_projection",
        "base_result_ref": output["result_ref"],
        "state": "no_candidate",
        "reads": [],
    }
    candidates = []
    for record in images:
        row = {"material_id": record["material_id"], "input_sha256": record["content"]["sha256"]}
        try:
            _read_content(Path(content_root).resolve(), record["content"])
        except (OSError, ValueError, KeyError):
            row.update(state="unavailable", reason="image_content_unverified")
            audit["reads"].append(row)
            continue
        reference = image_reader(record)
        if not reference or reference.get("error"):
            row.update(
                state="unavailable", reason=(reference or {}).get("error", "read_unavailable")
            )
            audit["reads"].append(row)
            continue
        try:
            data = Path(reference["ref"]).read_bytes()
            if hashlib.sha256(data).hexdigest() != reference["sha256"]:
                raise ValueError("read_hash_mismatch")
            read = json.loads(data)
            if (
                read.get("material_id") != record["material_id"]
                or read.get("input_sha256") != record["content"]["sha256"]
            ):
                raise ValueError("read_material_mismatch")
        except (OSError, ValueError, KeyError, TypeError) as error:
            row.update(state="unavailable", reason=str(error))
            audit["reads"].append(row)
            continue
        row.update(
            state="success", read_ref={"ref": reference["ref"], "sha256": reference["sha256"]}
        )
        for name in ("markdown_ref", "index_ref"):
            if name in reference:
                row[name] = reference[name]
        audit["reads"].append(row)
        candidates.extend(
            (record, reference, *match)
            for match in _company_lines(read, domain, record.get("scope") or {})
        )
    names = {candidate[2].casefold() for candidate in candidates}
    if len(names) == 1:
        evidence = []
        for record, reference, _value, brand, support in candidates:
            for item in (brand, support):
                if item is not None:
                    evidence.append(
                        _source(record, reference, item, source_id=f"S-inline-{len(evidence) + 1}")
                    )
        old_issues = {item["issue_id"] for item in company["issues"]}
        other_issues = {
            issue["issue_id"]
            for field in output["fields"]
            if field is not company
            for issue in field["issues"]
        }
        company.update(
            value=candidates[0][2], unit=None, status="known", sources=evidence, issues=[]
        )
        if "raw_values" in company:
            company["raw_values"] = [{"value": candidates[0][2]}]
        output["issues"] = [
            issue
            for issue in output["issues"]
            if issue["issue_id"] not in old_issues - other_issues
        ]
        output["sources"].extend(item["source"] for item in evidence)
        output["source_bindings"].extend(item["binding"] for item in evidence)
        for record, reference, *_ in candidates:
            artifact_id = "inline-ocr-" + record["material_id"]
            if not any(item["artifact_id"] == artifact_id for item in output["artifacts"]):
                output["artifacts"].append(
                    {
                        "artifact_id": artifact_id,
                        "kind": "inline_ocr_read_json",
                        "ref": reference["ref"],
                        "producer": "RapidOCR",
                        "artifact_hash": reference["sha256"],
                        "material_ids": [record["material_id"]],
                        "input_representation_hash": record["content"]["sha256"],
                    }
                )
        audit["state"] = "accepted"
    elif names:
        audit["state"] = "conflict"
    output["inline_ocr"] = audit
    return output


def publish_inline_company(base_response, projected, *, content_root, output_dir):
    """Publish a new validated material snapshot while preserving the base result and READ."""
    audit = projected.get("inline_ocr", {})
    if (
        audit.get("state") != "accepted"
        or audit.get("base_result_ref") != base_response["result_ref"]
    ):
        return None
    base = read_validated_result(base_response["result_ref"], content_root=content_root)
    snapshot = copy.deepcopy(base)
    handoff = snapshot["material_handoff"]
    read = copy.deepcopy(read_result(snapshot["read_result_ref"]))
    manifest = json.loads(Path(read["manifest_path"]).read_bytes())
    records = {item["material_id"]: item for item in manifest["materials"]}
    tasks = {item["parse_task_id"]: item for item in manifest["parse_tasks"]}
    inventory = {item["material_id"]: item for item in read["inventory"]}
    profile = handoff["required_field_profile"]
    requirements = [r for r in profile["requirements"] if r["field_name"] == "customer_company"]
    if len(requirements) != 1:
        return None
    requirement = requirements[0]
    assessment = next(
        a
        for a in handoff["requirement_assessments"]
        if a["requirement_id"] == requirement["requirement_id"]
    )
    candidates = {item["candidate_id"]: item for item in handoff["candidates"]}
    if len(assessment["candidate_ids"]) != 1:
        return None
    candidate = candidates[assessment["candidate_ids"][0]]
    issue_by_id = {item["issue_id"]: item for item in handoff["issues"]}
    candidate_path = f"/candidates/{handoff['candidates'].index(candidate)}/field"
    if (
        candidate["field"]["status"] not in {"uncertain", "missing"}
        or candidate["field"]["source_ids"]
        or any(
            issue_by_id[iid]["code"] != "field_not_found_or_scope_unread"
            or issue_by_id[iid]["paths"] != [candidate_path]
            for iid in assessment["issue_ids"]
        )
    ):
        return None
    if any(
        set(assessment["issue_ids"]) & set(other["issue_ids"])
        for other in handoff["requirement_assessments"]
        if other is not assessment
    ):
        return None
    company = next(
        field for field in projected["fields"] if field["field_name"] == "customer_company"
    )
    refs = {item["material_id"]: item for item in audit["reads"] if item["state"] == "success"}
    if not company["sources"] or not {
        e["source"]["material_id"] for e in company["sources"]
    }.issubset(refs):
        return None
    if any(not inventory[mid]["intake_source_bound"] for mid in refs):
        return None
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "parsed-artifacts").mkdir()
    material_versions = {item["material_id"]: item for item in handoff["material_versions"]}
    for mid, item in refs.items():
        record, observed = records[mid], inventory[mid]
        reference = item["read_ref"]
        image_read = read_result(reference)
        if (
            image_read.get("material_id") != mid
            or image_read.get("input_sha256") != record["content"]["sha256"]
            or image_read.get("reader") != "RapidOCR"
            or observed["execution_id"] is not None
        ):
            raise ValueError("Inline OCR READ does not bind the original unread material")
        execution_id = "inline-ocr-" + mid
        scope = {"kind": "full_representation"}
        config = {
            "reader": "RapidOCR",
            "versions": {},
            "options": {"offline": True},
            "requested_scope": scope,
        }
        occurrence_keys = (
            "material_id",
            "email_ref_id",
            "role_raw",
            "source_role",
            "kind",
            "parent_material_id",
            "source_path",
            "mime_path",
            "representation",
            "representation_sha256",
            "original_sha256",
            "intake_source_bound",
        )
        execution = {
            "artifact_semantics": "parsed-artifact/0.1-draft",
            "artifact_id": reference["sha256"],
            "artifact_hash": reference["sha256"],
            "ref": reference["ref"],
            "input_manifest_ref": {"ref": read["manifest_path"], "sha256": read["manifest_sha256"]},
            "input_plan_id": observed["input_plan_id"],
            "execution_id": execution_id,
            "execution_key": p02.execution_key(tasks[observed["input_plan_id"]], config),
            "input_ref": {"sha256": record["content"]["sha256"]},
            "occurrence_bindings": [{key: observed[key] for key in occurrence_keys}],
            "configured_profile": copy.deepcopy(config),
            "executed_profile": copy.deepcopy(config),
            "execution_state": "success",
            "reader_started": True,
            "structure_refs": [
                {
                    "kind": "inline_ocr_read_json",
                    "ref": reference["ref"],
                    "sha256": reference["sha256"],
                }
            ],
            "mapping_ref": {
                "ref": reference["ref"],
                "sha256": reference["sha256"],
                "basis": "image_ocr",
            },
            "coverage": {
                "requested_scope": scope,
                "actual_scope": {
                    "observed_docling_pages": None,
                    "observed_text_items": len(image_read["parts"][0]["lines"]),
                    "observed_table_items": 0,
                    "representation_completeness": "unverified",
                },
            },
            "issues": [],
            "result": {
                "conversion_status": "success",
                "json_path": reference["ref"],
                "json_sha256": reference["sha256"],
                "text_items": len(image_read["parts"][0]["lines"]),
                "table_items": 0,
                "errors": [],
            },
        }
        observed.update(read_state="success", reason=None, execution_id=execution_id)
        read["executions"].append(execution)
        material = material_versions[mid]
        parsed_profile = {**copy.deepcopy(config), "version": None}
        parsed = {
            "artifact_spec_version": "parsed-artifact/0.1-draft",
            "artifact_id": execution_id,
            "artifact_hash": reference["sha256"],
            "execution_id": execution_id,
            "ref": reference["ref"],
            "input_manifest_ref": execution["input_manifest_ref"],
            "input_plan_id": observed["input_plan_id"],
            "input_ref": execution["input_ref"],
            "executed_profile": parsed_profile,
            "occurrence_bindings": [
                {
                    "material_id": mid,
                    "email_ref_id": material["source_email_ref_id"],
                    "role_raw": material["role_raw"],
                    "source_role": material["source_role"],
                    "material_revision": material["material_revision"],
                    "representation": material["representation"],
                    "representation_ref": material["representation_ref"],
                    "original_ref": {"ref": material["raw_ref"], "sha256": material["content_hash"]}
                    if material["raw_ref"]
                    else None,
                }
            ],
            "execution_state": "finished",
            "termination_reason": "normal",
            "reader_conversion_state": "success",
            "coverage": execution["coverage"],
            "issues": [],
        }
        parsed_ref = _save(output / "parsed-artifacts" / (execution_id + ".json"), parsed)
        handoff["artifact_refs"].append(
            {
                "artifact_id": execution_id,
                "kind": "inline_ocr_read_json",
                "ref": reference["ref"],
                "producer": "RapidOCR",
                "artifact_hash": reference["sha256"],
                "material_ids": [mid],
                "input_representation_hash": record["content"]["sha256"],
                "execution_id": execution_id,
                "input_plan_id": observed["input_plan_id"],
                "parsed_artifact_ref": parsed_ref["ref"],
                "parsed_artifact_sha256": parsed_ref["sha256"],
            }
        )

    existing_ids = {item["source_id"] for item in handoff["sources"]}
    entries = copy.deepcopy(company["sources"])
    for entry in entries:
        sid = "S-inline-" + uuid4().hex
        while sid in existing_ids:
            sid = "S-inline-" + uuid4().hex
        existing_ids.add(sid)
        entry["source"]["source_id"] = entry["binding"]["source_id"] = sid
        entry["binding"]["section_evidence"] = signature_section_evidence(
            manifest, records[requirement["material_id"]], records[entry["source"]["material_id"]]
        )
        handoff["sources"].append(entry["source"])
        handoff["source_bindings"].append(entry["binding"])
    removed = set(assessment["issue_ids"])
    handoff["issues"] = [issue for issue in handoff["issues"] if issue["issue_id"] not in removed]
    assessment.update(state="checked_present", issue_ids=[])
    candidate["field"].update(
        value=company["value"],
        confidence=None,
        status="known",
        origin="extracted",
        source_ids=[entry["source"]["source_id"] for entry in entries],
        raw_values=[{"value": company["value"]}],
    )
    coverage, observed = _coverage(
        read["task_id"],
        read["inventory"],
        read["executions"],
        profile,
        snapshot.get("task_scope_source"),
    )
    handoff["coverage"] = coverage
    handoff["handoff_state"] = (
        "complete"
        if _complete(coverage, handoff["requirement_assessments"], profile)
        else "partial"
    )
    previous_revision = handoff["result_revision"]
    handoff.update(result_revision=previous_revision + 1, base_revision=previous_revision)
    snapshot["analysis"]["observed_read_scope"] = observed
    executions = {
        item["execution_id"]: item for item in read["executions"] if item.get("execution_id")
    }
    snapshot["per_material_results"] = [
        {
            "material_id": item["material_id"],
            "read_state": item["read_state"],
            "reason": item["reason"]
            or ("reader_conversion_failure" if item["read_state"] == "failure" else None),
            "preprocessing_issues": item["preprocessing_issues"],
            "read_issues": executions[item["execution_id"]]["issues"]
            if item["execution_id"] in executions
            else [],
            "candidate_ids": [
                c["candidate_id"]
                for c in handoff["candidates"]
                if c["object_id"].startswith(item["material_id"] + "/")
            ],
            "issue_ids": [
                issue["issue_id"]
                for issue in handoff["issues"]
                if item["material_id"] in issue["material_ids"]
            ],
        }
        for item in read["inventory"]
    ]
    read_ref = _save(output / "read-result.json", read)
    snapshot["read_result_ref"] = read_ref
    validate_material_handoff(
        snapshot,
        task_scope=snapshot.get("task_scope_source"),
        manifest_ref=handoff["input_manifest_ref"],
        field_profile=profile,
        read_ref=read_ref,
        content_root=content_root,
        field_contract_ref=snapshot["field_contract_source"],
    )
    result_ref = _save(output / "material-result.json", snapshot)
    read_validated_result(result_ref, content_root=content_root)
    base_progress = json.loads(
        Path(base_response["result_ref"]["ref"]).with_name("progress.json").read_bytes()
    )
    base_progress["read"]["result_ref"] = read_ref
    base_progress["result_ref"] = result_ref
    _save(output / "progress.json", base_progress)
    return {"status": handoff["handoff_state"], "result_ref": result_ref, "read_ref": read_ref}
