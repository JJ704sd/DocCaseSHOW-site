"""Behavioral checks for the local evidence and requirement boundary."""

import copy
import hashlib
import importlib.util
import json
from email.message import EmailMessage
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfWriter

from doccase_preprocessing.cargo_table import cargo_row_profile
from doccase_preprocessing.local_handoff import run_local_handoff
from doccase_preprocessing.main_channel import (
    _assess,
    _facts,
    _section,
    read_result,
    read_validated_result,
    run_main_channel,
)

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = (
    ROOT
    / "structured-output/integration-v03/snapshots/structured-output/evidence/parse-output-0.1.0.schema.json"
)
SPEC = importlib.util.spec_from_file_location(
    "p02_local_read", ROOT / "preprocessing/examples/p02_local_read.py"
)
p02 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(p02)


def prepare(tmp_path, body=None):
    request = json.loads(
        (ROOT / "architecture-integration/fixtures/partial-request.json").read_text(
            encoding="utf-8"
        )
    )
    if body is not None:
        request["email"]["body_text"] = body
    return run_local_handoff(
        json.dumps(request, ensure_ascii=False).encode(),
        tmp_path,
        input_format="json",
        request_reference="intake:test",
    )


def profile_for(prepared):
    body = next(
        item for item in prepared["received"]["manifest"]["materials"] if item["kind"] == "body"
    )
    mid = body["material_id"]
    scope = [
        item["material_id"] + "/representation"
        for item in prepared["received"]["manifest"]["materials"]
    ]
    return {
        "id": "test-profile",
        "version": "1",
        "task_scope_ref": "test-scope",
        "requirements": [
            {
                "requirement_id": rid,
                "field_name": name,
                "material_id": mid,
                "object_selector": mid + "/L1",
                "object_anchor": "L1",
                "source_kind": "current_body",
                "critical": name != "unit_gross_weight",
                "missing_policy": "allow" if name == "unit_gross_weight" else "block",
                "allow_not_applicable": False,
                "scope_dependencies": [{"stage": "extraction", "refs": scope}],
            }
            for rid, name in [
                ("count", "ctns"),
                ("gross", "line_gross_weight"),
                ("unit", "unit_gross_weight"),
                ("volume", "line_volume"),
            ]
        ],
    }


def execute(tmp_path, prepared, profile, *, material_agent=None):
    manifest = prepared["received"]["manifest"]
    selected = {item["material_id"] for item in manifest["materials"] if item["state"] == "ready"}
    return run_main_channel(
        prepared["manifest_path"],
        prepared["manifest_path"].parents[1],
        expected_task_id=manifest["task_id"],
        expected_request_fingerprint=prepared["request_fingerprint"],
        intake_context=prepared["intake_context"],
        selected_material_ids=selected,
        read_configurations=p02.configurations(),
        reader_versions={},
        field_profile=profile,
        field_contract_ref={
            "version": "0.1.0",
            "ref": str(CONTRACT),
            "sha256": hashlib.sha256(CONTRACT.read_bytes()).hexdigest(),
        },
        output_dir=tmp_path,
        reader=p02.parse_prepared,
        material_agent=material_agent,
    )


def test_partial_keeps_field_evidence_and_only_explicit_missing_dependency(tmp_path):
    prepared = prepare(tmp_path / "p01")
    result = execute(tmp_path / "run", prepared, profile_for(prepared))
    saved = read_result(result["result_ref"])
    handoff = saved["material_handoff"]
    candidates = {c["field_name"]: c for c in handoff["candidates"]}
    assert candidates["ctns"]["field"]["value"] == 3
    assert candidates["line_gross_weight"]["field"]["value"] == 315
    assert candidates["line_gross_weight"]["field"]["unit"] == "kg"
    assert candidates["unit_gross_weight"]["field"]["value"] is None
    assert candidates["line_volume"]["field"]["value"] is None
    missing = next(
        i for i in handoff["issues"] if i["code"] == "explicitly_referenced_material_unread"
    )
    assert missing["source_ids"]
    assert any(
        m["material_id"] in missing["material_ids"] and m["availability"] == "missing"
        for m in handoff["material_versions"]
    )
    assert handoff["handoff_state"] == "partial"
    assert {r["requirement_id"] for r in handoff["requirement_assessments"]} == {
        "count",
        "gross",
        "unit",
        "volume",
    }
    for source in handoff["sources"]:
        artifact = json.loads(Path(handoff["artifact_refs"][0]["ref"]).read_text(encoding="utf-8"))
        loc = source["locator"]
        assert artifact["text"][loc["char_start"] : loc["char_end"]] == source["excerpt"]


def test_readback_rejects_changed_per_material_unread_reason(tmp_path):
    prepared = prepare(tmp_path / "p01")
    result = execute(tmp_path / "run", prepared, profile_for(prepared))
    snapshot = read_result(result["result_ref"])
    snapshot["per_material_results"][0]["reason"] = "different from the actual READ"
    raw = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
    target = tmp_path / "changed-result.json"
    target.write_bytes(raw)
    with pytest.raises(ValueError):
        read_validated_result(
            {"ref": str(target), "sha256": hashlib.sha256(raw).hexdigest()},
            content_root=tmp_path / "p01",
        )


@pytest.mark.parametrize("different_group", [False, True])
def test_current_html_total_fields_use_adjacent_label_and_value_without_quoted_body(
    tmp_path, different_group
):
    message = EmailMessage()
    message["From"] = "a@example.invalid"
    message["To"] = "b@example.invalid"
    message.set_content("See the HTML body.")
    message.add_alternative("<p>Total Packages: 11 packages</p>", subtype="html")
    p01 = tmp_path / "p01"
    prepared = run_local_handoff(
        message.as_bytes(),
        p01,
        input_format="eml",
        request_reference="test:body",
        task_id="body-test",
        email_ref_id="body-email",
    )
    manifest = prepared["received"]["manifest"]
    body = next(
        item
        for item in manifest["materials"]
        if item["kind"] == "body" and item["state"] == "ready"
    )
    mid = body["material_id"]
    profile = {
        "id": "local-current-body-inquiry",
        "version": "1",
        "task_scope_ref": manifest["task_id"],
        "requirements": [
            {
                "requirement_id": name,
                "field_name": name,
                "material_id": mid,
                "object_selector": mid + "/document",
                "source_kind": "current_body",
                "critical": True,
                "missing_policy": "block",
                "allow_not_applicable": False,
                "scope_dependencies": [{"stage": "extraction", "refs": [mid + "/representation"]}],
            }
            for name in (
                "package_count",
                "line_gross_weight",
                "line_volume",
                "ctns",
                "chargeable_weight",
            )
        ],
    }
    groups = [{"self_ref": f"#/groups/{i}", "label": "inline"} for i in range(5)]
    texts = [
        {"text": value, "parent": {"$ref": f"#/groups/{group}"}}
        for value, group in [
            ("Total Packages:", 0),
            ("11 packages", 3 if different_group else 0),
            ("Total Gross Weight:", 1),
            ("510 kg", 1),
            ("Total Volume:", 2),
            ("1.39 CBM", 2),
            ("Chargeable Weight:", 4),
            ("1100 kg", 4),
            ("On Monday wrote:", 3),
            ("Total Packages:", 3),
            ("9 packages", 3),
        ]
    ]
    versions = p02.configurations()[("docling", "html")]["versions"]
    cache = tmp_path / "cache"
    cache.mkdir()
    configs = p02.configurations(cache_path=cache, declared_versions=versions)

    def worker(request):
        raw = json.dumps({"texts": texts, "groups": groups, "tables": []}).encode("utf-8")
        path = Path(request["json_path"])
        path.write_bytes(raw)
        return {
            "conversion_status": "success",
            "errors": [],
            "versions": versions,
            "json_path": str(path),
            "json_sha256": hashlib.sha256(raw).hexdigest(),
        }

    contract = {
        "version": "0.1.0",
        "ref": str(CONTRACT.resolve()),
        "sha256": hashlib.sha256(CONTRACT.read_bytes()).hexdigest(),
    }
    result = run_main_channel(
        prepared["manifest_path"],
        p01,
        expected_task_id=manifest["task_id"],
        expected_request_fingerprint=prepared["request_fingerprint"],
        intake_context=prepared["intake_context"],
        selected_material_ids={mid},
        read_configurations=configs,
        reader_versions=versions,
        field_profile=profile,
        field_contract_ref=contract,
        output_dir=tmp_path / "main",
        reader=p02.parse_prepared,
        worker_fn=worker,
    )
    snapshot = read_validated_result(result["result_ref"], content_root=p01)
    handoff = snapshot["material_handoff"]
    fields = {item["field_name"]: item["field"] for item in handoff["candidates"]}
    assert fields["package_count"]["value"] == (None if different_group else 11)
    assert fields["line_gross_weight"]["value"] == 510
    assert fields["line_volume"]["value"] == 1.39
    assert (fields["chargeable_weight"]["value"], fields["chargeable_weight"]["unit"]) == (
        1100,
        "kg",
    )
    assert fields["ctns"]["value"] is None
    assert all(
        source["kind"] == "current_body" and "9 packages" not in source["excerpt"]
        for source in handoff["sources"]
    )


