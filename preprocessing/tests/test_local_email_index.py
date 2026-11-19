"""Exercise local index discovery and MIME selection through the HTTP entry."""

import copy
import hashlib
import json
import threading
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from http.server import HTTPServer
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zipfile import ZipFile

import local_parse_server as app
import p02_local_read as p02
import pytest
from pypdf import PdfWriter

from doccase_preprocessing.main_channel import _assess, read_result


def _pdf(encrypted=False):
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    if encrypted:
        writer.encrypt("test-password")
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


def _email(pdf, filenames, body="See the attached packing list."):
    message = EmailMessage()
    message["Subject"] = "Synthetic index sample"
    message["From"] = "sender@example.invalid"
    message["To"] = "receiver@example.invalid"
    message.set_content(body)
    for filename in filenames:
        message.add_attachment(pdf, maintype="application", subtype="pdf", filename=filename)
    return message.as_bytes()


def _inline_email(pdf):
    message = EmailMessage()
    message["Subject"] = "Synthetic named inline PDF"
    message["From"] = "sender@example.invalid"
    message["To"] = "receiver@example.invalid"
    message.set_content("See the inline files.")
    for filename, cid in (
        ("inline-a.pdf", "<a@example.invalid>"),
        ("inline-b.pdf", "<b@example.invalid>"),
    ):
        message.add_attachment(
            pdf,
            maintype="application",
            subtype="pdf",
            filename=filename,
            disposition="inline",
            cid=cid,
        )
    message.add_attachment(
        pdf,
        maintype="application",
        subtype="pdf",
        filename="inline-no-cid.pdf",
        disposition="inline",
    )
    message.add_attachment(
        pdf,
        maintype="application",
        subtype="pdf",
        disposition="inline",
        cid="<unnamed@example.invalid>",
    )
    message.add_attachment(
        b"note", maintype="text", subtype="plain", filename="notes.txt", disposition="inline"
    )
    return message.as_bytes()


