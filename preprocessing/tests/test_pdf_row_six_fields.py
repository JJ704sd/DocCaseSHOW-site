"""Exercise six material fields on one explicitly selected PDF row."""

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

FIELDS = (
    "ctns",
    "line_net_weight",
    "line_gross_weight",
    "unit_net_weight",
    "unit_gross_weight",
    "line_volume",
)


def pdf_row_case(tmp_path, extra_texts=(), selected_count="2"):
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=600)
    pdf = BytesIO()
    writer.write(pdf)
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
            "content_base64": base64.b64encode(pdf.getvalue()).decode("ascii"),
        }
    ]
    prepared = run_local_handoff(
        json.dumps(request, ensure_ascii=False).encode("utf-8"),
        tmp_path / "p01",
        input_format="json",
        request_reference="local:synthetic-six-field-row",
    )
    manifest = prepared["received"]["manifest"]
    profile = {
        "id": "six-fields-row",
        "version": "1",
        "task_scope_ref": manifest["task_id"],
        "requirements": [
            {
                "requirement_id": field,
                "field_name": field,
                "material_id": "packing",
                "object_selector": "packing/row-23",
                "source_kind": "attachment",
                "critical": True,
                "missing_policy": "block",
                "allow_not_applicable": False,
                "scope_dependencies": [{"stage": "extraction", "refs": ["packing/representation"]}],
                "pdf_row_anchor": "#23",
            }
            for field in FIELDS
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

    headers = [
        ("Total", "CTN"),
        ("N.Weight", "(kg)/CTN"),
        ("G.Weight", "(kg)/CTN"),
        ("TOTAL", "N.W(KG)"),
        ("TOTAL", "G.W(KG)"),
        ("Total", "CBM"),
    ]
    texts = []
    for index, (upper, lower) in enumerate(headers):
        left = 100 + 40 * index
        texts.extend([block(upper, left, 95, left + 20, 90), block(lower, left, 86, left + 20, 81)])
    texts.extend(
        [block("#01~#22 Product A", 10, 65, 85, 60), block("#23 Product B", 10, 35, 85, 30)]
    )
    for index, (other, selected) in enumerate(
        zip(
            ("3", "4", "5", "12", "15", "0.35"),
            (selected_count, "7", "9", "14", "18", "0.12"),
            strict=True,
        )
    ):
        left = 100 + 40 * index
        texts.extend(
            [
                block(other, left + 2, 65, left + 18, 60),
                block(selected, left + 2, 35, left + 18, 30),
            ]
        )
    texts.extend(extra_texts)

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


def test_six_fields_are_scoped_to_one_pdf_row_and_bound_to_headers(tmp_path):
    _, _, result = pdf_row_case(tmp_path)
    handoff = read_result(result["result_ref"])["material_handoff"]
    fields = {item["field_name"]: item for item in handoff["candidates"]}
    assert {name: item["field"]["value"] for name, item in fields.items()} == {
        "ctns": 2,
        "line_net_weight": 14,
        "line_gross_weight": 18,
        "unit_net_weight": 7,
        "unit_gross_weight": 9,
        "line_volume": 0.12,
    }
    sources = {source["source_id"]: source for source in handoff["sources"]}
    for item in fields.values():
        selected = [sources[sid] for sid in item["field"]["source_ids"]]
        assert len(selected) == 4
        assert all(source["locator"]["page"] == 1 for source in selected)
        assert any(source["excerpt"].startswith("#23") for source in selected)


def test_duplicate_row_anchor_does_not_take_an_arbitrary_row(tmp_path):
    extra = {
        "text": "#23 Another product",
        "prov": [
            {
                "page_no": 1,
                "bbox": {"l": 10, "t": 15, "r": 85, "b": 10, "coord_origin": "BOTTOMLEFT"},
                "charspan": [0, 19],
            }
        ],
    }
    unrelated = {
        "text": "TOTAL G.W. 999 kg",
        "prov": [
            {
                "page_no": 1,
                "bbox": {"l": 10, "t": 8, "r": 85, "b": 3, "coord_origin": "BOTTOMLEFT"},
                "charspan": [0, 17],
            }
        ],
    }
    _, _, result = pdf_row_case(tmp_path, extra_texts=[extra, unrelated])
    handoff = read_result(result["result_ref"])["material_handoff"]
    assert all(candidate["field"]["value"] is None for candidate in handoff["candidates"])
    assert all(candidate["field"]["status"] == "uncertain" for candidate in handoff["candidates"])


def test_merged_numeric_cells_do_not_guess_carton_count(tmp_path):
    _, _, result = pdf_row_case(tmp_path, selected_count="1 2")
    handoff = read_result(result["result_ref"])["material_handoff"]
    fields = {item["field_name"]: item["field"] for item in handoff["candidates"]}
    assert fields["ctns"]["value"] is None
    assert fields["ctns"]["status"] == "uncertain"
    assert fields["unit_net_weight"]["value"] == 7


def test_ambiguous_value_does_not_hide_other_fields(tmp_path):
    extra = {
        "text": "8",
        "prov": [
            {
                "page_no": 1,
                "bbox": {"l": 142, "t": 35, "r": 158, "b": 30, "coord_origin": "BOTTOMLEFT"},
                "charspan": [0, 1],
            }
        ],
    }
    _, _, result = pdf_row_case(tmp_path, extra_texts=[extra])
    handoff = read_result(result["result_ref"])["material_handoff"]
    fields = {item["field_name"]: item["field"] for item in handoff["candidates"]}
    assert fields["unit_net_weight"]["value"] is None
    assert fields["unit_net_weight"]["status"] == "uncertain"
    assert fields["unit_gross_weight"]["value"] == 9
    assert fields["line_net_weight"]["value"] == 14


def test_overlapping_net_and_gross_headers_do_not_assign_one_value_twice(tmp_path):
    extra = {
        "text": "G.Weight",
        "prov": [
            {
                "page_no": 1,
                "bbox": {"l": 140, "t": 95, "r": 160, "b": 90, "coord_origin": "BOTTOMLEFT"},
                "charspan": [0, 8],
            }
        ],
    }
    _, _, result = pdf_row_case(tmp_path, extra_texts=[extra])
    handoff = read_result(result["result_ref"])["material_handoff"]
    fields = {item["field_name"]: item["field"] for item in handoff["candidates"]}
    assert fields["unit_net_weight"]["value"] is None
    assert fields["unit_gross_weight"]["value"] is None


def test_row_value_or_evidence_cannot_be_changed_after_extraction(tmp_path):
    prepared, profile, result = pdf_row_case(tmp_path)
    snapshot = read_result(result["result_ref"])
    read = read_result(result["read_ref"])
    handoff = snapshot["material_handoff"]
    for change in ("other_row_value", "page"):
        altered = copy.deepcopy(handoff)
        candidate = next(c for c in altered["candidates"] if c["field_name"] == "unit_net_weight")
        if change == "other_row_value":
            candidate["field"]["value"] = 4
        else:
            sid = candidate["field"]["source_ids"][0]
            next(s for s in altered["sources"] if s["source_id"] == sid)["locator"]["page"] = 2
        with pytest.raises(ValueError, match="fixed source evidence"):
            _assess(
                altered,
                profile,
                read,
                prepared["received"]["manifest"],
                tmp_path / "p01",
                snapshot["field_contract_source"],
            )


def test_row_weight_can_be_reconfirmed_from_same_evidence(tmp_path):
    prepared, profile, result = pdf_row_case(tmp_path)
    snapshot = read_result(result["result_ref"])
    manifest = prepared["received"]["manifest"]
    saved = commit_result(
        tmp_path / "store",
        snapshot,
        **context(prepared, profile, scope_for(manifest, profile, True), result),
    )
    cid = next(
        c["candidate_id"]
        for c in snapshot["material_handoff"]["candidates"]
        if c["field_name"] == "unit_net_weight"
    )
    revise_candidates(
        tmp_path / "store",
        saved["result_id"],
        base_revision=1,
        reviewer="reviewer",
        changes=[{"candidate_id": cid, "status": "uncertain", "reason": "review"}],
    )
    with pytest.raises(ValueError, match="fixed source evidence"):
        revise_candidates(
            tmp_path / "store",
            saved["result_id"],
            base_revision=2,
            reviewer="reviewer",
            changes=[{"candidate_id": cid, "status": "known", "value": 4, "reason": "other row"}],
        )
    revised = revise_candidates(
        tmp_path / "store",
        saved["result_id"],
        base_revision=2,
        reviewer="reviewer",
        changes=[
            {"candidate_id": cid, "status": "known", "value": 7, "reason": "source confirmed"}
        ],
    )
    assert revised["revision"] == 3
