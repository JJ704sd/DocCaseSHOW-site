"""Proof boundaries for two saved same-value Agent rejections."""

import pytest

import doccase_preprocessing.main_channel as main


def evidence_case(monkeypatch, paragraphs):
    blocks = [{"pointer": f"/texts/{i}/text", "text": text} for i, text in enumerate(paragraphs)]
    monkeypatch.setattr(main, "_blocks", lambda _execution: ({}, blocks))
    raw = "".join(f"<p>{text}</p>" for text in paragraphs).encode()
    monkeypatch.setattr(main, "_read_content", lambda *_: raw)
    chosen = [
        next(b for b in blocks if b["text"] == text) for text in ("Cargo details", "CBM: 4,581")
    ]
    sources = {
        f"s{i}": {
            "kind": "current_body",
            "material_id": "m1",
            "excerpt": b["text"],
            "locator": {"json_pointer": b["pointer"], "char_start": 0, "char_end": len(b["text"])},
        }
        for i, b in enumerate(chosen)
    }
    bindings = {sid: {"artifact_id": "e1"} for sid in sources}
    candidate = {
        "field_name": "line_volume",
        "object_id": "m1/document",
        "field": {"value": 4.581, "unit": "CBM", "source_ids": list(sources)},
    }
    profile = {
        "requirements": [
            {
                "field_name": "line_volume",
                "material_id": "m1",
                "object_selector": "m1/document",
                "source_kind": "current_body",
            }
        ]
    }
    records = {"m1": {"kind": "body", "role": "current", "content": {}}}
    return candidate, sources, bindings, {"e1": {}}, profile, records, "."


def test_general_cargo_case_normalizes_only_equivalent_value():
    assert (
        main._normalize_material_value("cargo_type", "General Cargo", "text_field")
        == "general cargo"
    )
    assert (
        main._normalize_material_value("cargo_type", "dangerous goods", "text_field")
        == "dangerous goods"
    )


@pytest.mark.parametrize(
    ("paragraphs", "expected"),
    [
        (("Cargo details", "CBM: 4,581"), True),
        (("Cargo details", "unrelated line", "CBM: 4,581"), False),
        (("Cargo details", "CBM: 4,581", "CBM: 6,400"), False),
        (("----- Original Message -----", "Cargo details", "CBM: 4,581"), False),
    ],
)
def test_volume_heading_proof_requires_current_adjacent_unambiguous_blocks(
    monkeypatch, paragraphs, expected
):
    args = evidence_case(monkeypatch, paragraphs)
    assert main._cargo_labelled_volume_supported(*args) is expected


def test_volume_heading_proof_rejects_another_artifact(monkeypatch):
    args = list(evidence_case(monkeypatch, ("Cargo details", "CBM: 4,581")))
    args[2]["s1"]["artifact_id"] = "other"
    assert not main._cargo_labelled_volume_supported(*args)


def test_volume_heading_proof_rejects_per_carton_qualifier(monkeypatch):
    args = list(evidence_case(monkeypatch, ("Cargo details", "CBM: 4,581")))
    text = "CBM: 4,581 per carton"
    blocks = [
        {"pointer": "/texts/0/text", "text": "Cargo details"},
        {"pointer": "/texts/1/text", "text": text},
    ]
    monkeypatch.setattr(main, "_blocks", lambda _execution: ({}, blocks))
    monkeypatch.setattr(
        main, "_read_content", lambda *_: f"<p>Cargo details</p><p>{text}</p>".encode()
    )
    args[1]["s1"]["locator"]["char_end"] = len(text)
    args[1]["s1"]["excerpt"] = text
    assert not main._cargo_labelled_volume_supported(*args)
