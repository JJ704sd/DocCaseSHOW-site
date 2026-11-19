"""Bounded Excel evidence: formula cache, provenance, and controlled handoff."""

import base64
import copy
import hashlib
import importlib.util
import json
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree

import pytest
from openpyxl import Workbook
from test_inputs import email, request

from doccase_preprocessing.local_handoff import (
    receive_handoff,
    run_local_file_handoff,
    run_local_handoff,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
spec = importlib.util.spec_from_file_location(
    "p02_excel_supplement", EXAMPLES / "p02_excel_supplement.py"
)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


def workbook_bytes():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "CI A"
    sheet["A1"] = "=1-1"
    sheet["B1"] = "=2+2"
    sheet["C1"] = 0
    sheet["D1"] = datetime(2026, 1, 2, 3, 4, 5)
    sheet["E1"] = "outside"
    sheet["A2"] = "marker"
    sheet.merge_cells("A2:B2")
    sheet["C3"] = 3
    sheet.column_dimensions["C"].hidden = True
    sheet.row_dimensions[3].hidden = True
    workbook.create_sheet("Other").sheet_state = "hidden"
    original = BytesIO()
    workbook.save(original)
    workbook.close()

    # openpyxl does not calculate formulas. Add a stored zero as fixture evidence.
    updated = BytesIO()
    with (
        zipfile.ZipFile(BytesIO(original.getvalue())) as source,
        zipfile.ZipFile(updated, "w") as out,
    ):
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "xl/worksheets/sheet1.xml":
                root = ElementTree.fromstring(data)
                ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                root.find(".//x:c[@r='A1']/x:v", ns).text = "0"
                data = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
            out.writestr(info, data)
    return updated.getvalue()


def scope():
    return {
        "sheet_count": 2,
        "sheets": [
            {
                "name": "CI A",
                "state": "visible",
                "hidden_rows": [3],
                "hidden_columns": [{"min": 3, "max": 3}],
                "merged_ranges": ["A2:B2"],
            },
            {
                "name": "Other",
                "state": "hidden",
                "hidden_rows": [],
                "hidden_columns": [],
                "merged_ranges": [],
            },
        ],
    }


def table_cell(row, col, text, row_end=None, col_end=None):
    return {
        "start_row_offset_idx": row,
        "end_row_offset_idx": row_end or row + 1,
        "start_col_offset_idx": col,
        "end_col_offset_idx": col_end or col + 1,
        "text": text,
    }


def document():
    return {
        "groups": [{"self_ref": "#/groups/0", "name": "CI A", "label": "sheet"}],
        "tables": [
            {
                "self_ref": "#/tables/0",
                "parent": {"$ref": "#/groups/0"},
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {
                            "l": 0.0,
                            "t": 0.0,
                            "r": 4.0,
                            "b": 3.0,
                            "coord_origin": "TOPLEFT",
                        },
                    }
                ],
                "data": {
                    "num_rows": 3,
                    "num_cols": 4,
                    "table_cells": [
                        table_cell(0, 0, "0"),
                        table_cell(0, 1, ""),
                        table_cell(0, 2, "wrong"),
                        table_cell(0, 3, ""),
                        table_cell(1, 0, "marker", col_end=2),
                        table_cell(1, 2, "unexpected"),
                        table_cell(2, 0, ""),
                    ],
                },
            }
        ],
    }


def test_two_read_only_views_keep_zero_unknown_and_declared_metadata():
    cells = app.read_cells(workbook_bytes(), scope(), bounds=(1, 1, 5, 3))
    indexed = {cell["coordinate"]: cell for cell in cells}
    assert len(cells) == 15
    assert indexed["A1"]["formula_view"]["value"] == "=1-1"
    assert indexed["A1"]["data_only_view"]["value"] == 0
    assert indexed["A1"]["formula_cache"] == {"state": "available", "freshness": "unknown"}
    assert indexed["B1"]["formula_view"]["value"] == "=2+2"
    assert indexed["B1"]["data_only_view"]["value"] is None
    assert indexed["B1"]["formula_cache"]["state"] == "unavailable"
    assert indexed["C1"]["formula_cache"]["state"] == "not_formula"
    assert indexed["D1"]["formula_view"]["value"]["python_type"] == "datetime"
    assert indexed["D1"]["formula_view"]["number_format"]
    assert indexed["A2"]["structural_flags"]["merged_membership"] == [
        {"range": "A2:B2", "role": "anchor"}
    ]
    assert indexed["B2"]["structural_flags"]["merged_membership"][0]["role"] == "member"
    assert indexed["C1"]["structural_flags"]["column_hidden"]
    assert indexed["A3"]["structural_flags"]["row_hidden"]
    assert "P01 manifest" in indexed["A3"]["structural_flags"]["basis"]


