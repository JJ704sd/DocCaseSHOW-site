"""Public adapter contracts: preservation, partial failure and explicit provenance."""

import base64
import copy
import json
from email import policy
from email.message import EmailMessage

import pytest

from doccase_preprocessing.inputs import adapt_eml, adapt_request
from doccase_preprocessing.models import InputError, Limits


def email(ref="current", **changes):
    value = {
        "email_ref_id": ref,
        "subject": "Re: quote",
        "from_addr": "buyer@example.test",
        "from_name": "Buyer",
        "to_addrs": ["sales@example.test"],
        "sent_at": "2026-11-06T09:00:00+08:00",
        "snippet": "Please quote",
        "thread_headers": {"message_id": f"<{ref}@example.test>"},
        "body_text": "10 boxes, 20 gross, unit unspecified\nRegards, Buyer\n> Old quote: 15",
    }
    value.update(changes)
    return value


def request(**changes):
    value = {"task_id": "task-1", "email": email()}
    value.update(changes)
    return value


def codes(material):
    return {issue.code for issue in material.issues}


def eml(raw, **kwargs):
    return adapt_eml(raw, task_id="task-eml", email_ref_id="mail-eml", **kwargs)


def cid_bundle(input_kind, present=False):
    html = '<p>10 boxes</p><img src="cid:packing%40mail"><img src="cid:packing@mail">'
    if input_kind == "json":
        attachments = (
            [
                {
                    "filename": "packing.txt",
                    "mime": "text/plain",
                    "content_id": "<packing@mail>",
                    "content_base64": base64.b64encode(b"packing table").decode(),
                }
            ]
            if present
            else []
        )
        return adapt_request(request(email=email(body_html=html, attachments=attachments)))
    message = EmailMessage()
    message["Subject"] = "Packing table"
    message.set_content(html, subtype="html")
    if present:
        message.add_related(
            b"packing table",
            maintype="text",
            subtype="plain",
            cid="<packing@mail>",
            filename="packing.txt",
        )
    return eml(message.as_bytes(policy=policy.SMTP))


def test_v1_inline_text_and_binary_keep_labels_and_representation():
    payload = request(
        email=email(
            attachments=[
                {
                    "filename": "packing.pdf",
                    "mime": "application/pdf",
                    "size_bytes": 123456,
                    "content_text": "10 boxes, no units supplied",
                },
                {
                    "filename": "packing.pdf",
                    "mime": "application/pdf",
                    "content_base64": base64.b64encode(b"%PDF-original").decode(),
                },
            ]
        )
    )
    bundle = adapt_request(payload, Limits(max_material_bytes=100))
    body, text, binary = bundle.materials
    assert body.representation == text.representation == "provided_text"
    assert text.filename == "packing.pdf" and text.mime == "application/pdf"
    assert text.declared_size == 123456 and not text.issues
    assert text.content == b"10 boxes, no units supplied"
    assert binary.representation == "original" and binary.content == b"%PDF-original"
    assert not bundle.issues


@pytest.mark.parametrize(
    "field",
    [
        "email_ref_id",
        "subject",
        "from_addr",
        "from_name",
        "to_addrs",
        "sent_at",
        "snippet",
        "thread_headers",
        "body_text",
    ],
)
def test_only_actual_v1_email_required_fields_are_required(field):
    payload = request()
    del payload["email"][field]
    with pytest.raises(InputError, match=field):
        adapt_request(payload)


def test_optional_draft_extensions_do_not_become_requirements():
    payload = request(schema_version="future-version", unknown_extension={"not": "interpreted"})
    payload["email"].update(from_addr=None, from_name=None, sent_at=None, thread_headers={})
    payload["email"]["source"] = {"kind": "archive"}
    bundle = adapt_request(payload)
    assert bundle.emails[0]["source"] == {"kind": "archive"}
    assert bundle.metadata["schema_version"] == "future-version"
    assert len(bundle.materials) == 1