@pytest.mark.parametrize("misaligned_destination", [False, True])
def test_current_html_inquiry_table_uses_only_adjacent_cells_in_the_same_row(
    tmp_path, misaligned_destination
):
    from local_parse_server import current_body_profile

    message = EmailMessage()
    message.set_content("See HTML.")
    message.add_alternative("<p>Inquiry</p>", subtype="html")
    p01 = tmp_path / "p01"
    prepared = run_local_handoff(
        message.as_bytes(),
        p01,
        input_format="eml",
        request_reference="test:table",
        task_id="body-table-test",
        email_ref_id="body-email",
    )
    manifest = prepared["received"]["manifest"]
    body = next(
        item
        for item in manifest["materials"]
        if item["kind"] == "body" and item["state"] == "ready"
    )
    mid = body["material_id"]
    profile = current_body_profile(manifest, body, "local-current-body-inquiry")

    def cell(row, col, text):
        return {
            "text": text,
            "start_row_offset_idx": row,
            "end_row_offset_idx": row + 1,
            "start_col_offset_idx": col,
            "end_col_offset_idx": col + 1,
            "column_header": False,
            "row_header": False,
        }

    cells = [
        cell(0, 0, "INCOTERM"),
        cell(0, 1, "EXW"),
        cell(1, 0, "ORIGIN"),
        cell(1, 1, "CHINA"),
        cell(2, 0, "DESTINATION"),
        cell(3 if misaligned_destination else 2, 1, "LIMA"),
        cell(4, 0, "CARGO WILL BE READY DATE:"),
        cell(4, 1, "16/12/2026"),
        cell(5, 0, "TOTAL CBM:"),
        cell(5, 1, "5"),
        cell(6, 0, "COMMODITY"),
        cell(6, 1, "GENERAL:"),
        cell(6, 2, "YES"),
    ]
    groups = [{"self_ref": "#/groups/0", "label": "inline"}]
    texts = [
        {"text": text, "parent": {"$ref": "#/groups/0"}}
        for text in ("CHARGEABLE WEIGHT:", "1183,00 kg")
    ]
    versions = p02.configurations()[("docling", "html")]["versions"]
    cache = tmp_path / "cache"
    cache.mkdir()

    def worker(request):
        data = json.dumps(
            {"texts": texts, "groups": groups, "tables": [{"data": {"table_cells": cells}}]}
        ).encode()
        path = Path(request["json_path"])
        path.write_bytes(data)
        return {
            "conversion_status": "success",
            "errors": [],
            "versions": versions,
            "json_path": str(path),
            "json_sha256": hashlib.sha256(data).hexdigest(),
        }

    contract = {
        "version": "0.1.0",
        "ref": str(CONTRACT.resolve()),
        "sha256": hashlib.sha256(CONTRACT.read_bytes()).hexdigest(),
    }
    result = run_main_channel(
        prepared["manifest_path"],
        p01,
        expected_task_id=manifest["task_id"],
        expected_request_fingerprint=prepared["request_fingerprint"],
        intake_context=prepared["intake_context"],
        selected_material_ids={mid},
        read_configurations=p02.configurations(cache_path=cache, declared_versions=versions),
        reader_versions=versions,
        field_profile=profile,
        field_contract_ref=contract,
        output_dir=tmp_path / "main",
        reader=p02.parse_prepared,
        worker_fn=worker,
    )
    handoff = read_validated_result(result["result_ref"], content_root=p01)["material_handoff"]
    fields = {item["field_name"]: item["field"] for item in handoff["candidates"]}
    for name, value in [
        ("trade_terms", "EXW"),
        ("origin", "CHINA"),
        ("cargo_ready_time", "16/12/2026"),
        ("line_volume", 5),
        ("chargeable_weight", 1183),
    ]:
        assert fields[name]["value"] == value
        assert len(fields[name]["source_ids"]) == 2
    assert fields["line_volume"]["unit"] == "CBM"
    assert fields["chargeable_weight"]["unit"] == "kg"
    assert fields["destination"]["value"] == (None if misaligned_destination else "LIMA")
    assert fields["cargo_description"]["value"] is None
    assert list(_facts("chargeable_weight", "Chargeable Weight: 1,234 kg")) == []


def test_current_html_table_before_forwarded_history_keeps_values_and_sources(tmp_path):
    from local_parse_server import current_body_profile

    message = EmailMessage()
    message.set_content("See HTML.")
    message.add_alternative("<p>Inquiry</p>", subtype="html")
    p01 = tmp_path / "p01"
    prepared = run_local_handoff(
        message.as_bytes(),
        p01,
        input_format="eml",
        request_reference="test:current-table",
        task_id="body-current-table",
        email_ref_id="body-email",
    )
    manifest = prepared["received"]["manifest"]
    body = next(
        item
        for item in manifest["materials"]
        if item["kind"] == "body" and item["state"] == "ready"
    )
    mid = body["material_id"]
    profile = current_body_profile(manifest, body, "local-current-body-inquiry")

    def cell(row, col, text):
        return {
            "text": text,
            "start_row_offset_idx": row,
            "end_row_offset_idx": row + 1,
            "start_col_offset_idx": col,
            "end_col_offset_idx": col + 1,
            "column_header": False,
            "row_header": False,
        }

    def table(values):
        return {
            "data": {
                "table_cells": [
                    cell(row, col, text)
                    for row, pair in enumerate(values)
                    for col, text in enumerate(pair)
                ]
            }
        }

    tables = [
        table(
            [
                ("No. Cartons", "13 Cartons"),
                ("Gross weight", "120.5 KG"),
                ("CBM", "0.5125 CBM"),
                ("Terms", "FOB Xiamen"),
            ]
        ),
        table([("No. Cartons", "77 Cartons"), ("Terms", "CIF")]),
    ]
    document = {
        "body": {
            "children": [
                {"$ref": "#/texts/0"},
                {"$ref": "#/tables/0"},
                {"$ref": "#/texts/1"},
                {"$ref": "#/tables/1"},
            ]
        },
        "texts": [{"text": "Please quote."}, {"text": "From: previous sender"}],
        "groups": [],
        "tables": tables,
    }
    versions = p02.configurations()[("docling", "html")]["versions"]
    cache = tmp_path / "cache"
    cache.mkdir()

    def worker(request):
        raw = json.dumps(document).encode()
        Path(request["json_path"]).write_bytes(raw)
        return {
            "conversion_status": "success",
            "errors": [],
            "versions": versions,
            "json_path": request["json_path"],
            "json_sha256": hashlib.sha256(raw).hexdigest(),
        }

    contract = {
        "version": "0.1.0",
        "ref": str(CONTRACT.resolve()),
        "sha256": hashlib.sha256(CONTRACT.read_bytes()).hexdigest(),
    }
    result = run_main_channel(
        prepared["manifest_path"],
        p01,
        expected_task_id=manifest["task_id"],
        expected_request_fingerprint=prepared["request_fingerprint"],
        intake_context=prepared["intake_context"],
        selected_material_ids={mid},
        read_configurations=p02.configurations(cache_path=cache, declared_versions=versions),
        reader_versions=versions,
        field_profile=profile,
        field_contract_ref=contract,
        output_dir=tmp_path / "main",
        reader=p02.parse_prepared,
        worker_fn=worker,
    )
    handoff = read_validated_result(result["result_ref"], content_root=p01)["material_handoff"]
    fields = {item["field_name"]: item["field"] for item in handoff["candidates"]}
    for name, expected, unit in [
        ("ctns", 13, None),
        ("line_gross_weight", 120.5, "kg"),
        ("line_volume", 0.5125, "CBM"),
        ("trade_terms", "FOB", None),
    ]:
        field = fields[name]
        assert (field["value"], field.get("unit"), field["status"]) == (expected, unit, "known")
        assert len(field["source_ids"]) == 2
    assert all(
        "77 Cartons" not in source["excerpt"] and "CIF" not in source["excerpt"]
        for source in handoff["sources"]
    )


@pytest.mark.parametrize(
    ("statement", "count", "kind"),
    [
        ("TOTAL 1 WOODEN BOX OF 347 KGS", 1, "wooden_case"),
        ("TOTAL: 2 PALLETS, 1 WOODEN BOX", None, None),
    ],
)
def test_current_body_explicit_package_totals_keep_kind_and_scope(tmp_path, statement, count, kind):
    from local_parse_server import current_body_profile

    prepared = prepare(
        tmp_path / "p01", "TERMS: EXW\n" + statement + "\nOn Monday wrote:\nTOTAL 9 CARTONS"
    )
    manifest = prepared["received"]["manifest"]
    body = next(item for item in manifest["materials"] if item["kind"] == "body")
    result = execute(
        tmp_path / "run",
        prepared,
        current_body_profile(manifest, body, "local-current-body-inquiry"),
    )
    handoff = read_validated_result(result["result_ref"], content_root=tmp_path / "p01")[
        "material_handoff"
    ]
    fields = {item["field_name"]: item["field"] for item in handoff["candidates"]}
    assert fields["package_count"]["value"] == count
    assert fields["package_type"]["value"] == kind
    assert fields["ctns"]["value"] is None
    if count:
        assert fields["package_count"]["source_ids"] and fields["package_type"]["source_ids"]
    assert all("9 CARTONS" not in source["excerpt"] for source in handoff["sources"])


@pytest.mark.parametrize(
    ("statement", "status", "value"),
    [
        (
            "L1: Total Packages: 11 packages\nOn Monday wrote:\nL1: Total Packages: 9 packages",
            "known",
            11,
        ),
        ("L1: Total Packages: 11 packages; Total Packages: 5 packages", "conflict", None),
    ],
)
def test_body_total_package_count_preserves_current_scope_and_conflicts(
    tmp_path, statement, status, value
):
    prepared = prepare(tmp_path / "p01", statement)
    profile = profile_for(prepared)
    requirement = dict(profile["requirements"][0])
    requirement.update(requirement_id="package-count", field_name="package_count")
    profile["requirements"].append(requirement)
    result = execute(tmp_path / "run", prepared, profile)
    snapshot = read_validated_result(result["result_ref"], content_root=tmp_path / "p01")
    fields = [
        item["field"]
        for item in snapshot["material_handoff"]["candidates"]
        if item["field_name"] == "package_count"
    ]
    assert fields and all(field["status"] == status and field["value"] == value for field in fields)


