"""Exercise optional Agent extraction with synthetic input and a local fake worker."""

import hashlib
import json
from pathlib import Path

import p02_local_read as p02
import pytest
from local_parse_server import FIELD_CONTRACT, current_body_profile
from test_inputs import email, request

from doccase_preprocessing.extraction_agent import (
    current_route_destinations,
    revised_count_supported,
)
from doccase_preprocessing.local_handoff import run_local_handoff
from doccase_preprocessing.main_channel import read_result, read_validated_result, run_main_channel


def test_destination_route_requires_current_and_unambiguous_evidence():
    route = {"pointer": "/texts/5/text", "text": "FOB Origin to Example City"}
    current = "<p>FOB Origin to Example City</p><blockquote>Old details</blockquote>"
    assert current_route_destinations([route], current) == [(route, "Example City")]
    assert not current_route_destinations(
        [route], "<blockquote>FOB Origin to Example City</blockquote>"
    )
    other = {"pointer": "/texts/6/text", "text": "FOB Origin to Other City"}
    assert not current_route_destinations(
        [route, other], "FOB Origin to Example City. FOB Origin to Other City."
    )
    assert not current_route_destinations(
        [route], "FOB Origin to Example City. FOB Origin to Example City."
    )
    assert not current_route_destinations([route], "FOB Origin to Example City or Other City.")


def synthetic_run(
    tmp_path,
    adapter,
    *,
    export_override=None,
    include_attachment=False,
    corrupt=False,
    read_fallback=False,
):
    sentence = "Number of packages has reduced from 947nos to 683nos."
    attachments = (
        [
            {
                "material_id": "other",
                "filename": "other.txt",
                "mime": "text/plain",
                "content_text": "A separate attachment.",
            }
        ]
        if include_attachment
        else []
    )
    payload = request(
        email=email(body_html=f"<p>{sentence}</p>", body_text="", attachments=attachments)
    )
    p01 = tmp_path / "p01"
    prepared = run_local_handoff(
        json.dumps(payload).encode(), p01, input_format="json", request_reference="local:synthetic"
    )
    manifest = prepared["received"]["manifest"]
    body = next(
        item
        for item in manifest["materials"]
        if item["kind"] == "body" and item["format"] == "html"
    )
    profile = current_body_profile(manifest, body, "local-current-body-inquiry")
    versions = p02.configurations()["docling", "html"]["versions"]

    def write(path, value):
        path.write_bytes(value)
        return hashlib.sha256(value).hexdigest()

    def fake_worker(operation, **_options):
        if operation["operation"] == "convert":
            path = Path(operation["json_path"])
            sha = write(path, json.dumps({"texts": [{"text": sentence}], "tables": []}).encode())
            return {
                "conversion_status": "success",
                "versions": versions,
                "errors": [],
                "json_path": str(path),
                "json_sha256": sha,
            }
        assert operation["operation"] == "export_markdown"
        if operation["source_kind"] == "attachment":
            return {"state": "failure", "reason": "no_markdown_table_mapping"}
        root = Path(operation["output_dir"])
        root.mkdir(parents=True)
        markdown, index = root / "content.md", root / "index.json"
        md_sha = write(markdown, sentence.encode())
        binding = {key: operation[key] for key in ("read_sha256", "material_id", "execution_id")}
        ix_sha = write(
            index,
            json.dumps(
                {
                    **binding,
                    "markdown_sha256": md_sha,
                    "limitations": [],
                    "fragments": [
                        {
                            "evidence_id": "E1",
                            "source_role": "current",
                            "read_pointer": "/texts/0/text",
                            "read_start": 0,
                            "read_end": len(sentence),
                            "markdown_start": 0,
                            "markdown_end": len(sentence),
                            "provenance": [],
                        }
                    ],
                }
            ).encode(),
        )
        return {
            "state": "success",
            "markdown_ref": str(markdown),
            "markdown_sha256": md_sha,
            "index_ref": str(index),
            "index_sha256": ix_sha,
        }

    def export(*args):
        result = p02.export_read_markdown(*args, worker_fn=fake_worker)
        if corrupt and result.get("markdown_ref"):
            Path(result["markdown_ref"]).write_text("changed", encoding="utf-8")
        return result

    selected = {body["material_id"]}
    if include_attachment:
        selected.update(
            item["material_id"] for item in manifest["materials"] if item["kind"] == "attachment"
        )
    result = run_main_channel(
        prepared["manifest_path"],
        p01,
        expected_task_id=manifest["task_id"],
        expected_request_fingerprint=prepared["request_fingerprint"],
        intake_context=prepared["intake_context"],
        selected_material_ids=selected,
        read_configurations=p02.configurations(cache_path=tmp_path, declared_versions=versions),
        reader_versions=versions,
        field_profile=profile,
        field_contract_ref={
            "version": "0.1.0",
            "ref": str(FIELD_CONTRACT),
            "sha256": hashlib.sha256(FIELD_CONTRACT.read_bytes()).hexdigest(),
        },
        output_dir=tmp_path / "run",
        reader=p02.parse_prepared,
        worker_fn=fake_worker,
        extraction_agent=adapter,
        markdown_provider=export_override or export,
        agent_read_fallback=read_fallback,
    )
    return read_validated_result(result["result_ref"], content_root=p01)


