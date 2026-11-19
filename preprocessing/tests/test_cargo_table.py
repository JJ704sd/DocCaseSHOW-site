from copy import deepcopy

import pytest

from doccase_preprocessing import main_channel
from doccase_preprocessing.cargo_table import cargo_row_profile, packing_rows


def cell(row, col, text, *, span=1):
    return {
        "text": text,
        "pointer": f"/tables/0/data/table_cells/{row * 20 + col}/text",
        "provenance": [{"page_no": 1}],
        "cell": {
            "start_row_offset_idx": row,
            "end_row_offset_idx": row + span,
            "start_col_offset_idx": col,
            "end_col_offset_idx": col + 1,
        },
    }


def test_packing_rows_bind_quantities_and_skip_merged_weights_and_total():
    from local_web import pdf_packing_rows

    header = [
        "Item",
        "Description",
        "Total quantity",
        "Cartons",
        "Net Weight (KG)",
        "Gross Weight (KG)",
        "Measurement (CM)",
        "Volume (CBM)",
    ]
    cells = [cell(0, i, x) for i, x in enumerate(header)]
    cells += [
        cell(1, i, x, span=2 if i == 4 else 1)
        for i, x in enumerate(["1", "Sample A", "200", "2", "19", "12.5", "40*25*30", "0.06"])
    ]
    cells += [
        cell(2, i, x)
        for i, x in enumerate(["2", "Sample B", "100", "1", "", "10", "30*20*25", "0.015"])
    ]
    cells += [cell(3, i, x) for i, x in enumerate(["Total", "", "300", "3", "19", "22.5"])]
    rows = list(packing_rows(cells, {"executed_profile": {"reader": "DoclingNativePdf"}}))
    assert [r["ordinal"] for r in rows] == [1, 2]
    a, b = [r["facts"] for r in rows]
    assert a["ctns"][:2] == (2, None)
    assert a["line_gross_weight"][:2] == (12.5, "kg")
    assert a["length_cm"][:2] == (40, "cm")
    assert b["line_volume"][:2] == (0.015, "CBM")
    assert all("line_net_weight" not in f and "unit_gross_weight" not in f for f in (a, b))
    assert a["ctns"][2][0]["pointer"] == cell(1, 3, "")["pointer"]
    first, second = pdf_packing_rows(cells, {"executed_profile": {"reader": "DoclingNativePdf"}})
    assert [first["row"], second["row"]] == [2, 3]
    assert first["fields"]["ctns"]["sources"][0] == {
        "page": 1,
        "pointer": cell(1, 3, "")["pointer"],
        "excerpt": "2",
    }
    assert "line_net_weight" not in first["fields"] and "line_net_weight" not in second["fields"]


def test_invoice_quantity_is_not_a_carton_column():
    cells = [
        cell(0, i, x)
        for i, x in enumerate(["Item", "Description", "Quantity", "Gross Weight (KG)"])
    ]
    cells += [cell(1, i, x) for i, x in enumerate(["1", "Sample", "3000", "12.5"])]
    assert list(packing_rows(cells, {"executed_profile": {"reader": "DoclingNativePdf"}})) == []


def test_bilingual_packing_does_not_use_sales_quantity_or_unscoped_weight():
    headers = [
        "shipping mark 唛头",
        "CTN NO. 箱号",
        "Products Description 产品描述",
        "Date: 2099-03-15 Qty 箱子数量",
        "Size 箱尺寸（厘米）",
        "CBM 立方米",
        "G.W.（KGS） 毛重（公斤）",
        "N.W. （KGS） 净重 （公斤）",
    ]
    data = ["SKU-A", "1-2", "sample product", "2.00", "50*50*50", "0.25", "62.0", "18.0"]
    total = ["4pcs", "2", "Total for 4 item", "2.0", "", "0.2500", "124.0", "36.0"]
    title = {"text": "PACKING LIST", "pointer": "/texts/0/text", "provenance": [{"page_no": 1}]}
    cells = [
        cell(i, j, value)
        for i, row in enumerate((headers, data, total))
        for j, value in enumerate(row)
    ]
    execution = {"executed_profile": {"reader": "DoclingPdfStructured"}}
    assert list(packing_rows(cells, execution)) == []
    (row,) = packing_rows([title, *cells], execution)
    assert row["selector"] == {"page": 1, "table": "/tables/0", "row": 2}
    assert row["facts"]["ctns"][:2] == (2, None)
    assert row["facts"]["product_name"][:2] == ("sample product", None)
    assert row["facts"]["line_volume"][:2] == (0.25, "CBM")
    assert all("weight" not in name for name in row["facts"])


