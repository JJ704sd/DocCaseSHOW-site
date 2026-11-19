"""Evidence and scope checks for object-specific candidate extraction."""

import sys
from copy import deepcopy
from pathlib import Path

import pytest
from local_web_agent_context import multi_shipment_material_request, natural_gap_material_request

from doccase_preprocessing.body_objects import body_objects
from doccase_preprocessing.object_agent import object_packet, validate_object_response
from local_web import Workbench


def row_fixture():
    return {
        "object_id": "row-A",
        "source_role": "current_body",
        "fields": {},
        "evidence_spans": [
            {
                "field": "line_net_weight",
                "position": "header",
                "pointer": "/table/header/net",
                "excerpt": "NET WEIGHT KG",
                "kind": "current_body",
                "row": 1,
                "column": 4,
            },
            {
                "field": "line_net_weight",
                "position": "value",
                "pointer": "/table/row/2/net",
                "excerpt": "13,5 kg",
                "kind": "current_body",
                "row": 2,
                "column": 4,
            },
            {
                "field": "line_gross_weight",
                "position": "header",
                "pointer": "/table/header/gross",
                "excerpt": "GROSS WEIGHT KG",
                "kind": "current_body",
                "row": 1,
                "column": 5,
            },
            {
                "field": "line_gross_weight",
                "position": "value",
                "pointer": "/table/row/2/gross",
                "excerpt": "20 kg",
                "kind": "current_body",
                "row": 2,
                "column": 5,
            },
        ],
    }


def test_missing_row_field_requires_matching_cell_header_and_unit():
    packet, bindings, problem = object_packet(row_fixture(), "cargo_line", lambda _: True)
    assert problem is None and len(packet["requirements"]) == 2
    req = packet["requirements"][0]
    candidate = {
        "object_id": "row-A",
        "field": "line_net_weight",
        "value": 13.5,
        "unit": "kgs",
        "evidence_ids": req["evidence_ids"],
    }
    updates, events = validate_object_response(packet, bindings, {"candidates": [candidate]}, {})
    assert updates["line_net_weight"]["value"] == 13.5
    assert updates["line_net_weight"]["unit"] == "kg"
    assert {s["row"] for s in updates["line_net_weight"]["sources"]} == {1, 2}
    assert events[0]["outcome"] == "accepted"
    for change, expected in (
        ({"object_id": "row-B"}, "object_or_level_mismatch"),
        ({"unit": "CBM"}, "semantic_or_unit_mismatch"),
        ({"value": 20}, "semantic_or_unit_mismatch"),
        ({"evidence_ids": req["evidence_ids"][:1]}, "evidence_binding_failed"),
        ({"evidence_ids": packet["requirements"][1]["evidence_ids"]}, "evidence_binding_failed"),
    ):
        updates, events = validate_object_response(
            packet, bindings, {"candidates": [{**candidate, **change}]}, {}
        )
        assert (
            not updates
            and events[0]["code"] == "evidence_not_supported"
            and events[0]["reason"] == expected
        )


def test_destination_and_role_boundaries_do_not_overwrite_known_values():
    obj = {
        "object_id": "s1",
        "source_role": "current_body",
        "direct_fields": {
            "ctns": {
                "value": 3,
                "unit": "carton",
                "sources": [
                    {
                        "pointer": "/texts/3",
                        "excerpt": "DATA: 3 CTNS / 12 KGS to LAX",
                        "kind": "current_body",
                    }
                ],
            }
        },
    }
    packet, bindings, _ = object_packet(obj, "shipment", lambda _: True)
    candidate = {
        "object_id": "s1",
        "field": "destination",
        "value": "lax",
        "unit": None,
        "evidence_ids": packet["requirements"][0]["evidence_ids"],
    }
    updates, events = validate_object_response(packet, bindings, {"candidates": [candidate]}, {})
    assert updates["destination"]["value"] == "LAX" and events[0]["outcome"] == "accepted"
    updates, events = validate_object_response(
        packet,
        bindings,
        {"candidates": [candidate]},
        {"destination": {"value": "NRT", "unit": None}},
    )
    assert not updates and events[0]["reason"] == "established_value_conflict"
    other = deepcopy(obj)
    other["source_role"] = "historical_quote"
    assert object_packet(other, "shipment", lambda _: True)[2] == "role_conflict"
    other = deepcopy(obj)
    other["direct_fields"]["ctns"]["sources"][0]["excerpt"] += " or LHR"
    packet, bindings, _ = object_packet(other, "shipment", lambda _: True)
    candidate["evidence_ids"] = packet["requirements"][0]["evidence_ids"]
    updates, events = validate_object_response(packet, bindings, {"candidates": [candidate]}, {})
    assert not updates and events[0]["reason"] == "conditional_or_alternative"
    assert object_packet(obj, "shipment", lambda _: False)[0] is None


