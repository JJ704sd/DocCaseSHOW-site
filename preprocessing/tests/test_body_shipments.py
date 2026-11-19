"""Body shipment delivery through the workbench using a fixed Docling READ."""

import hashlib
import json
import sys
import time
from email.message import EmailMessage
from pathlib import Path

import p02_local_read as p02

from doccase_preprocessing.body_objects import body_objects
from local_web import Workbench, body_read_objects


def test_workbench_delivers_separate_body_shipments_when_markdown_unavailable(
    tmp_path, monkeypatch
):
    lines = [
        "FOB SFO-LAX.",
        "DATA:12CTNS/240KGS/3.125CBM V.W:480.5",
        "DATA: 11 CTNS/173KGS/1.7, CW:275KGS",
        "DATA: 28 CTNS 510 KGS 4,84 CBM 1070 CW",
    ]
    message = EmailMessage()
    message["From"] = "sender@example.invalid"
    message["To"] = "receiver@example.invalid"
    message.set_content("".join(f"<p>{line}</p>" for line in lines), subtype="html")
    versions = p02.configurations()["docling", "html"]["versions"]

    def worker(request, **_options):
        if request["operation"] == "probe":
            return versions
        if request["operation"] == "export_markdown":
            return {"state": "failure", "reason": "no_mapped_table"}
        assert request["operation"] == "convert"
        document = {
            "texts": [{"text": value} for value in lines],
            "tables": [],
            "body": {"children": [{"$ref": f"#/texts/{i}"} for i in range(len(lines))]},
        }
        raw = json.dumps(document).encode()
        target = Path(request["json_path"])
        target.write_bytes(raw)
        return {
            "conversion_status": "success",
            "versions": versions,
            "errors": [],
            "json_path": str(target),
            "json_sha256": hashlib.sha256(raw).hexdigest(),
        }

    monkeypatch.setattr(p02, "worker", worker)
    app = Workbench(tmp_path, Path(sys.executable), tmp_path)
    try:
        email_id = app.submit("mail.eml", message.as_bytes(), route="docling")
        deadline = time.monotonic() + 15
        while app.jobs[email_id]["state"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.05)
        assert app.jobs[email_id]["state"] in {"ready", "partial"}
        result = app.group_review(email_id, [email_id], confirmed_first_email_id=email_id)[
            "structured_result"
        ]
        shipments = result["shipments"]
        assert len(shipments) == 3
        assert [s["direct_fields"]["ctns"]["value"] for s in shipments] == [12, 11, 28]
        assert [s["direct_fields"]["shipment_actual_weight"]["value"] for s in shipments] == [
            240,
            173,
            510,
        ]
        assert [s["direct_fields"]["shipment_volume"]["value"] for s in shipments] == [
            3.125,
            1.7,
            4.84,
        ]
        assert all(s["direct_fields"]["destination"]["value"] == "LAX" for s in shipments)
        assert shipments[1]["direct_fields"]["shipment_chargeable_weight"]["value"] == 275
        assert all(
            s["role"] == "first_inquiry" and s["location"]["pointer"].startswith("/texts/")
            for s in shipments
        )
        assert result["cargo_lines"] == []
    finally:
        app.pool.shutdown()
        app.group_pool.shutdown()


