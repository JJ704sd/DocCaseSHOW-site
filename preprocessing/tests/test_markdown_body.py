"""The optional body representation must retain a verifiable source mapping."""

import copy
import json
from email.message import EmailMessage

import pytest

from doccase_preprocessing.inspection import inspect_material
from doccase_preprocessing.local_handoff import run_local_handoff
from doccase_preprocessing.markdown_body import convert_body, mapped_sections, validate_mapping
from doccase_preprocessing.models import Limits, MaterialError


def test_plain_text_escapes_markdown_and_retains_source_positions():
    raw = "# ref 123\nGross weight: 12 kg\n".encode()
    markdown, mapping, reason = convert_body(raw, "text/plain")
    assert reason is None and markdown.startswith(b"\\# ref 123")
    validate_mapping(raw, markdown, mapping)
    assert any(item["text"] == "Gross weight: 12 kg" for item in mapping["segments"])


def test_inline_decimal_and_dashes_remain_one_readable_cargo_line():
    raw = b"<p>34 ctns - 288 KG - 1.55 CBM</p>"
    markdown, mapping, reason = convert_body(raw, "text/html")
    assert reason is None and b"34 ctns - 288 KG - 1.55 CBM" in markdown
    validate_mapping(raw, markdown, mapping)


def test_previous_markdown_mapping_still_revalidates():
    raw = b"<p>34 ctns - 288 KG - 1.55 CBM</p>"
    markdown, mapping, reason = convert_body(raw, "text/html", version="body-markdown-1")
    assert reason is None and b"1\\.55" in markdown
    validate_mapping(raw, markdown, mapping)


def test_literal_plain_text_list_markers_do_not_become_markdown_lists():
    markdown, mapping, reason = convert_body(b"1. ref\n- text\n", "text/plain")
    assert reason is None and b"1\\. ref" in markdown and b"\\- text" in markdown
    validate_mapping(b"1. ref\n- text\n", markdown, mapping)


def test_html_simple_table_and_quote_have_separate_sections():
    raw = (
        "<p>FOB Shanghai</p><table><tr><th>Gross</th><th>CBM</th></tr>"
        "<tr><td>12 kg</td><td>1.2</td></tr></table>"
        "<blockquote><p>EXW Ningbo</p></blockquote>"
    ).encode()
    markdown, mapping, reason = convert_body(raw, "text/html")
    assert reason is None and b"| Gross | CBM |" in markdown
    validate_mapping(raw, markdown, mapping)
    assert next(x for x in mapping["segments"] if x["text"] == "EXW Ningbo")["section"] == "quoted"
    assert (
        next(x for x in mapping["segments"] if x["text"] == "FOB Shanghai")["section"] == "current"
    )


def test_packing_table_keeps_columns_when_the_header_uses_td():
    raw = (
        b"<table><tr><td><strong>PACKAGE</strong></td><td><strong>LENGTH</strong></td>"
        b"<td><strong>WIDTH</strong></td><td><strong>HEIGHT</strong></td>"
        b"<td><strong>CBM</strong></td><td><strong>GROSS WEIGHT</strong></td></tr>"
        b"<tr><td>1x PALLET</td><td>-</td><td>-</td><td>-</td><td>0.46</td><td>84 kg</td></tr>"
        b"<tr><td></td><td></td><td></td><td></td><td></td><td>TOTAL</td></tr>"
        b"<tr><td>1x</td><td>-</td><td>-</td><td>-</td><td>0.46</td><td>84 kg</td></tr></table>"
    )
    markdown, mapping, reason = convert_body(raw, "text/html")
    assert reason is None
    assert markdown.splitlines()[:5] == [
        b"| PACKAGE | LENGTH | WIDTH | HEIGHT | CBM | GROSS WEIGHT |",
        b"| --- | --- | --- | --- | --- | --- |",
        b"| 1x PALLET | - | - | - | 0.46 | 84 kg |",
        b"|  |  |  |  |  | TOTAL |",
        b"| 1x | - | - | - | 0.46 | 84 kg |",
    ]
    validate_mapping(raw, markdown, mapping)
    assert (
        convert_body(raw, "text/html", version="body-markdown-2")[2]
        == "table_without_explicit_header"
    )
    other = raw.replace(b"PACKAGE", b"TITLE", 1)
    assert (
        convert_body(other, "text/html", version="body-markdown-3")[2]
        == "table_without_explicit_header"
    )
    markdown, mapping, reason = convert_body(other, "text/html")
    assert reason is None and other in markdown
    validate_mapping(other, markdown, mapping)


