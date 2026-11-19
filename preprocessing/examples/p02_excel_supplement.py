"""Replay one P01 workbook through an offline, bounded native supplement.

Run with the Docling environment and preprocessing's existing site-packages on
PYTHONPATH. This is local evidence, not a production parser or business verdict.
"""

import argparse
import json
import math
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import p02_docling_worker as docling_worker
import p02_local_read as local_read
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter, range_boundaries

from doccase_preprocessing.local_handoff import receive_handoff

BASE = Path(__file__).resolve().parents[1]
P01_REPORT = local_read.P01_REPORT
DOCLING_REPORT = Path("output/p02/report.json")
OUTPUT = BASE / "output/p02-excel-supplement"
SHEET = "CI A"
BOUNDS = (1, 1, 21, 105)  # min column, min row, max column, max row
SUPPLEMENT_VERSION = "0.1.0"
CONFIG_VERSION = "p02-excel-supplement-config/0.1.0"


def save_json(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return {"ref": str(path), "sha256": local_read.sha256(path.read_bytes())}


def json_value(value):
    """Retain the actual type; no formatting, formula calculation or coercion to zero."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, (datetime, date, time)):
        return {"python_type": type(value).__name__, "iso_value": value.isoformat()}
    if isinstance(value, timedelta):
        return {
            "python_type": "timedelta",
            "days": value.days,
            "seconds": value.seconds,
            "microseconds": value.microseconds,
        }
    if type(value).__name__ in {"ArrayFormula", "DataTableFormula"}:
        return {
            "python_type": type(value).__name__,
            "attributes": {key: json_value(val) for key, val in vars(value).items()},
        }
    raise TypeError(f"Unsupported workbook value type: {type(value).__name__}")


def cell_view(cell):
    return {
        "value": json_value(cell.value),
        "python_type": type(cell.value).__name__,
        "data_type": cell.data_type,
        "number_format": cell.number_format,
    }


def overlaps(bounds, row, column):
    left, top, right, bottom = bounds
    return left <= column <= right and top <= row <= bottom


def read_cells(raw, scope, sheet_name=SHEET, bounds=BOUNDS):
    """Read both views of each requested coordinate, using P01 for structural flags."""
    sheets = scope["sheets"]
    if scope["sheet_count"] != len(sheets) or len({s["name"] for s in sheets}) != len(sheets):
        raise ValueError("P01 worksheet inventory is inconsistent")
    matching = [s for s in sheets if s["name"] == sheet_name]
    if len(matching) != 1:
        raise ValueError("Selected worksheet is not uniquely present in P01")
    metadata = matching[0]
    merged = [(ref, range_boundaries(ref)) for ref in metadata["merged_ranges"]]
    hidden_rows = set(metadata["hidden_rows"])
    hidden_columns = metadata["hidden_columns"]
    formula_book = load_workbook(BytesIO(raw), read_only=True, data_only=False, keep_links=False)
    try:
        cache_book = load_workbook(BytesIO(raw), read_only=True, data_only=True, keep_links=False)
        try:
            recorded = [(item["name"], item["state"]) for item in sheets]
            for book in (formula_book, cache_book):
                actual = [(sheet.title, sheet.sheet_state) for sheet in book.worksheets]
                if actual != recorded:
                    raise ValueError("Workbook sheets differ from the P01 inventory")
            formula_sheet = formula_book[sheet_name]
            cache_sheet = cache_book[sheet_name]
            left, top, right, bottom = bounds
            rows = zip(
                formula_sheet.iter_rows(min_row=top, max_row=bottom, min_col=left, max_col=right),
                cache_sheet.iter_rows(min_row=top, max_row=bottom, min_col=left, max_col=right),
                strict=True,
            )
            cells = []
            for row_number, (formula_row, cache_row) in enumerate(rows, top):
                for column, (formula_cell, cache_cell) in enumerate(
                    zip(formula_row, cache_row, strict=True), left
                ):
                    coordinate = f"{get_column_letter(column)}{row_number}"
                    membership = [
                        {
                            "range": ref,
                            "role": "anchor" if (column, row_number) == (b[0], b[1]) else "member",
                        }
                        for ref, b in merged
                        if overlaps(b, row_number, column)
                    ]
                    is_formula = formula_cell.data_type == "f"
                    cells.append(
                        {
                            "sheet": sheet_name,
                            "coordinate": coordinate,
                            "row": row_number,
                            "column": column,
                            "formula_view": cell_view(formula_cell),
                            "data_only_view": cell_view(cache_cell),
                            "formula_cache": {
                                "state": (
                                    "available" if cache_cell.value is not None else "unavailable"
                                )
                                if is_formula
                                else "not_formula",
                                "freshness": "unknown" if is_formula else "not_applicable",
                            },
                            "structural_flags": {
                                "basis": "P01 manifest scope; declared worksheet XML metadata",
                                "sheet_state": metadata["state"],
                                "row_hidden": row_number in hidden_rows,
                                "column_hidden": any(
                                    span["min"] <= column <= span["max"] for span in hidden_columns
                                ),
                                "merged_membership": membership,
                            },
                        }
                    )
            expected = (right - left + 1) * (bottom - top + 1)
            if len(cells) != expected:
                raise ValueError("Requested worksheet range was not completely traversed")
            return cells
        finally:
            cache_book.close()
    finally:
        formula_book.close()


def integer(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Table coordinate is not numeric")
    if not math.isfinite(value) or int(value) != value:
        raise ValueError("Table coordinate is not integral")
    return int(value)


def comparison_status(docling_text, cell):
    value = cell["data_only_view"]["value"]
    if cell["formula_cache"]["state"] == "unavailable":
        return "formula_cache_unavailable"
    if value is None:
        return "match_empty" if docling_text == "" else "missing_native_value"
    if docling_text == "":
        return "missing_docling_value"
    if isinstance(value, dict):
        value = value.get("iso_value", value)
    return "match" if str(value) == docling_text else "difference"


def table_origin(table, group_refs):
    if table.get("parent", {}).get("$ref") not in group_refs:
        raise ValueError("Table is not bound to the selected worksheet group")
    provs = table.get("prov", [])
    if len(provs) != 1 or provs[0].get("page_no") != 1:
        raise ValueError("Table page provenance is ambiguous")
    bbox = provs[0].get("bbox", {})
    if bbox.get("coord_origin") != "TOPLEFT":
        raise ValueError("Table coordinate origin is not TOPLEFT")
    left, top, right, bottom = (integer(bbox[key]) for key in ("l", "t", "r", "b"))
    data = table["data"]
    if (
        left < 0
        or top < 0
        or right <= left
        or bottom <= top
        or right - left != data["num_cols"]
        or bottom - top != data["num_rows"]
    ):
        raise ValueError("Table bounding box and shape disagree")
    return left, top, right, bottom


def map_tables(document, cells, sheet_name=SHEET):
    """Map every Docling table cell; never invent a coordinate on invalid provenance."""
    group_refs = {
        group["self_ref"]
        for group in document.get("groups", [])
        if group.get("label") == "sheet" and group.get("name") == sheet_name
    }
    if len(group_refs) != 1:
        group_refs = set()
    by_coordinate = {cell["coordinate"]: cell for cell in cells}
    anchors, spans, table_regions = defaultdict(list), defaultdict(list), set()
    uncertain_regions = set()
    unknown_table_region = False
    comparisons, tables = [], []
    for table in document.get("tables", []):
        table_ref = table["self_ref"]
        source_cells = table["data"]["table_cells"]
        try:
            left, top, right, bottom = table_origin(table, group_refs)
            problem = None
        except (ValueError, KeyError, TypeError) as exc:
            problem = type(exc).__name__
        table_result = {
            "table_ref": table_ref,
            "original_provenance": table.get("prov", []),
            "mapping_state": "unmapped" if problem else "mapped",
            "mapping_issue": problem,
            "cell_count": len(source_cells),
        }
        if problem:
            unknown_table_region = True
        if not problem:
            table_result["a1_range"] = (
                f"{get_column_letter(left + 1)}{top + 1}:{get_column_letter(right)}{bottom}"
            )
            for item in cells:
                if left < item["column"] <= right and top < item["row"] <= bottom:
                    table_regions.add(item["coordinate"])
        tables.append(table_result)
        for index, source in enumerate(source_cells):
            ref = f"{table_ref}/data/table_cells/{index}"
            result = {
                "docling_cell_ref": ref,
                "docling_text": source.get("text"),
                "offsets": {
                    key: source.get(key)
                    for key in (
                        "start_row_offset_idx",
                        "end_row_offset_idx",
                        "start_col_offset_idx",
                        "end_col_offset_idx",
                    )
                },
                "coordinate": None,
                "span_a1": None,
                "status": "unmapped_provenance" if problem else None,
            }
            if not problem:
                try:
                    sr, er, sc, ec = (
                        integer(result["offsets"][key])
                        for key in (
                            "start_row_offset_idx",
                            "end_row_offset_idx",
                            "start_col_offset_idx",
                            "end_col_offset_idx",
                        )
                    )
                    if not (0 <= sr < er <= bottom - top and 0 <= sc < ec <= right - left):
                        raise ValueError("Table cell offset is outside table bounds")
                    if not isinstance(result["docling_text"], str):
                        raise ValueError("Docling table cell has no text")
                    coordinate = f"{get_column_letter(left + sc + 1)}{top + sr + 1}"
                    result["coordinate"] = coordinate
                    result["span_a1"] = f"{coordinate}:{get_column_letter(left + ec)}{top + er}"
                    target = by_coordinate.get(coordinate)
                    result["status"] = (
                        comparison_status(result["docling_text"], target)
                        if target
                        else "outside_requested_range"
                    )
                    if target:
                        anchors[coordinate].append({"ref": ref, "status": result["status"]})
                    for row in range(top + sr + 1, top + er + 1):
                        for column in range(left + sc + 1, left + ec + 1):
                            covered = f"{get_column_letter(column)}{row}"
                            if covered in by_coordinate and covered != coordinate:
                                spans[covered].append(ref)
                except (ValueError, TypeError, KeyError):
                    result["status"] = "unmapped_invalid_cell"
                    table_result["mapping_state"] = "partial"
                    table_result["mapping_issue"] = "invalid_cell_offset_or_text"
                    uncertain_regions.update(
                        item["coordinate"]
                        for item in cells
                        if left < item["column"] <= right and top < item["row"] <= bottom
                    )
            comparisons.append(result)
    for cell in cells:
        coordinate = cell["coordinate"]
        if anchors[coordinate]:
            state = "anchor_compared"
        elif spans[coordinate]:
            state = "covered_by_span_no_anchor"
        elif unknown_table_region or coordinate in uncertain_regions:
            state = "unresolved_docling_mapping"
        elif coordinate in table_regions:
            state = (
                "missing_table_cell"
                if cell["formula_view"]["value"] is not None
                else "blank_no_table_cell"
            )
        else:
            state = "outside_docling_table_regions"
        cell["docling_table_comparison"] = {
            "state": state,
            "anchor_cells": anchors[coordinate],
            "covering_span_refs": spans[coordinate],
        }
    return {
        "comparison_basis": (
            "Docling text versus Python string of openpyxl data_only value; "
            "datetime uses ISO text; strict, no Excel display-format rendering"
        ),
        "coordinate_basis": "Docling TOPLEFT zero-based bbox origin plus cell offsets; A1 is one-based",
        "tables": tables,
        "table_cells": comparisons,
        "table_cell_status_counts": dict(Counter(item["status"] for item in comparisons)),
        "native_coordinate_status_counts": dict(
            Counter(item["docling_table_comparison"]["state"] for item in cells)
        ),
    }


def supplement_config(versions, prior):
    return {
        "configuration_version": CONFIG_VERSION,
        "reader": "OpenpyxlWorkbookSupplement",
        "versions": {**versions, "workbook_supplement": SUPPLEMENT_VERSION},
        "options": {
            "read_only": True,
            "data_only_passes": [False, True],
            "keep_links": False,
            "formula_evaluation": False,
            "macros_executed": False,
            "network_used": False,
            "models_used": [],
        },
        "requested_scope": {"kind": "cell_range", "sheet": SHEET, "range": "A1:U105"},
        "prior_docling_execution_key": prior["execution_key"],
        "prior_docling_artifact_sha256": prior["result"]["json_sha256"],
    }


def bind_supplement(task, config, versions, prior):
    if config is None:
        return "execution_configuration_missing"
    reason = local_read.bind(task, prior["configured_profile"], versions)
    if reason:
        return reason
    if config != supplement_config(versions, prior):
        return "execution_configuration_unsupported"
    if prior["execution_key"] != local_read.execution_key(task, prior["configured_profile"]):
        return "prior_execution_identity_mismatch"
    return None


def verified_prior(prior, task, manifest_ref, versions):
    if (
        prior["execution_state"] != "success"
        or prior["result"]["conversion_status"] != "success"
        or prior["input_plan_id"] != task["parse_task_id"]
        or prior["input_ref"]["sha256"] != task["content_sha256"]
        or prior["input_manifest_ref"] != manifest_ref
        or prior["result"]["input_sha256_reverified"] != task["content_sha256"]
        or prior["configured_profile"]["versions"] != versions
        or prior["executed_profile"]["versions"] != versions
        or prior["result"]["versions"] != versions
    ):
        raise ValueError("Prior Docling result is not bound to this input and runtime")
    profile = prior["configured_profile"]
    if prior["executed_profile"] != {
        "reader": profile["reader"],
        "versions": profile["versions"],
        "options": profile["options"],
        "requested_scope": profile["requested_scope"],
    }:
        raise ValueError("Prior configured and executed profiles differ")
    path = Path(prior["ref"]).resolve(strict=True)
    expected = prior["result"]["json_sha256"]
    if (
        path != Path(prior["result"]["json_path"]).resolve(strict=True)
        or prior["artifact_hash"] != expected
        or prior["artifact_id"] != expected
        or local_read.sha256(path.read_bytes()) != expected
    ):
        raise ValueError("Prior Docling artifact reference or SHA-256 mismatch")
    return path


def run(output, p01_report=P01_REPORT, docling_report=DOCLING_REPORT):
    output = Path(output).resolve()
    allowed = OUTPUT.resolve()
    if not output.is_relative_to(allowed):
        raise ValueError("Output must stay inside the dedicated supplement directory")
    run_root = output / f"run-{uuid4().hex}"
    run_root.mkdir(parents=True, exist_ok=False)
    report = {
        "kind": "local_excel_supplement_evidence_not_formal_handoff",
        "execution_state": "not_started",
        "reader_started": False,
        "phase": "intake",
        "artifacts": {},
        "issues": [],
    }
    try:
        p01_path = Path(p01_report).resolve(strict=True)
        prior_report_path = Path(docling_report).resolve(strict=True)
        p01_digest = local_read.sha256(p01_path.read_bytes())
        prior_report_digest = local_read.sha256(prior_report_path.read_bytes())
        p01 = local_read.load_json(p01_path)
        manifest_path = Path(p01["first"]["manifest"]).resolve(strict=True)
        root = manifest_path.parents[1]
        manifest_digest = local_read.sha256(manifest_path.read_bytes())
        manifest_ref = {"ref": str(manifest_path), "sha256": manifest_digest}
        received = receive_handoff(
            manifest_path,
            root,
            expected_task_id="p01-real-fetch",
            expected_request_fingerprint=None,
            intake_context=None,
        )
        selected = next(
            item for item in received["materials"] if item["record"]["material_id"] == "XLSX"
        )
        record, raw = selected["record"], selected["content"]
        task = next(
            task
            for task in received["manifest"]["parse_tasks"]
            if task["parse_task_id"] == record["parse_task_id"]
        )
        if record["format"] != "xlsx" or record["representation"] != "original":
            raise ValueError("Selected input is not the original XLSX")
        if local_read.sha256(raw) != task["content_sha256"]:
            raise ValueError("Received workbook SHA-256 mismatch")
        versions = docling_worker.package_versions()
        unbound = local_read.parse_prepared(
            manifest_path,
            root,
            expected_task_id="p01-real-fetch",
            expected_request_fingerprint=None,
            intake_context=None,
            selected_material_ids={"XLSX"},
            configurations={},
            versions=versions,
            output_dir=run_root / "not-started",
        )
        unbound_execution = local_read.only_execution(unbound)
        if (
            unbound_execution["execution_state"] != "not_started"
            or unbound_execution["reader_started"]
        ):
            raise ValueError("Unbound reader unexpectedly started")
        prior_report = local_read.load_json(prior_report_path)
        prior = local_read.only_execution(prior_report["cases"]["xlsx"])
        prior_path = verified_prior(prior, task, manifest_ref, versions)
        config = supplement_config(versions, prior)
        reason = bind_supplement(task, config, versions, prior)
        report.update(
            {
                "source": {
                    "p01_report": {"ref": str(p01_path), "sha256": p01_digest},
                    "manifest": manifest_ref,
                    "content_root": str(root),
                    "workbook": {"ref": record["content"]["path"], **record["content"]},
                    "material_id": record["material_id"],
                    "intake_bound": received["intake_bound"],
                    "source_context_bound": selected["source_bound"],
                },
                "input_plan": {
                    "parse_task_id": task["parse_task_id"],
                    "parser": task["parser"],
                    "execution_state": task["execution_state"],
                },
                "unbound_check": unbound_execution,
                "prior_docling": {
                    "report": {"ref": str(prior_report_path), "sha256": prior_report_digest},
                    "artifact": {"ref": str(prior_path), "sha256": prior["artifact_hash"]},
                    "execution_key": prior["execution_key"],
                    "execution_state": prior["execution_state"],
                },
                "configuration": config,
                "unbound_check_inventory": unbound["inventory"],
                "material_states": [
                    {
                        "material_id": item["material_id"],
                        "preprocessing_state": item["preprocessing_state"],
                        "preprocessing_issues": item["preprocessing_issues"],
                        "supplement_selected": item["material_id"] == "XLSX",
                        "supplement_state": (
                            "not_started" if item["material_id"] == "XLSX" else "not_selected"
                        ),
                    }
                    for item in unbound["inventory"]
                ],
            }
        )
        if not selected["source_bound"]:
            report["issues"].append({"code": "intake_source_context_unavailable"})
        if reason:
            report["reason"] = reason
        else:
            report["execution_key"] = local_read.execution_key(task, config)
            report["reader_started"] = True
            report["phase"] = "native_read"
            cells = read_cells(raw, record["scope"])
            report["phase"] = "docling_mapping"
            docling_document = local_read.load_json(prior_path)
            comparison = map_tables(docling_document, cells)
            report["phase"] = "persist"
            report["artifacts"]["cells"] = save_json(
                run_root / "cells.json",
                {
                    "sheet": SHEET,
                    "range": "A1:U105",
                    "cells": cells,
                },
            )
            report["artifacts"]["comparison"] = save_json(run_root / "comparison.json", comparison)
            formulas = [cell for cell in cells if cell["formula_view"]["data_type"] == "f"]
            scope = record["scope"]
            report["coverage"] = {
                "requested_range": "CI A!A1:U105",
                "coordinates_traversed": len(cells),
                "source_nonempty": sum(c["formula_view"]["value"] is not None for c in cells),
                "formula_count": len(formulas),
                "formula_cache_states": dict(
                    Counter(c["formula_cache"]["state"] for c in formulas)
                ),
                "formula_cache_freshness": "unknown",
                "sheet_inventory": scope["sheets"],
                "metadata_basis": "P01 manifest XLSX preflight of the same SHA-256; sheet state cross-checked with both read-only workbook views",
                "other_sheets": "not_started",
                "hidden_sheet_contents": "not_verified",
                "selected_sheet_outside_requested_range": "not_verified",
                "docling_non_table_items": "not_compared",
            }
            cells_by_coordinate = {item["coordinate"]: item for item in cells}
            report["comparison_summary"] = {
                "table_count": len(comparison["tables"]),
                "table_cell_count": len(comparison["table_cells"]),
                "table_cell_status_counts": comparison["table_cell_status_counts"],
                "native_coordinate_status_counts": comparison["native_coordinate_status_counts"],
                "nonempty_by_table_relation": dict(
                    Counter(
                        item["docling_table_comparison"]["state"]
                        for item in cells
                        if item["formula_view"]["value"] is not None
                    )
                ),
                "difference_source_types": dict(
                    Counter(
                        cells_by_coordinate[difference["coordinate"]]["formula_view"]["data_type"]
                        for difference in comparison["table_cells"]
                        if difference["status"] == "difference"
                    )
                ),
            }
            report["execution_state"] = (
                "partial"
                if any(t["mapping_state"] != "mapped" for t in comparison["tables"])
                or any(c["status"].startswith("unmapped") for c in comparison["table_cells"])
                else "completed_bounded_read"
            )
            report["phase"] = "source_recheck"
            if (
                local_read.sha256(manifest_path.read_bytes()) != manifest_digest
                or local_read.sha256(local_read.verified_content(root, record["content"]))
                != task["content_sha256"]
                or local_read.sha256(p01_path.read_bytes()) != p01_digest
                or local_read.sha256(prior_report_path.read_bytes()) != prior_report_digest
                or local_read.sha256(prior_path.read_bytes()) != prior["artifact_hash"]
            ):
                raise ValueError("Source or prior artifact changed during supplement")
            report["integrity_rechecked_unchanged"] = True
            report["phase"] = "finished"
            next(item for item in report["material_states"] if item["supplement_selected"])[
                "supplement_state"
            ] = report["execution_state"]
    except Exception as exc:
        report["execution_state"] = "failed"
        if report["reader_started"]:
            for item in report.get("material_states", []):
                if item["supplement_selected"]:
                    item["supplement_state"] = "failed"
        report["issues"].append(
            {
                "code": "execution_failed",
                "phase": report["phase"],
                "exception_type": type(exc).__name__,
            }
        )
        save_json(run_root / "report.json", report)
        raise RuntimeError(
            f"Supplement failed in {report['phase']}; see {run_root / 'report.json'}"
        ) from None
    report_ref = save_json(run_root / "report.json", report)
    return {
        "report": report_ref,
        "execution_state": report["execution_state"],
        "reader_started": report["reader_started"],
        "unbound_check": report.get("unbound_check", {}).get("execution_state"),
        "coverage_counts": {
            key: report.get("coverage", {}).get(key)
            for key in (
                "coordinates_traversed",
                "source_nonempty",
                "formula_count",
                "formula_cache_states",
            )
        },
        "comparison_summary": report.get("comparison_summary"),
        "integrity_rechecked_unchanged": report.get("integrity_rechecked_unchanged", False),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    print(json.dumps(run(args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
