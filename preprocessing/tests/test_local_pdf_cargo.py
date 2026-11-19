"""Local opt-in cargo fields retain their own PDF evidence and package scope."""

import hashlib
import json
from pathlib import Path

import pytest
from test_local_email_index import _indexed_request, _request, p02

from doccase_preprocessing.main_channel import (
    _facts,
    _package_expression,
    _pdf_cargo_field,
    _pdf_document_total_candidates,
    _pdf_table_field,
    read_validated_result,
)

pytest_plugins = ["test_local_email_index"]


def block(text, left, top, right, bottom, index):
    return {
        "pointer": f"/texts/{index}/text",
        "text": text,
        "provenance": [
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


def row_blocks():
    rows = [
        ("HS Code", 15, 95, 35, 90),
        ("Description", 60, 95, 100, 90),
        ("Total", 125, 100, 145, 96),
        ("Qty(pcs)", 125, 92, 145, 88),
        ("Qty/", 160, 100, 180, 96),
        ("CTN", 160, 92, 180, 88),
        ("Total", 200, 100, 220, 96),
        ("CTN", 200, 92, 220, 88),
        ("Measurement", 245, 100, 295, 96),
        ("(cm)/Pallets", 245, 92, 295, 88),
        ("#A", 2, 65, 12, 60),
        ("1234", 16, 65, 35, 60),
        ("Blue", 58, 65, 72, 60),
        ("lamp", 74, 65, 90, 60),
        ("12", 130, 65, 140, 60),
        ("4", 165, 65, 175, 60),
        ("3", 205, 65, 215, 60),
        ("100*80*60", 250, 65, 290, 60),
        ("#B", 2, 45, 12, 40),
        ("5678", 16, 45, 35, 40),
        ("Red", 58, 45, 70, 40),
        ("table", 72, 45, 90, 40),
        ("1", 205, 45, 215, 40),
    ]
    return [block(*item, index) for index, item in enumerate(rows)]


def test_row_name_count_and_pallet_measurement_stay_in_selected_columns():
    blocks = row_blocks()

    def result(name, row="#A"):
        return _pdf_cargo_field(blocks, {"field_name": name, "pdf_row_anchor": row})

    assert result("product_name")[0] == "Blue lamp"
    assert result("product_name", "#B")[0] == "Red table"
    assert result("package_count")[0] == 3
    assert result("package_type")[0] == "carton"
    assert result("raw_observations")[0] == "100*80*60"
    assert result("length_cm") is None
    assert {item["text"] for item in result("product_name")[3]}.isdisjoint({"1234", "12", "Red"})


def test_cross_column_or_duplicate_anchor_does_not_guess_name():
    blocks = row_blocks()
    blocks[12] = block("#A 1234 Blue lamp", 2, 65, 90, 60, 12)
    blocks.pop(10)
    assert _pdf_cargo_field(blocks, {"field_name": "product_name", "pdf_row_anchor": "#A"}) is None
    blocks = row_blocks()
    blocks.append(block("#A", 2, 25, 12, 20, 90))
    assert _pdf_cargo_field(blocks, {"field_name": "package_count", "pdf_row_anchor": "#A"}) is None


def test_emailbridge_pallet_wording_is_not_a_carton():
    assert _package_expression("Bulto: 1 pallet.") == (1, "pallet")
    assert list(_facts("ctns", "Bulto: 1 pallet.")) == []
    assert _package_expression("1 package") == (1, None)


def test_document_wooden_case_total_has_own_column_evidence():
    blocks = [
        block("WOODEN CASE", 300, 100, 370, 90, 0),
        block("TOTAL:", 100, 35, 160, 30, 1),
        block("1", 330, 35, 340, 30, 2),
        block("2", 400, 35, 410, 30, 3),
    ]
    count = _pdf_cargo_field(blocks, {"field_name": "package_count"})
    kind = _pdf_cargo_field(blocks, {"field_name": "package_type"})
    assert count[0] == 1 and kind[0] == "wooden_case"
    assert {item["text"] for item in count[3]} == {"WOODEN CASE", "TOTAL:", "1"}
    assert _pdf_cargo_field(blocks, {"field_name": "ctns"}) is None


def test_pdf_total_volume_recovers_merged_header_from_page_positions():
    blocks = [
        block("CBM Total", 60, 100, 95, 88, 0),
        block("CBM", 78, 90, 94, 82, 1),
        block("0.40", 78, 70, 94, 65, 2),
        block("TOTAL:", 10, 35, 30, 30, 3),
        block("6.85", 78, 35, 94, 30, 4),
    ]
    ((value, unit, evidence),) = list(_pdf_document_total_candidates(blocks, "line_volume"))
    assert (value, unit) == (6.85, "CBM")
    assert {item["text"] for item in evidence} == {"CBM Total", "CBM", "TOTAL:", "6.85"}


def test_pdf_labeled_cargo_note_keeps_weight_and_volume_together():
    text = "COMM: GENERAL - GROSS WEIGHT - 3000KGS - 28CBM - 20GP"
    blocks = [block(text, 10, 50, 300, 40, 0)]
    for name, expected, unit in (("line_gross_weight", 3000, "kg"), ("line_volume", 28, "CBM")):
        ((value, actual_unit, evidence),) = list(_pdf_document_total_candidates(blocks, name))
        assert (value, actual_unit) == (expected, unit)
        assert evidence[0]["text"] == text
    assert (
        list(
            _pdf_document_total_candidates(
                [block(text + " per carton", 10, 50, 300, 40, 0)], "line_volume"
            )
        )
        == []
    )


def test_mixed_total_does_not_round_fractional_carton_count():
    text = "Total: 2 pallets, 1.5 cartons, 4.07 CBM"
    assert list(_pdf_document_total_candidates([block(text, 10, 50, 300, 40, 0)], "ctns")) == []


def test_total_marker_merged_with_item_quantity_keeps_other_columns_separate():
    entries = [
        (0, 0, 1, "Item No."),
        (0, 1, 2, "Description"),
        (0, 2, 3, "QUANTIT Y (pcs)"),
        (0, 3, 4, "CTN"),
        (0, 4, 5, "G.W. (KGS)"),
        (0, 5, 6, "N.W. (KGS)"),
        (0, 6, 7, "Measurement"),
        (0, 7, 8, "CBM CTN"),
        (0, 8, 9, "Total CBM"),
        (5, 0, 3, "TOTAL: 12"),
        (5, 3, 4, "17"),
        (5, 4, 5, "300.00"),
        (5, 5, 6, "246.00"),
        (5, 6, 9, "1.70"),
    ]
    cells = [
        {
            "pointer": f"/tables/0/data/table_cells/{i}/text",
            "text": value,
            "cell": {
                "start_row_offset_idx": row,
                "end_row_offset_idx": row + 1,
                "start_col_offset_idx": col,
                "end_col_offset_idx": end,
                "column_header": row == 0,
            },
        }
        for i, (row, col, end, value) in enumerate(entries)
    ]
    for field, value, unit in (
        ("ctns", 17, None),
        ("line_net_weight", 246, "kg"),
        ("line_gross_weight", 300, "kg"),
    ):
        ((actual, actual_unit, evidence),) = _pdf_table_field(cells, field)
        assert (actual, actual_unit) == (value, unit)
        assert {"TOTAL: 12", "QUANTIT Y (pcs)"} <= {item["text"] for item in evidence}
    assert _pdf_table_field(cells, "line_volume") == []
    cells[2] = {**cells[2], "text": "Model"}
    assert _pdf_table_field(cells, "line_gross_weight") == []


def test_total_columns_distinguish_material_weight_and_per_carton_volume():
    entries = [
        (0, 0, "Description"),
        (0, 1, "Net Weight (KG)"),
        (0, 2, "Net weight of Plastic(Kg)"),
        (0, 3, "CBM /CT"),
        (0, 4, "T/CBM"),
        (1, 0, "Total:"),
        (1, 1, "200"),
        (1, 2, "10"),
        (1, 3, "0.20"),
        (1, 4, "2.30"),
    ]
    cells = [
        {
            "pointer": f"/tables/0/data/table_cells/{i}/text",
            "text": text,
            "cell": {
                "start_row_offset_idx": row,
                "end_row_offset_idx": row + 1,
                "start_col_offset_idx": col,
                "end_col_offset_idx": col + 1,
                "column_header": row == 0,
                "row_header": False,
            },
        }
        for i, (row, col, text) in enumerate(entries)
    ]
    ((net, unit, net_evidence),) = _pdf_table_field(cells, "line_net_weight")
    ((volume, cbm, volume_evidence),) = _pdf_table_field(cells, "line_volume")
    assert (net, unit, volume, cbm) == (200, "kg", 2.3, "CBM")
    assert {item["text"] for item in net_evidence} == {"200", "Net Weight (KG)", "Total:"}
    assert {item["text"] for item in volume_evidence} == {"2.30", "T/CBM", "Total:"}


def test_table_total_for_items_uses_explicit_chinese_carton_count_header():
    entries = [
        (0, 0, "Description"),
        (0, 1, "Qty 箱子数量"),
        (0, 2, "CBM 立方米"),
        (0, 3, "G.W.（KGS） 毛重"),
        (0, 4, "N.W.（KGS） 净重"),
        (1, 0, "Total for 5 item"),
        (1, 1, "3.0"),
        (1, 2, "0.80"),
        (1, 3, "120.0"),
        (1, 4, "90.0"),
    ]
    cells = [
        {
            "pointer": f"/tables/0/data/table_cells/{i}/text",
            "text": text,
            "cell": {
                "start_row_offset_idx": row,
                "end_row_offset_idx": row + 1,
                "start_col_offset_idx": col,
                "end_col_offset_idx": col + 1,
                "column_header": row == 0,
                "row_header": False,
            },
        }
        for i, (row, col, text) in enumerate(entries)
    ]
    for field, expected, unit in (
        ("ctns", 3, None),
        ("line_volume", 0.8, "CBM"),
        ("line_gross_weight", 120, "kg"),
        ("line_net_weight", 90, "kg"),
    ):
        ((value, actual_unit, evidence),) = _pdf_table_field(cells, field)
        assert (value, actual_unit) == (expected, unit)
        assert "Total for 5 item" in {item["text"] for item in evidence}


def test_air_waybill_gross_column_keeps_chargeable_weight_separate():
    blocks = [
        block("Gross", 10, 100, 35, 95, 0),
        block("Weight", 10, 94, 35, 89, 1),
        block("210KGS", 9, 83, 38, 78, 2),
        block("Weight", 100, 94, 125, 89, 3),
        block("640KGS", 100, 83, 130, 78, 4),
    ]
    ((value, unit, evidence),) = list(_pdf_document_total_candidates(blocks, "line_gross_weight"))
    assert (value, unit) == (210, "kg")
    assert {item["text"] for item in evidence} == {"Gross", "Weight", "210KGS"}


def cargo_worker(
    tmp_path,
    package,
    *,
    label="Outer dimensions (L x W x H)",
    packed=None,
    dimension_observations=(),
    summary_entries=None,
):
    versions = p02.configurations(cache_path=tmp_path / "cache")[("docling", "pdf")]["versions"]

    def worker(request, *, python=None, model_cache=None):
        if request["operation"] == "probe":
            return versions
        entries = (
            [
                ("Total packages / type", 10, 95, 85, 90),
                (package, 130, 95, 175, 90),
            ]
            if packed is None
            else [(packed, 10, 95, 175, 90)]
        ) + [
            (label, 10, 75, 100, 70),
            ("52 x 27 x 19 cm", 130, 75, 190, 70),
            ("Description", 10, 55, 50, 50),
            ("First product", 130, 55, 190, 50),
            ("Other product", 130, 35, 190, 30),
        ]
        entries.extend(
            (text, 10, 20 - index * 10, 175, 15 - index * 10)
            for index, text in enumerate(dimension_observations)
        )
        if summary_entries is not None:
            entries = summary_entries
        texts = []
        for index, entry in enumerate(entries):
            b = block(*entry, index)
            texts.append({"text": b["text"], "prov": b["provenance"]})
        raw = json.dumps({"texts": texts, "tables": []}).encode("utf-8")
        path = Path(request["json_path"])
        path.write_bytes(raw)
        return {
            "conversion_status": "success",
            "errors": [],
            "versions": versions,
            "json_path": str(path),
            "json_sha256": hashlib.sha256(raw).hexdigest(),
            "document_pages": [1],
            "text_items": len(texts),
            "table_items": 0,
        }

    return worker


@pytest.mark.parametrize(
    ("second_gross", "expected_status"), [("1,183.00", "known"), ("1,200.00", "conflict")]
)
def test_packing_summary_uses_document_scope_and_preserves_conflict(
    indexed_server,
    tmp_path,
    monkeypatch,
    second_gross,
    expected_status,
):
    entries = [
        ("Qty of Volumes:", 80, 400, 150, 390),
        ("4", 190, 400, 200, 390),
        ("Net Weight (Kg):", 80, 385, 150, 375),
        ("1,058.00", 165, 385, 200, 375),
        ("Gross Weight (Kg):", 70, 370, 150, 360),
        ("1,183.00", 165, 370, 200, 360),
        ("Package Type:", 80, 355, 150, 345),
        ("PALLET", 165, 355, 200, 345),
        ("Net Weight (Kg):", 80, 260, 150, 250),
        ("0.057", 165, 260, 200, 250),
        ("Qty of Volumes: 4", 105, 140, 200, 130),
        ("Total Qty of Items: 21,000", 85, 125, 200, 115),
        ("Package Type: PALLET", 90, 110, 200, 100),
        ("Net Weight: 1,058.00 kg", 90, 95, 200, 85),
        (f"Gross Weight: {second_gross} kg", 90, 80, 200, 70),
    ]
    monkeypatch.setattr(p02, "worker", cargo_worker(tmp_path, "", summary_entries=entries))
    base, _, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    email = found["emails"][0]
    request = _indexed_request(email, email["attachments"][0])
    request["profile_id"] = "indexed-pdf-document-cargo"
    status, response = _request(base, "/local/parse", request)
    assert status == 200
    fields = {field["field_name"]: field for field in response["fields"]}
    assert (fields["line_net_weight"]["value"], fields["line_net_weight"]["unit"]) == (1058, "kg")
    assert fields["line_gross_weight"]["status"] == expected_status
    assert fields["line_gross_weight"]["value"] == (1183 if expected_status == "known" else None)
    assert fields["package_count"]["value"] == 4
    assert fields["package_type"]["value"] == "pallet"
    assert fields["ctns"]["status"] == fields["line_volume"]["status"] == "uncertain"
    assert all(s["source"]["locator"]["page"] == 1 for s in fields["line_net_weight"]["sources"])
    excerpts = {s["source"]["excerpt"] for s in fields["line_net_weight"]["sources"]}
    assert {"Net Weight (Kg):", "1,058.00", "Qty of Volumes:", "Package Type:"} <= excerpts
    assert "0.057" not in excerpts
    read_validated_result(
        response["result_ref"], content_root=tmp_path / "runs" / response["run_id"] / "p01"
    )


@pytest.mark.parametrize(
    ("sentence", "count"), [("PACKED IN ONE PACKAGE ONLY", 1), ("PACKED IN 3 PACKAGE ONLY", 3)]
)
def test_http_packed_in_keeps_generic_packages_distinct_from_cartons(
    indexed_server, tmp_path, monkeypatch, sentence, count
):
    monkeypatch.setattr(p02, "worker", cargo_worker(tmp_path, "", packed=sentence))
    base, _, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    email = found["emails"][0]
    request = _indexed_request(email, email["attachments"][0])
    request["profile_id"] = "indexed-pdf-document-cargo"
    status, response = _request(base, "/local/parse", request)
    fields = {field["field_name"]: field for field in response["fields"]}
    assert status == 200
    assert (fields["package_count"]["value"], fields["package_count"]["status"]) == (count, "known")
    assert fields["package_count"]["sources"][0]["source"]["excerpt"] == sentence
    assert fields["ctns"]["value"] is None
    assert fields["package_type"]["value"] is None


def test_http_dimension_observations_keep_weight_type_and_dimension_order_unresolved(
    indexed_server, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        p02,
        "worker",
        cargo_worker(
            tmp_path,
            "1 package",
            dimension_observations=[
                "DIMENSION:1# 84*72*41CM 140KG",
                "DIMENSION 2：57*38*47 CM 82KG",
            ],
        ),
    )
    base, _, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    email = found["emails"][0]
    request = _indexed_request(email, email["attachments"][0])
    request["profile_id"] = "indexed-pdf-document-cargo"
    status, response = _request(base, "/local/parse", request)
    assert status == 200
    observations = response["dimension_observations"]
    assert [(item["number"], item["dimensions"], item["weight_kg"]) for item in observations] == [
        ("1", "84*72*41", 140),
        ("2", "57*38*47", 82),
    ]
    assert all(
        item["dimension_order"] == "unspecified" and item["weight_kind"] == "unspecified"
        for item in observations
    )
    assert all(item["source"]["page"] == 1 for item in observations)


@pytest.mark.parametrize(
    ("package", "kind", "cartons", "dimension"),
    [
        ("1 carton", "carton", 1, 52),
        ("1 pallet", "pallet", None, None),
        ("1 package", None, None, None),
    ],
)
def test_http_document_package_scope_and_readback(
    indexed_server, tmp_path, monkeypatch, package, kind, cartons, dimension
):
    monkeypatch.setattr(p02, "worker", cargo_worker(tmp_path, package))
    base, _, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    email = found["emails"][0]
    request = _indexed_request(email, email["attachments"][0])
    request["profile_id"] = "indexed-pdf-document-cargo"
    status, response = _request(base, "/local/parse", request)
    assert status == 200
    fields = {field["field_name"]: field for field in response["fields"]}
    assert fields["package_count"]["value"] == 1
    assert fields["package_type"]["value"] == kind
    assert fields["ctns"]["value"] == cartons
    assert fields["length_cm"]["value"] == dimension
    assert fields["raw_observations"]["value"] == "52 x 27 x 19 cm"
    assert fields["product_name"]["value"] is None
    if kind:
        assert [s["source"]["locator"] for s in fields["package_type"]["sources"]] == [
            s["source"]["locator"] for s in fields["package_count"]["sources"]
        ]
    else:
        assert fields["package_type"]["issues"][0]["code"] == "package_type_unspecified"
    root = tmp_path / "runs" / response["run_id"] / "p01"
    snapshot = read_validated_result(response["result_ref"], content_root=root)
    if dimension is not None:
        candidate = next(
            c for c in snapshot["material_handoff"]["candidates"] if c["field_name"] == "length_cm"
        )
        candidate["field"]["value"] = 26
        tampered = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
        path = tmp_path / "tampered.json"
        path.write_bytes(tampered)
        with pytest.raises(ValueError, match="fixed source evidence"):
            read_validated_result(
                {"ref": str(path), "sha256": hashlib.sha256(tampered).hexdigest()},
                content_root=root,
            )


def test_dimensions_need_explicit_order():
    entries = [
        block("Total packages / type", 10, 95, 85, 90, 0),
        block("1 carton", 130, 95, 175, 90, 1),
        block("Outer dimensions", 10, 75, 100, 70, 2),
        block("52 x 27 x 19 cm", 130, 75, 190, 70, 3),
    ]
    assert _pdf_cargo_field(entries, {"field_name": "length_cm"}) is None
    assert _pdf_cargo_field(entries, {"field_name": "raw_observations"})[0] == "52 x 27 x 19 cm"


def test_weight_table_requires_exact_page_table_row_and_weight_column(
    indexed_server, tmp_path, monkeypatch
):
    def table(rows, page):
        return {
            "prov": [{"page_no": page}],
            "data": {
                "table_cells": [
                    {
                        "text": text,
                        "start_row_offset_idx": r,
                        "end_row_offset_idx": r + 1,
                        "start_col_offset_idx": c,
                        "end_col_offset_idx": c + 1,
                        "column_header": r == 0,
                    }
                    for r, row in enumerate(rows)
                    for c, text in enumerate(row)
                ]
            },
        }

    header = (
        "SKU",
        "Unit Gross Weight (kg)",
        "Line Net Weight (kg)",
        "Line Gross Weight (kg)",
        "Line Chargeable Weight (kg)",
        "Unit Net Weight (kg)",
    )
    tables = [
        table(
            [
                header,
                ("A", "9", "54", "63", "70", "8"),
                ("B", "19", "135", "152", "160 lb", "17"),
                ("Grand Total", "999", "189", "215", "230", "999"),
            ],
            1,
        ),
        table([header, ("B", "99", "795", "792", "800", "98")], 1),
    ]
    worker = cargo_worker(tmp_path, "")

    def structured_worker(request, **options):
        result = worker(request, **options)
        if request["operation"] == "convert":
            raw = json.dumps({"texts": [], "tables": tables}).encode()
            Path(result["json_path"]).write_bytes(raw)
            result.update(
                json_sha256=hashlib.sha256(raw).hexdigest(), text_items=0, table_items=len(tables)
            )
        return result

    monkeypatch.setattr(p02, "worker", structured_worker)
    base, _, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    email = found["emails"][0]
    request = _indexed_request(email, email["attachments"][0])
    request["profile_id"] = "local-attachment-weight-table"
    locator = {"table": "/parts/0/document/tables/0", "page": 1, "row": 3, "row_label": "B"}

    def parse(selector):
        status, response = _request(base, "/local/parse", {**request, "weight_table": selector})
        assert status == 200, response
        read_validated_result(
            response["result_ref"], content_root=tmp_path / "runs" / response["run_id"] / "p01"
        )
        return {field["field_name"]: field for field in response["fields"]}

    fields = parse(locator)
    assert {name: f["value"] for name, f in fields.items()} == {
        "unit_gross_weight": 19,
        "unit_net_weight": 17,
        "line_net_weight": 135,
        "line_gross_weight": 152,
        "line_chargeable_weight": None,
    }
    assert fields["unit_net_weight"]["quantity_scope"] == "per_item"
    assert fields["line_gross_weight"]["quantity_scope"] == "line_total"
    assert fields["line_chargeable_weight"]["quantity_kind"] == "chargeable_weight"
    for field in fields.values():
        for source in field["sources"]:
            pointer = source["source"]["locator"]
            assert (
                pointer["json_pointer"].startswith(locator["table"] + "/") and pointer["page"] == 1
            )
    assert "B" in {s["source"]["excerpt"] for s in fields["line_gross_weight"]["sources"]}
    assert parse({**locator, "row": 2, "row_label": "A"})["line_chargeable_weight"]["value"] == 70
    assert all(f["value"] is None for f in parse({**locator, "row": 2}).values())
    assert all(f["value"] is None for f in parse({**locator, "page": 2}).values())
    assert all(
        f["value"] is None
        for f in parse({**locator, "row": 4, "row_label": "Grand Total"}).values()
    )
    total = parse({"table": locator["table"], "page": 1})
    assert {name: f["value"] for name, f in total.items()} == {
        "shipment_net_weight": 189,
        "shipment_gross_weight": 215,
        "shipment_chargeable_weight": 230,
    }
    assert all(f["quantity_scope"] == "document" for f in total.values())


@pytest.mark.parametrize(
    ("footer", "matches"),
    [
        ("Total:2 Pallets, 1 Crate, G.W.: 640 KGS, 2.65CBM", True),
        ("Total:2 Pallets, 1 Crate, G.W.: 640 KGS, 2.64CBM", False),
        ("Total:2 Pallets, 1 Crate, G.W.: 640 KGS, 2.65CBM per pallet", False),
        ("Subtotal:2 Pallets, 1 Crate, G.W.: 640 KGS, 2.65CBM", False),
    ],
)
def test_packing_table_decimal_volume_is_confirmed_by_its_full_width_footer(footer, matches):
    entries = [
        (0, 0, 1, "Item"),
        (0, 1, 2, "Qty PCS"),
        (0, 2, 3, "CBM"),
        (1, 0, 1, "PLT 1"),
        (1, 1, 2, "3"),
        (1, 2, 3, "1,15"),
        (2, 0, 1, "TOTAL"),
        (2, 1, 2, "25"),
        (2, 2, 3, "2,65"),
        (3, 0, 3, footer),
    ]
    cells = [
        {
            "pointer": f"/tables/0/data/table_cells/{i}/text",
            "text": text,
            "cell": {
                "start_row_offset_idx": row,
                "end_row_offset_idx": row + 1,
                "start_col_offset_idx": col,
                "end_col_offset_idx": end,
                "column_header": row == 0,
            },
        }
        for i, (row, col, end, text) in enumerate(entries)
    ]
    actual = _pdf_table_field(cells, "line_volume")
    if matches:
        ((number, unit, evidence),) = actual
        assert (number, unit) == (2.65, "CBM")
        assert {x["text"] for x in evidence} == {"2,65", "CBM", "TOTAL", footer}
    else:
        assert actual == []