def test_agent_adopts_revised_count_and_preserves_evidence(tmp_path):
    calls = []

    def adapter(request, *, timeout_seconds):
        calls.append(timeout_seconds)
        evidence = next(
            item for item in request["evidence"] if "from 947nos to 683nos" in item["markdown"]
        )
        return {
            "requirement_id": request["requirement_id"],
            "candidate": {"value": 683, "evidence_ids": [evidence["evidence_id"]]},
        }

    snapshot = synthetic_run(tmp_path, adapter)
    handoff = snapshot["material_handoff"]
    field = next(
        item["field"] for item in handoff["candidates"] if item["field_name"] == "package_count"
    )
    sources = {item["source_id"]: item for item in handoff["sources"]}
    assert field["status"] == "known" and field["value"] == 683
    assert any("from 947nos to 683nos" in sources[item]["excerpt"] for item in field["source_ids"])
    assert calls == [5.0]


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("old", "evidence_not_supported"),
        ("unknown_id", "candidate_or_evidence_invalid"),
        ("bad_id_type", "candidate_or_evidence_invalid"),
        ("extra_field", "invalid_response_shape"),
        ("abstain", "abstained"),
        ("timeout", "timeout"),
    ],
)
def test_invalid_candidate_or_failure_keeps_rule_result(tmp_path, mode, reason):
    def adapter(request, *, timeout_seconds):
        if mode == "timeout":
            raise TimeoutError()
        if mode == "abstain":
            return {"requirement_id": request["requirement_id"], "candidate": None}
        evidence = next(
            item["evidence_id"]
            for item in request["evidence"]
            if "from 947nos to 683nos" in item["markdown"]
        )
        candidate = {
            "value": 947 if mode == "old" else 683,
            "evidence_ids": [
                {} if mode == "bad_id_type" else "unknown" if mode == "unknown_id" else evidence
            ],
        }
        result = {"requirement_id": request["requirement_id"], "candidate": candidate}
        if mode == "extra_field":
            result["field_name"] = "ctns"
        return result

    snapshot = synthetic_run(tmp_path, adapter)
    field = next(
        item["field"]
        for item in snapshot["material_handoff"]["candidates"]
        if item["field_name"] == "package_count"
    )
    events = json.loads((tmp_path / "run/agent-events.json").read_text(encoding="utf-8"))["events"]
    assert field["status"] == "uncertain" and events[0]["reason"] == reason


def test_agent_disabled_and_other_attachment_failure_do_not_block_body(tmp_path):
    snapshot = synthetic_run(tmp_path, None, include_attachment=True)
    field = next(
        item["field"]
        for item in snapshot["material_handoff"]["candidates"]
        if item["field_name"] == "package_count"
    )
    sidecars = json.loads((tmp_path / "run/markdown-outputs.json").read_text(encoding="utf-8"))[
        "materials"
    ]
    assert field["status"] == "uncertain"
    assert {item["state"] for item in sidecars.values()} == {"success", "failure"}
    assert not (tmp_path / "run/agent-events.json").exists()


