"""Isolated, offline Docling READ worker for a verified content object."""

import hashlib
import importlib.metadata
import json
import logging
import platform
import re
import socket
import sys
from io import BytesIO
from pathlib import Path
from time import time_ns


def package_versions():
    return {
        "python": platform.python_version(),
        "docling": importlib.metadata.version("docling"),
        "docling_core": importlib.metadata.version("docling-core"),
        "docling_parse": importlib.metadata.version("docling-parse"),
        "openpyxl": importlib.metadata.version("openpyxl"),
        "pypdfium2": importlib.metadata.version("pypdfium2"),
    }


def ocr_package_versions():
    return {
        **package_versions(),
        "rapidocr": importlib.metadata.version("rapidocr"),
        "onnxruntime": importlib.metadata.version("onnxruntime"),
        "torch": importlib.metadata.version("torch"),
        "transformers": importlib.metadata.version("transformers"),
        "docling_ibm_models": importlib.metadata.version("docling-ibm-models"),
        "safetensors": importlib.metadata.version("safetensors"),
        "pillow": importlib.metadata.version("pillow"),
    }


def disable_network():
    def unavailable(*args, **kwargs):
        raise RuntimeError("Network access is disabled for this local READ worker")

    socket.create_connection = unavailable
    socket.socket.connect = unavailable
    socket.socket.connect_ex = unavailable


def read_verified(root_path, content_path, expected_sha256):
    root = Path(root_path).resolve(strict=True)
    relative = Path(content_path)
    if relative.is_absolute():
        raise ValueError("Content path must be relative")
    target = (root / relative).resolve(strict=True)
    if not target.is_relative_to(root):
        raise ValueError("Content path escapes the trusted root")
    raw = target.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("Content SHA-256 mismatch at READ")
    return raw


def inspect_pdf_scan(request):
    """Use only text-layer and raster geometry; do not render or run OCR."""
    import pypdfium2
    from pypdfium2.raw import FPDF_PAGEOBJ_IMAGE

    raw = read_verified(request["content_root"], request["content_path"], request["sha256"])
    document = pypdfium2.PdfDocument(raw)
    try:
        if not 1 <= len(document) <= 12:
            return {"scan": "page_limit"}
        for i in range(len(document)):
            page = document[i]
            try:
                text_page = page.get_textpage()
                try:
                    if text_page.get_text_bounded().replace("\x00", "").strip():
                        return {"scan": "has_text_layer"}
                finally:
                    text_page.close()
                left, bottom, right, top = page.get_bbox()
                area = (right - left) * (top - bottom)
                if not area > 0:
                    return {"scan": "uncertain"}
                for image in page.get_objects(filter=[FPDF_PAGEOBJ_IMAGE], max_depth=1):
                    x1, y1, x2, y2 = image.get_bounds()
                    overlap = max(0, min(right, x2) - max(left, x1)) * max(
                        0, min(top, y2) - max(bottom, y1)
                    )
                    if overlap >= 0.72 * area:
                        break
                else:
                    return {"scan": "uncertain"}
            finally:
                page.close()
        return {"scan": "image_only"}
    finally:
        document.close()


def ocr_page_summary(pages):
    return [
        {
            "page_index": page.page_no,
            "intermediate_retained": page.parsed_page is not None,
            "text_cells": len(page.cells) if page.parsed_page is not None else None,
            "ocr_cells": (
                sum(cell.from_ocr for cell in page.cells) if page.parsed_page is not None else None
            ),
        }
        for page in pages
    ]


