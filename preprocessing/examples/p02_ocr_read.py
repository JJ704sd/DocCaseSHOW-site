"""Run a bounded local OCR READ on the selected P01 scanned PDF."""

import argparse
import hashlib
import json
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import p02_local_read as app
from pypdf import PdfReader

from doccase_preprocessing.local_handoff import receive_handoff

MODEL_FILES = {
    "ocr_detection": "RapidOcr/PP-OCRv6_det_small.onnx",
    "ocr_classification": "RapidOcr/ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    "ocr_recognition": "RapidOcr/PP-OCRv6_rec_small.onnx",
    "layout_weights": "docling-project--docling-layout-heron/model.safetensors",
    "layout_config": "docling-project--docling-layout-heron/config.json",
    "table_weights": "docling-project--docling-models/model_artifacts/tableformer/accurate/tableformer_accurate.safetensors",
    "table_config": "docling-project--docling-models/model_artifacts/tableformer/accurate/tm_config.json",
}
DECLARED_VERSIONS = {
    "python": "3.12.13",
    "docling": "2.145.0",
    "docling_core": "2.104.0",
    "docling_parse": "7.23.0",
    "openpyxl": "3.1.5",
    "pypdfium2": "5.14.0",
    "rapidocr": "3.10.0",
    "onnxruntime": "1.31.0",
    "torch": "2.14.1",
    "transformers": "5.20.2",
    "docling_ibm_models": "4.0.4",
    "safetensors": "0.8.0",
    "pillow": "13.0.0",
}
VISUAL_ANCHORS = (
    ("top", "SAFETY DATA SHEET", (0.01, 0.16)),
    ("middle", "Product information", (0.12, 0.35)),
    ("lower", "HAZARDS IDENTIFICATION", (0.36, 0.68)),
)


def file_sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def model_inventory(root, file_map=MODEL_FILES):
    root = Path(root)
    present = {}
    missing = []
    for label, relative in file_map.items():
        path = root / relative
        if not path.is_file():
            missing.append({"name": label, "relative_path": relative})
            continue
        present[label] = {
            "relative_path": relative,
            "sha256": file_sha256(path),
            "size_bytes": path.stat().st_size,
        }
    return present, missing


