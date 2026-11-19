"""Focused checks for the opt-in PDF candidate and its evidence boundary."""

import hashlib
import json

import pytest

from doccase_preprocessing.agent_gap_evidence import (
    compact_cargo_context,
    compact_gap_evidence,
    quote_table_gap_evidence,
    split_cargo_gap_evidence,
)
from doccase_preprocessing.cargo_table import packing_rows
from doccase_preprocessing.extraction_agent import (
    AgentResponseError,
    mapped_markdown_fragments,
    parse_response,
)
from doccase_preprocessing.inquiry_details import agent_body_detail_proofs
from doccase_preprocessing.main_channel import (
    _attachment_document_totals,
    _blocks,
    _pdf_split_total,
)
from doccase_preprocessing.material_agent import material_evidence
from doccase_preprocessing.pdf_profile import PDF_SCANNED


def block(text, index, left, *, page=1, top=100):
    return {
        "pointer": f"/texts/{index}/text",
        "text": text,
        "provenance": [
            {
                "page_no": page,
                "bbox": {
                    "l": left,
                    "r": left + 20,
                    "t": top,
                    "b": top - 5,
                    "coord_origin": "BOTTOMLEFT",
                },
            }
        ],
    }


def test_grouped_total_is_available_for_evidence_but_not_default_rule():
    label = block("TOTAL GROSS WEIGHT:", 0, 10)
    value = block("7,148.00 KGS", 1, 40)
    assert _pdf_split_total([label, value], "line_gross_weight") is None
    found = _pdf_split_total([label, value], "line_gross_weight", evidence_only=True)
    assert found[:2] == (7148, "kg")
    assert [item["text"] for item in found[2]] == [value["text"], label["text"]]
    assert (
        _pdf_split_total(
            [label, block(value["text"], 1, 40, page=2)], "line_gross_weight", evidence_only=True
        )
        is None
    )
    assert (
        _pdf_split_total(
            [label, block("71,48.00 KGS", 1, 40)], "line_gross_weight", evidence_only=True
        )
        is None
    )
    part_label = {**label, "pointer": "/parts/0/document/texts/0/text"}
    part_value = {**value, "pointer": "/parts/1/document/texts/1/text"}
    assert (
        _pdf_split_total([part_label, part_value], "line_gross_weight", evidence_only=True) is None
    )


def test_pdf_candidate_requires_two_distinct_supplied_evidence_ids():
    response = {
        "requirement_id": "gross",
        "candidate": {"value": 7148, "evidence_ids": ["E1", "E2"]},
    }
    assert (
        parse_response(response, "gross", {"E1", "E2"}, evidence_count=2) == response["candidate"]
    )
    response["candidate"]["evidence_ids"] = ["E1", "E1"]
    with pytest.raises(AgentResponseError):
        parse_response(response, "gross", {"E1", "E2"}, evidence_count=2)
    response["candidate"]["evidence_ids"] = ["E1", "other-material"]
    with pytest.raises(AgentResponseError):
        parse_response(response, "gross", {"E1", "E2"}, evidence_count=2)


def test_registered_markdown_position_mismatch_is_not_a_read_fallback(tmp_path):
    def write(path, value):
        path.write_bytes(value)
        return hashlib.sha256(value).hexdigest()

    read_path, md_path, index_path = (
        tmp_path / name for name in ("read.json", "content.md", "index.json")
    )
    read_hash = write(read_path, json.dumps({"texts": [{"text": "TOTAL"}]}).encode())
    md_hash = write(md_path, b"WRONG")
    index = {
        "markdown_sha256": md_hash,
        "read_sha256": read_hash,
        "execution_id": "x",
        "material_id": "m",
        "fragments": [
            {
                "source_role": "attachment",
                "read_pointer": "/texts/0/text",
                "read_start": 0,
                "read_end": 5,
                "markdown_start": 0,
                "markdown_end": 5,
            }
        ],
    }
    index_hash = write(index_path, json.dumps(index).encode())
    sidecar = {
        "state": "success",
        "markdown_ref": str(md_path),
        "markdown_sha256": md_hash,
        "index_ref": str(index_path),
        "index_sha256": index_hash,
    }
    execution = {"ref": str(read_path), "artifact_hash": read_hash, "execution_id": "x"}
    with pytest.raises(ValueError, match="fragment position mismatch"):
        mapped_markdown_fragments(sidecar, execution, "m", source_role="attachment", strict=True)


