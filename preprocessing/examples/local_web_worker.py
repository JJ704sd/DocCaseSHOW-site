"""Offline readers for the workbench, run in the existing Docling environment."""

import base64
import json
import posixpath
import sys
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

from local_attachment_markdown import serialize
from p02_docling_worker import disable_network, read_verified


def image_ocr(raw, cache):
    from PIL import Image, ImageOps
    from rapidocr import OCRVersion, RapidOCR

    cache = Path(cache) / "RapidOcr"
    paths = {
        "Det.model_path": cache / "PP-OCRv6_det_small.onnx",
        "Rec.model_path": cache / "PP-OCRv6_rec_small.onnx",
        "Cls.model_path": cache / "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    }
    if not all(path.is_file() for path in paths.values()):
        return {"error": "ocr_models_missing"}
    engine = RapidOCR(
        params={
            **{key: str(path) for key, path in paths.items()},
            "Det.ocr_version": OCRVersion.PPOCRV6,
            "Rec.ocr_version": OCRVersion.PPOCRV6,
            "Global.log_level": "warning",
            "EngineConfig.onnxruntime.intra_op_num_threads": 2,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        }
    )
    with Image.open(BytesIO(raw)) as image:
        oriented = ImageOps.exif_transpose(image).convert("RGB")
        width, height = oriented.size
        result = engine(oriented)
    if result.txts is None:
        return {"lines": [], "size": [width, height]}
    return {
        "size": [width, height],
        "lines": [
            {"text": text, "box": box.tolist(), "score": float(score)}
            for text, box, score in zip(result.txts, result.boxes, result.scores, strict=True)
        ],
    }


def pdf_preview(raw, number):
    import pypdfium2 as pdfium

    with pdfium.PdfDocument(raw) as document:
        if not 1 <= number <= len(document):
            return {"error": "page_out_of_range"}
        page = document[number - 1]
        try:
            bitmap = page.render(scale=min(2, 1500 / max(page.get_size())))
            try:
                png = BytesIO()
                bitmap.to_pil().save(png, "PNG")
            finally:
                bitmap.close()
        finally:
            page.close()
    return {"png": base64.b64encode(png.getvalue()).decode("ascii")}


def xlsx_merges(raw, visible, max_rows, max_cols):
    """Read bounded layout metadata without switching the value reader out of read-only mode."""
    from openpyxl.utils.cell import range_boundaries

    budget, result = 8 * 1024 * 1024, {}
    with ZipFile(BytesIO(raw)) as archive:

        def xml(name):
            nonlocal budget
            try:
                info = archive.getinfo(name)
                if info.file_size > budget:
                    return None
                budget -= info.file_size
                return ElementTree.fromstring(archive.read(info))
            except (KeyError, ElementTree.ParseError):
                return None

        book, relations = xml("xl/workbook.xml"), xml("xl/_rels/workbook.xml.rels")
        if book is None or relations is None:
            return result
        targets = {
            rel.get("Id"): rel.get("Target")
            for rel in relations
            if rel.get("TargetMode") != "External" and rel.get("Type", "").endswith("/worksheet")
        }
        for sheet in book.findall(".//{*}sheet"):
            name = sheet.get("name")
            rel = next((v for k, v in sheet.attrib.items() if k.endswith("}id")), None)
            if name not in visible or not (target := targets.get(rel)):
                continue
            path = posixpath.normpath(
                target.lstrip("/") if target.startswith("/") else "xl/" + target
            )
            if not path.startswith("xl/") or (root := xml(path)) is None:
                continue
            merges = []
            for cell in root.findall(".//{*}mergeCell"):
                try:
                    c1, r1, c2, r2 = range_boundaries(cell.get("ref", ""))
                except ValueError:
                    continue
                if (
                    all(v is not None for v in (c1, r1, c2, r2))
                    and 1 <= r1 <= r2 <= max_rows
                    and 1 <= c1 <= c2 <= max_cols
                ):
                    merges.append({"row": r1, "col": c1, "row_end": r2, "col_end": c2})
            result[name] = merges
    return result


def xlsx_values(raw):
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter

    max_rows, max_cols, remaining = 300, 50, 30000
    workbook = load_workbook(BytesIO(raw), read_only=True, data_only=True, keep_links=False)
    sheets = []
    try:
        merges = xlsx_merges(
            raw,
            {s.title for s in workbook.worksheets if s.sheet_state == "visible"},
            max_rows,
            max_cols,
        )
        for worksheet in workbook.worksheets:
            if worksheet.sheet_state != "visible":
                continue
            rows = []
            layout = merges.get(worksheet.title, [])
            clipped = (worksheet.max_row or 0) > max_rows or (worksheet.max_column or 0) > max_cols
            width = max((m["col_end"] for m in layout), default=1)
            # Recompute the read range from actual cells; worksheet dimensions can be stale.
            worksheet.reset_dimensions()
            for index, row in enumerate(
                worksheet.iter_rows(max_row=max_rows + 1, max_col=max_cols + 1), 1
            ):
                if index > max_rows:
                    clipped |= any(cell.value is not None for cell in row)
                    break
                cells = {}
                for col, cell in enumerate(row, 1):
                    if cell.value is None:
                        continue
                    if col > max_cols or remaining <= 0:
                        clipped = True
                        continue
                    value = str(cell.value)
                    if len(value) > 2000:
                        clipped = True
                    cells[get_column_letter(col)] = {
                        "value": value[:2000],
                        "type": cell.data_type,
                        "clipped": len(value) > 2000,
                        **({"number_format": cell.number_format} if cell.data_type == "n" else {}),
                    }
                    width = max(width, col)
                    remaining -= 1
                if cells:
                    rows.append({"number": index, "cells": cells})
            # The bounded range cannot establish coverage beyond its outer edge.
            sheets.append(
                {
                    "name": worksheet.title,
                    "rows": rows,
                    "columns": [get_column_letter(c) for c in range(1, width + 1)],
                    "clipped": clipped,
                    "merges": layout,
                }
            )
    finally:
        workbook.close()
    return {"sheets": sheets}


if __name__ == "__main__":
    request = json.load(sys.stdin)
    disable_network()
    raw = read_verified(request["root"], request["path"], request["sha256"])
    if request["operation"] == "image_ocr":
        result = image_ocr(raw, request["cache"])
    elif request["operation"] == "pdf_preview":
        result = pdf_preview(raw, request["page"])
    elif request["operation"] == "xlsx_values":
        result = xlsx_values(raw)
    elif request["operation"] == "attachment_markdown":
        read = request["read"]
        if read["input_sha256"] != request["sha256"]:
            raise ValueError("Attachment READ is not bound to uploaded bytes")
        result = serialize(read)
    else:
        raise ValueError("Unknown workbench operation")
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
