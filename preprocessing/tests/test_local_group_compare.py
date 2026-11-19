"""Selected material comparisons preserve their original document scopes."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from local_group_compare import compare_selected, load_response
from test_local_email_index import _indexed_request, _request

pytest_plugins = ["test_local_email_index"]


def response(run_id, email, attachment, gross, volume):
    source = {"email_sha256": email}
    if attachment:
        source.update(attachment_id=attachment, mime_path=attachment)
    return {
        "run_id": run_id,
        "email_id": email,
        "indexed_source": source,
        "result_ref": {"ref": run_id, "sha256": "hash"},
        "fields": [
            {"field_name": name, "value": value, "unit": unit, "status": "known", "sources": []}
            for name, value, unit in [
                ("line_gross_weight", gross, "kg"),
                ("line_volume", volume, "CBM"),
            ]
        ],
        "document_observations": [],
    }


def test_selected_documents_compare_totals_without_claiming_complete_group():
    body = response("body", "mail", None, 420, 1.08)
    first = response("first", "mail", "first-pdf", 140, 0.27)
    second = response("second", "mail", "second-pdf", 280, 0.81)
    for item, count in ((body, 4), (first, 1), (second, 3)):
        item["fields"].append(
            {
                "field_name": "package_count",
                "value": count,
                "unit": None,
                "status": "known",
                "sources": [],
            }
        )
    comparison = compare_selected(body, [first, second])
    assert comparison["selection_complete"] is None
    assert comparison["totals"]["line_gross_weight"]["selected_document_sum"] == 420
    assert comparison["totals"]["line_volume"]["selected_document_sum"] == 1.08
    assert comparison["totals"]["line_volume"]["comparison"] == "match"
    assert comparison["totals"]["package_count"]["selected_document_sum"] == 4
    assert comparison["totals"]["package_count"]["comparison"] == "match"
    assert comparison["derived"]["density_kg_per_m3"]["value"] == pytest.approx(420 / 1.08)
    assert comparison["derived"]["is_large_cargo"]["value"] is None
    assert comparison["derived"]["is_large_cargo"]["status"] == "uncertain"
    assert [item["run_id"] for item in comparison["documents"]] == ["first", "second"]


def test_missing_or_repeated_document_cannot_produce_complete_total():
    body = response("body", "mail", None, 420, 1.08)
    first = response("first", "mail", "first-pdf", 140, 0.27)
    second = response("second", "mail", "second-pdf", 280, 0.81)
    second["fields"][0].update(value=None, status="uncertain")
    assert (
        compare_selected(body, [first, second])["totals"]["line_gross_weight"]["comparison"]
        == "unresolved"
    )
    with pytest.raises(ValueError, match="Duplicate"):
        compare_selected(body, [first, first])


def test_identical_document_bytes_in_distinct_attachments_need_explicit_review():
    body = response("body", "mail", None, 420, 1.08)
    first = response("first", "mail", "a.pdf", 140, 0.27)
    second = response("second", "mail", "b.pdf", 140, 0.27)
    first["indexed_source"]["attachment_sha256"] = "same-content"
    second["indexed_source"]["attachment_sha256"] = "same-content"
    result = compare_selected(body, [first, second])
    assert result["totals"]["line_gross_weight"]["selected_document_sum"] is None
    assert result["totals"]["line_gross_weight"]["comparison"] == "unresolved"
    assert result["possible_duplicate_documents"][0]["run_ids"] == ["first", "second"]
    confirmed = compare_selected(body, [first, second], include_identical_documents=True)
    assert confirmed["totals"]["line_gross_weight"]["selected_document_sum"] == 280


@pytest.mark.parametrize(
    ("gross", "volume", "chargeable", "large"),
    [(1200, 0, 999.99, False), (600, 2, 1000, True), (600, 2, 1000.01, True)],
)
def test_derived_fields_use_chargeable_weight_threshold_and_nonzero_volume(
    gross, volume, chargeable, large
):
    body = response("body", "mail", None, gross, volume)
    body["fields"].append(
        {
            "field_name": "chargeable_weight",
            "value": chargeable,
            "unit": "kg",
            "status": "known",
            "sources": [],
        }
    )
    document = response("pdf", "mail", "a.pdf", gross, volume)
    derived = compare_selected(body, [document])["derived"]
    assert derived["is_large_cargo"]["value"] is large
    if volume == 0:
        assert derived["density_kg_per_m3"]["value"] is None
        assert derived["density_kg_per_m3"]["status"] == "uncertain"
    else:
        assert derived["density_kg_per_m3"]["value"] == pytest.approx(gross / volume)


def test_caller_chargeable_weight_is_used_but_disagreement_is_not_silenced():
    body = response("body", "mail", None, 600, 2)
    document = response("pdf", "mail", "a.pdf", 600, 2)
    selected = compare_selected(body, [document], chargeable_weight_kg=1000)
    assert selected["derived"]["is_large_cargo"]["value"] is True
    assert selected["chargeable_weight"]["caller_input_kg"] == 1000
    body["fields"].append(
        {
            "field_name": "chargeable_weight",
            "value": 999,
            "unit": "kg",
            "status": "known",
            "sources": [],
        }
    )
    conflicted = compare_selected(body, [document], chargeable_weight_kg=1000)
    assert conflicted["chargeable_weight"]["status"] == "conflict"
    assert conflicted["derived"]["is_large_cargo"]["value"] is None


def test_cli_rechecks_snapshots_and_compares_selected_results(indexed_server, tmp_path):
    base, _, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    email = found["emails"][0]
    _, body = _request(
        base,
        "/local/parse",
        {"email_id": email["email_id"], "profile_id": "local-current-body-inquiry"},
    )
    _, pdf = _request(base, "/local/parse", _indexed_request(email, email["attachments"][0]))
    body_path = tmp_path / "runs" / body["run_id"] / "response.json"
    pdf_path = tmp_path / "runs" / pdf["run_id"] / "response.json"
    script = Path(__file__).resolve().parents[1] / "examples" / "local_group_compare.py"
    command = [sys.executable, str(script), "--body", str(body_path), "--pdf", str(pdf_path)]
    completed = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", check=True
    )
    result = json.loads(completed.stdout)
    assert result["totals"]["line_gross_weight"]["comparison"] == "different"
    assert result["totals"]["line_gross_weight"]["selected_document_sum"] == 8
    assert result["selection_complete"] is None
    body["read_ref"] = pdf["read_ref"]
    body_path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(ValueError, match="READ reference"):
        load_response(body_path)


def test_cli_rejects_pdf_from_another_original_email(indexed_server, tmp_path):
    base, _, _ = indexed_server
    _, first_found = _request(base, "/local/index?q=one.eml")
    _, second_found = _request(base, "/local/index?q=two.eml")
    first, second = first_found["emails"][0], second_found["emails"][0]
    _, body = _request(
        base,
        "/local/parse",
        {"email_id": first["email_id"], "profile_id": "local-current-body-inquiry"},
    )
    _, same_pdf = _request(base, "/local/parse", _indexed_request(first, first["attachments"][0]))
    _, other_pdf = _request(
        base, "/local/parse", _indexed_request(second, second["attachments"][0])
    )
    paths = [
        tmp_path / "runs" / result["run_id"] / "response.json"
        for result in (body, same_pdf, other_pdf)
    ]
    before = [path.read_bytes() for path in paths]
    script = Path(__file__).resolve().parents[1] / "examples" / "local_group_compare.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--body",
            str(paths[0]),
            "--pdf",
            str(paths[1]),
            "--pdf",
            str(paths[2]),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode != 0
    assert "same original EML" in completed.stderr
    assert completed.stdout == ""
    assert [path.read_bytes() for path in paths] == before
