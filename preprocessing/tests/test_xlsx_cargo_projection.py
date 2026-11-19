"""A clipped workbook preview must not hide independently verified cargo cells."""

import hashlib
import json
from copy import deepcopy

from local_web_pipeline import pipeline_view


def packing_fixture(tmp_path):
    header = [
        "Item No.",
        "Item Name",
        "CARTONS",
        "Qty (PC)",
        "NET WEIGHT(KGS)",
        "GROSS WEIGHT(KGS)",
    ]
    values = [
        header,
        ["SKU-EXAMPLE", "Device", "16", "1600", "196", "224"],
        ["SKU-EXAMPLE", "Device", "24", "2400", "147", "168"],
        ["TOTAL:", "", "40", "4000", "343", "392"],
    ]
    table_cells = []
    rows = []
    for row_number, row in zip((1, 3, 4, 5), values, strict=True):
        cells = {}
        for col, value in enumerate(row):
            if not value:
                continue
            cells[chr(65 + col)] = {"value": value, "clipped": False}
            table_cells.append(
                {
                    "text": value,
                    "start_row_offset_idx": row_number - 1,
                    "end_row_offset_idx": row_number + (1 if row_number == 1 else 0),
                    "start_col_offset_idx": col,
                    "end_col_offset_idx": col + 1,
                }
            )
        rows.append({"number": row_number, "cells": cells})
    document = {
        "groups": [{"self_ref": "#/groups/0", "label": "sheet", "name": "Packing list"}],
        "tables": [
            {
                "parent": {"$ref": "#/groups/0"},
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {"l": 0, "t": 0, "r": 6, "b": 5, "coord_origin": "TOPLEFT"},
                    }
                ],
                "data": {"num_rows": 5, "num_cols": 6, "table_cells": table_cells},
            }
        ],
    }
    raw = json.dumps(document).encode()
    path = tmp_path / "read.json"
    path.write_bytes(raw)
    result_path = tmp_path / "result.json"
    result_path.write_text("{}", encoding="utf8")
    result_ref = {"ref": str(result_path), "sha256": hashlib.sha256(b"{}").hexdigest()}
    (tmp_path / "progress.json").write_text(json.dumps({"result_ref": result_ref}), encoding="utf8")
    execution = {
        "execution_id": "read-1",
        "ref": str(path),
        "artifact_hash": hashlib.sha256(raw).hexdigest(),
        "executed_profile": {
            "reader": "DoclingExcel",
            "requested_scope": {"sheet_names": ["Packing list"]},
        },
        "result": {
            "excel_evidence": {
                "selected_sheet": "Packing list",
                "processed_sheet_names": ["Packing list"],
                "docling_table_ranges": [{"sheet_name": "Packing list", "table_ref": "#/tables/0"}],
            }
        },
    }
    read = {
        "inventory": [{"material_id": "m1", "execution_id": "read-1", "read_state": "success"}],
        "executions": [execution],
    }
    record = {
        "material_id": "m1",
        "filename": "packing.xlsx",
        "kind": "attachment",
        "format": "xlsx",
        "state": "ready",
        "content": {"sha256": "0" * 64},
    }
    sheet = {
        "name": "Packing list",
        "clipped": True,
        "columns": list("ABCDEF"),
        "rows": rows,
        "merges": [{"row": 1, "row_end": 2, "col": col, "col_end": col} for col in range(1, 7)],
    }
    return result_ref, read, record, sheet


def test_clipped_preview_delivers_two_source_bound_rows_and_not_the_total(tmp_path):
    result_ref, read, record, sheet = packing_fixture(tmp_path)
    view = pipeline_view(
        {"result_ref": result_ref},
        read,
        {"status": "complete", "profile_id": "test", "fields": []},
        record,
        "deepseek-group",
        tmp_path,
        xlsx_sheet=sheet,
    )
    rows = view["structured"]["packing_rows"]
    assert [r["row"] for r in rows] == [3, 4]
    assert [
        (
            r["fields"]["ctns"]["value"],
            r["fields"]["item_quantity"]["value"],
            r["fields"]["line_net_weight"]["value"],
            r["fields"]["line_gross_weight"]["value"],
        )
        for r in rows
    ] == [(16, 1600, 196, 224), (24, 2400, 147, 168)]
    assert {(s["sheet"], s["cell"]) for s in rows[0]["fields"]["line_gross_weight"]["sources"]} >= {
        ("Packing list", "A3"),
        ("Packing list", "F1"),
        ("Packing list", "F3"),
    }
    assert all(r["fields"]["item_quantity"]["unit"] == "piece" for r in rows)


def test_unavailable_target_cell_is_not_delivered_as_a_known_field(tmp_path):
    result_ref, read, record, sheet = packing_fixture(tmp_path)
    altered = deepcopy(sheet)
    del altered["rows"][1]["cells"]["F"]
    view = pipeline_view(
        {"result_ref": result_ref},
        read,
        {"status": "complete", "profile_id": "test", "fields": []},
        record,
        "deepseek-group",
        tmp_path,
        xlsx_sheet=altered,
    )
    first, second = view["structured"]["packing_rows"]
    assert first["fields"]["line_gross_weight"]["value"] is None
    assert first["fields"]["line_gross_weight"]["status"] == "uncertain"
    assert second["fields"]["line_gross_weight"]["value"] == 168


def test_missing_weight_header_and_shared_merged_weight_do_not_fill_rows(tmp_path):
    result_ref, read, record, sheet = packing_fixture(tmp_path)
    execution = read["executions"][0]
    path = tmp_path / "read.json"
    document = json.loads(path.read_bytes())
    cells = document["tables"][0]["data"]["table_cells"]
    next(c for c in cells if c["text"] == "NET WEIGHT(KGS)")["text"] = "WEIGHT"
    sheet["rows"][0]["cells"]["E"]["value"] = "WEIGHT"
    gross = next(c for c in cells if c["text"] == "224")
    gross["end_row_offset_idx"] = 4
    cells.remove(next(c for c in cells if c["text"] == "168"))
    del sheet["rows"][2]["cells"]["F"]
    sheet["merges"].append({"row": 3, "row_end": 4, "col": 6, "col_end": 6})
    raw = json.dumps(document).encode()
    path.write_bytes(raw)
    execution["artifact_hash"] = hashlib.sha256(raw).hexdigest()
    view = pipeline_view(
        {"result_ref": result_ref},
        read,
        {"status": "complete", "profile_id": "test", "fields": []},
        record,
        "deepseek-group",
        tmp_path,
        xlsx_sheet=sheet,
    )
    first, second = view["structured"]["packing_rows"]
    for row in (first, second):
        assert row["fields"]["line_net_weight"]["status"] == "uncertain"
        assert row["fields"]["line_gross_weight"]["status"] == "uncertain"
        assert row["fields"]["line_gross_weight"]["value"] is None
