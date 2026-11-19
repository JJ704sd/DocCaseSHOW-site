"""Consume selected P01 plans and produce local, offline READ evidence."""

import argparse
import base64
import hashlib
import json
import os
import platform
import re
import statistics
import subprocess
import time
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from uuid import uuid4

from p02_markdown_pool import MarkdownWorkerPool
from pypdf import PdfReader
from pypdf.errors import DependencyError

from doccase_preprocessing.local_handoff import receive_handoff, run_local_handoff
from doccase_preprocessing.pdf_profile import (
    PDF_SCANNED,
    PDF_STRUCTURED,
    PDF_STRUCTURED_READERS,
    is_safety_sheet,
)
from doccase_preprocessing.pdf_profile import pdf_first_read as pdf_first_read

P01_REPORT = Path(os.environ.get("DOCCASE_P01_REPORT", "output/p01/report.json"))
INVENTORY = Path(os.environ.get("DOCCASE_INVENTORY", "private/inventory.json"))
DOCLING_PYTHON = Path(os.environ.get("DOCCASE_DOCLING_PYTHON", "python"))
MODEL_CACHE = Path(os.environ.get("DOCCASE_MODEL_CACHE", ".cache/models"))
WORKER = Path(__file__).with_name("p02_docling_worker.py")
NATIVE_PDF_ID = os.environ.get("DOCCASE_NATIVE_PDF_ID", "")
TEXT_READER_VERSION = "1.0"


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def _worker_environment(model_cache):
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONUTF8": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HOME": str(MODEL_CACHE if model_cache is None else model_cache),
            "CUDA_VISIBLE_DEVICES": "",
            "OMP_NUM_THREADS": "2",
            "DOCLING_NUM_THREADS": "2",
        }
    )
    return environment


def markdown_worker_pool(*, slots=2, python=None, model_cache=None, script=None):
    return MarkdownWorkerPool(
        [
            str(DOCLING_PYTHON if python is None else python),
            "-B",
            str(script or WORKER),
            "--markdown-stream",
        ],
        _worker_environment(model_cache),
        slots,
    )


def _timed_worker(command, request, environment, timings):
    payload = json.dumps({**request, "capture_timing": True}, ensure_ascii=False)
    timeout = request.get("hard_timeout_seconds")
    monotonic_start = time.monotonic()
    timings["launch_ns"] = time.time_ns()
    with subprocess.Popen(
        command,
        text=True,
        encoding="utf-8",
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    ) as process:
        timings["spawned_ns"] = time.time_ns()
        try:
            remaining = (
                None if timeout is None else max(0, timeout - (time.monotonic() - monotonic_start))
            )
            stdout, stderr = process.communicate(payload, timeout=remaining)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        finally:
            timings["host_return_ns"] = time.time_ns()
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def worker(request, *, python=None, model_cache=None, timing_sink=None):
    return _run_worker(request, python=python, model_cache=model_cache, timing_sink=timing_sink)


def _run_worker(request, *, python=None, model_cache=None, timing_sink=None):
    command = [str(DOCLING_PYTHON if python is None else python), str(WORKER)]
    timing = {}
    try:
        if timing_sink is not None and request["operation"] == "convert":
            process = _timed_worker(command, request, _worker_environment(model_cache), timing)
        else:
            process = subprocess.run(
                command,
                input=json.dumps(request, ensure_ascii=False),
                text=True,
                encoding="utf-8",
                capture_output=True,
                env=_worker_environment(model_cache),
                check=False,
                timeout=request.get("hard_timeout_seconds"),
            )
    except subprocess.TimeoutExpired:
        if timing:
            timing_sink({**timing, "status": "timeout"})
        if request["operation"] in {"probe", "probe_ocr", "pdf_page_count", "inspect_pdf_scan"}:
            raise TimeoutError("Docling environment probe exceeded its deadline") from None
        return {
            "conversion_status": "failure",
            "errors": [
                {"code": "worker_timeout", "message": "Docling worker exceeded its hard deadline"}
            ],
            "versions": request["versions"],
            "json_path": None,
            "json_sha256": None,
            "termination_reason": "timeout",
        }
    if process.returncode:
        if timing:
            timing_sink({**timing, "status": "worker_exit"})
        detail = re.sub(r"https?://\S+", "[reference withheld]", process.stderr)
        raise RuntimeError(f"Docling worker exited {process.returncode}: {detail}")
    response = json.loads(process.stdout)
    if timing:
        timing_sink(
            {
                **timing,
                **response.pop("_worker_timing", {}),
                "status": response.get("conversion_status"),
            }
        )
    if request["operation"] not in {"probe", "probe_ocr", "pdf_page_count", "inspect_pdf_scan"}:
        response["worker_diagnostics"] = [
            re.sub(r"https?://\S+", "[reference withheld]", line)
            for line in process.stderr.splitlines()
            if line.strip()
        ]
    return response


def inspect_pdf_scan(content_root, content, *, python=None, model_cache=None, timeout=5):
    """Run the bounded PDF geometry probe separately from the conversion worker."""
    if not content or timeout <= 0:
        return "probe_skipped"
    try:
        result = _run_worker(
            {
                "operation": "inspect_pdf_scan",
                "content_root": str(Path(content_root).resolve()),
                "content_path": content["path"],
                "sha256": content["sha256"],
                "hard_timeout_seconds": min(5, timeout),
            },
            python=python,
            model_cache=model_cache,
        )
    except TimeoutError:
        return "probe_timeout"
    except (OSError, RuntimeError, ValueError, KeyError):
        return "probe_failed"
    scan = result.get("scan") if isinstance(result, dict) else None
    return (
        scan
        if isinstance(scan, str)
        and scan in {"image_only", "has_text_layer", "page_limit", "uncertain"}
        else "probe_failed"
    )


def select_pdf_first_read(content_root, record, *, python=None, model_cache=None, timeout=5):
    filename = record.get("filename")
    scan = (
        "safety_sheet"
        if is_safety_sheet(filename)
        else inspect_pdf_scan(
            content_root,
            record.get("content"),
            python=python,
            model_cache=model_cache,
            timeout=timeout,
        )
    )
    return {"route": pdf_first_read(filename, scan=scan), "scan": scan}


