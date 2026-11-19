"""Exercise revision baselines and fixed-source correction through the local store."""

import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_main_channel import CONTRACT, ROOT, execute, p02, prepare, profile_for
from test_output_snapshot import context, partial_case, scope_for

from doccase_preprocessing.local_handoff import run_local_handoff
from doccase_preprocessing.main_channel import read_result as read_local_result
from doccase_preprocessing.main_channel import run_main_channel
from doccase_preprocessing.output_reprocess import reprocess_materials
from doccase_preprocessing.output_revision import revise_candidates
from doccase_preprocessing.output_snapshot import (
    SnapshotConflict,
    SnapshotValidationError,
    commit_result,
    read_result,
)
from doccase_preprocessing.rules import evaluate_candidates


def test_fixed_evidence_revision_preserves_old_and_recalculates_dependencies(tmp_path):
    request = json.loads(
        (ROOT / "preprocessing/tests/fixtures/partial-request.json").read_text(encoding="utf-8")
    )
    request["email"]["attachments"] = []
    request["email"]["body_text"] = (
        "L1: 3 cartons, Total G.W. 315 kg; all cartons have the same weight."
    )
    prepared = run_local_handoff(
        json.dumps(request).encode(),
        tmp_path / "p01",
        input_format="json",
        request_reference="local:revision",
    )
    manifest = prepared["received"]["manifest"]
    profile = profile_for(prepared)
    profile["requirements"] = [
        r for r in profile["requirements"] if r["field_name"] != "line_volume"
    ]
    scope = scope_for(manifest, profile, False)
    body = manifest["materials"][0]["material_id"]
    contract = {
        "version": "0.1.0",
        "ref": str(CONTRACT),
        "sha256": hashlib.sha256(CONTRACT.read_bytes()).hexdigest(),
    }
    result = run_main_channel(
        prepared["manifest_path"],
        tmp_path / "p01",
        expected_task_id=manifest["task_id"],
        expected_request_fingerprint=prepared["request_fingerprint"],
        intake_context=prepared["intake_context"],
        selected_material_ids={body},
        read_configurations=p02.configurations(),
        reader_versions={},
        field_profile=profile,
        task_scope=scope,
        field_contract_ref=contract,
        output_dir=tmp_path / "main",
        reader=p02.parse_prepared,
    )
    first = read_local_result(result["result_ref"])
    inputs = context(prepared, profile, scope, result)
    store = tmp_path / "store"
    saved = commit_result(store, first, **inputs)
    rid = saved["result_id"]
    fields = {item["field_name"]: item for item in first["material_handoff"]["candidates"]}
    assert fields["unit_gross_weight"]["field"]["value"] == 105
    cid = fields["ctns"]["candidate_id"]
    mark_uncertain = [{"candidate_id": cid, "status": "uncertain", "reason": "count needs review"}]
    second_ref = revise_candidates(
        store, rid, base_revision=1, reviewer="reviewer", changes=mark_uncertain
    )
    assert second_ref["revision"] == 2
    second = read_result(store, rid, 2)
    fields2 = {item["field_name"]: item for item in second["material_handoff"]["candidates"]}
    assert fields2["ctns"]["field"]["value"] is None
    assert fields2["unit_gross_weight"]["field"]["value"] is None
    assert second["material_handoff"]["handoff_state"] == "partial"
    assert read_result(store, rid, 1) == first

    with pytest.raises(SnapshotConflict, match="stale"):
        revise_candidates(store, rid, base_revision=1, reviewer="reviewer", changes=mark_uncertain)
    with pytest.raises(ValueError, match="fixed source evidence"):
        revise_candidates(
            store,
            rid,
            base_revision=2,
            reviewer="reviewer",
            changes=[{"candidate_id": cid, "status": "known", "value": 4, "reason": "unsupported"}],
        )
    third_ref = revise_candidates(
        store,
        rid,
        base_revision=2,
        reviewer="reviewer",
        changes=[
            {"candidate_id": cid, "status": "known", "value": 3, "reason": "source confirmed"}
        ],
    )
    assert third_ref["revision"] == 3
    third = read_result(store, rid, 3)
    fields3 = {item["field_name"]: item for item in third["material_handoff"]["candidates"]}
    assert fields3["ctns"]["field"]["source_ids"] == fields["ctns"]["field"]["source_ids"]
    assert fields3["unit_gross_weight"]["field"]["value"] == 105
    assert third["material_handoff"]["handoff_state"] == "complete"
    assert read_result(store, rid, 2) == second