def test_attachment_requires_same_row_identity_and_rejects_ambiguous_comma():
    row = row_fixture()
    row["source_role"] = "attachment"
    for source in row["evidence_spans"]:
        source["kind"] = "attachment"
    assert object_packet(row, "cargo_line", lambda _: True)[0] is None
    row["evidence_spans"].append(
        {
            "field": "line_net_weight",
            "position": "identity",
            "pointer": "/table/row/2/id",
            "excerpt": "item 2",
            "kind": "attachment",
        }
    )
    row["evidence_spans"][1]["excerpt"] = "1,234"
    packet, bindings, _ = object_packet(row, "cargo_line", lambda _: True)
    req = packet["requirements"][0]
    candidate = {
        "object_id": "row-A",
        "field": "line_net_weight",
        "value": 1.234,
        "unit": "kg",
        "evidence_ids": req["evidence_ids"],
    }
    updates, events = validate_object_response(packet, bindings, {"candidates": [candidate]}, {})
    assert not updates and events[0]["reason"] == "semantic_or_unit_mismatch"


def test_multi_shipment_material_request_does_not_mix_ticket_fields_or_repeat_known_contact():
    request = {
        "requirements": [
            {"field_name": name}
            for name in ("destination", "ctns", "line_volume", "contact_name", "signature_email")
        ],
        "evidence": [{"evidence_id": "E1", "text": "cargo"}],
    }
    baseline = [
        {"name": "contact_name", "status": "known"},
        {"name": "signature_email", "status": "uncertain"},
    ]
    narrowed = multi_shipment_material_request(request, baseline)
    assert [r["field_name"] for r in narrowed["requirements"]] == ["signature_email"]
    assert narrowed["evidence"] == request["evidence"]
    request["evidence"].extend(
        [
            {"evidence_id": "E2", "text": "name@example.invalid"},
            {"evidence_id": "E3", "text": "USD 150"},
        ]
    )
    assert [
        item["evidence_id"]
        for item in multi_shipment_material_request(request, baseline)["evidence"]
    ] == ["E1"]


def test_gap_request_keeps_only_matching_table_header_and_total_row():
    def cell(eid, text, row, column, table="/tables/0"):
        return {
            "evidence_id": eid,
            "text": text,
            "cell": {"table": table, "row": row, "column": column},
        }

    request = {
        "requirements": [{"field_name": "ctns"}, {"field_name": "line_volume"}],
        "evidence": [
            cell("E1", "CARTONS", 1, 2),
            cell("E2", "Quantity PCS", 1, 3),
            cell("E3", "Volume CBM", 1, 4),
            cell("E4", "TOTAL", 5, 1),
            cell("E5", "50", 5, 2),
            cell("E6", "300", 5, 3),
            cell("E7", "1.25", 5, 4),
            cell("E8", "100", 5, 2, "/tables/1"),
        ],
    }
    fields = [
        {"name": "ctns", "status": "uncertain", "value": None},
        {"name": "line_volume", "status": "known", "value": 1.25},
    ]
    narrowed = natural_gap_material_request(request, fields)
    assert [r["field_name"] for r in narrowed["requirements"]] == ["ctns"]
    assert [e["evidence_id"] for e in narrowed["evidence"]] == ["E1", "E4", "E5"]
    assert (
        natural_gap_material_request(request, [{"name": "ctns", "status": "conflict"}])[
            "requirements"
        ]
        == []
    )


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("line_volume", "0.38CBM"),
        ("ctns", "50CTNS"),
        ("package_count", "5PKGS"),
    ],
)
def test_gap_request_keeps_compact_units_without_mixing_shipments(field, text):
    request = {
        "requirements": [{"field_name": field}],
        "evidence": [{"evidence_id": "E1", "text": text}],
    }
    fields = [{"name": field, "status": "uncertain", "value": None}]
    assert natural_gap_material_request(request, fields)["evidence"] == request["evidence"]
    assert natural_gap_material_request(request, fields, multi_shipment=True)["requirements"] == []


