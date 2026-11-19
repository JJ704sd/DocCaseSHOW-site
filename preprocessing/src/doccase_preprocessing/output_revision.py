"""Review fixed evidence and publish a new material-result revision."""

import copy
from datetime import datetime, timezone
from uuid import uuid4

from .main_channel import (
    _complete,
    _facts,
    _pdf_row_field_supported,
    _pdf_total_volume_supported,
    read_result,
)
from .output_snapshot import SnapshotConflict, _read_entry, commit_result
from .rules import evaluate_candidates


def revise_candidates(root, result_id, *, base_revision, changes, reviewer):
    """Apply an explicit review to existing source bindings; do not invoke READ.

    A change is ``{candidate_id, status, reason, value?}``. A known value must
    be supported by the same candidate's existing evidence; uncertain clears its
    effective value. New evidence belongs in the reprocessing route instead.
    """
    if not isinstance(base_revision, int) or isinstance(base_revision, bool) or base_revision < 1:
        raise ValueError("A positive base_revision is required")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("A reviewer is required")
    if not isinstance(changes, list) or not changes:
        raise ValueError("At least one explicit candidate change is required")
    old, context = _read_entry(root, result_id, base_revision)
    snapshot = copy.deepcopy(old)
    handoff = snapshot["material_handoff"]
    candidates = handoff["candidates"]
    by_id = {item["candidate_id"]: item for item in candidates}
    sources = {item["source_id"]: item for item in handoff["sources"]}
    changed_ids = set()
    review_changes = []
    for change in changes:
        if not isinstance(change, dict) or set(change) - {
            "candidate_id",
            "status",
            "value",
            "reason",
        }:
            raise ValueError("Candidate review contains unsupported fields")
        cid = change.get("candidate_id")
        if cid not in by_id or cid in changed_ids:
            raise ValueError("Candidate review identity is unknown or duplicated")
        if not isinstance(change.get("reason"), str) or not change["reason"].strip():
            raise ValueError("Candidate review must include its reason")
        candidate = by_id[cid]
        field = candidate["field"]
        if field["origin"] != "extracted" or not field["source_ids"]:
            raise ValueError("Only an evidence-bound extracted candidate can be reviewed")
        before = {"status": field["status"], "value": field["value"]}
        status = change.get("status")
        if status == "uncertain":
            if change.get("value") is not None:
                raise ValueError("Uncertain candidate cannot have an effective value")
            field["status"], field["value"] = "uncertain", None
        elif status == "known":
            value = change.get("value")
            is_pdf_row = any(
                item.get("pdf_row_anchor")
                and item["field_name"] == candidate["field_name"]
                and item["object_selector"] == candidate["object_id"]
                for item in context["field_profile"]["requirements"]
            )
            supported = not is_pdf_row and any(
                observed == value and (unit is None or unit == field.get("unit"))
                for sid in field["source_ids"]
                for _, observed, unit in _facts(candidate["field_name"], sources[sid]["excerpt"])
            )
            if is_pdf_row or (not supported and candidate["field_name"] == "line_volume"):
                read = read_result(context["read_ref"])
                executions = {
                    item["execution_id"]: item
                    for item in read["executions"]
                    if item.get("execution_id")
                }
                bindings = {item["source_id"]: item for item in handoff["source_bindings"]}
                check = _pdf_row_field_supported if is_pdf_row else _pdf_total_volume_supported
                supported = check(
                    candidate, value, sources, bindings, executions, context["field_profile"]
                )
            if value is None or isinstance(value, bool) or not supported:
                raise ValueError("Reviewed value is not supported by the fixed source evidence")
            field["status"], field["value"] = "known", value
        else:
            raise ValueError("Review status must be known or uncertain")
        changed_ids.add(cid)
        review_changes.append(
            {
                "candidate_id": cid,
                "before": before,
                "after": {"status": field["status"], "value": field["value"]},
                "source_ids": list(field["source_ids"]),
                "reason": change["reason"],
            }
        )

    # Re-evaluate only derived values that depend on the reviewed candidates.
    for candidate in candidates:
        field = candidate["field"]
        if field["origin"] != "calculated" or not changed_ids.intersection(
            candidate.get("derived_from", [])
        ):
            continue
        if field.get("rule_id") != "equal-carton-weights-v1":
            raise ValueError("Unsupported dependent calculation")
        evaluated = evaluate_candidates(candidates, candidate["object_id"])
        if evaluated is None:
            field["value"], field["status"] = None, "uncertain"
        else:
            parents = evaluated["parents"]
            equality = candidate["uniform_per_item_evidence"]
            if equality not in sources:
                raise ValueError("Equal-weight evidence is no longer available")
            field["value"], field["status"] = evaluated["value"], "known"
            field["source_ids"] = list(
                dict.fromkeys(
                    parents["ctns"]["field"]["source_ids"]
                    + parents["line_gross_weight"]["field"]["source_ids"]
                    + [equality]
                )
            )
            candidate["derived_from"] = [
                parents["ctns"]["candidate_id"],
                parents["line_gross_weight"]["candidate_id"],
            ]
            field["input_paths"] = [
                f"/candidates/{candidates.index(parents['ctns'])}/field",
                f"/candidates/{candidates.index(parents['line_gross_weight'])}/field",
            ]
        changed_ids.add(candidate["candidate_id"])

    def review_issue_candidates(issue):
        return [candidates[int(path.split("/")[2])]["candidate_id"] for path in issue["paths"]]

    # Remove only issues created by earlier review revisions whose candidates are now known.
    resolved_review_issues = set()
    for issue in handoff["issues"]:
        if issue["code"] != "revision_candidate_unresolved":
            continue
        if all(
            by_id[review_id]["field"]["status"] == "known"
            for review_id in review_issue_candidates(issue)
        ):
            resolved_review_issues.add(issue["issue_id"])
    handoff["issues"] = [
        issue for issue in handoff["issues"] if issue["issue_id"] not in resolved_review_issues
    ]
    for assessment in handoff["requirement_assessments"]:
        assessment["issue_ids"] = [
            iid for iid in assessment["issue_ids"] if iid not in resolved_review_issues
        ]
        affected = [cid for cid in assessment["candidate_ids"] if cid in changed_ids]
        if not affected:
            continue
        uncertain = [cid for cid in affected if by_id[cid]["field"]["status"] != "known"]
        if uncertain:
            existing = {
                cid
                for issue in handoff["issues"]
                if issue["code"] == "revision_candidate_unresolved"
                for cid in review_issue_candidates(issue)
            }
            for cid in uncertain:
                if cid in existing:
                    continue
                candidate = by_id[cid]
                issue_id = "review-" + uuid4().hex
                handoff["issues"].append(
                    {
                        "issue_id": issue_id,
                        "category": "uncertain",
                        "code": "revision_candidate_unresolved",
                        "severity": "warning",
                        "paths": [f"/candidates/{candidates.index(candidate)}/field"],
                        "source_ids": list(candidate["field"]["source_ids"]),
                        "material_ids": [candidate["object_id"].split("/", 1)[0]],
                        "message": "Candidate or a required calculation input requires review",
                    }
                )
                assessment["issue_ids"].append(issue_id)
            assessment["state"] = "unresolved"
        elif not assessment["issue_ids"] and all(
            by_id[cid]["field"]["status"] == "known" for cid in assessment["candidate_ids"]
        ):
            assessment["state"] = "checked_present"

    handoff["result_revision"] = base_revision + 1
    handoff["base_revision"] = base_revision
    handoff["handoff_state"] = (
        "complete"
        if _complete(
            handoff["coverage"], handoff["requirement_assessments"], context["field_profile"]
        )
        else "partial"
    )
    handoff["review_items"].append(
        {
            "kind": "fixed_evidence_candidate_review",
            "base_revision": base_revision,
            "reviewer": reviewer,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "changes": review_changes,
        }
    )
    for item in snapshot["per_material_results"]:
        mid = item["material_id"]
        item["issue_ids"] = [
            issue["issue_id"] for issue in handoff["issues"] if mid in issue["material_ids"]
        ]
    try:
        return commit_result(root, snapshot, **context, _expected_base=base_revision)
    except SnapshotConflict as error:
        raise SnapshotConflict("Review base_revision is stale or already applied") from error
