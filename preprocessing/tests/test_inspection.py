"""Synthetic fixtures test format/structure checks, not extraction quality."""

import struct
import unittest
import warnings
import zipfile
import zlib
from io import BytesIO
from unittest.mock import PropertyMock, patch

from PIL import Image
from pypdf import PdfWriter

from doccase_preprocessing.inspection import inspect_material
from doccase_preprocessing.models import Limits, MaterialError

CT = "http://schemas.openxmlformats.org/package/2006/content-types"
RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLS = "application/vnd.ms-excel"
CFB = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def package(parts, compression=zipfile.ZIP_STORED):
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", compression=compression) as archive:
        for name, content in parts.items() if isinstance(parts, dict) else parts:
            archive.writestr(name, content)
    return stream.getvalue()


def content_types(fmt):
    folder, main = ("xl", "workbook") if fmt == "xlsx" else ("word", "document")
    mime = XLSX if fmt == "xlsx" else DOCX
    return (
        f'<Types xmlns="{CT}">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/{folder}/{main}.xml" ContentType="{mime}.main+xml"/>'
        "</Types>"
    )


def office_rels(target):
    return (
        f'<Relationships xmlns="{RELS}">'
        f'<Relationship Id="root" Type="{R}/officeDocument" Target="{target}"/>'
        "</Relationships>"
    )


def workbook_parts():
    return {
        "[Content_Types].xml": content_types("xlsx"),
        "_rels/.rels": office_rels("xl/workbook.xml"),
        "xl/workbook.xml": (
            f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets>'
            '<sheet name="Visible" sheetId="1" r:id="rA"/>'
            '<sheet name="Hidden" sheetId="2" state="hidden" r:id="rB"/>'
            '<sheet name="Deep hidden" sheetId="3" state="veryHidden" r:id="rC"/>'
            "</sheets></workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            f'<Relationships xmlns="{RELS}">'
            f'<Relationship Id="rA" Type="{R}/worksheet" Target="worksheets/custom.xml"/>'
            f'<Relationship Id="rB" Type="{R}/worksheet" Target="/xl/worksheets/second.xml"/>'
            f'<Relationship Id="rC" Type="{R}/worksheet" Target="worksheets/third%20sheet.xml"/>'
            "</Relationships>"
        ),
        "xl/worksheets/custom.xml": (
            f'<worksheet xmlns="{S}"><dimension ref="B2:Z900"/>'
            '<cols><col min="2" max="4" hidden="true"/><col min="6" max="6" hidden="0"/></cols>'
            '<sheetData><row r="2" hidden="1"><c r="B2"><f>1+1</f><v>2</v></c></row>'
            '<row r="3"/><row hidden="true"/></sheetData>'
            '<mergeCells><mergeCell ref="B2:D2"/></mergeCells></worksheet>'
        ),
        "xl/worksheets/second.xml": f'<worksheet xmlns="{S}"><sheetData/></worksheet>',
        "xl/worksheets/third sheet.xml": f'<worksheet xmlns="{S}"><sheetData/></worksheet>',
    }


def document_parts():
    return {
        "[Content_Types].xml": content_types("docx"),
        "_rels/.rels": office_rels("word/document.xml"),
        "word/document.xml": (
            f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>Example</w:t>'
            "</w:r></w:p></w:body></w:document>"
        ),
        "word/media/image1.png": b"media is inventoried, not decoded",
        "docProps/app.xml": "<Properties><Pages>42</Pages></Properties>",
    }


def pdf(pages=2, password=None):
    stream = BytesIO()
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    if password is not None:
        writer.encrypt(password, algorithm="RC4-128")
    writer.write(stream)
    return stream.getvalue()


def picture(fmt="PNG", size=(12, 8)):
    stream = BytesIO()
    with Image.new("RGB", size, "red") as img:
        img.save(stream, format=fmt)
    return stream.getvalue()


def encrypted_cfb():
    raw = bytearray(CFB + b"\0" * (1024 - len(CFB)))
    for index, name in enumerate(("EncryptionInfo", "EncryptedPackage")):
        offset = 512 + index * 128
        encoded = (name + "\0").encode("utf-16-le")
        raw[offset : offset + len(encoded)] = encoded
        struct.pack_into("<H", raw, offset + 64, len(encoded))
        raw[offset + 66] = 2
    return bytes(raw)


