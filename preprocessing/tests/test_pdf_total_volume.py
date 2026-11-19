"""A PDF total-volume regression with a controlled Docling READ artifact."""

import base64
import copy
import hashlib
import json
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfWriter
from test_main_channel import CONTRACT, ROOT, p02
from test_output_snapshot import context, scope_for

from doccase_preprocessing.local_handoff import run_local_handoff
from doccase_preprocessing.main_channel import _assess, read_result, run_main_channel
from doccase_preprocessing.output_revision import revise_candidates
from doccase_preprocessing.output_snapshot import commit_result


def pdf_case(tmp_path, *, extra_texts=()):
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=600)
    raw_pdf = BytesIO()
    writer.write(raw_pdf)
    request = json.loads(
        (ROOT / "architecture-integration/fixtures/partial-request.json").read_text(
            encoding="utf-8"
        )
    )
    request["email"]["attachments"] = [
        {
            "material_id": "packing",
            "filename": "packing.pdf",
            "mime": "application/pdf",
            "content_base64": base64.b64encode(raw_pdf.getvalue()).decode("ascii"),
        }
    ]
    prepared = run_local_handoff(
        json.dumps(request, ensure_ascii=False).encode("utf-8"),
        tmp_path / "p01",
        input_format="json",
        request_reference="local:synthetic-pdf-layout",
    )
    manifest = prepared["received"]["manifest"]
    profile = {
        "id": "pdf-total-test",
        "version": "1",
        "task_scope_ref": manifest["task_id"],
        "requirements": [
            {
                "requirement_id": "volume",
                "field_name": "line_volume",
                "material_id": "packing",
                "object_selector": "packing/document-total",
                "source_kind": "attachment",
                "critical": True,
                "missing_policy": "block",
                "allow_not_applicable": False,
                "scope_dependencies": [{"stage": "extraction", "refs": ["packing/representation"]}],
                "pdf_total_volume": True,
            }
        ],
    }
    configs = p02.configurations(cache_path=tmp_path)
    versions = configs[("docling", "pdf")]["versions"]

    def block(text, left, top, right, bottom):
        return {
            "text": text,
            "prov": [
                {
                    "page_no": 1,
                    "bbox": {
                        "l": left,
                        "t": top,
                        "r": right,
                        "b": bottom,
                        "coord_origin": "BOTTOMLEFT",
                    },
                    "charspan": [0, len(text)],
                }
            ],
        }

    texts = [
        block("TOTAL", 70, 95, 90, 90),
        block("CBM", 72, 86, 88, 81),
        block("0.771", 72, 70, 88, 65),
        block("0.427", 72, 55, 88, 50),
        block("TOTAL:", 10, 35, 30, 30),
        block("1.311", 72, 35, 88, 30),
        block("TOTAL VOLUME: 11360USD", 10, 15, 60, 10),
        *extra_texts,
    ]

    def worker(request):
        artifact = Path(request["json_path"])
        data = json.dumps({"texts": texts, "tables": []}).encode("utf-8")
        artifact.write_bytes(data)
        return {
            "conversion_status": "success",
            "errors": [],
            "versions": versions,
            "json_path": str(artifact),
            "json_sha256": hashlib.sha256(data).hexdigest(),
            "document_pages": [1],
            "text_items": len(texts),
            "table_items": 0,
        }

    result = run_main_channel(
        prepared["manifest_path"],
        tmp_path / "p01",
        expected_task_id=manifest["task_id"],
        expected_request_fingerprint=prepared["request_fingerprint"],
        intake_context=prepared["intake_context"],
        selected_material_ids={"packing"},
        read_configurations=configs,
        reader_versions=versions,
        field_profile=profile,
        field_contract_ref={
            "version": "0.1.0",
            "ref": str(CONTRACT),
            "sha256": hashlib.sha256(CONTRACT.read_bytes()).hexdigest(),
        },
        output_dir=tmp_path / "main",
        reader=p02.parse_prepared,
        worker_fn=worker,
    )
    return prepared, profile, result


def test_pdf_total_volume_keeps_value_row_and_header_evidence(tmp_path):
    _, _, result = pdf_case(tmp_path)
    handoff = read_result(result["result_ref"])["material_handoff"]
    volume = handoff["candidates"][0]["field"]
    assert volume["value"] == 1.311
    assert volume["unit"] == "CBM"
    evidence = {
        source["excerpt"]
        for source in handoff["sources"]
        if source["source_id"] in volume["source_ids"]
    }
    assert evidence == {"TOTAL", "CBM", "TOTAL:", "1.311"}
    assert all(source["locator"]["page"] == 1 for source in handoff["sources"])


def test_pdf_total_volume_is_unread_if_total_row_has_multiple_values(tmp_path):
    duplicate = {
        "text": "2.622",
        "prov": [
            {
                "page_no": 1,
                "bbox": {"l": 72, "t": 35, "r": 88, "b": 30, "coord_origin": "BOTTOMLEFT"},
                "charspan": [0, 5],
            }
        ],
    }
    _, _, result = pdf_case(tmp_path, extra_texts=[duplicate])
    handoff = read_result(result["result_ref"])["material_handoff"]
    field = handoff["candidates"][0]["field"]
    assert field["value"] is None
    assert field["status"] == "uncertain"


def test_pdf_total_volume_rejects_changed_value_or_page_evidence(tmp_path):
    prepared, profile, result = pdf_case(tmp_path)
    snapshot = read_result(result["result_ref"])
    read = read_result(result["read_ref"])
    manifest = prepared["received"]["manifest"]
    handoff = snapshot["material_handoff"]
    for change in ("value", "page"):
        altered = copy.deepcopy(handoff)
        if change == "value":
            altered["candidates"][0]["field"]["value"] = 0.771
        else:
            altered["sources"][0]["locator"]["page"] = 2
        with pytest.raises(ValueError, match="fixed source evidence"):
            _assess(
                altered,
                profile,
                read,
                manifest,
                tmp_path / "p01",
                snapshot["field_contract_source"],
            )


def test_pdf_total_volume_can_be_reconfirmed_from_same_evidence(tmp_path):
    prepared, profile, result = pdf_case(tmp_path)
    snapshot = read_result(result["result_ref"])
    manifest = prepared["received"]["manifest"]
    inputs = context(prepared, profile, scope_for(manifest, profile, True), result)
    saved = commit_result(tmp_path / "store", snapshot, **inputs)
    cid = snapshot["material_handoff"]["candidates"][0]["candidate_id"]
    revise_candidates(
        tmp_path / "store",
        saved["result_id"],
        base_revision=1,
        reviewer="reviewer",
        changes=[{"candidate_id": cid, "status": "uncertain", "reason": "review layout"}],
    )
    with pytest.raises(ValueError, match="fixed source evidence"):
        revise_candidates(
            tmp_path / "store",
            saved["result_id"],
            base_revision=2,
            reviewer="reviewer",
            changes=[
                {"candidate_id": cid, "status": "known", "value": 0.771, "reason": "wrong row"}
            ],
        )
    revised = revise_candidates(
        tmp_path / "store",
        saved["result_id"],
        base_revision=2,
        reviewer="reviewer",
        changes=[{"candidate_id": cid, "status": "known", "value": 1.311, "reason": "confirmed"}],
    )
    assert revised["revision"] == 3