def test_table_comparison_keeps_matches_differences_missing_and_span():
    cells = app.read_cells(workbook_bytes(), scope(), bounds=(1, 1, 5, 3))
    mapped = app.map_tables(document(), cells)
    statuses = {item["coordinate"]: item["status"] for item in mapped["table_cells"]}
    assert statuses == {
        "A1": "match",
        "B1": "formula_cache_unavailable",
        "C1": "difference",
        "D1": "missing_docling_value",
        "A2": "match",
        "C2": "missing_native_value",
        "A3": "match_empty",
    }
    indexed = {cell["coordinate"]: cell["docling_table_comparison"] for cell in cells}
    assert indexed["B2"]["state"] == "covered_by_span_no_anchor"
    assert indexed["C3"]["state"] == "missing_table_cell"
    assert indexed["E1"]["state"] == "outside_docling_table_regions"
    assert mapped["tables"][0]["a1_range"] == "A1:D3"
    assert mapped["table_cells"][4]["span_a1"] == "A2:B2"


def test_nonzero_origin_and_invalid_provenance_do_not_invent_coordinates():
    cells = app.read_cells(workbook_bytes(), scope(), bounds=(1, 1, 5, 3))
    source = document()
    table = source["tables"][0]
    table["prov"][0]["bbox"].update(l=1, t=1, r=3, b=3)
    table["data"] = {"num_rows": 2, "num_cols": 2, "table_cells": [table_cell(1, 1, "3")]}
    mapped = app.map_tables(source, cells)
    assert mapped["tables"][0]["a1_range"] == "B2:C3"
    assert mapped["table_cells"][0]["coordinate"] == "C3"
    assert mapped["table_cells"][0]["status"] == "match"
    table["prov"][0]["bbox"]["coord_origin"] = "BOTTOMLEFT"
    invalid = app.map_tables(source, cells)
    assert invalid["tables"][0]["mapping_state"] == "unmapped"
    assert invalid["table_cells"][0]["coordinate"] is None
    assert invalid["table_cells"][0]["status"] == "unmapped_provenance"
    assert all(
        cell["docling_table_comparison"]["state"] == "unresolved_docling_mapping" for cell in cells
    )


def test_invalid_cell_offset_keeps_its_table_region_unresolved():
    cells = app.read_cells(workbook_bytes(), scope(), bounds=(1, 1, 5, 3))
    source = document()
    source["tables"][0]["data"]["table_cells"][0]["end_col_offset_idx"] = 99
    mapped = app.map_tables(source, cells)
    indexed = {cell["coordinate"]: cell["docling_table_comparison"] for cell in cells}
    assert mapped["tables"][0]["mapping_state"] == "partial"
    assert mapped["table_cells"][0]["status"] == "unmapped_invalid_cell"
    assert mapped["table_cells"][0]["coordinate"] is None
    assert indexed["A1"]["state"] == "unresolved_docling_mapping"
    assert indexed["E1"]["state"] == "outside_docling_table_regions"


def test_p01_unbound_does_not_start_and_tampered_source_is_rejected(tmp_path, monkeypatch):
    payload = request(
        email=email(
            attachments=[
                {
                    "material_id": "XLSX",
                    "filename": "fixture.xlsx",
                    "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "content_base64": base64.b64encode(workbook_bytes()).decode(),
                }
            ]
        )
    )
    root = tmp_path / "p01"
    prepared = run_local_handoff(
        json.dumps(payload).encode(), root, input_format="json", request_reference="test:intake"
    )
    manifest = prepared["received"]["manifest"]
    task = next(t for t in manifest["parse_tasks"] if "XLSX" in t["material_ids"])
    monkeypatch.setattr(app.local_read, "worker", lambda _: pytest.fail("reader was started"))
    unbound = app.local_read.parse_prepared(
        prepared["manifest_path"],
        root,
        expected_task_id=manifest["task_id"],
        expected_request_fingerprint=None,
        intake_context=None,
        selected_material_ids={"XLSX"},
        configurations={},
        versions={},
        output_dir=tmp_path / "unused",
    )
    execution = app.local_read.only_execution(unbound)
    assert execution["execution_state"] == "not_started"
    assert execution["reason"] == "execution_configuration_missing"
    assert task["parser"]["version"] == "unbound" and task["execution_state"] == "not_started"
    assert not unbound["intake_bound"]
    assert app.bind_supplement(task, None, {}, {}) == "execution_configuration_missing"
    (root / task["content"]["path"]).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="SHA-256"):
        receive_handoff(
            prepared["manifest_path"],
            root,
            expected_task_id=manifest["task_id"],
            expected_request_fingerprint=None,
            intake_context=None,
        )


