"""Check evidence-preserving boundaries of the prior PoC bridge."""

import copy
import hashlib
import json

import pytest
from test_output_snapshot import complete_case, partial_case

from doccase_preprocessing.business_result import (
    _write_new,
    read_business_result,
    save_business_result,
)
from doccase_preprocessing.emailbridge_local import material_sources, reviewed_business_decision
from doccase_preprocessing.output_snapshot import SnapshotValidationError, _path, commit_result


def test_bridge_requires_confirmed_job_exact_sources_and_explicit_field_mapping(tmp_path):
    snapshot, inputs, _ = complete_case(tmp_path / "complete")
    store = tmp_path / "store"
    saved = commit_result(store, snapshot, **inputs)
    _, sources, bindings = material_sources(store, saved["result_id"], 1)
    sid = sources[0]["id"]

    def cell(value, quote):
        return {"value": value, "source_id": sid, "quote": quote, "provenance": "extracted"}

    job = {
        "id": "synthetic-job",
        "revision": 1,
        "status": "confirmed",
        "confirmed_at": "2026-11-09T00:00:00Z",
        "sources": sources,
        "issues": [],
        "result": {
            "notes": [],
            "inquiries": [
                {
                    "fields": {},
                    "totals": {
                        "package_count": cell("3", "3 cartons"),
                        "gross_kg": cell("315", "Total G.W. 315 kg"),
                    },
                    "cargo": [{}],
                    "weight_breaks": [],
                }
            ],
        },
    }
    mappings = {
        "requirements[0].ctns": "inquiries.0.totals.package_count",
        "requirements[0].gross_weight": "inquiries.0.totals.gross_kg",
    }
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(
        json.dumps(
            {
                "job": job,
                "source_bindings": bindings,
                "mappings": mappings,
                "reviewer": "synthetic",
                "confirmed_intent": "inquiry",
            }
        ),
        encoding="utf-8",
    )
    evidence = {
        "ref": str(evidence_path),
        "sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
    }
    kwargs = {
        "material_store": store,
        "result_id": saved["result_id"],
        "revision": 1,
        "reviewer": "synthetic",
        "confirmed_intent": "inquiry",
        "mappings": mappings,
        "agent_evidence": evidence,
    }
    decision = reviewed_business_decision(job, **kwargs)
    assert (
        decision["inquiry_assignment"]["value"][0]["inquiry"]["requirements"][0]["gross_weight"][
            "value"
        ]
        == 315
    )

    unconfirmed = copy.deepcopy(job)
    unconfirmed["status"] = "review"
    with pytest.raises(ValueError, match="confirmed EmailBridge job"):
        reviewed_business_decision(unconfirmed, **kwargs)
    with pytest.raises(ValueError, match="confirmed EmailBridge job"):
        reviewed_business_decision(job, **{**kwargs, "confirmed_intent": "undetermined"})
    incomplete = dict(mappings)
    incomplete.pop("requirements[0].ctns")
    with pytest.raises(ValueError, match="Every nonempty"):
        reviewed_business_decision(job, **{**kwargs, "mappings": incomplete})
    wrong_scope = {"requirements[0].cargo_lines[0].ctns": "inquiries.0.totals.gross_kg"}
    with pytest.raises(ValueError, match="unsupported field or semantic scope"):
        reviewed_business_decision(job, **{**kwargs, "mappings": wrong_scope})
    altered = copy.deepcopy(job)
    altered["result"]["inquiries"][0]["totals"]["gross_kg"]["quote"] = "not present"
    with pytest.raises(ValueError, match="exact extracted source evidence"):
        reviewed_business_decision(altered, **kwargs)
    warning = copy.deepcopy(job)
    warning["issues"] = [{"level": "warning", "message": "needs review"}]
    with pytest.raises(ValueError, match="without unresolved review issues"):
        reviewed_business_decision(warning, **kwargs)

    mismatched = copy.deepcopy(job)
    mismatched["result"]["inquiries"][0]["totals"]["gross_kg"]["value"] = "999"
    mismatch_evidence = tmp_path / "mismatched-evidence.json"
    mismatch_evidence.write_text(
        json.dumps(
            {
                "job": mismatched,
                "source_bindings": bindings,
                "mappings": mappings,
                "reviewer": "synthetic",
                "confirmed_intent": "inquiry",
            }
        ),
        encoding="utf-8",
    )
    mismatch_ref = {
        "ref": str(mismatch_evidence),
        "sha256": hashlib.sha256(mismatch_evidence.read_bytes()).hexdigest(),
    }
    with pytest.raises(ValueError, match="not supported by its cited evidence"):
        reviewed_business_decision(mismatched, **{**kwargs, "agent_evidence": mismatch_ref})

    baseline = tmp_path / "business-context.json"
    baseline.write_text('{"synthetic":true}', encoding="utf-8")
    business_args = {
        "material_store": store,
        "material_results": [{"result_id": saved["result_id"], "revision": 1}],
        "context_baseline": {
            "ref": str(baseline),
            "sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
        },
    }
    business = save_business_result(tmp_path / "business", agent_decision=decision, **business_args)
    assert (
        read_business_result(tmp_path / "business", business["business_result_id"], 1)[
            "agent_decision"
        ]
        == decision
    )
    unbound_decision = copy.deepcopy(decision)
    unbound_decision["inquiry_assignment"]["value"][0]["inquiry"]["requirements"][0][
        "gross_weight"
    ]["value"] = 999
    with pytest.raises(SnapshotValidationError, match="EmailBridge.*does not match"):
        save_business_result(
            tmp_path / "business", agent_decision=unbound_decision, **business_args
        )
    # Simulate a legacy writer that saved a self-consistent envelope before
    # the evidence/decision association was checked on read.
    legacy = read_business_result(tmp_path / "business", business["business_result_id"], 1)
    legacy["business_result_id"] = "legacy-invalid-business"
    legacy["agent_decision"] = unbound_decision
    legacy_store = tmp_path / "legacy-business"
    _write_new(
        _path(legacy_store, legacy["business_result_id"], 1), legacy, "business-result-snapshot/1"
    )
    with pytest.raises(SnapshotValidationError, match="EmailBridge.*does not match"):
        read_business_result(legacy_store, legacy["business_result_id"], 1)
    assert (
        read_business_result(tmp_path / "business", business["business_result_id"], 1)[
            "agent_decision"
        ]
        == decision
    )

    partial, partial_inputs = partial_case(tmp_path / "partial")
    partial_saved = commit_result(store, partial, **partial_inputs)
    with pytest.raises(ValueError, match="complete declared material scope"):
        material_sources(store, partial_saved["result_id"], 1)