def test_inquiry_conflicts_and_quoted_text_are_kept_separate(tmp_path):
    from local_parse_server import current_body_profile

    body = (
        "Incoterm: FCA Ningbo\nIncoterm: EXW Ningbo\nDestination: SCL\n"
        "On Monday wrote:\nDestination: Hamburg\n"
    )
    prepared = prepare(tmp_path / "p01", body)
    manifest = prepared["received"]["manifest"]
    target = next(item for item in manifest["materials"] if item["kind"] == "body")
    profile = current_body_profile(manifest, target, "local-current-body-inquiry")
    result = execute(tmp_path / "run", prepared, profile)
    handoff = read_validated_result(result["result_ref"], content_root=tmp_path / "p01")[
        "material_handoff"
    ]
    terms = [item["field"] for item in handoff["candidates"] if item["field_name"] == "trade_terms"]
    assert len(terms) == 2 and all(
        item["status"] == "conflict" and item["value"] is None for item in terms
    )
    assert any(issue["code"] == "CONFLICTING_VALUES" for issue in handoff["issues"])
    destination = next(
        item["field"] for item in handoff["candidates"] if item["field_name"] == "destination"
    )
    assert (destination["value"], destination["status"]) == ("SCL", "known")
    assert all("Hamburg" not in source["excerpt"] for source in handoff["sources"])


@pytest.mark.parametrize(("terms", "expected"), [("Ex- Works", "EXW"), ("FOB", "FOB")])
def test_current_body_short_labels_keep_their_own_units_and_ignore_history(
    tmp_path, terms, expected
):
    from local_parse_server import current_body_profile

    body = (
        f"Terms : {terms}\nG. Weight : 3262 kgs\nMeasures: 0.81 CBM\n"
        "On Monday wrote:\nTerms: CIF\nG. Weight : 9 kgs\n"
    )
    prepared = prepare(tmp_path / "p01", body)
    manifest = prepared["received"]["manifest"]
    target = next(item for item in manifest["materials"] if item["kind"] == "body")
    profile = current_body_profile(manifest, target, "local-current-body-inquiry")
    result = execute(tmp_path / "run", prepared, profile)
    handoff = read_validated_result(result["result_ref"], content_root=tmp_path / "p01")[
        "material_handoff"
    ]
    fields = {item["field_name"]: item["field"] for item in handoff["candidates"]}
    assert (fields["trade_terms"]["value"], fields["trade_terms"]["status"]) == (expected, "known")
    assert (fields["line_gross_weight"]["value"], fields["line_gross_weight"]["unit"]) == (
        3262,
        "kg",
    )
    assert (fields["line_volume"]["value"], fields["line_volume"]["unit"]) == (0.81, "CBM")
    assert all(
        "CIF" not in source["excerpt"] and "9 kgs" not in source["excerpt"]
        for source in handoff["sources"]
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Please quote on EXW terms.", "EXW"),
        ("Please quote under FCA terms.", "FCA"),
        ("Please quote in CPT terms.", "CPT"),
        ("The EXW price is pending.", None),
        ("Please do not quote under EXW terms.", None),
        ("Please see the attachment.\nOn Monday wrote:\nPlease quote on EXW terms.", None),
    ],
)
def test_current_body_trade_terms_require_explicit_context_and_ignore_history(
    tmp_path, body, expected
):
    from local_parse_server import current_body_profile

    prepared = prepare(tmp_path / "p01", body)
    manifest = prepared["received"]["manifest"]
    target = next(item for item in manifest["materials"] if item["kind"] == "body")
    result = execute(
        tmp_path / "run",
        prepared,
        current_body_profile(manifest, target, "local-current-body-inquiry"),
    )
    handoff = read_validated_result(result["result_ref"], content_root=tmp_path / "p01")[
        "material_handoff"
    ]
    terms = next(
        item["field"] for item in handoff["candidates"] if item["field_name"] == "trade_terms"
    )
    assert terms["value"] == expected
    assert terms["status"] == ("known" if expected else "uncertain")
    assert bool(terms["source_ids"]) == bool(expected)


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("Please share your best air rates to LHR.", "LHR"),
        ("Please quote rates to LHR and FRA.", None),
    ],
)
def test_current_body_natural_language_destination_keeps_alternatives_unresolved(
    tmp_path, statement, expected
):
    from local_parse_server import current_body_profile

    prepared = prepare(tmp_path / "p01", statement)
    manifest = prepared["received"]["manifest"]
    target = next(item for item in manifest["materials"] if item["kind"] == "body")
    result = execute(
        tmp_path / "run",
        prepared,
        current_body_profile(manifest, target, "local-current-body-inquiry"),
    )
    handoff = read_validated_result(result["result_ref"], content_root=tmp_path / "p01")[
        "material_handoff"
    ]
    field = next(
        item["field"] for item in handoff["candidates"] if item["field_name"] == "destination"
    )
    assert field["value"] == expected
    assert field["status"] == ("known" if expected else "uncertain")
    assert bool(field["source_ids"]) == bool(expected)


def test_current_body_cartons_weight_and_volume_share_one_total_line(tmp_path):
    from local_parse_server import current_body_profile

    prepared = prepare(tmp_path / "p01", "12 ctns - 190 KG - 2.50 CBM")
    manifest = prepared["received"]["manifest"]
    target = next(item for item in manifest["materials"] if item["kind"] == "body")
    result = execute(
        tmp_path / "run",
        prepared,
        current_body_profile(manifest, target, "local-current-body-inquiry"),
    )
    handoff = read_validated_result(result["result_ref"], content_root=tmp_path / "p01")[
        "material_handoff"
    ]
    field = next(
        item["field"] for item in handoff["candidates"] if item["field_name"] == "line_volume"
    )
    assert (field["value"], field.get("unit"), field["status"]) == (2.5, "CBM", "known")
    assert field["source_ids"]


def test_current_body_new_count_excludes_original_count_in_same_sentence(tmp_path):
    from local_parse_server import current_body_profile

    prepared = prepare(
        tmp_path / "p01",
        "New cargo: 12 cartons, while the original one was 3 cartons.\nDestination: LHR",
    )
    manifest = prepared["received"]["manifest"]
    target = next(item for item in manifest["materials"] if item["kind"] == "body")
    result = execute(
        tmp_path / "run",
        prepared,
        current_body_profile(manifest, target, "local-current-body-inquiry"),
    )
    handoff = read_validated_result(result["result_ref"], content_root=tmp_path / "p01")[
        "material_handoff"
    ]
    fields = {item["field_name"]: item["field"] for item in handoff["candidates"]}
    assert (fields["ctns"]["value"], fields["ctns"]["status"]) == (12, "known")
    assert fields["destination"]["value"] == "LHR"
    assert all("3 cartons" not in source["excerpt"] for source in handoff["sources"])


@pytest.mark.parametrize("mismatched_row", [False, True])
def test_current_body_table_total_pairs_gross_and_volume_on_one_row(tmp_path, mismatched_row):
    from local_parse_server import current_body_profile

    message = EmailMessage()
    message.set_content("See HTML.")
    message.add_alternative("<p>Inquiry</p>", subtype="html")
    p01 = tmp_path / "p01"
    prepared = run_local_handoff(
        message.as_bytes(),
        p01,
        input_format="eml",
        request_reference="test:totals",
        task_id="body-total-test",
        email_ref_id="body-email",
    )
    manifest = prepared["received"]["manifest"]
    body = next(
        item
        for item in manifest["materials"]
        if item["kind"] == "body" and item["state"] == "ready"
    )
    mid = body["material_id"]
    profile = current_body_profile(manifest, body, "local-current-body-inquiry")

    def cell(row, col, text):
        return {
            "text": text,
            "start_row_offset_idx": row,
            "end_row_offset_idx": row + 1,
            "start_col_offset_idx": col,
            "end_col_offset_idx": col + 1,
            "column_header": row == 0,
            "row_header": False,
        }

    cells = [
        cell(0, 0, "PACKAGE"),
        cell(0, 1, "CBM"),
        cell(0, 2, "GROSS WEIGHT"),
        cell(1, 2, "TOTAL"),
        cell(2, 0, "4x"),
        cell(3 if mismatched_row else 2, 1, "7.01m³"),
        cell(2, 2, "1183.00kg"),
    ]
    versions = p02.configurations()[("docling", "html")]["versions"]
    cache = tmp_path / "cache"
    cache.mkdir()

    def worker(request):
        data = json.dumps({"texts": [], "tables": [{"data": {"table_cells": cells}}]}).encode()
        path = Path(request["json_path"])
        path.write_bytes(data)
        return {
            "conversion_status": "success",
            "errors": [],
            "versions": versions,
            "json_path": str(path),
            "json_sha256": hashlib.sha256(data).hexdigest(),
        }

    contract = {
        "version": "0.1.0",
        "ref": str(CONTRACT.resolve()),
        "sha256": hashlib.sha256(CONTRACT.read_bytes()).hexdigest(),
    }
    result = run_main_channel(
        prepared["manifest_path"],
        p01,
        expected_task_id=manifest["task_id"],
        expected_request_fingerprint=prepared["request_fingerprint"],
        intake_context=prepared["intake_context"],
        selected_material_ids={mid},
        read_configurations=p02.configurations(cache_path=cache, declared_versions=versions),
        reader_versions=versions,
        field_profile=profile,
        field_contract_ref=contract,
        output_dir=tmp_path / "main",
        reader=p02.parse_prepared,
        worker_fn=worker,
    )
    handoff = read_validated_result(result["result_ref"], content_root=p01)["material_handoff"]
    fields = {item["field_name"]: item["field"] for item in handoff["candidates"]}
    assert fields["line_gross_weight"]["value"] == (None if mismatched_row else 1183)
    assert fields["line_volume"]["value"] == (None if mismatched_row else 7.01)
    assert fields["chargeable_weight"]["value"] is None
    if not mismatched_row:
        sources = {source["source_id"]: source["excerpt"] for source in handoff["sources"]}
        assert {"TOTAL", "GROSS WEIGHT", "1183.00kg", "CBM", "7.01m³"} == {
            sources[sid] for sid in fields["line_gross_weight"]["source_ids"]
        }


