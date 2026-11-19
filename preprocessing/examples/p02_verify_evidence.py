"""Reopen local P02 evidence and verify its inputs, artifacts and sampled locations."""

import argparse
import hashlib
import json
from pathlib import Path


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def require(condition, message):
    if not condition:
        raise ValueError(message)


def pointer(document, reference):
    require(reference.startswith("#/"), "Invalid native JSON pointer")
    value = document
    for segment in reference[2:].split("/"):
        segment = segment.replace("~1", "/").replace("~0", "~")
        value = value[int(segment)] if isinstance(value, list) else value[segment]
    return value


def normalized(text):
    return "".join(character.casefold() for character in text if character.isalnum())


def verify(report_path):
    report_path = Path(report_path).resolve(strict=True)
    root = report_path.parent
    report_bytes = report_path.read_bytes()
    report = json.loads(report_bytes)
    observations = {}
    manual_review_pending = False
    for name, case in report["cases"].items():
        manifest_bytes = Path(case["manifest_path"]).read_bytes()
        require(sha256(manifest_bytes) == case["manifest_sha256"], "Input manifest hash changed")
        manifest = json.loads(manifest_bytes)
        require(
            manifest["schema_version"] == "doccase-preprocessing/0.1", "Manifest version changed"
        )
        require(
            all(task["execution_state"] == "not_started" for task in manifest["parse_tasks"]),
            "Input plan execution state changed",
        )
        execution = case["executions"][0]
        plan = next(
            task
            for task in manifest["parse_tasks"]
            if task["parse_task_id"] == execution["input_plan_id"]
        )
        require(
            plan["content_sha256"] == execution["input_ref"]["sha256"],
            "Artifact input differs from plan",
        )
        config = execution["configured_profile"]
        identity = {
            "input_plan_id": plan["parse_task_id"],
            "input_content_sha256": plan["content_sha256"],
            "actual_configuration": config,
            "requested_scope": config["requested_scope"],
        }
        require(sha256(canonical(identity)) == execution["execution_key"], "Execution key differs")
        require(
            execution["executed_profile"]["versions"] == config["versions"],
            "Executed versions differ from declared versions",
        )
        artifact = Path(execution["ref"]).resolve(strict=True)
        require(artifact.is_relative_to(root), "Artifact escapes the local run")
        artifact_bytes = artifact.read_bytes()
        require(
            sha256(artifact_bytes)
            == execution["artifact_hash"]
            == execution["structure_refs"][0]["sha256"],
            "Artifact hash differs",
        )
        document = json.loads(artifact_bytes)
        require(execution["coverage"]["actual_scope"] is not None, "Actual read scope is absent")
        observation = {
            "read_state": execution["execution_state"],
            "conversion_status": execution["result"]["conversion_status"],
            "artifact_sha256": execution["artifact_hash"],
            "execution_key": execution["execution_key"],
            "input_manifest_sha256": case["manifest_sha256"],
            "intake_bound": case["intake_bound"],
        }
        if name in {"xlsx", "html", "text"}:
            require(not case["intake_bound"], "Unexpected replay intake binding")
            require(
                any(
                    issue.get("code") == "intake_source_context_unavailable"
                    for issue in execution["issues"]
                ),
                "Missing source binding was not recorded",
            )
        if name == "pdf":
            require(case["intake_bound"], "Same-call intake context was not bound")
            validation = execution["validation"]
            require(
                validation["page_counts_match"] and validation["page_evidence_found"],
                "PDF evidence missing",
            )
            anchor = validation["matched_page_anchors"][0]
            output = pointer(document, anchor["docling_ref"])
            require(
                normalized(anchor["source_excerpt"]) in normalized(output["text"]),
                "PDF anchor differs",
            )
            require(
                any(item["page_no"] == anchor["source_page"] for item in output["prov"]),
                "PDF anchor page differs",
            )
            references = [validation["source_page_preview"]]
            manual_reference = validation.get("manual_review_ref")
            if manual_reference:
                references.append(manual_reference)
            else:
                require(
                    validation["visual_confirmation"] == "manual_review_required",
                    "PDF visual review status is inconsistent",
                )
                manual_review_pending = True
            for ref in references:
                evidence_path = Path(ref["path"]).resolve(strict=True)
                require(evidence_path.is_relative_to(root), "PDF review escapes the local run")
                require(
                    sha256(evidence_path.read_bytes()) == ref["sha256"], "PDF review hash differs"
                )
            if manual_reference:
                manual = json.loads(Path(manual_reference["path"]).read_bytes())
                prior = manual.get("prior_review")
                if prior:
                    prior_path = Path(prior["path"]).resolve(strict=True)
                    require(
                        prior_path.is_relative_to(root.parent),
                        "Prior review escapes the evidence root",
                    )
                    require(
                        sha256(prior_path.read_bytes()) == prior["sha256"],
                        "Prior review hash differs",
                    )
                    prior_review = json.loads(prior_path.read_bytes())
                    require(
                        prior_review["preview"]["sha256"]
                        == validation["source_page_preview"]["sha256"],
                        "Prior review refers to a different page preview",
                    )
            observation["visual_review"] = validation["visual_confirmation"]
            observation["source_pages"] = validation["source_page_count"]
        elif name == "xlsx":
            validation = execution["validation"]
            require(validation["processed_sheet_names"] == ["CI A"], "Unexpected workbook scope")
            require("PL" in validation["unread_sheet_names"], "Hidden sheet was not marked unread")
            require(validation["all_sampled_cells_match"], "Workbook sampled cells differ")
            for cell in validation["cell_comparison"]:
                require(
                    cell["match"] and cell["docling_values"], "Workbook comparison lacks evidence"
                )
                for candidate in cell["docling_values"]:
                    output = pointer(document, candidate["ref"])
                    require(output["text"] == candidate["value"], "Workbook cell reference differs")
            observation["sampled_cells"] = len(validation["cell_comparison"])
            observation["processed_sheets"] = validation["processed_sheet_names"]
            observation["unread_sheets"] = validation["unread_sheet_names"]
        elif name == "html":
            validation = execution["validation"]
            length = validation["visible_projection_characters"]
            require(validation["derived_html_bytes"] == 74289, "HTML representation size differs")
            require(validation["bom_and_complete_mime_text_match"], "HTML differs from MIME source")
            for anchor in validation["anchors"]:
                require(
                    0 <= anchor["source_visible_start"] < anchor["source_visible_end"] <= length,
                    "HTML anchor range is invalid",
                )
                require(anchor["output_refs"], "HTML anchor lacks an output reference")
                require(
                    any(
                        normalized(anchor["excerpt"]) in normalized(pointer(document, ref)["text"])
                        for ref in anchor["output_refs"]
                    ),
                    "HTML anchor output differs",
                )
            blocked = [
                item for item in case["inventory"] if item["preprocessing_state"] == "blocked"
            ]
            require(
                len(blocked) == 3
                and all(
                    item["read_state"] == "blocked" and item["preprocessing_issues"]
                    for item in blocked
                ),
                "Blocked HTML-email materials were not preserved",
            )
            observation["segments_checked"] = validation["eligible_segments"]
            observation["segments_not_found"] = len(validation["missing_segments"])
            observation["anchors_checked"] = len(validation["anchors"])
        elif name == "text":
            require(
                execution["occurrence_bindings"][0]["representation"] == "provided_text"
                and execution["occurrence_bindings"][0]["original_sha256"] is None,
                "Provided text acquired an invented original",
            )
            require(execution["result"]["page_location"] is None, "Text acquired an invented page")
            require(
                any(
                    item["read_state"] == "blocked"
                    and "not_whitelisted" in item["preprocessing_issue_codes"]
                    for item in case["inventory"]
                ),
                "Unavailable attachment was not retained",
            )
            observation["characters_read"] = execution["result"]["characters_read"]
        observations[name] = observation
    unbound = report["unbound_check"]
    require(
        unbound["reason"] == "execution_configuration_missing" and not unbound["reader_started"],
        "Unbound plan was not blocked",
    )
    require(all(report["p01_manifest_hashes_unchanged"].values()), "A P01 manifest changed")
    return {
        "result": "machine_checks_passed_manual_review_pending"
        if manual_review_pending
        else "passed",
        "report_sha256": sha256(report_bytes),
        "artifacts_checked": len(observations),
        "unbound_plan_not_started": True,
        "p01_manifests_unchanged": report["p01_manifest_hashes_unchanged"],
        "observations": observations,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = verify(args.report)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
    print(text)


if __name__ == "__main__":
    main()