def test_pallet_quantity_and_gross_do_not_leak_across_merged_rows():
    title = {
        "text": "PL",
        "pointer": "/tables/0/data/table_cells/0/text",
        "provenance": [{"page_no": 1}],
        "cell": {**cell(0, 0, "")["cell"], "end_col_offset_idx": 8},
    }
    headers = ["ITEM#", "ITEM Description", "QTY PCS", "G.W. PER PLT KGS", "CBM", "Packing SIZE(m)"]
    data = [
        ["PLT 1", "Product A", "2", "155", "1,50"],
        ["Crate 1", "Product B", "1", "110", "1,25"],
        ["", "Product C", "10", "", ""],
    ]
    cells = [cell(1, j, value) for j, value in enumerate(headers)]
    cells += [
        cell(i, j, value, span=2 if i == 3 and j in (0, 3, 4) else 1)
        for i, row in enumerate(data, 2)
        for j, value in enumerate(row)
    ]
    rows = list(
        packing_rows([title, *cells], {"executed_profile": {"reader": "DoclingPdfStructured"}})
    )
    assert [r["ordinal"] for r in rows] == [1, 2, 3]
    assert rows[0]["facts"]["line_gross_weight"][:2] == (155, "kg")
    assert rows[0]["facts"]["line_volume"][:2] == (1.50, "CBM")
    assert all("ctns" not in r["facts"] and "unit_gross_weight" not in r["facts"] for r in rows)
    assert [set(r["facts"]) for r in rows[1:]] == [{"product_name"}, {"product_name"}]


def test_weight_per_item_has_its_own_header_and_proof():
    headers = [
        "Item",
        "Description",
        "Cartons",
        "Gross Weight (kg)",
        "Net Weight (kg)",
        "G.W./CTN (kg)",
        "Unit Gross Weight (kg)",
    ]
    data = ["1", "Product", "2", "30", "26", "15", "3"]
    cells = [
        cell(i, j, value) for i, row in enumerate((headers, data)) for j, value in enumerate(row)
    ]
    (row,) = packing_rows(cells, {"executed_profile": {"reader": "DoclingPdfStructured"}})
    facts = row["facts"]
    assert facts["line_net_weight"][:2] == (26, "kg")
    assert facts["line_gross_weight"][:2] == (30, "kg")
    assert facts["unit_gross_weight"][:2] == (3, "kg")
    assert facts["unit_gross_weight"][2][1]["pointer"] == cell(0, 6, "")["pointer"]
    assert "unit_net_weight" not in facts


def test_multilingual_two_level_headers_keep_real_leaf_sources_and_repeat():
    def band(y):
        merged = cell(y, 4, "Peso (kg)")
        merged["cell"]["end_col_offset_idx"] = 6
        return [
            cell(y, 0, "Número de línea", span=2),
            cell(y, 1, "Descripción de la mercancía", span=2),
            cell(y, 2, "Cajas de cartón", span=2),
            cell(y, 3, "PCS", span=2),
            merged,
            cell(y + 1, 4, "Neto"),
            cell(y + 1, 5, "Bruto"),
        ]

    data = [
        *band(0),
        *[cell(2, i, value) for i, value in enumerate(("1", "A", "3", "200", "12", "15"))],
        *band(3),
        *[
            cell(5, i, value, span=2 if i == 4 else 1)
            for i, value in enumerate(("2", "B", "4", "300", "30", "33"))
        ],
    ]
    a, b = packing_rows(data, {"executed_profile": {"reader": "DoclingPdfStructured"}})
    assert [a["selector"]["row"], b["selector"]["row"]] == [3, 6]
    assert a["facts"]["ctns"][:2] == (3, None)
    assert a["facts"]["line_net_weight"][:2] == (12, "kg")
    assert [part["text"] for part in b["facts"]["line_gross_weight"][2]] == [
        "33",
        "Peso (kg)",
        "Bruto",
        "2",
    ]
    assert "line_net_weight" not in b["facts"]


def test_cross_page_table_uses_the_repeat_header_on_each_page():
    header = ["Item", "Product", "Cartons", "Gross Weight (kg)"]
    blocks = []
    for i, row in enumerate(
        (header, ["1", "A", "2", "10"], header, ["2", "B", "3", "14"], ["3", "C", "4", "18"])
    ):
        page = 1 if i < 2 else 2 if i < 4 else 3
        blocks.extend(
            {**cell(i, j, value), "provenance": [{"page_no": page}]} for j, value in enumerate(row)
        )
    first, second = packing_rows(blocks, {"executed_profile": {"reader": "DoclingPdfStructured"}})
    assert first["selector"] == {"page": 1, "table": "/tables/0", "row": 2}
    assert second["selector"] == {"page": 2, "table": "/tables/0", "row": 4}
    assert {b["provenance"][0]["page_no"] for b in second["facts"]["line_gross_weight"][2]} == {2}


