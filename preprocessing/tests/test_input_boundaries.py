"""Synthetic regressions for accepted MIME and HTML resource boundaries."""

from email import policy
from email.parser import BytesParser

import pytest
from test_inputs import email, request

from doccase_preprocessing.inputs import adapt_eml, adapt_request


@pytest.mark.parametrize("newline", [b"\r\n", b"\n", b"\r"], ids=["crlf", "lf", "cr"])
@pytest.mark.parametrize("paragraphs", [1, 2], ids=["single", "multiple"])
@pytest.mark.parametrize("with_headers", [False, True], ids=["empty-headers", "normal-headers"])
def test_mime_empty_header_line_keeps_the_complete_body(newline, paragraphs, with_headers):
    body = b"Please quote 10 boxes."
    if paragraphs == 2:
        body += newline * 2 + b"Regards, Buyer."
    child_headers = b"Content-Type: text/plain; charset=us-ascii" + newline if with_headers else b""
    raw = (
        newline.join(
            [
                b"Subject: MIME body boundary",
                b"MIME-Version: 1.0",
                b"Content-Type: multipart/mixed; boundary=PART",
                b"",
                b"--PART",
            ]
        )
        + newline
        + child_headers
        + newline
        + body
        + newline
        + b"--PART--"
        + newline
    )
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    assert all(not part.defects for part in parsed.walk())
    assert parsed.get_payload()[0].get_payload(decode=True) == body

    bundle = adapt_eml(raw, task_id="mime-boundary", email_ref_id="current")
    original, material = bundle.materials
    assert material.content == body
    assert material.kind == "body" and material.selected and not material.issues
    assert original.content == raw


def test_header_like_body_text_is_preserved_after_the_empty_header_line():
    raw = (
        b"Subject: quote\r\nContent-Type: multipart/mixed; boundary=PART\r\n\r\n"
        b"--PART\r\n\r\nQuantity: 10 boxes\r\n\r\nSignature\r\n--PART--\r\n"
    )
    bundle = adapt_eml(raw, task_id="header-like-body", email_ref_id="current")
    assert bundle.materials[1].content == b"Quantity: 10 boxes\r\n\r\nSignature"


def html_bundle(input_kind, html):
    if input_kind == "json":
        return adapt_request(request(email=email(body_html=html)))
    raw = (
        b"Subject: resource references\r\nContent-Type: text/html; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\n" + html.encode("utf-8")
    )
    return adapt_eml(raw, task_id="html-references", email_ref_id="current")


@pytest.mark.parametrize("input_kind", ["json", "eml"])
@pytest.mark.parametrize(
    "html",
    [
        "<p>Shipment label CID:12345.</p>",
        '<!-- <img src="cid:comment@mail"> --><p>Quote</p>',
        '<script src="cid:script-file@mail">const image="cid:script@mail";</script><p>Quote</p>',
        '<img src="https://example.test/logo.png" alt="cid:alt@mail" title="cid:title@mail">',
        '<style>/* background:url(cid:comment@mail) */ p {content:"cid:label"}</style><p>Quote</p>',
    ],
)
def test_cid_ignores_non_resource_text(input_kind, html):
    bundle = html_bundle(input_kind, html)
    body = next(m for m in bundle.materials if m.kind == "body" and m.mime == "text/html")
    assert body.metadata["cid_references"] == []
    assert not any(m.cid for m in bundle.materials)
    assert body.content.decode("utf-8-sig") == html


@pytest.mark.parametrize("input_kind", ["json", "eml"])
@pytest.mark.parametrize(
    "html",
    [
        '<img src="cid:packing&#64;mail"><a href="cid:packing%40mail">Same table</a>',
        "<div style=\"background-image: url('cid:packing&#64;mail')\">Quote</div>",
        "<style>.table {background-image: URL( cid:packing%40mail )}</style><p>Quote</p>",
        '<a href="CID:packing%40mail">Packing table</a>',
    ],
)
def test_cid_resource_urls_decode_entities_then_percent_escapes(input_kind, html):
    bundle = html_bundle(input_kind, html)
    body = next(m for m in bundle.materials if m.kind == "body" and m.mime == "text/html")
    assert body.metadata["cid_references"] == ["packing@mail"]
    resources = [m for m in bundle.materials if m.cid]
    assert len(resources) == 1 and resources[0].cid == "packing@mail"
    assert resources[0].metadata["referenced_by_material_ids"] == [body.material_id]
    assert body.content.decode("utf-8-sig") == html


def test_entity_escaped_cid_matches_the_supplied_material():
    bundle = adapt_request(
        request(
            email=email(
                body_html='<a href="cid:packing&#64;mail">Packing table</a>',
                attachments=[
                    {
                        "filename": "packing.txt",
                        "mime": "text/plain",
                        "content_id": "packing@mail",
                        "content_text": "10 boxes",
                    }
                ],
            )
        )
    )
    resources = [m for m in bundle.materials if m.cid]
    assert len(resources) == 1 and not resources[0].issues