@pytest.mark.parametrize("complete_headers", [False, True])
@pytest.mark.parametrize("separate_groups", [False, True])
def test_current_html_excludes_split_forwarded_email_headers(
    tmp_path, complete_headers, separate_groups
):
    from local_parse_server import current_body_profile

    message = EmailMessage()
    message.set_content("See HTML.")
    message.add_alternative("<p>Inquiry</p>", subtype="html")
    p01 = tmp_path / "p01"
    prepared = run_local_handoff(
        message.as_bytes(),
        p01,
        input_format="eml",
        request_reference="test:quoted",
        task_id="body-quote-test",
        email_ref_id="body-email",
    )
    manifest = prepared["received"]["manifest"]
    body = next(
        item
        for item in manifest["materials"]
        if item["kind"] == "body" and item["state"] == "ready"
    )
    profile = current_body_profile(manifest, body, "local-current-body-inquiry")
    parts = ["From:", "Sender"] + (
        ["Sent:", "Yesterday", "To:", "Recipient"] if complete_headers else []
    )
    texts = [{"text": "Terms: FOB"}]
    texts += [
        {"text": value, "parent": {"$ref": f"#/groups/{index // 2 if separate_groups else 0}"}}
        for index, value in enumerate(parts)
    ]
    texts += [{"text": "Terms: EXW"}]
    groups = [
        {"self_ref": f"#/groups/{index}", "label": "inline"}
        for index in range((len(parts) + 1) // 2 if separate_groups else 1)
    ]
    versions = p02.configurations()[("docling", "html")]["versions"]
    cache = tmp_path / "cache"
    cache.mkdir()

    def worker(request):
        data = json.dumps({"texts": texts, "groups": groups, "tables": []}).encode()
        path = Path(request["json_path"])
        path.write_bytes(data)
        return {
            "conversion_status": "success",
            "errors": [],
            "versions": versions,
            "json_path": str(path),
            "json_sha256": hashlib.sha256(data).hexdigest(),
        }

    contract = {
        "version": "0.1.0",
        "ref": str(CONTRACT.resolve()),
        "sha256": hashlib.sha256(CONTRACT.read_bytes()).hexdigest(),
    }
    result = run_main_channel(
        prepared["manifest_path"],
        p01,
        expected_task_id=manifest["task_id"],
        expected_request_fingerprint=prepared["request_fingerprint"],
        intake_context=prepared["intake_context"],
        selected_material_ids={body["material_id"]},
        read_configurations=p02.configurations(cache_path=cache, declared_versions=versions),
        reader_versions=versions,
        field_profile=profile,
        field_contract_ref=contract,
        output_dir=tmp_path / "main",
        reader=p02.parse_prepared,
        worker_fn=worker,
    )
    handoff = read_validated_result(result["result_ref"], content_root=p01)["material_handoff"]
    terms = [item["field"] for item in handoff["candidates"] if item["field_name"] == "trade_terms"]
    if complete_headers:
        assert len(terms) == 1 and (terms[0]["value"], terms[0]["status"]) == ("FOB", "known")
        assert all("EXW" not in source["excerpt"] for source in handoff["sources"])
    else:
        assert len(terms) == 2 and all(item["status"] == "conflict" for item in terms)


def test_unlinked_missing_attachment_does_not_become_volume_dependency(tmp_path):
    prepared = prepare(tmp_path / "p01", "L1: 3 cartons, Total G.W. 315 kg. Please quote.")
    handoff = read_result(execute(tmp_path / "run", prepared, profile_for(prepared))["result_ref"])[
        "material_handoff"
    ]
    assert not any(i["code"] == "explicitly_referenced_material_unread" for i in handoff["issues"])
    assert (
        next(a for a in handoff["requirement_assessments"] if a["requirement_id"] == "volume")[
            "state"
        ]
        == "unresolved"
    )


def test_cargo_details_volume_is_extracted_with_source(tmp_path):
    prepared = prepare(tmp_path / "p01", "L1: Cargo details: 9CTNS 180KG 1.20CBM")
    result = execute(tmp_path / "run", prepared, profile_for(prepared))
    handoff = read_result(result["result_ref"])["material_handoff"]
    volume = next(item for item in handoff["candidates"] if item["field_name"] == "line_volume")
    assert volume["field"]["value"] == 1.2
    assert volume["field"]["unit"] == "CBM"
    source_id = volume["field"]["source_ids"][0]
    source = next(item for item in handoff["sources"] if item["source_id"] == source_id)
    assert source["excerpt"] == "Cargo details: 9CTNS 180KG 1.20CBM"


def test_total_gross_abbreviation_is_extracted_with_source(tmp_path):
    prepared = prepare(tmp_path / "p01", "L1: TOTAL G.150.75KG")
    result = execute(tmp_path / "run", prepared, profile_for(prepared))
    handoff = read_result(result["result_ref"])["material_handoff"]
    gross = next(
        item for item in handoff["candidates"] if item["field_name"] == "line_gross_weight"
    )
    assert gross["field"]["value"] == 150.75
    assert gross["field"]["unit"] == "kg"
    source_id = gross["field"]["source_ids"][0]
    source = next(item for item in handoff["sources"] if item["source_id"] == source_id)
    assert source["excerpt"] == "TOTAL G.150.75KG"


@pytest.mark.parametrize(
    "statement",
    [
        "Cargo details: 9CTNS 180KG per carton 1.20CBM",
        "Cargo details: 9CTNS 180KG 1.20CBM or 2.2CBM",
        "Cargo details: 9CTNS 180KG not 1.20CBM",
    ],
)
def test_cargo_details_does_not_guess_per_item_conflicting_or_negated_volume(tmp_path, statement):
    prepared = prepare(tmp_path / "p01", "L1: " + statement)
    result = execute(tmp_path / "run", prepared, profile_for(prepared))
    handoff = read_result(result["result_ref"])["material_handoff"]
    volume = next(item for item in handoff["candidates"] if item["field_name"] == "line_volume")
    assert volume["field"]["value"] is None
    assert volume["field"]["status"] == "uncertain"


@pytest.mark.parametrize(
    "body",
    [
        "L1: Please quote.\nL2: 4 cartons, Total G.W. 318 kg.",
        "L1: Please quote. L2: 4 cartons, Total G.W. 318 kg.",
        "L1: Please quote. L10: 4 cartons, Total G.W. 318 kg.",
        "L10: 4 cartons, Total G.W. 318 kg.",
    ],
)
def test_object_anchor_does_not_take_values_from_another_line_or_label(tmp_path, body):
    prepared = prepare(tmp_path / "p01", body)
    handoff = read_result(execute(tmp_path / "run", prepared, profile_for(prepared))["result_ref"])[
        "material_handoff"
    ]
    fields = {item["field_name"]: item["field"] for item in handoff["candidates"]}
    assert fields["ctns"]["status"] == "uncertain"
    assert fields["ctns"]["value"] is None
    assert fields["line_gross_weight"]["status"] == "uncertain"
    assert fields["line_gross_weight"]["value"] is None


def test_ambiguous_object_line_keeps_known_evidence_but_leaves_requirement_unresolved(tmp_path):
    prepared = prepare(
        tmp_path / "p01",
        "L1: 3 cartons, Total G.W. 315 kg.\nL1: Please quote. L2: 4 cartons, Total G.W. 318 kg.",
    )
    handoff = read_result(execute(tmp_path / "run", prepared, profile_for(prepared))["result_ref"])[
        "material_handoff"
    ]
    count = next(c for c in handoff["candidates"] if c["field_name"] == "ctns")
    assessment = next(
        a for a in handoff["requirement_assessments"] if a["requirement_id"] == "count"
    )
    assert count["field"]["value"] == 3
    assert assessment["state"] == "unresolved"
    assert any(issue["code"] == "object_scope_ambiguous" for issue in handoff["issues"])
    scope_ref = count["object_id"].split("/", 1)[0] + "/representation"
    assert scope_ref in handoff["coverage"]["parsing"]["processed_refs"]
    assert scope_ref in handoff["coverage"]["extraction"]["unread_refs"]
    assert scope_ref not in handoff["coverage"]["extraction"]["processed_refs"]
    assert scope_ref in handoff["coverage"]["aggregation"]["unread_refs"]


def test_missing_profile_keeps_requested_not_started_and_no_handoff(tmp_path):
    prepared = prepare(tmp_path / "p01")
    result = execute(tmp_path / "run", prepared, None)
    assert result["result_ref"] is None
    assert result["reason"] == "required_field_profile_missing"
    progress = read_result(result["progress_ref"])
    assert progress["extraction"] == {
        "requested": True,
        "stage_state": "not_started",
        "reason": "required_field_profile_missing",
    }
    assert read_result(result["read_ref"])["executions"][0]["execution_state"] == "success"
    assert not (tmp_path / "run/material-result.json").exists()


def test_rules_distinguish_weight_scope_type_and_units():
    text = "Total G.W. 1000 g; each N.W. 2 lb; gross weight 9 kg"
    assert [value for _, value, _ in _facts("line_gross_weight", text)] == [1]
    assert [value for _, value, _ in _facts("unit_net_weight", text)] == [0.90718474]
    assert list(_facts("unit_gross_weight", text)) == []
    assert list(_facts("line_net_weight", text)) == []


def test_quoted_section_requires_ordered_header_evidence():
    markers = ["De:", "Enviado el:", "earlier date"]
    blocks = [
        {"pointer": f"/texts/{i}/text", "text": value}
        for i, value in enumerate(markers + ["Cargo details: 9CTNS"])
    ]
    section = _section({"quoted_section_markers": markers}, blocks, blocks[-1])
    assert section["kind"] == "quoted_body"
    assert [item["excerpt"] for item in section["markers"]] == markers
    with pytest.raises(ValueError, match="markers"):
        _section({"quoted_section_markers": markers[::-1]}, blocks, blocks[-1])


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("all cartons have the same weight", 105),
        ("not all cartons have the same weight", None),
    ],
)
def test_per_item_derivation_needs_positive_equal_weight_evidence(tmp_path, statement, expected):
    prepared = prepare(tmp_path / "p01", f"L1: 3 cartons, Total G.W. 315 kg; {statement}.")
    handoff = read_result(execute(tmp_path / "run", prepared, profile_for(prepared))["result_ref"])[
        "material_handoff"
    ]
    unit = next(c for c in handoff["candidates"] if c["field_name"] == "unit_gross_weight")
    assert unit["field"]["value"] == expected
    if expected is not None:
        assert unit["field"]["origin"] == "calculated"
        assert len(unit["derived_from"]) == 2
        assert len(unit["field"]["source_ids"]) == 3


