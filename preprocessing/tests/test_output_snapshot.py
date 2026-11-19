"""Check the independent consumer and immutable local result boundary."""

import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from test_main_channel import CONTRACT, ROOT, execute, p02, prepare, profile_for

from doccase_preprocessing.local_handoff import run_local_handoff
from doccase_preprocessing.main_channel import read_result as read_local_result
from doccase_preprocessing.main_channel import run_main_channel
from doccase_preprocessing.output_consume import assess_handoff
from doccase_preprocessing.output_snapshot import (
    SnapshotConflict,
    SnapshotStorageError,
    SnapshotValidationError,
    commit_result,
    read_result,
)

OWNERS = {
    "acquisition": "preprocessing",
    "parsing": "document_reader",
    "extraction": "field_extractor",
    "aggregation": "rule_validator",
}


def scope_for(manifest, profile, unknown):
    ids = [item["material_id"] for item in manifest["materials"]]
    return {
        "task_id": manifest["task_id"],
        "email_ref_id": manifest["emails"][0]["email_ref_id"],
        "scope_ref": profile["task_scope_ref"],
        "material_ids": ids,
        "required_stages": list(OWNERS),
        "original_requirement": "explicit_representation_only",
        "expected_scopes": {
            stage: {
                "scope_ref": manifest["task_id"] + ":" + stage,
                "scope_owner": owner,
                "expected_refs": [mid + "/representation" for mid in ids],
                "unknown_scope": unknown,
            }
            for stage, owner in OWNERS.items()
        },
    }