def test_replaced_material_reprepares_rereads_and_preserves_prior_revision(tmp_path):
    first, inputs = partial_case(tmp_path / "initial")
    store = tmp_path / "store"
    saved = commit_result(store, first, **inputs)
    rid = saved["result_id"]
    request = json.loads(
        (ROOT / "preprocessing/tests/fixtures/partial-request.json").read_text(encoding="utf-8")
    )
    request["email"]["attachments"][0] = {
        "filename": "M2-packing-list.txt",
        "mime": "text/plain",
        "content_text": "L1: 5 cartons",
    }

    def supply_requirements(manifest):
        profile = copy.deepcopy(inputs["field_profile"])
        attachment = next(item for item in manifest["materials"] if item["kind"] == "attachment")
        mid = attachment["material_id"]
        profile["requirements"].append(
            {
                "requirement_id": "attachment-count",
                "field_name": "ctns",
                "material_id": mid,
                "object_selector": mid + "/L1",
                "object_anchor": "L1",
                "source_kind": "attachment",
                "critical": True,
                "missing_policy": "block",
                "allow_not_applicable": False,
                "scope_dependencies": [{"stage": "extraction", "refs": [mid + "/representation"]}],
            }
        )
        return {
            "field_profile": profile,
            "task_scope": scope_for(manifest, profile, True),
            "selected_material_ids": [
                item["material_id"] for item in manifest["materials"] if item["state"] == "ready"
            ],
        }

    def omit_changed_requirement(manifest):
        supplied = supply_requirements(manifest)
        supplied["field_profile"]["requirements"].pop()
        return supplied

    with pytest.raises(ValueError, match="independent extraction requirement"):
        reprocess_materials(
            store,
            rid,
            base_revision=1,
            raw=json.dumps(request).encode(),
            input_format="json",
            request_reference="local:omitted-extraction",
            output_dir=tmp_path / "omitted",
            select_and_require=omit_changed_requirement,
            reader=p02.parse_prepared,
            read_configurations=p02.configurations(),
            reader_versions={},
            field_contract_ref=inputs["field_contract_ref"],
        )

    revised = reprocess_materials(
        store,
        rid,
        base_revision=1,
        raw=json.dumps(request).encode(),
        input_format="json",
        request_reference="local:material-replacement",
        output_dir=tmp_path / "replacement",
        select_and_require=supply_requirements,
        reader=p02.parse_prepared,
        read_configurations=p02.configurations(),
        reader_versions={},
        field_contract_ref=inputs["field_contract_ref"],
    )
    assert revised["revision"] == 2
    assert len(revised["affected_material_ids"]) == 1
    second = read_result(store, rid, 2)
    mid = revised["affected_material_ids"][0]
    material = next(
        item
        for item in second["material_handoff"]["material_versions"]
        if item["material_id"] == mid
    )
    assert material["material_revision"] == 2
    assert material["representation_revision"] == 2
    assert (
        next(item for item in second["per_material_results"] if item["material_id"] == mid)[
            "read_state"
        ]
        == "success"
    )
    assert (
        next(
            item
            for item in second["material_handoff"]["candidates"]
            if item["object_id"] == mid + "/L1"
        )["field"]["value"]
        == 5
    )
    assert read_result(store, rid, 1) == first
    assert (
        next(
            item
            for item in first["material_handoff"]["material_versions"]
            if item["material_id"] == mid
        )["availability"]
        == "missing"
    )
    with pytest.raises(SnapshotConflict, match="stale"):
        reprocess_materials(
            store,
            rid,
            base_revision=1,
            raw=json.dumps(request).encode(),
            input_format="json",
            request_reference="local:material-replacement",
            output_dir=tmp_path / "stale",
            select_and_require=supply_requirements,
            reader=p02.parse_prepared,
            read_configurations=p02.configurations(),
            reader_versions={},
            field_contract_ref=inputs["field_contract_ref"],
        )
    assert not (tmp_path / "stale").exists()