def export_read_markdown(execution, record, records, content_root, output_dir, *, worker_fn=None):
    """Use Docling's serializer on the verified READ without parsing the input again."""
    if execution.get("execution_state") not in {"success", "partial_success"} or not execution.get(
        "ref"
    ):
        return {"state": "failure", "reason": "read_unavailable"}
    source_record = record
    if record.get("format") == "markdown" and record.get("parent_material_id"):
        source_record = records[record["parent_material_id"]]
    request = {
        "operation": "export_markdown",
        "read_path": execution["ref"],
        "read_sha256": execution["artifact_hash"],
        "material_id": record["material_id"],
        "execution_id": execution["execution_id"],
        "source_kind": "current_body" if record["kind"] == "body" else "attachment",
        "output_dir": str(Path(output_dir).resolve()),
        "read_state": execution["execution_state"],
        "unread_pages": execution.get("result", {}).get("unread_pages", []),
    }
    if source_record.get("format") in {"html", "text"}:
        request["source_path"] = str(
            (Path(content_root) / source_record["content"]["path"]).resolve()
        )
        request["source_sha256"] = source_record["content"]["sha256"]
    if execution.get("executed_profile", {}).get("reader") == "DoclingExcel":
        selected_sheet = execution.get("result", {}).get("excel_evidence", {}).get("selected_sheet")
        if selected_sheet:
            request["worksheet"] = selected_sheet
    result = (worker if worker_fn is None else worker_fn)(request)
    if result["state"] == "failure":
        return result
    for kind in ("markdown", "index"):
        if sha256(Path(result[kind + "_ref"]).read_bytes()) != result[kind + "_sha256"]:
            raise ValueError("Markdown sidecar SHA-256 mismatch")
    return result


def _structured_pdf_pages(passes):
    return {
        item["pages"][0]
        for item in passes
        if item.get("artifact_ref")
        and item.get("profile", {}).get("do_table_structure")
        and item["pages"][0] == item["pages"][1]
    }


def _resolve_merged_pdf_cells(parts):
    """Use a native text box on the same row to separate a merged header and value."""
    native = []
    for part_index, part in enumerate(parts):
        if part["kind"] != "native":
            continue
        document = part["document"]
        for index, item in enumerate(document.get("texts", [])):
            for provenance in item.get("prov", []):
                page = provenance.get("page_no")
                box = provenance.get("bbox", {})
                height = document.get("pages", {}).get(str(page), {}).get("size", {}).get("height")
                if height and box.get("coord_origin") == "BOTTOMLEFT":
                    native.append(
                        {
                            "page": page,
                            "text": item.get("text", ""),
                            "pointer": f"/parts/{part_index}/document/texts/{index}/text",
                            "box": {
                                "l": box["l"],
                                "r": box["r"],
                                "t": height - box["t"],
                                "b": height - box["b"],
                            },
                        }
                    )

    resolved = []
    for part_index, part in enumerate(parts):
        if part["kind"] != "structure":
            continue
        for table_index, table in enumerate(part["document"].get("tables", [])):
            pages = {item.get("page_no") for item in table.get("prov", [])}
            if len(pages) != 1:
                continue
            page = next(iter(pages))
            cells = table.get("data", {}).get("table_cells", [])
            for cell_index, cell in enumerate(cells):
                match = re.fullmatch(
                    r".*[A-Za-z\u4e00-\u9fff].*?\s+(\d+(?:\.\d+)?)\s*", cell.get("text", "")
                )
                box = cell.get("bbox") or {}
                row = cell.get("start_row_offset_idx")
                if not match or box.get("coord_origin") != "TOPLEFT":
                    continue
                peers = [
                    (index, other)
                    for index, other in enumerate(cells)
                    if index != cell_index
                    and other.get("start_row_offset_idx") == row
                    and other.get("end_row_offset_idx") == row + 1
                    and (other.get("bbox") or {}).get("coord_origin") == "TOPLEFT"
                ]
                if not peers:
                    continue
                top = statistics.median(other["bbox"]["t"] for _, other in peers)
                bottom = statistics.median(other["bbox"]["b"] for _, other in peers)
                if bottom <= top or box["b"] - box["t"] <= 2 * (bottom - top):
                    continue
                candidates = [
                    item
                    for item in native
                    if item["page"] == page
                    and item["text"].strip() == match.group(1)
                    and max(item["box"]["l"], box["l"]) < min(item["box"]["r"], box["r"])
                    and max(item["box"]["t"], top) < min(item["box"]["b"], bottom)
                ]
                if len(candidates) != 1:
                    continue
                row_index, _ = min(peers, key=lambda item: item[1]["start_col_offset_idx"])
                prefix = f"/parts/{part_index}/document/tables/{table_index}/data/table_cells"
                source_text = candidates[0]["text"]
                start = len(source_text) - len(source_text.lstrip())
                result = {
                    "page": page,
                    "row": row,
                    "column": cell["start_col_offset_idx"],
                    "value": source_text.strip(),
                    "source_pointer": candidates[0]["pointer"],
                    "char_start": start,
                    "char_end": start + len(source_text.strip()),
                    "table_cell_pointer": f"{prefix}/{cell_index}/text",
                    "row_cell_pointer": f"{prefix}/{row_index}/text",
                    "method": "native_text_on_table_row",
                }
                header = cell["text"][: match.start(1)].strip()

                def compact(value):
                    return re.sub(r"\s+", "", value)

                header_parts = sorted(
                    (
                        item
                        for item in native
                        if item["page"] == page
                        and item["text"].strip()
                        and compact(item["text"]) in compact(header)
                        and max(item["box"]["l"], box["l"]) < min(item["box"]["r"], box["r"])
                        and item["box"]["t"] >= box["t"] - 1
                        and item["box"]["b"] <= top
                    ),
                    key=lambda item: (item["box"]["t"], item["box"]["l"]),
                )
                if header_parts and compact(
                    "".join(item["text"] for item in header_parts)
                ) == compact(header):
                    result["header_fragment"] = header
                    result["header_source_pointers"] = [item["pointer"] for item in header_parts]
                resolved.append(result)
    return resolved


