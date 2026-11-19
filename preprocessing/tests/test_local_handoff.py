"""Regressions for the local intake-to-manifest receiving boundary."""

import base64
import copy
import json
from email import policy
from email.message import EmailMessage
from pathlib import Path

import pytest
from test_inputs import email, request

from doccase_preprocessing.local_handoff import receive_handoff, run_local_handoff
from doccase_preprocessing.models import InputError, Limits, MaterialError

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "partial-request.json"


def raw_request(payload):
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def run_request(raw, root, limits=None):
    return run_local_handoff(
        raw, root, input_format="json", request_reference="intake:local", limits=limits
    )


def receive(result, root, context=None, expected_task_id=None):
    return receive_handoff(
        result["manifest_path"],
        root,
        expected_task_id=expected_task_id or result["received"]["manifest"]["task_id"],
        expected_request_fingerprint=result["request_fingerprint"],
        intake_context=context if context is not None else result["intake_context"],
    )


def test_local_handoff_forwards_resolver_and_keeps_other_materials(tmp_path):
    references = {
        "ok": "https://example.invalid/ok",
        "missing": "https://example.invalid/missing",
        "text": "https://example.invalid/text",
    }
    attachments = [
        {
            "material_id": "ok",
            "filename": "ok.txt",
            "mime": "text/plain",
            "oss_url": references["ok"],
        },
        {
            "material_id": "missing",
            "filename": "missing.txt",
            "mime": "text/plain",
            "oss_url": references["missing"],
        },
        {
            "material_id": "text",
            "filename": "text.pdf",
            "mime": "application/pdf",
            "content_text": "provided text",
            "oss_url": references["text"],
        },
    ]
    calls = []

    def resolver(locator, byte_limit):
        calls.append((locator, byte_limit))
        if locator == references["missing"]:
            raise MaterialError("not_whitelisted", "Source reference unavailable")
        assert locator == references["ok"]
        assert byte_limit >= len(b"original text")
        return b"original text"

    result = run_local_handoff(
        raw_request(request(email=email(attachments=attachments))),
        tmp_path,
        input_format="json",
        request_reference="intake:local",
        fetch_attachment=resolver,
    )
    received = result["received"]
    by_id = {item["record"]["material_id"]: item for item in received["materials"]}
    assert received["manifest"]["state"] == "partial"
    assert [locator for locator, _ in calls] == [references["ok"], references["missing"]]
    assert by_id["ok"]["content"] == b"original text" and by_id["ok"]["record"]["original"]
    assert by_id["missing"]["record"]["issues"][0]["code"] == "not_whitelisted"
    assert (
        by_id["text"]["content"] == b"provided text" and by_id["text"]["record"]["original"] is None
    )
    assert all(
        task["execution_state"] == "not_started" for task in received["manifest"]["parse_tasks"]
    )


def test_local_handoff_does_not_swallow_resolver_programming_errors(tmp_path):
    payload = request(
        email=email(
            attachments=[
                {"filename": "f.txt", "mime": "text/plain", "oss_url": "https://example.invalid/f"}
            ]
        )
    )

    def broken_resolver(locator, byte_limit):
        raise RuntimeError("resolver implementation failed")

    with pytest.raises(RuntimeError, match="resolver implementation failed"):
        run_local_handoff(
            raw_request(payload),
            tmp_path,
            input_format="json",
            request_reference="intake:local",
            fetch_attachment=broken_resolver,
        )


def test_partial_manifest_is_read_from_disk_and_detects_wrong_root_or_tampering(tmp_path):
    root = tmp_path / "partial"
    result = run_request(FIXTURE.read_bytes(), root)
    received = result["received"]
    manifest = received["manifest"]
    body, missing = received["materials"]

    assert manifest["state"] == "partial"
    assert "没有声明每箱同重" in body["content"].decode("utf-8")
    assert missing["record"]["filename"] == "M2-packing-list.pdf"
    assert missing["record"]["issues"][0]["code"] == "missing_content"
    assert missing["record"]["state"] == "blocked" and missing["content"] is None
    assert len(manifest["parse_tasks"]) == 1
    assert manifest["parse_tasks"][0]["execution_state"] == "not_started"
    assert manifest["parse_tasks"][0]["parser"]["version"] == "unbound"

    wrong_root = tmp_path / "wrong"
    wrong_root.mkdir()
    with pytest.raises(FileNotFoundError):
        receive(result, wrong_root)
    (root / body["record"]["content"]["path"]).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        receive(result, root)