@pytest.mark.parametrize("payload", [None, [], {}, {"task_id": "x", "email": []}])
def test_unrecognizable_request_is_input_error(payload):
    with pytest.raises(InputError):
        adapt_request(payload)


def test_html_is_selected_without_deleting_text_signature_quotes_or_cids():
    html = '<table><tr><td>10 boxes</td></tr></table><img src="cid:logo%40mail">'
    html += "<footer>Regards, Buyer</footer><blockquote>Old quote: 15</blockquote>"
    payload = request(email=email(body_html=html))
    bundle = adapt_request(payload)
    text, rich = [m for m in bundle.materials if m.kind == "body"]
    assert text.content.decode() == payload["email"]["body_text"]
    assert rich.content.decode("utf-8-sig") == html
    assert not text.selected and rich.selected
    assert text.alternative_group == rich.alternative_group
    assert rich.representation == "derived_text"
    assert rich.metadata["cid_references"] == ["logo@mail"]
    assert not text.issues and not rich.issues


@pytest.mark.parametrize("html", [None, "", " \r\n "])
def test_empty_html_does_not_hide_available_text(html):
    bundle = adapt_request(request(email=email(body_html=html)))
    assert bundle.materials[0].selected


def test_blocked_json_html_does_not_hide_available_text():
    bundle = adapt_request(
        request(email=email(body_text="Usable text and signature", body_html="<p>" + "x" * 100)),
        Limits(max_material_bytes=100),
    )
    text, html = bundle.materials
    assert text.selected and not text.issues
    assert "limit_exceeded" in codes(html)
    assert html.content.decode("utf-8-sig") == "<p>" + "x" * 100


@pytest.mark.parametrize(
    "headers,html_content,issue_code",
    [
        (b"Content-Type: text/html; charset=x-unknown\r\n", b"<p>quote</p>", "unknown_charset"),
        (
            b"Content-Type: text/html; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n",
            b"!broken!",
            "invalid_transfer_encoding",
        ),
    ],
)
def test_eml_invalid_html_does_not_suppress_usable_plain(headers, html_content, issue_code):
    raw = (
        b"Subject: alternatives\r\nContent-Type: multipart/alternative; boundary=B\r\n\r\n"
        b"--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        b"10 boxes\nRegards, Buyer\n> old quote\r\n--B\r\n"
        + headers
        + b"\r\n"
        + html_content
        + b"\r\n--B--\r\n"
    )
    original, text, html = eml(raw).materials
    assert text.selected and not text.issues
    assert text.content == b"10 boxes\nRegards, Buyer\n> old quote"
    assert html.content == html_content and issue_code in codes(html)
    assert text.alternative_group == html.alternative_group
    assert original.content == raw


def test_unselected_eml_html_does_not_suppress_usable_plain():
    message = EmailMessage()
    message["Subject"] = "Whitespace alternative"
    message.set_content("Usable text")
    message.add_alternative("\u00a0", subtype="html")
    _, text, html = eml(message.as_bytes(policy=policy.SMTP)).materials
    assert text.selected and not text.issues
    assert not html.selected and not html.issues


@pytest.mark.parametrize("input_kind", ["json", "eml"])
@pytest.mark.parametrize("present", [False, True])
def test_cid_inventory_registers_missing_once_per_email(input_kind, present):
    bundle = cid_bundle(input_kind, present)
    html = next(m for m in bundle.materials if m.kind == "body" and m.mime == "text/html")
    resources = [m for m in bundle.materials if m.cid == "packing@mail"]
    assert len(resources) == 1
    resource = resources[0]
    assert html.selected and not html.issues
    assert resource.email_ref_id == html.email_ref_id and resource.role == html.role
    if present:
        assert resource.content == b"packing table" and not resource.issues
    else:
        assert resource.kind == "inline" and resource.selected and resource.content is None
        assert codes(resource) == {"missing_cid"}
        assert resource.metadata["referenced_by_material_ids"] == [html.material_id]
        assert resource.parent_material_id == html.parent_material_id
        repeated = next(m for m in cid_bundle(input_kind).materials if m.cid == "packing@mail")
        assert resource.material_id == repeated.material_id