def read_pdf_adaptive(
    request,
    *,
    worker_fn=None,
    force_structure=False,
    structure_pages=None,
    document_timeout=45,
    supplement_timeout=45,
    task_timeout=180,
    reuse_result=None,
    ocr_page=None,
):
    """Read native PDF once, then run Docling OCR/structure only for selected pages.

    Each pass remains independently addressable; the bundle embeds the exact
    exported documents and their hashes so extraction can retain one snapshot.
    """
    if request.get("format") != "pdf":
        return (worker if worker_fn is None else worker_fn)(request)
    if any(
        not isinstance(x, (int, float)) or x <= 0
        for x in (document_timeout, supplement_timeout, task_timeout)
    ):
        raise ValueError("PDF time budgets must be positive")
    call = worker if worker_fn is None else worker_fn
    start = time.monotonic()
    deadline = start + task_timeout
    target = Path(request["json_path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    root = Path(request["content_root"]).resolve(strict=True)
    relative = Path(request["content_path"])
    source = (root / relative).resolve(strict=True)
    if relative.is_absolute() or not source.is_relative_to(root):
        raise ValueError("PDF source escapes its controlled content root")
    raw = source.read_bytes()
    if sha256(raw) != request["sha256"]:
        raise ValueError("PDF content SHA-256 mismatch")
    page_range = request["requested_scope"].get("page_range")
    if page_range:
        first, last = page_range
    else:
        try:
            page_count = len(PdfReader(source).pages)
        except DependencyError:
            probe = {
                "operation": "pdf_page_count",
                "content_root": str(root),
                "content_path": str(relative),
                "sha256": request["sha256"],
                "hard_timeout_seconds": min(30, max(0.001, deadline - time.monotonic())),
            }
            page_count = call(probe)["page_count"]
        first, last = 1, page_count
    if not 1 <= first <= last:
        raise ValueError("PDF page range is outside the document")
    expected = list(range(first, last + 1))
    parts, passes, issues = [], [], []

    def run_pass(kind, pages, timeout, *, trigger=None):
        entry = {"kind": kind, "pages": pages or [first, last]}
        if trigger:
            entry["trigger"] = trigger
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            passes.append({**entry, "status": "not_started", "reason": "task_timeout"})
            issues.append(
                {
                    "code": "task_timeout",
                    "message": "PDF task deadline reached before this pass",
                    "pages": pages,
                }
            )
            return None
        part_request = dict(request)
        profile = dict(request["profile"])
        if kind != "native":
            profile = {
                key: profile[key]
                for key in (
                    "cache_path",
                    "device",
                    "threads",
                    "offline",
                    "enable_remote_services",
                    "allow_external_plugins",
                )
                if key in profile
            }
            use_ocr = kind == "ocr" or (kind == "structure" and pages[0] not in with_text)
            profile.update(
                pipeline="StandardPdfPipeline",
                do_ocr=use_ocr,
                do_table_structure=kind == "structure",
                table_mode="accurate",
                ocr_engine="RapidOCR" if use_ocr else "not_used",
            )
        profile["document_timeout"] = min(timeout, remaining)
        part_request["profile"] = profile
        part_request["requested_scope"] = (
            {"kind": "page_range", "page_range": pages} if pages else request["requested_scope"]
        )
        part_request["json_path"] = str(
            target.with_name(target.stem + f".{kind}-{uuid4().hex}.json")
        )
        part_request["hard_timeout_seconds"] = min(remaining, timeout + 30)
        before = time.monotonic()
        try:
            result = call(part_request)
        except subprocess.TimeoutExpired:
            result = {
                "conversion_status": "failure",
                "errors": [
                    {
                        "code": "worker_timeout",
                        "message": "Docling worker exceeded its hard deadline",
                    }
                ],
                "versions": request["versions"],
                "json_path": None,
                "json_sha256": None,
                "termination_reason": "timeout",
            }
        entry.update(
            status=result["conversion_status"],
            seconds=round(time.monotonic() - before, 3),
            profile=profile,
            versions=result.get("versions"),
            effective_configuration=result.get("effective_ocr_configuration"),
            errors=result.get("errors", []),
            termination_reason=result.get("termination_reason"),
        )
        if result.get("json_path"):
            data = Path(result["json_path"]).read_bytes()
            if sha256(data) != result["json_sha256"]:
                raise ValueError("PDF pass artifact SHA-256 mismatch")
            part = {
                "kind": kind,
                "pages": pages or [first, last],
                "ref": result["json_path"],
                "sha256": result["json_sha256"],
                "document": json.loads(data),
            }
            parts.append(part)
            entry["artifact_ref"] = {"ref": part["ref"], "sha256": part["sha256"]}
        passes.append(entry)
        issues.extend(result.get("errors", []))
        return result

    if reuse_result is None:
        native = run_pass("native", list(page_range) if page_range else None, document_timeout)
    else:
        prior = Path(reuse_result["json_path"]).read_bytes()
        if sha256(prior) != reuse_result["json_sha256"]:
            raise ValueError("Prior PDF read bundle SHA-256 mismatch")
        bundle = json.loads(prior)
        if (
            bundle.get("kind") != "docling_pdf_read_bundle"
            or bundle["input_sha256"] != request["sha256"]
        ):
            raise ValueError("Prior PDF read does not match this source")
        parts = bundle["parts"][:]
        passes = bundle["passes"][:]
        issues = [issue for item in passes for issue in item.get("errors", [])]
        native = reuse_result

    def nonempty_pages(part):
        document = part["document"]
        pages = set()
        for item in document.get("texts", []):
            if str(item.get("text", "")).strip():
                pages.update(p["page_no"] for p in item.get("prov", []) if p.get("page_no"))
        for table in document.get("tables", []):
            if any(
                str(cell.get("text", "")).strip()
                for cell in table.get("data", {}).get("table_cells", [])
            ):
                pages.update(p["page_no"] for p in table.get("prov", []) if p.get("page_no"))
        return pages

    native_parts = [part for part in parts if part["kind"] == "native"]
    native_pages = {
        int(page) for part in native_parts for page in part["document"].get("pages", {})
    }
    if not native_pages and native and native.get("document_pages"):
        native_pages = set(native["document_pages"])
    with_text = (
        set().union(*(nonempty_pages(part) for part in native_parts)) if native_parts else set()
    )
    eligible = native_pages & set(expected)
    ocr_pages = eligible - with_text if reuse_result is None else set()
    if ocr_page in eligible and not any(
        p["kind"] == "ocr" and p["pages"] == [ocr_page, ocr_page] and p["status"] == "success"
        for p in passes
    ):
        ocr_pages.add(ocr_page)
    for page in sorted(ocr_pages):
        run_pass(
            "ocr",
            [page, page],
            supplement_timeout,
            trigger="explicit_page"
            if page == ocr_page and (page in with_text or reuse_result)
            else None,
        )
    chosen = set(structure_pages or [])
    if force_structure:
        chosen.update(set().union(*(nonempty_pages(part) for part in parts)) & set(expected))
    if not chosen.issubset(expected):
        raise ValueError("Table supplement page is outside the requested range")
    already_structured = _structured_pdf_pages(passes)
    for page in sorted(chosen - already_structured):
        run_pass("structure", [page, page], supplement_timeout)

    if not parts:
        return {
            "conversion_status": "failure",
            "errors": issues or [{"code": "no_pdf_read_artifact"}],
            "versions": request["versions"],
            "json_path": None,
            "json_sha256": None,
            "document_pages": [],
            "unread_pages": expected,
            "text_items": 0,
            "table_items": 0,
            "passes": passes,
            "read_strategy": "adaptive_pdf",
            "termination_reason": "timeout"
            if any(
                p.get("termination_reason") == "timeout" or p.get("reason") == "task_timeout"
                for p in passes
            )
            else "failure",
        }
    observed = set().union(*(nonempty_pages(part) for part in parts))
    unread = sorted(set(expected) - observed)
    stopped = any(p["status"] != "success" for p in passes)
    timed_out = any(
        p.get("termination_reason") == "timeout" or p.get("reason") == "task_timeout"
        for p in passes
    )
    resolved_cells = _resolve_merged_pdf_cells(parts)
    bundle = {
        "kind": "docling_pdf_read_bundle",
        "input_sha256": request["sha256"],
        "expected_pages": expected,
        "parts": parts,
        "passes": passes,
        "resolved_table_cells": resolved_cells,
    }
    with target.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(bundle, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return {
        "conversion_status": "partial_success" if unread or stopped else "success",
        "errors": issues,
        "versions": request["versions"],
        "json_path": str(target),
        "json_sha256": sha256(target.read_bytes()),
        "document_pages": sorted(native_pages),
        "text_items": sum(len(part["document"].get("texts", [])) for part in parts),
        "table_items": sum(len(part["document"].get("tables", [])) for part in parts),
        "unread_pages": unread,
        "passes": passes,
        "read_strategy": "adaptive_pdf",
        "resolved_table_cells": resolved_cells,
        "termination_reason": "timeout" if timed_out else "normal",
        "elapsed_seconds": round(time.monotonic() - start, 3),
    }


def pdf_table_supplement_pages(result, unresolved_fields):
    """Select observed pages with table-like headers and unresolved requested fields."""
    if not unresolved_fields or not result.get("json_path"):
        return []
    raw = Path(result["json_path"]).read_bytes()
    if sha256(raw) != result["json_sha256"]:
        raise ValueError("PDF read bundle SHA-256 mismatch")
    bundle = json.loads(raw)
    if bundle.get("kind") != "docling_pdf_read_bundle":
        return []
    structured = _structured_pdf_pages(bundle["passes"])
    page_text = {}
    for part in bundle["parts"]:
        if part["kind"] not in {"native", "ocr"}:
            continue
        for item in part["document"].get("texts", []):
            for provenance in item.get("prov", []):
                page = provenance.get("page_no")
                if page:
                    page_text.setdefault(page, []).append(str(item.get("text", "")))
    cues = (
        r"\bn\s*[./]?\s*w\b|net\s*weight",
        r"\bg\s*[./]?\s*w\b|gross\s*weight",
        r"\bcbm\b|m[³3]|\bvolume\b",
        r"\bcartons?\b|\bctns?\b|\bpackages?\b",
        r"\bqty\b|\bquantity\b",
        r"\btotal\b|合计",
    )
    return sorted(
        page
        for page, blocks in page_text.items()
        if page not in structured
        and sum(any(re.search(pattern, block, re.I) for pattern in cues) for block in blocks) >= 2
        and sum(bool(re.search(pattern, " ".join(blocks), re.I)) for pattern in cues) >= 2
    )


def pdf_supplement_support(
    *, document_timeout=45, supplement_timeout=45, deadline=None, task_timeout=180, ocr_page=None
):
    """Supply the existing P02 PDF operations to the common main entry."""
    return {
        "read_pdf_adaptive": read_pdf_adaptive,
        "configurations": configurations,
        "table_supplement_pages": pdf_table_supplement_pages,
        "document_timeout": document_timeout,
        "supplement_timeout": supplement_timeout,
        "deadline": deadline,
        "task_timeout": task_timeout,
        "ocr_page": ocr_page,
    }


def bind(task, config, versions):
    if config is None:
        return "execution_configuration_missing"
    expected_reader = {
        ("text", "text"): "TextReader",
        ("docling", "pdf"): "DoclingNativePdf",
        ("docling", "xlsx"): "DoclingExcel",
        ("docling", "html"): "DoclingHtml",
        ("docling", "markdown"): "DoclingMarkdown",
    }.get((task["route"], task["format"]))
    if (task["route"], task["format"]) == ("text", "text") and config.get(
        "reader"
    ) == "DoclingText":
        expected_reader = "DoclingText"
    if (task["route"], task["format"]) == ("docling", "pdf") and config.get(
        "reader"
    ) in PDF_STRUCTURED_READERS:
        expected_reader = config["reader"]
    if not expected_reader or config.get("reader") != expected_reader:
        return "reader_not_bound_for_route"
    actual = (
        {"python": platform.python_version(), "text_reader": TEXT_READER_VERSION}
        if expected_reader == "TextReader"
        else versions
    )
    if config.get("versions") != actual:
        return "reader_version_mismatch"
    plan_version = task["parser"]["version"]
    selected_version = (
        actual["text_reader"] if expected_reader == "TextReader" else actual["docling"]
    )
    if plan_version not in {"unbound", selected_version}:
        return "plan_version_mismatch"
    options = config.get("options", {})
    scope = config.get("requested_scope")
    if not isinstance(scope, dict):
        return "execution_configuration_unsupported"
    try:
        supported = configurations(
            cache_path=options.get("cache_path"),
            sheet_name=options.get("sheet_name", "CI A"),
            declared_versions=versions if expected_reader != "TextReader" else None,
            pdf_text_cell_unit=options.get("text_cell_unit", "line"),
            pdf_page_range=scope.get("page_range") if scope.get("kind") == "page_range" else None,
            docling_text=expected_reader == "DoclingText",
            pdf_first_read={PDF_SCANNED: "scanned_ocr", PDF_STRUCTURED: "packing_table"}.get(
                expected_reader, "native"
            ),
        )
    except ValueError:
        return "execution_configuration_unsupported"
    if config != supported[(task["route"], task["format"])]:
        return "execution_configuration_unsupported"
    if expected_reader != "TextReader" and not Path(config["options"]["cache_path"]).is_dir():
        return "local_cache_unavailable"
    return None


def execution_key(task, config):
    identity = {
        "input_plan_id": task["parse_task_id"],
        "input_content_sha256": task["content_sha256"],
        "actual_configuration": config,
        "requested_scope": config["requested_scope"],
    }
    return sha256(canonical(identity))


def text_read(raw, target):
    text = raw.decode("utf-8-sig")
    result = {
        "reader": "TextReader",
        "reader_version": TEXT_READER_VERSION,
        "text": text,
        "location": {
            "basis": "decoded_representation_unicode_codepoints",
            "start": 0,
            "end": len(text),
        },
        "page_location": None,
    }
    with target.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return {
        "conversion_status": "success",
        "errors": [],
        "json_path": str(target),
        "json_sha256": sha256(target.read_bytes()),
        "characters_read": len(text),
        "location": result["location"],
        "page_location": None,
    }


def occurrence(record, source_bound):
    return {
        "material_id": record["material_id"],
        "email_ref_id": record["email_ref_id"],
        "role_raw": record["role"],
        "source_role": "supplement" if record["role"] == "supplementary" else record["role"],
        "kind": record["kind"],
        "parent_material_id": record["parent_material_id"],
        "source_path": record["metadata"].get("source_path"),
        "mime_path": record["metadata"].get("mime_path"),
        "representation": record["representation"],
        "representation_sha256": record["content"]["sha256"] if record["content"] else None,
        "original_sha256": record["original"]["sha256"] if record["original"] else None,
        "intake_source_bound": source_bound,
    }


def parse_prepared(
    manifest_path,
    content_root,
    *,
    expected_task_id,
    expected_request_fingerprint,
    intake_context,
    selected_material_ids,
    configurations,
    versions,
    output_dir,
    worker_fn=None,
):
    manifest_path = Path(manifest_path)
    manifest_digest = sha256(manifest_path.read_bytes())
    received = receive_handoff(
        manifest_path,
        content_root,
        expected_task_id=expected_task_id,
        expected_request_fingerprint=expected_request_fingerprint,
        intake_context=intake_context,
    )
    manifest = received["manifest"]
    by_id = {item["record"]["material_id"]: item for item in received["materials"]}
    if not set(selected_material_ids).issubset(by_id):
        raise ValueError("A selected material is absent from the manifest")
    records = []
    for item in received["materials"]:
        record = item["record"]
        status = record["state"] if record["state"] in {"blocked", "preserved"} else "not_selected"
        records.append(
            {
                **occurrence(record, item["source_bound"]),
                "preprocessing_state": record["state"],
                "preprocessing_issue_codes": [issue["code"] for issue in record["issues"]],
                "preprocessing_issues": record["issues"],
                "selected_for_parsing": record["selected_for_parsing"],
                "input_plan_id": record["parse_task_id"],
                "read_state": status,
                "reason": (
                    "not_selected_for_this_local_run"
                    if status == "not_selected"
                    else "not_selected_for_parsing_by_p01"
                    if status == "preserved"
                    else None
                ),
                "source_context_gap": not item["source_bound"],
                "execution_id": None,
            }
        )
    by_record_id = {record["material_id"]: record for record in records}
    executions = []
    for task in manifest["parse_tasks"]:
        selected = [
            material_id
            for material_id in task["material_ids"]
            if material_id in selected_material_ids
        ]
        if not selected:
            continue
        config = configurations.get((task["route"], task["format"]))
        reason = bind(task, config, versions)
        if reason:
            for material_id in selected:
                by_record_id[material_id]["read_state"] = "not_started"
                by_record_id[material_id]["reason"] = reason
            executions.append(
                {
                    "input_plan_id": task["parse_task_id"],
                    "material_ids": task["material_ids"],
                    "execution_state": "not_started",
                    "reason": reason,
                    "reader_started": False,
                }
            )
            continue
        selected_item = by_id[selected[0]]
        record = selected_item["record"]
        key = execution_key(task, config)
        execution_id = uuid4().hex
        artifact_path = Path(output_dir) / "artifacts" / f"{execution_id}.json"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        if task["route"] == "text" and config["reader"] == "TextReader":
            result = text_read(selected_item["content"], artifact_path)
            executed_versions = config["versions"]
        else:
            request = {
                "operation": "convert",
                "content_root": str(Path(content_root).resolve()),
                "content_path": task["content"]["path"],
                "sha256": task["content_sha256"],
                "format": task["format"],
                "versions": config["versions"],
                "profile": config["options"],
                "requested_scope": config["requested_scope"],
                "json_path": str(artifact_path.resolve()),
            }
            try:
                result = (worker if worker_fn is None else worker_fn)(request)
            except (RuntimeError, TimeoutError) as error:
                if config["reader"] != PDF_SCANNED:
                    raise
                code = "timeout" if isinstance(error, TimeoutError) else "ocr_worker_exit"
                result = {
                    "conversion_status": "failure",
                    "versions": config["versions"],
                    "errors": [{"code": code}],
                    "termination_reason": code,
                    "json_path": None,
                    "json_sha256": None,
                }
            if config["reader"] == PDF_SCANNED:
                if (
                    result["conversion_status"] in {"success", "partial_success"}
                    and result.get("input_sha256_reverified") != task["content_sha256"]
                ):
                    raise ValueError("Docling OCR input hash mismatch")
                if result["conversion_status"] == "failure" and not result.get(
                    "termination_reason"
                ):
                    result = {**result, "termination_reason": "ocr_conversion_failed"}
                elif (
                    result["conversion_status"] in {"success", "partial_success"}
                    and not result.get("termination_reason")
                    and result.get("text_items") == result.get("table_items") == 0
                ):
                    result = {
                        **result,
                        "conversion_status": "partial_success",
                        "termination_reason": "ocr_no_usable_content",
                        "errors": [*result["errors"], {"code": "ocr_no_usable_content"}],
                    }
            executed_versions = result["versions"]
            if (
                task["format"] == "markdown"
                and result["conversion_status"] in {"success", "partial_success"}
                and result.get("input_sha256_reverified") != task["content_sha256"]
            ):
                raise ValueError("Docling Markdown input hash mismatch")
        state = result["conversion_status"]
        for material_id in selected:
            by_record_id[material_id]["read_state"] = state
            by_record_id[material_id]["reason"] = (
                "timeout"
                if result.get("termination_reason") == "timeout"
                else result.get("termination_reason")
                if config["reader"] == PDF_SCANNED and result.get("termination_reason")
                else "pages_unread"
                if result.get("unread_pages")
                else None
            )
            by_record_id[material_id]["execution_id"] = execution_id
        executions.append(
            {
                "artifact_semantics": "parsed-artifact/0.1-draft",
                "artifact_id": result["json_sha256"],
                "artifact_hash": result["json_sha256"],
                "ref": result["json_path"],
                "input_manifest_ref": {"ref": str(manifest_path), "sha256": manifest_digest},
                "input_plan_id": task["parse_task_id"],
                "execution_id": execution_id,
                "execution_key": key,
                "input_ref": {"sha256": task["content_sha256"]},
                "occurrence_bindings": [
                    occurrence(by_id[material_id]["record"], by_id[material_id]["source_bound"])
                    for material_id in task["material_ids"]
                ],
                "configured_profile": config,
                "executed_profile": {
                    "reader": "DoclingPdfAdaptive"
                    if result.get("read_strategy") == "adaptive_pdf"
                    else config["reader"],
                    "versions": executed_versions,
                    "options": config["options"],
                    "requested_scope": config["requested_scope"],
                    **(
                        {"passes": result["passes"]}
                        if result.get("read_strategy") == "adaptive_pdf"
                        else {}
                    ),
                },
                "execution_state": state,
                "reader_started": True,
                "structure_refs": (
                    [
                        {
                            "kind": "text_json"
                            if config["reader"] == "TextReader"
                            else "docling_pdf_read_bundle_json"
                            if result.get("read_strategy") == "adaptive_pdf"
                            else "docling_document_json",
                            "ref": result["json_path"],
                            "sha256": result["json_sha256"],
                        }
                    ]
                    if result["json_path"]
                    else []
                ),
                "mapping_ref": (
                    {
                        "ref": result["json_path"],
                        "sha256": result["json_sha256"],
                        "basis": (
                            "decoded_representation_unicode_codepoints"
                            if config["reader"] == "TextReader"
                            else "docling_native_provenance"
                        ),
                    }
                    if result["json_path"]
                    else None
                ),
                "coverage": {"requested_scope": config["requested_scope"], "actual_scope": None},
                "issues": [
                    {
                        "code": "intake_source_context_unavailable",
                        "material_id": material_id,
                    }
                    for material_id in task["material_ids"]
                    if not by_id[material_id]["source_bound"]
                ]
                + result["errors"],
                "result": result,
            }
        )
    if sha256(manifest_path.read_bytes()) != manifest_digest:
        raise ValueError("P01 manifest changed during READ")
    return {
        "task_id": manifest["task_id"],
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_digest,
        "intake_bound": received["intake_bound"],
        "inventory": records,
        "executions": executions,
    }


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def verified_content(root, descriptor):
    root = Path(root).resolve(strict=True)
    relative = Path(descriptor["path"])
    if relative.is_absolute():
        raise ValueError("Content path must be relative")
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root):
        raise ValueError("Content path escapes the trusted root")
    raw = path.read_bytes()
    if sha256(raw) != descriptor["sha256"]:
        raise ValueError("Content SHA-256 mismatch")
    return raw


def indexed_native_pdf(output):
    inventory = load_json(INVENTORY)
    item = next(item for item in inventory["items"] if item["id"] == NATIVE_PDF_ID)
    root = INVENTORY.parent.resolve(strict=True)
    source = (root / item["source_file"]).resolve(strict=True)
    if not source.is_relative_to(root / "sources"):
        raise ValueError("Indexed PDF source is outside the trusted source directory")
    raw = source.read_bytes()
    if sha256(raw) != item["sha256"] or len(raw) != item["size"]:
        raise ValueError("Indexed PDF differs from the source inventory")
    reader = PdfReader(source)
    if reader.is_encrypted or not any(page.extract_text() for page in reader.pages):
        raise ValueError("Indexed replacement has no usable native text layer")
    payload = {
        "task_id": "p02-indexed-native-pdf",
        "email": {
            "email_ref_id": "p02-local-index",
            "subject": "Local indexed native-text PDF",
            "from_addr": "local@example.invalid",
            "from_name": "Local sample",
            "to_addrs": ["reader@example.invalid"],
            "sent_at": "2026-11-09T10:00:00+08:00",
            "snippet": "Indexed local PDF",
            "thread_headers": {"message_id": "<p02-local@example.invalid>"},
            "body_text": "Read the indexed local PDF.",
            "attachments": [
                {
                    "material_id": "NATIVE_PDF",
                    "filename": "indexed.pdf",
                    "mime": "application/pdf",
                    "content_base64": base64.b64encode(raw).decode("ascii"),
                    "size_bytes": item["size"],
                    "sha256": item["sha256"],
                }
            ],
        },
    }
    prepared = run_local_handoff(
        canonical(payload),
        output,
        input_format="json",
        request_reference="intake:p02-local-index",
    )
    return prepared, {
        "item_id": item["id"],
        "inventory_path": str(INVENTORY),
        "inventory_sha256": sha256(INVENTORY.read_bytes()),
        "source_file": str(source),
        "source_sha256": item["sha256"],
        "source_size_bytes": item["size"],
        "asset_refs": item["asset_refs"],
        "mime_refs": item["mime_refs"],
        "native_text_page_count": len(reader.pages),
        "selection_reason": "P01 PDF samples had no extractable native text",
    }


def normalized(text):
    return "".join(character.casefold() for character in text if character.isalnum())


def output_blocks(document):
    blocks = []
    for item in document.get("texts", []):
        if item.get("text"):
            blocks.append(
                {
                    "ref": item["self_ref"],
                    "text": item["text"],
                    "provenance": item.get("prov", []),
                }
            )
    for table in document.get("tables", []):
        for index, cell in enumerate(table["data"]["table_cells"]):
            if cell.get("text"):
                blocks.append(
                    {
                        "ref": f"{table['self_ref']}/data/table_cells/{index}",
                        "text": cell["text"],
                        "provenance": table.get("prov", []),
                    }
                )
    return blocks


def pdf_evidence(execution, root, descriptor):
    raw = verified_content(root, descriptor)
    from io import BytesIO

    source = PdfReader(BytesIO(raw))
    document = load_json(execution["result"]["json_path"])
    blocks = output_blocks(document)
    pages = sorted(int(page) for page in document.get("pages", {}))
    source_pages = len(source.pages)
    anchors = []
    for page_index, page in enumerate(source.pages, start=1):
        lines = [line.strip() for line in (page.extract_text() or "").splitlines()]
        found = None
        for line in lines:
            fragment = line[:100]
            needle = normalized(fragment)
            if len(needle) < 12:
                continue
            for block in blocks:
                if not any(prov.get("page_no") == page_index for prov in block["provenance"]):
                    continue
                if needle in normalized(block["text"]):
                    found = {
                        "source_page": page_index,
                        "source_excerpt": fragment,
                        "docling_ref": block["ref"],
                        "docling_provenance": block["provenance"],
                    }
                    break
            if found:
                break
        if found:
            anchors.append(found)
    return {
        "source_page_count": source_pages,
        "docling_page_numbers": pages,
        "source_pages_with_native_text": [
            index for index, page in enumerate(source.pages, start=1) if page.extract_text()
        ],
        "matched_page_anchors": anchors,
        "page_counts_match": pages == list(range(1, source_pages + 1)),
        "page_evidence_found": bool(anchors),
        "source_page_preview": execution["result"]["source_page_preview"],
        "visual_confirmation": "manual_review_required",
        "coverage_note": "Page and sampled native text checked; reading order and tables not established.",
        "unknown_scope": [
            "Only a sampled native-text anchor was compared with the original page.",
            "NativePdfPipeline does not infer layout, table structure or reading order.",
        ],
    }


class VisibleHtml(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.stack = []
        self.segments = []
        self.visible_offset = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        style = attributes.get("style", "").replace(" ", "").lower()
        hidden = (
            tag in {"head", "script", "style", "template"}
            or "hidden" in attributes
            or "display:none" in style
            or "visibility:hidden" in style
        )
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.stack.append((tag, hidden))
            self.hidden += int(hidden)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                removed = self.stack[index:]
                self.hidden -= sum(int(hidden) for _, hidden in removed)
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.hidden:
            return
        text = " ".join(data.replace("\xa0", " ").split())
        if not text:
            return
        start = self.visible_offset
        self.visible_offset += len(text)
        self.segments.append({"start": start, "end": self.visible_offset, "text": text})
        self.visible_offset += 1


def html_evidence(execution, root, manifest, html_record):
    original = next(
        record for record in manifest["materials"] if record["kind"] == "original_email"
    )
    raw_eml = verified_content(root, original["content"])
    message = BytesParser(policy=policy.default).parsebytes(raw_eml)
    mime_path = html_record["metadata"]["mime_path"]
    prefix = original["email_ref_id"] + ".mime"
    if not mime_path.startswith(prefix):
        raise ValueError("HTML MIME path is not bound to the original email")
    path_suffix = mime_path[len(prefix) :].strip(".")
    part = message
    for index in (int(value) for value in path_suffix.split(".") if value):
        part = part.get_payload()[index]
    source_html = part.get_payload(decode=True)
    charset = part.get_content_charset() or "us-ascii"
    text = source_html.decode(charset, errors="strict")
    expected = text.encode("utf-8")
    if not expected.startswith(b"\xef\xbb\xbf"):
        expected = b"\xef\xbb\xbf" + expected
    represented = verified_content(root, html_record["content"])
    if represented != expected:
        raise ValueError("Derived HTML differs from the MIME text and BOM rule")
    parser = VisibleHtml()
    parser.feed(text)
    document = load_json(execution["result"]["json_path"])
    blocks = output_blocks(document)
    searchable = [(block, normalized(block["text"])) for block in blocks]
    aggregate = "".join(value for _, value in searchable)
    segments = [segment for segment in parser.segments if normalized(segment["text"])]
    checked = []
    for segment in segments:
        needle = normalized(segment["text"])
        matches = [block["ref"] for block, value in searchable if needle in value]
        checked.append(
            {
                "source_visible_start": segment["start"],
                "source_visible_end": segment["end"],
                "excerpt": segment["text"][:200],
                "output_refs": matches,
                "found_across_output_blocks": bool(matches) or needle in aggregate,
            }
        )
    anchor_candidates = [item for item in checked if len(normalized(item["excerpt"])) >= 12]
    if not anchor_candidates:
        raise ValueError("No substantial visible HTML text is available for comparison")
    anchors = []
    for label, target in (
        ("beginning", 0),
        ("middle", parser.visible_offset // 2),
        ("end", parser.visible_offset),
    ):
        anchor = dict(
            min(
                anchor_candidates,
                key=lambda item: abs(
                    (item["source_visible_start"] + item["source_visible_end"]) // 2 - target
                ),
            )
        )
        anchor["region"] = label
        anchors.append(anchor)
    missing = [segment for segment in checked if not segment["found_across_output_blocks"]]
    return {
        "parent_material_id": html_record["parent_material_id"],
        "original_email_sha256": original["content"]["sha256"],
        "mime_path": mime_path,
        "mime_source_sha256": sha256(source_html),
        "derived_html_sha256": sha256(represented),
        "derived_html_bytes": len(represented),
        "source_charset": charset,
        "bom_and_complete_mime_text_match": True,
        "comparison_basis": "visible text-node projection; alphanumeric normalized; not a rendered page",
        "unknown_scope": [
            "CSS-rendered visibility, images and visual layout were not checked.",
            "Text-node matching does not establish reading order or duplicate occurrence counts.",
        ],
        "visible_projection_characters": parser.visible_offset,
        "eligible_segments": len(checked),
        "missing_segments": missing,
        "anchors": anchors,
        "page_location": None,
    }


def record_assessment(result, execution, evidence, passed):
    execution["validation"] = evidence
    execution["coverage"]["actual_scope"] = evidence
    if not passed and execution["execution_state"] == "success":
        execution["execution_state"] = "partial_success"
        for material in result["inventory"]:
            if material["execution_id"] == execution["execution_id"]:
                material["read_state"] = "partial_success"
                material["reason"] = "source_comparison_has_gaps"


def original_pdf_probe(manifest_path, root):
    from io import BytesIO

    manifest = load_json(manifest_path)
    original = next(record for record in manifest["materials"] if record["material_id"] == "PDF")
    reader = PdfReader(BytesIO(verified_content(root, original["content"])))
    return {
        "material_id": "PDF",
        "source_sha256": original["content"]["sha256"],
        "pages": len(reader.pages),
        "pages_with_native_text": sum(bool(page.extract_text()) for page in reader.pages),
    }


def configurations(
    *,
    cache_path=None,
    sheet_name="CI A",
    declared_versions=None,
    pdf_text_cell_unit="line",
    pdf_page_range=None,
    docling_text=False,
    pdf_first_read="native",
):
    if pdf_first_read not in {"native", "packing_table", "scanned_ocr"}:
        raise ValueError("Unsupported PDF first-read profile")
    if pdf_first_read != "native" and (pdf_page_range is not None or pdf_text_cell_unit != "line"):
        raise ValueError("Structured PDF first-read profile reads the complete document once")
    if pdf_text_cell_unit not in {"line", "word"}:
        raise ValueError("Unsupported native PDF text-cell unit")
    if pdf_page_range is not None and (
        not isinstance(pdf_page_range, (list, tuple))
        or len(pdf_page_range) != 2
        or any(type(page) is not int or page < 1 for page in pdf_page_range)
        or pdf_page_range[0] != pdf_page_range[1]
    ):
        raise ValueError("Native PDF supplement must select exactly one positive page")
    if pdf_text_cell_unit == "word" and pdf_page_range is None:
        raise ValueError("Word-level native PDF reading requires one explicit page")
    declared = (
        declared_versions
        if declared_versions is not None
        else {
            "python": "3.12.13",
            "docling": "2.145.0",
            "docling_core": "2.104.0",
            "docling_parse": "7.23.0",
            "openpyxl": "3.1.5",
            "pypdfium2": "5.14.0",
        }
    )
    common = {
        "cache_path": str(MODEL_CACHE if cache_path is None else cache_path),
        "device": "cpu",
        "threads": 2,
        "offline": True,
        "enable_remote_services": False,
        "allow_external_plugins": False,
        "model_weights_used": [],
    }
    return {
        ("docling", "pdf"): {
            "reader": {"packing_table": PDF_STRUCTURED, "scanned_ocr": PDF_SCANNED}.get(
                pdf_first_read, "DoclingNativePdf"
            ),
            "versions": declared,
            "options": (
                {
                    **common,
                    "pipeline": "StandardPdfPipeline",
                    "do_ocr": pdf_first_read == "scanned_ocr",
                    "do_table_structure": True,
                    **(
                        {"force_full_page_ocr": True, "ocr_engine": "RapidOCR"}
                        if pdf_first_read == "scanned_ocr"
                        else {}
                    ),
                    "table_mode": "accurate",
                    "text_cell_unit": "line",
                    "generate_page_images": False,
                    "generate_picture_images": False,
                }
                if pdf_first_read != "native"
                else {
                    **common,
                    "pipeline": "NativePdfPipeline",
                    "parser_threads": 2,
                    "ocr": "not_used",
                    "layout_model": "not_used",
                    "table_structure_model": "not_used",
                    "generate_page_images": False,
                    "generate_picture_images": False,
                    "text_cell_unit": pdf_text_cell_unit,
                    "verification_preview": {"renderer": "pypdfium2", "page": 1, "scale": 1.5},
                }
            ),
            "requested_scope": {
                "kind": "page_range" if pdf_page_range else "all_pages",
                "page_range": list(pdf_page_range) if pdf_page_range else None,
            },
        },
        ("docling", "xlsx"): {
            "reader": "DoclingExcel",
            "versions": declared,
            "options": {
                **common,
                "pipeline": "SimplePipeline",
                "sheet_name": sheet_name,
                "parse_charts": False,
                "formula_evaluation": False,
                "enable_local_fetch": False,
            },
            "requested_scope": {"kind": "worksheets", "sheet_names": [sheet_name]},
        },
        ("docling", "html"): {
            "reader": "DoclingHtml",
            "versions": declared,
            "options": {
                **common,
                "pipeline": "SimplePipeline",
                "render_page": False,
                "fetch_images": False,
                "enable_local_fetch": False,
                "infer_furniture": False,
                "add_title": False,
            },
            "requested_scope": {"kind": "full_representation"},
        },
        ("docling", "markdown"): {
            "reader": "DoclingMarkdown",
            "versions": declared,
            "options": {
                **common,
                "pipeline": "SimplePipeline",
                "fetch_images": False,
                "enable_remote_fetch": False,
                "enable_local_fetch": False,
            },
            "requested_scope": {"kind": "full_representation"},
        },
        ("text", "text"): {
            "reader": "DoclingText" if docling_text else "TextReader",
            "versions": declared
            if docling_text
            else {"python": platform.python_version(), "text_reader": TEXT_READER_VERSION},
            "options": (
                {**common, "pipeline": "DoclingTextAdapter", "encoding": "utf-8-sig"}
                if docling_text
                else {"encoding": "utf-8-sig", "character_unit": "unicode_codepoint"}
            ),
            "requested_scope": {"kind": "full_representation"},
        },
    }


def only_execution(result):
    if len(result["executions"]) != 1:
        raise ValueError("Expected one selected plan")
    return result["executions"][0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run_root = args.output.resolve() / f"run-{uuid4().hex}"
    run_root.mkdir(parents=True, exist_ok=False)
    p01_report = load_json(P01_REPORT)
    paths = {
        name: {
            "manifest": Path(p01_report[name]["manifest"]),
            "root": Path(p01_report[name]["manifest"]).parents[1],
        }
        for name in ("first", "second", "third")
    }
    original_hashes = {
        name: sha256(value["manifest"].read_bytes()) for name, value in paths.items()
    }
    actual_versions = worker({"operation": "probe"})
    configs = configurations()
    missing_config = parse_prepared(
        paths["second"]["manifest"],
        paths["second"]["root"],
        expected_task_id="p01-real-partial",
        expected_request_fingerprint=None,
        intake_context=None,
        selected_material_ids={"dual"},
        configurations={},
        versions=actual_versions,
        output_dir=run_root / "unbound-check",
    )
    check = only_execution(missing_config)
    if check["execution_state"] != "not_started" or check["reader_started"]:
        raise AssertionError("Unbound plan was started")

    original_pdf = original_pdf_probe(paths["first"]["manifest"], paths["first"]["root"])
    if original_pdf["pages_with_native_text"]:
        raise ValueError("The 01-fetch PDF has native text; replacement is not justified")
    replacement_root = run_root / "indexed-p01"
    replacement, replacement_source = indexed_native_pdf(replacement_root)
    cases = {}
    cases["pdf"] = parse_prepared(
        replacement["manifest_path"],
        replacement_root,
        expected_task_id="p02-indexed-native-pdf",
        expected_request_fingerprint=replacement["request_fingerprint"],
        intake_context=replacement["intake_context"],
        selected_material_ids={"NATIVE_PDF"},
        configurations=configs,
        versions=actual_versions,
        output_dir=run_root / "pdf",
    )
    cases["xlsx"] = parse_prepared(
        paths["first"]["manifest"],
        paths["first"]["root"],
        expected_task_id="p01-real-fetch",
        expected_request_fingerprint=None,
        intake_context=None,
        selected_material_ids={"XLSX"},
        configurations=configs,
        versions=actual_versions,
        output_dir=run_root / "xlsx",
    )
    third_manifest = load_json(paths["third"]["manifest"])
    html_record = next(
        item
        for item in third_manifest["materials"]
        if item["kind"] == "body" and item["format"] == "html"
    )
    cases["html"] = parse_prepared(
        paths["third"]["manifest"],
        paths["third"]["root"],
        expected_task_id="p01-real-eml",
        expected_request_fingerprint=None,
        intake_context=None,
        selected_material_ids={html_record["material_id"]},
        configurations=configs,
        versions=actual_versions,
        output_dir=run_root / "html",
    )
    cases["text"] = parse_prepared(
        paths["second"]["manifest"],
        paths["second"]["root"],
        expected_task_id="p01-real-partial",
        expected_request_fingerprint=None,
        intake_context=None,
        selected_material_ids={"dual"},
        configurations=configs,
        versions=actual_versions,
        output_dir=run_root / "text",
    )

    pdf_run = only_execution(cases["pdf"])
    if pdf_run["execution_state"] in {"success", "partial_success"}:
        replacement_manifest = load_json(replacement["manifest_path"])
        descriptor = next(
            item["content"]
            for item in replacement_manifest["materials"]
            if item["material_id"] == "NATIVE_PDF"
        )
        evidence = pdf_evidence(pdf_run, replacement_root, descriptor)
        record_assessment(
            cases["pdf"],
            pdf_run,
            evidence,
            evidence["page_counts_match"] and evidence["page_evidence_found"],
        )

    excel_run = only_execution(cases["xlsx"])
    if excel_run["execution_state"] in {"success", "partial_success"}:
        evidence = excel_run["result"]["excel_evidence"]
        record_assessment(cases["xlsx"], excel_run, evidence, evidence["all_sampled_cells_match"])

    html_run = only_execution(cases["html"])
    if html_run["execution_state"] in {"success", "partial_success"}:
        evidence = html_evidence(html_run, paths["third"]["root"], third_manifest, html_record)
        record_assessment(cases["html"], html_run, evidence, not evidence["missing_segments"])

    text_run = only_execution(cases["text"])
    if text_run["execution_state"] in {"success", "partial_success"}:
        item = next(
            record for record in cases["text"]["inventory"] if record["material_id"] == "dual"
        )
        if item["representation"] != "provided_text" or item["original_sha256"] is not None:
            raise AssertionError("Provided text was treated as an original file")
        text_run["coverage"]["actual_scope"] = {
            "location": text_run["result"]["location"],
            "page_location": None,
            "original_coverage": "unknown",
        }

    unchanged = {
        name: sha256(value["manifest"].read_bytes()) == original_hashes[name]
        for name, value in paths.items()
    }
    if not all(unchanged.values()):
        raise ValueError("A P01 manifest changed during local READ")
    report = {
        "kind": "local_read_evidence_not_formal_handoff",
        "docling_runtime_versions": actual_versions,
        "original_pdf_probe": original_pdf,
        "replacement_source": replacement_source,
        "unbound_check": check,
        "cases": cases,
        "p01_manifest_hashes_unchanged": unchanged,
        "limits": [
            "Local-only READ; no ANALYZE, business fields or formal output projection.",
            "Native PDF pipeline uses no OCR, layout model, table model or model weights.",
            "HTML comparison checks text nodes, not a browser-rendered visual page.",
            "Workbook formula calculation, hidden-sheet reading and complete cell coverage are not established.",
            "Prior P01 intake contexts are unavailable during replay; original-source verification remains a gap.",
        ],
    }
    report_path = run_root / "report.json"
    with report_path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    summary = {
        "report": str(report_path),
        "unbound_check": check["reason"],
        "p01_manifest_hashes_unchanged": unchanged,
        "cases": {
            name: {
                "execution_state": only_execution(case)["execution_state"],
                "conversion_status": only_execution(case)
                .get("result", {})
                .get("conversion_status"),
                "json_path": only_execution(case).get("result", {}).get("json_path"),
            }
            for name, case in cases.items()
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