def test_result_hash_detects_modified_snapshot(tmp_path):
    prepared = prepare(tmp_path / "p01")
    reference = execute(tmp_path / "run", prepared, profile_for(prepared))["result_ref"]
    Path(reference["ref"]).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256"):
        read_result(reference)


def test_consumer_rejects_changed_source_and_false_read_coverage(tmp_path):
    prepared = prepare(tmp_path / "p01")
    profile = profile_for(prepared)
    result = execute(tmp_path / "run", prepared, profile)
    snapshot = read_result(result["result_ref"])
    read = read_result(result["read_ref"])
    manifest = prepared["received"]["manifest"]
    contract = snapshot["field_contract_source"]
    root = prepared["manifest_path"].parents[1]
    handoff = snapshot["material_handoff"]
    _assess(handoff, profile, read, manifest, root, contract)
    broken = copy.deepcopy(handoff)
    broken["sources"][0]["locator"]["char_start"] += 1
    with pytest.raises(ValueError, match="excerpt"):
        _assess(broken, profile, read, manifest, root, contract)
    broken = copy.deepcopy(handoff)
    broken["sources"][0]["email_ref_id"] = "another-email"
    with pytest.raises(ValueError, match="email or occurrence"):
        _assess(broken, profile, read, manifest, root, contract)
    broken = copy.deepcopy(handoff)
    stage = broken["coverage"]["parsing"]
    stage["processed_refs"].append(stage["unread_refs"].pop())
    with pytest.raises(ValueError, match="actual READ"):
        _assess(broken, profile, read, manifest, root, contract)
    broken = copy.deepcopy(handoff)
    broken["requirement_assessments"].pop()
    with pytest.raises(ValueError, match="requirements"):
        _assess(broken, profile, read, manifest, root, contract)


def test_malformed_profile_fails_without_starting_reader(tmp_path):
    prepared = prepare(tmp_path / "p01")
    profile = copy.deepcopy(profile_for(prepared))
    profile["requirements"][0]["field_name"] = "unknown"
    with pytest.raises(ValueError, match="Unsupported field"):
        execute(tmp_path / "run", prepared, profile)
    assert not (tmp_path / "run/read-result.json").exists()


@pytest.mark.parametrize(
    ("key", "value"), [("object_anchor", 42), ("object_anchor", ""), ("source_pattern", "[")]
)
def test_invalid_profile_scope_selector_fails_before_reader(tmp_path, key, value):
    prepared = prepare(tmp_path / "p01")
    profile = copy.deepcopy(profile_for(prepared))
    profile["requirements"][0][key] = value
    with pytest.raises(ValueError, match="scope selector"):
        execute(tmp_path / "run", prepared, profile)
    assert not (tmp_path / "run/read-result.json").exists()


def test_material_agent_uses_markdown_and_preserves_conflict_and_unknown(tmp_path):
    from local_parse_server import current_body_profile

    paragraphs = (
        "Destination: Paris",
        "FOB Origin to Madrid",
        "On Monday wrote:",
        "Destination: Cairo",
    )
    mail = EmailMessage()
    mail.set_content("See HTML")
    mail.add_alternative(
        "<p>Destination: Paris</p><p>FOB Origin to Madrid</p>"
        "<blockquote><p>On Monday wrote:</p><p>Destination: Cairo</p></blockquote>",
        subtype="html",
    )
    p01 = tmp_path / "p01"
    prepared = run_local_handoff(
        mail.as_bytes(),
        p01,
        input_format="eml",
        request_reference="test:agent",
        task_id="agent-test",
        email_ref_id="agent-email",
    )
    manifest = prepared["received"]["manifest"]
    body = next(x for x in manifest["materials"] if x["kind"] == "body" and x["state"] == "ready")
    profile = current_body_profile(manifest, body, "local-current-body-inquiry")
    versions = p02.configurations()[("docling", "html")]["versions"]
    cache = tmp_path / "cache"
    cache.mkdir()

    def worker(request):
        raw = json.dumps(
            {"texts": [{"text": text} for text in paragraphs], "groups": [], "tables": []}
        ).encode()
        path = Path(request["json_path"])
        path.write_bytes(raw)
        return {
            "conversion_status": "success",
            "errors": [],
            "versions": versions,
            "json_path": str(path),
            "json_sha256": hashlib.sha256(raw).hexdigest(),
        }

    def provider(execution, record, _records, _content, destination):
        destination.mkdir(parents=True)
        markdown, index = destination / "content.md", destination / "index.json"
        raw = "\n\n".join(paragraphs).encode()
        markdown.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        fragments, offset = [], 0
        for i, text in enumerate(paragraphs):
            fragments.append(
                {
                    "evidence_id": f"E{i}",
                    "source_role": "current" if i < 2 else "quoted",
                    "read_pointer": f"/texts/{i}/text",
                    "read_start": 0,
                    "read_end": len(text),
                    "markdown_start": offset,
                    "markdown_end": offset + len(text),
                }
            )
            offset += len(text) + 2
        raw_index = json.dumps(
            {
                "markdown_sha256": digest,
                "read_sha256": execution["artifact_hash"],
                "material_id": record["material_id"],
                "execution_id": execution["execution_id"],
                "fragments": fragments,
            }
        ).encode()
        index.write_bytes(raw_index)
        return {
            "state": "success",
            "markdown_ref": str(markdown),
            "markdown_sha256": digest,
            "index_ref": str(index),
            "index_sha256": hashlib.sha256(raw_index).hexdigest(),
        }

    calls = []

    def material_agent(request, *, timeout_seconds):
        calls.append(request)
        assert {x["text"] for x in request["evidence"]} == set(paragraphs[:2])
        assert all(x["representation"] == "markdown" for x in request["evidence"])
        ids = {x["text"]: x["evidence_id"] for x in request["evidence"]}
        return {
            "candidates": [
                {
                    "requirement_id": "REQ-DESTINATION",
                    "value": "Madrid",
                    "evidence_ids": [ids[paragraphs[1]]],
                },
                {
                    "requirement_id": "REQ-LINE_VOLUME",
                    "value": 44,
                    "evidence_ids": [ids[paragraphs[0]]],
                },
            ]
        }

    result = run_main_channel(
        prepared["manifest_path"],
        p01,
        expected_task_id=manifest["task_id"],
        expected_request_fingerprint=prepared["request_fingerprint"],
        intake_context=prepared["intake_context"],
        selected_material_ids={body["material_id"]},
        read_configurations=p02.configurations(cache_path=cache, declared_versions=versions),
        reader_versions=versions,
        field_profile=profile,
        field_contract_ref={
            "version": "0.1.0",
            "ref": str(CONTRACT.resolve()),
            "sha256": hashlib.sha256(CONTRACT.read_bytes()).hexdigest(),
        },
        output_dir=tmp_path / "main",
        reader=p02.parse_prepared,
        worker_fn=worker,
        markdown_provider=provider,
        material_agent=material_agent,
    )
    handoff = read_validated_result(result["result_ref"], content_root=p01)["material_handoff"]
    fields = [x["field"] for x in handoff["candidates"] if x["field_name"] == "destination"]
    volume = next(x["field"] for x in handoff["candidates"] if x["field_name"] == "line_volume")
    assert len(calls) == 1 and len(fields) == 2
    assert all(x["status"] == "conflict" and x["value"] is None for x in fields)
    assert volume["status"] == "uncertain" and volume["value"] is None
    assert any(x["code"] == "CONFLICTING_VALUES" for x in handoff["issues"])


