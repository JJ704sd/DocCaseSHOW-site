"""Names and pre-read classification for verified PDF material routes."""

import re
import unicodedata

PDF_STRUCTURED = "DoclingPdfStructured"
PDF_SCANNED = "DoclingPdfScanned"
PDF_STRUCTURED_READERS = frozenset({PDF_STRUCTURED, PDF_SCANNED})
PDF_READERS = frozenset({"DoclingNativePdf", "DoclingPdfAdaptive", *PDF_STRUCTURED_READERS})

_PACKING = re.compile(
    r"(?<![a-z])packing[\s_.-]*list(?![a-z])|(?<![a-z0-9])pl(?![a-z0-9])|装箱单|包装清单", re.I
)
_SAFETY_SHEET = re.compile(
    r"(?<![a-z])m?sds(?![a-z])|safety[\s_.-]+data[\s_.-]+sheet|安全数据表", re.I
)
_INVOICE_TITLE = re.compile(r"(?:factura\s+)?invoice", re.I)
_PACKING_TITLE = re.compile(r"(?:albar[aá]n\s+)?packing\s+list", re.I)


def pdf_page_title(text):
    """Recognize explicit standalone titles, without classifying from a filename."""
    if _INVOICE_TITLE.fullmatch(text.strip()):
        return "invoice"
    if _PACKING_TITLE.fullmatch(text.strip()):
        return "packing"
    return None


def _name(filename):
    return unicodedata.normalize("NFKC", filename or "").replace("\\", "/").rsplit("/", 1)[-1]


def is_safety_sheet(filename):
    return bool(_SAFETY_SHEET.search(_name(filename)))


def pdf_first_read(filename, *, scan=None):
    """Keep the existing route unless a verified scan inspection identifies every page."""
    if is_safety_sheet(filename):
        return "native"
    if scan == "image_only":
        return "scanned_ocr"
    return "packing_table" if _PACKING.search(_name(filename)) else "native"