def test_shipment_chargeable_weight_is_not_confused_with_actual_weight():
    obj = {
        "object_id": "ship",
        "source_role": "current_body",
        "direct_fields": {
            "ctns": {
                "value": 5,
                "unit": "carton",
                "sources": [
                    {
                        "pointer": "/texts/1",
                        "excerpt": "DATA: 5 CTNS / 100 KGS / C.W.: 120 KGS",
                        "kind": "current_body",
                    }
                ],
            },
            "shipment_actual_weight": {"value": 100, "unit": "kg", "sources": []},
        },
    }
    packet, bindings, _ = object_packet(obj, "shipment", lambda _: True)
    req = next(
        item for item in packet["requirements"] if item["field"] == "shipment_chargeable_weight"
    )
    candidate = {
        "object_id": "ship",
        "field": req["field"],
        "value": 120,
        "unit": "KGS",
        "evidence_ids": req["evidence_ids"],
    }
    updates, events = validate_object_response(packet, bindings, {"candidates": [candidate]}, {})
    assert updates[req["field"]]["value"] == 120 and events[0]["outcome"] == "accepted"
    candidate["value"] = 100
    updates, events = validate_object_response(packet, bindings, {"candidates": [candidate]}, {})
    assert not updates and events[0]["reason"] == "semantic_or_unit_mismatch"


def test_trade_term_is_delivered_locally_when_adjacent_to_the_same_shipment():
    lines = [
        "Shipment 1: AB123",
        "Cargo details: 7PLTS 1628.75KG 1.57CBM",
        "EXW",
        "Shipment 2: AB124",
        "Cargo details: 8PLTS/2396KG/6.32CBM",
        "FOB",
    ]
    document = {
        "texts": [{"text": text} for text in lines],
        "tables": [],
        "body": {"children": [{"$ref": f"#/texts/{i}"} for i in range(len(lines))]},
    }
    original = "".join(f"<p>{text}</p>" for text in lines)
    shipments = body_objects(
        document, original, email_sha="mail", material_sha="body", subject="AB-123-124"
    )["shipments"]
    assert len(shipments) == 2
    term = shipments[0]["direct_fields"]["trade_terms"]
    assert term["value"] == "EXW"
    assert {s["pointer"] for s in term["sources"]} == {"/texts/1/text", "/texts/2/text"}
    assert shipments[1]["direct_fields"]["trade_terms"]["value"] == "FOB"
    assert object_packet(shipments[0], "shipment", lambda _: True)[0] is None
    separated = ["Cargo details: 7PLTS 1628.75KG 1.57CBM", "unrelated statement", "EXW"]
    separated_doc = {
        "texts": [{"text": text} for text in separated],
        "tables": [],
        "body": {"children": [{"$ref": f"#/texts/{i}"} for i in range(len(separated))]},
    }
    separated_original = "".join(f"<p>{text}</p>" for text in separated)
    item = body_objects(separated_doc, separated_original, email_sha="m", material_sha="b")[
        "shipments"
    ][0]
    assert "trade_terms" not in item["direct_fields"]
    assert object_packet(item, "shipment", lambda _: True)[0] is None
    prefix_original = "<p>Cargo details: 7PLTS 1628.75KG 1.57CBM</p><p>EXWORKS</p>"
    prefix_doc = {
        "texts": [{"text": lines[1]}, {"text": "EXW"}],
        "tables": [],
        "body": {"children": [{"$ref": "#/texts/0"}, {"$ref": "#/texts/1"}]},
    }
    item = body_objects(prefix_doc, prefix_original, email_sha="m", material_sha="b")["shipments"][
        0
    ]
    assert "trade_terms" not in item["direct_fields"]
    quote_original = (
        "<p>Cargo details: 7PLTS 1628.75KG 1.57CBM</p><blockquote>"
        "<p>From: old Sent: yesterday To: us</p><p>EXW</p></blockquote>"
    )
    item = body_objects(prefix_doc, quote_original, email_sha="m", material_sha="b")["shipments"][0]
    assert "trade_terms" not in item["direct_fields"]


def test_workbench_object_agent_does_not_request_a_locally_proved_trade_term(tmp_path, monkeypatch):
    lines = ["Cargo details: 7PLTS 1628.75KG 1.57CBM", "EXW"]
    document = {
        "texts": [{"text": text} for text in lines],
        "tables": [],
        "body": {"children": [{"$ref": f"#/texts/{i}"} for i in range(len(lines))]},
    }
    observed = body_objects(
        document, "".join(f"<p>{text}</p>" for text in lines), email_sha="mail", material_sha="body"
    )
    packets = []

    class Adapter:
        def extract_material(self, request, *, timeout_seconds):
            raise AssertionError("not used by this replay")

        def extract_objects(self, request, *, timeout_seconds):
            packets.append(request)
            return {"candidates": []}

    app = Workbench(tmp_path, Path(sys.executable), tmp_path, extraction_agent=Adapter())
    app.jobs["mail"] = {
        "details": {"0": {"body_shipments": observed["shipments"], "packing_rows": []}}
    }
    monkeypatch.setattr(
        app, "main_result", lambda *args, **kwargs: (None, None, {"agent": {"events": []}})
    )
    try:
        app._run_group_agent(
            ("mail",), [("mail", "0", "body", (None, None, None, None, None, None), set())], {}
        )
        assert packets == []
        event = app.group_runs[("mail",)]["object_events"][0]
        assert event["outcome"] == "unselected" and event["reason"] == "no_selected_evidence"
    finally:
        app.pool.shutdown()
        app.group_pool.shutdown()