@pytest.fixture
def indexed_server(tmp_path, monkeypatch):
    archive_path = tmp_path / "mail.zip"
    pdf = _pdf()
    first = _email(
        pdf,
        ["first.pdf", "copy.pdf"],
        "Total Packages: 11 packages\nTotal Gross Weight: 510 kg\nTotal Volume: 1.39 CBM\n"
        "Incoterm: FCA Seattle\nDestination: LAX, USA\n"
        "Cargo ready date: 2099-12-01\nContact person: Alex Chen\n"
        "Chargeable weight: 1100 kg\n"
        "From: earlier@example.invalid\nIncoterm: EXW\nDestination: Osaka",
    )
    second = _email(pdf, ["second.pdf"])
    inline = _inline_email(pdf)
    encrypted = _pdf(encrypted=True)
    blocked = _email(encrypted, ["encrypted.pdf"])
    blocked_inline_message = EmailMessage()
    blocked_inline_message["Subject"] = "Encrypted inline PDF"
    blocked_inline_message.set_content("See the PDF.")
    blocked_inline_message.add_attachment(
        encrypted,
        maintype="application",
        subtype="pdf",
        filename="encrypted-inline.pdf",
        disposition="inline",
    )
    with ZipFile(archive_path, "w") as archive:
        archive.writestr("one.eml", first)
        archive.writestr("two.eml", second)
        archive.writestr("inline.eml", inline)
        archive.writestr("blocked.eml", blocked)
        archive.writestr("blocked-inline.eml", blocked_inline_message.as_bytes())
    index_path = tmp_path / "index.json"
    inventory = {
        "archives": [{"id": "test", "path": str(archive_path)}],
        "items": [
            {
                "sha256": hashlib.sha256(pdf).hexdigest(),
                "extension": ".pdf",
                "size": len(pdf),
                "mime_refs": [
                    {
                        "archive": "test",
                        "eml": "one.eml",
                        "part_index": 2,
                        "filename": "first.pdf",
                        "disposition": "attachment",
                    },
                    {
                        "archive": "test",
                        "eml": "one.eml",
                        "part_index": 3,
                        "filename": "copy.pdf",
                        "disposition": "attachment",
                    },
                    {
                        "archive": "test",
                        "eml": "two.eml",
                        "part_index": 2,
                        "filename": "second.pdf",
                        "disposition": "attachment",
                    },
                    {
                        "archive": "test",
                        "eml": "inline.eml",
                        "part_index": 2,
                        "filename": "inline-a.pdf",
                        "disposition": "inline",
                        "content_id": "<a@example.invalid>",
                    },
                    {
                        "archive": "test",
                        "eml": "inline.eml",
                        "part_index": 3,
                        "filename": "inline-b.pdf",
                        "disposition": "inline",
                        "content_id": "<b@example.invalid>",
                    },
                    {
                        "archive": "test",
                        "eml": "inline.eml",
                        "part_index": 4,
                        "filename": "inline-no-cid.pdf",
                        "disposition": "inline",
                        "content_id": None,
                    },
                    {
                        "archive": "test",
                        "eml": "inline.eml",
                        "part_index": 5,
                        "filename": None,
                        "disposition": "inline",
                        "content_id": "<unnamed@example.invalid>",
                    },
                ],
            },
            {
                "sha256": hashlib.sha256(b"note").hexdigest(),
                "extension": ".txt",
                "size": len(b"note"),
                "mime_refs": [
                    {
                        "archive": "test",
                        "eml": "inline.eml",
                        "part_index": 6,
                        "filename": "notes.txt",
                        "disposition": "inline",
                    }
                ],
            },
            {
                "sha256": hashlib.sha256(encrypted).hexdigest(),
                "extension": ".pdf",
                "size": len(encrypted),
                "mime_refs": [
                    {
                        "archive": "test",
                        "eml": "blocked.eml",
                        "part_index": 2,
                        "filename": "encrypted.pdf",
                        "disposition": "attachment",
                    },
                    {
                        "archive": "test",
                        "eml": "blocked-inline.eml",
                        "part_index": 2,
                        "filename": "encrypted-inline.pdf",
                        "disposition": "inline",
                    },
                ],
            },
        ],
    }
    index_path.write_text(json.dumps(inventory), encoding="utf-8")
    cache = tmp_path / "cache"
    cache.mkdir()
    versions = p02.configurations(cache_path=cache)[("docling", "pdf")]["versions"]

    def worker(request, *, python=None, model_cache=None):
        if request["operation"] == "probe":
            return versions
        texts = []
        for index, text in enumerate(
            ["1 carton", "Total gross weight 8 kg", "Total volume 0.02 CBM"]
        ):
            texts.append(
                {
                    "text": text,
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {
                                "l": 10,
                                "r": 80,
                                "t": 100 - index * 10,
                                "b": 95 - index * 10,
                                "coord_origin": "BOTTOMLEFT",
                            },
                            "charspan": [0, len(text)],
                        }
                    ],
                }
            )
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

    monkeypatch.setattr(p02, "worker", worker)
    parser = app.LocalParser(tmp_path, tmp_path / "runs", tmp_path / "python", cache, index_path)
    server = HTTPServer(("127.0.0.1", 0), app.make_handler(parser))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", archive_path, inventory
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _request(base, path, value=None):
    body = None if value is None else json.dumps(value).encode("utf-8")
    request = Request(
        base + path,
        data=body,
        headers={"Content-Type": "application/json"} if body else {},
        method="GET" if body is None else "POST",
    )
    try:
        response = urlopen(request, timeout=30)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response)


def _indexed_request(email, attachment):
    return {
        "email_id": email["email_id"],
        "attachment_id": attachment["attachment_id"],
        "profile_id": "indexed-pdf-document",
    }