def test_added_material_restarts_read_and_preserves_profile_missing_read(tmp_path):
    first, inputs = partial_case(tmp_path / "initial")
    store = tmp_path / "store"
    saved = commit_result(store, first, **inputs)
    rid = saved["result_id"]
    request = json.loads(
        (ROOT / "preprocessing/tests/fixtures/partial-request.json").read_text(encoding="utf-8")
    )
    request["email"]["attachments"].append(
        {"filename": "M3-addition.txt", "mime": "text/plain", "content_text": "L1: 5 cartons"}
    )

    def requirements(manifest):
        profile = copy.deepcopy(inputs["field_profile"])
        attachment = next(
            item for item in manifest["materials"] if item["filename"] == "M3-addition.txt"
        )
        mid = attachment["material_id"]
        profile["requirements"].append(
            {
                "requirement_id": "added-attachment-count",
                "field_name": "ctns",
                "material_id": mid,
                "object_selector": mid + "/L1",
                "object_anchor": "L1",
                "source_kind": "attachment",
                "critical": True,
                "missing_policy": "block",
                "allow_not_applicable": False,
                "scope_dependencies": [{"stage": "extraction", "refs": [mid + "/representation"]}],
            }
        )
        return {
            "field_profile": profile,
            "task_scope": scope_for(manifest, profile, True),
            "selected_material_ids": [
                item["material_id"] for item in manifest["materials"] if item["state"] == "ready"
            ],
        }

    def without_profile(manifest):
        result = requirements(manifest)
        result["field_profile"] = None
        return result

    args = {
        "base_revision": 1,
        "raw": json.dumps(request, ensure_ascii=False).encode(),
        "input_format": "json",
        "reader": p02.parse_prepared,
        "read_configurations": p02.configurations(),
        "reader_versions": {},
        "field_contract_ref": inputs["field_contract_ref"],
    }
    unavailable = reprocess_materials(
        store,
        rid,
        **args,
        request_reference="local:added-without-profile",
        output_dir=tmp_path / "missing-profile",
        select_and_require=without_profile,
    )
    assert unavailable["reason"] == "required_field_profile_missing"
    assert read_local_result(unavailable["progress_ref"])["extraction"] == {
        "requested": True,
        "stage_state": "not_started",
        "reason": "required_field_profile_missing",
    }
    mid = unavailable["affected_material_ids"][0]
    read = read_local_result(unavailable["read_ref"])
    entry = next(item for item in read["inventory"] if item["material_id"] == mid)
    execution = next(
        item for item in read["executions"] if item["execution_id"] == entry["execution_id"]
    )
    assert execution["reader_started"] and execution["execution_state"] == "success"
    artifact = json.loads(Path(execution["ref"]).read_text(encoding="utf-8"))
    assert artifact["text"] == "L1: 5 cartons"
    assert (
        hashlib.sha256(Path(execution["ref"]).read_bytes()).hexdigest()
        == execution["artifact_hash"]
    )
    assert read_result(store, rid, 1) == first
    with pytest.raises(SnapshotValidationError, match="readback failed"):
        read_result(store, rid, 2)

    revised = reprocess_materials(
        store,
        rid,
        **args,
        request_reference="local:added-material",
        output_dir=tmp_path / "addition",
        select_and_require=requirements,
    )
    assert revised["revision"] == 2
    assert revised["affected_material_ids"] == [mid]
    assert revised["removed_material_ids"] == []
    second = read_result(store, rid, 2)
    assert mid not in {
        item["material_id"] for item in first["material_handoff"]["material_versions"]
    }
    added = next(
        item
        for item in second["material_handoff"]["material_versions"]
        if item["material_id"] == mid
    )
    assert (added["material_revision"], added["representation_revision"]) == (1, 1)
    versions = {
        item["material_id"]: item for item in second["material_handoff"]["material_versions"]
    }
    for previous in first["material_handoff"]["material_versions"]:
        updated = versions[previous["material_id"]]
        assert updated["material_revision"] == previous["material_revision"]
        assert updated["representation_revision"] == previous["representation_revision"]
        assert updated["availability"] == previous["availability"]
    candidate = next(
        item
        for item in second["material_handoff"]["candidates"]
        if item["object_id"] == mid + "/L1"
    )
    assert candidate["field"]["value"] == 5
    read = read_local_result(revised["read_ref"])
    entry = next(item for item in read["inventory"] if item["material_id"] == mid)
    execution = next(
        item for item in read["executions"] if item["execution_id"] == entry["execution_id"]
    )
    assert execution["reader_started"] and execution["execution_state"] == "success"
    assert read_result(store, rid, 1) == first