class InspectionTests(unittest.TestCase):
    def error(self, code, raw, filename, mime, limits=None):
        with self.assertRaises(MaterialError) as caught:
            inspect_material(raw, filename, mime, limits)
        self.assertEqual(caught.exception.issue.code, code)
        self.assertTrue(caught.exception.issue.message)
        return caught.exception.issue.details

    def test_pdf_page_count_route_and_exact_limit(self):
        result = inspect_material(pdf(), "TEST.PDF", "application/pdf", Limits(max_pdf_pages=2))
        self.assertEqual((result.format, result.route), ("pdf", "docling"))
        self.assertEqual(result.scope["page_count"], 2)
        self.assertEqual(result.scope["content_coverage"], "not_assessed")
        self.assertFalse(result.scope["encrypted"])

    def test_pdf_limit_reports_actual_limit_resource(self):
        details = self.error(
            "limit_exceeded", pdf(3), "a.pdf", "application/pdf", Limits(max_pdf_pages=2)
        )
        self.assertEqual(
            (details["actual"], details["limit"], details["resource"]), (3, 2, "pdf_pages")
        )

    def test_encrypted_pdf_including_empty_password_is_blocked(self):
        for password in ("secret", ""):
            with self.subTest(password=password):
                details = self.error(
                    "encrypted", pdf(password=password), "a.pdf", "application/pdf"
                )
                self.assertIsNone(details["page_count"])

    def test_corrupt_pdf_does_not_invent_zero_pages(self):
        details = self.error("corrupt", b"%PDF-1.7\ntruncated", "a.pdf", "application/pdf")
        self.assertIsNone(details["page_count"])

    def test_pdf_missing_page_tree_does_not_return_zero_or_leak_attribute_error(self):
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        del writer.root_object["/Pages"]
        stream = BytesIO()
        writer.write(stream)
        details = self.error("corrupt", stream.getvalue(), "a.pdf", "application/pdf")
        self.assertIsNone(details["page_count"])

    def test_xlsx_registers_relationships_and_structural_ranges(self):
        result = inspect_material(package(workbook_parts()), "a.xlsx", XLSX)
        self.assertEqual((result.format, result.route), ("xlsx", "docling"))
        self.assertEqual(result.scope["sheet_count"], 3)
        sheets = result.scope["sheets"]
        self.assertEqual([s["state"] for s in sheets], ["visible", "hidden", "veryHidden"])
        self.assertEqual([s["name"] for s in sheets], ["Visible", "Hidden", "Deep hidden"])
        self.assertEqual(sheets[0]["part"], "xl/worksheets/custom.xml")
        self.assertEqual(sheets[2]["part"], "xl/worksheets/third sheet.xml")
        self.assertEqual(sheets[0]["dimension"], "B2:Z900")
        self.assertEqual(sheets[0]["dimension_source"], "worksheet_declared")
        self.assertEqual(sheets[0]["hidden_rows"], [2, None])
        self.assertEqual(sheets[0]["hidden_columns"], [{"min": 2, "max": 4}])
        self.assertEqual(sheets[0]["merged_ranges"], ["B2:D2"])
        self.assertIsNone(sheets[1]["dimension"])
        self.assertEqual(result.scope["formula_cache"], "not_assessed")
        self.assertEqual(result.scope["nonempty_cell_coverage"], "not_assessed")
        self.assertIn("coverage_unverified", [i.code for i in result.issues])
        self.assertNotIn("1+1", repr(result.scope))

    def test_xlsx_missing_parts_and_invalid_structures(self):
        for part in (
            "[Content_Types].xml",
            "_rels/.rels",
            "xl/workbook.xml",
            "xl/_rels/workbook.xml.rels",
            "xl/worksheets/custom.xml",
        ):
            with self.subTest(part=part):
                parts = workbook_parts()
                del parts[part]
                self.error("corrupt", package(parts), "a.xlsx", XLSX)
        for before, after in (
            ('hidden="1"', 'hidden="maybe"'),
            ('min="2"', 'min="5"'),
            ('r="2"', 'r="0"'),
        ):
            with self.subTest(after=after):
                parts = workbook_parts()
                parts["xl/worksheets/custom.xml"] = parts["xl/worksheets/custom.xml"].replace(
                    before, after
                )
                self.error("corrupt", package(parts), "a.xlsx", XLSX)

    def test_xlsx_rejects_missing_sheet_relationship_and_external_worksheet(self):
        for replacement in ('Id="other"', 'Id="rA" TargetMode="External"'):
            with self.subTest(replacement=replacement):
                parts = workbook_parts()
                parts["xl/_rels/workbook.xml.rels"] = parts["xl/_rels/workbook.xml.rels"].replace(
                    'Id="rA"',
                    replacement,
                )
                self.error("corrupt", package(parts), "a.xlsx", XLSX)

    def test_xlsx_strict_namespace_and_deflate(self):
        parts = workbook_parts()
        parts = {
            name: data.replace(S, "http://purl.oclc.org/ooxml/spreadsheetml/main").replace(
                R, "http://purl.oclc.org/ooxml/officeDocument/relationships"
            )
            for name, data in parts.items()
        }
        result = inspect_material(package(parts, zipfile.ZIP_DEFLATED), "a.xlsx", XLSX)
        self.assertEqual(result.scope["sheet_count"], 3)

    def test_docx_core_parts_media_and_unknown_pages(self):
        result = inspect_material(package(document_parts()), "a.docx", DOCX)
        self.assertEqual((result.format, result.route), ("docx", "docling"))
        self.assertIsNone(result.scope["page_count"])
        self.assertEqual(result.scope["media_parts"], ["word/media/image1.png"])
        self.assertEqual(result.scope["content_coverage"], "not_assessed")

    def test_docx_missing_core_parts_or_body(self):
        for part in ("[Content_Types].xml", "_rels/.rels", "word/document.xml"):
            with self.subTest(part=part):
                parts = document_parts()
                del parts[part]
                self.error("corrupt", package(parts), "a.docx", DOCX)
        parts = document_parts()
        parts["word/document.xml"] = f'<w:document xmlns:w="{W}"/>'
        self.error("corrupt", package(parts), "a.docx", DOCX)

    def test_external_hyperlink_is_registered_without_fetch(self):
        parts = document_parts()
        parts["word/_rels/document.xml.rels"] = (
            f'<Relationships xmlns="{RELS}"><Relationship Id="link" Type="{R}/hyperlink" '
            'Target="https://invalid.example/never-fetch" TargetMode="External"/></Relationships>'
        )
        with patch("urllib.request.urlopen", side_effect=AssertionError("network access")):
            result = inspect_material(package(parts), "a.docx", DOCX)
        self.assertEqual(result.scope["external_relationship_count"], 1)
        self.assertFalse(result.scope["external_relationships_followed"])

    def test_external_main_relationship_is_rejected(self):
        parts = document_parts()
        parts["_rels/.rels"] = parts["_rels/.rels"].replace(
            'Id="root"', 'Id="root" TargetMode="External"'
        )
        self.error("corrupt", package(parts), "a.docx", DOCX)

    def test_zip_member_count_and_total_unpacked_bytes_limits(self):
        parts = document_parts()
        count = len(parts)
        size = sum(len(p.encode() if isinstance(p, str) else p) for p in parts.values())
        raw = package(parts, zipfile.ZIP_DEFLATED)
        details = self.error(
            "limit_exceeded", raw, "a.docx", DOCX, Limits(max_archive_entries=count - 1)
        )
        self.assertEqual((details["actual"], details["limit"]), (count, count - 1))
        details = self.error(
            "limit_exceeded", raw, "a.docx", DOCX, Limits(max_unpacked_bytes=size - 1)
        )
        self.assertEqual((details["actual"], details["limit"]), (size, size - 1))
        inspect_material(
            raw, "a.docx", DOCX, Limits(max_archive_entries=count, max_unpacked_bytes=size)
        )

    def test_duplicate_zip_member_is_rejected(self):
        parts = list(document_parts().items())
        parts.append(parts[0])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            raw = package(parts)
        self.error("corrupt", raw, "a.docx", DOCX)

    def test_zip_crc_corruption_in_unparsed_media_is_rejected(self):
        raw = package(document_parts())
        self.assertIn(b"media is inventoried", raw)
        raw = raw.replace(b"media is inventoried", b"Media is inventoried", 1)
        self.error("corrupt", raw, "a.docx", DOCX)

    def test_encrypted_zip_member_is_rejected(self):
        raw = bytearray(package(document_parts()))
        central = raw.index(b"PK\x01\x02")
        struct.pack_into("<H", raw, 6, 1)
        struct.pack_into("<H", raw, central + 8, 1)
        self.error("encrypted", bytes(raw), "a.docx", DOCX)

    def test_zip_unsafe_names_and_relationship_traversal(self):
        for name in ("../outside.xml", "/absolute.xml", "word/../alias.xml", "word\\alias.xml"):
            with self.subTest(name=name):
                parts = document_parts()
                # Windows ZipInfo normalizes backslashes when writing. Patch both
                # headers afterwards so this fixture retains the malformed name.
                parts[name.replace("\\", "/")] = "<root/>"
                raw = package(parts)
                if "\\" in name:
                    raw = raw.replace(name.replace("\\", "/").encode(), name.encode())
                self.error("corrupt", raw, "a.docx", DOCX)
        parts = document_parts()
        parts["_rels/.rels"] = office_rels("../../outside.xml")
        self.error("corrupt", package(parts), "a.docx", DOCX)

    def test_zip_unsupported_compression_and_corruption(self):
        self.error(
            "unsupported_format", package(document_parts(), zipfile.ZIP_BZIP2), "a.docx", DOCX
        )
        self.error("corrupt", package(document_parts())[:-30], "a.docx", DOCX)
        self.error("corrupt", b"PK\x03\x04invalid", "a.xlsx", XLSX)

    def test_doctype_and_entities_rejected_in_all_xml_encodings(self):
        payloads = (
            '<!DOCTYPE root [<!ENTITY x "expanded">]><root>&x;</root>',
            '<!DOCTYPE root SYSTEM "file:///never-read"><root/>',
            '<!DOCTYPE root [<!ENTITY x SYSTEM "https://invalid.example/x">]><root>&x;</root>',
        )
        for payload in payloads:
            for encoding in ("utf-8", "utf-16", "utf-16-be"):
                with self.subTest(payload=payload, encoding=encoding):
                    parts = document_parts()
                    parts["word/unused.xml"] = payload.encode(encoding)
                    self.error("corrupt", package(parts), "a.docx", DOCX)
        parts = document_parts()
        parts["word/unused.xml"] = "<root>"
        self.error("corrupt", package(parts), "a.docx", DOCX)

    def test_ordinary_zip_is_unsupported_not_an_empty_document(self):
        raw = package({"readme.txt": "example"})
        self.error("unsupported_format", raw, "a.zip", "application/zip")
        self.error("format_mismatch", raw, "a.xlsx", XLSX)

    def test_ooxml_content_type_mismatch(self):
        parts = document_parts()
        parts["[Content_Types].xml"] = parts["[Content_Types].xml"].replace(
            DOCX + ".main+xml",
            "application/vnd.ms-word.document.macroEnabled.main+xml",
        )
        self.error("format_mismatch", package(parts), "a.docx", DOCX)
        self.error("format_mismatch", package(document_parts()), "a.xlsx", XLSX)

    def test_png_jpeg_integrity_dimensions_and_pixel_limit(self):
        for fmt, extension, mime in (("PNG", "png", "image/png"), ("JPEG", "jpg", "image/jpeg")):
            with self.subTest(fmt=fmt):
                raw = picture(fmt)
                result = inspect_material(raw, f"a.{extension}", mime, Limits(max_image_pixels=96))
                self.assertEqual((result.scope["width"], result.scope["height"]), (12, 8))
                self.assertEqual(result.scope["pixel_count"], 96)
                self.assertEqual(result.route, "docling")
                self.assertFalse(result.scope["animated"])
                details = self.error(
                    "limit_exceeded", raw, f"a.{extension}", mime, Limits(max_image_pixels=95)
                )
                self.assertEqual((details["actual"], details["limit"]), (96, 95))
                self.error("corrupt", raw[:-20], f"a.{extension}", mime)

    def test_png_bad_crc_is_corrupt(self):
        raw = bytearray(picture())
        raw[29] ^= 1  # IHDR checksum.
        self.error("corrupt", bytes(raw), "a.png", "image/png")

    def test_decoder_bomb_error_preserves_actual_and_limit(self):
        raw = bytearray(picture())
        struct.pack_into(">II", raw, 16, 20000, 20000)
        struct.pack_into(">I", raw, 29, zlib.crc32(raw[12:29]))
        details = self.error("limit_exceeded", bytes(raw), "a.png", "image/png")
        self.assertEqual(details["actual"], 400_000_000)
        self.assertEqual(details["limit"], Limits().max_image_pixels)

    def test_animated_png_is_blocked_before_frame_decode(self):
        stream = BytesIO()
        with Image.new("RGB", (4, 4), "red") as first, Image.new("RGB", (4, 4), "blue") as second:
            first.save(stream, format="PNG", save_all=True, append_images=[second], duration=100)
        with patch(
            "PIL.PngImagePlugin.PngImageFile.load", side_effect=AssertionError("decoded APNG")
        ):
            details = self.error("unsupported_format", stream.getvalue(), "a.png", "image/png")
        self.assertEqual(details["frame_count"], 2)
        self.assertTrue(details["animated"])

    def test_utf8_plain_text_html_fragments_and_empty_text(self):
        for content in ("中文正文", "<div>HTML source</div>", "", "\ufeffUTF-8 BOM"):
            with self.subTest(content=content):
                result = inspect_material(content.encode(), "a.txt", "text/plain; charset=UTF-8")
                self.assertEqual((result.format, result.route), ("text", "text"))
                self.assertEqual(result.scope["character_count"], len(content))
        for content in (b"ordinary text fragment", b"<p>fragment", b"<!DOCTYPE html><html></html>"):
            self.assertEqual(inspect_material(content, "a.html", "text/html").route, "docling")

    def test_invalid_encoding_and_binary_signature_disguised_as_text(self):
        self.error("invalid_encoding", b"\xff\xfe", "a.txt", "text/plain")
        self.error("invalid_encoding", b"\xc3\x28", "a.html", "text/html")
        self.error("format_mismatch", b"a\x00b", "a.txt", "text/plain")
        self.error("format_mismatch", pdf(), "a.txt", "text/plain")
        self.error("format_mismatch", picture(), "a.html", "text/html")

    def test_extension_mime_and_signature_conflicts(self):
        for raw, filename, mime in (
            (pdf(), "a.pdf", "image/png"),
            (picture(), "a.jpg", "image/jpeg"),
            (pdf(), "a.png", "image/png"),
            (pdf(), "a.bin", "application/pdf"),
            (pdf(), "a.pdf", "application/x-other"),
            (b"not a pdf", "a.pdf", "application/pdf"),
        ):
            with self.subTest(filename=filename, mime=mime):
                self.error("format_mismatch", raw, filename, mime)

    def test_generic_mime_missing_extension_and_case_normalization(self):
        self.assertEqual(inspect_material(pdf(), "a.PDF", "application/octet-stream").format, "pdf")
        self.assertEqual(inspect_material(pdf(), "", "APPLICATION/PDF").format, "pdf")
        self.assertEqual(inspect_material(b"text", "", "text/plain").route, "text")

    def test_legacy_xls_is_only_identified_and_requires_conversion(self):
        result = inspect_material(CFB + b"\0" * 504, "a.xls", XLS)
        self.assertIsNone(result.route)
        self.assertIsNone(result.scope["sheet_count"])
        self.assertEqual(result.issues[0].code, "conversion_required")
        self.assertEqual(result.scope["content_coverage"], "not_assessed")
        self.error("format_mismatch", CFB + b"\0" * 504, "a.xlsx", XLSX)
        self.error("corrupt", CFB, "a.xls", XLS)

    def test_ole_encrypted_ooxml_signatures(self):
        for extension, mime in (("docx", DOCX), ("xlsx", XLSX)):
            with self.subTest(extension=extension):
                self.error("encrypted", encrypted_cfb(), f"a.{extension}", mime)
        self.error("format_mismatch", encrypted_cfb(), "a.xls", XLS)

    def test_other_formats_are_explicitly_unsupported(self):
        self.error("unsupported_format", b"GIF89a", "a.gif", "image/gif")
        self.error("unsupported_format", b"arbitrary", "a.bin", "application/octet-stream")

    def test_material_size_limit_reports_actual_and_limit(self):
        details = self.error(
            "limit_exceeded", b"1234", "a.txt", "text/plain", Limits(max_material_bytes=3)
        )
        self.assertEqual(
            (details["actual"], details["limit"], details["resource"]), (4, 3, "material_bytes")
        )

    def test_programming_errors_are_not_swallowed(self):
        for error in (RuntimeError("bug"), TypeError("bug"), ValueError("bug")):
            with self.subTest(error=type(error).__name__):
                with patch("doccase_preprocessing.inspection.PdfReader", side_effect=error):
                    with self.assertRaises(type(error)):
                        inspect_material(b"%PDF-1.7", "a.pdf", "application/pdf")

    def test_unexpected_page_enumeration_typeerror_is_not_swallowed(self):
        with patch(
            "pypdf.PdfReader.pages",
            new_callable=PropertyMock,
            side_effect=TypeError("unexpected page enumeration bug"),
        ):
            with self.assertRaisesRegex(TypeError, "unexpected page enumeration bug"):
                inspect_material(pdf(), "a.pdf", "application/pdf")


if __name__ == "__main__":
    unittest.main()