@pytest.mark.parametrize(
    ("line", "proposed_cbm", "carton", "cbm"),
    [
        ("CNF,24ctns, 245kgs, 1.875cbm", 1.875, "carton", 1.875),
        ("Cargo details: 7CTNS 185KG 0.93CBM", 0.93, "carton", 0.93),
        ("CNF,24ctns, 245kgs, 1.875cbm", 1875, "carton", None),
        ("CNF,24ctns, 245kgs, 1.875cbm per carton", 1.875, None, None),
        ("CNF,24ctns, 245kgs, 1.875cbm / 2 pallets", 1.875, None, None),
        ("CNF,24ctns, 245kgs, 1.875 cubic feet", 1.875, None, None),
    ],
)
def test_material_agent_accepts_only_proven_fields_from_a_compact_cargo_summary(
    tmp_path,
    line,
    proposed_cbm,
    carton,
    cbm,
):
    from local_parse_server import current_body_profile

    p01 = tmp_path / "p01"
    prepared = prepare(p01, line)
    manifest = prepared["received"]["manifest"]
    body = next(item for item in manifest["materials"] if item["kind"] == "body")
    profile = current_body_profile(manifest, body, "local-current-body-inquiry")

    def agent(request, *, timeout_seconds):
        evidence = next(item for item in request["evidence"] if item["text"] == line)
        candidates = {
            "package_type": "carton",
            "line_volume": proposed_cbm,
            "line_gross_weight": 245,
            "line_net_weight": 245,
            "trade_terms": "CNF",
        }
        return {
            "candidates": [
                {
                    "requirement_id": item["requirement_id"],
                    "value": candidates[item["field_name"]],
                    "evidence_ids": [evidence["evidence_id"]],
                }
                for item in request["requirements"]
                if item["field_name"] in candidates
            ]
        }

    result = execute(tmp_path / "run", prepared, profile, material_agent=agent)
    handoff = read_validated_result(result["result_ref"], content_root=p01)["material_handoff"]
    fields = {item["field_name"]: item["field"] for item in handoff["candidates"]}
    assert (fields["package_type"]["value"], fields["line_volume"]["value"]) == (carton, cbm)
    assert all(
        fields[name]["status"] == "uncertain" for name in ("line_gross_weight", "trade_terms")
    )
    for name, value in (("package_type", carton), ("line_volume", cbm)):
        if value is not None:
            assert fields[name]["status"] == "known" and fields[name]["source_ids"]


def test_comma_delimited_inquiry_form_proves_fields_without_promoting_generic_pieces(tmp_path):
    from local_parse_server import current_body_profile

    body = (
        "Incoterm, EXW\nOrigin airport,\nDestination airport, SFO San Francisco\nPieces nr , 19 bx\n"
        "Total gross weight, 680 kg\nTotal volume, 1,35 cbm\n"
        "On Monday wrote:\nDestination airport, LAX Los Angeles\nTotal gross weight, 17 kg"
    )
    prepared = prepare(tmp_path / "p01", body)
    manifest = prepared["received"]["manifest"]
    target = next(item for item in manifest["materials"] if item["kind"] == "body")
    result = execute(
        tmp_path / "run",
        prepared,
        current_body_profile(manifest, target, "local-current-body-inquiry"),
    )
    handoff = read_validated_result(result["result_ref"], content_root=tmp_path / "p01")[
        "material_handoff"
    ]
    fields = {item["field_name"]: item["field"] for item in handoff["candidates"]}
    for name, value, unit in (
        ("trade_terms", "EXW", None),
        ("destination", "SFO", None),
        ("line_gross_weight", 680, "kg"),
        ("line_volume", 1.35, "CBM"),
    ):
        assert (fields[name]["value"], fields[name].get("unit")) == (value, unit)
        assert fields[name]["source_ids"]
    assert all(
        fields[name]["status"] == "uncertain" for name in ("ctns", "origin", "package_count")
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Cargo origin: Suzhou\nPickup city: Shanghai\nOrigin airport: PVG\nDestination: LHR",
            ("Suzhou", "Shanghai", "PVG"),
        ),
        (
            "Pickup location: Main Road, XIAMEN CITY, FUJIAN, CHINA\nOrigin airport: CAN/SZX",
            (None, "XIAMEN", None),
        ),
        (
            "Origin: Unclassified\nCompany: Shanghai Example\nAddress: 1 Main Road, Shanghai City, China",
            (None, None, None),
        ),
        (
            "Pickup address:\nNo. 7, Hangzhou City, Zhejiang Province\nCargo origin: PVG Airport",
            (None, "Hangzhou", None),
        ),
        ("Pickup address:\nCompany address: 8 Main Road, Shanghai City, China", (None, None, None)),
        (
            "Port of loading: Shanghai Port\nOn Monday wrote:\nPickup city: Ningbo\nCargo origin: Huzhou",
            (None, None, "Shanghai Port"),
        ),
    ],
)
def test_typed_inquiry_places_are_independent_and_source_backed(tmp_path, text, expected):
    from local_parse_server import current_body_profile

    prepared = prepare(tmp_path / "p01", text)
    manifest = prepared["received"]["manifest"]
    body = next(item for item in manifest["materials"] if item["kind"] == "body")
    profile = current_body_profile(manifest, body, "local-current-body-inquiry-locations")
    result = execute(tmp_path / "run", prepared, profile)
    handoff = read_validated_result(result["result_ref"], content_root=tmp_path / "p01")[
        "material_handoff"
    ]
    fields = {item["field_name"]: item["field"] for item in handoff["candidates"]}
    assert "origin" not in fields
    for name, value in zip(
        ("cargo_origin", "pickup_city", "departure_port"), expected, strict=True
    ):
        assert fields[name]["value"] == value
        assert fields[name]["status"] == ("known" if value else "uncertain")
        assert bool(fields[name]["source_ids"]) == bool(value)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Total gross weight, 1725 kg\nTotal net weight: 1600 kg\nChargeable weight, 1800 kg\n"
            "Each carton gross weight: 18 kg\nNet weight per carton: 16 kg\nLine gross weight: 315 kg",
            (1600, 1725, 1800, 16, 18),
        ),
        (
            "1pkg / Gross Weight 315kg / 2.46cbm\nCargo details: 1PLT 315KG 2.46CBM CW:372KG",
            (None, 315, 372, None, None),
        ),
        (
            "Weight: 403 kg\nTotal Peso Volumen: 212.64 kg\n4 boxes - 39 kg each\n"
            "Line chargeable weight: 350 kg\nGross weight: 400 kg each\n"
            "On Monday wrote:\nTotal gross weight: 444 kg\nChargeable weight: 500 kg",
            (None, None, None, None, None),
        ),
    ],
)
def test_scoped_body_weights_do_not_mix_weight_kinds_or_quantity_scopes(tmp_path, text, expected):
    from local_parse_server import current_body_profile

    p01 = tmp_path / "p01"
    prepared = prepare(p01, text)
    manifest = prepared["received"]["manifest"]
    body = next(item for item in manifest["materials"] if item["kind"] == "body")
    result = execute(
        tmp_path / "run",
        prepared,
        current_body_profile(manifest, body, "local-current-body-inquiry-weights"),
    )
    handoff = read_validated_result(result["result_ref"], content_root=p01)["material_handoff"]
    fields = {c["field_name"]: c for c in handoff["candidates"]}
    assert {"line_gross_weight", "line_net_weight", "chargeable_weight"}.isdisjoint(fields)
    names = (
        "shipment_net_weight",
        "shipment_gross_weight",
        "shipment_chargeable_weight",
        "unit_net_weight",
        "unit_gross_weight",
    )
    for name, value in zip(names, expected, strict=True):
        field = fields[name]
        assert field["field"]["value"] == value
        assert field["quantity_scope"] == ("per_item" if name.startswith("unit_") else "document")
        assert field["field"].get("unit") == ("kg" if value is not None else None)


@pytest.mark.parametrize(
    "summary,expected",
    [
        ("1pkg / Gross Weight 315kg / 2.46cbm", (1, None, 315, 2.46)),
        ("7ctns / 90k / 0.8cbm", (7, 7, None, 0.8)),
        ("1pkg / Gross Weight 315kg / 2.46cbm per package", (None, None, None, None)),
    ],
)
def test_slash_cargo_summary_keeps_scope_and_weight_kind(tmp_path, summary, expected):
    from local_parse_server import current_body_profile

    prepared = prepare(tmp_path / "p01", summary)
    manifest = prepared["received"]["manifest"]
    target = next(item for item in manifest["materials"] if item["kind"] == "body")
    result = execute(
        tmp_path / "run",
        prepared,
        current_body_profile(manifest, target, "local-current-body-inquiry"),
    )
    handoff = read_validated_result(result["result_ref"], content_root=tmp_path / "p01")[
        "material_handoff"
    ]
    fields = {item["field_name"]: item["field"] for item in handoff["candidates"]}
    assert (
        tuple(
            fields[name]["value"]
            for name in ("package_count", "ctns", "line_gross_weight", "line_volume")
        )
        == expected
    )