def test_missing_cid_retains_each_referring_material_once():
    message = EmailMessage()
    message["Subject"] = "Two references"
    message.make_mixed()
    for text in ("Packing table", "See the same table"):
        part = EmailMessage()
        part.set_content(f'<p>{text}</p><img src="cid:table"><img src="cid:table">', subtype="html")
        message.attach(part)
    bundle = eml(message.as_bytes(policy=policy.SMTP))
    bodies = [m for m in bundle.materials if m.kind == "body"]
    missing = [m for m in bundle.materials if m.cid == "table"]
    assert len(missing) == 1
    assert missing[0].metadata["referenced_by_material_ids"] == [m.material_id for m in bodies]
    assert all(m.selected and not m.issues for m in bodies)


def test_cid_lookup_and_missing_ids_are_scoped_to_each_mail_occurrence():
    html = '<p>See table</p><img src="cid:table">'
    payload = request(
        email=email("current", body_html=html),
        context={
            "ancestors": [
                email(
                    "history",
                    body_html=html,
                    attachments=[
                        {
                            "filename": "table.txt",
                            "mime": "text/plain",
                            "content_id": "table",
                            "content_text": "historical table",
                        }
                    ],
                )
            ],
            # Even a repeated email_ref_id has a separate source occurrence.
            "supplementary": [email("current", body_html=html)],
        },
    )
    bundle = adapt_request(payload)
    missing = [m for m in bundle.materials if "missing_cid" in codes(m)]
    assert [m.role for m in missing] == ["current", "supplementary"]
    assert len({m.material_id for m in missing}) == 2
    for resource in missing:
        body = next(
            m for m in bundle.materials if m.role == resource.role and m.mime == "text/html"
        )
        assert resource.metadata["referenced_by_material_ids"] == [body.material_id]
    repeated = [
        m.material_id
        for m in adapt_request(copy.deepcopy(payload)).materials
        if "missing_cid" in codes(m)
    ]
    assert repeated == [m.material_id for m in missing]


@pytest.mark.parametrize("bad", ["!invalid", "YQ==\n", "雪", "YQ=", 12])
def test_bad_base64_blocks_only_that_attachment(bad):
    bundle = adapt_request(
        request(
            email=email(
                attachments=[
                    {"filename": "bad.bin", "content_base64": bad},
                    {"filename": "good.bin", "content_base64": "YQ=="},
                ]
            )
        )
    )
    assert "invalid_base64" in codes(bundle.materials[1])
    assert bundle.materials[2].content == b"a" and not bundle.materials[2].issues
    assert bundle.materials[0].selected


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("filename", [], "invalid_attachment"),
        ("mime", 123, "invalid_attachment"),
        ("availability", {}, "invalid_availability"),
        ("availability", [], "invalid_availability"),
        ("availability", "invented", "invalid_availability"),
    ],
)
def test_invalid_used_attachment_fields_are_visible_and_do_not_abort(field, value, code):
    bundle = adapt_request(
        request(
            email=email(
                attachments=[
                    {field: value, "content_text": "still preserved"},
                    {"content_text": "valid"},
                ]
            )
        )
    )
    assert code in codes(bundle.materials[1])
    assert bundle.materials[1].content == b"still preserved"
    assert bundle.materials[2].content == b"valid" and not bundle.materials[2].issues


@pytest.mark.parametrize("state", ["filtered", "read_failed", "unavailable"])
def test_upstream_failures_and_missing_content_are_material_issues(state):
    bundle = adapt_request(
        request(
            email=email(
                attachments=[
                    {
                        "filename": "upstream.pdf",
                        "availability": state,
                        "availability_reason": "upstream reason",
                        "content_text": "partial evidence",
                    },
                    {"filename": "missing.pdf"},
                    None,
                ]
            )
        )
    )
    failed, missing, malformed = bundle.materials[1:]
    assert state in codes(failed) and failed.content == b"partial evidence"
    assert "missing_content" in codes(missing)
    assert "invalid_attachment" in codes(malformed)