def test_quoted_table_preserves_row_provenance_and_never_infers_box_dimensions():
    headings = ("MEASUREMENTS CM", "NET WEIGHT KG", "GROSS WEIGHT KG", "VOLUME M3")
    values = (("80", "80", "60", "10", "12", "0.55"), ("", "", "", "10", "12", "0.55"))
    cells = []
    for col, text in ((0, headings[0]), (3, headings[1]), (4, headings[2]), (5, headings[3])):
        cells.append(
            {
                "text": text,
                "start_row_offset_idx": 0,
                "end_row_offset_idx": 2,
                "start_col_offset_idx": col,
                "end_col_offset_idx": col + 1,
            }
        )
    for row, data in enumerate(values, 2):
        for col, text in enumerate(data):
            cells.append(
                {
                    "text": text,
                    "start_row_offset_idx": row,
                    "end_row_offset_idx": row + 1,
                    "start_col_offset_idx": col,
                    "end_col_offset_idx": col + 1,
                }
            )
    document = {
        "texts": [{"text": "AB123"}],
        "groups": [],
        "tables": [{"data": {"table_cells": cells}}],
        "body": {"children": [{"$ref": "#/texts/0"}, {"$ref": "#/tables/0"}]},
    }
    original = (
        "<p>Current message</p><p>From: old@example.invalid Sent: yesterday To: us</p>"
        "<p>AB123</p><table><tr><td>MEASUREMENTS CM</td><td>NET WEIGHT KG</td>"
        "<td>GROSS WEIGHT KG</td><td>VOLUME M3</td></tr>"
        "<tr><td>80</td><td>80</td><td>60</td><td>10</td><td>12</td><td>0.55</td></tr></table>"
    )
    result = body_objects(
        document, original, email_sha="email-hash", material_sha="body-hash", subject="AB-123-124"
    )
    assert len(result["shipments"]) == len(result["packing_rows"]) == 1
    shipment, line = result["shipments"][0], result["packing_rows"][0]
    assert shipment["source_role"] == line["source_role"] == "historical_quote"
    assert shipment["association"]["status"] == "verified"
    assert line["shipment_id"] == shipment["object_id"]
    assert {k: v["value"] for k, v in line["fields"].items()} == {
        "line_net_weight": 10,
        "line_gross_weight": 12,
        "line_volume": 0.55,
    }
    assert all(
        "length" not in name and "width" not in name and "height" not in name
        for name in line["fields"]
    )
    assert len(line["fields"]["line_net_weight"]["sources"]) == 2
    assert (
        body_objects(
            document,
            original,
            email_sha="email-hash",
            material_sha="body-hash",
            subject="AB-123-124",
        )["packing_rows"][0]["object_id"]
        == line["object_id"]
    )


def test_generic_box_is_not_a_carton_and_missing_volume_stays_missing():
    text = "DATA: 5 BOXES / 100 KGS"
    document = {
        "texts": [{"text": text}],
        "tables": [],
        "body": {"children": [{"$ref": "#/texts/0"}]},
    }
    result = body_objects(document, f"<p>{text}</p>", email_sha="email", material_sha="body")
    fields = result["shipments"][0]["direct_fields"]
    assert fields["package_count"]["value"] == 5 and fields["package_count"]["unit"] == "box"
    assert "ctns" not in fields and "shipment_volume" not in fields
    assert fields["shipment_actual_weight"]["value"] == 100


def test_alternative_fractional_and_duplicate_quote_are_not_current_facts():
    for text in (
        "DATA: 5.5 CTNS / 100 KGS",
        "DATA: 5 CTNS / 100 KGS or 200 KGS",
        "DATA: 7 PLTS / 20 CTNS / 100 KGS",
    ):
        document = {
            "texts": [{"text": text}],
            "tables": [],
            "body": {"children": [{"$ref": "#/texts/0"}]},
        }
        assert (
            body_objects(document, f"<p>{text}</p>", email_sha="a", material_sha="b")["shipments"]
            == []
        )
    text = "DATA: 5 CTNS / 100 KGS"
    original = (
        f"<p>{text}</p><blockquote><p>From: x Sent: today To: y</p><p>{text}</p></blockquote>"
    )
    document = {
        "texts": [{"text": text}],
        "tables": [],
        "body": {"children": [{"$ref": "#/texts/0"}]},
    }
    assert body_objects(document, original, email_sha="a", material_sha="b")["shipments"] == []