@pytest.mark.parametrize(
    ("group", "name", "value", "quote", "target", "source_path"),
    [
        (
            "totals",
            "package_count",
            "9",
            "3 cartons",
            "requirements[0].ctns",
            "inquiries.0.totals.package_count",
        ),
        (
            "totals",
            "gross_kg",
            "3",
            "3 cartons, Total G.W. 315 kg",
            "requirements[0].gross_weight",
            "inquiries.0.totals.gross_kg",
        ),
        ("totals", "cbm", "3", "3 cartons", "requirements[0].volume", "inquiries.0.totals.cbm"),
        (
            "cargo",
            "package_unit",
            "pallet",
            "3 cartons",
            "requirements[0].package_type",
            "inquiries.0.cargo.0.package_unit",
        ),
        (
            "fields",
            "destination",
            "London",
            "3 cartons",
            "requirements[0].destination_port",
            "inquiries.0.fields.destination",
        ),
        (
            "fields",
            "ready",
            "2099-12-15",
            "3 cartons",
            "requirements[0].cargo_ready_time",
            "inquiries.0.fields.ready",
        ),
    ],
)
def test_bridge_rejects_reviewed_value_unsupported_by_real_quote(
    tmp_path, group, name, value, quote, target, source_path
):
    snapshot, inputs, _ = complete_case(tmp_path / "complete")
    store = tmp_path / "store"
    saved = commit_result(store, snapshot, **inputs)
    _, sources, bindings = material_sources(store, saved["result_id"], 1)
    cell = {
        "value": value,
        "source_id": sources[0]["id"],
        "quote": quote,
        "provenance": "extracted",
    }
    inquiry = {"fields": {}, "totals": {}, "cargo": [{}], "weight_breaks": []}
    row = inquiry[group][0] if group == "cargo" else inquiry[group]
    row[name] = cell
    job = {
        "id": "synthetic-job",
        "revision": 1,
        "status": "confirmed",
        "confirmed_at": "2026-11-09T00:00:00Z",
        "sources": sources,
        "issues": [],
        "result": {"notes": [], "inquiries": [inquiry]},
    }
    mappings = {target: source_path}
    path = tmp_path / "confirmed-review.json"
    path.write_text(
        json.dumps(
            {
                "job": job,
                "source_bindings": bindings,
                "mappings": mappings,
                "reviewer": "synthetic",
                "confirmed_intent": "inquiry",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not supported by its cited evidence"):
        reviewed_business_decision(
            job,
            material_store=store,
            result_id=saved["result_id"],
            revision=1,
            reviewer="synthetic",
            confirmed_intent="inquiry",
            mappings=mappings,
            agent_evidence={
                "ref": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            },
        )