def test_locator_is_not_fetched_and_filename_is_only_a_label(monkeypatch):
    import urllib.request

    def forbidden(*args, **kwargs):
        pytest.fail("Adapter must not download a locator")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    locator = "https://objects.example.test/a?signature=private-token"
    label = r"../../outside/C:\private\invoice.pdf"
    bundle = adapt_request(
        request(
            email=email(
                attachments=[
                    {"filename": label, "mime": "application/pdf", "oss_url": locator},
                ]
            )
        )
    )
    material = bundle.materials[1]
    assert material.locator == locator and material.content is None
    assert material.filename == label and not material.issues


def test_emails_and_metadata_do_not_duplicate_transport_or_signed_urls():
    attachment = {
        "filename": "doc.pdf",
        "oss_url": "https://oss.test/?token=SECRET",
        "content_base64": base64.b64encode(b"unique binary evidence").decode(),
    }
    ancestor = email("ancestor", body_text="private ancestor body", attachments=[attachment])
    payload = request(
        email=email(attachments=[attachment]),
        callback_url="https://callback.test/?key=PRIVATE",
        context={"ancestors": [ancestor], "supplementary": [email("side")], "coverage": "partial"},
        history_emails=[email("history", attachments=[attachment])],
        matched_inquiry_hint={"inquiry_no": "S123", "version": 2, "matched_by": "thread"},
        matched_candidates=[{"inquiry_no": "S456", "version": 1}],
    )
    original = copy.deepcopy(payload)
    bundle = adapt_request(payload)
    serialized = json.dumps([bundle.emails, bundle.metadata], ensure_ascii=False)
    for excluded in (
        attachment["oss_url"],
        "SECRET",
        attachment["content_base64"],
        payload["callback_url"],
        payload["email"]["body_text"],
        ancestor["body_text"],
        "content_base64",
        "history_emails",
        "attachments",
    ):
        assert excluded not in serialized
    assert bundle.metadata["matched_inquiry_hint"] == payload["matched_inquiry_hint"]
    assert bundle.metadata["matched_candidates"] == payload["matched_candidates"]
    assert bundle.metadata["context"] == {"coverage": "partial"}
    assert payload == original


def test_confirmed_roles_history_and_context_issues_are_not_business_inference():
    payload = request(
        context={
            "ancestors": [email("a")],
            "supplementary": [email("b")],
            "issues": [{"code": "missing_parent", "message": "Confirmed gap"}],
        },
        history_emails=[email("history", thread_headers={"in_reply_to": "<a@example.test>"})],
    )
    bundle = adapt_request(payload)
    assert [item["role"] for item in bundle.emails] == [
        "current",
        "ancestor",
        "supplementary",
        "supplementary",
    ]
    assert [item.role for item in bundle.materials] == [
        "current",
        "ancestor",
        "supplementary",
        "supplementary",
    ]
    assert {issue.code for issue in bundle.issues} == {"unclassified_history", "missing_parent"}
    assert all(not material.issues for material in bundle.materials)
    assert bundle.materials[0].content.decode() == payload["email"]["body_text"]
    assert "unit unspecified" in bundle.materials[0].content.decode()
    assert "inquiry" not in bundle.metadata and "weight" not in bundle.metadata


def test_occurrence_ids_are_stable_and_provided_ids_are_preserved():
    payload = request(
        email=email(
            attachments=[
                {"content_text": "same", "filename": "same.txt"},
                {"content_text": "same", "filename": "same.txt"},
                {"material_id": "upstream-id", "content_text": "same"},
            ]
        ),
        context={"ancestors": [email("current")]},
    )
    first, second = adapt_request(payload), adapt_request(copy.deepcopy(payload))
    assert [m.material_id for m in first.materials] == [m.material_id for m in second.materials]
    assert len({m.material_id for m in first.materials}) == len(first.materials)
    assert first.materials[3].material_id == "upstream-id"
    payload["email"]["attachments"][0]["material_id"] = "upstream-id"
    with pytest.raises(InputError, match="Duplicate material_id"):
        adapt_request(payload)


