"""Focused tests for model provenance and sampled scanned-page locations."""

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
sys.path.insert(0, str(EXAMPLES))
try:
    spec = importlib.util.spec_from_file_location("p02_ocr_read", EXAMPLES / "p02_ocr_read.py")
    ocr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ocr)
finally:
    sys.path.pop(0)


def test_model_inventory_marks_missing_assets_without_fabricating_hashes(tmp_path):
    (tmp_path / "present.bin").write_bytes(b"model")
    present, missing = ocr.model_inventory(
        tmp_path, {"detector": "present.bin", "recognizer": "missing.bin"}
    )
    assert present["detector"]["sha256"] == hashlib.sha256(b"model").hexdigest()
    assert missing == [{"name": "recognizer", "relative_path": "missing.bin"}]
    assert "recognizer" not in present


def test_ocr_anchor_requires_both_text_and_page_position():
    def text_item(ref, text, top, bottom, page=1):
        return {
            "self_ref": ref,
            "text": text,
            "prov": [
                {
                    "page_no": page,
                    "bbox": {"t": top, "b": bottom, "coord_origin": "TOPLEFT"},
                }
            ],
        }

    document = {
        "pages": {"1": {"size": {"height": 100}}},
        "texts": [
            text_item("#/texts/0", "SAFETY DATA SHEET", 4, 8),
            text_item("#/texts/1", "Product information", 20, 24),
            text_item("#/texts/2", "HAZARDS IDENTIFICATION", 46, 50),
        ],
    }
    anchors = ocr.anchor_evidence(document)
    assert all(anchor["matched"] for anchor in anchors)
    document["texts"][1]["prov"][0]["page_no"] = 2
    assert not ocr.anchor_evidence(document)[1]["matched"]


def test_unretained_intermediate_pages_do_not_appear_as_zero_ocr_cells():
    spec = importlib.util.spec_from_file_location(
        "p02_worker_for_ocr", EXAMPLES / "p02_docling_worker.py"
    )
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    missing = SimpleNamespace(page_no=1, parsed_page=None, cells=[])
    retained = SimpleNamespace(
        page_no=2, parsed_page=object(), cells=[SimpleNamespace(from_ocr=True)]
    )
    assert worker.ocr_page_summary([missing, retained]) == [
        {"page_index": 1, "intermediate_retained": False, "text_cells": None, "ocr_cells": None},
        {"page_index": 2, "intermediate_retained": True, "text_cells": 1, "ocr_cells": 1},
    ]