@pytest.mark.parametrize(
    ("line", "name", "expected"),
    [
        ("Total volume: 1.37 CBM per carton", "line_volume", None),
        ("Per pallet: 6 cartons", "ctns", None),
        ("Total packages: 6 cartons/pallet", "package_count", None),
        ("Total packages: 6 cartons/pallet", "package_type", None),
        ("Total G.W.: 42 kg (per carton)", "line_gross_weight", None),
        ("Chargeable weight: 42 kg each", "chargeable_weight", None),
        ("总体积：1.37 CBM/箱", "line_volume", None),
        ("Total volume: 2.75 CBM", "line_volume", 2.75),
        ("6 cartons each weighing 5 kg", "ctns", 6),
        ("Total packages: 6 cartons", "package_count", 6),
    ],
)
def test_current_body_rollups_respect_explicit_quantity_scope(tmp_path, line, name, expected):
    from local_parse_server import current_body_profile

    p01 = tmp_path / "p01"
    prepared = prepare(p01, line)
    manifest = prepared["received"]["manifest"]
    body = next(item for item in manifest["materials"] if item["kind"] == "body")
    profile = current_body_profile(manifest, body, "local-current-body-inquiry")
    result = execute(tmp_path / "run", prepared, profile)
    snapshot = read_validated_result(result["result_ref"], content_root=p01)
    field = next(
        item["field"]
        for item in snapshot["material_handoff"]["candidates"]
        if item["field_name"] == name
    )
    assert (field["status"], field["value"]) == (
        ("uncertain", None) if expected is None else ("known", expected)
    )


@pytest.mark.parametrize(
    ("line", "proposals", "expected", "rejection"),
    [
        (
            "到LHR 3PLTS/735KGS/6.42CBM",
            {"destination": "LHR", "package_count": 3, "ctns": 3},
            {"destination": "LHR", "package_count": 3, "ctns": None},
            ("ctns", "field_scope_mismatch"),
        ),
        (
            "到LHR 3PLTS/735KGS/6.42CBM\n115*95*120\n115*95*135",
            {"destination": "LHR", "package_count": 3, "ctns": 3},
            {"destination": "LHR", "package_count": 3, "ctns": None},
            ("ctns", "field_scope_mismatch"),
        ),
        (
            "IF TERM FOB /CFR, insurance is needed.",
            {"trade_terms": "FOB"},
            {"trade_terms": None},
            ("trade_terms", "conditional_or_alternative"),
        ),
        (
            "到LHR 3PLTS/735KGS/6.42CBM 或到AMS",
            {"destination": "LHR", "package_count": 3},
            {"destination": None, "package_count": None},
            ("destination", "conditional_or_alternative"),
        ),
        (
            "到LHR 3PLTS/735KGS/6.42CBM per carton",
            {"package_count": 3},
            {"package_count": None},
            ("package_count", "field_scope_mismatch"),
        ),
        (
            "Destination: LHR",
            {"destination": "AMS"},
            {"destination": "LHR"},
            ("destination", "evidence_binding_failed"),
        ),
    ],
)
def test_agent_checks_route_and_package_evidence(tmp_path, line, proposals, expected, rejection):
    from local_parse_server import current_body_profile

    prepared = prepare(tmp_path / "p01", line)
    manifest = prepared["received"]["manifest"]
    body = next(r for r in manifest["materials"] if r["kind"] == "body")
    profile = current_body_profile(manifest, body, "local-current-body-inquiry")

    def agent(request, *, timeout_seconds):
        eid = next(e["evidence_id"] for e in request["evidence"] if e["text"] == line)
        return {
            "candidates": [
                {
                    "requirement_id": r["requirement_id"],
                    "value": proposals[r["field_name"]],
                    "evidence_ids": ["E999"]
                    if rejection[1] == "evidence_binding_failed"
                    else [eid],
                }
                for r in request["requirements"]
                if r["field_name"] in proposals
            ]
        }

    run = tmp_path / "run"
    result = execute(run, prepared, profile, material_agent=agent)
    handoff = read_validated_result(result["result_ref"], content_root=tmp_path / "p01")[
        "material_handoff"
    ]
    actual = {
        r["field_name"]: r["field"]["value"]
        for r in handoff["candidates"]
        if r["field_name"] in expected
    }
    assert actual == expected
    events = json.loads((run / "agent-events.json").read_text(encoding="utf-8"))["events"]
    rid = next(
        r["requirement_id"] for r in profile["requirements"] if r["field_name"] == rejection[0]
    )
    assert any(
        e.get("requirement_id") == rid
        and e["outcome"] == "rejected"
        and e.get("reason") == rejection[1]
        for e in events
    )


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("General Cargo", "general cargo"),
        ("Is this General Cargo?", None),
        ("Please see the details.\nOn Monday wrote:\nGeneral Cargo", None),
    ],
)
def test_current_body_cargo_type_needs_an_explicit_statement(tmp_path, statement, expected):
    from local_parse_server import current_body_profile

    p01 = tmp_path / "p01"
    prepared = prepare(p01, statement)
    manifest = prepared["received"]["manifest"]
    target = next(item for item in manifest["materials"] if item["kind"] == "body")
    result = execute(
        tmp_path / "run",
        prepared,
        current_body_profile(manifest, target, "local-current-body-inquiry"),
    )
    handoff = read_validated_result(result["result_ref"], content_root=p01)["material_handoff"]
    field = next(
        item["field"] for item in handoff["candidates"] if item["field_name"] == "cargo_type"
    )
    assert (field["value"], field["status"]) == (expected, "known" if expected else "uncertain")
    assert bool(field["source_ids"]) == bool(expected)


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("21 ctns - 325 KG - 1.75 CBM", "carton"),
        ("DATA:24CTNS/380KGS/3.750CBM V.W:620.0", "carton"),
        ("12ctns / 125k / 0.8cbm", "carton"),
        ("1 PALLET", "pallet"),
        ("1 Package:", None),
        ("10 PCS", None),
        ("4 cartons per pallet", None),
        ("4 cartons or 2 pallets", None),
        ("See previous quote.\nOn Monday wrote:\n21 ctns - 325 KG - 1.75 CBM", None),
    ],
)
def test_current_body_package_type_requires_a_specific_stated_kind(tmp_path, statement, expected):
    from local_parse_server import current_body_profile

    p01 = tmp_path / "p01"
    prepared = prepare(p01, statement)
    manifest = prepared["received"]["manifest"]
    target = next(item for item in manifest["materials"] if item["kind"] == "body")
    result = execute(
        tmp_path / "run",
        prepared,
        current_body_profile(manifest, target, "local-current-body-inquiry"),
    )
    handoff = read_validated_result(result["result_ref"], content_root=p01)["material_handoff"]
    field = next(
        item["field"] for item in handoff["candidates"] if item["field_name"] == "package_type"
    )
    assert (field["value"], field["status"]) == (expected, "known" if expected else "uncertain")
    assert bool(field["source_ids"]) == bool(expected)


def test_current_inquiry_binds_department_signature_and_prefers_explicit_mobile(tmp_path):
    from local_parse_server import current_body_profile

    prepared = prepare(
        tmp_path / "p01",
        "Ready :2099/12/05\nGENERAL CARGO - STACKABLE\nBest regards,\n"
        "Alex Doe\nAirfreight Pricing Department / Example Freight\n"
        "Mobile (001) 2025550100 Office (001) 2025550101 / 2025550102\nalex@example.test",
    )
    manifest = prepared["received"]["manifest"]
    body = next(r for r in manifest["materials"] if r["kind"] == "body")
    profile = current_body_profile(manifest, body, "local-current-body-inquiry-weights")
    result = execute(tmp_path / "run", prepared, profile)
    handoff = read_validated_result(result["result_ref"], content_root=tmp_path / "p01")[
        "material_handoff"
    ]
    fields = {c["field_name"]: c["field"]["value"] for c in handoff["candidates"]}
    assert fields["customer_company"] == "Example Freight"
    assert fields["contact_phone"] == "(001) 2025550100"
    assert fields["cargo_ready_time"] == "2099/12/05"
    assert fields["business_remarks"] == "GENERAL CARGO - STACKABLE"


def test_current_inquiry_extracts_signature_date_and_keeps_independent_constraints(tmp_path):
    from local_parse_server import current_body_profile

    body_text = (
        "Cargo ready 12/08\n"
        "货物已备好，收到付款后才能放货\n"
        "设备包含电池，不得按无电申报。\n"
        "如有电池请务必告知。\n"
        "Best regards,\nAlex Doe\nExample Logistics Ltd.\n"
        "Tel: +1 202 555 0100\nE-mail: alex@example.test"
    )
    prepared = prepare(tmp_path / "p01", body_text)
    manifest = prepared["received"]["manifest"]
    body = next(r for r in manifest["materials"] if r["kind"] == "body")
    profile = current_body_profile(manifest, body, "local-current-body-inquiry-weights")
    result = execute(tmp_path / "run", prepared, profile)
    handoff = read_validated_result(result["result_ref"], content_root=tmp_path / "p01")[
        "material_handoff"
    ]
    fields = {c["field_name"]: c["field"] for c in handoff["candidates"]}
    expected = {
        "customer_company": "Example Logistics Ltd.",
        "contact_name": "Alex Doe",
        "contact_phone": "+1 202 555 0100",
        "signature_email": "alex@example.test",
        "cargo_ready_time": "12/08",
        "business_remarks": "货物已备好，收到付款后才能放货\n设备包含电池，不得按无电申报。",
    }
    assert {k: fields[k]["value"] for k in expected} == expected
    assert all(fields[k]["source_ids"] for k in expected)