def test_snapshot_readback_rechecks_candidate_without_adapter(tmp_path):
    def adapter(request, *, timeout_seconds):
        evidence = request["evidence"][0]["evidence_id"]
        return {
            "requirement_id": request["requirement_id"],
            "candidate": {"value": 683, "evidence_ids": [evidence]},
        }

    snapshot = synthetic_run(tmp_path, adapter)
    candidate = next(
        item
        for item in snapshot["material_handoff"]["candidates"]
        if item["field_name"] == "package_count"
    )
    candidate["field"]["value"] = 947
    changed = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
    path = tmp_path / "changed.json"
    path.write_bytes(changed)
    read = read_result(snapshot["read_result_ref"])
    with pytest.raises(ValueError, match="does not match fixed source evidence"):
        read_validated_result(
            {"ref": str(path), "sha256": hashlib.sha256(changed).hexdigest()},
            content_root=Path(read["manifest_path"]).parent.parent,
        )


def test_quoted_reduction_is_not_current_even_if_unique():
    blocks = [
        {"pointer": "/texts/0/text", "text": "Current message."},
        {"pointer": "/texts/1/text", "text": "De:"},
        {"pointer": "/texts/2/text", "text": "Enviado el:"},
        {"pointer": "/texts/3/text", "text": "Para:"},
        {
            "pointer": "/texts/4/text",
            "text": "Number of packages has reduced from 19nos to 13nos.",
        },
    ]
    source = {
        "kind": "current_body",
        "locator": {
            "json_pointer": "/texts/4/text",
            "char_start": 0,
            "char_end": len(blocks[-1]["text"]),
        },
    }
    raw = (
        "<p>Current message.</p><p>De: x Enviado el: yesterday Para: y</p><p>"
        + blocks[-1]["text"]
        + "</p>"
    )
    assert not revised_count_supported(13, source, blocks, raw)


def test_corrupt_markdown_hash_is_not_misreported_as_agent_abstention(tmp_path):
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        synthetic_run(tmp_path, lambda *_args, **_kwargs: None, corrupt=True)


def test_failed_export_does_not_call_adapter(tmp_path):
    calls = []
    snapshot = synthetic_run(
        tmp_path,
        lambda *_args, **_kwargs: calls.append(True),
        export_override=lambda *_args: {
            "state": "failure",
            "reason": "unsupported_docling_read_document",
        },
    )
    field = next(
        item["field"]
        for item in snapshot["material_handoff"]["candidates"]
        if item["field_name"] == "package_count"
    )
    event = json.loads((tmp_path / "run/agent-events.json").read_text(encoding="utf-8"))["events"][
        0
    ]
    assert field["status"] == "uncertain" and not calls
    assert event["reason"] == "unsupported_docling_read_document"


def test_opt_in_read_fallback_preserves_current_evidence(tmp_path):
    def adapter(request, *, timeout_seconds):
        evidence = next(
            item for item in request["evidence"] if "from 947nos to 683nos" in item["read"]
        )
        assert evidence["representation"] == "read"
        return {
            "requirement_id": request["requirement_id"],
            "candidate": {"value": 683, "evidence_ids": [evidence["evidence_id"]]},
        }

    snapshot = synthetic_run(
        tmp_path,
        adapter,
        read_fallback=True,
        export_override=lambda *_args: {"state": "failure", "reason": "markdown_unavailable"},
    )
    field = next(
        item["field"]
        for item in snapshot["material_handoff"]["candidates"]
        if item["field_name"] == "package_count"
    )
    event = json.loads((tmp_path / "run/agent-events.json").read_text(encoding="utf-8"))["events"][
        0
    ]
    assert field["value"] == 683 and event["representation"] == "read"
    assert event["read_fallback_reason"] == "markdown_unavailable"


def test_original_divider_excludes_quoted_evidence_from_current_body():
    from doccase_preprocessing.extraction_agent import current_body_text_check

    raw = "<p>Trade terms: EXW.</p><p>----- Original -----</p><p>1.23 CBM</p>"
    current = current_body_text_check(raw, allow_quoted_duplicate=True)
    assert current("Trade terms: EXW.")
    assert not current("1.23 CBM")
    text = "Synthetic packing summary: 8 boxes were revised to 10 boxes."
    blocks = [
        {"pointer": "/texts/0/text", "text": "Trade terms: EXW."},
        {"pointer": "/texts/1/text", "text": "----- Original -----"},
        {"pointer": "/texts/2/text", "text": text},
    ]
    source = {
        "kind": "current_body",
        "locator": {"json_pointer": "/texts/2/text", "char_start": 0, "char_end": len(text)},
    }
    assert not revised_count_supported(8, source, blocks, raw.replace("1.23 CBM", text))
