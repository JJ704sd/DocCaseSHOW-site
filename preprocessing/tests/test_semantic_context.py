from doccase_preprocessing.agent_gap_evidence import group_declaration_evidence
from doccase_preprocessing.extraction_agent import current_body_text_check
from doccase_preprocessing.inquiry_details import agent_body_detail_proofs
from doccase_preprocessing.semantic_context import semantic_context_spans


def text_blocks(*texts):
    return [{"pointer": f"/texts/{i}/text", "text": text} for i, text in enumerate(texts)]


def test_weight_gap_gets_context_without_crossing_contact_or_history():
    blocks = text_blocks(
        "Cargo details:",
        "2 packages",
        "Weight: 245 kg",
        "Chargeable weight: 285 kg",
        "TEL: private",
        "Weight: 999 kg",
    )

    def current(s):
        return s in "Cargo details: 2 packages Weight: 245 kg Chargeable weight: 285 kg"

    result = semantic_context_spans(
        blocks, current, lambda s: "private" not in s, {"shipment_gross_weight"}
    )
    assert [item[3] for item in result["shipment_gross_weight"]] == [
        "Cargo details:",
        "2 packages",
        "Weight: 245 kg",
        "Chargeable weight: 285 kg",
    ]
    assert [item[0]["pointer"] for item in result["shipment_gross_weight"]] == [
        f"/texts/{i}/text" for i in range(4)
    ]


def test_table_context_keeps_one_table_and_coordinate_pair():
    def cell(table, row, col, text):
        return {
            "pointer": f"/tables/{table}/data/table_cells/{row * 3 + col}/text",
            "text": text,
            "cell": {
                "start_row_offset_idx": row,
                "end_row_offset_idx": row + 1,
                "start_col_offset_idx": col,
                "end_col_offset_idx": col + 1,
            },
        }

    blocks = [cell(0, 0, 0, "Gross weight"), cell(0, 0, 1, "245 kg"), cell(1, 0, 0, "285 kg")]
    result = semantic_context_spans(
        blocks, lambda _: True, lambda _: True, {"shipment_gross_weight"}
    )
    assert [x[3] for x in result["shipment_gross_weight"]] == ["Gross weight", "245 kg"]


def test_no_context_is_opened_for_an_unrequested_field_or_noncurrent_line():
    blocks = text_blocks("Weight: 285 kg")
    assert semantic_context_spans(blocks, lambda _: True, lambda _: True, {"signature_email"}) == {}
    assert (
        semantic_context_spans(blocks, lambda _: False, lambda _: True, {"shipment_gross_weight"})
        == {}
    )


def test_split_ready_and_negative_remark_use_current_adjacent_read_lines():
    lines = (
        "Cargo ready date:",
        "tomorrow afternoon",
        "Partial shipments:",
        "not allowed",
        "-----Original Message-----",
        "Cargo ready date:",
        "2099-12-31",
    )
    original = "\n".join(lines)
    blocks = text_blocks(*lines)
    verify = current_body_text_check(original)
    spans = semantic_context_spans(
        blocks, verify, lambda _: True, {"cargo_ready_time", "business_remarks"}
    )
    assert [item[3] for item in spans["cargo_ready_time"]][:2] == list(lines[:2])
    assert all(item[3] != lines[-1] for item in spans["cargo_ready_time"])
    proof = agent_body_detail_proofs([(b, len(b["text"])) for b in blocks], original)
    assert proof["cargo_ready_time"][0] == "tomorrow afternoon"
    assert proof["business_remarks"][0] == "Partial shipments: not allowed"
    assert [b["pointer"] for b, _, _ in proof["business_remarks"][1]] == [
        "/texts/3/text",
        "/texts/2/text",
    ]


def test_current_multilingual_declarations_abstain_on_quoted_or_conflicting_fields():
    lines = (
        "Destino:",
        "LHR",
        "Incoterms: FCA",
        "Carga General",
        "Volumen por caja: 0,65 m³",
        "Cargo details: 2PLTS 90KG 1.2CBM",
        "-----Original Message-----",
        "Incoterms: FOB",
    )

    def extract(lines):
        current = current_body_text_check("\n".join(lines))
        return {
            field: (value, len(proof))
            for field, value, _, proof in group_declaration_evidence(text_blocks(*lines), current)
        }

    assert extract(lines) == {
        "destination": ("LHR", 2),
        "trade_terms": ("FCA", 1),
        "cargo_type": ("general cargo", 2),
        "package_type": ("pallet", 1),
    }
    assert (
        extract(
            (
                "Rates are based on",
                "Cargo type: General Cargo",
                "Package type: 1 Package",
                "Destination: LHR or AMS",
                "Terms: FCA",
                "Terms: FOB",
            )
        )
        == {}
    )
    assert "package_type" not in extract(
        ("Cargo details: 2PLTS 90KG 1.2CBM", "Cargo details: 3CTNS 100KG 0.8CBM")
    )