def test_indexed_current_body_http_returns_fields_and_original_identity(indexed_server):
    base, archive_path, _ = indexed_server
    _, result = _request(base, "/local/index?q=one.eml")
    email = result["emails"][0]
    request = {"email_id": email["email_id"], "profile_id": "local-current-body-totals"}
    status, response = _request(base, "/local/parse", request)
    assert status == 200 and response["status"] == "partial"
    with ZipFile(archive_path) as archive:
        digest = hashlib.sha256(archive.read("one.eml")).hexdigest()
    assert response["indexed_source"]["email_sha256"] == digest
    assert response["indexed_source"]["email_id"] == email["email_id"]
    assert "attachment_id" not in response["indexed_source"]
    metadata = response["email_metadata"]
    assert metadata["scope"] == "single_email_header"
    assert metadata["fields"]["subject"]["value"] == "Synthetic index sample"
    assert metadata["fields"]["from_addr"]["value"] == "sender@example.invalid"
    assert metadata["fields"]["to_addrs"]["value"] == ["receiver@example.invalid"]
    assert metadata["fields"]["cc_addrs"]["status"] == "uncertain"
    assert metadata["fields"]["sent_at"]["value"] is None
    source = metadata["fields"]["subject"]["source"]
    raw_manifest = Path(source["ref"]).read_bytes()
    assert hashlib.sha256(raw_manifest).hexdigest() == source["sha256"]
    assert (
        json.loads(raw_manifest)["emails"][0]["subject"] == metadata["fields"]["subject"]["value"]
    )
    assert source["json_pointer"] == "/emails/0/subject"
    fields = {item["field_name"]: item for item in response["fields"]}
    assert [
        (fields[name]["value"], fields[name]["unit"], fields[name]["status"])
        for name in ("package_count", "line_gross_weight", "line_volume", "ctns")
    ] == [
        (11, None, "known"),
        (510, "kg", "known"),
        (1.39, "CBM", "known"),
        (None, None, "uncertain"),
    ]
    assert all(
        fields[name]["sources"] for name in ("package_count", "line_gross_weight", "line_volume")
    )
    assert all(
        item["read_state"] == "not_selected"
        for item in response["materials"]
        if item["kind"] == "attachment"
    )
    assert _request(base, "/local/parse", {**request, "attachment_id": "unexpected"})[0] == 400
    assert _request(base, "/local/parse", {**request, "email_id": "unknown"})[0] == 400


def test_inquiry_profile_reads_labeled_current_body_fields_with_evidence(indexed_server):
    base, _, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    request = {
        "email_id": found["emails"][0]["email_id"],
        "profile_id": "local-current-body-inquiry",
    }
    status, response = _request(base, "/local/parse", request)
    assert status == 200
    fields = {field["field_name"]: field for field in response["fields"]}
    for name, expected in [
        ("trade_terms", "FCA"),
        ("destination", "LAX, USA"),
        ("cargo_ready_time", "2099-12-01"),
        ("contact_name", "Alex Chen"),
    ]:
        assert (fields[name]["value"], fields[name]["status"]) == (expected, "known")
        assert fields[name]["sources"][0]["source"]["kind"] == "current_body"
    assert fields["origin"]["value"] is None
    assert (fields["chargeable_weight"]["value"], fields["chargeable_weight"]["unit"]) == (
        1100,
        "kg",
    )
    assert fields["line_gross_weight"]["value"] == 510
    assert response["email_metadata"]["fields"]["subject"]["value"] == "Synthetic index sample"


def test_discover_email_and_select_exact_duplicate_mime_occurrence(indexed_server):
    base, _, _ = indexed_server
    status, result = _request(base, "/local/index?q=one.eml")
    assert status == 200 and result["total"] == 1
    email = result["emails"][0]
    assert len(email["attachments"]) == 2
    first, second = email["attachments"]
    assert first["sha256"] == second["sha256"]
    assert first["attachment_id"] != second["attachment_id"]
    status, response = _request(base, "/local/parse", _indexed_request(email, second))
    assert status == 200 and response["status"] == "partial"
    assert response["indexed_source"]["mime_part_index"] == 3
    assert response["indexed_source"]["mime_path"].endswith(".mime.2")
    assert response["indexed_source"]["index_sha256"] == result["index_sha256"]
    fields = {item["field_name"]: item for item in response["fields"]}
    assert set(fields) == {
        "ctns",
        "line_gross_weight",
        "line_net_weight",
        "unit_gross_weight",
        "unit_net_weight",
        "line_volume",
    }
    assert fields["ctns"]["value"] == 1
    assert fields["line_gross_weight"]["value"] == 8
    assert fields["line_volume"]["value"] == 0.02
    assert fields["line_net_weight"]["value"] is None