def test_text_and_original_reference_remain_one_occurrence_without_leaking(tmp_path, capsys):
    locator = "https://example.invalid/object.pdf?signature=synthetic-secret"
    issue = {
        "code": "upstream_partial",
        "message": "original pending",
        "details": {
            "material_id": "M1",
            "source_path": "email.attachments[0]",
            "reason": "upstream extraction supplied",
        },
    }
    payload = request(
        email=email(
            snippet=f"Source reference: {locator}",
            attachments=[
                {
                    "material_id": "M1",
                    "filename": "packing.pdf",
                    "mime": "application/pdf",
                    "content_text": "3 cartons, Total G.W. 315 kg",
                    "oss_url": locator,
                }
            ],
        ),
        context={"issues": [issue]},
    )
    result = run_request(raw_request(payload), tmp_path)
    received = result["received"]
    attachments = [item for item in received["materials"] if item["record"]["kind"] == "attachment"]
    assert len(attachments) == 1
    attachment = attachments[0]
    assert attachment["content"] == b"3 cartons, Total G.W. 315 kg"
    assert attachment["record"]["representation"] == "provided_text"
    assert attachment["record"]["original"] is None
    assert attachment["original_reference"] == locator
    assert attachment["original_reference_available"]
    assert not attachment["original_content_integrity_checked"]
    assert attachment["upstream_issues"] == [issue]
    assert received["upstream_issues"] == [issue]
    assert received["request_reference"] == "intake:local"
    assert locator not in result["manifest_path"].read_text(encoding="utf-8")
    captured = capsys.readouterr()
    assert "synthetic-secret" not in captured.out + captured.err

    for altered in (
        "missing_reference",
        "wrong_source_path",
        "wrong_fingerprint",
        "missing_request",
    ):
        context = copy.deepcopy(result["intake_context"])
        if altered == "missing_reference":
            context["materials"]["M1"]["original_reference"] = None
        elif altered == "wrong_source_path":
            context["materials"]["M1"]["source_path"] = "email.attachments[1]"
        elif altered == "wrong_fingerprint":
            context["request_fingerprint"] = "wrong"
        else:
            context["request_reference"] = None
        item = next(
            value
            for value in receive(result, tmp_path, context)["materials"]
            if value["record"]["material_id"] == "M1"
        )
        assert item["content"] == attachment["content"]
        assert not item["original_reference_available"]
        assert not item["original_content_integrity_checked"]

    conflicting = copy.deepcopy(result["intake_context"])
    conflicting["upstream_issues"][0]["material_id"] = "other-material"
    item = next(
        value
        for value in receive(result, tmp_path, conflicting)["materials"]
        if value["record"]["material_id"] == "M1"
    )
    assert item["upstream_issues"] == []
    assert (
        receive(result, tmp_path, conflicting)["upstream_issues"] == conflicting["upstream_issues"]
    )


def test_identical_text_occurrences_share_a_task_but_keep_distinct_sources(tmp_path):
    attachments = [
        {
            "material_id": material_id,
            "filename": "packing.pdf",
            "mime": "application/pdf",
            "content_text": "3 cartons",
            "oss_url": f"https://example.invalid/{material_id}?signature=synthetic",
        }
        for material_id in ("M1", "M2")
    ]
    result = run_request(raw_request(request(email=email(attachments=attachments))), tmp_path)
    occurrences = [
        item for item in result["received"]["materials"] if item["record"]["kind"] == "attachment"
    ]
    assert len(occurrences) == 2
    assert occurrences[0]["record"]["parse_task_id"] == occurrences[1]["record"]["parse_task_id"]
    assert [item["record"]["metadata"]["source_path"] for item in occurrences] == [
        "email.attachments[0]",
        "email.attachments[1]",
    ]
    assert [item["original_reference"] for item in occurrences] == [
        attachment["oss_url"] for attachment in attachments
    ]


