"""Run the three synthetic local handoff examples without accessing external services."""

import argparse
import base64
import copy
import json
from pathlib import Path
from uuid import uuid4

from doccase_preprocessing.local_handoff import receive_handoff, run_local_handoff
from doccase_preprocessing.models import InputError, Limits


def email(body_text, attachments):
    return {
        "email_ref_id": "local-mail",
        "subject": "Synthetic local handoff",
        "from_addr": "buyer@example.invalid",
        "from_name": "Buyer",
        "to_addrs": ["sales@example.invalid"],
        "sent_at": "2026-11-06T10:00:00+08:00",
        "snippet": "Synthetic local example",
        "thread_headers": {"message_id": "<local@example.invalid>"},
        "body_text": body_text,
        "attachments": attachments,
    }


def encode(payload):
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def run(raw, root, limits=None):
    return run_local_handoff(
        raw, root, input_format="json", request_reference="intake:local-example", limits=limits
    )


def receive(result, root, context):
    return receive_handoff(
        result["manifest_path"],
        root,
        expected_task_id=result["received"]["manifest"]["task_id"],
        expected_request_fingerprint=result["request_fingerprint"],
        intake_context=context,
    )


def partial_example(root):
    fixture = (
        Path(__file__).resolve().parents[2]
        / "architecture-integration/fixtures/partial-request.json"
    )
    raw = fixture.read_bytes()
    result = run(raw, root / "partial")
    manifest = result["received"]["manifest"]
    body, missing = result["received"]["materials"]
    assert manifest["state"] == "partial" and body["content"]
    assert missing["record"]["issues"][0]["code"] == "missing_content"
    assert [task["execution_state"] for task in manifest["parse_tasks"]] == ["not_started"]

    wrong_root = root / "wrong-root"
    wrong_root.mkdir()
    try:
        receive(result, wrong_root, result["intake_context"])
    except FileNotFoundError:
        wrong_root_detected = True
    else:
        raise AssertionError("Incorrect content root was accepted")

    negative = run(raw, root / "tampered-example")
    body_path = negative["received"]["materials"][0]["record"]["content"]["path"]
    (root / "tampered-example" / body_path).write_bytes(b"deliberately altered")
    try:
        receive(negative, root / "tampered-example", negative["intake_context"])
    except ValueError as error:
        if "SHA-256 mismatch" not in str(error):
            raise
        tampering_detected = True
    else:
        raise AssertionError("Tampered content was accepted")
    return {
        "state": manifest["state"],
        "missing_attachment": missing["record"]["issues"][0]["code"],
        "unstarted_tasks": len(manifest["parse_tasks"]),
        "wrong_root_detected": wrong_root_detected,
        "tampering_detected": tampering_detected,
    }


def reference_example(root):
    locator = "https://example.invalid/original.pdf?signature=synthetic-secret"
    issue = {
        "code": "upstream_partial",
        "message": "original pending",
        "details": {"material_id": "M1", "source_path": "email.attachments[0]", "reason": "local"},
    }
    payload = {
        "task_id": "p01-reference",
        "email": email(
            "请查附件。",
            [
                {
                    "material_id": "M1",
                    "filename": "original.pdf",
                    "mime": "application/pdf",
                    "content_text": "3 cartons, Total G.W. 315 kg",
                    "oss_url": locator,
                }
            ],
        ),
        "context": {"issues": [issue]},
    }
    result = run(encode(payload), root / "reference")
    attachments = [
        item for item in result["received"]["materials"] if item["record"]["kind"] == "attachment"
    ]
    assert len(attachments) == 1
    attachment = attachments[0]
    assert attachment["content"] and attachment["record"]["original"] is None
    assert attachment["original_reference"] == locator and attachment["upstream_issues"] == [issue]
    assert locator not in result["manifest_path"].read_text(encoding="utf-8")
    for change in ("missing_reference", "wrong_fingerprint"):
        context = copy.deepcopy(result["intake_context"])
        if change == "missing_reference":
            context["materials"]["M1"]["original_reference"] = None
        else:
            context["request_fingerprint"] = "wrong"
        received = receive(result, root / "reference", context)
        item = next(item for item in received["materials"] if item["record"]["material_id"] == "M1")
        assert item["content"] == attachment["content"] and not item["original_reference_available"]
        assert not item["original_content_integrity_checked"]
    return {
        "registered_attachments": 1,
        "text_readable": True,
        "reference_bound": True,
        "original_bytes_acquired": False,
        "upstream_issue_bound": True,
        "missing_or_mismatched_reference_keeps_text": True,
    }


def long_example(root):
    correction, negation, pointer = "更正：三箱。", "未声明每箱同重。", "请核对附件M1和缺失附件M2。"
    body = correction + "\n" + "ordinary detail\n" * 600 + negation
    body += "\n" + "other detail\n" * 600 + pointer
    table = "Item | Qty | Weight each (kg) | Total gross (kg)\nL1 | 3 | 105 | 315\n"
    payload = {
        "task_id": "p01-long",
        "email": email(
            body,
            [
                {
                    "material_id": "M1",
                    "filename": "packing.txt",
                    "mime": "text/plain",
                    "content_base64": base64.b64encode(table.encode()).decode(),
                },
                {"material_id": "M2", "filename": "missing.pdf", "mime": "application/pdf"},
            ],
        ),
    }
    raw = encode(payload)
    result = run(raw, root / "long")
    entries = {
        item["record"]["metadata"].get("source_path"): item
        for item in result["received"]["materials"]
    }
    assert entries["email.body_text"]["content"].decode() == body
    assert body.index(correction) < body.index(negation) < body.index(pointer)
    assert entries["email.attachments[0]"]["content"].decode() == table
    assert entries["email.attachments[0]"]["record"]["material_id"] == "M1"
    assert entries["email.attachments[1]"]["record"]["issues"][0]["code"] == "missing_content"
    assert all(item["source_bound"] for item in entries.values())
    try:
        run(raw, root / "over-input-limit", Limits(max_input_bytes=len(raw) - 1))
    except InputError:
        rejected_over_limit = True
    else:
        raise AssertionError("Oversized input was accepted")
    return {
        "state": result["received"]["manifest"]["state"],
        "body_characters": len(body),
        "begin_middle_end_retained": True,
        "table_and_occurrences_retained": True,
        "over_limit_rejected": rejected_over_limit,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output / uuid4().hex
    root.mkdir(parents=True, exist_ok=False)
    report = {
        "output": str(root.resolve()),
        "partial": partial_example(root),
        "reference": reference_example(root),
        "long": long_example(root),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
