"""The V1.0 draft preview must block information it cannot faithfully express."""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest
from test_output_snapshot import complete_case, partial_case

from doccase_preprocessing.business_result import save_business_result
from doccase_preprocessing.output_project import project_business_result_draft, read_draft_preview
from doccase_preprocessing.output_revision import revise_candidates
from doccase_preprocessing.output_snapshot import commit_result

MOCK_SPEC = importlib.util.spec_from_file_location(
    "local_v1_mock", Path(__file__).resolve().parents[1] / "examples/local_v1_mock.py"
)
local_v1_mock = importlib.util.module_from_spec(MOCK_SPEC)
MOCK_SPEC.loader.exec_module(local_v1_mock)


def prepare_business(tmp_path, *, partial=False, decision_transform=None):
    snapshot, inputs, *_ = (
        partial_case(tmp_path / "material") if partial else complete_case(tmp_path / "material")
    )
    material_store = tmp_path / "material-store"
    material = commit_result(material_store, snapshot, **inputs)
    baseline_path = tmp_path / "context.json"
    baseline_path.write_text('{"synthetic":true}', encoding="utf-8")
    baseline = {
        "ref": str(baseline_path),
        "sha256": hashlib.sha256(baseline_path.read_bytes()).hexdigest(),
    }
    interface_path = tmp_path / "interface.json"
    interface_path.write_text(
        json.dumps(
            {
                "data": {
                    "document": {"document_id": "synthetic-interface-document", "revision_id": 1}
                }
            }
        ),
        encoding="utf-8",
    )
    interface = {
        "ref": str(interface_path),
        "sha256": hashlib.sha256(interface_path.read_bytes()).hexdigest(),
    }
    decision = {
        "agent_id": "synthetic",
        "decision_ref": "synthetic:confirmed",
        "task_id": snapshot["material_handoff"]["task_id"],
        "confirmed_at": "2026-11-09T00:00:00Z",
        "intent": {"confirmed": True, "value": "inquiry"},
        "inquiry_assignment": {
            "confirmed": True,
            "value": [
                {
                    "inquiry": {
                        "requirements": [
                            {
                                "ctns": {"value": 3, "confidence": None},
                                "gross_weight": {"value": 315, "confidence": None},
                                "cargo_lines": [],
                            }
                        ]
                    }
                }
            ],
        },
        "field_applicability": {
            "confirmed": True,
            "value": [
                {"path": "requirements[0].ctns", "state": "known"},
                {"path": "requirements[0].gross_weight", "state": "known"},
            ],
        },
    }
    if decision_transform:
        decision_transform(decision)
    business_store = tmp_path / "business-store"
    business = save_business_result(
        business_store,
        material_store=material_store,
        material_results=[{"result_id": material["result_id"], "revision": 1}],
        context_baseline=baseline,
        agent_decision=decision,
    )
    return snapshot, material, material_store, business, business_store, interface


def test_representable_confirmed_result_produces_only_a_persisted_draft(tmp_path):
    _, _, _, business, business_store, interface = prepare_business(tmp_path)
    root = tmp_path / "projection"
    assessed = project_business_result_draft(
        root, business_store, business["business_result_id"], 1, interface_ref=interface
    )
    assert assessed["state"] == "draft_preview"
    assert assessed["reason"] == "v1_mapping_pending_bilateral_review"
    assert assessed["delivery_state"] == "not_queued"
    preview = read_draft_preview(root, assessed["payload_ref"]["projection_id"])
    assert preview["kind"] == "unapproved_draft_preview"
    assert json.loads(preview["wire_payload_json"]) == preview["wire_payload"]
    assert (
        hashlib.sha256(preview["wire_payload_json"].encode("utf-8")).hexdigest()
        == preview["wire_sha256"]
    )
    assert preview["wire_payload"]["result"] == {"state": "succeeded"}
    assert preview["wire_payload"]["inquiry"]["requirements"][0]["gross_weight"]["value"] == 315
    assert preview["wire_payload"]["missing_fields"] == []