def test_named_inline_pdf_uses_original_mime_occurrence_and_file_evidence(indexed_server, tmp_path):
    base, archive_path, _ = indexed_server
    status, result = _request(base, "/local/index?q=inline-a.pdf")
    assert status == 200 and result["total"] == 1
    email = result["emails"][0]
    first, second, no_cid, unnamed, non_pdf = email["attachments"]
    assert first["supported"] and second["supported"] and no_cid["supported"]
    assert not unnamed["supported"] and not non_pdf["supported"]
    assert first["sha256"] == second["sha256"] and first["attachment_id"] != second["attachment_id"]
    status, response = _request(base, "/local/parse", _indexed_request(email, second))
    assert status == 200
    source = response["indexed_source"]
    assert source["mime_part_index"] == 3 and source["mime_path"].endswith(".mime.2")
    assert (
        source["mime_disposition"] == "inline"
        and source["mime_content_id"] == "<b@example.invalid>"
    )
    target = next(item for item in response["materials"] if item["filename"] == "inline-b.pdf")
    assert target["kind"] == "inline" and target["read_state"] == "success"
    assert target["mime_disposition"] == "inline" and target["cid"] == "b@example.invalid"
    first_material = next(
        item for item in response["materials"] if item["filename"] == "inline-a.pdf"
    )
    assert first_material["kind"] == "inline" and first_material["read_state"] == "not_selected"
    fields = {item["field_name"]: item for item in response["fields"]}
    assert fields["line_gross_weight"]["value"] == 8
    evidence = fields["line_gross_weight"]["sources"][0]
    assert evidence["source"]["material_id"] == target["material_id"]
    assert evidence["source"]["kind"] == "attachment" and evidence["source"]["locator"]["page"] == 1
    run = tmp_path / "runs" / response["run_id"]
    read = read_result(response["read_ref"])
    manifest = json.loads(Path(read["manifest_path"]).read_text(encoding="utf-8"))
    target_record = next(
        item for item in manifest["materials"] if item["material_id"] == target["material_id"]
    )
    assert target_record["metadata"]["disposition"] == "inline"
    assert target_record["metadata"]["mime_filename"] == "inline-b.pdf"
    original = next(item for item in manifest["materials"] if item["kind"] == "original_email")
    with ZipFile(archive_path) as archive:
        original_eml = archive.read("inline.eml")
    assert (run / "p01" / original["content"]["path"]).read_bytes() == original_eml
    assert hashlib.sha256(original_eml).hexdigest() == source["email_sha256"]
    assert (
        hashlib.sha256((run / "p01" / target_record["content"]["path"]).read_bytes()).hexdigest()
        == second["sha256"]
    )


def test_unnamed_inline_pdf_is_not_selectable(indexed_server):
    base, _, _ = indexed_server
    _, result = _request(base, "/local/index?q=inline-a.pdf")
    email = result["emails"][0]
    unnamed = next(item for item in email["attachments"] if item["filename"] is None)
    assert not unnamed["supported"]
    status, response = _request(base, "/local/parse", _indexed_request(email, unnamed))
    assert status == 400 and response["error"] == "invalid_request"
    non_pdf = next(item for item in email["attachments"] if item["filename"] == "notes.txt")
    status, response = _request(base, "/local/parse", _indexed_request(email, non_pdf))
    assert status == 400 and response["error"] == "invalid_request"


