"""UTF-8 HTML must not be decoded using a stale in-document charset."""

import codecs
from email import policy
from email.message import EmailMessage

import pytest
from test_inputs import email, eml, request

from doccase_preprocessing.inputs import adapt_request
from doccase_preprocessing.pipeline import prepare


@pytest.mark.parametrize("input_kind", ["eml", "json"])
@pytest.mark.parametrize("existing_bom", [False, True])
def test_html_handoff_declares_utf8_without_rewriting_markup(tmp_path, input_kind, existing_bom):
    markup = (
        '<html><head><meta http-equiv="Content-Type" '
        'content="text/html; charset=iso-8859-1"></head>'
        "<body><p>货物 café: 10 boxes, 105 kg each.</p></body></html>\n"
    )
    supplied = ("\ufeff" if existing_bom else "") + markup
    if input_kind == "eml":
        message = EmailMessage()
        message["Subject"] = "Encoding handoff"
        message.set_content(supplied, subtype="html", charset="utf-8", cte="base64")
        raw = message.as_bytes(policy=policy.default)
        bundle = eml(raw)
    else:
        bundle = adapt_request(request(email=email(body_text="", body_html=supplied)))

    result = prepare(bundle, tmp_path)
    body = next(m for m in result["materials"] if m["declared_mime"] == "text/html")
    content = (tmp_path / body["content"]["path"]).read_bytes()
    # HTML consumers prioritize the BOM over stale meta declarations. Keep the
    # supplied markup and its CID links intact instead of reserializing the DOM.
    assert content.startswith(codecs.BOM_UTF8)
    assert content.decode("utf-8-sig") == markup
    assert body["representation"] == "derived_text" and body["original"] is None
    assert body["scope"]["encoding"] == "utf-8"
    task = next(t for t in result["parse_tasks"] if t["parse_task_id"] == body["parse_task_id"])
    assert task["route"] == "docling" and task["content"] == body["content"]
    assert task["execution_state"] == "not_started"
    if input_kind == "eml":
        original = next(m for m in result["materials"] if m["kind"] == "original_email")
        assert body["parent_material_id"] == original["material_id"]
        assert (tmp_path / original["original"]["path"]).read_bytes() == raw


def test_legacy_charset_html_transcodes_without_changing_its_text():
    markup = '<meta charset="iso-8859-1"><p>café: 105 kg each</p>\n'
    message = EmailMessage()
    message["Subject"] = "Legacy charset"
    message.set_content(markup, subtype="html", charset="iso-8859-1", cte="base64")
    raw = message.as_bytes(policy=policy.default)
    original, body = eml(raw).materials
    assert body.content.startswith(codecs.BOM_UTF8)
    assert body.content.decode("utf-8-sig") == markup
    assert body.metadata["source_charset"] == "iso-8859-1"
    assert original.content == raw


@pytest.mark.parametrize("input_kind", ["eml", "json"])
def test_bom_only_html_does_not_suppress_nonempty_text(input_kind):
    if input_kind == "eml":
        message = EmailMessage()
        message["Subject"] = "Empty HTML alternative"
        message.set_content("10 boxes, 105 kg each")
        message.add_alternative("\ufeff \n", subtype="html", charset="utf-8")
        bundle = eml(message.as_bytes(policy=policy.default))
    else:
        bundle = adapt_request(
            request(email=email(body_text="10 boxes, 105 kg each", body_html="\ufeff \n"))
        )
    bodies = {m.mime: m for m in bundle.materials if m.kind == "body"}
    assert bodies["text/plain"].selected
    assert not bodies["text/html"].selected