@pytest.mark.parametrize(
    "rendered", ["A &amp; B&#124;C<br>D &lt; E", "A &amp; B&#124;C<br>X &lt; E"]
)
def test_escaped_excel_cell_must_match_the_same_read_span(tmp_path, rendered):
    raw = "A & B|C\nD < E"
    pointer = "/tables/0/data/table_cells/0/text"
    read, md, ix = (tmp_path / name for name in ("read.json", "content.md", "index.json"))
    read.write_text(
        json.dumps({"tables": [{"data": {"table_cells": [{"text": raw}]}}]}), encoding="utf-8"
    )
    md.write_bytes(f"<td>{rendered}</td>".encode())
    read_sha, md_sha = (hashlib.sha256(path.read_bytes()).hexdigest() for path in (read, md))
    ix.write_text(
        json.dumps(
            {
                "markdown_sha256": md_sha,
                "read_sha256": read_sha,
                "execution_id": "x",
                "material_id": "m",
                "fragments": [
                    {
                        "source_role": "attachment",
                        "read_pointer": pointer,
                        "markdown_encoding": "html",
                        "read_start": 0,
                        "read_end": len(raw),
                        "markdown_start": 4,
                        "markdown_end": 4 + len(rendered),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    sidecar = {
        "state": "success",
        "markdown_ref": str(md),
        "markdown_sha256": md_sha,
        "index_ref": str(ix),
        "index_sha256": hashlib.sha256(ix.read_bytes()).hexdigest(),
    }
    execution = {"ref": str(read), "artifact_hash": read_sha, "execution_id": "x"}
    if "<br>X" in rendered:
        with pytest.raises(ValueError, match="fragment position mismatch"):
            mapped_markdown_fragments(
                sidecar, execution, "m", source_role="attachment", strict=True
            )
    else:
        [(fragment, evidence)] = mapped_markdown_fragments(
            sidecar, execution, "m", source_role="attachment", strict=True
        )
        assert (fragment["read_pointer"], evidence) == (pointer, raw)


@pytest.mark.parametrize(
    ("line", "quoted", "expected"),
    [
        (
            "The shipper confirms that this shipment is FCA our warehouse.",
            False,
            "this shipment is FCA our warehouse",
        ),
        ("This shipment is FCA our warehouse, or FOB at the port.", False, None),
        ("If this shipment is FCA our warehouse, please quote.", False, None),
        ("This shipment is FCA our warehouse if the quote is accepted.", False, None),
        ("This shipment is FCA our warehouse.", True, None),
    ],
)
def test_current_shipment_term_registers_exact_clause_only(line, quoted, expected):
    raw = f"<p>Please see below</p><blockquote>{line}</blockquote>" if quoted else f"<p>{line}</p>"
    evidence, bindings = material_evidence(None, {}, "body", [block(line, 0, 10)], "current", raw)
    spans = [item for item in evidence if item.get("text") != line]
    assert [item["text"] for item in spans] == ([expected] if expected else [])
    if expected:
        b, start, end, representation = bindings[spans[0]["evidence_id"]]
        assert (b["text"][start:end], representation) == (expected, "read")


@pytest.mark.parametrize(("page", "row"), [(2, 3), (1, 3), (2, 4)])
def test_markdown_table_location_is_verified_against_read(tmp_path, page, row):
    pointer = "/tables/0/data/table_cells/0/text"
    read, md, ix = (tmp_path / name for name in ("read.json", "content.md", "index.json"))
    read.write_text(
        json.dumps(
            {
                "tables": [
                    {
                        "prov": [{"page_no": 2}],
                        "data": {
                            "table_cells": [
                                {
                                    "text": "12",
                                    "start_row_offset_idx": 2,
                                    "end_row_offset_idx": 3,
                                    "start_col_offset_idx": 1,
                                    "end_col_offset_idx": 2,
                                }
                            ]
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    md.write_bytes(b"| 12 |")
    read_sha, md_sha = (hashlib.sha256(p.read_bytes()).hexdigest() for p in (read, md))
    ix.write_text(
        json.dumps(
            {
                "markdown_sha256": md_sha,
                "read_sha256": read_sha,
                "execution_id": "x",
                "material_id": "m",
                "fragments": [
                    {
                        "source_role": "attachment",
                        "read_pointer": pointer,
                        "read_start": 0,
                        "read_end": 2,
                        "markdown_start": 2,
                        "markdown_end": 4,
                        "markdown_encoding": "html",
                        "table_location": {
                            "table_pointer": "/tables/0",
                            "page": page,
                            "row": row,
                            "row_end": 3,
                            "column": 2,
                            "column_end": 2,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    sidecar = {
        "state": "success",
        "markdown_ref": str(md),
        "markdown_sha256": md_sha,
        "index_ref": str(ix),
        "index_sha256": hashlib.sha256(ix.read_bytes()).hexdigest(),
    }
    execution = {"ref": str(read), "artifact_hash": read_sha, "execution_id": "x"}
    if (page, row) == (2, 3):
        assert len(mapped_markdown_fragments(sidecar, execution, "m", strict=True)) == 1
    else:
        with pytest.raises(ValueError, match="fragment position mismatch"):
            mapped_markdown_fragments(sidecar, execution, "m", strict=True)


@pytest.mark.parametrize(("current", "expected"), [(True, 1), (False, 0)])
def test_read_binds_current_route_even_if_reply_quotes_same_text(current, expected):
    line = "到LHR 3PLTS/735KGS/6.42CBM"
    original = f"<p>{line if current else 'Please see below'}</p><blockquote>{line}</blockquote>"
    evidence, bindings = material_evidence(
        None, {}, "body", [{"pointer": "/texts/0/text", "text": line}], "current", original
    )
    assert len(evidence) == expected
    if current:
        assert evidence[0]["text"] == line
        assert bindings[evidence[0]["evidence_id"]][0]["pointer"] == "/texts/0/text"


def test_scanned_invoice_rows_do_not_reuse_packing_list_schema():
    blocks = []
    for row, values in enumerate(
        (("Item", "Product Name", "Cartons", "Gross Weight (kg)"), ("1", "Fabric", "4", "9"))
    ):
        for col, value in enumerate(values):
            index = len(blocks)
            blocks.append(
                {
                    **block(value, index, col * 100, page=2),
                    "pointer": f"/tables/0/data/table_cells/{index}/text",
                    "cell": {
                        "start_row_offset_idx": row,
                        "end_row_offset_idx": row + 1,
                        "start_col_offset_idx": col,
                        "end_col_offset_idx": col + 1,
                    },
                }
            )
    execution = {"executed_profile": {"reader": PDF_SCANNED}}
    packing = {**block("ALBARAN PACKING LIST", 0, 20, page=1), "pdf_pass": "ocr"}
    invoice = {**block("FACTURA INVOICE", 1, 20, page=2), "pdf_pass": "ocr"}
    assert len(list(packing_rows([packing, *blocks], execution))) == 1
    assert list(packing_rows([packing, invoice, *blocks], execution)) == []


@pytest.mark.parametrize("variant", ["packing", "invoice", "wrong_page", "ambiguous"])
def test_scanned_packing_footer_binds_page_columns_and_units(tmp_path, variant):
    lines = [
        ("ALBARAN PACKING LIST" if variant != "invoice" else "FACTURA INVOICE", 20, 200, 500, 480),
        ("TOTAL S U M A", 185, 295, 120, 109),
        ("Bultos Packages", 20, 77, 85, 62),
        ("17", 55, 65, 62.4, 50),
        ("Peso Bruto P.B. kg.", 92, 206, 85, 73),
        ("GrossWeight G.W. 192,2500", 99, 200, 73.4, 50),
        ("Peso Neto P.B. kg. Net Weight G.W. 180,5000", 229, 343, 85, 49),
        ("Volumen m3. Volume", 379, 449, 84, 62),
        ("0, 5621", 402, 449, 62, 49),
        ("843,00", 402, 449, 160, 150),
    ]
    if variant == "ambiguous":
        lines.append(("18", 54, 66, 61, 49))
    doc = {
        "texts": [
            {
                "text": value,
                "prov": [
                    {
                        "page_no": 3 if variant == "wrong_page" and i == 8 else 2,
                        "bbox": {
                            "l": left,
                            "r": right,
                            "t": top,
                            "b": bottom,
                            "coord_origin": "BOTTOMLEFT",
                        },
                    }
                ],
            }
            for i, (value, left, right, top, bottom) in enumerate(lines)
        ]
    }
    ref = tmp_path / "scanned.json"
    raw = json.dumps(doc).encode()
    ref.write_bytes(raw)
    _, blocks = _blocks(
        {
            "ref": str(ref),
            "artifact_hash": hashlib.sha256(raw).hexdigest(),
            "executed_profile": {"reader": PDF_SCANNED},
        }
    )
    for field, expected, unit in (
        ("package_count", 17, None),
        ("line_gross_weight", 192.25, "kg"),
        ("line_net_weight", 180.5, "kg"),
        ("line_volume", 0.5621, "CBM"),
        ("ctns", None, None),
    ):
        req = {
            "field_name": field,
            "material_id": "pdf",
            "object_selector": "pdf/document",
            "source_kind": "attachment",
            "pdf_table_total": True,
        }
        proofs = list(_attachment_document_totals(blocks, req, PDF_SCANNED))
        if variant == "packing" and expected is not None:
            assert len(proofs) == 1
            assert proofs[0][:2] == (expected, unit)
            assert {"/texts/0/text", "/texts/1/text"} <= {b["pointer"] for b in proofs[0][2]}
            assert all(b["provenance"][0]["page_no"] == 2 for b in proofs[0][2])
        else:
            assert proofs == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "货物信息：\nshipment3:\nLHR-AMS\n3托盘/1.10X0.8X1.20 450KG 3.4CBM",
            {"package_type": ("pallet", None), "line_volume": (3.4, "CBM")},
        ),
        ("Qty: 5 Cartons", {"package_type": ("carton", None)}),
        ("24ctns/ 380KG 1.75CBM", {"package_type": ("carton", None), "line_volume": (1.75, "CBM")}),
        ("24ctns/ 380KG 1.75CBM per carton", {}),
        ("24ctns/ 380KG 1.75CBM\n每箱数据", {}),
        ("shipment1:\n24ctns/ 380KG 1.75CBM\nshipment2:\n12ctns/ 310KG 0.9CBM", {}),
        ("If 24ctns/ 380KG 1.75CBM", {}),
        ("条款为 EXW Ningbo. 需要安排上门提货。", {"trade_terms": ("EXW", None)}),
        ("条款为 EXW Ningbo 或者 FOB", {}),
        ("条款为 EXW FOB", {}),
        ("条款为 EXW Ningbo. 需要安排上门提货，待确认。", {}),
    ],
)
def test_gap_proofs_keep_field_scope_and_complete_context(text, expected):
    actual = {
        field: (value, unit)
        for field, value, unit, _, _ in compact_gap_evidence(text, lambda _: True)
    }
    assert actual == expected
    assert not list(compact_gap_evidence(text, lambda _: False))


def test_context_window_includes_only_the_current_shipment_heading_and_route():
    line = "3托盘/1.10X0.8X1.20 450KG 3.4CBM"
    context = f"货物信息：\nshipment3:\nLHR-AMS\n{line}"
    text = f"Unrelated greeting\n{context}\n1:120"
    start = text.index(line)
    left, right = compact_cargo_context(text, start, start + len(line), lambda _: True)
    assert text[left:right] == context
    assert compact_cargo_context(text, start, start + len(line), lambda _: False) is None
    unsafe = f"货物信息：\n可选路线\nLHR-AMS\n{line}"
    assert compact_cargo_context(unsafe, unsafe.index(line), len(unsafe), lambda _: True) is None


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (
            ["TEL: 202-555-0100", "ADD: Test City", "mail: dispatch@example.test"],
            {"contact_phone": "202-555-0100", "signature_email": "dispatch@example.test"},
        ),
        (["TEL: 202-555-0100", "Forwarded party:", "mail: dispatch@example.test"], {}),
        (["TEL: 202-555-0100", "TEL: 202-555-0101", "mail: dispatch@example.test"], {}),
        (["TEL: 202-555-0100", "From: dispatch@example.test"], {}),
        (["2.货今天下午好，但是核对后才能放货"], {"cargo_ready_time": "今天下午"}),
        (
            ["Cargo will be ready by tomorrow afternoon."],
            {"cargo_ready_time": "by tomorrow afternoon"},
        ),
        (["货今天下午好，等核对后才能备好"], {}),
        (["请问货今天下午好吗？", "Cargo readiness date, ready"], {}),
        (["本票货物为普货"], {"cargo_type": "general cargo"}),
        (
            ["Contact person: Ana", "Customer Company: Example Trading"],
            {"contact_name": "Ana", "customer_company": "Example Trading"},
        ),
        (["此票需要增加一份订单一起运输,普货 无电池", "以下报价适用于普货"], {}),
    ],
)
def test_agent_details_prove_explicit_fields_without_a_signoff(lines, expected):
    blocks = [(block(t, i, 10), len(t)) for i, t in enumerate(lines)]
    original = "".join(f"<p>{t}</p>" for t in lines)
    assert {f: v for f, (v, _) in agent_body_detail_proofs(blocks, original).items()} == expected
    assert not agent_body_detail_proofs(blocks, f"<blockquote>{original}</blockquote>")


@pytest.mark.parametrize(
    "variant",
    [
        "valid",
        "cost_column",
        "merged_columns",
        "second_shipment",
        "per_box",
        "multi_line",
        "history",
    ],
)
def test_quote_table_only_proves_the_unique_current_data_column(variant):
    def cell(index, text, row, col, *, width=1, height=1):
        return {
            "pointer": f"/tables/0/data/table_cells/{index}/text",
            "text": text,
            "cell": {
                "start_row_offset_idx": row,
                "end_row_offset_idx": row + height,
                "start_col_offset_idx": col,
                "end_col_offset_idx": col + width,
            },
        }

    head = [cell(i, text, 0, i) for i, text in enumerate(("数据", "成本", "渠道"))]
    summary = "LHR 4PALLETS,560.00KG,6.40CBM"
    if variant == "per_box":
        summary += " per carton"
    if variant == "multi_line":
        summary += "\n80 × 60 × 50 cm"
    cargo = cell(
        3,
        summary,
        1,
        1 if variant == "cost_column" else 0,
        width=2 if variant == "merged_columns" else 1,
        height=4,
    )
    blocks = [*head, cargo, cell(4, "rate quote", 2, 1)]
    if variant == "second_shipment":
        blocks.append(cell(5, "AMS 5CTNS,85KG,0.55CBM", 5, 0))
    result = list(quote_table_gap_evidence(blocks, lambda _: variant != "history"))
    if variant == "multi_line":
        assert [(f, v, u) for f, v, u, _ in result] == [("package_type", "pallet", None)]
    elif variant != "valid":
        assert result == []
    else:
        assert [(f, v, u) for f, v, u, _ in result] == [
            ("package_type", "pallet", None),
            ("line_volume", 6.4, "CBM"),
        ]
        assert all(
            [b["text"] for b, _, _ in proof] == ["数据", summary] for _, _, _, proof in result
        )


@pytest.mark.parametrize("variant", ["valid", "per_box", "second_volume", "split_scope", "quoted"])
def test_split_cargo_volume_needs_contiguous_current_context(variant):
    parts = ["Cargo details:", "24 cartons", "50x40x40 cm", "280 kg", "2,40 cbm"]
    if variant == "per_box":
        parts.append("per carton")
    elif variant == "second_volume":
        parts.append("3.80 cbm")
    elif variant == "split_scope":
        parts.insert(3, "Shipment 2:")
    blocks = [{"text": t, "pointer": f"/texts/{i}/text"} for i, t in enumerate(parts)]
    results = list(
        split_cargo_gap_evidence(
            blocks, lambda s: variant != "quoted" and s.startswith("Cargo details:")
        )
    )
    if variant == "valid":
        ((field, value, unit, proof),) = results
        assert (field, value, unit) == ("line_volume", 2.4, "CBM")
        assert [b["text"] for b, _, _ in proof] == ["Cargo details:", "24 cartons", "2,40 cbm"]
    else:
        assert results == []