def test_concurrent_same_base_publishes_only_one_revision(tmp_path):
    snapshot, inputs = partial_case(tmp_path / "initial")
    store = tmp_path / "store"
    saved = commit_result(store, snapshot, **inputs)
    candidate = next(
        c for c in snapshot["material_handoff"]["candidates"] if c["field_name"] == "ctns"
    )

    def review_once():
        try:
            return revise_candidates(
                store,
                saved["result_id"],
                base_revision=1,
                reviewer="synthetic",
                changes=[
                    {
                        "candidate_id": candidate["candidate_id"],
                        "status": "uncertain",
                        "reason": "concurrent review",
                    }
                ],
            )["revision"]
        except SnapshotConflict:
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: review_once(), range(2)))
    assert outcomes.count(2) == 1
    assert outcomes.count("stale") == 1
    assert read_result(store, saved["result_id"], 1) == snapshot
    assert read_result(store, saved["result_id"], 2)["material_handoff"]["base_revision"] == 1


def test_rules_do_not_derive_from_one_selected_value_while_another_conflicts(tmp_path):
    prepared = prepare(
        tmp_path / "p01",
        "L1: 3 cartons, Total G.W. 315 kg; Total G.W. 318 kg; all cartons have the same weight.",
    )
    profile = profile_for(prepared)
    result = execute(tmp_path / "main", prepared, profile)
    snapshot = read_local_result(result["result_ref"])
    inputs = context(
        prepared, profile, scope_for(prepared["received"]["manifest"], profile, True), result
    )
    gross = [
        item
        for item in snapshot["material_handoff"]["candidates"]
        if item["field_name"] == "line_gross_weight"
    ]
    assert len(gross) == 2
    assert all(item["field"]["status"] == "conflict" for item in gross)
    store = tmp_path / "store"
    saved = commit_result(store, snapshot, **inputs)
    revise_candidates(
        store,
        saved["result_id"],
        base_revision=1,
        reviewer="synthetic",
        changes=[
            {
                "candidate_id": gross[0]["candidate_id"],
                "status": "known",
                "value": 315,
                "reason": "reviewed one source",
            }
        ],
    )
    revised = read_result(store, saved["result_id"], 2)["material_handoff"]
    remaining = next(
        item for item in revised["candidates"] if item["candidate_id"] == gross[1]["candidate_id"]
    )
    assert remaining["field"]["status"] == "conflict"
    assert evaluate_candidates(revised["candidates"], gross[0]["object_id"]) is None