def test_complex_table_keeps_original_spans_and_separate_cell_sources():
    raw = (
        b'<table><tr><td colspan="2">Dimensions</td></tr>'
        b"<tr><td>Outer<table><tr><td>42 kg</td></tr></table></td><td>1.2 CBM</td></tr></table>"
    )
    markdown, mapping, reason = convert_body(raw, "text/html")
    assert reason is None and raw in markdown
    validate_mapping(raw, markdown, mapping)
    sources = {part["text"]: part["section"] for part in mapping["segments"]}
    assert sources == {
        "Dimensions": "current",
        "Outer": "current",
        "42 kg": "current",
        "1.2 CBM": "current",
    }
    assert mapped_sections(["Outer42 kg"], mapping) == ["unknown"]
    assert convert_body(raw, "text/html", version="body-markdown-3")[2] == "merged_table_cells"


def test_ordered_nested_list_and_empty_outlook_superscript():
    inline = b"<div>Please <b>quote</b> the following shipment.</div>"
    raw = inline + (
        b'<img style="width:0"><ol start="3"><li>4 packages<sup class="note"></sup><ul>'
        b"<li>1.08 CBM</li></ul></li><li>420 kg</li></ol>"
    )
    markdown, mapping, reason = convert_body(raw, "text/html")
    assert reason is None and b'<ol start="3">' in markdown and b"<ul>" in markdown
    assert inline in markdown
    validate_mapping(raw, markdown, mapping)
    assert [part["text"] for part in mapping["segments"]] == [
        "Please quote the following shipment.",
        "4 packages",
        "1.08 CBM",
        "420 kg",
    ]
    assert convert_body(b"<p>1 m<sup>3</sup></p>", "text/html")[2] == "unsupported_html_semantics"


def test_portuguese_forward_header_needs_sent_and_recipient():
    raw = b"<p>40 cartons</p><p>De: A<br>Enviada em: Monday<br>Para: B</p><p>2 cartons</p>"
    markdown, mapping, reason = convert_body(raw, "text/html")
    assert reason is None
    validate_mapping(raw, markdown, mapping)
    assert mapped_sections(["40 cartons", "2 cartons"], mapping) == ["current", "quoted"]
    assert (
        convert_body(raw.replace(b"Enviada em: Monday<br>", b""), "text/html")[2]
        == "ambiguous_quote_boundary"
    )


def test_repeated_body_text_needs_both_full_occurrences():
    raw = b"<p>40 cartons</p><blockquote><p>40 cartons</p></blockquote>"
    markdown, mapping, reason = convert_body(raw, "text/html")
    assert reason is None
    validate_mapping(raw, markdown, mapping)
    assert mapped_sections(["40 cartons"], mapping) == ["unknown"]
    assert mapped_sections(["40 cartons", "40 cartons"], mapping) == ["current", "quoted"]


def test_current_line_shared_by_longer_quote_requires_both_intact_blocks():
    line = "This shipment is packed in 40 cartons."
    quote = f"{line} Please arrange pickup."
    raw = f"<p>{line}</p><blockquote><p>{quote}</p></blockquote>".encode()
    markdown, mapping, reason = convert_body(raw, "text/html")
    assert reason is None
    validate_mapping(raw, markdown, mapping)
    assert mapped_sections([line, quote], mapping) == ["current", "quoted"]
    assert mapped_sections([line], mapping) == ["unknown"]
    assert mapped_sections([quote, line], mapping) == ["quoted", "unknown"]


