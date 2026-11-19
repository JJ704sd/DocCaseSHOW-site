"""Caller-visible PDF escalation behavior with a local Docling-worker stand-in."""

import hashlib
import json
import subprocess
import sys
import time
from io import BytesIO
from pathlib import Path

import local_parse_server as app
import p02_local_read as p02
import pytest
from pypdf import PdfWriter
from pypdf.errors import DependencyError
from test_local_email_index import _indexed_request, _request

pytest_plugins = ["test_local_email_index"]


def _block(text):
    return {
        "text": text,
        "prov": [
            {
                "page_no": 1,
                "bbox": {"l": 10, "r": 180, "t": 100, "b": 90, "coord_origin": "BOTTOMLEFT"},
                "charspan": [0, len(text)],
            }
        ],
    }


def test_line_reader_can_select_a_single_pdf_page():
    config = p02.configurations(pdf_page_range=[6, 6])[("docling", "pdf")]
    assert config["options"]["text_cell_unit"] == "line"
    assert config["requested_scope"] == {"kind": "page_range", "page_range": [6, 6]}


def test_empty_native_page_uses_ocr_and_returns_locatable_field(
    indexed_server, tmp_path, monkeypatch
):
    versions = p02.configurations(cache_path=tmp_path / "cache")[("docling", "pdf")]["versions"]
    calls = []

    def worker(request, *, python=None, model_cache=None):
        if request["operation"] == "probe":
            return versions
        calls.append(request)
        ocr = request["profile"]["pipeline"] == "StandardPdfPipeline"
        document = {
            "texts": [_block("Total gross weight 8 kg")] if ocr else [],
            "tables": [],
            "pages": {"1": {}},
        }
        path = Path(request["json_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(document).encode("utf-8")
        path.write_bytes(raw)
        return {
            "conversion_status": "success",
            "errors": [],
            "versions": versions,
            "json_path": str(path),
            "json_sha256": hashlib.sha256(raw).hexdigest(),
            "document_pages": [1],
            "text_items": len(document["texts"]),
            "table_items": 0,
        }

    monkeypatch.setattr(p02, "worker", worker)
    base, _, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    email = found["emails"][0]
    status, result = _request(
        base, "/local/parse", _indexed_request(email, email["attachments"][0])
    )
    fields = {item["field_name"]: item for item in result["fields"]}
    assert status == 200
    assert len(result["fields"]) == len(fields)
    assert fields["line_gross_weight"]["value"] == 8
    assert fields["line_gross_weight"]["sources"][0]["source"]["locator"]["page"] == 1
    assert [call["profile"]["pipeline"] for call in calls] == [
        "NativePdfPipeline",
        "StandardPdfPipeline",
    ]


@pytest.mark.parametrize("profile_id", ["indexed-pdf-document", "indexed-pdf-document-cargo"])
@pytest.mark.parametrize("merged_description", [False, True])
@pytest.mark.parametrize("shifted_net_column", [False, True])
@pytest.mark.parametrize("row_quantities", [False, True])
def test_missing_table_field_triggers_structure_without_losing_known_value(
    indexed_server,
    tmp_path,
    monkeypatch,
    profile_id,
    merged_description,
    shifted_net_column,
    row_quantities,
):
    versions = p02.configurations(cache_path=tmp_path / "cache")[("docling", "pdf")]["versions"]
    calls = []

    def cell(row, col, text, *, header=False, width=1):
        return {
            "text": text,
            "start_row_offset_idx": row,
            "end_row_offset_idx": row + 1,
            "start_col_offset_idx": col,
            "end_col_offset_idx": col + width,
            "bbox": {"l": col * 50, "r": (col + width) * 50, "t": row * 20, "b": (row + 1) * 20},
            "column_header": header,
        }

    cells = [
        cell(0, 0, "PACKING LIST", header=True),
        cell(0, 9, "Shipment Terms"),
        cell(1, 9, "FCA YANTAI"),
        cell(0, 10, "Shipping Way"),
        cell(1, 10, "BY SEA"),
    ]
    shift = int(merged_description)
    cells += [
        cell(1, 0, "Item", header=not merged_description),
        cell(1, 1, "Description", header=not merged_description, width=1 + shift),
    ]
    cells.append(cell(1, 2 + shift, "Unit", header=not merged_description))
    cells += [
        cell(1, col + shift, value, header=not merged_description)
        for col, value in enumerate(["N.W. (kg)", "G.W. (kg)", "Size (m³)"], 3)
    ]
    for row, item, name, net in [(2, "1", "Item A", "4"), (3, "2", "Item B", "2")]:
        net_cell = cell(row, 3 + shift - int(shifted_net_column), net)
        net_cell["bbox"] = {
            "l": (3 + shift) * 50,
            "r": (4 + shift) * 50,
            "t": row * 20,
            "b": (row + 1) * 20,
        }
        cells += [cell(row, 0, item), cell(row, 1, name, width=1 + shift), net_cell]
        if row_quantities:
            cells += [
                cell(row, 4 + shift, "5" if row == 2 else "3"),
                cell(row, 5 + shift, "0.012" if row == 2 else "0.008"),
            ]
    if not row_quantities:
        for col, value in [(4 + shift, "8"), (5 + shift, "0.02")]:
            merged = cell(2, col, value)
            merged["end_row_offset_idx"] = 4
            cells.append(merged)
    cells += [
        cell(4, 1, "Total", width=1 + shift),
        cell(4, 3 + shift, "6"),
        cell(4, 4 + shift, "8"),
        cell(4, 5 + shift, "0.02"),
    ]

    def worker(request, *, python=None, model_cache=None):
        if request["operation"] == "probe":
            return versions
        calls.append(request)
        structural = request["profile"]["pipeline"] == "StandardPdfPipeline"
        table = {"prov": _block("table")["prov"], "data": {"table_cells": cells}}
        document = {
            "texts": []
            if structural
            else [
                _block("Total gross weight 8 kg"),
                _block("N.W. (kg)"),
                _block("Size (m³)"),
                _block("Total"),
            ],
            "tables": [table] if structural else [],
            "pages": {"1": {}},
        }
        path = Path(request["json_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(document, ensure_ascii=False).encode("utf-8")
        path.write_bytes(raw)
        return {
            "conversion_status": "success",
            "errors": [],
            "versions": versions,
            "json_path": str(path),
            "json_sha256": hashlib.sha256(raw).hexdigest(),
            "document_pages": [1],
            "text_items": len(document["texts"]),
            "table_items": len(document["tables"]),
        }

    monkeypatch.setattr(p02, "worker", worker)
    base, _, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    email = found["emails"][0]
    request = _indexed_request(email, email["attachments"][0])
    request["profile_id"] = profile_id
    status, result = _request(base, "/local/parse", request)
    fields = {item["field_name"]: item for item in result["fields"]}
    assert status == 200
    assert fields["line_gross_weight"]["value"] == 8
    assert sum(item["field_name"] == "line_gross_weight" for item in result["fields"]) == 1
    assert fields["line_volume"]["value"] == 0.02
    assert any(
        "/tables/" in source["source"]["locator"]["json_pointer"]
        for source in fields["line_volume"]["sources"]
    )
    assert calls[1]["profile"]["do_ocr"] is False
    if profile_id == "indexed-pdf-document-cargo":
        assert [
            (item["field_name"], item["value"]) for item in result["document_observations"]
        ] == [("shipment_terms", "FCA YANTAI"), ("shipping_way", "BY SEA")]
        rows = result["cargo_rows"]
        assert [(row["item_no"], row["product_name"]) for row in rows] == [
            ("1", "Item A"),
            ("2", "Item B"),
        ]
        assert [
            (row["line_net_weight"]["value"], row["line_net_weight"]["unit"]) for row in rows
        ] == [(4, "kg"), (2, "kg")]
        assert rows[0]["scope"] == "product_row"
        assert "/tables/" in rows[0]["source"]["description_pointer"]
        for row in rows:
            source = row["source"]
            raw = Path(source["ref"]).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == source["sha256"]
            document = json.loads(raw)
            for name, pointer in (
                ("item_no", "item_pointer"),
                ("product_name", "description_pointer"),
            ):
                value = document
                for token in source[pointer].lstrip("/").split("/"):
                    value = value[int(token)] if isinstance(value, list) else value[token]
                assert value == row[name]
            if row_quantities:
                assert row["line_gross_weight"]["status"] == "known"
                assert row["line_volume"]["status"] == "known"
                assert row["density_kg_per_m3"]["value"] == pytest.approx(
                    row["line_gross_weight"]["value"] / row["line_volume"]["value"]
                )
            else:
                assert row["line_gross_weight"]["status"] == "uncertain"
                assert row["line_volume"]["status"] == "uncertain"
                assert row["density_kg_per_m3"]["value"] is None


@pytest.mark.parametrize("mode", ["missing_unit", "duplicate_header"])
def test_cargo_pdf_does_not_guess_ambiguous_table_volume(
    indexed_server, tmp_path, monkeypatch, mode
):
    versions = p02.configurations(cache_path=tmp_path / "cache")[("docling", "pdf")]["versions"]

    def cell(row, col, value):
        return {
            "text": value,
            "start_row_offset_idx": row,
            "end_row_offset_idx": row + 1,
            "start_col_offset_idx": col,
            "end_col_offset_idx": col + 1,
            "column_header": row == 0,
        }

    header = "Size" if mode == "missing_unit" else "Size (m³)"
    cells = [cell(0, 0, "Description"), cell(0, 1, header), cell(1, 0, "Total"), cell(1, 1, "0.27")]
    if mode == "duplicate_header":
        cells.extend([cell(0, 2, "Size (m³)"), cell(1, 2, "0.50")])

    def worker(request, *, python=None, model_cache=None):
        if request["operation"] == "probe":
            return versions
        structural = request["profile"]["pipeline"] == "StandardPdfPipeline"
        document = {
            "texts": [] if structural else [_block("Size (m³)"), _block("Total")],
            "tables": [{"prov": _block("table")["prov"], "data": {"table_cells": cells}}]
            if structural
            else [],
            "pages": {"1": {}},
        }
        path = Path(request["json_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(document, ensure_ascii=False).encode("utf-8")
        path.write_bytes(raw)
        return {
            "conversion_status": "success",
            "errors": [],
            "versions": versions,
            "json_path": str(path),
            "json_sha256": hashlib.sha256(raw).hexdigest(),
            "document_pages": [1],
            "text_items": len(document["texts"]),
            "table_items": len(document["tables"]),
        }

    monkeypatch.setattr(p02, "worker", worker)
    base, _, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    email = found["emails"][0]
    request = _indexed_request(email, email["attachments"][0])
    request["profile_id"] = "indexed-pdf-document-cargo"
    status, result = _request(base, "/local/parse", request)
    volume = next(item for item in result["fields"] if item["field_name"] == "line_volume")
    assert status == 200
    assert (volume["value"], volume["status"]) == (None, "uncertain")


def test_direct_main_entry_runs_pdf_table_supplement(indexed_server, tmp_path):
    from local_email_index import LocalEmailIndex, read_indexed_email
    from local_parse_server import FIELD_CONTRACT, _indexed_profile, _indexed_target

    from doccase_preprocessing.local_handoff import run_local_handoff
    from doccase_preprocessing.main_channel import read_validated_result, run_main_channel

    base, archive, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    email = found["emails"][0]
    attachment = email["attachments"][0]
    index = LocalEmailIndex(tmp_path / "index.json")
    raw, source = read_indexed_email(
        index, archive.parent, email["email_id"], attachment["attachment_id"]
    )
    p01 = tmp_path / "direct-p01"
    prepared = run_local_handoff(
        raw,
        p01,
        input_format="eml",
        request_reference="test:direct",
        task_id="test-direct",
        email_ref_id=email["email_id"],
    )
    manifest = prepared["received"]["manifest"]
    target = _indexed_target(manifest, source)
    profile = _indexed_profile("indexed-pdf-document", manifest, target)
    versions = p02.configurations(cache_path=tmp_path / "cache")[("docling", "pdf")]["versions"]
    configs = p02.configurations(cache_path=tmp_path / "cache", declared_versions=versions)

    def cell(row, col, text, header=False):
        return {
            "text": text,
            "start_row_offset_idx": row,
            "end_row_offset_idx": row + 1,
            "start_col_offset_idx": col,
            "end_col_offset_idx": col + 1,
            "column_header": header,
        }

    cells = [
        cell(0, col, value, True)
        for col, value in enumerate(["Description", "N.W. (kg)", "G.W. (kg)", "Size (m³)"])
    ]
    cells += [cell(1, col, value) for col, value in enumerate(["Total", "6", "8", "0.02"])]

    def worker(request):
        structure = request["profile"]["pipeline"] == "StandardPdfPipeline"
        table = {"prov": _block("table")["prov"], "data": {"table_cells": cells}}
        document = {
            "texts": []
            if structure
            else [
                _block("Total gross weight 8 kg"),
                _block("N.W. (kg)"),
                _block("Size (m³)"),
                _block("Total"),
            ],
            "tables": [table] if structure else [],
            "pages": {"1": {}},
        }
        path = Path(request["json_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(document, ensure_ascii=False).encode()
        path.write_bytes(data)
        return {
            "conversion_status": "success",
            "errors": [],
            "versions": versions,
            "json_path": str(path),
            "json_sha256": hashlib.sha256(data).hexdigest(),
            "document_pages": [1],
            "text_items": len(document["texts"]),
            "table_items": len(document["tables"]),
        }

    support = {
        "read_pdf_adaptive": p02.read_pdf_adaptive,
        "configurations": p02.configurations,
        "table_supplement_pages": p02.pdf_table_supplement_pages,
        "document_timeout": 45,
        "supplement_timeout": 45,
        "deadline": time.monotonic() + 30,
    }
    contract = {
        "version": "0.1.0",
        "ref": str(FIELD_CONTRACT.resolve()),
        "sha256": hashlib.sha256(FIELD_CONTRACT.read_bytes()).hexdigest(),
    }
    result = run_main_channel(
        prepared["manifest_path"],
        p01,
        expected_task_id=manifest["task_id"],
        expected_request_fingerprint=prepared["request_fingerprint"],
        intake_context=prepared["intake_context"],
        selected_material_ids={target["material_id"]},
        read_configurations=configs,
        reader_versions=versions,
        field_profile=profile,
        field_contract_ref=contract,
        output_dir=tmp_path / "direct-main",
        reader=p02.parse_prepared,
        worker_fn=worker,
        pdf_support=support,
    )
    snapshot = read_validated_result(result["result_ref"], content_root=p01)
    fields = {
        item["field_name"]: item["field"] for item in snapshot["material_handoff"]["candidates"]
    }
    assert result["pdf_supplement"]["state"] == "applied"
    assert result["pdf_supplement"]["pages"] == [1]
    assert fields["line_gross_weight"]["value"] == 8
    assert fields["line_volume"]["value"] == 0.02


def test_ocr_table_summary_keeps_conflicting_gross_values(indexed_server, tmp_path, monkeypatch):
    versions = p02.configurations(cache_path=tmp_path / "cache")[("docling", "pdf")]["versions"]
    calls = []

    def cell(row, col, text, *, end=None, header=False):
        return {
            "text": text,
            "start_row_offset_idx": row,
            "end_row_offset_idx": row + 1,
            "start_col_offset_idx": col,
            "end_col_offset_idx": end or col + 1,
            "column_header": header,
        }

    summary = "Total Packages:1 PLT, NW:65KG, GW:80KG, Cbm: 0.762 CBM"
    cells = [
        cell(0, 0, "Packing Type", header=True),
        cell(0, 1, "QUANT", header=True),
        cell(0, 2, "NET WEIGHT", header=True),
        cell(0, 3, "GROSS WEIGHT", header=True),
        cell(1, 0, "PALLET/BOX"),
        cell(1, 1, "1"),
        cell(1, 2, "65KG"),
        cell(1, 3, "88KG"),
        cell(2, 0, "合计Total"),
        cell(2, 1, summary, end=4),
    ]

    def worker(request, *, python=None, model_cache=None):
        if request["operation"] == "probe":
            return versions
        ocr = request["profile"]["pipeline"] == "StandardPdfPipeline"
        calls.append(request["profile"])
        structure = ocr and request["profile"]["do_table_structure"]
        table = {"prov": _block("table")["prov"], "data": {"table_cells": cells}}
        document = {
            "texts": [_block("NET WEIGHT"), _block("GROSS WEIGHT"), _block("Total")]
            if ocr and not structure
            else [],
            "tables": [table] if structure else [],
            "pages": {"1": {}},
        }
        path = Path(request["json_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(document, ensure_ascii=False).encode("utf-8")
        path.write_bytes(raw)
        return {
            "conversion_status": "success",
            "errors": [],
            "versions": versions,
            "json_path": str(path),
            "json_sha256": hashlib.sha256(raw).hexdigest(),
            "document_pages": [1],
            "text_items": len(document["texts"]),
            "table_items": len(document["tables"]),
        }

    monkeypatch.setattr(p02, "worker", worker)
    base, _, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    email = found["emails"][0]
    status, result = _request(
        base, "/local/parse", _indexed_request(email, email["attachments"][0])
    )
    assert status == 200
    fields = {
        name: [field for field in result["fields"] if field["field_name"] == name]
        for name in ("ctns", "line_net_weight", "line_gross_weight", "line_volume")
    }
    assert fields["ctns"][0]["value"] is None
    assert fields["line_net_weight"][0]["value"] == 65
    assert fields["line_volume"][0]["value"] == 0.762
    gross = fields["line_gross_weight"]
    assert len(gross) == 1
    assert all(field["value"] is None and field["status"] == "conflict" for field in gross)
    assert {source["source"]["excerpt"] for field in gross for source in field["sources"]} >= {
        "88KG",
        summary,
    }
    assert [
        (profile.get("do_ocr"), profile.get("do_table_structure")) for profile in calls[1:]
    ] == [(True, False), (True, True)]


def test_structure_timeout_preserves_primary_fields_and_reports_timeout(
    indexed_server, tmp_path, monkeypatch
):
    versions = p02.configurations(cache_path=tmp_path / "cache")[("docling", "pdf")]["versions"]

    def worker(request, *, python=None, model_cache=None):
        if request["operation"] == "probe":
            return versions
        if request["profile"]["pipeline"] == "StandardPdfPipeline":
            raise subprocess.TimeoutExpired("docling", 1)
        document = {
            "texts": [
                _block("Total gross weight 8 kg"),
                _block("N.W. (kg)"),
                _block("Size (m³)"),
                _block("Total"),
            ],
            "tables": [],
            "pages": {"1": {}},
        }
        path = Path(request["json_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(document, ensure_ascii=False).encode("utf-8")
        path.write_bytes(raw)
        return {
            "conversion_status": "success",
            "errors": [],
            "versions": versions,
            "json_path": str(path),
            "json_sha256": hashlib.sha256(raw).hexdigest(),
            "document_pages": [1],
            "text_items": len(document["texts"]),
            "table_items": 0,
        }

    monkeypatch.setattr(p02, "worker", worker)
    base, _, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    email = found["emails"][0]
    status, result = _request(
        base, "/local/parse", _indexed_request(email, email["attachments"][0])
    )
    fields = {field["field_name"]: field for field in result["fields"]}
    assert status == 200
    assert fields["line_gross_weight"]["value"] == 8
    assert result["pdf_supplement"]["reason"] == "timeout"
    assert result["pdf_supplement"]["read_ref"]


@pytest.mark.parametrize("force_structure", [False, True])
def test_ocr_result_survives_expensive_table_structure(tmp_path, force_structure):
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = BytesIO()
    writer.write(buffer)
    raw = buffer.getvalue()
    (tmp_path / "source.pdf").write_bytes(raw)
    request = {
        "operation": "convert",
        "format": "pdf",
        "content_root": str(tmp_path),
        "content_path": "source.pdf",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "versions": {"docling": "test"},
        "profile": {"pipeline": "NativePdfPipeline"},
        "requested_scope": {"kind": "page_range", "page_range": [1, 1]},
        "json_path": str(tmp_path / "result.json"),
    }

    def worker(request):
        profile = request["profile"]
        native = profile["pipeline"] == "NativePdfPipeline"
        if not native and profile["do_table_structure"]:
            raise subprocess.TimeoutExpired("table structure", 1)
        document = {
            "texts": [] if native else [_block("Total gross weight 8 kg")],
            "tables": [],
            "pages": {"1": {}},
        }
        data = json.dumps(document).encode()
        Path(request["json_path"]).write_bytes(data)
        return {
            "conversion_status": "success",
            "errors": [],
            "versions": request["versions"],
            "json_path": request["json_path"],
            "json_sha256": hashlib.sha256(data).hexdigest(),
            "document_pages": [1],
            "text_items": len(document["texts"]),
            "table_items": 0,
        }

    result = p02.read_pdf_adaptive(request, worker_fn=worker, force_structure=force_structure)
    bundle = json.loads(Path(result["json_path"]).read_text(encoding="utf-8"))
    assert bundle["parts"][1]["document"]["texts"][0]["text"] == "Total gross weight 8 kg"
    assert result["unread_pages"] == []
    assert result["termination_reason"] == ("timeout" if force_structure else "normal")


@pytest.mark.parametrize("ambiguous", [False, True])
def test_table_cell_uses_native_text_on_its_row_when_header_was_merged(tmp_path, ambiguous):
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = BytesIO()
    writer.write(buffer)
    raw = buffer.getvalue()
    (tmp_path / "source.pdf").write_bytes(raw)
    request = {
        "operation": "convert",
        "format": "pdf",
        "content_root": str(tmp_path),
        "content_path": "source.pdf",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "versions": {"docling": "test"},
        "profile": {"pipeline": "NativePdfPipeline"},
        "requested_scope": {"kind": "page_range", "page_range": [1, 1]},
        "json_path": str(tmp_path / "result.json"),
    }

    def text(value, left, bottom):
        return {
            "text": value,
            "prov": [
                {
                    "page_no": 1,
                    "bbox": {
                        "l": left,
                        "r": left + 20,
                        "b": bottom,
                        "t": bottom + 10,
                        "coord_origin": "BOTTOMLEFT",
                    },
                    "charspan": [0, len(value)],
                }
            ],
        }

    def cell(value, row, col, bbox):
        return {
            "text": value,
            "start_row_offset_idx": row,
            "end_row_offset_idx": row + 1,
            "start_col_offset_idx": col,
            "end_col_offset_idx": col + 1,
            "bbox": {**bbox, "coord_origin": "TOPLEFT"},
        }

    texts = [
        text("C01", 10, 130),
        text("Voltage before (%)", 120, 170),
        text("100.00", 120, 130),
        text("100.00", 120, 110),
    ]
    if ambiguous:
        texts.append(text("100.00", 120, 130))
    cells = [
        cell("C01", 1, 0, {"l": 10, "r": 30, "t": 60, "b": 70}),
        cell("Voltage before (%) 100.00", 1, 1, {"l": 110, "r": 150, "t": 20, "b": 70}),
    ]

    def worker(request):
        structure = request["profile"]["pipeline"] == "StandardPdfPipeline"
        table = {"prov": [{"page_no": 1}], "data": {"table_cells": cells}}
        document = {
            "texts": [] if structure else texts,
            "tables": [table] if structure else [],
            "pages": {"1": {"size": {"width": 200, "height": 200}}},
        }
        data = json.dumps(document).encode()
        Path(request["json_path"]).write_bytes(data)
        return {
            "conversion_status": "success",
            "errors": [],
            "versions": request["versions"],
            "json_path": request["json_path"],
            "json_sha256": hashlib.sha256(data).hexdigest(),
            "document_pages": [1],
            "text_items": len(document["texts"]),
            "table_items": len(document["tables"]),
        }

    result = p02.read_pdf_adaptive(request, worker_fn=worker, force_structure=True)
    bundle = json.loads(Path(result["json_path"]).read_text(encoding="utf-8"))
    resolved = bundle["resolved_table_cells"]
    if ambiguous:
        assert resolved == []
        return
    assert len(resolved) == 1
    assert resolved[0]["value"] == "100.00"
    assert resolved[0]["source_pointer"] == "/parts/0/document/texts/2/text"
    assert resolved[0]["table_cell_pointer"] == "/parts/1/document/tables/0/data/table_cells/1/text"
    assert resolved[0]["header_fragment"] == "Voltage before (%)"
    assert resolved[0]["header_source_pointers"] == ["/parts/0/document/texts/1/text"]
    assert bundle["parts"][1]["document"]["tables"][0]["data"]["table_cells"][1]["text"] == (
        "Voltage before (%) 100.00"
    )


def test_table_resolution_is_returned_with_supplement_reference(
    indexed_server, tmp_path, monkeypatch
):
    versions = p02.configurations(cache_path=tmp_path / "cache")[("docling", "pdf")]["versions"]

    def text(value, left, bottom):
        return {
            "text": value,
            "prov": [
                {
                    "page_no": 1,
                    "bbox": {
                        "l": left,
                        "r": left + 20,
                        "b": bottom,
                        "t": bottom + 10,
                        "coord_origin": "BOTTOMLEFT",
                    },
                    "charspan": [0, len(value)],
                }
            ],
        }

    def cell(value, col, top, bottom):
        return {
            "text": value,
            "start_row_offset_idx": 1,
            "end_row_offset_idx": 2,
            "start_col_offset_idx": col,
            "end_col_offset_idx": col + 1,
            "bbox": {
                "l": 10 if col == 0 else 110,
                "r": 30 if col == 0 else 150,
                "t": top,
                "b": bottom,
                "coord_origin": "TOPLEFT",
            },
        }

    texts = [
        text("G.W. (kg)", 10, 180),
        text("Total", 40, 180),
        text("C01", 10, 130),
        text("100.00", 120, 130),
    ]
    cells = [cell("C01", 0, 60, 70), cell("Voltage before (%) 100.00", 1, 20, 70)]

    def worker(request, *, python=None, model_cache=None):
        if request["operation"] == "probe":
            return versions
        structure = request["profile"]["pipeline"] == "StandardPdfPipeline"
        document = {
            "texts": [] if structure else texts,
            "tables": [{"prov": [{"page_no": 1}], "data": {"table_cells": cells}}]
            if structure
            else [],
            "pages": {"1": {"size": {"width": 200, "height": 200}}},
        }
        path = Path(request["json_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(document).encode()
        path.write_bytes(data)
        return {
            "conversion_status": "success",
            "errors": [],
            "versions": versions,
            "json_path": str(path),
            "json_sha256": hashlib.sha256(data).hexdigest(),
            "document_pages": [1],
            "text_items": len(document["texts"]),
            "table_items": len(document["tables"]),
        }

    monkeypatch.setattr(p02, "worker", worker)
    base, _, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    email = found["emails"][0]
    status, result = _request(
        base, "/local/parse", _indexed_request(email, email["attachments"][0])
    )
    assert status == 200
    supplement = result["pdf_supplement"]
    assert supplement["state"] == "still_unresolved"
    assert supplement["resolved_table_cells"][0]["value"] == "100.00"
    assert supplement["read_ref"]


def test_document_timeout_retains_completed_page_and_marks_remaining_unread(tmp_path):
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)
    buffer = BytesIO()
    writer.write(buffer)
    raw = buffer.getvalue()
    source = tmp_path / "source.pdf"
    source.write_bytes(raw)
    request = {
        "operation": "convert",
        "format": "pdf",
        "content_root": str(tmp_path),
        "content_path": source.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "versions": {"docling": "test"},
        "profile": {"pipeline": "NativePdfPipeline"},
        "requested_scope": {"kind": "all_pages", "page_range": None},
        "json_path": str(tmp_path / "result.json"),
    }

    def worker(request):
        data = json.dumps(
            {"texts": [_block("Total gross weight 8 kg")], "tables": [], "pages": {"1": {}}}
        ).encode()
        Path(request["json_path"]).write_bytes(data)
        return {
            "conversion_status": "partial_success",
            "errors": [{"code": "document_timeout"}],
            "termination_reason": "timeout",
            "versions": request["versions"],
            "json_path": request["json_path"],
            "json_sha256": hashlib.sha256(data).hexdigest(),
            "document_pages": [1],
            "text_items": 1,
            "table_items": 0,
        }

    result = p02.read_pdf_adaptive(request, worker_fn=worker)
    assert result["conversion_status"] == "partial_success"
    assert result["termination_reason"] == "timeout"
    assert result["unread_pages"] == [2]
    bundle = json.loads(Path(result["json_path"]).read_text(encoding="utf-8"))
    assert bundle["parts"][0]["document"]["texts"][0]["text"] == "Total gross weight 8 kg"


def test_aes_page_count_uses_the_existing_docling_pdf_backend(tmp_path, monkeypatch):
    raw = b"AES PDF represented by the worker fixture"
    (tmp_path / "source.pdf").write_bytes(raw)
    request = {
        "operation": "convert",
        "format": "pdf",
        "content_root": str(tmp_path),
        "content_path": "source.pdf",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "versions": {"docling": "test"},
        "profile": {"pipeline": "NativePdfPipeline"},
        "requested_scope": {"kind": "all_pages", "page_range": None},
        "json_path": str(tmp_path / "result.json"),
    }

    def page_count_unavailable(_):
        raise DependencyError("AES dependency is missing")

    monkeypatch.setattr(p02, "PdfReader", page_count_unavailable)
    calls = []

    def worker(request):
        calls.append(request["operation"])
        if request["operation"] == "pdf_page_count":
            return {"page_count": 2}
        data = json.dumps(
            {"texts": [_block("Total gross weight 8 kg")], "tables": [], "pages": {"1": {}}}
        ).encode()
        Path(request["json_path"]).write_bytes(data)
        return {
            "conversion_status": "partial_success",
            "termination_reason": "timeout",
            "errors": [{"code": "document_timeout"}],
            "versions": request["versions"],
            "json_path": request["json_path"],
            "json_sha256": hashlib.sha256(data).hexdigest(),
            "document_pages": [1],
            "text_items": 1,
            "table_items": 0,
        }

    result = p02.read_pdf_adaptive(request, worker_fn=worker)
    assert calls == ["pdf_page_count", "convert"]
    assert result["unread_pages"] == [2]
    assert result["termination_reason"] == "timeout"


def test_worker_enforces_hard_deadline(tmp_path, monkeypatch):
    script = tmp_path / "slow_worker.py"
    script.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")
    monkeypatch.setattr(p02, "WORKER", script)
    started = time.monotonic()
    result = p02.worker(
        {"operation": "convert", "versions": {"docling": "test"}, "hard_timeout_seconds": 0.2},
        python=sys.executable,
        model_cache=tmp_path,
    )
    assert time.monotonic() - started < 3
    assert result["conversion_status"] == "failure"
    assert result["termination_reason"] == "timeout"


def test_document_timeout_is_visible_in_saved_material_result(
    indexed_server, tmp_path, monkeypatch
):
    versions = p02.configurations(cache_path=tmp_path / "cache")[("docling", "pdf")]["versions"]

    def worker(request, *, python=None, model_cache=None):
        if request["operation"] == "probe":
            return versions
        data = json.dumps(
            {"texts": [_block("Total gross weight 8 kg")], "tables": [], "pages": {"1": {}}}
        ).encode()
        path = Path(request["json_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return {
            "conversion_status": "partial_success",
            "termination_reason": "timeout",
            "errors": [{"code": "document_timeout"}],
            "versions": versions,
            "json_path": str(path),
            "json_sha256": hashlib.sha256(data).hexdigest(),
            "document_pages": [1],
            "text_items": 1,
            "table_items": 0,
        }

    monkeypatch.setattr(p02, "worker", worker)
    base, _, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    email = found["emails"][0]
    status, result = _request(
        base, "/local/parse", _indexed_request(email, email["attachments"][0])
    )
    assert status == 200
    assert (
        next(item for item in result["fields"] if item["field_name"] == "line_gross_weight")[
            "value"
        ]
        == 8
    )
    assert result["pdf_read"]["termination_reason"] == "timeout"
    assert result["coverage"]["parsing"]["stage_state"] == "interrupted"
    from doccase_preprocessing.main_channel import read_result

    handoff = read_result(result["result_ref"])["material_handoff"]
    assert (
        handoff["execution_state"],
        handoff["termination_reason"],
        handoff["handoff_state"],
    ) == ("interrupted", "deadline", "partial")


@pytest.mark.parametrize(("net_total", "expected"), [("10", 10), ("11", None)])
@pytest.mark.parametrize("profile_id", ["indexed-pdf-document", "indexed-pdf-document-cargo"])
def test_unlabeled_total_requires_carton_ranges_and_matching_column_sum(
    indexed_server, tmp_path, monkeypatch, net_total, expected, profile_id
):
    versions = p02.configurations(cache_path=tmp_path / "cache")[("docling", "pdf")]["versions"]

    def cell(row, col, text, width=1):
        return {
            "text": text,
            "start_row_offset_idx": row,
            "end_row_offset_idx": row + 1,
            "start_col_offset_idx": col,
            "end_col_offset_idx": col + width,
            "column_header": row == 0,
        }

    cells = [
        cell(0, 0, "Carton No."),
        cell(0, 1, "Description"),
        cell(0, 2, "PACKAGE"),
        cell(0, 3, "N.W.", 2),
        cell(0, 5, "G.W.", 2),
    ]
    for row, carton, count, net, gross in [
        (1, "1-2", "2", "5", "6"),
        (2, "3", "1", "3", "4"),
        (3, "4", "1", "2", "3"),
    ]:
        cells.extend(
            [
                cell(row, 0, carton),
                cell(row, 1, "item"),
                cell(row, 2, count),
                cell(row, 3, net),
                cell(row, 4, "KG"),
                cell(row, 5, gross),
                cell(row, 6, "KG"),
            ]
        )
    cells.extend(
        [
            cell(4, 2, "4"),
            cell(4, 3, net_total),
            cell(4, 4, "KG"),
            cell(4, 5, "13"),
            cell(4, 6, "KG"),
        ]
    )

    def worker(request, *, python=None, model_cache=None):
        if request["operation"] == "probe":
            return versions
        table_read = request["profile"]["pipeline"] == "StandardPdfPipeline"
        table = {"prov": _block("table")["prov"], "data": {"table_cells": cells}}
        doc = {
            "texts": []
            if table_read
            else [_block("Carton No."), _block("PACKAGE"), _block("N.W."), _block("G.W.")],
            "tables": [table] if table_read else [],
            "pages": {"1": {}},
        }
        raw = json.dumps(doc).encode()
        path = Path(request["json_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return {
            "conversion_status": "success",
            "errors": [],
            "versions": versions,
            "json_path": str(path),
            "json_sha256": hashlib.sha256(raw).hexdigest(),
            "document_pages": [1],
            "text_items": len(doc["texts"]),
            "table_items": len(doc["tables"]),
        }

    monkeypatch.setattr(p02, "worker", worker)
    base, _, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    email = found["emails"][0]
    request = _indexed_request(email, email["attachments"][0])
    request["profile_id"] = profile_id
    status, result = _request(base, "/local/parse", request)
    fields = {field["field_name"]: field for field in result["fields"]}
    assert status == 200
    assert fields["ctns"]["value"] == 4
    assert fields["line_gross_weight"]["value"] == 13
    assert fields["line_net_weight"]["value"] == expected
    if profile_id == "indexed-pdf-document-cargo":
        assert (fields["package_count"]["value"], fields["package_count"]["status"]) == (4, "known")
        assert "PACKAGE" in {
            item["source"]["excerpt"] for item in fields["package_count"]["sources"]
        }


@pytest.mark.parametrize(
    "layout",
    [
        "nested_packages",
        "total_and_unit_columns",
        "mixed_outer_pack",
        "bilingual_scopes",
        "mixed_summary",
        "unit_values",
        "generic_pkgs",
    ],
)
def test_document_total_uses_the_matching_total_column_and_package_scope(
    indexed_server, tmp_path, monkeypatch, layout
):
    versions = p02.configurations(cache_path=tmp_path / "cache")[("docling", "pdf")]["versions"]

    def cell(row, col, text, width=1):
        return {
            "text": text,
            "start_row_offset_idx": row,
            "end_row_offset_idx": row + 1,
            "start_col_offset_idx": col,
            "end_col_offset_idx": col + width,
            "column_header": row == 0,
        }

    if layout == "nested_packages":
        headers = [
            "Description",
            "件 数 (CTNS) Packages",
            "净 重 (KG) Net Weight",
            "毛 重 (KG) Gross Weight",
            "体积(M 3 ) Meas.",
        ]
        total = ["TOTAL:", "58 CTNS 2 PLTS", "1,920 KGS", "1940 KGS", "2.475 CBM"]
        expected = {
            "ctns": 58,
            "line_net_weight": 1920,
            "line_gross_weight": 1940,
            "line_volume": 2.475,
        }
    elif layout == "total_and_unit_columns":
        headers = ["Name", "CTN.", "NW KGS", "GW KGS", "CBM", "TOTAL CBM", "TOTAL NW", "TOTAL GW"]
        total = ["TOTAL:", "35", "", "", "", "4.830", "560.000", "647.500"]
        expected = {
            "ctns": 35,
            "line_net_weight": 560,
            "line_gross_weight": 647.5,
            "line_volume": 4.83,
        }
    elif layout == "mixed_outer_pack":
        headers = ["Description", "QTY", "OUTER PACK", "M3", "NET WT (KG)", "GROSS WT (KG)"]
        total = ["Total", "2", "PALLETS, 4 CARTONS", "1.32", "91.20", "107.20"]
        expected = {
            "ctns": 4,
            "line_net_weight": 91.2,
            "line_gross_weight": 107.2,
            "line_volume": 1.32,
        }
    elif layout == "bilingual_scopes":
        headers = [
            "Description",
            "TOTAL CTN 总箱数",
            "N.W/CTN (KG) 净重",
            "G.W/CTN (KG) 毛重",
            "Total N.W 总净重",
            "Total G.W 总毛重",
            "TOTAL CBM 总体积",
            "TOTAL CBM 托盘总",
        ]
        total = ["Total总计", "17.00", "~", "~", "160.70", "209.70", "1.45", "2.08"]
        expected = {"ctns": 17, "line_net_weight": 160.7, "line_gross_weight": 209.7}
    elif layout == "mixed_summary":
        headers = ["Description", "G.W. (KG)"]
        total = ["TOTAL", "508"]
        expected = {"ctns": 1, "line_gross_weight": 508, "line_volume": 4.07}
    elif layout == "unit_values":
        headers = [
            "Description",
            "Gross Weight per product",
            "Carton size",
            "Qty per carton",
            "CTNS 件数",
            "CBM",
            "G.W整 箱毛重",
        ]
        total = ["Total", "", "", "240pcs", "9ctns", "0.75CBM", "120.50kg"]
        expected = {"ctns": 9, "line_gross_weight": 120.5, "line_volume": 0.75}
    else:
        headers = ["Description", "Pkgs", "G.W (kg)", "CBM"]
        total = ["TOTAL", "2", "201.00", "0.36"]
        expected = {"package_count": 2, "line_gross_weight": 201, "line_volume": 0.36}
    cells = [cell(0, i, text) for i, text in enumerate(headers)]
    cells += [cell(1, i, text) for i, text in enumerate(total) if text]

    def worker(request, *, python=None, model_cache=None):
        if request["operation"] == "probe":
            return versions
        structural = request["profile"]["pipeline"] == "StandardPdfPipeline"
        table = {"prov": _block("table")["prov"], "data": {"table_cells": cells}}
        texts = (
            []
            if structural
            else [_block("N.W. (kg)"), _block("G.W. (kg)"), _block("Size (m³)"), _block("Total")]
        )
        if layout == "mixed_summary":
            texts.append(_block("Total:2Pallets, 1 Crate,1 Carton , G.W.: 508 KGS, 4.07CBM"))
        doc = {"texts": texts, "tables": [table] if structural else [], "pages": {"1": {}}}
        raw = json.dumps(doc).encode()
        path = Path(request["json_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return {
            "conversion_status": "success",
            "errors": [],
            "versions": versions,
            "json_path": str(path),
            "json_sha256": hashlib.sha256(raw).hexdigest(),
            "document_pages": [1],
            "text_items": len(doc["texts"]),
            "table_items": len(doc["tables"]),
        }

    monkeypatch.setattr(p02, "worker", worker)
    base, _, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    email = found["emails"][0]
    request = _indexed_request(email, email["attachments"][0])
    request["profile_id"] = "indexed-pdf-document-cargo"
    status, result = _request(base, "/local/parse", request)
    assert status == 200
    fields = {field["field_name"]: field for field in result["fields"]}
    for name, value in expected.items():
        assert (fields[name]["value"], fields[name]["status"]) == (value, "known")
        assert all(source["source"]["locator"]["page"] == 1 for source in fields[name]["sources"])
    if layout in {"nested_packages", "mixed_outer_pack", "mixed_summary"}:
        assert fields["package_count"]["value"] is None
    elif layout == "total_and_unit_columns":
        excerpts = {item["source"]["excerpt"] for item in fields["line_net_weight"]["sources"]}
        assert {"TOTAL NW", "NW KGS", "560.000"} <= excerpts
    elif layout == "bilingual_scopes":
        assert fields["line_volume"]["value"] is None
    elif layout == "generic_pkgs":
        assert fields["ctns"]["value"] is None
        assert fields["package_type"]["value"] is None


@pytest.mark.parametrize("ocr_page,timeout", [(None, False), (1, False), (1, True)])
def test_mixed_pdf_can_explicitly_supplement_one_page(
    indexed_server, tmp_path, monkeypatch, ocr_page, timeout
):
    cache = tmp_path / "cache"
    versions = p02.configurations(cache_path=cache)[("docling", "pdf")]["versions"]
    calls = []

    def worker(request, *, python=None, model_cache=None):
        if request["operation"] == "probe":
            return versions
        calls.append(request)
        ocr = request["profile"]["pipeline"] == "StandardPdfPipeline"
        if ocr and timeout:
            raise subprocess.TimeoutExpired("page OCR", 1)
        texts = [_block("4 箱")]
        if ocr:
            texts.append(_block("Total gross weight: 321 kg"))
        raw = json.dumps(
            {"texts": texts, "tables": [], "pages": {"1": {}}}, ensure_ascii=False
        ).encode()
        path = Path(request["json_path"])
        path.write_bytes(raw)
        return {
            "conversion_status": "success",
            "errors": [],
            "versions": versions,
            "json_path": str(path),
            "json_sha256": hashlib.sha256(raw).hexdigest(),
            "document_pages": [1],
            "text_items": len(texts),
            "table_items": 0,
        }

    monkeypatch.setattr(p02, "worker", worker)
    base, _, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    email = found["emails"][0]
    parser = app.LocalParser(
        tmp_path,
        tmp_path / "ocr-runs",
        tmp_path / "python",
        cache,
        tmp_path / "index.json",
        pdf_ocr_page=ocr_page,
    )
    result = parser.parse(_indexed_request(email, email["attachments"][0]))
    fields = {field["field_name"]: field for field in result["fields"]}
    assert fields["ctns"]["value"] == 4
    assert len(calls) == (2 if ocr_page else 1)
    if ocr_page:
        assert calls[1]["requested_scope"] == {"kind": "page_range", "page_range": [1, 1]}
        assert calls[1]["profile"]["do_ocr"] is True
        assert calls[1]["profile"]["do_table_structure"] is False
    if ocr_page and not timeout:
        gross = fields["line_gross_weight"]
        assert gross["value"] == 321
        locator = gross["sources"][0]["source"]["locator"]
        assert locator["page"] == 1
        assert locator["json_pointer"] == "/parts/1/document/texts/1/text"
    else:
        assert fields["line_gross_weight"]["value"] is None


@pytest.mark.parametrize("variant", ["footer", "no_total", "wrong_column"])
def test_bilingual_ocr_footer_proves_document_totals_but_not_cartons(
    indexed_server,
    tmp_path,
    monkeypatch,
    variant,
):
    cache = tmp_path / "cache"
    versions = p02.configurations(cache_path=cache)[("docling", "pdf")]["versions"]
    texts = [
        ("TOTAL S U M A", 185, 295, 120, 109),
        ("Bultos Packages", 20, 77, 85, 62),
        ("17", 55, 65, 62, 50),
        ("Peso Bruto P.B. kg.", 92, 206, 85, 73),
        ("Gross Weight G.W. 192,2500", 99, 200, 73, 50),
        ("Peso Neto P.B. kg. Net Weight G.W. 180,5000", 229, 343, 85, 49),
        ("Volumen m3. Volume", 379, 449, 84, 62),
        ("0,5621", 402, 449, 62, 49),
    ]
    if variant == "no_total":
        texts[0] = ("DETAIL", *texts[0][1:])
    elif variant == "wrong_column":
        texts[4] = (texts[4][0], 229, 343, 73, 50)

    def worker(request, *, python=None, model_cache=None):
        if request["operation"] == "probe":
            return versions
        profile = request["profile"]
        scanned = profile.get("do_ocr") and not profile.get("do_table_structure")
        blocks = (
            [
                {
                    "text": value,
                    "prov": [
                        {
                            "page_no": 1,
                            "charspan": [0, len(value)],
                            "bbox": {
                                "l": left,
                                "r": right,
                                "t": top,
                                "b": bottom,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                }
                for value, left, right, top, bottom in texts
            ]
            if scanned
            else []
        )
        raw = json.dumps({"texts": blocks, "tables": [], "pages": {"1": {}}}).encode()
        path = Path(request["json_path"])
        path.write_bytes(raw)
        return {
            "conversion_status": "success",
            "errors": [],
            "versions": versions,
            "json_path": str(path),
            "json_sha256": hashlib.sha256(raw).hexdigest(),
            "document_pages": [1],
            "text_items": len(blocks),
            "table_items": 0,
        }

    proposals = {
        "package_count": (17, (1, 2)),
        "ctns": (17, (1, 2)),
        "line_gross_weight": (192.25, (3, 4)),
        "line_net_weight": (180.5, (5,)),
        "line_volume": (0.5621, (6, 7)),
    }

    def adapter(request, *, timeout_seconds):
        evidence = {e["text"]: e["evidence_id"] for e in request["evidence"]}
        return {
            "candidates": [
                {
                    "requirement_id": r["requirement_id"],
                    "value": value,
                    "evidence_ids": [evidence[texts[i][0]] for i in indices],
                }
                for r in request["requirements"]
                if r["field_name"] in proposals
                for value, indices in [proposals[r["field_name"]]]
                if all(texts[i][0] in evidence for i in indices)
            ]
        }

    monkeypatch.setattr(p02, "worker", worker)
    monkeypatch.setattr(p02, "export_read_markdown", lambda *a, **kw: {"state": "failure"})
    base, _, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    email = found["emails"][0]
    parser = app.LocalParser(
        tmp_path,
        tmp_path / "footer-runs",
        tmp_path / "python",
        cache,
        tmp_path / "index.json",
        material_agent=adapter,
    )
    request = _indexed_request(email, email["attachments"][0])
    result = parser.parse({**request, "profile_id": "indexed-pdf-document-cargo"})
    fields = {f["field_name"]: f for f in result["fields"]}
    for name, (expected, _) in proposals.items():
        field = fields[name]
        if name != "ctns" and variant == "footer":
            assert (field["status"], field["value"]) == ("known", expected)
            assert all(s["source"]["locator"]["page"] == 1 for s in field["sources"])
        else:
            assert (field["status"], field["value"]) == ("uncertain", None)


@pytest.mark.parametrize(
    ("filename", "scan", "route"),
    [
        ("Documents.pdf", "image_only", "scanned_ocr"),
        ("Packing List.pdf", "image_only", "scanned_ocr"),
        ("Packing List.pdf", "has_text_layer", "packing_table"),
        ("Packing List.pdf", "probe_timeout", "packing_table"),
        ("Documents.pdf", "uncertain", "native"),
        ("MSDS.pdf", "image_only", "native"),
    ],
)
def test_first_read_scan_routing_is_conservative(filename, scan, route, tmp_path, monkeypatch):
    probes = []

    def probe(*args, **kwargs):
        probes.append(args)
        return scan

    monkeypatch.setattr(p02, "inspect_pdf_scan", probe)
    decision = p02.select_pdf_first_read(
        tmp_path, {"filename": filename, "content": {"path": "x", "sha256": "0"}}
    )
    assert decision == {"route": route, "scan": "safety_sheet" if not probes else scan}
    assert bool(probes) == (filename != "MSDS.pdf")


@pytest.mark.parametrize("outcome", ["success", "timeout", "empty"])
def test_image_only_pdf_is_converted_once_with_ocr_and_locatable_field(
    indexed_server, tmp_path, monkeypatch, outcome
):
    versions = p02.configurations(cache_path=tmp_path / "cache")[("docling", "pdf")]["versions"]
    calls = []

    def worker(request, *, python=None, model_cache=None):
        if request["operation"] == "probe":
            return versions
        calls.append(request)
        if outcome == "timeout":
            raise TimeoutError("local hard deadline")
        ocr = request["profile"].get("force_full_page_ocr")
        document = {
            "texts": [_block("Total gross weight 8 kg")] if ocr and outcome == "success" else [],
            "tables": [],
            "pages": {"1": {}},
        }
        path = Path(request["json_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(document).encode("utf-8")
        path.write_bytes(raw)
        return {
            "conversion_status": "success",
            "errors": [],
            "versions": versions,
            "input_sha256_reverified": request["sha256"],
            "json_path": str(path),
            "json_sha256": hashlib.sha256(raw).hexdigest(),
            "document_pages": [1],
            "text_items": len(document["texts"]),
            "table_items": 0,
        }

    monkeypatch.setattr(p02, "worker", worker)
    monkeypatch.setattr(p02, "inspect_pdf_scan", lambda *_a, **_k: "image_only", raising=False)
    base, _, _ = indexed_server
    _, found = _request(base, "/local/index?q=one.eml")
    email = found["emails"][0]
    status, result = _request(
        base, "/local/parse", _indexed_request(email, email["attachments"][0])
    )
    assert status == 200
    assert len(calls) == 1
    opts = calls[0]["profile"]
    assert opts["do_ocr"] and opts["do_table_structure"] and opts["force_full_page_ocr"]
    assert calls[0]["requested_scope"] == {"kind": "all_pages", "page_range": None}
    field = next(x for x in result["fields"] if x["field_name"] == "line_gross_weight")
    if outcome == "success":
        assert field["value"] == 8 and field["sources"][0]["source"]["locator"]["page"] == 1
    else:
        item = next(x for x in result["materials"] if x["format"] == "pdf")
        assert (item["read_state"], item["reason"]) == (
            ("failure", "timeout")
            if outcome == "timeout"
            else ("partial_success", "ocr_no_usable_content")
        )
        assert field["value"] is None and not field["sources"]


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("Packing List example.pdf", "packing_table"),
        ("PL.pdf", "packing_table"),
        ("SAMPLE-PL-202701.pdf", "packing_table"),
        ("装箱单.pdf", "packing_table"),
        ("MSDS packing list reference.pdf", "native"),
        ("Application.pdf", "native"),
        ("Commercial Invoice.pdf", "native"),
    ],
)
def test_initial_pdf_profile_uses_document_type_without_a_preliminary_read(filename, expected):
    layout = p02.pdf_first_read(filename)
    assert layout == expected
    config = p02.configurations(pdf_first_read=layout)[("docling", "pdf")]
    assert config["requested_scope"] == {"kind": "all_pages", "page_range": None}
    if expected == "packing_table":
        assert config["reader"] == "DoclingPdfStructured"
        assert config["options"]["pipeline"] == "StandardPdfPipeline"
        assert config["options"]["do_table_structure"] and not config["options"]["do_ocr"]
    else:
        assert config["options"]["pipeline"] == "NativePdfPipeline"