def excel_evidence(raw, document, sheet_name):
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter

    workbook = load_workbook(BytesIO(raw), read_only=True, data_only=False, keep_links=False)
    sheet_metadata = [
        {"sheet_name": sheet.title, "visibility": sheet.sheet_state}
        for sheet in workbook.worksheets
    ]
    worksheet = workbook[sheet_name]
    if worksheet.sheet_state != "visible":
        raise ValueError("Selected comparison worksheet is not visible")
    coordinates = ("A11", "B11", "C11", "D11", "E11", "B12")
    original = {
        coordinate: {
            "value": str(worksheet[coordinate].value),
            "data_type": worksheet[coordinate].data_type,
        }
        for coordinate in coordinates
    }
    workbook.close()
    if any(item["data_type"] == "f" for item in original.values()):
        raise ValueError("The selected comparison cells must not be formulas")

    output = {}
    ranges = []
    for table in document.get("tables", []):
        if not table.get("prov"):
            continue
        provenance = table["prov"][0]
        bbox = provenance["bbox"]
        if provenance["page_no"] != 1 or bbox.get("coord_origin") != "TOPLEFT":
            continue
        left, top, right, bottom = (int(bbox[key]) for key in ("l", "t", "r", "b"))
        ranges.append(
            {
                "sheet_name": sheet_name,
                "range": f"{get_column_letter(left + 1)}{top + 1}:{get_column_letter(right)}{bottom}",
                "table_ref": table["self_ref"],
                "basis": "Docling sheet-page zero-based table bounding box",
            }
        )
        for cell_index, cell in enumerate(table["data"]["table_cells"]):
            row = top + cell["start_row_offset_idx"] + 1
            column = left + cell["start_col_offset_idx"] + 1
            coordinate = f"{get_column_letter(column)}{row}"
            if coordinate in original:
                output.setdefault(coordinate, []).append(
                    {
                        "value": cell["text"],
                        "ref": f"{table['self_ref']}/data/table_cells/{cell_index}",
                    }
                )
    comparison = []
    for coordinate, source in original.items():
        candidates = output.get(coordinate, [])
        source_value = " ".join(source["value"].split())
        matched = any(" ".join(item["value"].split()) == source_value for item in candidates)
        comparison.append(
            {
                "coordinate": coordinate,
                "source_value": source["value"],
                "source_data_type": source["data_type"],
                "docling_values": candidates,
                "match": matched,
            }
        )
    processed_sheets = [
        group["name"] for group in document.get("groups", []) if group.get("label") == "sheet"
    ]
    return {
        "source_sheet_metadata": sheet_metadata,
        "selected_sheet": sheet_name,
        "processed_sheet_names": processed_sheets,
        "unread_sheet_names": [
            item["sheet_name"]
            for item in sheet_metadata
            if item["sheet_name"] not in processed_sheets
        ],
        "docling_table_ranges": ranges,
        "cell_comparison": comparison,
        "all_sampled_cells_match": all(item["match"] for item in comparison),
        "formula_execution": False,
        "hidden_sheet_read_claimed": False,
        "unknown_scope": [
            "Cell coverage outside the sampled comparison is not established.",
            "Formulas are not evaluated and cached results are not independently verified.",
        ],
    }


