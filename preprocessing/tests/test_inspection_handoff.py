"""Inspection regressions through public checks and the material handoff."""

import zipfile
from io import BytesIO

import pytest
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, NameObject, NumberObject

from doccase_preprocessing.inspection import inspect_material
from doccase_preprocessing.models import Bundle, Material, MaterialError
from doccase_preprocessing.pipeline import prepare


def broken_pdf_number_child():
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.root_object["/Pages"][NameObject("/Kids")] = ArrayObject([NumberObject(1)])
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


def assert_corrupt_material_is_isolated(raw, filename, mime, output_dir):
    valid = b"Independent valid material"
    bundle = Bundle(
        "inspection-handoff",
        [],
        [
            Material("bad", "email", "current", "attachment", filename, mime, content=raw),
            Material(
                "good", "email", "current", "attachment", "good.txt", "text/plain", content=valid
            ),
        ],
    )
    result = prepare(bundle, output_dir)
    assert result["state"] == "partial"
    assert result["summary"]["blocked"] == result["summary"]["ready"] == 1
    bad, good = result["materials"]
    assert bad["state"] == "blocked" and bad["parse_task_id"] is None
    assert bad["issues"][0]["code"] == "corrupt"
    assert bad["scope"] is None
    assert (output_dir / bad["original"]["path"]).read_bytes() == raw
    assert good["state"] == "ready" and not good["issues"]
    assert (output_dir / good["content"]["path"]).read_bytes() == valid
    assert len(result["parse_tasks"]) == 1
    assert result["parse_tasks"][0]["material_ids"] == ["good"]
    assert result["parse_tasks"][0]["route"] == "text"
    assert (output_dir / result["manifest_path"]).is_file()


def test_pdf_number_child_is_a_material_error_with_unknown_page_count():
    with pytest.raises(MaterialError) as caught:
        inspect_material(broken_pdf_number_child(), "broken.pdf", "application/pdf")
    assert caught.value.issue.code == "corrupt"
    assert caught.value.issue.details["page_count"] is None


def test_pdf_number_child_preserves_valid_sibling_and_partial_handoff(tmp_path):
    assert_corrupt_material_is_isolated(
        broken_pdf_number_child(),
        "broken.pdf",
        "application/pdf",
        tmp_path,
    )


def docx_with_unknown_xml_charset():
    parts = {
        "[Content_Types].xml": (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'wordprocessingml.document.main+xml"/></Types>'
        ),
        "_rels/.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="root" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>'
        ),
        "word/document.xml": (
            '<?xml version="1.0" encoding="unknown-charset"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Example</w:t></w:r></w:p></w:body></w:document>"
        ),
    }
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return stream.getvalue()


def test_unknown_xml_charset_is_a_material_error():
    with pytest.raises(MaterialError) as caught:
        inspect_material(docx_with_unknown_xml_charset(), "broken.docx", "")
    assert caught.value.issue.code == "corrupt"


def test_unknown_xml_charset_preserves_valid_sibling_and_partial_handoff(tmp_path):
    assert_corrupt_material_is_isolated(
        docx_with_unknown_xml_charset(),
        "broken.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        tmp_path,
    )


def test_pkg_shipping_text_is_not_a_zip_archive():
    raw = b"PKG: 10 cartons; gross weight 120 kg"
    inspected = inspect_material(raw, "packing.txt", "text/plain")
    assert (inspected.format, inspected.route) == ("text", "text")
    assert not inspected.issues


@pytest.mark.parametrize("representation", ["original", "provided_text"])
def test_pkg_shipping_text_is_handed_off_without_losing_its_content(tmp_path, representation):
    raw = b"PKG: 10 cartons; gross weight 120 kg"
    filename, mime = (
        ("packing.pdf", "application/pdf")
        if representation == "provided_text"
        else (
            "packing.txt",
            "text/plain",
        )
    )
    bundle = Bundle(
        "pkg-handoff",
        [],
        [
            Material(
                "pkg",
                "email",
                "current",
                "attachment",
                filename,
                mime,
                content=raw,
                representation=representation,
            )
        ],
    )
    result = prepare(bundle, tmp_path)
    assert result["state"] == "ready" and result["summary"]["blocked"] == 0
    record = result["materials"][0]
    assert record["state"] == "ready" and record["format"] == "text"
    assert record["filename"] == filename and record["declared_mime"] == mime
    assert (tmp_path / record["content"]["path"]).read_bytes() == raw
    assert (record["original"] is None) == (representation == "provided_text")
    assert len(result["parse_tasks"]) == 1
    assert result["parse_tasks"][0]["route"] == "text"
    assert result["parse_tasks"][0]["material_ids"] == ["pkg"]


@pytest.mark.parametrize("signature", [b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"])
def test_truncated_zip_signatures_are_not_accepted_as_text(signature):
    with pytest.raises(MaterialError) as caught:
        inspect_material(signature + b"truncated", "archive", "application/octet-stream")
    assert caught.value.issue.code == "corrupt"