def test_named_inline_pdf_without_content_id_can_be_selected(indexed_server):
    base, _, _ = indexed_server
    _, result = _request(base, "/local/index?q=inline-a.pdf")
    email = result["emails"][0]
    attachment = next(
        item for item in email["attachments"] if item["filename"] == "inline-no-cid.pdf"
    )
    status, response = _request(base, "/local/parse", _indexed_request(email, attachment))
    assert status == 200 and response["indexed_source"]["mime_content_id"] is None
    target = next(item for item in response["materials"] if item["filename"] == "inline-no-cid.pdf")
    assert (
        target["kind"] == "inline" and target["cid"] is None and target["read_state"] == "success"
    )


def test_encrypted_named_inline_pdf_retains_blocked_reason(indexed_server):
    base, _, _ = indexed_server
    _, result = _request(base, "/local/index?q=blocked-inline.eml")
    email = result["emails"][0]
    status, response = _request(
        base, "/local/parse", _indexed_request(email, email["attachments"][0])
    )
    assert status == 200 and response["status"] == "partial"
    assert all(field["value"] is None for field in response["fields"])
    target = next(
        item for item in response["materials"] if item["filename"] == "encrypted-inline.pdf"
    )
    assert target["kind"] == "inline" and target["mime_disposition"] == "inline"
    assert target["read_state"] == "blocked" and target["reason"] == "encrypted"


@pytest.mark.parametrize("changed", ["content_id", "add_content_id", "disposition", "filename"])
def test_changed_inline_mime_metadata_is_rejected(indexed_server, changed):
    base, archive_path, _ = indexed_server
    _, result = _request(base, "/local/index?q=inline-a.pdf")
    email = result["emails"][0]
    filename = "inline-no-cid.pdf" if changed == "add_content_id" else "inline-b.pdf"
    attachment = next(item for item in email["attachments"] if item["filename"] == filename)
    with ZipFile(archive_path) as archive:
        message = BytesParser(policy=policy.default).parsebytes(archive.read("inline.eml"))
    part = list(message.walk())[4 if changed == "add_content_id" else 3]
    if changed == "content_id":
        part.replace_header("Content-ID", "<changed@example.invalid>")
    elif changed == "add_content_id":
        part["Content-ID"] = "<added@example.invalid>"
    elif changed == "disposition":
        part.replace_header("Content-Disposition", 'attachment; filename="inline-b.pdf"')
    else:
        part.replace_header("Content-Disposition", 'inline; filename="renamed.pdf"')
    with ZipFile(archive_path, "w") as archive:
        archive.writestr("inline.eml", message.as_bytes())
    status, response = _request(base, "/local/parse", _indexed_request(email, attachment))
    assert status == 422 and response["error"] == "indexed_source_unavailable"


def test_invalid_index_requests_are_rejected_before_parse(indexed_server):
    base, _, _ = indexed_server
    _, all_emails = _request(base, "/local/index?limit=10")
    by_name = {email["member"]: email for email in all_emails["emails"]}
    one, two = by_name["one.eml"], by_name["two.eml"]
    status, result = _request(base, "/local/parse", _indexed_request(one, two["attachments"][0]))
    assert status == 400 and result["error"] == "invalid_request"
    status, _ = _request(base, "/local/index?limit=101")
    assert status == 400
    request = _indexed_request(one, one["attachments"][0])
    request["row_anchor"] = "arbitrary"
    status, _ = _request(base, "/local/parse", request)
    assert status == 400


def test_indexed_row_profile_keeps_caller_selected_anchor(indexed_server):
    base, _, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    email = found["emails"][0]
    request = _indexed_request(email, email["attachments"][0])
    request["profile_id"] = "indexed-pdf-row"
    request["row_anchor"] = "#23"
    status, result = _request(base, "/local/parse", request)
    assert status == 200 and result["status"] == "partial"
    assert result["word_supplement"]["state"] == "not_triggered"
    assert len(result["fields"]) == 6
    assert all(field["value"] is None for field in result["fields"])
    snapshot = json.loads(Path(result["result_ref"]["ref"]).read_text(encoding="utf-8"))
    requirements = snapshot["material_handoff"]["required_field_profile"]["requirements"]
    assert all(item["pdf_row_anchor"] == "#23" for item in requirements)


