"""A material snapshot cannot masquerade as a confirmed business decision."""

import copy
import hashlib
import json

import pytest
from test_output_snapshot import partial_case

from doccase_preprocessing.business_result import (
    read_business_result,
    read_projection_assessment,
    record_unavailable_projection,
    save_business_result,
)
from doccase_preprocessing.output_snapshot import SnapshotValidationError, commit_result


def test_business_snapshot_requires_confirmed_decision_and_exact_material_revision(tmp_path):
    material, inputs = partial_case(tmp_path / "material")
    material_store = tmp_path / "material-store"
    saved = commit_result(material_store, material, **inputs)
    baseline = tmp_path / "business-context.json"
    baseline.write_text(json.dumps({"synthetic": True}), encoding="utf-8")
    context = {"ref": str(baseline), "sha256": hashlib.sha256(baseline.read_bytes()).hexdigest()}
    decision = {
        "agent_id": "synthetic-agent",
        "decision_ref": "synthetic:decision-1",
        "task_id": material["material_handoff"]["task_id"],
        "confirmed_at": "2026-11-09T00:00:00Z",
        "intent": {"confirmed": True, "value": "synthetic-inquiry"},
        "inquiry_assignment": {"confirmed": True, "value": [{"id": "synthetic-1"}]},
        "field_applicability": {
            "confirmed": True,
            "value": [{"field": "ctns", "applicable": True}],
        },
    }
    refs = [{"result_id": saved["result_id"], "revision": 1}]
    business_store = tmp_path / "business-store"
    rejected = copy.deepcopy(decision)
    rejected["inquiry_assignment"]["confirmed"] = False
    with pytest.raises(SnapshotValidationError, match="explicitly confirm"):
        save_business_result(
            business_store,
            material_store=material_store,
            material_results=refs,
            context_baseline=context,
            agent_decision=rejected,
        )
    with pytest.raises(SnapshotValidationError, match="readback failed"):
        save_business_result(
            business_store,
            material_store=material_store,
            material_results=[{"result_id": saved["result_id"], "revision": 9}],
            context_baseline=context,
            agent_decision=decision,
        )
    business = save_business_result(
        business_store,
        material_store=material_store,
        material_results=refs,
        context_baseline=context,
        agent_decision=decision,
    )
    readback = read_business_result(business_store, business["business_result_id"], 1)
    assert readback["material_results"] == refs
    assert readback["agent_decision"] == decision
    assessment = record_unavailable_projection(
        tmp_path / "assessments", business_store, business["business_result_id"], 1
    )
    assert assessment["state"] == "blocked"
    assert assessment["reason"] == "approved_v1_mapping_unavailable"
    assert assessment["payload_ref"] is None and assessment["delivery_state"] == "not_queued"
    assert (
        read_projection_assessment(tmp_path / "assessments", assessment["assessment_id"])[
            "business_result_id"
        ]
        == business["business_result_id"]
    )