def test_mapping_rejects_a_forged_current_section():
    raw = b"<p>current</p><blockquote><p>40 cartons</p></blockquote>"
    markdown, mapping, _ = convert_body(raw, "text/html")
    forged = copy.deepcopy(mapping)
    forged["segments"][-1]["section"] = "current"
    with pytest.raises(ValueError, match="does not reproduce"):
        validate_mapping(raw, markdown, forged)


def test_table_with_mixed_quote_scope_keeps_original_selected(tmp_path):
    mail = EmailMessage()
    mail.set_content("plain fallback")
    mail.add_alternative(
        "<table><tr><td>4 packages<blockquote>2 cartons</blockquote></td></tr></table>",
        subtype="html",
    )
    result = run_local_handoff(
        mail.as_bytes(),
        tmp_path,
        input_format="eml",
        task_id="task",
        email_ref_id="email",
        request_reference="local",
        body_input="markdown",
    )
    bodies = [x for x in result["received"]["manifest"]["materials"] if x["kind"] == "body"]
    selected = [x for x in bodies if x["state"] == "ready"]
    assert len(selected) == 1 and selected[0]["format"] == "html"
    assert (
        selected[0]["metadata"]["markdown_conversion"]["reason"]
        == "unsupported_embedded_html_scope"
    )
    assert not any(x["filename"] == "body.md" for x in bodies)


def test_markdown_material_links_preserved_body_and_mapping(tmp_path):
    mail = EmailMessage()
    mail.set_content("Gross weight: 12 kg")
    result = run_local_handoff(
        mail.as_bytes(),
        tmp_path,
        input_format="eml",
        task_id="task",
        email_ref_id="email",
        request_reference="local",
        body_input="markdown",
    )
    records = result["received"]["manifest"]["materials"]
    derived = next(x for x in records if x["filename"] == "body.md")
    original = next(x for x in records if x["material_id"] == derived["parent_material_id"])
    assert derived["state"] == "ready" and derived["format"] == "markdown"
    assert original["state"] == "preserved"
    mapping_ref = derived["metadata"]["markdown_conversion"]["mapping"]
    mapping = json.loads((tmp_path / mapping_ref["path"]).read_bytes())
    validate_mapping(
        (tmp_path / original["content"]["path"]).read_bytes(),
        (tmp_path / derived["content"]["path"]).read_bytes(),
        mapping,
    )


def test_mapping_respects_material_size_limit_and_falls_back(tmp_path):
    mail = EmailMessage()
    mail.set_content("Gross weight: 12 kg")
    result = run_local_handoff(
        mail.as_bytes(),
        tmp_path,
        input_format="eml",
        task_id="task",
        email_ref_id="email",
        request_reference="local",
        body_input="markdown",
        limits=Limits(max_material_bytes=300),
    )
    records = result["received"]["manifest"]["materials"]
    body = next(x for x in records if x["kind"] == "body" and x["state"] == "ready")
    assert body["format"] == "text"
    assert body["metadata"]["markdown_conversion"]["reason"] == "source_map_limit"


def test_markdown_format_is_opt_in_and_not_enabled_for_regular_attachments(tmp_path):
    with pytest.raises(MaterialError) as error:
        inspect_material(b"hello", "note.md", "text/markdown")
    assert error.value.issue.code == "unsupported_format"
    assert (
        inspect_material(b"hello", "body.md", "text/markdown", allow_markdown=True).route
        == "docling"
    )
    mail = EmailMessage()
    mail.set_content("current body")
    mail.add_attachment("attachment", subtype="markdown", filename="note.md")
    result = run_local_handoff(
        mail.as_bytes(),
        tmp_path,
        input_format="eml",
        task_id="task",
        email_ref_id="email",
        request_reference="local",
        body_input="markdown",
    )
    records = result["received"]["manifest"]["materials"]
    assert next(x for x in records if x["filename"] == "body.md")["state"] == "ready"
    attachment = next(x for x in records if x["filename"] == "note.md")
    assert (
        attachment["state"] == "blocked" and attachment["issues"][0]["code"] == "unsupported_format"
    )