def test_upstream_issue_retains_raw_detail_without_persisting_signed_reference(tmp_path):
    locator = "https://example.invalid/packing.pdf?signature=synthetic-secret"
    issue = {
        "code": "source_unavailable",
        "message": f"Original is unavailable: {locator}",
        "details": {"material_id": "M1", "source_path": "email.attachments[0]"},
    }
    payload = request(
        email=email(
            attachments=[
                {
                    "material_id": "M1",
                    "filename": "packing.pdf",
                    "mime": "application/pdf",
                    "content_text": "3 cartons",
                    "oss_url": locator,
                    "availability_reason": f"Not acquired: {locator}",
                }
            ]
        ),
        context={"issues": [issue]},
    )
    result = run_request(raw_request(payload), tmp_path)
    manifest_text = result["manifest_path"].read_text(encoding="utf-8")
    assert locator not in manifest_text
    assert "controlled reference withheld" in manifest_text
    assert result["received"]["upstream_issues"] == [issue]


def test_intake_cannot_bind_to_a_different_run_with_the_same_task_and_positions(tmp_path):
    def payload(text, reference):
        return request(
            email=email(
                attachments=[
                    {
                        "material_id": "M1",
                        "filename": "packing.pdf",
                        "mime": "application/pdf",
                        "content_text": text,
                        "oss_url": reference,
                    }
                ]
            )
        )

    first = run_request(
        raw_request(payload("first text", "https://example.invalid/first")), tmp_path / "first"
    )
    second = run_request(
        raw_request(payload("second text", "https://example.invalid/second")), tmp_path / "second"
    )
    mixed = receive_handoff(
        first["manifest_path"],
        tmp_path / "first",
        expected_task_id="task-1",
        expected_request_fingerprint=second["request_fingerprint"],
        intake_context=second["intake_context"],
    )
    attachment = next(item for item in mixed["materials"] if item["record"]["material_id"] == "M1")
    assert attachment["content"] == b"first text"
    assert not mixed["intake_bound"]
    assert not attachment["original_reference_available"]


def test_long_body_and_attachment_evidence_survive_without_truncation(tmp_path):
    correction = "更正：箱数为三箱。"
    negation = "未声明每箱同重。"
    pointer = "请核对附件 M1，体积见缺失附件 M2。"
    body = correction + "\n" + "ordinary detail\n" * 600 + negation
    body += "\n" + "other detail\n" * 600 + pointer
    table = "Item | Qty | Weight per carton (kg) | Total gross (kg)\nL1 | 3 | 105 | 315\n"
    payload = request(
        email=email(
            body_text=body,
            attachments=[
                {
                    "material_id": "M1",
                    "filename": "packing.txt",
                    "mime": "text/plain",
                    "content_base64": base64.b64encode(table.encode()).decode(),
                },
                {"material_id": "M2", "filename": "missing.pdf", "mime": "application/pdf"},
            ],
        ),
        context={"ancestors": [email(ref="earlier", body_text="旧记录：四箱。 ")]},
    )
    raw = raw_request(payload)
    result = run_request(raw, tmp_path / "full")
    received = result["received"]
    assert received["manifest"]["state"] == "partial"
    by_path = {
        item["record"]["metadata"].get("source_path"): item for item in received["materials"]
    }
    body_item = by_path["email.body_text"]
    assert body_item["content"].decode() == body
    assert body.index(correction) < body.index(negation) < body.index(pointer)
    assert body_item["record"]["role"] == "current" and body_item["source_bound"]
    table_item = by_path["email.attachments[0]"]
    assert table_item["record"]["material_id"] == "M1"
    assert table_item["content"].decode() == table and table_item["source_bound"]
    assert by_path["email.attachments[1]"]["record"]["issues"][0]["code"] == "missing_content"
    assert by_path["context.ancestors[0].body_text"]["record"]["role"] == "ancestor"
    assert not received["manifest"]["parsing_completed"]

    with pytest.raises(InputError, match="Input exceeds"):
        run_request(raw, tmp_path / "input-limit", Limits(max_input_bytes=len(raw) - 1))
    limited = run_request(raw, tmp_path / "material-limit", Limits(max_material_bytes=64))
    limited_body = limited["received"]["materials"][0]
    assert limited_body["record"]["state"] == "blocked"
    assert any(issue["code"] == "limit_exceeded" for issue in limited_body["record"]["issues"])