def test_request_limits_bound_entry_material_count_and_actual_text():
    payload = request(email=email(body_text="1234"))
    size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
    assert adapt_request(payload, Limits(max_input_bytes=size)).materials
    with pytest.raises(InputError, match="Input byte"):
        adapt_request(payload, Limits(max_input_bytes=size - 1))
    oversized = adapt_request(payload, Limits(max_material_bytes=3)).materials[0]
    assert oversized.content == b"1234" and "limit_exceeded" in codes(oversized)
    issue = oversized.issues[0]
    assert issue.details["actual"] == 4 and issue.details["limit"] == 3
    payload["email"]["attachments"] = [{"content_text": "a"}]
    with pytest.raises(InputError, match="Material count"):
        adapt_request(payload, Limits(max_materials=1))


def test_eml_alternatives_cid_and_original_provenance():
    message = EmailMessage()
    message["Subject"] = "Table and signature"
    message["From"] = "Buyer <buyer@example.test>"
    message["Message-ID"] = "<original@example.test>"
    message.set_content("Plain signature\n> quoted history")
    message.add_alternative(
        "<table><tr><td>10 boxes</td></tr></table>"
        '<img src="cid:logo"><blockquote>history</blockquote>',
        subtype="html",
    )
    message.get_payload()[1].add_related(
        b"\x89PNG\r\n\x1a\n",
        maintype="image",
        subtype="png",
        cid="<logo>",
        disposition="inline",
        filename="logo.png",
    )
    message.add_attachment(
        b"%PDF-original", maintype="application", subtype="pdf", filename="../../invoice.pdf"
    )
    raw = message.as_bytes(policy=policy.SMTP)
    bundle = eml(raw)
    original = bundle.materials[0]
    assert original.kind == "original_email" and original.content == raw
    assert not original.selected and original.representation == "original"
    text, html = [m for m in bundle.materials if m.kind == "body"]
    assert not text.selected and html.selected
    assert b"signature" in text.content and b"quoted history" in text.content
    assert b"<table>" in html.content and html.metadata["cid_references"] == ["logo"]
    assert text.alternative_group == html.alternative_group
    assert text.representation == html.representation == "derived_text"
    inline = next(m for m in bundle.materials if m.kind == "inline")
    assert inline.cid == "logo" and inline.content == b"\x89PNG\r\n\x1a\n"
    assert inline.selected and inline.representation == "original"
    attachment = next(m for m in bundle.materials if m.kind == "attachment")
    assert attachment.filename == "../../invoice.pdf"
    assert all(m.parent_material_id == original.material_id for m in bundle.materials[1:])
    assert all(not m.issues for m in bundle.materials)


@pytest.mark.parametrize("charset,text", [("gb18030", "货物及签名"), ("iso-8859-1", "café")])
def test_eml_uses_declared_charset_and_marks_utf8_derivative(charset, text):
    raw = (
        f"Subject: charset\r\nContent-Type: text/plain; charset={charset}\r\n"
        "Content-Transfer-Encoding: base64\r\n\r\n"
    ).encode()
    raw += base64.b64encode(text.encode(charset))
    original, body = eml(raw).materials
    assert original.content == raw
    assert body.content == text.encode("utf-8") and not body.issues
    assert body.representation == "derived_text"
    assert body.metadata["source_charset"] == charset
    assert body.parent_material_id == original.material_id


@pytest.mark.parametrize(
    "declaration,content,code",
    [
        ('; charset="unknown-charset"', b"abc", "unknown_charset"),
        ('; charset="utf-8"', b"\xff", "invalid_text_encoding"),
        ('; charset="utf-7"', b"+2AA-", "invalid_text_encoding"),
        ("", "雪".encode(), "invalid_text_encoding"),
    ],
)
def test_eml_bad_charset_is_visible_without_replacement_or_fallback(declaration, content, code):
    raw = (
        f"Subject: bad charset\r\nContent-Type: text/plain{declaration}\r\n"
        "Content-Transfer-Encoding: base64\r\n\r\n"
    ).encode() + base64.b64encode(content)
    bundle = eml(raw)
    body = bundle.materials[1]
    assert code in codes(body) and body.content == content
    assert body.representation == "original"
    assert bundle.materials[0].content == raw