def context(prepared, profile, scope, result):
    manifest_path = prepared["manifest_path"]
    return {
        "task_scope": scope,
        "manifest_ref": {
            "ref": str(manifest_path),
            "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        },
        "field_profile": profile,
        "read_ref": result["read_ref"],
        "content_root": manifest_path.parent.parent,
        "field_contract_ref": {
            "version": "0.1.0",
            "ref": str(CONTRACT),
            "sha256": hashlib.sha256(CONTRACT.read_bytes()).hexdigest(),
        },
    }


def partial_case(tmp_path):
    prepared = prepare(tmp_path / "p01")
    manifest = prepared["received"]["manifest"]
    profile = profile_for(prepared)
    result = execute(tmp_path / "main", prepared, profile)
    return read_local_result(result["result_ref"]), context(
        prepared, profile, scope_for(manifest, profile, True), result
    )


def test_consumer_rejects_independent_scope_profile_and_state_tampering(tmp_path):
    snapshot, inputs = partial_case(tmp_path)
    assert assess_handoff(snapshot, **inputs)["handoff_state"] == "partial"

    changed = copy.deepcopy(inputs)
    changed["task_scope"]["material_ids"].pop()
    with pytest.raises(ValueError, match="full independent material inventory"):
        assess_handoff(snapshot, **changed)

    changed = copy.deepcopy(inputs)
    changed["field_profile"]["requirements"].pop()
    with pytest.raises(ValueError, match="Independent field profile"):
        assess_handoff(snapshot, **changed)

    changed_snapshot = copy.deepcopy(snapshot)
    changed_snapshot["material_handoff"]["handoff_state"] = "complete"
    with pytest.raises(ValueError, match="Complete handoff"):
        assess_handoff(changed_snapshot, **inputs)

    changed_snapshot = copy.deepcopy(snapshot)
    changed_snapshot["material_handoff"]["candidates"][0]["field"]["value"] = 999
    with pytest.raises(ValueError, match="does not match fixed source evidence"):
        assess_handoff(changed_snapshot, **inputs)

    changed_snapshot = copy.deepcopy(snapshot)
    changed_snapshot["material_handoff"]["source_bindings"][0]["origin_locator"] = {
        "char_start": 0,
        "char_end": 1,
    }
    with pytest.raises(ValueError, match="Original evidence locator"):
        assess_handoff(changed_snapshot, **inputs)

    changed_snapshot = copy.deepcopy(snapshot)
    changed_snapshot["per_material_results"][-1]["preprocessing_issues"] = []
    with pytest.raises(ValueError, match="unread reasons"):
        assess_handoff(changed_snapshot, **inputs)


def complete_case(tmp_path):
    request = json.loads(
        (ROOT / "preprocessing/tests/fixtures/partial-request.json").read_text(encoding="utf-8")
    )
    request["email"]["attachments"] = []
    request["email"]["body_text"] = "L1: 3 cartons, Total G.W. 315 kg."
    prepared = run_local_handoff(
        json.dumps(request).encode(),
        tmp_path / "p01",
        input_format="json",
        request_reference="local:test",
    )
    manifest = prepared["received"]["manifest"]
    profile = profile_for(prepared)
    profile["requirements"] = [
        r for r in profile["requirements"] if r["field_name"] in {"ctns", "line_gross_weight"}
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
    snapshot = read_local_result(result["result_ref"])
    return snapshot, context(prepared, profile, scope, result), result


def test_complete_is_limited_to_an_explicit_scope_that_was_actually_read(tmp_path):
    snapshot, inputs, result = complete_case(tmp_path)
    assert result["status"] == "complete"
    assert assess_handoff(snapshot, **inputs)["validated"]
    assert all(c["state"] == "complete" for c in snapshot["material_handoff"]["coverage"].values())

    forged = copy.deepcopy(snapshot)
    forged["material_handoff"]["requirement_assessments"][0]["state"] = "unresolved"
    with pytest.raises(ValueError, match="Complete handoff"):
        assess_handoff(forged, **inputs)


def test_snapshots_are_immutable_and_recheck_evidence_on_read(tmp_path):
    snapshot, inputs = partial_case(tmp_path)
    saved = commit_result(tmp_path / "store", snapshot, **inputs)
    assert read_result(tmp_path / "store", saved["result_id"], 1) == snapshot
    with pytest.raises(SnapshotConflict, match="already exists"):
        commit_result(tmp_path / "store", snapshot, **inputs)
    artifact = Path(snapshot["material_handoff"]["artifact_refs"][0]["ref"])
    artifact.write_bytes(artifact.read_bytes() + b" ")
    with pytest.raises(SnapshotValidationError, match="artifact hash mismatch"):
        read_result(tmp_path / "store", saved["result_id"], 1)


def test_publish_failure_is_explicit_and_does_not_publish_partial_json(tmp_path, monkeypatch):
    snapshot, inputs = partial_case(tmp_path)

    def fail_link(*_args):
        raise PermissionError("simulated link failure")

    monkeypatch.setattr("doccase_preprocessing.output_snapshot.os.link", fail_link)
    with pytest.raises(SnapshotStorageError, match="simulated link failure"):
        commit_result(tmp_path / "store", snapshot, **inputs)
    assert not list((tmp_path / "store").rglob("*.json"))
    assert not list((tmp_path / "store").rglob("*.tmp"))


def test_snapshot_does_not_change_if_caller_mutates_during_commit(tmp_path, monkeypatch):
    snapshot, inputs = partial_case(tmp_path)
    original_scope_ref = inputs["task_scope"]["scope_ref"]
    original_assess = assess_handoff
    validated, resume = Event(), Event()

    def pause_after_validation(candidate, **context):
        assessment = original_assess(candidate, **context)
        validated.set()
        if not resume.wait(5):
            raise RuntimeError("Test did not resume the commit")
        return assessment

    monkeypatch.setattr(
        "doccase_preprocessing.output_snapshot.assess_handoff", pause_after_validation
    )
    store = tmp_path / "store"
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(commit_result, store, snapshot, **inputs)
        try:
            assert validated.wait(5)
            count = next(
                item
                for item in snapshot["material_handoff"]["candidates"]
                if item["field_name"] == "ctns"
            )
            assert count["field"]["value"] == 3
            count["field"]["value"] = 999
            inputs["task_scope"]["scope_ref"] = "changed-after-validation"
        finally:
            resume.set()
        saved = future.result(timeout=5)
    monkeypatch.setattr("doccase_preprocessing.output_snapshot.assess_handoff", original_assess)
    readback = read_result(store, saved["result_id"], 1)
    count = next(
        item for item in readback["material_handoff"]["candidates"] if item["field_name"] == "ctns"
    )
    assert count["field"]["value"] == 3
    envelope = json.loads(Path(saved["snapshot_ref"]).read_text(encoding="utf-8"))
    assert envelope["payload"]["validation_inputs"]["task_scope"]["scope_ref"] == original_scope_ref


def test_consumer_rejects_calculated_value_that_disagrees_with_fixed_inputs(tmp_path):
    prepared = prepare(
        tmp_path / "p01",
        "L1: 3 cartons, Total G.W. 315 kg; all cartons have the same weight.",
    )
    profile = profile_for(prepared)
    result = execute(tmp_path / "main", prepared, profile)
    snapshot = read_local_result(result["result_ref"])
    inputs = context(
        prepared, profile, scope_for(prepared["received"]["manifest"], profile, True), result
    )
    calculated = next(
        item
        for item in snapshot["material_handoff"]["candidates"]
        if item["field_name"] == "unit_gross_weight"
    )
    assert calculated["field"]["value"] == 105
    calculated["field"]["value"] = 999
    with pytest.raises(ValueError, match="Calculated candidate"):
        assess_handoff(snapshot, **inputs)


@pytest.mark.parametrize("tamper", ["input_paths", "derived_from", "equal_weight_evidence"])
def test_consumer_rejects_calculated_candidate_with_wrong_dependencies(tmp_path, tamper):
    prepared = prepare(
        tmp_path / "p01",
        "L1: 3 cartons, Total G.W. 315 kg; all cartons have the same weight.",
    )
    profile = profile_for(prepared)
    result = execute(tmp_path / "main", prepared, profile)
    snapshot = read_local_result(result["result_ref"])
    inputs = context(
        prepared, profile, scope_for(prepared["received"]["manifest"], profile, True), result
    )
    calculated = next(
        item
        for item in snapshot["material_handoff"]["candidates"]
        if item["field_name"] == "unit_gross_weight"
    )
    if tamper == "input_paths":
        calculated["field"]["input_paths"] = ["/candidates/0/field", "/candidates/0/field"]
    elif tamper == "derived_from":
        calculated["derived_from"][0] = calculated["candidate_id"]
    else:
        calculated["uniform_per_item_evidence"] = calculated["field"]["source_ids"][0]
    with pytest.raises(ValueError, match="Calculated candidate"):
        assess_handoff(snapshot, **inputs)


@pytest.mark.parametrize(
    ("attribute", "value"),
    [("quantity_scope", "per_item"), ("quantity_kind", "net_weight")],
)
def test_consumer_rejects_relabelled_line_gross_weight_semantics(tmp_path, attribute, value):
    snapshot, inputs = partial_case(tmp_path)
    gross = next(
        item
        for item in snapshot["material_handoff"]["candidates"]
        if item["field_name"] == "line_gross_weight"
    )
    assert gross["field"]["value"] == 315
    gross[attribute] = value
    with pytest.raises(ValueError, match="Candidate quantity semantics"):
        assess_handoff(snapshot, **inputs)


def test_consumer_rejects_carton_count_relabelled_as_pallets(tmp_path):
    snapshot, inputs = partial_case(tmp_path)
    cartons = next(
        item for item in snapshot["material_handoff"]["candidates"] if item["field_name"] == "ctns"
    )
    cartons["packaging_level"] = "pallet"
    with pytest.raises(ValueError, match="Candidate packaging level"):
        assess_handoff(snapshot, **inputs)


def test_consumer_rejects_evidence_from_another_object_in_same_material(tmp_path):
    prepared = prepare(
        tmp_path / "p01",
        "L1: 3 cartons, Total G.W. 315 kg.\nL2: 4 cartons, Total G.W. 318 kg.",
    )
    profile = profile_for(prepared)
    second = copy.deepcopy(profile["requirements"][0])
    second["requirement_id"] = "count-l2"
    second["object_selector"] = second["material_id"] + "/L2"
    second["object_anchor"] = "L2"
    profile["requirements"].append(second)
    result = execute(tmp_path / "main", prepared, profile)
    snapshot = read_local_result(result["result_ref"])
    inputs = context(
        prepared, profile, scope_for(prepared["received"]["manifest"], profile, True), result
    )
    candidates = snapshot["material_handoff"]["candidates"]
    first = next(
        c for c in candidates if c["field_name"] == "ctns" and c["object_id"].endswith("/L1")
    )
    second = next(
        c for c in candidates if c["field_name"] == "ctns" and c["object_id"].endswith("/L2")
    )
    assert first["field"]["value"] == 3 and second["field"]["value"] == 4
    first["field"] = copy.deepcopy(second["field"])
    with pytest.raises(ValueError, match="independent object scope"):
        assess_handoff(snapshot, **inputs)


def test_consumer_rejects_suppressed_ambiguous_object_scope(tmp_path):
    prepared = prepare(
        tmp_path / "p01",
        "L1: 3 cartons, Total G.W. 315 kg.\nL1: Please quote. L2: 4 cartons, Total G.W. 318 kg.",
    )
    profile = profile_for(prepared)
    result = execute(tmp_path / "main", prepared, profile)
    snapshot = read_local_result(result["result_ref"])
    inputs = context(
        prepared, profile, scope_for(prepared["received"]["manifest"], profile, True), result
    )
    handoff = snapshot["material_handoff"]
    removed = {i["issue_id"] for i in handoff["issues"] if i["code"] == "object_scope_ambiguous"}
    assert removed
    handoff["issues"] = [i for i in handoff["issues"] if i["issue_id"] not in removed]
    for assessment in handoff["requirement_assessments"]:
        assessment["issue_ids"] = [iid for iid in assessment["issue_ids"] if iid not in removed]
        if assessment["requirement_id"] in {"count", "gross"}:
            assessment["state"] = "checked_present"
    for material in snapshot["per_material_results"]:
        material["issue_ids"] = [iid for iid in material["issue_ids"] if iid not in removed]
    with pytest.raises(ValueError, match="Ambiguous object scope"):
        assess_handoff(snapshot, **inputs)