def test_pdf_position_proof_rejects_another_rows_agent_value(tmp_path):
    pdf, raw = PdfWriter(), BytesIO()
    pdf.add_blank_page(width=300, height=200)
    pdf.write(raw)
    mail = EmailMessage()
    mail.set_content("See packing list.")
    mail.add_attachment(
        raw.getvalue(), maintype="application", subtype="pdf", filename="packing.pdf"
    )
    p01 = tmp_path / "p01"
    prepared = run_local_handoff(
        mail.as_bytes(),
        p01,
        input_format="eml",
        request_reference="test:positioned",
        task_id="position-test",
        email_ref_id="position-email",
    )
    manifest = prepared["received"]["manifest"]
    (mid,) = [m["material_id"] for m in manifest["materials"] if m["kind"] == "attachment"]
    profile = cargo_row_profile(
        mid,
        manifest["task_id"],
        [
            {"selector": {"page": 1, "anchor": f"/texts/{i}/text", "row": row}}
            for i, row in ((3, 1), (5, 2))
        ],
    )
    versions = p02.configurations()[("docling", "pdf")]["versions"]
    cache = tmp_path / "cache"
    cache.mkdir()
    data = [
        ("Packing List", 10, 155, 80),
        ("Product Name", 10, 120, 80),
        ("Cartons", 120, 120, 35),
        ("Sample A", 10, 100, 50),
        ("2", 125, 100, 7),
        ("Sample B", 10, 80, 50),
        ("3", 125, 80, 7),
    ]

    def worker(request):
        blocks = [
            {
                "text": t,
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {
                            "l": x,
                            "r": x + w,
                            "t": y,
                            "b": y - 8,
                            "coord_origin": "BOTTOMLEFT",
                        },
                        "charspan": [0, len(t)],
                    }
                ],
            }
            for t, x, y, w in data
        ]
        artifact = json.dumps({"texts": blocks, "tables": [], "pages": {"1": {}}}).encode()
        path = Path(request["json_path"])
        path.write_bytes(artifact)
        return {
            "conversion_status": "success",
            "errors": [],
            "versions": versions,
            "json_path": str(path),
            "json_sha256": hashlib.sha256(artifact).hexdigest(),
            "document_pages": [1],
            "text_items": len(blocks),
            "table_items": 0,
        }

    def material_agent(request, *, timeout_seconds):
        by_text = {item["text"]: item["evidence_id"] for item in request["evidence"]}
        return {
            "candidates": [
                {
                    "requirement_id": "ROW-1-ctns",
                    "value": count,
                    "evidence_ids": [
                        by_text[t] for t in (str(count), "Cartons", name, "Packing List")
                    ],
                }
                for count, name in ((2, "Sample A"), (3, "Sample B"))
            ]
        }

    where = tmp_path / "main"
    result = run_main_channel(
        prepared["manifest_path"],
        p01,
        expected_task_id=manifest["task_id"],
        expected_request_fingerprint=prepared["request_fingerprint"],
        intake_context=prepared["intake_context"],
        selected_material_ids={mid},
        read_configurations=p02.configurations(cache_path=cache, declared_versions=versions),
        reader_versions=versions,
        field_profile=profile,
        field_contract_ref={
            "version": "0.1.0",
            "ref": str(CONTRACT.resolve()),
            "sha256": hashlib.sha256(CONTRACT.read_bytes()).hexdigest(),
        },
        output_dir=where,
        reader=p02.parse_prepared,
        worker_fn=worker,
        material_agent=material_agent,
    )
    handoff = read_validated_result(result["result_ref"], content_root=p01)["material_handoff"]
    first = profile["requirements"][0]["object_selector"]
    (field,) = [
        c["field"]
        for c in handoff["candidates"]
        if c["object_id"] == first and c["field_name"] == "ctns"
    ]
    assert (field["value"], field["status"]) == (2, "known")
    events = json.loads((where / "agent-events.json").read_bytes())["events"]
    assert any(
        e["outcome"] == "corroborated" and e["requirement_id"] == "ROW-1-ctns" for e in events
    )
    assert any(
        e.get("reason") == "evidence_not_supported" and e["requirement_id"] == "ROW-1-ctns"
        for e in events
    )


@pytest.mark.parametrize(
    ("text", "values"),
    [
        (
            "一票货物由我司安排空运，数据为：2wooden case, 500KG, 2.50CBM\n请问是否已经货好？",
            {"package_type": "wooden_case", "line_volume": 2.5},
        ),
        (
            "Cargo Details:\nGeneral Cargo\n8 Package:\nDims.: 40*45*55cm\nGW:71KG\n"
            "For this fictional shipment, destination handling charges are borne by the shipper.",
            {
                "shipment_gross_weight": 71,
                "business_remarks": "For this fictional shipment, destination handling charges are borne by the shipper.",
            },
        ),
        (
            "Cargo Details:\n1 Package:\nGW:64KG per carton\n"
            "-----Original Message-----\nDestination handling is paid by the consignee.",
            {},
        ),
        ("Cargo Details:\n1 Package:\nGW:64KG\nCargo Details:\n1 Package:\nGW:80KG", {}),
    ],
)
def test_current_inquiry_proves_cargo_summary_and_responsibility(tmp_path, text, values):
    from local_parse_server import current_body_profile

    prepared = prepare(tmp_path / "p01", text)
    manifest = prepared["received"]["manifest"]
    body = next(row for row in manifest["materials"] if row["kind"] == "body")
    result = execute(
        tmp_path / "run",
        prepared,
        current_body_profile(manifest, body, "local-current-body-inquiry-weights"),
    )
    handoff = read_validated_result(result["result_ref"], content_root=tmp_path / "p01")[
        "material_handoff"
    ]
    names = {"package_type", "line_volume", "shipment_gross_weight", "business_remarks"}
    facts = {
        c["field_name"]: c["field"]
        for c in handoff["candidates"]
        if c["field_name"] in names and c["field"]["status"] == "known"
    }
    assert {name: field["value"] for name, field in facts.items()} == values
    if "shipment_gross_weight" in facts:
        weight = facts["shipment_gross_weight"]
        sources = {source["source_id"]: source["excerpt"] for source in handoff["sources"]}
        assert weight["unit"] == "kg"
        assert [sources[sid] for sid in weight["source_ids"]] == ["GW:71KG", "Cargo Details:"]


def test_current_generic_package_count_does_not_invent_a_package_type(tmp_path):
    from local_parse_server import WEIGHT_INQUIRY_PROFILE, current_body_profile

    body = "Cargo Details:\nGeneral Cargo\n8 Package:\nDims.: 40*45*55cm\nGW:71KG"
    prepared = prepare(tmp_path / "p01", body)
    manifest = prepared["received"]["manifest"]
    target = next(item for item in manifest["materials"] if item["kind"] == "body")
    result = execute(
        tmp_path / "run", prepared, current_body_profile(manifest, target, WEIGHT_INQUIRY_PROFILE)
    )
    handoff = read_validated_result(result["result_ref"], content_root=tmp_path / "p01")[
        "material_handoff"
    ]
    fields = {item["field_name"]: item["field"] for item in handoff["candidates"]}
    assert (fields["package_count"]["status"], fields["package_count"]["value"]) == ("known", 8)
    assert fields["package_type"]["status"] == "uncertain"
    assert fields["ctns"]["status"] == "uncertain"
    sources = {item["source_id"]: item["excerpt"] for item in handoff["sources"]}
    assert {sources[sid] for sid in fields["package_count"]["source_ids"]} == {
        "Cargo Details:",
        "8 Package:",
    }


@pytest.mark.parametrize(
    ("middle", "quoted"),
    [("------------------ Original ------------------", True), ("Original cargo details", False)],
)
def test_original_divider_keeps_quoted_cargo_out_of_current_fields(tmp_path, middle, quoted):
    from local_parse_server import WEIGHT_INQUIRY_PROFILE, current_body_profile

    parts = ["Trade terms: EXW.", middle, "一票货物，数据为：5wooden case, 725KG, 2.75CBM"]
    message = EmailMessage()
    message.set_content("See HTML.")
    message.add_alternative("".join(f"<p>{part}</p>" for part in parts), subtype="html")
    p01 = tmp_path / "p01"
    prepared = run_local_handoff(
        message.as_bytes(),
        p01,
        input_format="eml",
        request_reference="test:original-divider",
        task_id="original-divider",
        email_ref_id="body-email",
    )
    manifest = prepared["received"]["manifest"]
    target = next(
        item
        for item in manifest["materials"]
        if item["kind"] == "body" and item["state"] == "ready"
    )
    profile = current_body_profile(manifest, target, WEIGHT_INQUIRY_PROFILE)
    versions = p02.configurations()[("docling", "html")]["versions"]
    cache = tmp_path / "cache"
    cache.mkdir()

    def worker(request):
        raw = json.dumps({"texts": [{"text": part} for part in parts], "tables": []}).encode()
        path = Path(request["json_path"])
        path.write_bytes(raw)
        return {
            "conversion_status": "success",
            "errors": [],
            "versions": versions,
            "json_path": str(path),
            "json_sha256": hashlib.sha256(raw).hexdigest(),
        }

    contract = {
        "version": "0.1.0",
        "ref": str(CONTRACT.resolve()),
        "sha256": hashlib.sha256(CONTRACT.read_bytes()).hexdigest(),
    }
    result = run_main_channel(
        prepared["manifest_path"],
        p01,
        expected_task_id=manifest["task_id"],
        expected_request_fingerprint=prepared["request_fingerprint"],
        intake_context=prepared["intake_context"],
        selected_material_ids={target["material_id"]},
        read_configurations=p02.configurations(cache_path=cache, declared_versions=versions),
        reader_versions=versions,
        field_profile=profile,
        field_contract_ref=contract,
        output_dir=tmp_path / "main",
        reader=p02.parse_prepared,
        worker_fn=worker,
    )
    handoff = read_validated_result(result["result_ref"], content_root=p01)["material_handoff"]
    fields = {item["field_name"]: item["field"] for item in handoff["candidates"]}
    assert fields["trade_terms"]["value"] == "EXW"
    assert fields["package_type"]["value"] == (None if quoted else "wooden_case")
    assert fields["line_volume"]["value"] == (None if quoted else 2.75)