def test_agent_can_omit_proven_row_headers_but_not_cite_a_different_row(monkeypatch):
    header = ["Item", "Product", "Cartons", "Gross Weight (kg)"]
    blocks = [
        cell(i, j, value)
        for i, row in enumerate((header, ["1", "A", "2", "10"], ["2", "B", "3", "10"]))
        for j, value in enumerate(row)
    ]
    execution = {"executed_profile": {"reader": "DoclingPdfStructured"}}
    rows = list(packing_rows(blocks, execution))
    req = next(
        r
        for r in cargo_row_profile("M1", "T1", rows)["requirements"]
        if r["requirement_id"] == "ROW-1-line_gross_weight"
    )
    monkeypatch.setattr(main_channel, "_blocks", lambda _: ({}, blocks))

    def check(b):
        return main_channel._complete_agent_read_evidence(
            [(b, 0, len(b["text"]), "read")],
            10,
            req,
            {},
            execution,
            {},
            None,
        )

    assert [b["text"] for b, *_ in check(blocks[7])] == ["10", "Gross Weight (kg)", "1"]
    assert [b for b, *_ in check(blocks[11])] == [blocks[11]]


def test_explicit_product_weight_uses_its_own_parent_and_compatible_numeric_cell():
    item_weight = cell(0, 4, "Per item weight (kg)")
    item_weight["cell"]["end_col_offset_idx"] = 6
    carton_weight = cell(0, 6, "Per carton weight (kg)")
    cells = [
        cell(0, 0, "Item", span=2),
        cell(0, 1, "Product", span=2),
        cell(0, 2, "Cartons", span=2),
        cell(0, 3, "Gross Weight (kg)", span=2),
        item_weight,
        carton_weight,
        cell(1, 4, "Net"),
        cell(1, 5, "Gross"),
        cell(1, 6, "Gross"),
    ]
    cells += [
        cell(2, i, value)
        for i, value in enumerate(("1", "A", "2 ctns", "1.234,50 kg", "0,45", "1,250", "6 kg"))
    ]
    cells += [
        cell(3, i, value)
        for i, value in enumerate(("2", "B", "20 PCS", "180 lb", "0,20", "0,25 kg", "6 kg"))
    ]
    a, b = packing_rows(cells, {"executed_profile": {"reader": "DoclingPdfStructured"}})
    assert a["facts"]["line_gross_weight"][:2] == (1234.5, "kg")
    assert a["facts"]["unit_net_weight"][:2] == (0.45, "kg")
    assert a["facts"]["ctns"][:2] == (2, None)
    assert "unit_gross_weight" not in a["facts"]
    assert b["facts"]["unit_gross_weight"][:2] == (0.25, "kg")
    assert "line_gross_weight" not in b["facts"] and "ctns" not in b["facts"]


def positioned(index, text, x, y, width=25, *, height=8, page=1):
    return {
        "text": text,
        "pointer": f"/texts/{index}/text",
        "provenance": [
            {
                "page_no": page,
                "bbox": {
                    "l": x,
                    "r": x + width,
                    "t": y,
                    "b": y - height,
                    "coord_origin": "BOTTOMLEFT",
                },
            }
        ],
    }