def test_body_read_requires_same_material_hash_before_parsing():
    read = {
        "inventory": [
            {
                "material_id": "body",
                "read_state": "success",
                "intake_source_bound": True,
                "representation_sha256": "other",
                "execution_id": "e1",
            }
        ],
        "executions": [],
    }
    record = {"material_id": "body", "content": {"sha256": "expected"}}
    assert body_read_objects(read, record, "DATA: 3 CTNS / 12 KGS", email_sha="mail") == {
        "shipments": [],
        "packing_rows": [],
    }


def test_three_pallet_summaries_reach_workbench_as_independent_reference_shipments(tmp_path):
    lines = [
        "Shipment 1: AB123",
        "Cargo details: 7PLTS 1628.75KG 1.57CBM 1:2000",
        "EXW",
        "Shipment 2: AB124",
        "Cargo details: 8 PALLETS/2396KG/6.32CBM 1:500",
        "EXW",
        "Shipment 3: AB125",
        "Cargo details: 12PLT/3146KG/17.48CBM",
        "EXW",
    ]
    document = {
        "texts": [{"text": text} for text in lines],
        "tables": [],
        "body": {"children": [{"$ref": f"#/texts/{i}"} for i in range(len(lines))]},
    }
    original = "".join(f"<p>{line}</p>" for line in lines)
    observed = body_objects(
        document,
        original,
        email_sha="email-hash",
        material_sha="body-hash",
        subject="AB-123-124-125",
    )
    app = Workbench(tmp_path, Path(sys.executable), tmp_path)
    app.jobs["email1"] = {
        "id": "email1",
        "filename": "reply.eml",
        "is_email": True,
        "state": "ready",
        "route": "docling",
        "parse_scope": "all",
        "reason": None,
        "email_sha256": "email-hash",
        "metadata": [{"email_ref_id": "email1", "subject": "AB-123-124-125"}],
        "materials": [{"id": "0", "kind": "body", "format": "html", "filename": "body.html"}],
        "origins": {},
        "replays": {},
        "html_previews": {},
        "inline_images": {},
        "details": {
            "0": {
                "fields": [],
                "packing_rows": observed["packing_rows"],
                "body_shipments": observed["shipments"],
                "chain": {
                    "read": {"state": "success"},
                    "preprocessing": {"material_id": "body-read"},
                },
            }
        },
    }
    try:
        result = app.group_review("email1", ["email1"])["structured_result"]
        assert result["first_inquiry"]["email_ref_id"] is None
        assert len(result["shipments"]) == 3 and result["cargo_lines"] == []
        assert [s["association"]["reference"] for s in result["shipments"]] == [
            "AB123",
            "AB124",
            "AB125",
        ]
        assert len({s["object_id"] for s in result["shipments"]}) == 3
        for item, (count, weight, volume) in zip(
            result["shipments"],
            [(7, 1628.75, 1.57), (8, 2396, 6.32), (12, 3146, 17.48)],
            strict=True,
        ):
            fields = item["direct_fields"]
            assert "evidence_spans" not in item
            assert item["role"] == "reference" and item["source_role"] == "current_body"
            assert (fields["package_count"]["value"], fields["package_count"]["unit"]) == (
                count,
                "pallet",
            )
            assert fields["package_type"]["value"] == "pallet"
            assert fields["shipment_actual_weight"]["value"] == weight
            assert fields["shipment_volume"]["value"] == volume
            assert fields["trade_terms"]["value"] == "EXW"
            assert "ctns" not in fields and "shipment_chargeable_weight" not in fields
            assert all(
                s["email_ref_id"] == "email1" and s["material_id"] == "0"
                for field in fields.values()
                for s in field["sources"]
            )
            term_sources = fields["trade_terms"]["sources"]
            assert term_sources[0]["pointer"] == item["location"]["pointer"]
            assert (
                term_sources[1]["pointer"] != item["location"]["pointer"]
                and term_sources[1]["excerpt"] == "EXW"
            )
    finally:
        app.pool.shutdown()
        app.group_pool.shutdown()