def test_partial_multi_inquiry_and_unread_are_explicitly_blocked(tmp_path):
    _, _, _, business, business_store, interface = prepare_business(
        tmp_path / "partial", partial=True
    )
    assessed = project_business_result_draft(
        tmp_path / "partial" / "projection",
        business_store,
        business["business_result_id"],
        1,
        interface_ref=interface,
    )
    assert assessed["reason"] == "partial_material_unrepresentable"
    assert assessed["payload_ref"] is None

    def multiple(decision):
        decision["inquiry_assignment"]["value"].append(
            copy.deepcopy(decision["inquiry_assignment"]["value"][0])
        )

    _, _, _, business, business_store, interface = prepare_business(
        tmp_path / "multiple", decision_transform=multiple
    )
    assessed = project_business_result_draft(
        tmp_path / "multiple" / "projection",
        business_store,
        business["business_result_id"],
        1,
        interface_ref=interface,
    )
    assert assessed["reason"] == "multiple_inquiries_unrepresentable"

    def unread(decision):
        decision["field_applicability"]["value"][0]["state"] = "unread"

    _, _, _, business, business_store, interface = prepare_business(
        tmp_path / "unread", decision_transform=unread
    )
    assessed = project_business_result_draft(
        tmp_path / "unread" / "projection",
        business_store,
        business["business_result_id"],
        1,
        interface_ref=interface,
    )
    assert assessed["reason"] == "field_state_unrepresentable"
    assert assessed["payload_ref"] is None


def test_material_revision_change_requires_new_business_confirmation(tmp_path):
    snapshot, material, material_store, business, business_store, interface = prepare_business(
        tmp_path
    )
    count = next(
        item for item in snapshot["material_handoff"]["candidates"] if item["field_name"] == "ctns"
    )
    revise_candidates(
        material_store,
        material["result_id"],
        base_revision=1,
        reviewer="synthetic",
        changes=[
            {"candidate_id": count["candidate_id"], "status": "uncertain", "reason": "review"}
        ],
    )
    assessed = project_business_result_draft(
        tmp_path / "projection",
        business_store,
        business["business_result_id"],
        1,
        interface_ref=interface,
    )
    assert assessed["reason"] == "business_material_revision_stale"
    assert assessed["payload_ref"] is None


def test_stale_preview_stays_readable_but_cannot_enter_local_query_or_callback(tmp_path):
    snapshot, material, material_store, business, business_store, interface = prepare_business(
        tmp_path
    )
    root = tmp_path / "projection"
    assessed = project_business_result_draft(
        root, business_store, business["business_result_id"], 1, interface_ref=interface
    )
    projection_id = assessed["payload_ref"]["projection_id"]
    historical = read_draft_preview(root, projection_id)
    count = next(
        item for item in snapshot["material_handoff"]["candidates"] if item["field_name"] == "ctns"
    )
    revise_candidates(
        material_store,
        material["result_id"],
        base_revision=1,
        reviewer="synthetic",
        changes=[
            {"candidate_id": count["candidate_id"], "status": "uncertain", "reason": "review"}
        ],
    )
    assert read_draft_preview(root, projection_id) == historical
    receiver = tmp_path / "receiver.json"
    with pytest.raises(ValueError, match="business_material_revision_stale"):
        local_v1_mock.exercise(root, assessed["assessment_id"], receiver)
    assert not receiver.exists()


@pytest.mark.parametrize("change_before", ["GET", "POST"])
def test_local_http_rechecks_material_revision_for_each_request(
    tmp_path, monkeypatch, change_before
):
    snapshot, material, material_store, business, business_store, interface = prepare_business(
        tmp_path
    )
    root = tmp_path / "projection"
    assessed = project_business_result_draft(
        root, business_store, business["business_result_id"], 1, interface_ref=interface
    )
    count = next(
        item for item in snapshot["material_handoff"]["candidates"] if item["field_name"] == "ctns"
    )
    open_request = local_v1_mock.urlopen
    changed = False

    def revise_before_request(request, *args, **kwargs):
        nonlocal changed
        if request.get_method() == change_before and not changed:
            changed = True
            revise_candidates(
                material_store,
                material["result_id"],
                base_revision=1,
                reviewer="synthetic",
                changes=[
                    {
                        "candidate_id": count["candidate_id"],
                        "status": "uncertain",
                        "reason": "review",
                    }
                ],
            )
        return open_request(request, *args, **kwargs)

    monkeypatch.setattr(local_v1_mock, "urlopen", revise_before_request)
    receiver = tmp_path / "receiver.json"
    with pytest.raises(HTTPError) as failure:
        local_v1_mock.exercise(root, assessed["assessment_id"], receiver)
    assert changed and failure.value.code == 409
    assert json.loads(failure.value.read())["code"] == "business_material_revision_stale"
    assert not receiver.exists()
    assert (
        read_draft_preview(root, assessed["payload_ref"]["projection_id"])["kind"]
        == "unapproved_draft_preview"
    )