def convert(request, *, timing=None):
    declared = request["versions"]
    profile = request["profile"]
    ocr_pdf = request["format"] == "pdf" and profile.get("pipeline") == "StandardPdfPipeline"
    actual = ocr_package_versions() if ocr_pdf else package_versions()
    if any(actual.get(key) != value for key, value in declared.items()):
        raise ValueError("Declared Docling environment version differs from runtime")
    raw = read_verified(request["content_root"], request["content_path"], request["sha256"])

    if request["format"] == "text":
        from docling_core.types.doc import DocItemLabel, DoclingDocument

        document = DoclingDocument(name="verified.txt")
        document.add_text(label=DocItemLabel.TEXT, text=raw.decode("utf-8-sig"))
        target = Path(request["json_path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(document.export_to_dict(), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        return {
            "conversion_status": "success",
            "errors": [],
            "versions": actual,
            "input_sha256_reverified": hashlib.sha256(raw).hexdigest(),
            "json_path": str(target),
            "json_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "document_pages": [],
            "text_items": 1,
            "table_items": 0,
        }

    from docling.datamodel.accelerator_options import AcceleratorOptions
    from docling.datamodel.backend_options import (
        HTMLBackendOptions,
        MarkdownBackendOptions,
        MsExcelBackendOptions,
    )
    from docling.datamodel.base_models import DocumentStream, InputFormat
    from docling.datamodel.pipeline_options import (
        ConvertPipelineOptions,
        NativePdfPipelineOptions,
        PdfPipelineOptions,
        RapidOcrOptions,
        TableFormerMode,
        TableStructureOptions,
        TextCellUnit,
    )
    from docling.document_converter import (
        DocumentConverter,
        ExcelFormatOption,
        FormatOption,
        HTMLFormatOption,
        NativePdfFormatOption,
        PdfFormatOption,
    )

    common = {
        "accelerator_options": AcceleratorOptions(device="cpu", num_threads=2),
        "enable_remote_services": False,
        "allow_external_plugins": False,
        "artifacts_path": profile["cache_path"],
        "do_picture_classification": False,
        "do_picture_description": False,
        "do_chart_extraction": False,
    }
    if profile.get("document_timeout") is not None:
        common["document_timeout"] = profile["document_timeout"]
    format_name = request["format"]
    if ocr_pdf:
        selected = InputFormat.PDF
        options = PdfFormatOption(
            pipeline_options=PdfPipelineOptions(
                **common,
                do_ocr=profile.get("do_ocr", True),
                ocr_options=RapidOcrOptions(
                    lang=["ch"],
                    backend="onnxruntime",
                    model_size="small",
                    force_full_page_ocr=profile.get("force_full_page_ocr", False),
                ),
                do_table_structure=profile.get("do_table_structure", True),
                table_structure_options=TableStructureOptions(
                    mode=TableFormerMode.ACCURATE, do_cell_matching=True
                ),
                generate_page_images=False,
                generate_picture_images=False,
                generate_parsed_pages=True,
            )
        )
    elif format_name == "pdf":
        selected = InputFormat.PDF
        options = NativePdfFormatOption(
            pipeline_options=NativePdfPipelineOptions(
                **common,
                parser_threads=2,
                text_cell_unit=TextCellUnit(profile["text_cell_unit"]),
                generate_page_images=False,
                generate_picture_images=False,
            )
        )
    elif format_name == "xlsx":
        selected = InputFormat.XLSX
        options = ExcelFormatOption(
            pipeline_options=ConvertPipelineOptions(**common),
            backend_options=MsExcelBackendOptions(
                sheet_names=[profile["sheet_name"]],
                parse_charts=False,
                enable_remote_fetch=False,
                enable_local_fetch=False,
            ),
        )
    elif format_name == "html":
        selected = InputFormat.HTML
        options = HTMLFormatOption(
            pipeline_options=ConvertPipelineOptions(**common),
            backend_options=HTMLBackendOptions(
                enable_remote_fetch=False,
                enable_local_fetch=False,
                fetch_images=False,
                render_page=False,
                infer_furniture=False,
                add_title=False,
            ),
        )
    elif format_name == "markdown":
        from docling.backend.md_backend import MarkdownDocumentBackend
        from docling.pipeline.simple_pipeline import SimplePipeline

        selected = InputFormat.MD
        options = FormatOption(
            pipeline_cls=SimplePipeline,
            backend=MarkdownDocumentBackend,
            pipeline_options=ConvertPipelineOptions(**common),
            backend_options=MarkdownBackendOptions(
                enable_remote_fetch=False, enable_local_fetch=False, fetch_images=False
            ),
        )
    else:
        raise ValueError("No Docling reader was bound for the requested format")
    converter = DocumentConverter(allowed_formats=[selected], format_options={selected: options})
    page_range = request["requested_scope"].get("page_range") if format_name == "pdf" else None
    conversion_options = {"page_range": tuple(page_range)} if page_range else {}
    if timing is not None:
        timing["convert_started_ns"] = time_ns()
    result = converter.convert(
        DocumentStream(
            name=f"verified.{'md' if format_name == 'markdown' else format_name}",
            stream=BytesIO(raw),
        ),
        raises_on_error=False,
        **conversion_options,
    )
    if timing is not None:
        timing["convert_finished_ns"] = time_ns()
    status = result.status.value
    errors = []
    for error in result.errors:
        item = error.model_dump(mode="json")
        if item.get("error_message"):
            item["error_message"] = re.sub(
                r"https?://\S+", "[reference withheld]", item["error_message"]
            )
        errors.append(item)
    response = {
        "conversion_status": status,
        "errors": errors,
        "versions": actual,
        "input_sha256_reverified": hashlib.sha256(raw).hexdigest(),
        "json_path": None,
        "json_sha256": None,
    }
    if any(
        "timeout" in str(item.get("category", "")).lower()
        or "timeout" in str(item.get("error_message", "")).lower()
        for item in errors
    ):
        response["termination_reason"] = "timeout"
    if ocr_pdf:
        pipeline_options = options.pipeline_options
        response["effective_ocr_configuration"] = {
            "pipeline": type(pipeline_options).__name__,
            "do_ocr": pipeline_options.do_ocr,
            "ocr_backend": pipeline_options.ocr_options.backend,
            "ocr_lang": pipeline_options.ocr_options.lang,
            "ocr_mode": pipeline_options.ocr_options.mode.value,
            "ocr_scale": pipeline_options.ocr_options.scale,
            "ocr_model_size": pipeline_options.ocr_options.model_size,
            "do_table_structure": pipeline_options.do_table_structure,
            "table_mode": pipeline_options.table_structure_options.mode.value,
            "do_cell_matching": pipeline_options.table_structure_options.do_cell_matching,
            "generate_parsed_pages": pipeline_options.generate_parsed_pages,
            "layout_model": pipeline_options.layout_options.model_spec.model_dump(mode="json"),
        }
        response["ocr_page_cells"] = ocr_page_summary(result.pages)
    if status in {"success", "partial_success"}:
        target = Path(request["json_path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        document = result.document.export_to_dict()
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        response["json_path"] = str(target)
        response["json_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
        response["document_pages"] = sorted(int(page) for page in document.get("pages", {}))
        response["text_items"] = len(document.get("texts", []))
        response["table_items"] = len(document.get("tables", []))
        if ocr_pdf:
            cells_path = target.with_name(target.stem + ".ocr-cells.json")
            cell_pages = [
                {
                    "page_no": page.page_no,
                    "cells": (
                        [cell.model_dump(mode="json") for cell in page.cells]
                        if page.parsed_page is not None
                        else None
                    ),
                }
                for page in result.pages
            ]
            with cells_path.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(cell_pages, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            response["ocr_cells_ref"] = {
                "path": str(cells_path),
                "sha256": hashlib.sha256(cells_path.read_bytes()).hexdigest(),
            }
        if format_name == "pdf":
            import pypdfium2

            source_pdf = pypdfium2.PdfDocument(raw)
            preview_page = page_range[0] if page_range else 1
            source_page = source_pdf[preview_page - 1]
            bitmap = source_page.render(scale=1.5)
            preview_path = target.with_name(target.stem + f".source-page-{preview_page}.png")
            with preview_path.open("xb") as stream:
                bitmap.to_pil().save(stream, format="PNG")
            bitmap.close()
            source_page.close()
            source_pdf.close()
            response["source_page_preview"] = {
                "page": preview_page,
                "scale": 1.5,
                "renderer": "pypdfium2",
                "path": str(preview_path),
                "sha256": hashlib.sha256(preview_path.read_bytes()).hexdigest(),
            }
        if format_name == "xlsx":
            response["excel_evidence"] = excel_evidence(raw, document, profile["sheet_name"])
    return response


def markdown_stream():
    from p02_markdown_export import export_saved_read

    for line in sys.stdin:
        envelope = json.loads(line)
        request = envelope["request"]
        if request.get("operation") != "export_markdown":
            result = {"state": "failure", "reason": "invalid_markdown_request"}
        else:
            try:
                result = export_saved_read(request)
            except (OSError, TypeError, ValueError, KeyError):
                result = {"state": "failure", "reason": "markdown_request_failed"}
        print(json.dumps({"id": envelope["id"], "result": result}, ensure_ascii=False), flush=True)


def main():
    entry = time_ns()
    logging.basicConfig(level=logging.ERROR)
    disable_network()
    if sys.argv[1:] == ["--markdown-stream"]:
        markdown_stream()
        return
    request = json.load(sys.stdin)
    if request["operation"] == "probe":
        response = package_versions()
    elif request["operation"] == "probe_ocr":
        response = ocr_package_versions()
    elif request["operation"] == "inspect_pdf_scan":
        response = inspect_pdf_scan(request)
    elif request["operation"] == "pdf_page_count":
        import pypdfium2

        raw = read_verified(request["content_root"], request["content_path"], request["sha256"])
        document = pypdfium2.PdfDocument(raw)
        response = {"page_count": len(document)}
        document.close()
    elif request["operation"] == "export_markdown":
        from p02_markdown_export import export_saved_read

        response = export_saved_read(request)
    else:
        timing = {"worker_entered_ns": entry} if request.get("capture_timing") else None
        response = convert(request, timing=timing)
        if timing is not None:
            response["_worker_timing"] = {**timing, "worker_output_ready_ns": time_ns()}
    print(json.dumps(response, ensure_ascii=False))


if __name__ == "__main__":
    main()