def anchor_evidence(document):
    page = document["pages"]["1"]
    height = page["size"]["height"]
    blocks = app.output_blocks(document)
    anchors = []
    for label, expected, y_band in VISUAL_ANCHORS:
        candidates = []
        needle = app.normalized(expected)
        for block in blocks:
            if needle not in app.normalized(block["text"]):
                continue
            for provenance in block["provenance"]:
                if provenance.get("page_no") != 1:
                    continue
                bbox = provenance["bbox"]
                center = (bbox["t"] + bbox["b"]) / 2
                if bbox["coord_origin"] == "BOTTOMLEFT":
                    center = height - center
                relative_y = center / height
                candidates.append(
                    {
                        "docling_ref": block["ref"],
                        "provenance": provenance,
                        "center_y_from_top": relative_y,
                        "in_visual_band": y_band[0] <= relative_y <= y_band[1],
                    }
                )
        anchors.append(
            {
                "region": label,
                "source_page": 1,
                "expected_visual_text": expected,
                "expected_y_band_from_top": list(y_band),
                "matches": candidates,
                "matched": any(candidate["in_visual_band"] for candidate in candidates),
            }
        )
    return anchors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run_root = args.output.resolve() / f"run-{uuid4().hex}"
    run_root.mkdir(parents=True, exist_ok=False)
    p01_report = app.load_json(app.P01_REPORT)
    manifest_path = Path(p01_report["first"]["manifest"])
    content_root = manifest_path.parents[1]
    before = app.sha256(manifest_path.read_bytes())
    received = receive_handoff(
        manifest_path,
        content_root,
        expected_task_id="p01-real-fetch",
        expected_request_fingerprint=None,
        intake_context=None,
    )
    manifest = received["manifest"]
    selected = next(
        item for item in received["materials"] if item["record"]["material_id"] == "PDF"
    )
    record = selected["record"]
    if record["state"] != "ready" or record["format"] != "pdf":
        raise ValueError("Selected scanned PDF is not a ready PDF material")
    task = next(
        task for task in manifest["parse_tasks"] if task["parse_task_id"] == record["parse_task_id"]
    )
    source = PdfReader(BytesIO(selected["content"]))
    if len(source.pages) < 1 or source.pages[0].extract_text():
        raise ValueError("Selected first page is not a native-text-free PDF page")
    actual_versions = app.worker({"operation": "probe_ocr"})
    models, missing = model_inventory(app.MODEL_CACHE)
    scope = {"kind": "page_range", "page_range": [1, 1]}
    config = {
        "reader": "DoclingStandardPdfRapidOCR",
        "versions": DECLARED_VERSIONS,
        "options": {
            "pipeline": "StandardPdfPipeline",
            "cache_path": str(app.MODEL_CACHE),
            "device": "cpu",
            "threads": 2,
            "offline": True,
            "enable_remote_services": False,
            "allow_external_plugins": False,
            "do_ocr": True,
            "ocr_engine": "RapidOCR",
            "ocr_language": ["ch"],
            "ocr_backend": "onnxruntime",
            "ocr_mode": "default",
            "ocr_scale": 3.0,
            "ocr_model_size": "small",
            "layout_model": "layout_heron_default",
            "do_table_structure": True,
            "table_mode": "accurate",
            "do_cell_matching": True,
            "generate_parsed_pages": True,
            "model_assets": models,
        },
        "requested_scope": scope,
    }
    reason = None
    if task["route"] != "docling" or task["format"] != "pdf":
        reason = "reader_not_bound_for_route"
    elif task["parser"]["version"] not in {"unbound", DECLARED_VERSIONS["docling"]}:
        reason = "plan_version_mismatch"
    elif actual_versions != DECLARED_VERSIONS:
        reason = "reader_version_mismatch"
    elif missing:
        reason = "local_model_assets_missing"
    execution = {
        "input_plan_id": task["parse_task_id"],
        "material_ids": task["material_ids"],
        "input_sha256": task["content_sha256"],
        "input_manifest_ref": {"ref": str(manifest_path), "sha256": before},
        "configured_profile": config,
        "executed_profile": None,
        "execution_id": None,
        "execution_key": None,
        "execution_state": "not_started",
        "reason": reason,
        "reader_started": False,
        "result": None,
        "validation": None,
        "occurrence_bindings": [app.occurrence(record, selected["source_bound"])],
        "issues": (
            [] if selected["source_bound"] else [{"code": "intake_source_context_unavailable"}]
        ),
    }
    if reason is None:
        execution_id = uuid4().hex
        artifact_path = run_root / "artifacts" / f"{execution_id}.json"
        result = app.worker(
            {
                "operation": "convert",
                "content_root": str(content_root.resolve()),
                "content_path": task["content"]["path"],
                "sha256": task["content_sha256"],
                "format": "pdf",
                "versions": DECLARED_VERSIONS,
                "profile": config["options"],
                "requested_scope": scope,
                "json_path": str(artifact_path),
            }
        )
        execution.update(
            {
                "execution_id": execution_id,
                "execution_key": app.execution_key(task, config),
                "executed_profile": {
                    "reader": config["reader"],
                    "versions": result["versions"],
                    "options": result["effective_ocr_configuration"],
                    "requested_scope": scope,
                },
                "execution_state": result["conversion_status"],
                "reader_started": True,
                "result": result,
            }
        )
        execution["issues"].extend(result["errors"])
        if result["conversion_status"] in {"success", "partial_success"}:
            document = app.load_json(result["json_path"])
            anchors = anchor_evidence(document)
            page_cells = [page["ocr_cells"] for page in result["ocr_page_cells"]]
            ocr_cells = sum(page_cells) if all(value is not None for value in page_cells) else None
            pages = result["document_pages"]
            tables = [
                {
                    "ref": table["self_ref"],
                    "provenance": table.get("prov", []),
                    "rows": table["data"]["num_rows"],
                    "columns": table["data"]["num_cols"],
                    "cells": len(table["data"]["table_cells"]),
                }
                for table in document.get("tables", [])
            ]
            passed = (
                pages == [1]
                and ocr_cells is not None
                and ocr_cells > 0
                and all(anchor["matched"] for anchor in anchors)
            )
            execution["validation"] = {
                "source_page_count": len(source.pages),
                "selected_pages": [1],
                "docling_pages": pages,
                "unread_pages": list(range(2, len(source.pages) + 1)),
                "ocr_cells": ocr_cells,
                "visual_anchors": anchors,
                "detected_tables": tables,
                "full_page_accuracy_established": False,
                "unread_scope": "Pages 2–13 were not processed; table and character accuracy require review.",
            }
            execution["structure_refs"] = [
                {
                    "kind": "docling_document_json",
                    "path": result["json_path"],
                    "sha256": result["json_sha256"],
                },
                {"kind": "ocr_cells_json", **result["ocr_cells_ref"]},
            ]
            if not passed and execution["execution_state"] == "success":
                execution["execution_state"] = "partial_success"
                execution["reason"] = "source_page_or_sampled_ocr_evidence_incomplete"

    if app.sha256(manifest_path.read_bytes()) != before:
        raise ValueError("P01 manifest changed during OCR READ")
    inventory = []
    for item in received["materials"]:
        material = item["record"]
        inventory.append(
            {
                **app.occurrence(material, item["source_bound"]),
                "preprocessing_state": material["state"],
                "preprocessing_issues": material["issues"],
                "read_state": (
                    execution["execution_state"]
                    if material["material_id"] == "PDF"
                    else material["state"]
                    if material["state"] in {"blocked", "preserved"}
                    else "not_selected"
                ),
                "source_context_gap": not item["source_bound"],
            }
        )
    report = {
        "kind": "local_ocr_read_evidence_not_field_extraction",
        "source_manifest_unchanged": True,
        "intake_bound": received["intake_bound"],
        "original_pdf_native_text_first_page": False,
        "actual_versions": actual_versions,
        "missing_model_assets": missing,
        "inventory": inventory,
        "execution": execution,
        "limitations": [
            "One scanned page only; unselected pages and original-source context remain unknown.",
            "OCR recognition and sampled locations do not establish full-page or business-field accuracy.",
            "Table recognition output is recorded but has not been exhaustively checked against the page.",
        ],
    }
    report_path = run_root / "report.json"
    with report_path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    validation = execution["validation"] or {}
    print(
        json.dumps(
            {
                "report": str(report_path),
                "execution_state": execution["execution_state"],
                "conversion_status": execution["result"]["conversion_status"]
                if execution["result"]
                else None,
                "reason": execution["reason"],
                "selected_pages": validation.get("selected_pages"),
                "unread_page_count": len(validation.get("unread_pages", [])),
                "ocr_cells": validation.get("ocr_cells"),
                "sampled_anchors_matched": sum(
                    anchor["matched"] for anchor in validation.get("visual_anchors", [])
                ),
                "detected_tables": len(validation.get("detected_tables", [])),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