def test_eml_bad_base64_and_unknown_transfer_encoding_do_not_drop_siblings():
    raw = (
        b"Subject: partial\r\nContent-Type: multipart/mixed; boundary=B\r\n\r\n"
        b"--B\r\nContent-Type: text/plain\r\n\r\ngood body\r\n"
        b"--B\r\nContent-Type: application/pdf\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n!broken!\r\n"
        b"--B\r\nContent-Type: application/octet-stream\r\n"
        b"Content-Transfer-Encoding: x-mystery\r\n\r\nopaque\r\n"
        b"--B\r\nContent-Type: application/pdf\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\nJVBE\r\nRi1vaw==\r\n--B--\r\n"
    )
    bundle = eml(raw)
    assert len(bundle.materials) == 5
    assert bundle.materials[1].content == b"good body"
    assert "invalid_transfer_encoding" in codes(bundle.materials[2])
    assert "unsupported_transfer_encoding" in codes(bundle.materials[3])
    assert bundle.materials[4].content == b"%PDF-ok" and not bundle.materials[4].issues


def test_nested_rfc822_is_an_opaque_attachment_with_exact_bytes():
    nested = b"Subject:  nested\r\nX-Spacing:   unchanged\r\n\r\nNESTED BODY\r\n"
    raw = (
        b"Subject: outer\r\nContent-Type: multipart/mixed; boundary=B\r\n\r\n"
        b"--B\r\nContent-Type: text/plain\r\n\r\nCURRENT BODY\r\n"
        b"--B\r\nContent-Type: message/rfc822\r\n"
        b"Content-Disposition: attachment; filename=forward.eml\r\n\r\n" + nested + b"\r\n--B--\r\n"
    )
    bundle = eml(raw)
    assert len(bundle.emails) == 1 and len(bundle.materials) == 3
    original, body, attachment = bundle.materials
    assert body.content == b"CURRENT BODY"
    assert attachment.kind == "attachment" and "unsupported" in codes(attachment)
    assert attachment.content == nested and attachment.representation == "original"
    assert attachment.parent_material_id == original.material_id


def test_eml_mixed_bodies_are_distinct_but_alternative_html_is_chosen_once():
    message = EmailMessage()
    message["Subject"] = "multiple bodies"
    message.make_mixed()
    for text in ("first independent body", "second independent body"):
        part = EmailMessage()
        part.set_content(text)
        message.attach(part)
    alternative = EmailMessage()
    alternative.set_content("plain alternative")
    alternative.add_alternative("<p>first HTML</p>", subtype="html")
    alternative.add_alternative("<p>second HTML</p>", subtype="html")
    message.attach(alternative)
    bundle = eml(message.as_bytes(policy=policy.SMTP))
    bodies = [m for m in bundle.materials if m.kind == "body"]
    assert len(bodies) == 5
    assert [m.selected for m in bodies] == [True, True, False, False, True]


@pytest.mark.parametrize("raw", [b"", b"not an email", "Subject: not bytes"])
def test_unrecognizable_eml_is_input_error(raw):
    with pytest.raises(InputError):
        eml(raw)


def test_eml_limits_include_original_and_preserve_oversized_item():
    raw = b"Subject: example\r\n\r\nbody"
    with pytest.raises(InputError, match="Input byte"):
        eml(raw, limits=Limits(max_input_bytes=len(raw) - 1))
    with pytest.raises(InputError, match="Material count"):
        eml(raw, limits=Limits(max_materials=1))
    bundle = eml(raw, limits=Limits(max_material_bytes=3))
    assert bundle.materials[0].content == raw and bundle.materials[1].content == b"body"
    assert all("limit_exceeded" in codes(m) for m in bundle.materials)