def test_multi_shipment_rejects_citation_that_was_not_sent(tmp_path, monkeypatch):
    class Adapter:
        def extract_material(self, request, *, timeout_seconds):
            assert [item["evidence_id"] for item in request["evidence"]] == ["E1"]
            return {
                "candidates": [
                    {"requirement_id": "CONTACT", "value": "Example Name", "evidence_ids": ["E2"]}
                ]
            }

    app = Workbench(tmp_path, Path(sys.executable), tmp_path, extraction_agent=Adapter())
    app.jobs["e"] = {
        "details": {
            "0": {
                "body_shipments": [{"source_role": "current_body"}, {"source_role": "current_body"}]
            }
        }
    }

    def fake_result(*args, replay, **kwargs):
        response = replay[1](
            {
                "requirements": [{"requirement_id": "CONTACT", "field_name": "contact_name"}],
                "evidence": [
                    {"evidence_id": "E1", "text": "Contact name: Example Name"},
                    {"evidence_id": "E2", "text": "name@example.invalid"},
                ],
            },
            timeout_seconds=1,
        )
        assert response == {"candidates": []}
        return (
            None,
            None,
            {"agent": {"events": [{"stage": "material_agent", "outcome": "received"}]}},
        )

    monkeypatch.setattr(app, "main_result", fake_result)
    try:
        app._run_group_agent(
            ("e",),
            [("e", "0", "body", (None, None, None, None, None, None), set())],
            {("e", "0"): [{"name": "contact_name", "status": "uncertain", "value": None}]},
        )
        events = app.group_runs[("e",)]["validation"][0]["events"]
        assert any(event.get("reason_detail") == "evidence_not_sent" for event in events)
    finally:
        app.pool.shutdown()
        app.group_pool.shutdown()


def test_workbench_applies_validated_object_result_without_overwriting_rules(tmp_path, monkeypatch):
    class Adapter:
        def extract_material(self, request, *, timeout_seconds):
            raise AssertionError("not called by this replay")

        def extract_objects(self, request, *, timeout_seconds):
            return {
                "candidates": [
                    {
                        "object_id": request["object_id"],
                        "field": "destination",
                        "value": "LAX",
                        "unit": None,
                        "evidence_ids": request["requirements"][0]["evidence_ids"],
                    }
                ]
            }

    app = Workbench(tmp_path, Path(sys.executable), tmp_path, extraction_agent=Adapter())
    obj = {
        "object_id": "ship-1",
        "source_role": "current_body",
        "location": {"pointer": "/texts/0"},
        "association": {"status": "local_segment"},
        "direct_fields": {
            "ctns": {
                "value": 3,
                "unit": "carton",
                "status": "known",
                "sources": [
                    {
                        "pointer": "/texts/0",
                        "excerpt": "DATA: 3 CTNS / 12 KGS to LAX",
                        "kind": "current_body",
                    }
                ],
            }
        },
    }
    job = {
        "id": "email1",
        "filename": "mail.eml",
        "is_email": True,
        "state": "ready",
        "route": "deepseek-group",
        "parse_scope": "all",
        "reason": None,
        "metadata": [],
        "materials": [{"id": "0", "kind": "body", "format": "html", "filename": "body.html"}],
        "origins": {},
        "replays": {},
        "html_previews": {},
        "inline_images": {},
        "details": {
            "0": {
                "fields": [],
                "packing_rows": [],
                "body_shipments": [obj],
                "chain": {"read": {"state": "success"}, "preprocessing": {"material_id": "m"}},
            }
        },
    }
    app.jobs["email1"] = job
    monkeypatch.setattr(
        app, "main_result", lambda *args, **kwargs: (None, None, {"agent": {"events": []}})
    )
    try:
        app._run_group_agent(
            ("email1",), [("email1", "0", "body", (None, None, None, None, None, None), set())], {}
        )
        result = app.group_review("email1", ["email1"], confirmed_first_email_id="email1")
        fields = result["structured_result"]["shipments"][0]["direct_fields"]
        assert fields["ctns"]["value"] == 3
        assert fields["destination"]["value"] == "LAX"
        assert fields["destination"]["sources"][0]["email_ref_id"] == "email1"
        assert result["agent"]["object_events"][0]["events"][0]["outcome"] == "accepted"
    finally:
        app.pool.shutdown()
        app.group_pool.shutdown()