@pytest.mark.parametrize("reader", ["DoclingNativePdf", "DoclingPdfScanned"])
def test_positioned_packing_list_does_not_need_a_docling_table(reader):
    blocks = [
        positioned(0, "Packing List", 10, 150, 90),
        positioned(1, "Product Name", 10, 120, 80),
        positioned(2, "Cartons", 115, 120),
        positioned(3, "QTY PCS", 165, 120),
        positioned(4, "Line Gross Weight (kg)", 215, 120, 90),
        positioned(5, "Unit Net Weight (kg)", 335, 120, 80),
        positioned(6, "Sample A", 10, 100, 50),
        positioned(7, "2", 120, 100, 6),
        positioned(8, "60", 175, 100, 9),
        positioned(9, "17.5", 260, 100, 15),
        positioned(10, "0.25", 380, 100, 15),
        positioned(11, "Sample B", 10, 80, 50),
        positioned(12, "3", 120, 80, 6),
        positioned(13, "9", 260, 80, 6),
        positioned(14, "Total", 10, 60),
        positioned(15, "5", 120, 60, 6),
        positioned(16, "26.5", 260, 60, 15),
    ]
    rows = list(packing_rows(blocks, {"executed_profile": {"reader": reader}}))
    first, second = rows
    assert first["selector"] == {"page": 1, "anchor": "/texts/6/text", "row": 1}
    assert {name: proof[:2] for name, proof in first["facts"].items()} == {
        "product_name": ("Sample A", None),
        "ctns": (2, None),
        "line_gross_weight": (17.5, "kg"),
        "unit_net_weight": (0.25, "kg"),
    }
    assert second["facts"]["ctns"][:2] == (3, None)
    proof = first["facts"]["line_gross_weight"][2]
    assert {b["pointer"] for b in proof} == {
        "/texts/0/text",
        "/texts/4/text",
        "/texts/6/text",
        "/texts/9/text",
    }
    profile = cargo_row_profile("m", "task", rows)
    objects = {r["object_selector"] for r in profile["requirements"]}
    assert len(objects) == 2 and all("/cargo-position/page-1/texts/" in obj for obj in objects)


@pytest.mark.parametrize(
    "variant", ["invoice", "duplicate_anchor", "cross_row", "ambiguous_column"]
)
def test_positioned_fallback_rejects_ambiguous_fields(variant):
    blocks = [
        positioned(0, "Packing List", 10, 150, 90),
        positioned(1, "Product Name", 10, 120, 80),
        positioned(2, "Cartons", 115, 120),
        positioned(3, "Sample A", 10, 100, 50),
        positioned(4, "Sample A" if variant == "duplicate_anchor" else "Sample B", 10, 90, 50),
        positioned(5, "2", 120, 100, 6, height=18 if variant == "cross_row" else 8),
    ]
    if variant == "invoice":
        blocks.append(positioned(6, "Invoice", 230, 140, 40))
    if variant == "ambiguous_column":
        blocks.append(positioned(6, "5", 130, 100, 6))
    rows = list(packing_rows(blocks, {"executed_profile": {"reader": "DoclingNativePdf"}}))
    if variant in {"invoice", "duplicate_anchor"}:
        assert rows == []
    else:
        assert len(rows) == 2
        assert all("ctns" not in r["facts"] for r in rows)


def test_pdf_row_net_weights_recover_only_from_matching_positioned_text():
    """A merged cell must not prevent recovery of independently positioned values."""
    header = ["Item", "Description", "Cartons", "Net Weight (KG)", "Gross Weight (KG)"]
    source_rows = [
        ["1", "Alpha", "1", "17.4 19", "21.4"],
        ["2", "Beta", "1", "9", "10"],
        ["3", "Gamma", "2", "63", "70"],
        ["4", "Delta", "1", "9", "10"],
        ["5", "Epsilon", "1", "", "22"],
    ]
    blocks = [
        cell(y, x, text)
        for y, row in enumerate([header, *source_rows])
        for x, text in enumerate(row)
        if text
    ]
    for i, (name, net) in enumerate(
        zip(
            ("Alpha", "Beta", "Gamma", "Delta", "Epsilon"),
            ("17.4", "9", "63", "9", "19"),
            strict=True,
        ),
        1,
    ):
        blocks += [
            positioned(2 * i, f"{i} P{i} {name}", 20, 160 - i * 20, 90),
            positioned(2 * i + 1, net, 200, 160 - i * 20, 12),
        ]
    rows = list(packing_rows(blocks, {"executed_profile": {"reader": "DoclingNativePdf"}}))
    assert [r["ordinal"] for r in rows] == [1, 2, 3, 4, 5]
    assert rows[0]["facts"]["line_net_weight"][:2] == (17.4, "kg")
    assert rows[4]["facts"]["line_net_weight"][:2] == (19, "kg")
    assert rows[0]["facts"]["line_net_weight"][2][0]["pointer"] == "/texts/3/text"
    assert rows[4]["facts"]["line_net_weight"][2][0]["pointer"] == "/texts/11/text"
    ambiguous = [*blocks, positioned(99, "17.4", 200, 140, 12)]
    first = next(packing_rows(ambiguous, {"executed_profile": {"reader": "DoclingNativePdf"}}))
    assert "line_net_weight" not in first["facts"]
    shared = deepcopy(blocks)
    shared_net = next(b for b in shared if b.get("cell") and b["text"] == "17.4 19")
    shared_net["cell"]["end_row_offset_idx"] = 3
    first = next(packing_rows(shared, {"executed_profile": {"reader": "DoclingNativePdf"}}))
    assert "line_net_weight" not in first["facts"]