def test_supplement_identity_binds_scope_prior_artifact_and_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(app.local_read, "MODEL_CACHE", tmp_path)
    profile = app.local_read.configurations()[("docling", "xlsx")]
    versions = profile["versions"]
    task = {
        "route": "docling",
        "format": "xlsx",
        "parser": {"version": "unbound"},
        "parse_task_id": "plan",
        "content_sha256": "source",
    }
    prior = {
        "configured_profile": profile,
        "execution_key": app.local_read.execution_key(task, profile),
        "result": {"json_sha256": "prior-artifact"},
    }
    config = app.supplement_config(versions, prior)
    assert app.bind_supplement(task, config, versions, prior) is None
    key = app.local_read.execution_key(task, config)
    changed = copy.deepcopy(config)
    changed["requested_scope"]["range"] = "A1:A1"
    assert (
        app.bind_supplement(task, changed, versions, prior) == "execution_configuration_unsupported"
    )
    assert app.local_read.execution_key(task, changed) != key
    changed = copy.deepcopy(config)
    changed["prior_docling_artifact_sha256"] = "different"
    assert app.local_read.execution_key(task, changed) != key
    mismatched_versions = {**versions, "openpyxl": "wrong"}
    assert (
        app.bind_supplement(task, config, mismatched_versions, prior) == "reader_version_mismatch"
    )


def test_excel_weight_table_binds_the_observed_worksheet_and_unmerged_row(tmp_path):
    import p02_local_read as p02
    from local_parse_server import FIELD_CONTRACT, attachment_weight_profile

    from doccase_preprocessing.main_channel import read_validated_result, run_main_channel

    rows = (
        ("SKU", "Line Net Weight (kg)", "Unit Gross Weight (kg)"),
        ("A", "13", "7"),
        ("B", "26", "14"),
    )
    book = Workbook()
    sheet = book.active
    sheet.title = "Packing"
    for row in rows:
        sheet.append(row)
    book.create_sheet("Other")
    workbook = BytesIO()
    book.save(workbook)
    book.close()
    p01 = tmp_path / "p01"
    prepared = run_local_file_handoff(
        workbook.getvalue(),
        p01,
        filename="packing.xlsx",
        task_id="weights",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    manifest = prepared["received"]["manifest"]
    target = manifest["materials"][0]
    versions = p02.configurations()[("docling", "xlsx")]["versions"]

    def run(name, selected, *, merged=False, table_sheet="Packing"):
        def worker(request):
            cells = [
                table_cell(r, c, value, row_end=r + 2 if merged and (r, c) == (1, 2) else None)
                for r, row in enumerate(rows)
                for c, value in enumerate(row)
            ]
            raw = json.dumps({"texts": [], "tables": [{"data": {"table_cells": cells}}]}).encode()
            path = Path(request["json_path"])
            path.write_bytes(raw)
            return {
                "conversion_status": "success",
                "errors": [],
                "versions": versions,
                "json_path": str(path),
                "json_sha256": hashlib.sha256(raw).hexdigest(),
                "excel_evidence": {
                    "selected_sheet": "Packing",
                    "processed_sheet_names": ["Packing"],
                    "docling_table_ranges": [
                        {"table_ref": "#/tables/0", "sheet_name": table_sheet, "range": "A1:C3"}
                    ],
                },
            }

        result = run_main_channel(
            prepared["manifest_path"],
            p01,
            expected_task_id=manifest["task_id"],
            expected_request_fingerprint=prepared["request_fingerprint"],
            intake_context=prepared["intake_context"],
            selected_material_ids={target["material_id"]},
            read_configurations=p02.configurations(
                cache_path=tmp_path, declared_versions=versions, sheet_name="Packing"
            ),
            reader_versions=versions,
            field_profile=attachment_weight_profile(
                manifest,
                target,
                {
                    "worksheet": selected,
                    "table": "/tables/0",
                    "row": 3,
                    "row_label": "B",
                },
            ),
            field_contract_ref={
                "version": "0.1.0",
                "ref": str(FIELD_CONTRACT),
                "sha256": hashlib.sha256(FIELD_CONTRACT.read_bytes()).hexdigest(),
            },
            output_dir=tmp_path / name,
            reader=p02.parse_prepared,
            worker_fn=worker,
        )
        snapshot = read_validated_result(result["result_ref"], content_root=p01)
        return {
            item["field_name"]: item["field"]["value"]
            for item in snapshot["material_handoff"]["candidates"]
        }

    valid = run("correct", "Packing")
    assert (valid["line_net_weight"], valid["unit_gross_weight"]) == (26, 14)
    assert all(value is None for value in run("other-sheet", "Other").values())
    assert all(
        value is None for value in run("unbound-table", "Packing", table_sheet="Other").values()
    )
    merged = run("merged-row", "Packing", merged=True)
    assert (merged["line_net_weight"], merged["unit_gross_weight"]) == (26, None)