def test_changed_mime_bytes_are_not_silently_replaced(indexed_server):
    base, archive_path, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    email = found["emails"][0]
    changed = _email(_pdf(encrypted=True), ["first.pdf", "copy.pdf"])
    with ZipFile(archive_path, "w") as archive:
        archive.writestr("one.eml", changed)
    status, result = _request(
        base, "/local/parse", _indexed_request(email, email["attachments"][0])
    )
    assert status == 422 and result["error"] == "indexed_source_unavailable"


def test_encrypted_pdf_returns_unread_material_and_six_unknowns(indexed_server):
    base, _, _ = indexed_server
    _, found = _request(base, "/local/index?q=blocked.eml")
    email = found["emails"][0]
    status, result = _request(
        base, "/local/parse", _indexed_request(email, email["attachments"][0])
    )
    assert status == 200 and result["status"] == "partial"
    assert len(result["fields"]) == 6
    assert all(field["value"] is None for field in result["fields"])
    target = next(item for item in result["materials"] if item["filename"] == "encrypted.pdf")
    assert target["read_state"] == "blocked" and target["reason"] == "encrypted"


def _split_total_worker(
    tmp_path, extra_texts=(), gross_value="8.0 kg (including outer carton)", gross_top=75
):
    versions = p02.configurations(cache_path=tmp_path / "cache")[("docling", "pdf")]["versions"]

    def block(text, left, top, right, bottom):
        return {
            "text": text,
            "prov": [
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

    texts = [
        block("Total packages / type", 10, 115, 95, 110),
        block("1 carton", 150, 115, 190, 110),
        block("Total volume", 10, 95, 90, 90),
        block("0.0234 m³", 150, 95, 200, 90),
        block("Total gross weight", 10, 75, 95, 70),
        block(gross_value, 150, gross_top, 240, gross_top - 5),
        *extra_texts,
    ]

    def worker(request, *, python=None, model_cache=None):
        if request["operation"] == "probe":
            return versions
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


def test_document_total_fields_link_separate_label_and_value_blocks(
    indexed_server, tmp_path, monkeypatch
):
    monkeypatch.setattr(p02, "worker", _split_total_worker(tmp_path))
    base, _, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    email = found["emails"][0]
    status, response = _request(
        base, "/local/parse", _indexed_request(email, email["attachments"][0])
    )
    assert status == 200
    fields = {item["field_name"]: item for item in response["fields"]}
    assert (fields["line_gross_weight"]["value"], fields["line_gross_weight"]["unit"]) == (8, "kg")
    assert (fields["line_volume"]["value"], fields["line_volume"]["unit"]) == (0.0234, "CBM")
    assert {x["source"]["excerpt"] for x in fields["line_gross_weight"]["sources"]} == {
        "Total gross weight",
        "8.0 kg (including outer carton)",
    }
    assert {x["source"]["excerpt"] for x in fields["line_volume"]["sources"]} == {
        "Total volume",
        "0.0234 m³",
    }
    assert all(
        x["source"]["locator"]["page"] == 1
        for name in ("line_gross_weight", "line_volume")
        for x in fields[name]["sources"]
    )


def _positioned_test_block(text, left, top, right, bottom):
    return {
        "text": text,
        "prov": [
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


@pytest.mark.parametrize(
    "extra_texts,gross_top",
    [
        ([_positioned_test_block("9 kg", 250, 75, 285, 70)], 75),
        ([_positioned_test_block("Total gross weight", 10, 55, 95, 50)], 75),
        ([_positioned_test_block("Total net weight", 110, 75, 145, 70)], 75),
        ([], 55),
    ],
    ids=["multiple_values", "duplicate_label", "intervening_label", "different_row"],
)
def test_split_total_ambiguity_does_not_guess_value(
    indexed_server, tmp_path, monkeypatch, extra_texts, gross_top
):
    monkeypatch.setattr(
        p02, "worker", _split_total_worker(tmp_path, extra_texts=extra_texts, gross_top=gross_top)
    )
    base, _, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    email = found["emails"][0]
    status, response = _request(
        base, "/local/parse", _indexed_request(email, email["attachments"][0])
    )
    fields = {item["field_name"]: item for item in response["fields"]}
    assert status == 200
    assert fields["line_gross_weight"]["value"] is None
    assert fields["line_gross_weight"]["status"] == "uncertain"
    assert fields["line_volume"]["value"] == 0.0234


def test_split_total_converts_weight_units_with_fixed_evidence(
    indexed_server, tmp_path, monkeypatch
):
    monkeypatch.setattr(p02, "worker", _split_total_worker(tmp_path, gross_value="8000 g"))
    base, _, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    email = found["emails"][0]
    status, response = _request(
        base, "/local/parse", _indexed_request(email, email["attachments"][0])
    )
    gross = next(item for item in response["fields"] if item["field_name"] == "line_gross_weight")
    assert status == 200 and (gross["value"], gross["unit"]) == (8, "kg")
    assert any(item["source"]["excerpt"] == "8000 g" for item in gross["sources"])


def test_split_total_value_and_page_cannot_be_changed_after_extraction(
    indexed_server, tmp_path, monkeypatch
):
    monkeypatch.setattr(p02, "worker", _split_total_worker(tmp_path))
    base, _, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    email = found["emails"][0]
    _, response = _request(base, "/local/parse", _indexed_request(email, email["attachments"][0]))
    snapshot = read_result(response["result_ref"])
    read = read_result(response["read_ref"])
    manifest = json.loads(Path(read["manifest_path"]).read_text(encoding="utf-8"))
    handoff = snapshot["material_handoff"]
    profile = handoff["required_field_profile"]
    for change in ("value", "page"):
        altered = copy.deepcopy(handoff)
        candidate = next(c for c in altered["candidates"] if c["field_name"] == "line_gross_weight")
        if change == "value":
            candidate["field"]["value"] = 9
        else:
            sid = candidate["field"]["source_ids"][1]
            next(source for source in altered["sources"] if source["source_id"] == sid)["locator"][
                "page"
            ] = 2
        with pytest.raises(ValueError, match="fixed source evidence"):
            _assess(
                altered,
                profile,
                read,
                manifest,
                tmp_path / "runs" / response["run_id"] / "p01",
                snapshot["field_contract_source"],
            )


def _row_word_worker(tmp_path, calls, *, ambiguous=False, overlap=False, inconsistent=False):
    versions = p02.configurations(cache_path=tmp_path / "cache")[("docling", "pdf")]["versions"]

    def block(text, left, top, right, bottom):
        return {
            "text": text,
            "prov": [
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

    headers = [
        ("Total", "CTN"),
        ("N.Weight", "(kg)/CTN"),
        ("G.Weight", "(kg)/CTN"),
        ("TOTAL", "N.W(KG)"),
        ("TOTAL", "G.W(KG)"),
        ("Total", "CBM"),
    ]

    def worker(request, *, python=None, model_cache=None):
        if request["operation"] == "probe":
            return versions
        calls.append(request)
        unit = request["profile"].get("text_cell_unit", "line")
        qty_left = 130 if unit == "word" and overlap else 100
        texts = [
            block("Qty/", qty_left, 95, qty_left + 20, 90),
            block("CTN", qty_left, 86, qty_left + 20, 81),
        ]
        for index, (upper, lower) in enumerate(headers):
            left = 140 + 40 * index
            texts.extend(
                [block(upper, left, 95, left + 20, 90), block(lower, left, 86, left + 20, 81)]
            )
        texts.append(block("#01~#22", 10, 65, 80, 60))
        if unit == "word":
            texts.extend([block("2", 105, 65, 115, 60), block("22", 142, 65, 158, 60)])
            if ambiguous:
                texts.append(block("23", 143, 65, 157, 60))
        else:
            texts.append(block("2 22", 105, 65, 158, 60))
        values = ("24", "27", "529" if unit == "word" and inconsistent else "528", "594", "3.73")
        for index, value in enumerate(values, start=1):
            left = 140 + 40 * index
            texts.append(block(value, left + 2, 65, left + 18, 60))
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


def test_merged_row_column_uses_word_read_with_locatable_sources(
    indexed_server, tmp_path, monkeypatch
):
    calls = []
    monkeypatch.setattr(p02, "worker", _row_word_worker(tmp_path, calls))
    base, _, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    email = found["emails"][0]
    request = _indexed_request(email, email["attachments"][0])
    request.update(profile_id="indexed-pdf-row", row_anchor="#01~#22")
    status, result = _request(base, "/local/parse", request)
    assert status == 200
    fields = {item["field_name"]: item for item in result["fields"]}
    assert {name: field["value"] for name, field in fields.items()} == {
        "ctns": 22,
        "unit_net_weight": 24,
        "unit_gross_weight": 27,
        "line_net_weight": 528,
        "line_gross_weight": 594,
        "line_volume": 3.73,
    }
    assert {item["source"]["excerpt"] for item in fields["ctns"]["sources"]} == {
        "22",
        "Total",
        "CTN",
        "#01~#22",
    }
    assert all(item["source"]["locator"]["page"] == 1 for item in fields["ctns"]["sources"])
    assert [call["profile"]["text_cell_unit"] for call in calls] == ["line", "word"]
    assert calls[1]["requested_scope"]["page_range"] == [1, 1]


@pytest.mark.parametrize("mode", ["duplicate_values", "overlapping_columns"])
def test_word_read_with_column_ambiguity_stays_uncertain(
    indexed_server, tmp_path, monkeypatch, mode
):
    calls = []
    monkeypatch.setattr(
        p02,
        "worker",
        _row_word_worker(
            tmp_path,
            calls,
            ambiguous=mode == "duplicate_values",
            overlap=mode == "overlapping_columns",
        ),
    )
    base, _, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    email = found["emails"][0]
    request = _indexed_request(email, email["attachments"][0])
    request.update(profile_id="indexed-pdf-row", row_anchor="#01~#22")
    status, result = _request(base, "/local/parse", request)
    fields = {item["field_name"]: item for item in result["fields"]}
    assert status == 200
    assert fields["ctns"]["value"] is None and fields["ctns"]["status"] == "uncertain"
    assert fields["line_net_weight"]["value"] == 528
    assert result["word_supplement"]["state"] == "still_unresolved"
    assert [call["profile"]["text_cell_unit"] for call in calls[:2]] == ["line", "word"]
    assert calls[2]["profile"]["pipeline"] == "StandardPdfPipeline"


def test_word_read_disagreement_preserves_previously_known_values(
    indexed_server, tmp_path, monkeypatch
):
    calls = []
    monkeypatch.setattr(p02, "worker", _row_word_worker(tmp_path, calls, inconsistent=True))
    base, _, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    email = found["emails"][0]
    request = _indexed_request(email, email["attachments"][0])
    request.update(profile_id="indexed-pdf-row", row_anchor="#01~#22")
    status, result = _request(base, "/local/parse", request)
    fields = {item["field_name"]: item for item in result["fields"]}
    assert status == 200
    assert fields["ctns"]["value"] is None
    assert fields["line_net_weight"]["value"] == 528
    assert result["word_supplement"]["state"] == "inconsistent_with_primary"


def test_indexed_location_profile_keeps_typed_locations_separate(indexed_server):
    base, _, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    status, response = _request(
        base,
        "/local/parse",
        {
            "email_id": found["emails"][0]["email_id"],
            "profile_id": "local-current-body-inquiry-locations",
        },
    )
    assert status == 200
    fields = {field["field_name"]: field for field in response["fields"]}
    assert "origin" not in fields
    for name in ("cargo_origin", "pickup_city", "departure_port"):
        assert fields[name]["value"] is None and fields[name]["status"] == "uncertain"