def test_eml_parent_mime_positions_and_derived_html_are_preserved(tmp_path):
    message = EmailMessage()
    message["Subject"] = "synthetic handoff"
    message.set_content("Plain body")
    message.add_alternative("<p>HTML 中文</p>", subtype="html")
    message.add_attachment("table kg each total", filename="table.txt")
    result = run_local_handoff(
        message.as_bytes(policy=policy.SMTP),
        tmp_path,
        input_format="eml",
        task_id="eml-task",
        email_ref_id="mail-1",
        request_reference="intake:eml",
    )
    items = result["received"]["materials"]
    original = next(item for item in items if item["record"]["kind"] == "original_email")
    html = next(item for item in items if item["record"]["declared_mime"] == "text/html")
    plain = next(
        item
        for item in items
        if item["record"]["declared_mime"] == "text/plain" and item["record"]["kind"] == "body"
    )
    assert original["record"]["state"] == plain["record"]["state"] == "preserved"
    assert original["record"]["parse_task_id"] is None
    assert html["record"]["parent_material_id"] == original["record"]["material_id"]
    assert html["record"]["metadata"]["mime_path"].startswith("mail-1.mime")
    assert html["source_bound"] and html["record"]["original"] is None
    assert "HTML 中文" in html["content"].decode("utf-8-sig")


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ("version", "Unsupported manifest version"),
        ("material_to_task", "Material-to-task reference is broken"),
        ("task_to_material", "Task-to-material reference is broken"),
        ("task_content", "Parse task content does not match"),
        ("text_as_original", "text derivative cannot be its original"),
        ("escape", "Content path escapes"),
    ],
)
def test_receiver_rejects_broken_manifest_references(tmp_path, change, error):
    root = tmp_path / "accepted"
    result = run_request(FIXTURE.read_bytes(), root)
    manifest = copy.deepcopy(result["received"]["manifest"])
    body = manifest["materials"][0]
    task = manifest["parse_tasks"][0]
    if change == "version":
        manifest["schema_version"] = "doccase-preprocessing/other"
    elif change == "material_to_task":
        body["parse_task_id"] = "missing"
    elif change == "task_to_material":
        task["material_ids"].append("missing")
    elif change == "task_content":
        task["content_sha256"] = "0" * 64
    elif change == "text_as_original":
        body["original"] = body["content"]
    else:
        outside = tmp_path / "outside.txt"
        outside.write_bytes(result["received"]["materials"][0]["content"])
        body["content"]["path"] = "../outside.txt"
        task["content"]["path"] = "../outside.txt"
    changed_path = tmp_path / "modified-manifest.json"
    changed_path.write_text(json.dumps(manifest), encoding="utf-8")
    changed_result = {**result, "manifest_path": changed_path}
    with pytest.raises(ValueError, match=error):
        receive(changed_result, root)


def test_receiver_rejects_wrong_expected_task_but_keeps_text_without_intake_context(tmp_path):
    result = run_request(FIXTURE.read_bytes(), tmp_path)
    with pytest.raises(ValueError, match="expected task"):
        receive(result, tmp_path, expected_task_id="other-task")
    received = receive_handoff(
        result["manifest_path"],
        tmp_path,
        expected_task_id=result["received"]["manifest"]["task_id"],
        expected_request_fingerprint=result["request_fingerprint"],
        intake_context=None,
    )
    assert not received["intake_bound"]
    assert received["materials"][0]["content"] == result["received"]["materials"][0]["content"]
    assert not received["materials"][0]["source_bound"]
